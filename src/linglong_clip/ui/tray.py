"""系统托盘图标与菜单。

托盘是这类常驻工具的「主入口」：窗口可以关，托盘必须一直在。
图标用代码绘制而非打包图片资源，省去各平台图标格式（ico/icns/png）的适配。
"""

from __future__ import annotations


from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..utils.logger import get_logger
from .theme import KIND_COLORS, Palette

_log = get_logger("tray")


def build_icon(palette: Palette, size: int = 64) -> QIcon:
    """绘制图标：两个叠放的圆角方块 + 一个剪贴板挂钩，表示「多层历史」。

    直接向量绘制，任意 DPI 下都清晰，不必提供多套位图。
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    unit = size / 64.0

    back = QPainterPath()
    back.addRoundedRect(
        QRect(int(20 * unit), int(8 * unit), int(36 * unit), int(44 * unit)),
        int(7 * unit),
        int(7 * unit),
    )
    painter.setBrush(QColor(palette.accent).lighter(140))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawPath(back)

    front = QPainterPath()
    front.addRoundedRect(
        QRect(int(9 * unit), int(17 * unit), int(38 * unit), int(40 * unit)),
        int(7 * unit),
        int(7 * unit),
    )
    painter.setBrush(QColor(KIND_COLORS["link"]))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawPath(front)

    painter.setPen(QPen(QColor("#ffffff"), max(1.6, 3.0 * unit)))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for index, width in enumerate((20, 20, 12)):
        y = int((27 + index * 9) * unit)
        painter.drawLine(
            int(16 * unit), y, int(16 * unit) + int(width * unit), y
        )

    painter.end()
    return QIcon(pixmap)


class TrayIcon(QSystemTrayIcon):
    """托盘图标。

    只负责「发出意图」，具体动作交给外部槽函数，
    这样控制器可以统一处理窗口显示、快捷键注册等逻辑。
    """

    show_window_requested = Signal()
    quick_paste_requested = Signal()
    toggle_capture_requested = Signal()
    settings_requested = Signal()
    sync_requested = Signal()
    quit_requested = Signal()

    def __init__(self, palette: Palette, parent=None) -> None:
        super().__init__(build_icon(palette), parent)
        self._palette_theme = palette
        self._capture_enabled = True

        self.setToolTip("玲珑剪切板")
        self._menu = QMenu()
        self._build_menu()
        self.setContextMenu(self._menu)
        self.activated.connect(self._on_activated)

    def _build_menu(self) -> None:
        self._menu.clear()

        show_action = QAction("打开主窗口", self._menu)
        show_action.triggered.connect(self.show_window_requested.emit)
        self._menu.addAction(show_action)

        quick_action = QAction("快速粘贴…", self._menu)
        quick_action.triggered.connect(self.quick_paste_requested.emit)
        self._menu.addAction(quick_action)

        self._menu.addSeparator()

        self._capture_action = QAction("暂停采集", self._menu)
        self._capture_action.setCheckable(True)
        self._capture_action.toggled.connect(self._on_capture_toggled)
        self._menu.addAction(self._capture_action)

        sync_action = QAction("立即同步", self._menu)
        sync_action.triggered.connect(self.sync_requested.emit)
        self._menu.addAction(sync_action)

        self._menu.addSeparator()

        settings_action = QAction("设置…", self._menu)
        settings_action.triggered.connect(self.settings_requested.emit)
        self._menu.addAction(settings_action)

        quit_action = QAction("退出", self._menu)
        quit_action.triggered.connect(self.quit_requested.emit)
        self._menu.addAction(quit_action)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # 单击或双击都直接唤起主窗口：托盘图标太小，区分单击/双击反而难用
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_window_requested.emit()

    def _on_capture_toggled(self, paused: bool) -> None:
        self._capture_action.setText("恢复采集" if paused else "暂停采集")
        self.toggle_capture_requested.emit()

    def set_capture_enabled(self, enabled: bool) -> None:
        self._capture_enabled = enabled
        self._capture_action.blockSignals(True)
        self._capture_action.setChecked(not enabled)
        self._capture_action.setText("暂停采集" if enabled else "恢复采集")
        self._capture_action.blockSignals(False)

    def set_paused(self, paused: bool) -> None:
        self.set_capture_enabled(not paused)
        suffix = "（已暂停）" if paused else ""
        self.setToolTip(f"玲珑剪切板{suffix}")

    def notify(self, title: str, message: str, msecs: int = 2200) -> None:
        if not self.isSystemTrayAvailable():
            return
        try:
            self.showMessage(title, message, self.icon(), msecs)
        except Exception:  # noqa: BLE001 - 部分桌面环境通知服务不可用
            _log.debug("托盘通知发送失败", exc_info=True)
