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
from dataclasses import replace
from typing import List, Optional

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QTimer, Qt, Signal
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from ..core.automation import AutomationEngine
from ..core.collector import CAPTURED, MERGED, Collector
from ..core.copy_guard import ClipboardReleaser, plan_outbound
from ..core.models import PINNED_BY_KEY, ClipboardItem
from ..core.monitor import ClipboardMonitor, FocusTracker
from ..core.ocr import build_engine as build_ocr_engine
from ..core.privacy import PrivacyGuard
from ..core.semantic import SemanticEngine
from ..core.repository import open_repository
from ..core.lan_sync import LanSync, build_lan_sync
from ..core.sync import FolderSync, SyncCipher
from ..core.sync_cursor import SyncCursorStore
from ..core.tombstones import TombstoneStore, fingerprint
from ..core.flag_changes import (
    FLAG_FAVORITE,
    FLAG_PINNED,
    FlagChangeStore,
)
from ..platform import detect_backend
from ..utils.config import Config
from ..utils.logger import get_logger, setup_logging
from ..utils.paths import is_linux
from .clipboard_qt import QtClipboardAdapter, clipboard_payload
from .hotkeys import HotkeyManager, expand_hotkey_range
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

    def __init__(
        self,
        argv: list,
        *,
        start_minimized: bool = False,
        monitor_primary: bool = False,
    ) -> None:
        super().__init__()
        self._argv = argv
        #: CLI 的 `--minimized` 是显式指令；没给时由 `ui.start_minimized` 决定，
        #: 在 `run()` 里解析（那时配置已加载）。
        self._start_minimized = start_minimized
        self._start_minimized_explicit = start_minimized
        #: CLI 的 `--monitor-primary` 同样是显式指令，优先级高于配置。
        #: 与 `--minimized` 一样只在构造时生效，不写回配置文件 ——
        #: 一次性的命令行参数不该悄悄改掉用户的持久设置。
        self._monitor_primary_explicit = monitor_primary
        #: 实际注册成功的快速粘贴快捷键（注册失败时为空）
        self._quick_hotkey_sequences: List[str] = []
        #: 出站规则的临时产物。为 None 时 `copy_item` 写原记录；
        #: 有值时写它 —— 出站加工的结果只进剪贴板，不写回历史。
        self._outbound_override: Optional[ClipboardItem] = None
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
        self.tombstones = TombstoneStore()
        self.tombstones.load()
        # 标记变更通道：让「取消固定/取消收藏」也能跨设备传播。
        # 与墓碑并列，见 `core/flag_changes.py`。
        self.flag_changes = FlagChangeStore()
        self.flag_changes.load()

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
            monitor_primary=self._monitor_primary_explicit
            or self._resolve_monitor_primary(),
            primary_interval_ms=int(
                self.config.get("capture.primary_interval_ms", 1000)
            ),
            gui_invoke=self._invoker.call,
        )
        #: 复制出站后的收尾（标记不进系统历史、限时清空）
        self.releaser = ClipboardReleaser(
            read_text=self._read_clipboard_text,
            clear=self.clipboard.clear,
        )
        self.focus = FocusTracker(self.backend)

        #: 原生能力自检结果，首次访问设置页时惰性计算
        self._native_report = None

        self._wire_signals()
        self._setup_native_event_filter()
        self._setup_timers()
        self._install_hotkeys()

        if monitor_primary:
            # 让设置页显示的状态与实际行为一致：开关是「生效中」的，
            # 否则用户会看到界面说关、程序却在采集主选择区。
            self.config.set("capture.monitor_primary", True)

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

    def _resolve_monitor_primary(self) -> bool:
        """是否采集主选择区（Linux PRIMARY）。

        两个条件都要满足：用户在设置里开了 `capture.monitor_primary`，
        **且**当前后端真的读得到主选择区。只看配置会在 Windows/macOS 上
        留下一个永远无效的开关 —— 同前几轮的 `transient_clipboard` 写死 True。
        后端探测失败一律算「不支持」，而不是让开关悬在那里。
        """
        if not bool(self.config.get("capture.monitor_primary", False)):
            return False
        reader = getattr(self.backend, "read_primary_text", None)
        if reader is None:
            return False
        try:
            caps = self.backend.capabilities()
        except Exception:  # noqa: BLE001
            return False
        return bool(caps.get("primary_selection", False))

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
                self.storage, self.config, self.guard, cursors=self.sync_cursors,
                tombstones=self.tombstones, flag_changes=self.flag_changes,
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
            tombstones=self.tombstones,
            flag_changes=self.flag_changes,
        )

    def _wire_signals(self) -> None:
        self.item_captured.connect(self._on_captured_ui)

        self.window.close_requested.connect(self._on_window_close_requested)
        self.window.copy_requested.connect(self.copy_item)
        self.window.outbound_requested.connect(self.apply_outbound_rules)
        self.window.paste_requested.connect(self.paste_item)
        self.window.delete_requested.connect(self.delete_item)
        self.window.flag_toggled.connect(self.toggle_flag)
        self.window.clear_requested.connect(self.clear_unpinned)
        self.window.compact_requested.connect(self.compact_storage)
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
        if not self.hotkeys.register(sequence, self.show_quick_paste):
            # 注册失败不影响使用：应用内 Ctrl+Shift+V 与托盘菜单仍然可用
            reason = self.hotkeys.reason or "全局快捷键不可用"
            _log.warning("全局快捷键未启用：%s", reason)
            self._hotkey_warning = reason
            if is_linux():
                self.tray.setToolTip(f"玲珑剪切板 · 全局快捷键不可用（{reason}）")
            return

        self.tray.setToolTip(f"玲珑剪切板 · {sequence} 唤起快速粘贴")
        self._install_quick_paste_hotkeys()

    def _install_quick_paste_hotkeys(self) -> int:
        """按 `ui.quick_paste_hotkey` 注册「直接粘贴第 N 条」的全局快捷键。

        这个配置项此前只被设置页显示、没有任何读取方 —— 界面上写着
        「Ctrl+Shift+1..9」，实际上一个都没注册，用户按下去什么也不会发生。
        现在按声明真的注册。

        两处刻意的行为：

        - **整体成功才留下**。若 9 个里有被占用的，把已注册的撤掉，
          避免「1、2 能用、3 不行」这种半残状态 —— 半残比完全没有更难排查。
        - **失败不报错，只记日志**。快速粘贴的快捷键是便利功能，
          注册不到不该影响主快捷键与托盘菜单。
        """
        spec = str(self.config.get("ui.quick_paste_hotkey", "") or "")
        sequences = expand_hotkey_range(spec)
        if not sequences:
            if spec.strip():
                _log.warning("快速粘贴快捷键写法无法解析，已跳过：%r", spec)
            return 0

        succeeded: List[str] = []
        for index, sequence in enumerate(sequences, start=1):
            if self.hotkeys.register(sequence, lambda i=index: self._paste_nth_quick(i)):
                succeeded.append(sequence)
                continue
            # 有一个没注册上就把这一批全撤掉，不留半残状态
            _log.warning(
                "快速粘贴快捷键 %s 注册失败（%s），该组快捷键整体不启用",
                sequence,
                self.hotkeys.reason or "可能被占用",
            )
            self.hotkeys.unregister(succeeded)
            return 0

        _log.info("已注册 %d 个快速粘贴快捷键（%s）", len(succeeded), spec)
        self._quick_hotkey_sequences = list(sequences)
        return len(succeeded)

    def _paste_nth_quick(self, index: int) -> None:
        """快速粘贴第 index 条：先唤起浮层，再按序号粘贴。"""
        self.show_quick_paste()
        self.preview.paste_nth(index)

    # ---- 启动与退出 ----

    def _resolve_start_minimized(self) -> bool:
        """决定启动时是否直接进托盘。

        CLI 显式传了 `--minimized` 就以它为准；否则读 `ui.start_minimized`。
        配置里默认 `True`，符合剪贴板工具的直觉 —— 它是后台常驻程序，
        每次开机都弹一个窗口出来是打扰。
        """
        if self._start_minimized_explicit:
            return True
        return bool(self.config.get("ui.start_minimized", True))

    def _on_window_close_requested(self) -> None:
        """主窗口关闭按钮的落点。

        `ui.close_to_tray` 为真时收进托盘（默认），为假时执行真正的退出。
        托盘不可用时只能退出 —— 收进托盘等于让用户没办法再把它叫回来。
        """
        if not self.config.get("ui.close_to_tray", True) or not QSystemTrayIcon.isSystemTrayAvailable():
            self.quit()
            return
        self.window.hide()
        _log.info("主窗口已收进托盘，采集继续在后台运行")

    def run(self) -> int:
        self._start_minimized = self._resolve_start_minimized()
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
        # 取消待执行的限时清空：进程都要退了，再清一次剪贴板没有意义，
        # 反而可能打断用户刚复制的内容。
        self.releaser.cancel_all()
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

    def apply_outbound_rules(self, item_id: int, plain_text_only: bool = False) -> str:
        """把这条记录按 `trigger = outbound` 的规则加工后写进剪贴板。

        这是「复制前清理链接跟踪参数」这类规则的落点。规则此前只能在界面上
        配、在文档里写，**没有任何执行时机** —— 配了也没人生效。

        一段刻意的顺序：**规则产物不落库**。加工的产物只进这一次的剪贴板，
        历史里留的仍是原始内容。理由是出站加工常常是「为了这次粘贴顺手清理一下」，
        把结果写回历史会让用户在历史里找不到自己原本复制的东西，
        而那种「我明明复制的是官网链接，怎么变成别的了」的困惑比便利更贵。

        隐私与出站防护没有被绕开：内容先按原记录算出敏感性（原记录才是
        用户真正保留的那份），再交给 `copy_item` 走它既有的脱敏、
        排除系统历史、限时清空这套流程。
        """
        item = self.storage.get(item_id)
        if item is None:
            return ""

        data = self.automation.outbound_data(item)
        if not data.modified and not data.errors:
            return ""

        described = data.describe()
        if not data.modified:
            # 命中了规则但没跑成（比如内容不适用该转换）：照原样复制，
            # 但要说出来，否则用户无从判断「为什么这次没加工」
            self.window.notify(described)
            return described

        # 只本次有效：`copy_item` 收的是一份临时记录，不写回存储。
        # 指纹交给 `_mark_own_write` 按读取侧同一口径重算 —— 这里先算一个
        # 只会多出第二个口径，而两处口径不一致正是自我写入标记失效的原因。
        patched = replace(item)
        patched.content = data.text
        patched.html = ""
        patched.hash = ""

        self._outbound_override = patched
        try:
            self.copy_item(item_id, plain_text_only=plain_text_only)
        finally:
            self._outbound_override = None

        self.window.notify(described)
        return described

    def copy_item(self, item_id: int, plain_text_only: bool = False) -> None:
        item = self.storage.get(item_id)
        if item is None:
            return

        # 出站规则的产物优先：它只影响这次写入剪贴板，历史里仍是原记录
        outbound = self._outbound_override

        text = outbound.content if outbound is not None else item.content
        redacted = item.sensitive and bool(
            self.config.get("privacy.redact_on_copy_out", False)
        )
        if redacted:
            text = self.guard.redact(text)
        plan = self._outbound_plan(item, redacted=redacted)
        if redacted:
            patched = ClipboardItem(
                kind=item.kind,
                content=text,
                html="",
                files=item.files,
                image_path=item.image_path,
            )
            self.clipboard.write_item(
                patched,
                plain_text_only=plain_text_only,
                exclude_from_history=plan.exclude_from_history,
            )
        else:
            self.clipboard.write_item(
                outbound if outbound is not None else item,
                plain_text_only=plain_text_only,
                exclude_from_history=plan.exclude_from_history,
            )

        # 记下指纹，避免自己的写入又被采集一遍。
        # 有出站产物时按产物算：历史里留下的那条仍是原记录，
        # 而剪贴板里现在装的是加工后的内容，采集线程看到的正是后者。
        self._mark_own_write(outbound if outbound is not None else item, text)
        self.storage.touch(item_id)
        self.window.reload()

        note = self._schedule_outbound_release(plan, text, item.kind)
        self.window.notify(f"已复制：{item.kind_label}{note}")

    def copy_text(self, text: str) -> None:
        # 转换结果也可能就是一段密钥（「提取 API Key」这种转换就产出它），
        # 所以同样过一遍出站判定，而不是当作普通文本直接写出去。
        plan = plan_outbound(
            sensitive=self.guard.inspect(ClipboardItem(kind="text", content=text)).sensitive,
            auto_clear_seconds=self._auto_clear_seconds(),
        )
        self.clipboard.set_text(text, exclude_from_history=plan.exclude_from_history)
        self._mark_own_write(None, text)
        note = self._schedule_outbound_release(plan, text, "text")
        self.window.notify(f"已复制转换结果{note}")

    def _mark_own_write(self, item: Optional[ClipboardItem], text: str) -> None:
        """告诉采集线程「剪贴板里这份是我们自己写的」，别又记一遍。

        两件事必须与读取侧完全一致，缺一个标记就失效（历史里会凭空多出
        一条自己操作产生、来源不明的记录）：

        - **载荷**：向 `QtClipboardAdapter` 要口径（`clipboard_payload()`），
          而不是自己再挑一个字段。`mark_self_write()` 内部会拿它去算指纹，
          所以这里传的是**载荷**而不是算好的指纹 —— 传指纹会被再哈希一次，
          又是一处对不上的口径。
        - **类型**：一律 `text`。写进系统剪贴板的是纯文本，
          读取侧也是按 `text` 算指纹 —— 这里若沿用记录自己的类型
          （从历史里复制 `link` 时就是 `link`），两边算出的指纹不同，
          比对立刻失败。
        """
        html = item.html if item is not None and item.kind == "rich" else ""
        self.monitor.mark_self_write(clipboard_payload(text, html), "text")

    # ---- 出站防护 ----

    def _auto_clear_seconds(self) -> int:
        """自动清空时长（秒）。统一从 `PrivacyGuard` 取，避免两处口径分叉。"""
        return self.guard.auto_clear_seconds()

    def _outbound_plan(self, item: ClipboardItem, *, redacted: bool = False):
        """这条记录复制出去时该怎么处理。"""
        return plan_outbound(
            sensitive=bool(item.sensitive),
            auto_clear_seconds=self._auto_clear_seconds(),
            redacted=redacted,
        )

    def _schedule_outbound_release(self, plan, text: str, kind: str) -> str:
        """安排限时清空，返回给用户看的说明后缀。

        图片走不通这条路：剪贴板里是图像数据，`read_text()` 取不到内容，
        也就无从比对「还是不是我写的那份」。与其做一个判不准的清理，
        不如明确不承诺，并在文档里写明。
        """
        if not plan.auto_clear_seconds:
            return ""
        if kind == "image":
            return "（图片无法限时清空）"
        if not self.releaser.schedule(text, plan.auto_clear_seconds):
            return ""
        return f"（{plan.auto_clear_seconds} 秒后自动清空剪贴板）"

    def _read_clipboard_text(self) -> str:
        """读剪贴板纯文本，用于判断「还是不是我写的那份」。"""
        return self.qt_app.clipboard().text() or ""

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

    def toggle_flag(self, item_id: int, field: str, value: bool) -> None:
        """切换固定/收藏标记。

        界面只发意图，写入在这里做，因为有三件界面不该操心的事：

        1. **固定标记要记来源设备**（`pinned_by`）。删除传播默认会被固定标记
           挡住（「别动它」的意图优先），但若固定是**发起删除的那台设备**设的，
           就说明它已经改主意了，那个固定是过期残留，不该继续挡着。
           记下来源，收到删除标记时才判断得出这一点 —— 详见 `pin_is_stale`。
        2. **写入要能被同步层看见**：标记靠同步传播，顺序不能乱。
        3. **「取消」也要记成事件**。只写 `pinned=False` 的话，同步层拿到的
           仍然只是一个布尔值 —— 「取消过」和「从未设过」还是分不开，
           并集语义下取消就传不出去。所以这里额外往变更通道里记一笔
           （带时间戳的显式动作），对端才推得出「这台设备取消了」。
           详见 `core/flag_changes.py`。
        """
        if field not in ("pinned", "favorite"):
            return
        item = self.storage.get(item_id)
        if item is None:
            return

        metadata = dict(item.metadata)
        if field == "pinned":
            # 本机用户刚刚做了决定：固定就是本机设的，来源清空。
            # 空来源在 `pin_is_stale` 里表示「本机的意图」，永远不判为过期；
            # 写对端设备名反而会让本机的固定被误判成别的设备的残留。
            metadata.pop(PINNED_BY_KEY, None)
        self.storage.update_fields(
            item_id,
            metadata=metadata,
            **{field: bool(value)},
        )
        self._record_flag_change(item, field, bool(value))
        self.window.reload()

    def _record_flag_change(self, item: ClipboardItem, field: str, value: bool) -> None:
        """把这次显式动作记进变更通道（含取消）。

        只在指纹已知时记：没指纹的记录本来就不参与同步，
        记下来也只会变成一条永远匹配不上的死事件。
        失败只记日志 —— 取消传播不上不该影响用户在这台机器上的操作。
        """
        digest = fingerprint(item)
        if not digest:
            return
        flag = FLAG_PINNED if field == "pinned" else FLAG_FAVORITE
        try:
            self.flag_changes.record(digest, flag, value)
        except Exception:  # noqa: BLE001 - 变更记录是辅助信息，失败不该冒泡到界面
            _log.exception("记录标记变更失败：%s %s", digest, flag)

    def delete_item(self, item_id: int) -> None:
        # 先取指纹再删：记录没了就没法算指纹，也就没东西能告诉对端「这条删了」
        item = self.storage.get(item_id)
        digest = fingerprint(item) if item is not None else ""
        self.storage.delete(item_id)
        if digest:
            self.tombstones.record(digest)
        self.window.reload()
        self.window.notify("已删除")

    def clear_unpinned(self) -> None:
        """清理未固定记录，并给**真的消失了**的指纹记删除标记。

        走这里而不是让界面直接 `storage.clear()`：界面上那个按钮此前直接清库，
        墓碑一条都不记，于是「在这台清了、别的设备上还在」，下一轮同步又原样
        回来 —— 与「删一条」的传播漏掉是同一个错误，只是入口不同。
        CLI 的 `--clear` 早就是这么做的，两边必须一致。

        只给真正消失的指纹记墓碑：固定项不受清理影响、仍在库里，
        把它们算成删除会误删别的设备上那些记录。
        """
        from ..core.tombstones import local_fingerprints

        before = set(local_fingerprints(self.storage))
        removed = self.storage.clear(keep_pinned=True)
        survivors = set(local_fingerprints(self.storage))
        doomed = [digest for digest in before if digest not in survivors]
        recorded = self.tombstones.record_many(doomed) if doomed else 0
        _log.info("清理未固定记录 %d 条，记下 %d 条删除标记", removed, recorded)
        self.window.notify_cleared(removed)

    def compact_storage(self) -> None:
        """压缩存储文件。界面只发意图，实际动作留在这里，保持分层。"""
        self.storage.optimize()
        self.window.notify_compacted()

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
        self.window.apply_density(
            self.config.get("ui.card_density", "comfortable")
        )
        self.monitor.set_interval(int(self.config.get("capture.poll_interval_ms", 300)))
        self.monitor.set_monitor_primary(self._resolve_monitor_primary())
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
        elif key in ("capture.monitor_primary", "capture.primary_interval_ms"):
            self.monitor._primary_interval = max(
                100, int(self.config.get("capture.primary_interval_ms", 1000))
            ) / 1000.0
            self.monitor.set_monitor_primary(self._resolve_monitor_primary())
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
        elif key in ("ui.global_hotkey", "ui.quick_paste_hotkey"):
            # 快捷键改了要重注册：只把主快捷键撤掉重来，快速粘贴那一组
            # 由 `_install_hotkeys` 一并重建，避免旧绑定残留在系统里。
            self.hotkeys.unregister_all()
            self._quick_hotkey_sequences = []
            self._install_hotkeys()
