"""快速粘贴浮层（Ctrl+Shift+V）。

设计要点：
- 无边框 + 居中 + 跟随鼠标所在屏幕，减少视线移动；
- 单个输入框既是搜索也是编号跳转（输入 `3` 直接取第三条）；
- 列表只渲染一屏，用 `1-9` 与方向键操作，不引入滚动；
- `Esc` 关闭，失焦自动关闭 —— 浮层最忌讳「关不掉」。
"""

from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.models import ClipboardItem
from ..core.semantic import SemanticEngine
from ..core.storage import Storage
from .item_card import humanize_time
from .theme import KIND_COLORS, Palette, palette_for, toast_style

#: 浮层里最多显示几条
MAX_ROWS = 9


class QuickRow(QFrame):
    """浮层里的一行。"""

    activated = Signal(int)  # 1-based 序号

    def __init__(self, index: int, item: ClipboardItem, palette: Palette, parent=None) -> None:
        super().__init__(parent)
        self._index = index
        self.setObjectName("QuickRow")
        self.setProperty("active", "false")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(10)

        badge = QLabel(str(index))
        badge.setObjectName("QuickIndex")
        badge.setFixedWidth(16)
        badge.setStyleSheet(f"color: {KIND_COLORS.get(item.kind, palette.accent)};")
        layout.addWidget(badge)

        body = QVBoxLayout()
        body.setSpacing(1)

        text = QLabel(self._row_text(item))
        text.setObjectName("QuickText")
        text.setTextFormat(Qt.TextFormat.PlainText)
        body.addWidget(text)

        meta = QLabel(f"{item.kind_label} · {item.display_source} · {humanize_time(item.created_at)}")
        meta.setObjectName("QuickHint")
        body.addWidget(meta)
        layout.addLayout(body, 1)

        if item.pinned:
            layout.addWidget(QLabel("📌"))

    @staticmethod
    def _row_text(item: ClipboardItem) -> str:
        if item.kind == "image":
            return f"图片 {item.image_width}×{item.image_height}" if item.image_width else "图片"
        if item.kind == "file" and item.files:
            return item.files[0] if len(item.files) == 1 else f"{item.files[0]} 等 {len(item.files)} 个"
        collapsed = "  ".join(
            line.strip() for line in (item.content or "").splitlines() if line.strip()
        )
        return collapsed[:160] or "(空)"

    def set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self.activated.emit(self._index)
        event.accept()


class QuickPasteOverlay(QWidget):
    """快速粘贴浮层。"""

    item_chosen = Signal(int, bool)  # (item_id, plain_text_only)
    closed = Signal()

    def __init__(
        self,
        storage: Storage,
        engine: SemanticEngine,
        config,
        parent: QWidget = None,
    ) -> None:
        super().__init__(parent)
        self._storage = storage
        self._engine = engine
        self._config = config
        self._palette_theme = palette_for(config.get("ui.theme", "system"))
        self._items: List[ClipboardItem] = []
        self._rows: List[QuickRow] = []
        self._active = 0

        self.setWindowFlags(
            Qt.WindowType.Popup
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(560)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(90)
        self._debounce.timeout.connect(self._reload)

        self._build_ui()

    # ---- 界面 ----

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        panel = QFrame()
        panel.setObjectName("QuickPanel")
        panel.setStyleSheet(toast_style(self._palette_theme))
        outer.addWidget(panel)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 11)
        layout.setSpacing(9)

        self._search = QLineEdit()
        self._search.setObjectName("QuickSearch")
        self._search.setPlaceholderText("搜索或输入序号 · Enter 粘贴 · Shift+Enter 纯文本")
        self._search.textChanged.connect(self._on_search_changed)
        self._search.installEventFilter(self)
        layout.addWidget(self._search)

        self._rows_box = QVBoxLayout()
        self._rows_box.setSpacing(2)
        layout.addLayout(self._rows_box)

        self._hint = QLabel("")
        self._hint.setObjectName("QuickHint")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._hint)

    # ---- 展示 ----

    def show_centered(self) -> None:
        """在鼠标所在的屏幕上居中显示。"""
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._reload()

        screen = QGuiApplication.screenAt(self.cursor().pos()) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.adjustSize()
            x = area.center().x() - self.width() // 2
            y = area.top() + int(area.height() * 0.22)
            self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
        self._search.setFocus()

    def _reload(self) -> None:
        query = self._search.text().strip()
        # 纯数字输入视作序号选择，不参与检索
        if query.isdigit() and len(query) == 1:
            self._items = self._storage.list(limit=MAX_ROWS)
        else:
            self._items = self._engine.search(query, limit=MAX_ROWS)
        self._render()

    def _render(self) -> None:
        while self._rows_box.count():
            entry = self._rows_box.takeAt(0)
            widget = entry.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows.clear()

        if not self._items:
            empty = QLabel("没有匹配的记录")
            empty.setObjectName("QuickHint")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._rows_box.addWidget(empty)
            self._hint.setText("")
        else:
            for index, item in enumerate(self._items, start=1):
                row = QuickRow(index, item, self._palette_theme)
                row.activated.connect(self._choose)
                self._rows_box.addWidget(row)
                self._rows.append(row)
            self._hint.setText(
                "↑↓ 选择 · 1-9 直选 · Enter 粘贴 · Shift+Enter 纯文本 · Esc 关闭"
            )

        self._active = 0
        self._sync_active()
        self.adjustSize()

    def _sync_active(self) -> None:
        for index, row in enumerate(self._rows):
            row.set_active(index == self._active)

    # ---- 交互 ----

    def _on_search_changed(self, text: str) -> None:
        # 单字符数字：立即按序号跳转，不必等防抖
        if text.isdigit() and len(text) == 1:
            position = int(text) - 1
            if 0 <= position < len(self._rows):
                self._active = position
                self._sync_active()
            return
        self._debounce.start()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt 命名
        if watched is not self._search or event.type() != event.Type.KeyPress:
            return super().eventFilter(watched, event)
        return self._handle_key(event)

    def _handle_key(self, event: QKeyEvent) -> bool:
        key = event.key()
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Tab):
            self._move(1)
            return True
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Backtab):
            self._move(-1)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            plain = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self._choose(self._active + 1, plain)
            return True
        if key == Qt.Key.Key_Escape:
            self.close_overlay()
            return True
        # 数字前缀是「跳转」而不是文本输入，单独拦下
        if Qt.Key.Key_1 <= key <= Qt.Key.Key_9 and not self._search.text():
            position = key - Qt.Key.Key_1
            if position < len(self._rows):
                self._active = position
                self._sync_active()
            return True
        return False

    def _move(self, delta: int) -> None:
        if not self._rows:
            return
        self._active = (self._active + delta) % len(self._rows)
        self._sync_active()

    def _choose(self, index: int, plain_text_only: bool = False) -> None:
        position = index - 1
        if not (0 <= position < len(self._items)):
            return
        item = self._items[position]
        if item.id is None:
            return
        self.close_overlay()
        self.item_chosen.emit(item.id, plain_text_only)

    def close_overlay(self) -> None:
        self.hide()
        self.closed.emit()

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # Popup 失焦时 Qt 会自己隐藏，这里补发信号让主窗口知道
        if event.type() == event.Type.ActivationChange and not self.isActiveWindow():
            if self.isVisible():
                self.closed.emit()
        super().changeEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt 命名
        if event.key() == Qt.Key.Key_Escape:
            self.close_overlay()
            return
        super().keyPressEvent(event)
