"""转换结果窗口。

加工后的内容不直接覆盖剪贴板，而是先展示 —— 用户可以核对、
继续叠加其它动作、或只复制其中一部分。这比「一键替换」更可控。
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.automation import REGISTRY, AutomationEngine
from ..core.models import ClipboardItem
from .theme import palette_for


class TransformWindow(QWidget):
    """展示单个动作的产物，并支持继续链式加工。"""

    copy_result = Signal(str)

    def __init__(self, config, automation: AutomationEngine, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._config = config
        self._automation = automation
        self._source: Optional[ClipboardItem] = None
        self._history: List[str] = []  # 已应用的动作 key

        self.setWindowTitle("转换结果 — 玲珑剪切板")
        self.resize(720, 520)
        self.setWindowFlag(Qt.WindowType.Window)

        palette = palette_for(config.get("ui.theme", "system"))
        self.setStyleSheet(
            f"QWidget {{ background-color: {palette.bg}; color: {palette.text}; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self._title = QLabel("转换结果")
        self._title.setObjectName("StatValue")
        layout.addWidget(self._title)

        self._chain_label = QLabel("")
        self._chain_label.setObjectName("CardMeta")
        self._chain_label.setWordWrap(True)
        layout.addWidget(self._chain_label)

        self._body = QTextEdit()
        self._body.setReadOnly(True)
        self._body.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._body.setStyleSheet(
            f"QTextEdit {{ background-color: {palette.surface}; color: {palette.text};"
            f" border: 1px solid {palette.border}; border-radius: 8px; padding: 10px; }}"
        )
        layout.addWidget(self._body, 1)

        self._actions_row = QHBoxLayout()
        self._actions_row.setSpacing(7)
        layout.addLayout(self._actions_row)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self._copy_button = QPushButton("复制结果并关闭")
        self._copy_button.setObjectName("Primary")
        self._copy_button.clicked.connect(self._copy_and_close)
        footer.addWidget(self._copy_button)

        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        footer.addWidget(close_button)
        layout.addLayout(footer)

    # ---- 展示 ----

    def show_result(
        self, item: ClipboardItem, action_key: str, output: str, chain: Optional[List[str]] = None
    ) -> None:
        self._source = item
        self._history = list(chain or [action_key])
        action = REGISTRY.get(action_key)
        self._title.setText(action.name if action else action_key)
        self._chain_label.setText(
            f"来源：{item.kind_label} · {item.display_source}  |  "
            f"链路：{' → '.join(self._history)}  |  "
            f"原始 {len(item.content)} 字符 → 结果 {len(output)} 字符"
        )
        self._body.setPlainText(output)
        self._rebuild_action_buttons(output)
        self.show()
        self.raise_()
        self.activateWindow()

    def _rebuild_action_buttons(self, current_output: str) -> None:
        """在当前结果上继续加工：只列适用当前内容的动作。"""
        while self._actions_row.count():
            entry = self._actions_row.takeAt(0)
            widget = entry.widget()
            if widget is not None:
                widget.deleteLater()

        if self._source is None:
            return

        probe = ClipboardItem(
            kind=self._source.kind,
            content=current_output,
            preview=current_output[:200],
            source_app=self._source.source_app,
            hash=self._source.hash,
        )
        candidates = [
            action
            for action in self._automation.suggest_for(probe)
            if action.key not in self._history
        ][:5]

        if not candidates:
            return

        label = QLabel("继续转换：")
        label.setObjectName("CardMeta")
        self._actions_row.addWidget(label)

        for action in candidates:
            button = QPushButton(action.name)
            button.setObjectName("Ghost")
            button.setToolTip(action.description or action.name)
            button.clicked.connect(
                lambda _=False, key=action.key: self._apply_next(key, current_output)
            )
            self._actions_row.addWidget(button)
        self._actions_row.addStretch(1)

    def _apply_next(self, action_key: str, current_output: str) -> None:
        if self._source is None:
            return
        probe = ClipboardItem(
            kind=self._source.kind,
            content=current_output,
            preview=current_output[:200],
            source_app=self._source.source_app,
            hash=self._source.hash,
        )
        result = self._automation.apply(action_key, probe)
        if not result.ok:
            self._chain_label.setText(f"⚠ {result.error}")
            return
        self._history.append(action_key)
        self.show_result(self._source, action_key, result.output, self._history)

    def _copy_and_close(self) -> None:
        text = self._body.toPlainText()
        if text:
            self.copy_result.emit(text)
        self.close()
