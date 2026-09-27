"""应用控制器：把存储、采集、同步、界面装配到一起。

职责边界：
- 控制器持有所有长生命周期对象，界面只发信号，不直接碰存储；
- 采集回调在后台线程触发，跨线程更新界面一律走 Qt 信号；
- 任何可选能力（全局快捷键、原生后端、同步）失败都只降级、不抛出。
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from typing import Optional

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QTimer, Qt, Signal
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from ..core.automation import AutomationEngine
from ..core.collector import CAPTURED, MERGED, Collector
from ..core.models import ClipboardItem
from ..core.monitor import ClipboardMonitor, FocusTracker
from ..core.ocr import build_engine as build_ocr_engine
from ..core.privacy import PrivacyGuard
from ..core.semantic import SemanticEngine
from ..core.repository import open_repository
from ..core.lan_sync import LanSync, build_lan_sync
from ..core.sync import FolderSync, SyncCipher
from ..core.sync_cursor import SyncCursorStore
from ..platform import detect_backend
from ..utils.config import Config
from ..utils.logger import get_logger, setup_logging
from ..utils.paths import is_linux
from .clipboard_qt import QtClipboardAdapter
from .hotkeys import HotkeyManager
from .main_window import MainWindow
from .quick_paste import QuickPasteOverlay
from ..utils.selfcheck import run_self_check
from .settings_dialog import SettingsDialog
from .theme import palette_for
from .transform_window import TransformWindow
from .tray import TrayIcon

_log = get_logger("app")

#: 同步与清理的定时周期（毫秒）
SYNC_INTERVAL_MS = 60_000

#: Windows 事件驱动生效后的轮询兜底间隔（毫秒）
EVENT_DRIVEN_FALLBACK_MS = 1_000
SENSITIVE_SWEEP_MS = 30_000


class GuiThreadInvoker(QObject):
    """把可调用对象投递到 GUI 线程执行，并可同步取回结果。

    存在的原因：`QClipboard` 只能在它所属的线程被安全访问，
    采集线程直接读会得到空数据。这里用「信号 + 事件循环」把读取搬回主线程，
    并在调用方等待，因此对采集线程而言仍是同步调用。

    `LINGLONG_CLIPBOARD_HOME` 之外的场景不需要它，
    但代价只有一次事件投递，换来的正确性值得。
    """

    _run_requested = Signal(object)

    def __init__(self, owner: QObject) -> None:
        super().__init__(owner)
        self._owner = owner
        self._cancelled = False
        self._run_requested.connect(self._execute, Qt.ConnectionType.QueuedConnection)

    def cancel(self) -> None:
        """退出流程中调用：唤醒所有等待者，让采集线程能结束。"""
        self._cancelled = True

    def call(self, func, blocking: bool = True):
        from PySide6.QtCore import QThread

        if self._cancelled:
            return None

        app = QApplication.instance()
        if app is not None and QThread.currentThread() is app.thread():
            return func()

        box: dict = {"done": threading.Event()}
        self._run_requested.emit((func, box))
        if not blocking:
            return None

        if not box["done"].wait(timeout=2.0):
            _log.debug("等待 GUI 线程执行超时")
            return None
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def _execute(self, payload) -> None:
        func, box = payload
        try:
            box["result"] = func()
        except Exception as exc:  # noqa: BLE001 - 异常带回调用线程处理
            box["error"] = exc
        finally:
            box["done"].set()


class Application(QObject):
    """应用装配与事件路由。"""

    #: 采集线程 -> 主线程
    item_captured = Signal(object, str)

    def __init__(self, argv: list, *, start_minimized: bool = False) -> None:
        super().__init__()
        self._argv = argv
        self._start_minimized = start_minimized
        self._shutting_down = False
        self._sync_signature_cache: Optional[tuple] = None

        # 复用已存在的实例：Qt 全局只允许一个 QApplication，
        # 嵌入到宿主进程中时（如 IDE 插件）不能重复创建。
        self.qt_app = QApplication.instance() or QApplication(argv)
        self._invoker = GuiThreadInvoker(self)
        self.qt_app.setApplicationName("玲珑剪切板")
        self.qt_app.setApplicationDisplayName("玲珑剪切板")
        self.qt_app.setOrganizationName("Linglong")
        self.qt_app.setQuitOnLastWindowClosed(False)

        self.config = Config()
        self._setup_logging()

        self.storage = open_repository(self.config)
        self.backend = detect_backend()
        self.guard = PrivacyGuard(self.config)
        self.automation = AutomationEngine()
        self.automation.load_from_config(self.config.get("automation.workflows", []) or [])
        self.engine = SemanticEngine(self.storage)
        self.ocr = build_ocr_engine(self.config)
        # 同步增量游标：两种同步模式共用一份，切换模式时进度不丢
        self.sync_cursors = SyncCursorStore()
        self.sync_cursors.load()

        self.collector = Collector(
            self.storage,
            self.config,
            self.guard,
            device_name=self._device_name(),
            on_captured=self._on_collected_threadsafe,
            ocr=self.ocr,
        )
        self.sync = self._build_sync()
        self._sync_signature_cache = self._sync_signature()

        self.hotkeys = HotkeyManager()
        self.window = MainWindow(self.storage, self.config, self.engine, self.automation)
        self.preview = QuickPasteOverlay(self.storage, self.engine, self.config)
        self.transformer = TransformWindow(self.config, self.automation)
        self.tray = TrayIcon(palette_for(self.config.get("ui.theme", "system")))

        self.clipboard = QtClipboardAdapter(
            self.qt_app.clipboard(),
            self.storage.images_dir,
            self.backend,
            on_image_saved=self._on_image_saved,
        )
        self.monitor = ClipboardMonitor(
            self.clipboard,
            interval_ms=int(self.config.get("capture.poll_interval_ms", 300)),
            on_capture=self._on_capture_threadsafe,
            backend=self.backend,
            gui_invoke=self._invoker.call,
        )
        self.focus = FocusTracker(self.backend)

        #: 原生能力自检结果，首次访问设置页时惰性计算
        self._native_report = None

        self._wire_signals()
        self._setup_native_event_filter()
        self._setup_timers()
        self._install_hotkeys()

        self.config.on_change(self._on_config_change)

    # ---- 初始化辅助 ----

    def _setup_logging(self) -> None:
        import logging

        level = logging.DEBUG if os.environ.get("LINGLONG_DEBUG") else logging.INFO
        setup_logging(level)

    def _on_image_saved(self, relative: str, data: bytes) -> None:
        """JSON 后端：把图片内联进历史文件，实现「单文件即可搬走」。

        只在真正的 JSON 后端里做，SQLite 后端仍走 `images/` 目录，
        不做无用的 base64 膨胀。
        """
        if not hasattr(self.storage, "set_image_data"):
            return
        if not self.config.get("storage.json_inline_images", True):
            return
        import base64

        limit = int(self.config.get("storage.json_max_inline_image_kb", 2048) or 2048) * 1024
        if len(data) > limit:
            _log.info("图片 %.1fKB 超过内联上限，仍存为独立文件", len(data) / 1024)
            return
        self.storage.set_image_data(
            relative, f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"
        )

    def _device_name(self) -> str:
        import socket

        configured = str(self.config.get("sync.device_name", "") or "").strip()
        return configured or socket.gethostname()

    def _stop_sync(self) -> None:
        """释放旧同步后端的资源，尤其是局域网监听端口。"""
        previous = self.sync
        if previous is not None and hasattr(previous, "stop"):
            try:
                previous.stop()
            except Exception:  # noqa: BLE001
                _log.debug("关闭旧同步后端失败", exc_info=True)

    def refresh_sync(self, *, force: bool = False):
        """配置变化后重建同步后端，避免旧监听端口一直占着。

        配置没实质变化时直接复用现有后端，跳过整轮重建。
        """
        signature = self._sync_signature()
        if not force and signature == self._sync_signature_cache and self.sync is not None:
            return self.sync
        self._stop_sync()
        self.sync = self._build_sync()
        self._sync_signature_cache = signature
        return self.sync

    def refresh_ocr(self) -> None:
        """配置变化后重建 OCR 引擎，并同步给采集器（无需重启应用）。"""
        self.ocr = build_ocr_engine(self.config)
        self.collector.set_ocr(self.ocr)

    def _sync_signature(self) -> tuple:
        """同步相关配置的指纹，用于判断是否需要重建后端。

        没有它的话，设置页一次保存会逐键触发重建，每次都要重开监听端口。
        """
        keys = (
            "sync.enabled", "sync.mode", "sync.folder", "sync.device_name",
            "sync.encrypt", "sync.lan_port", "sync.lan_token",
            "sync.lan_discovery",
        )
        peers = self.config.get("sync.lan_peers", []) or []
        return tuple(self.config.get(k) for k in keys) + (tuple(peers),)

    def _build_sync(self):
        """按 `sync.mode` 构造同步后端：共享文件夹或局域网直连。

        两者对外形状一致（`export`/`import_all` 或 `sync_all`/`status`），
        所以 `run_sync` 不需要为模式分支。
        """
        if not self.config.get("sync.enabled", False):
            return None

        mode = str(self.config.get("sync.mode", "folder") or "folder").lower()
        if mode == "lan":
            lan = build_lan_sync(
                self.storage, self.config, self.guard, cursors=self.sync_cursors
            )
            if lan is not None and lan.start():
                _log.info("局域网同步已就绪：%s", lan.status())
            return lan

        folder = str(self.config.get("sync.folder", "") or "").strip()
        if not folder:
            return None
        from pathlib import Path

        cipher = SyncCipher(
            bool(self.config.get("sync.encrypt", True)),
            password=self._device_name(),
        )
        return FolderSync(
            self.storage,
            Path(folder).expanduser(),
            device_name=self._device_name(),
            cipher=cipher,
            guard=self.guard,
            cursors=self.sync_cursors,
        )

    def _wire_signals(self) -> None:
        self.item_captured.connect(self._on_captured_ui)

        self.window.copy_requested.connect(self.copy_item)
        self.window.paste_requested.connect(self.paste_item)
        self.window.delete_requested.connect(self.delete_item)
        self.window.transform_ready.connect(self._show_transform)
        self.preview.item_chosen.connect(self._on_quick_chosen)
        self.preview.closed.connect(self._on_preview_closed)
        self.transformer.copy_result.connect(self.copy_text)

        self.tray.show_window_requested.connect(self.show_window)
        self.tray.quick_paste_requested.connect(self.show_quick_paste)
        self.tray.toggle_capture_requested.connect(self.toggle_capture)
        self.tray.settings_requested.connect(self.show_settings)
        self.tray.sync_requested.connect(self.run_sync)
        self.tray.quit_requested.connect(self.quit)

    def _setup_native_event_filter(self) -> None:
        """把 Windows 的 WM_CLIPBOARDUPDATE / WM_HOTKEY 接进来。

        这一段此前是「接了一半」：Qt 侧的事件过滤器装好了，
        但 `AddClipboardFormatListener` 从未被调用 —— 没有注册监听，
        Windows 根本不会投递 `WM_CLIPBOARDUPDATE`。事件驱动通道全程静默，
        实际一直在跑 300ms 轮询；`monitor.set_event_driven(True)` 也没人调，
        那个开关成了死代码，末尾那行 `require_event_driven = True`
        更是个从未被读过的字段。

        修正后的顺序：拿窗口句柄 → 注册监听 → 注册成功才切事件驱动。
        注册失败就保持轮询，绝不假装自己在事件驱动。
        """
        if not sys.platform.startswith("win"):
            return

        from ..platform.windows import WM_CLIPBOARDUPDATE, WindowsBackend

        controller = self

        class _Filter(QAbstractNativeEventFilter):
            """`installNativeEventFilter` 要求的是 `QAbstractNativeEventFilter`。

            不能拿 `QObject` 凑 —— PySide6 会在装的时候直接抛 TypeError。
            这个分支只在 Windows 上会走到，Linux CI 永远跑不到，
            等于藏了一个「一启动就崩」的隐患。
            """

            def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt 命名
                try:
                    from ctypes import wintypes

                    # message 在这里是 MSG*，需要按指针还原成 MSG 结构体
                    address = int(message)
                    if not address:
                        return False, 0
                    msg = wintypes.MSG.from_address(address)
                except Exception:  # noqa: BLE001
                    return False, 0

                if controller.hotkeys.handle_native_event(event_type, msg):
                    return True, 0
                if int(msg.message) == WM_CLIPBOARDUPDATE:
                    controller.monitor.on_native_event()
                    return True, 0
                return False, 0

        self._native_filter = _Filter()
        self.qt_app.installNativeEventFilter(self._native_filter)

        if not isinstance(self.backend, WindowsBackend):
            return

        hwnd = self._native_window_handle()
        if not hwnd:
            _log.warning("未取到窗口句柄，剪贴板监听退回轮询")
            return

        if self.backend.register_listener(hwnd):
            # 事件到达时 `on_native_event` 会立刻采一次，
            # 把轮询放宽到 1s 作为「事件漏投」的兜底，而不是唯一手段。
            self.monitor.set_event_driven(True)
            self.monitor.set_interval(EVENT_DRIVEN_FALLBACK_MS)
            _log.info("已启用 Windows 剪贴板原生事件监听（窗口句柄 %s）", hwnd)
        else:
            _log.warning("AddClipboardFormatListener 注册失败，继续使用轮询")

    def _native_window_handle(self) -> int:
        """取一个稳定的窗口句柄给 `AddClipboardFormatListener` 用。

        用主窗口的 `winId()`：Qt 里它是懒创建的，`winId()` 会顺带触发创建，
        即使窗口随后被隐藏。不依赖托盘图标那类看不见的内部实现。
        """
        try:
            handle = int(self.window.winId())
            if handle:
                return handle
        except Exception:  # noqa: BLE001
            _log.debug("取 winId 失败", exc_info=True)
        return 0

    def _setup_timers(self) -> None:
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(SYNC_INTERVAL_MS)
        self._sync_timer.timeout.connect(self.run_sync)

        self._sweep_timer = QTimer(self)
        self._sweep_timer.setInterval(SENSITIVE_SWEEP_MS)
        self._sweep_timer.timeout.connect(self._sweep_sensitive)

    def native_report(self):
        """原生能力自检结果（缓存）。

        跑一次会真实注册并撤销一个全局快捷键，不该在界面操作里反复触发，
        所以首次算完就留着；`--selfcheck` 命令行路径不受此缓存影响。
        """
        if self._native_report is None:
            try:
                self._native_report = run_self_check(
                    backend=self.backend, hotkeys_factory=lambda: self.hotkeys
                )
            except Exception:  # noqa: BLE001 - 自检失败不能挡住设置页
                _log.exception("原生能力自检失败")
        return self._native_report

    def _install_hotkeys(self) -> None:
        sequence = str(self.config.get("ui.global_hotkey", "Ctrl+Shift+V") or "Ctrl+Shift+V")
        if self.hotkeys.register(sequence, self.show_quick_paste):
            self.tray.setToolTip(f"玲珑剪切板 · {sequence} 唤起快速粘贴")
            return

        # 注册失败不影响使用：应用内 Ctrl+Shift+V 与托盘菜单仍然可用
        reason = self.hotkeys.reason or "全局快捷键不可用"
        _log.warning("全局快捷键未启用：%s", reason)
        self._hotkey_warning = reason
        if is_linux():
            self.tray.setToolTip(f"玲珑剪切板 · 全局快捷键不可用（{reason}）")

    # ---- 启动与退出 ----

    def run(self) -> int:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
            self.tray.set_paused(self.monitor.is_paused)
        else:
            _log.info("系统托盘不可用，主窗口将常驻显示")
            self._start_minimized = False

        self.monitor.start()
        if self.config.get("sync.enabled", False) and self.sync is not None:
            self._sync_timer.start()

        if self._start_minimized and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.notify(
                "玲珑剪切板已在后台运行",
                f"按 {self.config.get('ui.global_hotkey', 'Ctrl+Shift+V')} 唤起快速粘贴",
            )
        else:
            self.show_window()

        signal.signal(signal.SIGINT, lambda *_: self.quit())
        # 让 Python 有机会处理信号：Qt 事件循环会吞掉 SIGINT
        self._signal_timer = QTimer(self)
        self._signal_timer.start(400)
        self._signal_timer.timeout.connect(lambda: None)

        return self.qt_app.exec()

    def quit(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        _log.info("正在退出…")
        self._invoker.cancel()
        self.monitor.stop()
        self._unregister_native_listener()
        self.hotkeys.unregister_all()
        self._sync_timer.stop()
        self._sweep_timer.stop()
        self._stop_sync()
        self.storage.close()
        self.tray.hide()
        self.qt_app.quit()

    def _unregister_native_listener(self) -> None:
        """注销 Windows 剪贴板监听。

        不注销的话，`AddClipboardFormatListener` 的残留注册会拖住进程，
        表现是「关不掉，只能强杀」。
        """
        from ..platform.windows import WindowsBackend

        if isinstance(self.backend, WindowsBackend):
            try:
                self.backend.unregister_listener()
            except Exception:  # noqa: BLE001
                _log.debug("注销剪贴板监听失败", exc_info=True)

    # ---- 采集管线 ----

    def _on_capture_threadsafe(self, item: ClipboardItem) -> None:
        """采集线程入口：识别 → 风控 → 落库，全在后台线程完成。

        落库不碰 Qt 对象，可以安全地在采集线程里做；
        只有「刷新界面」这一步需要切回 GUI 线程，
        因此这里把结果打包成信号发出去，而不是反过来让主线程干重活。
        """
        if self._shutting_down:
            return
        try:
            outcome, saved = self.collector.process(item, self.focus.current())
        except Exception:  # noqa: BLE001 - 单条失败不能中断采集线程
            _log.exception("采集处理失败")
            return
        if saved is None:
            return
        self.item_captured.emit(saved, outcome)

    def _on_collected_threadsafe(self, item: ClipboardItem, outcome: str) -> None:
        """`Collector` 内部的回调，与采集成败无关，仅用于日志。"""
        _log.debug("采集结果 %s：%s", outcome, item.preview[:60])

    def _on_captured_ui(self, item: ClipboardItem, outcome: str) -> None:
        """GUI 线程：刷新列表与提示。"""
        if self._shutting_down:
            return
        self.window.reload()
        if outcome == CAPTURED:
            self.window.notify(f"已记录：{item.kind_label} · {item.display_source}")
        elif outcome == MERGED:
            self.window.notify("内容已存在，已更新使用记录")

        if item.sensitive:
            self.tray.notify("已拦截敏感内容", "包含密码或密钥，未写入历史")

    # ---- 剪贴板操作 ----

    def copy_item(self, item_id: int, plain_text_only: bool = False) -> None:
        item = self.storage.get(item_id)
        if item is None:
            return

        text = item.content
        if item.sensitive and self.config.get("privacy.redact_on_copy_out", False):
            text = self.guard.redact(text)
            patched = ClipboardItem(
                kind=item.kind,
                content=text,
                html="",
                files=item.files,
                image_path=item.image_path,
            )
            self.clipboard.write_item(patched, plain_text_only=plain_text_only)
        else:
            self.clipboard.write_item(item, plain_text_only=plain_text_only)

        # 记下指纹，避免自己的写入又被采集一遍
        payload = item.hash if item.kind == "image" else text
        self.monitor.mark_self_write(payload or text, item.kind)
        self.storage.touch(item_id)
        self.window.reload()
        self.window.notify(f"已复制：{item.kind_label}")

    def copy_text(self, text: str) -> None:
        self.clipboard.set_text(text)
        self.monitor.mark_self_write(text, "text")
        self.window.notify("已复制转换结果")

    def paste_item(self, item_id: int) -> None:
        """复制后尝试直接粘贴到上一次的前台窗口。"""
        self.copy_item(item_id)
        if not self._paste_to_previous_window():
            self.window.notify("已复制到剪贴板，请手动粘贴（Ctrl+V）")

    def _paste_to_previous_window(self) -> bool:
        from ..utils.paths import is_macos, is_windows

        if is_windows():
            return self._paste_windows()
        if is_macos():
            return self._paste_macos()
        return self._paste_linux()

    def _paste_windows(self) -> bool:
        try:
            import ctypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            VK_CONTROL, VK_V = 0x11, 0x56
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            user32.keybd_event(VK_V, 0, 0, 0)
            user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _paste_macos(self) -> bool:
        import subprocess

        try:
            subprocess.Popen(
                ["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except OSError:
            return False

    def _paste_linux(self) -> bool:
        """X11 下用 xdotool；Wayland 出于安全设计不允许模拟全局按键。"""
        import shutil
        import subprocess

        if not shutil.which("xdotool"):
            return False
        try:
            subprocess.Popen(
                ["xdotool", "key", "--clearmodifiers", "ctrl+v"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except OSError:
            return False

    def delete_item(self, item_id: int) -> None:
        self.storage.delete(item_id)
        self.window.reload()
        self.window.notify("已删除")

    # ---- 界面 ----

    def show_window(self) -> None:
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.window.focus_search()

    def show_quick_paste(self) -> None:
        self.preview.show_centered()

    def _on_quick_chosen(self, item_id: int, plain_text_only: bool) -> None:
        if plain_text_only:
            self.copy_item(item_id, plain_text_only=True)
        else:
            self.paste_item(item_id)

    def _on_preview_closed(self) -> None:
        return None

    def _show_transform(self, item: ClipboardItem, action_key: str, output: str) -> None:
        self.transformer.show_result(item, action_key, output)

    def toggle_capture(self) -> None:
        if self.monitor.is_paused:
            self.monitor.resume()
            self.window.notify("已恢复采集")
        else:
            self.monitor.pause()
            self.window.notify("已暂停采集")
        self.tray.set_paused(self.monitor.is_paused)

    def show_settings(self) -> None:
        dialog = SettingsDialog(
            self.config,
            self.automation,
            self.sync.status() if self.sync else {},
            self.window,
            native_report=self.native_report(),
        )
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
            return

        self.guard.reload_rules()
        self.window.apply_theme(self.config.get("ui.theme", "system"))
        self.monitor.set_interval(int(self.config.get("capture.poll_interval_ms", 300)))
        if "sync.folder" in dialog.changes or "sync.encrypt" in dialog.changes:
            self.sync = self._build_sync()
        if self.config.get("sync.enabled", False) and self.sync is not None:
            self._sync_timer.start()
        else:
            self._sync_timer.stop()

        self.window.reload()
        self.window.notify("设置已保存")

    # ---- 定时任务 ----

    def _sweep_sensitive(self) -> None:
        try:
            self.collector.purge_sensitive()
        except Exception:  # noqa: BLE001
            _log.exception("敏感记录清理失败")

    def run_sync(self) -> None:
        if self.sync is None or not self.config.get("sync.enabled", False):
            self.window.notify("同步未启用（请在设置中配置同步目录）")
            return

        sync = self.sync

        def work() -> None:
            try:
                if isinstance(sync, LanSync):
                    imported = sync.sync_all()
                    _log.info("局域网同步完成：导入 %d 条", imported)
                    return
                exported = sync.export()
                imported = sync.import_all()
                sync.compact()
                _log.info("同步完成：导出 %d 条，导入 %d 条", exported, imported)
            except Exception:  # noqa: BLE001
                _log.exception("同步执行失败")

        threading.Thread(target=work, name="linglong-sync", daemon=True).start()

    # ---- 配置变更 ----

    def _on_config_change(self, key: str, value) -> None:
        if key == "capture.poll_interval_ms":
            self.monitor.set_interval(int(value))
        elif key == "capture.enabled":
            if value:
                self.monitor.resume()
            else:
                self.monitor.pause()
            self.tray.set_paused(self.monitor.is_paused)
        elif key.startswith("sync."):
            # 开关与具体参数分开处理：关掉时要释放监听端口，打开时再重建
            if key == "sync.enabled" and not value:
                self._stop_sync()
                self.sync = None
                self._sync_signature_cache = self._sync_signature()
                self._sync_timer.stop()
                return
            self.refresh_sync()
            if self.sync is not None and self.config.get("sync.enabled", False):
                self._sync_timer.start()
            else:
                self._sync_timer.stop()
        elif key.startswith("ocr.") or key == "ui.language":
            self.refresh_ocr()
