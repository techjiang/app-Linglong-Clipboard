"""Qt 剪贴板适配器。

把 `QClipboard` 的 MIME 数据转成 `ClipboardItem`，并负责写回。
图片以 PNG 落盘（同尺寸下比 BMP/QImage 序列化小得多），
数据库只存相对路径，换机器/换目录后仍可定位。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, QUrl
from PySide6.QtGui import QClipboard, QImage

from ..core.models import ClipboardItem, content_hash
from ..utils.logger import get_logger

_log = get_logger("clipboard-qt")

#: 单张图片的落盘上限，超过则放弃保存（防止把磁盘写满）
MAX_IMAGE_BYTES = 32 * 1024 * 1024


class QtClipboardAdapter:
    """实现 `ClipboardMonitor` 期望的 `read_clipboard()` 接口。"""

    def __init__(self, clipboard: QClipboard, images_dir: Path, backend=None) -> None:
        self._clipboard = clipboard
        self._images_dir = Path(images_dir)
        self._images_dir.mkdir(parents=True, exist_ok=True)
        self._backend = backend
        self._last_image_hash = ""

    # ---- 读取 ----

    def read_clipboard(self) -> Optional[ClipboardItem]:
        """读取当前剪贴板。

        必须整体在 GUI 线程执行：`QClipboard.mimeData()` 返回的是内部快照，
        跨线程访问会拿到空数据。采集线程只负责触发，
        真正的读取由主线程完成（见 `ui.application` 的线程调度）。
        """
        mime: Optional[QMimeData] = self._clipboard.mimeData()
        if mime is None:
            return None

        # 顺序有讲究：文件的优先级高于图片 ——
        # 从文件管理器复制图片时两种格式同时存在，此时应记为「文件」。
        item = self._from_urls(mime)
        if item is not None:
            return item

        item = self._from_image(mime)
        if item is not None:
            return item

        return self._from_text(mime)

    def _from_urls(self, mime: QMimeData) -> Optional[ClipboardItem]:
        if not mime.hasUrls():
            return None
        paths: List[str] = []
        for url in mime.urls():
            local = url.toLocalFile()
            if local:
                paths.append(local)
        if not paths:
            return None
        payload = "\n".join(paths)
        return ClipboardItem(
            kind="file",
            content=payload,
            files=paths,
            hash=content_hash("file", payload),
            size=sum(len(path.encode("utf-8")) for path in paths),
        )

    def _from_image(self, mime: QMimeData) -> Optional[ClipboardItem]:
        if not mime.hasImage():
            return None
        image = self._clipboard.image()
        if image is None or image.isNull():
            return None

        png = self._encode_png(image)
        if png is None:
            return None
        if len(png) > MAX_IMAGE_BYTES:
            _log.warning("图片过大（%.1fMB），跳过保存", len(png) / 1024 / 1024)
            return None

        digest = hashlib.blake2b(png, digest_size=16).hexdigest()
        # 同一张图反复复制（如截图工具连续截同一区域）只落一份文件
        filename = f"{digest}.png"
        target = self._images_dir / filename
        if not target.exists():
            try:
                target.write_bytes(png)
            except OSError as exc:
                _log.warning("图片写入失败: %s", exc)
                return None

        self._last_image_hash = digest
        relative = f"{self._images_dir.name}/{filename}"
        return ClipboardItem(
            kind="image",
            content=filename,
            preview=f"[图片 {image.width()}×{image.height()}]",
            image_path=relative,
            image_width=image.width(),
            image_height=image.height(),
            hash=digest,
            size=len(png),
        )

    def _from_text(self, mime: QMimeData) -> Optional[ClipboardItem]:
        has_html = mime.hasHtml()
        has_rtf = mime.hasFormat("text/rtf")
        text = ""
        if mime.hasText():
            text = self._clipboard.text()
        html = self._clipboard.html() if has_html else ""

        if not text and html:
            # 有些应用（如部分网页编辑器）只给 HTML，取纯文本兜底
            from PySide6.QtGui import QTextDocument

            document = QTextDocument()
            document.setHtml(html)
            text = document.toPlainText()

        if not text and not html and not has_rtf:
            return None
        if text and len(text) > 5_000_000:
            _log.warning("文本过长（%d 字符），跳过采集", len(text))
            return None

        item = ClipboardItem(
            content=text,
            html=html if has_html else "",
            rtf="(rtf)" if has_rtf else "",
            hash=content_hash("text", text or html),
            size=len((text or html).encode("utf-8")),
        )
        if html and not text:
            item.hash = content_hash("rich", html)
        return item

    @staticmethod
    def _encode_png(image: QImage) -> Optional[bytes]:
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            return None
        data: QByteArray = buffer.data()
        buffer.close()
        return bytes(data)

    # ---- 写入 ----

    def write_item(self, item: ClipboardItem, *, plain_text_only: bool = False) -> bool:
        """把一条记录写回系统剪贴板。

        `plain_text_only=True` 时优先写纯文本：粘贴到终端等场景下，
        带 HTML 的剪贴板会被插入大量样式噪音。
        """
        mime = QMimeData()

        if item.kind == "image" and item.image_path:
            image = self._load_image(item.image_path)
            if image is not None:
                mime.setImageData(image)
                self._clipboard.setMimeData(mime)
                return True

        if item.kind == "file" and item.files:
            mime.setUrls([QUrl.fromLocalFile(path) for path in item.files])
            if not plain_text_only and item.html:
                mime.setHtml(item.html)
            mime.setText(item.content)
            self._clipboard.setMimeData(mime)
            return True

        if plain_text_only or not item.html:
            self.set_text(item.content)
            return True

        mime.setHtml(item.html)
        mime.setText(item.content)
        self._clipboard.setMimeData(mime)
        return True

    def set_text(self, text: str) -> None:
        self._clipboard.setText(text, QClipboard.Mode.Clipboard)

    def clear(self) -> None:
        self._clipboard.clear(QClipboard.Mode.Clipboard)

    def _load_image(self, relative: str) -> Optional[QImage]:
        """按「绝对路径 -> 数据目录下的相对路径 -> 相对 CWD」依次定位。

        库里存的是相对数据目录的路径，但调用方也可能传入绝对路径，
        两种都要支持；必须用绝对路径交给 QImage，否则会随 CWD 变化而找不到文件。
        """
        from ..utils.paths import data_dir

        raw = Path(relative)
        candidates = [raw] if raw.is_absolute() else [data_dir() / raw, Path.cwd() / raw]
        for candidate in candidates:
            resolved = candidate.expanduser()
            if resolved.is_file():
                image = QImage(str(resolved.resolve()))
                if not image.isNull():
                    return image
        _log.debug("图片文件缺失：%s", relative)
        return None
