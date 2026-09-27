"""主窗口：搜索 + 侧栏筛选 + 列表 + 预览。

布局用 `QSplitter` 而非固定宽度：用户常在「宽列表快速翻阅」与
「宽预览看长文」之间切换，固定宽度会逼着人去拖动窗口边框。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core import automation as auto_mod
from ..core.models import ClipboardItem
from ..core.semantic import SemanticEngine
from ..core.storage import Storage
from ..utils.logger import get_logger
from .item_card import ItemCard, humanize_size, humanize_time
from .theme import Palette, build_stylesheet, palette_for

_log = get_logger("main-window")

#: 侧栏筛选：显示名 -> (类型 or None, 特殊标记)
SIDE_FILTERS = [
    ("全部", None, "all"),
    ("📌 固定", None, "pinned"),
    ("⭐ 收藏", None, "favorite"),
    ("文本", "text", "kind"),
    ("链接", "link", "kind"),
    ("图片", "image", "kind"),
    ("文件", "file", "kind"),
    ("代码", "code", "kind"),
    ("颜色", "color", "kind"),
    ("富文本", "rich", "kind"),
    ("🛡 敏感", "sensitive", "kind"),
]


class PreviewPanel(QFrame):
    """右侧预览：文本用只读文本框，图片用缩放后的 QLabel。"""

    def __init__(self, palette: Palette, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._palette_theme = palette
        self.setObjectName("Preview")
        self.setMinimumWidth(280)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(9)

        self._title = QLabel("预览")
        self._title.setObjectName("SectionTitle")
        layout.addWidget(self._title)

        self._meta = QLabel("")
        self._meta.setObjectName("CardMeta")
        self._meta.setWordWrap(True)
        layout.addWidget(self._meta)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack, 1)

        # 页 0：空状态
        self._empty = QLabel("选中一条记录查看详情")
        self._empty.setObjectName("EmptyHint")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        self._stack.addWidget(self._empty)

        # 页 1：文本
        self._text = QTextEdit()
        self._text.setObjectName("PreviewBody")
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._stack.addWidget(self._text)

        # 页 2：图片
        self._image = QLabel()
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image.setMinimumHeight(120)
        self._stack.addWidget(self._image)

        # 页 3：文件列表
        self._files = QTextEdit()
        self._files.setObjectName("PreviewBody")
        self._files.setReadOnly(True)
        self._stack.addWidget(self._files)

        layout.addWidget(self._hint_label())
        self.show_item(None)

    def _hint_label(self) -> QLabel:
        hint = QLabel("双击复制 · 空格预览 · Ctrl+1~9 快速粘贴")
        hint.setObjectName("CardMeta")
        hint.setWordWrap(True)
        return hint

    def show_item(self, item: Optional[ClipboardItem]) -> None:
        if item is None:
            self._title.setText("预览")
            self._meta.setText("")
            self._stack.setCurrentIndex(0)
            self._empty.setText("选中一条记录查看详情")
            return

        self._title.setText(f"{item.kind_label} · {item.display_source or '未知来源'}")
        parts = [humanize_time(item.created_at)]
        size = humanize_size(item.size)
        if size:
            parts.append(size)
        if item.use_count:
            parts.append(f"使用 {item.use_count} 次")
        if item.tags:
            parts.append(" ".join(f"#{tag}" for tag in item.tags[:5]))
        if item.device:
            parts.append(f"来自 {item.device}")
        self._meta.setText(" · ".join(parts))

        if item.kind == "image":
            self._show_image(item)
            return

        if item.kind == "file" and item.files:
            self._files.setPlainText("\n".join(item.files))
            self._stack.setCurrentIndex(3)
            return

        self._text.setPlainText(item.content or "(无内容)")
        self._stack.setCurrentIndex(1)

    def _show_image(self, item: ClipboardItem) -> None:
        from ..utils.paths import data_dir

        raw = Path(item.image_path)
        candidates = [raw] if raw.is_absolute() else [data_dir() / raw]
        pixmap = QPixmap()
        for candidate in candidates:
            if candidate.is_file() and pixmap.load(str(candidate)):
                break
        if pixmap.isNull():
            self._empty.setText("图片文件已不在本地")
            self._stack.setCurrentIndex(0)
            return

        area = self._stack.size()
        scaled = pixmap.scaled(
            max(120, area.width() - 20),
            max(120, area.height() - 20),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image.setPixmap(scaled)
        self._stack.setCurrentIndex(2)

    def clear(self) -> None:
        self._image.clear()
        self._text.clear()
        self._files.clear()


class MainWindow(QWidget):
    """应用主界面。"""

    copy_requested = Signal(int, bool)  # (item_id, plain_text_only)
    paste_requested = Signal(int)
    delete_requested = Signal(int)
    #: 转换完成：(原记录, 动作 key, 产物)
    transform_ready = Signal(object, str, str)

    def __init__(
        self,
        storage: Storage,
        config,
        engine: SemanticEngine,
        automation: Optional[auto_mod.AutomationEngine] = None,
        parent: QWidget = None,
    ) -> None:
        super().__init__(parent)
        self._storage = storage
        self._config = config
        self._engine = engine
        self._automation = automation or auto_mod.AutomationEngine()

        self._items: List[ClipboardItem] = []
        self._cards: Dict[int, ItemCard] = {}
        self._selected_id: Optional[int] = None
        self._filter = "all"
        self._kind_filter: Optional[str] = None
        # 先定主题再建控件：卡片的类型色条在构造时就要用到色板
        self._palette_theme = palette_for(self._config.get("ui.theme", "system"))

        self.setWindowTitle("玲珑剪切板")
        self.setMinimumSize(QSize(880, 560))
        self.resize(1180, 720)
        self.setObjectName("Root")
        self.setAcceptDrops(False)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(120)  # 输入防抖：连打时只查最后一次
        self._search_timer.timeout.connect(self.reload)

        self._build_ui()
        self.setStyleSheet(build_stylesheet(self._palette_theme))
        self.reload()

    # ---- 主题 ----

    def apply_theme(self, theme: str) -> None:
        """切换主题。已渲染的卡片持有旧色板，需要重建。"""
        self._palette_theme = palette_for(theme)
        self._preview._palette_theme = self._palette_theme
        self.setStyleSheet(build_stylesheet(self._palette_theme))
        self.reload()

    # ---- 构建界面 ----

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_sidebar())
        splitter.addWidget(self._build_list_area())
        self._preview = PreviewPanel(self._palette_theme)
        splitter.addWidget(self._preview)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([190, 620, 340])
        root.addWidget(splitter, 1)

        self._build_status_bar(root)
        self._install_shortcuts()

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("Header")
        layout = QVBoxLayout(header)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(9)

        top = QHBoxLayout()
        top.setSpacing(8)

        self._search = QLineEdit()
        self._search.setObjectName("SearchBox")
        self._search.setPlaceholderText(
            "搜索内容，或直接输入「上周的截图」「来自 Chrome 的链接」「固定项」"
        )
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(lambda _: self._search_timer.start())
        self._search.returnPressed.connect(self._copy_first_result)
        top.addWidget(self._search, 1)

        self._chips: Dict[str, QPushButton] = {}
        for label, kind in (("全部", None), ("文本", "text"), ("链接", "link"), ("图片", "image")):
            chip = QPushButton(label)
            chip.setObjectName("Chip")
            chip.setCheckable(True)
            chip.setChecked(kind is None)
            chip.clicked.connect(lambda _=False, k=kind: self._on_chip(k))
            top.addWidget(chip)
            self._chips[label] = chip

        layout.addLayout(top)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._intent_label = QLabel("")
        self._intent_label.setObjectName("CardMeta")
        row.addWidget(self._intent_label)
        row.addStretch(1)

        self._count_label = QLabel("")
        self._count_label.setObjectName("CardMeta")
        row.addWidget(self._count_label)
        layout.addLayout(row)

        return header

    def _build_sidebar(self) -> QWidget:
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setMinimumWidth(150)
        side.setMaximumWidth(260)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(11, 12, 11, 12)
        layout.setSpacing(9)

        stats_title = QLabel("概览")
        stats_title.setObjectName("SectionTitle")
        layout.addWidget(stats_title)

        self._stats_row = QHBoxLayout()
        self._stats_row.setSpacing(14)
        self._stat_items = self._add_stat(self._stats_row, "记录")
        self._stat_today = self._add_stat(self._stats_row, "今日")
        layout.addLayout(self._stats_row)

        filters_title = QLabel("筛选")
        filters_title.setObjectName("SectionTitle")
        layout.addWidget(filters_title)

        self._side_list = QListWidget()
        self._side_list.setObjectName("SideList")
        for label, _kind, mode in SIDE_FILTERS:
            entry = QListWidgetItem(label)
            entry.setData(Qt.ItemDataRole.UserRole, (label, mode))
            self._side_list.addItem(entry)
        self._side_list.setCurrentRow(0)
        self._side_list.currentRowChanged.connect(self._on_side_changed)
        layout.addWidget(self._side_list, 1)

        layout.addWidget(self._build_actions())
        return side

    def _add_stat(self, layout: QHBoxLayout, label: str) -> QLabel:
        box = QVBoxLayout()
        box.setSpacing(0)
        value = QLabel("0")
        value.setObjectName("StatValue")
        box.addWidget(value)
        caption = QLabel(label)
        caption.setObjectName("StatLabel")
        box.addWidget(caption)
        layout.addLayout(box)
        return value

    def _build_actions(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        clear_button = QPushButton("清理未固定记录")
        clear_button.setToolTip("删除所有未固定、未收藏的记录（固定项保留）")
        clear_button.clicked.connect(self._clear_unpinned)
        layout.addWidget(clear_button)

        compact_button = QPushButton("压缩数据库")
        compact_button.setToolTip("回收空间，整理全文索引")
        compact_button.clicked.connect(self._compact_db)
        layout.addWidget(compact_button)

        return box

    def _build_list_area(self) -> QWidget:
        area = QWidget()
        layout = QVBoxLayout(area)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        self._list_layout = QVBoxLayout(container)
        self._list_layout.setContentsMargins(4, 4, 4, 4)
        self._list_layout.setSpacing(0)
        self._list_layout.addStretch(1)
        self._scroll.setWidget(container)
        layout.addWidget(self._scroll, 1)

        self._empty_label = QLabel(
            "还没有记录。\n\n复制任意内容后会自动出现在这里。\nCtrl+Shift+V 随时唤起快速粘贴。"
        )
        self._empty_label.setObjectName("EmptyHint")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._empty_label.hide()
        layout.addWidget(self._empty_label)

        return area

    def _build_status_bar(self, root: QVBoxLayout) -> None:
        bar = QStatusBar()
        bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._status = bar
        self._status_label = QLabel("就绪")
        bar.addWidget(self._status_label)
        root.addWidget(bar)

    def _install_shortcuts(self) -> None:
        def bind(sequence: str, slot) -> None:
            action = QAction(self)
            action.setShortcut(QKeySequence(sequence))
            action.triggered.connect(slot)
            self.addAction(action)

        bind("Ctrl+F", lambda: self._search.setFocus())
        bind("Escape", self._on_escape)
        bind("Return", self._copy_selected_plain)
        bind("Ctrl+C", self._copy_selected_plain)
        bind("Delete", self._delete_selected)
        bind("Ctrl+P", self._toggle_pin_selected)
        bind("Ctrl+D", self._toggle_favorite_selected)
        bind("Ctrl+R", self.reload)
        for index in range(1, 10):
            bind(f"Ctrl+{index}", lambda _=False, i=index: self._paste_nth(i))

    # ---- 数据加载 ----

    def reload(self) -> None:
        query = self._search.text().strip()
        kind = self._kind_filter
        pinned = self._filter == "pinned"
        favorite = self._filter == "favorite"

        if query:
            items = self._engine.search(
                query, kind=kind, pinned_only=pinned, favorite_only=favorite
            )
            explanation = self._engine.explain(query)
            self._intent_label.setText(
                "已理解：" + " · ".join(explanation) if explanation else ""
            )
        else:
            items = self._storage.list(
                kind=kind, pinned_only=pinned, favorite_only=favorite, limit=300
            )
            self._intent_label.setText("")

        self._items = items
        self._render_list()
        self._refresh_stats()
        self._status_label.setText(
            f"{len(items)} 条记录" if items else "无匹配记录"
        )

    def _render_list(self) -> None:
        # 清空旧卡片
        while self._list_layout.count() > 1:
            entry = self._list_layout.takeAt(0)
            widget = entry.widget()
            if widget is not None:
                widget.deleteLater()
        self._cards.clear()

        if not self._items:
            self._empty_label.show()
            self._scroll.hide()
            self._preview.show_item(None)
            return

        self._empty_label.hide()
        self._scroll.show()

        for item in self._items:
            card = ItemCard(item, self._palette_theme)
            card.double_clicked.connect(lambda item_id: self.copy_requested.emit(item_id, False))
            card.context_requested.connect(self._show_context_menu)
            self._list_layout.insertWidget(self._list_layout.count() - 1, card)
            if item.id is not None:
                self._cards[item.id] = card

        self._select(self._selected_id if self._selected_id in self._cards else self._first_id())

    def _first_id(self) -> Optional[int]:
        for item in self._items:
            if item.id is not None:
                return item.id
        return None

    def _refresh_stats(self) -> None:
        import time

        total = self._storage.count()
        today_start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        today = len([item for item in self._storage.list(limit=1000) if item.created_at >= today_start])
        self._stat_items.setText(str(total))
        self._stat_today.setText(str(today))

    # ---- 选择与交互 ----

    def _select(self, item_id: Optional[int]) -> None:
        for card_id, card in self._cards.items():
            card.set_selected(card_id == item_id)
        self._selected_id = item_id
        item = self._storage.get(item_id) if item_id is not None else None
        self._preview.show_item(item)

    def _on_side_changed(self, row: int) -> None:
        if row < 0 or row >= len(SIDE_FILTERS):
            return
        label, kind, mode = SIDE_FILTERS[row]
        if mode == "all":
            self._filter, self._kind_filter = "all", None
        elif mode == "pinned":
            self._filter, self._kind_filter = "pinned", None
        elif mode == "favorite":
            self._filter, self._kind_filter = "favorite", None
        else:
            self._filter, self._kind_filter = "all", kind
        self._sync_chips()
        self.reload()

    def _on_chip(self, kind: Optional[str]) -> None:
        self._kind_filter = kind
        self._filter = "all"
        self._side_list.setCurrentRow(0 if kind is None else self._row_for_kind(kind))
        self._sync_chips()
        self.reload()

    def _row_for_kind(self, kind: str) -> int:
        for index, (_label, entry_kind, mode) in enumerate(SIDE_FILTERS):
            if mode == "kind" and entry_kind == kind:
                return index
        return 0

    def _sync_chips(self) -> None:
        mapping = {"全部": None, "文本": "text", "链接": "link", "图片": "image"}
        for label, chip in self._chips.items():
            expected = mapping[label]
            chip.setChecked(expected == self._kind_filter)

    def _on_escape(self) -> None:
        if self._search.text():
            self._search.clear()
            return
        self.hide()

    def _copy_first_result(self) -> None:
        if self._items:
            first = self._items[0]
            if first.id is not None:
                self.copy_requested.emit(first.id, False)

    def _copy_selected_plain(self) -> None:
        if self._selected_id is not None:
            self.copy_requested.emit(self._selected_id, True)

    def _delete_selected(self) -> None:
        if self._selected_id is not None:
            self.delete_requested.emit(self._selected_id)

    def _toggle_pin_selected(self) -> None:
        self._toggle_flag("pinned")

    def _toggle_favorite_selected(self) -> None:
        self._toggle_flag("favorite")

    def _toggle_flag(self, field: str) -> None:
        if self._selected_id is None:
            return
        item = self._storage.get(self._selected_id)
        if item is None:
            return
        new_value = not getattr(item, field)
        self._storage.update_fields(self._selected_id, **{field: new_value})
        label = "固定" if field == "pinned" else "收藏"
        self._status_label.setText(f"已{'取消' if not new_value else ''}{label}")
        self.reload()

    def _paste_nth(self, index: int) -> None:
        if 1 <= index <= len(self._items):
            item = self._items[index - 1]
            if item.id is not None:
                self.paste_requested.emit(item.id)

    # ---- 右键菜单 ----

    def _show_context_menu(self, item_id: int, position) -> None:
        item = self._storage.get(item_id)
        if item is None:
            return
        self._select(item_id)

        menu = QMenu(self)

        copy_action = menu.addAction("复制")
        copy_action.triggered.connect(lambda: self.copy_requested.emit(item_id, False))

        plain_action = menu.addAction("复制为纯文本")
        plain_action.triggered.connect(lambda: self.copy_requested.emit(item_id, True))

        paste_action = menu.addAction("直接粘贴到上次的窗口")
        paste_action.triggered.connect(lambda: self.paste_requested.emit(item_id))

        menu.addSeparator()

        pin_action = menu.addAction("取消固定" if item.pinned else "固定")
        pin_action.triggered.connect(lambda: self._toggle_flag("pinned"))

        favorite_action = menu.addAction("取消收藏" if item.favorite else "收藏")
        favorite_action.triggered.connect(lambda: self._toggle_flag("favorite"))

        menu.addSeparator()
        menu.addMenu(self._build_transform_menu(item))

        menu.addSeparator()
        delete_action = menu.addAction("删除")
        delete_action.triggered.connect(lambda: self.delete_requested.emit(item_id))

        menu.exec(position)

    def _build_transform_menu(self, item: ClipboardItem) -> QMenu:
        """按内容类型动态生成「转换」子菜单。"""
        menu = QMenu("转换为…", self)
        actions = self._automation.suggest_for(item)
        if not actions:
            empty = menu.addAction("（该类型暂无可用的转换）")
            empty.setEnabled(False)
            return menu

        by_category: Dict[str, QMenu] = {}
        for action in actions:
            submenu = by_category.get(action.category)
            if submenu is None:
                submenu = QMenu(action.category, menu)
                by_category[action.category] = submenu
                menu.addMenu(submenu)
            entry = submenu.addAction(action.name)
            if action.description:
                entry.setToolTip(action.description)
            entry.triggered.connect(
                lambda _=False, key=action.key: self._run_transform(item.id, key)
            )
        return menu

    def _run_transform(self, item_id: Optional[int], action_key: str) -> None:
        item = self._storage.get(item_id) if item_id is not None else None
        if item is None:
            return
        result = self._automation.apply(action_key, item)
        if not result.ok:
            QMessageBox.information(self, "无法转换", result.error)
            return
        self.transform_ready.emit(item, action_key, result.output)

    # ---- 动作 ----

    def _clear_unpinned(self) -> None:
        reply = QMessageBox.question(
            self,
            "确认清理",
            "删除所有未固定的记录？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        removed = self._storage.clear(keep_pinned=True)
        self._status_label.setText(f"已清理 {removed} 条记录")
        self.reload()

    def _compact_db(self) -> None:
        self._storage.optimize()
        self._status_label.setText("数据库已压缩")

    def focus_search(self) -> None:
        self._search.setFocus()
        self._search.selectAll()

    def notify(self, message: str, timeout_ms: int = 2500) -> None:
        self._status_label.setText(message)
        QTimer.singleShot(timeout_ms, lambda: self._status_label.setText("就绪"))
