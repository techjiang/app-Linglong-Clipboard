"""Qt 剪贴板适配器。

把 `QClipboard` 的 MIME 数据转成 `ClipboardItem`，并负责写回。
图片以 PNG 落盘（同尺寸下比 BMP/QImage 序列化小得多），
数据库只存相对路径，换机器/换目录后仍可定位。
"""

from __future__ import annotations

import base64
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

    def __init__(
        self,
        clipboard: QClipboard,
        images_dir: Path,
        backend=None,
        on_image_saved=None,
    ) -> None:
        self._clipboard = clipboard
        self._images_dir = Path(images_dir)
        self._images_dir.mkdir(parents=True, exist_ok=True)
        self._backend = backend
        self._last_image_hash = ""
        # 图片落盘后的回调：JSON 后端借此把图片内联进历史文件
        self._on_image_saved = on_image_saved

    # ---- 读取 ----

    def read_clipboard(self, *, keep_formats: bool = True) -> Optional[ClipboardItem]:
        """读取当前剪贴板。

        必须整体在 GUI 线程执行：`QClipboard.mimeData()` 返回的是内部快照，
        跨线程访问会拿到空数据。采集线程只负责触发，
        真正的读取由主线程完成（见 `ui.application` 的线程调度）。

        `keep_formats=False` 时跳过「收下各种形式」这一步（见
        `core/clipboard_formats.py`）：记录里只会留下我们直接用的那一份，
        代价是复制出站时拿不回原来的 HTML / 原始字节。
        """
        mime: Optional[QMimeData] = self._clipboard.mimeData()
        if mime is None:
            return None

        # 顺序有讲究：文件的优先级高于图片 ——
        # 从文件管理器复制图片时两种格式同时存在，此时应记为「文件」。
        item = self._from_urls(mime)
        if item is not None:
            item.metadata.setdefault(
                "formats", self._collect_formats(mime, text=item.content).to_metadata()
            )
            return item

        item = self._from_image(mime)
        if item is not None:
            return item

        item = self._from_text(mime)
        if item is not None:
            item.metadata.setdefault(
                "formats", self._collect_formats(mime, text=item.content).to_metadata()
            )
        return item

    def _collect_formats(self, mime: QMimeData, *, text: str):
        """把系统实际给出的形式收下来，供复制出站时原样摆回去。

        这件事必须在**采集那一刻**做：`metadata` 只有那时才看得到真实的
        `QMimeData` 快照，之后系统剪贴板早被下一次复制覆盖了。
        """
        from ..core.clipboard_formats import collect_formats

        def _text_of(name: str) -> str:
            if not mime.hasFormat(name):
                return ""
            try:
                raw = bytes(mime.data(name))
            except Exception:  # noqa: BLE001 - 取不到就当没有，采集不能因此失败
                _log.debug("读取剪贴板格式失败：%s", name, exc_info=True)
                return ""
            return raw.decode("utf-8", "replace")

        html = self._html_of(mime)
        other: List[str] = []
        for name in mime.formats():
            name = str(name)
            if name in ("text/plain", "text/html", "text/rtf", "application/rtf"):
                continue
            if name.startswith("application/x-qt-") or name in _INTERNAL_MIME_PREFIXES:
                # Qt 自己的内部标记（如 `application/x-qt-windows-mime;...`）不是内容
                continue
            other.append(name)

        return collect_formats(
            text=text,
            html=html,
            rtf=_text_of("text/rtf") or _text_of("application/rtf"),
            markdown=_text_of("text/markdown") or _text_of("text/x-markdown"),
            uri_list=_text_of("text/uri-list"),
            files=[url.toLocalFile() for url in mime.urls() if url.toLocalFile()],
            other_names=other,
        )

    def _html_of(self, mime: QMimeData) -> str:
        """取 HTML 那份内容。

        `mime.html()` 有时给空（部分应用只把 HTML 放在自己的格式名里），
        所以优先读 `text/html` 的原始字节，拿不到再退回 `mime.html()`。
        """
        if mime.hasHtml():
            try:
                return mime.html()
            except Exception:  # noqa: BLE001
                pass
        if mime.hasFormat("text/html"):
            try:
                return bytes(mime.data("text/html")).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return ""
        return ""

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

        self._notify_image_saved(relative=f"{self._images_dir.name}/{filename}", data=png)
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
            try:
                text = mime.text()
            except Exception:  # noqa: BLE001 - 取不到就当没有文本，别有更坏的结果
                text = ""
        html = self._html_of(mime) if has_html else ""

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
            hash=_text_fingerprint(text, html),
            size=len((text or html).encode("utf-8")),
        )
        item.metadata["text_bytes"] = _encode_text_bytes(mime, text)
        return item

    def _notify_image_saved(self, *, relative: str, data: bytes) -> None:
        """回调失败不能影响采集：图片已经写好了，内联只是增强。"""
        if self._on_image_saved is None:
            return
        try:
            self._on_image_saved(relative, data)
        except Exception:  # noqa: BLE001
            _log.exception("图片落盘回调失败")

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

    def write_item(
        self,
        item: ClipboardItem,
        *,
        plain_text_only: bool = False,
        exclude_from_history: bool = False,
        use_stored_formats: bool = True,
    ) -> bool:
        """把一条记录写回系统剪贴板。

        `plain_text_only=True` 时优先写纯文本：粘贴到终端等场景下，
        带 HTML 的剪贴板会被插入大量样式噪音。

        `exclude_from_history=True` 时附带系统级的「不进系统剪贴板历史」标记。
        这件事**只能在写入时做**：内容一旦被系统历史记下，就没有任何接口
        能把它取出来。所以隐私相关的出站都走这个开关，而不是事后补救。

        `use_stored_formats=True` 时把采集那一刻收下的形式（HTML、RTF、
        原始字节…）一并摆回剪贴板，让收件应用自己挑 —— 这是「复制一段带链接
        的正文，粘出来链接还在」和「粘贴结果与原文逐字节一致」的实现方式。
        纯文本优先（`plain_text_only`）或调用方明确不要时跳过。
        """
        mime = QMimeData()
        if exclude_from_history:
            self._mark_excluded(mime)

        # 图片与文件的「形式」就是字节本身，不走形式回放那条路
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

        # 富文本类载荷：把采集时收下来的形式摆回去，让收件应用自己挑
        if use_stored_formats and not plain_text_only:
            if self._attach_stored_formats(mime, item):
                self._clipboard.setMimeData(mime)
                return True

        # 纯文本分支也必须复用同一个 mime。
        # 此前这里走的是 `self.set_text(item.content)`，把刚挂上的排除格式
        # 连同 mime 一起丢掉了 —— 结果是「标记逻辑写了，但只对带 HTML 的
        # 内容生效」，而敏感内容恰好绝大多数都是纯文本。
        text = _plain_text_of(item)
        if plain_text_only or not item.html:
            mime.setText(text)
            self._apply_exact_bytes(mime, item, plain_text_only=plain_text_only)
            self._clipboard.setMimeData(mime)
            return True

        mime.setHtml(item.html)
        mime.setText(text)
        self._apply_exact_bytes(mime, item, plain_text_only=plain_text_only)
        self._clipboard.setMimeData(mime)
        return True

    @staticmethod
    def _apply_exact_bytes(
        mime: QMimeData, item: ClipboardItem, *, plain_text_only: bool = False
    ) -> None:
        """把 `text/plain` 还原成采集时的精确字节。

        直接 `mime.setText(content)` 时 Qt 会按平台规矩处理行尾，用户看到的
        结果是「粘贴出来末尾多了一行」，而且只在部分应用里出现 —— 很难归因。
        这里把原始字节原样放回 `text/plain`，粘贴出去与原文逐字节一致。
        `plain_text_only` 时不做，因为那个入口的语义就是「只要干净的一行文本」。
        """
        if plain_text_only:
            return
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        raw = decode_text_bytes(str(metadata.get("text_bytes") or ""))
        if raw:
            mime.setData("text/plain", QByteArray(raw.encode("utf-8")))

    def _attach_stored_formats(self, mime: QMimeData, item: ClipboardItem) -> bool:
        """把存下来的形式挂到 `mime` 上，返回是否挂上了东西。

        至少要有「一份比纯文本多出东西的形式」才算挂上 —— 只有
        `text/plain` 的话走普通文本分支即可，没必要绕这一圈。
        """
        from ..core.clipboard_formats import ClipboardFormats, preferred_plain_text

        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        formats = ClipboardFormats.from_metadata(metadata.get("formats"))
        if formats.is_empty:
            return False

        plain, source = preferred_plain_text(
            content=item.content,
            text_bytes=decode_text_bytes(str(metadata.get("text_bytes") or "")),
            markdown=formats.markdown(),
        )
        extra = bool(formats.html() or formats.rtf() or formats.data.get("text/uri-list"))
        if not extra and source != "bytes":
            return False

        html = formats.html()
        rtf = formats.rtf()
        if html:
            mime.setHtml(html)
        if rtf:
            mime.setData("text/rtf", QByteArray(rtf.encode("utf-8", "replace")))
        uri_list = formats.data.get("text/uri-list", "")
        if uri_list:
            mime.setData("text/uri-list", QByteArray(uri_list.encode("utf-8", "replace")))
        if plain:
            mime.setText(plain)
        if source == "bytes" and plain:
            # 精确字节：粘贴出去与原文逐字节一致，不含 Qt 按平台补出来的行尾
            mime.setData("text/plain", QByteArray(plain.encode("utf-8")))
        return True

    def set_text(self, text: str, *, exclude_from_history: bool = False) -> None:
        """写纯文本。`exclude_from_history` 时带上系统排除标记。

        带标记时走 `setMimeData` 而不是 `setText`：后者无法携带自定义
        格式，系统就看不到「别记这条」的声明。
        """
        if exclude_from_history:
            mime = QMimeData()
            mime.setText(text)
            if self._mark_excluded(mime):
                self._clipboard.setMimeData(mime)
                return
        self._clipboard.setText(text, QClipboard.Mode.Clipboard)

    # ---- 系统剪贴板历史排除 ----

    def _exclusion_formats(self) -> List[str]:
        """本平台声明的「不进系统剪贴板历史」格式名。

        能力探测失败返回空列表而不是抛出：探测不该拦住写入 ——
        内容是用户明确要复制的，这里失败的结果最多是「没挂上标记」。
        """
        getter = getattr(self._backend, "history_exclusion_formats", None)
        if getter is None:
            return []
        try:
            return [str(name) for name in (getter() or []) if name]
        except Exception:  # noqa: BLE001
            return []

    def _mark_excluded(self, mime: QMimeData) -> bool:
        """给剪贴板数据挂上排除格式，返回是否挂上了。

        使用 `QClipboard` 而不是自己调 `OpenClipboard`：Qt 写入时会
        自己开关剪贴板，自定义格式名会经 `RegisterClipboardFormatW`
        映射过去。这样不必和 Qt 抢剪贴板的所有权 —— 抢的结果通常是
        两边都写不进去。

        格式的**值必须为空**：系统只检查格式是否存在。
        塞入真实数据反而会变成一块内容，被历史当成条目存下来。
        """
        names = self._exclusion_formats()
        for name in names:
            mime.setData(name, QByteArray())
        return bool(names)

    def clear(self) -> None:
        self._clipboard.clear(QClipboard.Mode.Clipboard)

    def _load_image(self, relative: str) -> Optional[QImage]:
        """按「内联数据 -> 绝对路径 -> 数据目录下的相对路径 -> 相对 CWD」依次定位。

        库里存的是相对数据目录的路径，但调用方也可能传入绝对路径，
        两种都要支持；必须用绝对路径交给 QImage，否则会随 CWD 变化而找不到文件。
        JSON 后端把图片内联成 data URI，这种值本身就是完整载荷，不能再当路径拼。
        """
        from ..core.repository import is_data_uri
        from ..utils.paths import data_dir

        if is_data_uri(relative):
            image = QImage.fromData(QByteArray(base64.b64decode(relative.partition(",")[2])))
            if not image.isNull():
                return image
            _log.debug("内联图片解码失败")
            return None

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


#: 这些 MIME 前缀是 Qt / 桌面环境自己的簿记，不是内容形式
_INTERNAL_MIME_PREFIXES = (
    "application/x-qt-",
    "application/x-qt",
    "x-special/",
    "COMPOUND_TEXT",
    "TARGETS",
    "MULTIPLE",
    "TIMESTAMP",
    "SAVE_TARGETS",
)


def _plain_text_of(item: ClipboardItem) -> str:
    """这条记录「作为纯文本」该是什么。

    Markdown 记录优先给 Markdown 原文：用户从 GitHub 复制的 README，
    粘出去时应当还是带 `#` 的 Markdown，而不是被 Qt 转换过的版本。
    """
    from ..core.clipboard_formats import ClipboardFormats, preferred_plain_text

    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    formats = ClipboardFormats.from_metadata(metadata.get("formats"))
    text, _ = preferred_plain_text(content=item.content, markdown=formats.markdown())
    return text


def _encode_text_bytes(mime: QMimeData, text: str) -> str:
    """把 `text/plain` 的**精确字节**收下来（base64）。

    为什么不用 `item.content` 代替：`QMimeData.text()` 会把行分隔符统一成
    `\n`，而 Windows 应用放进去的是 `CRLF`。写回时 Qt 再按平台规矩补一次，
    用户看到的结果就是「复制一段文本，粘贴出来末尾多了一行」—— 而且
    只在某些应用里多，换一个应用又正常。

    只有和 `text` 逐字节一致（解码后）才存，否则说明这份 `text/plain`
    和 Qt 给出的文本不是一回事，存下来反而会把不一致带进粘贴结果。
    """
    if not mime.hasFormat("text/plain"):
        return ""
    try:
        raw = bytes(mime.data("text/plain"))
    except Exception:  # noqa: BLE001
        return ""
    if not raw or len(raw) > 64 * 1024:
        return ""
    decoded = raw.decode("utf-8", "replace")
    # 判定要「除了行尾之外完全一致」，而不是「前缀相同」。
    # 只做前缀比较的话，`mime.text()` 在某些平台上拿到的是**另一份**内容
    # （比如应用把摘要贴在纯文本里），前缀恰好撞上就把不一致存进去了 ——
    # 之后每一次粘贴都会带着这份不一致走出去。
    if _without_trailing_newlines(decoded) != _without_trailing_newlines(text or ""):
        return ""
    return base64.b64encode(raw).decode("ascii")


def _without_trailing_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").rstrip("\n")


def decode_text_bytes(value: str) -> str:
    """把 `metadata["text_bytes"]` 还原成文本；坏值返回空串。"""
    if not value:
        return ""
    try:
        return base64.b64decode(value).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 坏值按「没有」处理
        return ""


def _text_fingerprint(text: str, html: str) -> str:
    """纯文本/富文本的指纹口径，唯一的定义处。

    对外的门面是模块级的 `clipboard_payload()`：`ui/application.py` 写入剪贴板时
    要用同一个载荷口径算「自我写入」指纹（避免自己写出去的内容又被采集回来）。
    两处只要差一个字节标记就失效，表现是历史里凭空多出一条自己刚复制的内容 ——
    很容易被当成「采集正常」而放过。

    规则只有一条：**有纯文本就用纯文本，没有才退回整段 HTML**，
    类型随之取 `text` 或 `rich`。此前这两件事在 `_from_text` 里写成
    「先按 text 算，再在 `html and not text` 时覆盖成 rich」，
    结果与 `mark_self_write` 的口径对不上 —— 自我写入标记形同虚设。
    """
    payload = text or html
    if text:
        return content_hash("text", payload)
    return content_hash("rich", payload)


def clipboard_payload(text: str, html: str = "") -> str:
    """写入剪贴板时该拿什么去算指纹 —— 供 `mark_self_write` 复用同一口径。

    返回的是**载荷**而不是指纹：`mark_self_write()` 会拿它再算一次哈希
    （那边还要拼上类型），所以这里给指纹的话会被哈希两遍。
    """
    return text or html
