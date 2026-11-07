"""首次启动 / 空历史的引导页。

为什么不是一行「还没有记录」：新装用户打开软件看到空界面，最想知道的是
**「我复制的东西会存到哪、会不会丢、装完了吗」**。原来的空状态只说了
「复制任意内容后会自动出现在这里」，一个字都没提数据落在磁盘哪个位置，
于是「数据持久化」这件事只能靠用户自己猜 —— 这正是被投诉的点。

这里直接把答案写在界面上：数据目录、当前用的是哪种存储、文件叫什么、
以及两个「能立刻做的动作」。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..utils.paths import data_dir, database_path, json_dir
from .icons import app_icon


def _storage_facts() -> tuple:
    """当前存储方式与目标文件。读配置失败时不阻断界面渲染。"""
    try:
        from ..utils.config import Config

        backend = str(Config().get("storage.backend", "sqlite") or "sqlite")
    except Exception:  # noqa: BLE001 - 引导页不该因为读配置失败就白屏
        backend = "sqlite"
    if backend == "json":
        return backend, json_dir() / "clipboard.json"
    return backend, database_path()


class EmptyStatePanel(QWidget):
    """空历史时显示的引导面板。"""

    #: 请求打开数据目录（由 Application 接住，用系统文件管理器打开）
    open_data_dir_requested = Signal()
    #: 请求打开快速粘贴浮层
    quick_paste_requested = Signal()
    #: 请求打开设置
    settings_requested = Signal()

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setObjectName("EmptyState")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 32, 40, 32)
        layout.setSpacing(14)
        layout.addStretch(1)

        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(96, 96))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)

        title = QLabel("还没有记录")
        title.setObjectName("EmptyStateTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        self._subtitle = QLabel(
            "复制任意内容，它会自动出现在这里。\n"
            "选中文字后按 Ctrl+C，或直接复制文件、图片都可以。"
        )
        self._subtitle.setObjectName("EmptyStateHint")
        self._subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._subtitle.setWordWrap(True)
        layout.addWidget(self._subtitle)

        layout.addSpacing(8)
        layout.addWidget(self._build_storage_card())
        layout.addSpacing(4)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)
        self._open_dir = QPushButton("打开数据目录")
        self._open_dir.setToolTip("在文件管理器里打开历史数据所在目录")
        self._open_dir.clicked.connect(self.open_data_dir_requested.emit)
        buttons.addWidget(self._open_dir)

        self._quick = QPushButton("试试快速粘贴")
        self._quick.setToolTip("Ctrl+Shift+V")
        self._quick.clicked.connect(self.quick_paste_requested.emit)
        buttons.addWidget(self._quick)

        self._settings = QPushButton("打开设置")
        self._settings.clicked.connect(self.settings_requested.emit)
        buttons.addWidget(self._settings)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        layout.addStretch(2)

    def _build_storage_card(self) -> QWidget:
        backend, target = _storage_facts()
        kind_label = {
            "json": "JSON 单文件（可直接打开看、可拷走）",
            "sqlite": "SQLite 数据库（推荐，记录多时更快）",
        }.get(backend, backend)

        card = QFrame()
        card.setObjectName("EmptyStateCard")
        card.setMaximumWidth(560)
        form = QVBoxLayout(card)
        form.setContentsMargins(18, 14, 18, 16)
        form.setSpacing(6)

        heading = QLabel("你的数据存在这里，不会丢")
        heading.setObjectName("EmptyStateCardTitle")
        form.addWidget(heading)

        rows = (
            ("数据目录", str(data_dir())),
            ("存储方式", kind_label),
            ("历史文件", str(target)),
        )
        self._path_labels = {}
        for label, value in rows:
            row = QHBoxLayout()
            row.setSpacing(8)
            key = QLabel(f"{label}：")
            key.setObjectName("EmptyStateKey")
            key.setMinimumWidth(72)
            val = QLabel(value)
            val.setObjectName("EmptyStateValue")
            val.setWordWrap(True)
            # 允许选中复制：用户往往要把这个路径贴到别处去用
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(key, 0)
            row.addWidget(val, 1)
            form.addLayout(row)
            self._path_labels[label] = val

        note = QLabel(
            "本地优先：历史只存在这台机器上，不经过任何服务器。"
            "卸载程序不会删除这个目录。"
        )
        note.setObjectName("EmptyStateNote")
        note.setWordWrap(True)
        form.addWidget(note)

        # 卡片自己有高度，必须让它按内容撑开。
        # 之前塞进一个没有 sizeHint 的 holder 里，卡片被压成一条，
        # 三行路径叠在一起 —— 只有在真跑起来看截图时才发现。
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        return card

    def refresh(self) -> None:
        """数据目录可能在运行中被改（LINGLONG_CLIPBOARD_HOME），重新读一次。"""
        backend, target = _storage_facts()
        if "数据目录" in self._path_labels:
            self._path_labels["数据目录"].setText(str(data_dir()))
        if "历史文件" in self._path_labels:
            self._path_labels["历史文件"].setText(str(target))
