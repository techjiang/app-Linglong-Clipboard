"""列表里的单条卡片。

用自定义 widget 而非 `QListWidgetItem` 的文本绘制，
因为要同时呈现「类型色条 + 摘要 + 多行预览 + 来源与时间」，
纯文本项做不到这种信息密度。
"""

from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.models import ClipboardItem
from ..core.theme_spec import (  # noqa: F401 - 转出给既有调用方
    DEFAULT_DENSITY,
    DENSITY_OPTIONS,
    DENSITY_VALUES,
    density_option,
    is_valid_density,
    normalize_density,
)
from .theme import KIND_COLORS, Palette

#: 卡片里预览文本显示的最大行数
PREVIEW_LINES = 2

#: 密度档位与它们的含义定义在 `core/theme_spec.py`（单一来源）：
#: 卡片、设置页下拉、配置校验、`--ui-status` 读的都是那一份。
#: 这里只保留两个惯用别名，避免既有调用方全线改名。
#:
#: 密度只影响**展示**，不影响任何采集/检索行为 —— 切换后历史一条不少，
#: 只是列表看起来更密。
DENSITY_COMFORTABLE = DEFAULT_DENSITY
DENSITY_COMPACT = "compact"


def humanize_time(timestamp: float, *, now: Optional[float] = None) -> str:
    """把时间戳转成「刚刚 / 5 分钟前 / 昨天 14:30」这类口语表达。"""
    if not timestamp:
        return ""
    now = now or time.time()
    delta = max(0.0, now - timestamp)
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    local = time.localtime(timestamp)
    stamp = time.strftime("%H:%M", local)
    days = int(delta // 86400)
    if days == 1:
        return f"昨天 {stamp}"
    if days < 7:
        return f"{days} 天前"
    return time.strftime("%Y-%m-%d", local)


def humanize_size(size: int) -> str:
    if size <= 0:
        return ""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


class ItemCard(QFrame):
    """一条记录的卡片视图。"""

    double_clicked = Signal(int)
    context_requested = Signal(int, object)

    def __init__(
        self,
        item: ClipboardItem,
        palette: Palette,
        parent: QWidget = None,
        *,
        density: str = DENSITY_COMFORTABLE,
    ) -> None:
        super().__init__(parent)
        self._item = item
        self._palette_theme = palette
        self._density = normalize_density(density)
        self.setObjectName("Card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        accent = KIND_COLORS.get(item.kind, palette.accent)
        self.setStyleSheet(
            f"QFrame#Card {{ border-left: 3px solid {accent}; }}"
        )
        self._build_ui()
        self.setToolTip(self._tooltip_text())

    @property
    def item_id(self) -> Optional[int]:
        return self._item.id

    @property
    def is_compact(self) -> bool:
        return self._density == DENSITY_COMPACT

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        if self.is_compact:
            # 竖向留白是卡片高度的主要来源，压缩它比缩小字号更有效
            outer.setContentsMargins(9, 4, 9, 4)
            outer.setSpacing(1)
        else:
            outer.setContentsMargins(11, 8, 11, 8)
            outer.setSpacing(4)

        # 第一行：类型徽标 + 标记 + 来源
        head = QHBoxLayout()
        head.setSpacing(7)

        kind_label = QLabel(self._item.kind_label)
        kind_label.setObjectName("CardKind")
        kind_label.setStyleSheet(
            f"color: {KIND_COLORS.get(self._item.kind, self._palette_theme.accent)};"
        )
        head.addWidget(kind_label)

        if self._item.pinned:
            pin = QLabel("📌")
            pin.setObjectName("CardPin")
            pin.setToolTip("已固定，不会被自动清理")
            head.addWidget(pin)
        if self._item.favorite:
            star = QLabel("⭐")
            star.setToolTip("已收藏")
            head.addWidget(star)
        if self._item.sensitive:
            shield = QLabel("🛡")
            shield.setToolTip("包含敏感信息")
            head.addWidget(shield)

        head.addStretch(1)

        if self._item.device:
            device = QLabel(self._item.device)
            device.setObjectName("CardMeta")
            head.addWidget(device)

        outer.addLayout(head)

        # 第二行：预览正文
        self._body = QLabel()
        self._body.setObjectName("CardTitle")
        self._body.setWordWrap(False)
        self._body.setTextFormat(Qt.TextFormat.PlainText)
        self._body.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._body.setText(self._preview_text())
        outer.addWidget(self._body)

        if self.is_compact:
            # 紧凑模式只保留前两行：来源/时间/标签这些信息在悬停提示里仍有，
            # 不为了「一屏多看几条」把它们删掉，只是不在行内占高度。
            self._apply_elide()
            return

        # 第三行：元信息
        meta = QHBoxLayout()
        meta.setSpacing(9)
        source = QLabel(self._item.display_source)
        source.setObjectName("CardMeta")
        meta.addWidget(source)

        stamp = QLabel(humanize_time(self._item.created_at))
        stamp.setObjectName("CardMeta")
        meta.addWidget(stamp)

        if self._item.tags:
            tags = QLabel(" ".join(f"#{tag}" for tag in self._item.tags[:4]))
            tags.setObjectName("CardMeta")
            meta.addWidget(tags)

        meta.addStretch(1)

        size_text = humanize_size(self._item.size)
        if size_text:
            size = QLabel(size_text)
            size.setObjectName("CardMeta")
            meta.addWidget(size)

        if self._item.use_count > 1:
            used = QLabel(f"使用 {self._item.use_count} 次")
            used.setObjectName("CardMeta")
            meta.addWidget(used)

        outer.addLayout(meta)
        self._apply_elide()

    def _preview_text(self) -> str:
        """把内容压成单行摘要，并留出与卡片宽度无关的安全长度。"""
        if self._item.kind == "image":
            width = self._item.image_width
            height = self._item.image_height
            if width and height:
                return f"图片  {width} × {height}"
            return "图片"
        if self._item.kind == "file" and self._item.files:
            if len(self._item.files) == 1:
                return self._item.files[0]
            head = self._item.files[0]
            return f"{head}   等 {len(self._item.files)} 个文件"
        if self._item.kind == "color":
            return self._item.content
        text = self._item.content or self._item.preview
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if not lines:
            return "(空)"
        return "  ".join(lines[:PREVIEW_LINES])[:400]

    def _tooltip_text(self) -> str:
        lines = [
            f"类型：{self._item.kind_label}",
            f"来源：{self._item.display_source}",
        ]
        if self._item.source_title:
            lines.append(f"窗口：{self._item.source_title}")
        lines.append(f"时间：{humanize_time(self._item.created_at)}")
        if self._item.size:
            lines.append(f"大小：{humanize_size(self._item.size)}")
        lines.append("")
        lines.append("双击复制 · 右键更多操作")
        return "\n".join(lines)

    # ---- 交互 ----

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._item.id is not None:
            self.double_clicked.emit(self._item.id)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._item.id is not None:
            self.context_requested.emit(self._item.id, event.globalPos())
        event.accept()

    def _apply_elide(self) -> None:
        """按可用宽度截断预览，超出部分用省略号。

        这里不用 `QLabel.setWordWrap(True)` 自动换行，
        因为卡片高度会因此浮动，列表滚动时行高不一会显得凌乱。
        """
        metrics = QFontMetrics(self._body.font())
        available = max(120, self.width() - 30)
        self._body.setText(
            metrics.elidedText(self._preview_text(), Qt.TextElideMode.ElideRight, available)
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._apply_elide()
