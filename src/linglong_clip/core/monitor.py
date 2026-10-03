"""剪贴板监听：把系统剪贴板的变化送进采集管线。

轮询而非纯事件驱动的原因：三平台里只有 Windows 有可靠的剪贴板变更通知，
X11/Wayland/macOS 都需要轮询或原生扩展。默认 300ms 一次，
每次只做「序列号/内容指纹比对」，实测 CPU 占用可忽略。
Windows 上若注册了 `AddClipboardFormatListener`，Qt 层会把事件喂给 `on_native_event`，
此时自动跳过本轮轮询比对。

除剪贴板（CLIPBOARD）外，Linux 还有一个独立的**主选择区**（PRIMARY，选中即生效）。
它和剪贴板是两条互不相干的通道，所以有独立的指纹与独立的开关
（`monitor_primary`）：只要求监听剪贴板时，不该因为选了一段文字就被记下来。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional

from ..platform.base import FocusInfo
from ..utils.logger import get_logger
from .models import ClipboardItem, content_hash

_log = get_logger("monitor")

#: 被排除的文件管理器内部路径，避免记录临时文件
_IGNORED_TEXT_PREFIXES = ("x-special/", "text/uri-list")


class ClipboardMonitor:
    """剪贴板采集调度器。

    只负责「发现变化 → 读内容 → 交给回调」，不做落库与过滤，
    这样采集逻辑可以在无 GUI 的测试里被替换成假剪贴板。

    **线程约定**：`read_clipboard` 由 `gui_invoke` 指定的可调用对象执行。
    Qt 的剪贴板对象不能跨线程使用（会拿到空数据），
    因此默认实现把读取投递到 GUI 线程，只有在同步执行时才就地调用。
    """

    def __init__(
        self,
        clipboard,
        *,
        interval_ms: int = 300,
        on_capture: Optional[Callable[[ClipboardItem], None]] = None,
        backend=None,
        monitor_primary: bool = False,
        primary_interval_ms: int = 1000,
        clock: Callable[[], float] = time.time,
        gui_invoke: Optional[Callable[[Callable[[], object], bool], object]] = None,
    ) -> None:
        self._clipboard = clipboard
        self._interval = max(60, int(interval_ms)) / 1000.0
        self._on_capture = on_capture
        self._backend = backend
        self._clock = clock
        #: (callable, blocking) -> result，用于把剪贴板读取投递到 GUI 线程
        self._gui_invoke = gui_invoke

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

        self._last_hash = ""
        self._last_change_at = 0.0
        self._paused = False
        self._event_driven = False
        self._capture_count = 0

        #: 主选择区（Linux）采集开关与独立节流
        self._monitor_primary = bool(monitor_primary)
        self._primary_interval = max(100, int(primary_interval_ms)) / 1000.0
        self._primary_last_hash = ""
        self._primary_last_at = 0.0

    # ---- 生命周期 ----

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def capture_count(self) -> int:
        return self._capture_count

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, name="linglong-clipboard-monitor", daemon=True
            )
            self._thread.start()
            _log.info("剪贴板监听已启动，间隔 %.0fms", self._interval * 1000)

    def stop(self, timeout: float = 2.0) -> None:
        with self._lock:
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        _log.info("剪贴板监听已停止")

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False
        # 恢复时重置基线，避免把暂停期间的剪贴板内容当成新内容
        self._last_hash = ""
        self._primary_last_hash = ""

    def set_interval(self, interval_ms: int) -> None:
        self._interval = max(60, int(interval_ms)) / 1000.0

    @property
    def monitors_primary(self) -> bool:
        return self._monitor_primary

    def set_monitor_primary(self, enabled: bool) -> None:
        """开关主选择区采集，供设置页热切换。"""
        self._monitor_primary = bool(enabled)
        # 关掉再打开时重置基线：这期间选中的内容不该被当成新的
        self._primary_last_hash = ""
        _log.info("主选择区采集已%s", "启用" if self._monitor_primary else "停用")

    def set_event_driven(self, enabled: bool) -> None:
        """Windows 原生监听生效后置为 True。

        事件驱动下不需要靠密集轮询发现变化，但**不能完全停掉轮询**：
        `AddClipboardFormatListener` 在会话切换、剪贴板持有者崩溃等情况下
        可能漏投通知。所以这里只把间隔放宽（由调用方通过 `set_interval` 设定），
        保留一条低频兜底路径。
        """
        self._event_driven = enabled
        _log.info("剪贴板监听切换为%s", "事件驱动（含低频兜底）" if enabled else "轮询")

    @property
    def is_event_driven(self) -> bool:
        return self._event_driven

    # ---- 事件入口 ----

    def on_native_event(self) -> None:
        """由 Qt nativeEventFilter 在收到剪贴板变更时调用。"""
        if self._paused:
            return
        self._poll_once()

    def mark_self_write(self, text: str, kind: str = "text") -> None:
        """把自己写入剪贴板的内容记为已见，避免立刻又被采集回来。"""
        with self._lock:
            self._last_hash = content_hash(kind, text)

    # ---- 内部 ----

    def _run(self) -> None:
        # 启动时先建立基线，不把「启动前剪贴板里已有的内容」当新内容采集
        self._prime()
        while not self._stop_event.is_set():
            if not self._paused:
                try:
                    self._poll_once()
                except Exception:  # noqa: BLE001 - 采集线程必须活下去
                    _log.exception("采集循环出错")
                try:
                    self._poll_primary_once()
                except Exception:  # noqa: BLE001
                    _log.exception("主选择区采集出错")
            self._stop_event.wait(self._interval)

    def _prime(self) -> None:
        """建立基线：启动前剪贴板里已有的内容不应被当成新采集。"""
        item = self.read_current()
        if item and not item.is_empty:
            with self._lock:
                self._last_hash = self._fingerprint(item)

    def _poll_once(self) -> None:
        # 无论轮询还是事件驱动，都靠指纹比对去重：
        # 原生事件可能由我们自己的写入触发，不能无条件当成新内容。
        item = self.read_current()
        if item is None or item.is_empty:
            return

        fingerprint = self._fingerprint(item)
        with self._lock:
            if fingerprint == self._last_hash:
                return
            self._last_hash = fingerprint
            self._last_change_at = self._clock()

        self._capture_count += 1
        if self._on_capture:
            try:
                self._on_capture(item)
            except Exception:  # noqa: BLE001
                _log.exception("采集回调执行失败")

    def _poll_primary_once(self) -> None:
        """采集主选择区（Linux PRIMARY）。

        为什么不复用剪贴板那套「每次轮询都读」：主选择区跟着鼠标选中走，
        用户拖选一段长文本的过程中每一帧都是「变化」，按 300ms 读会
        把中间态全存下来，也会让 X 选择请求持续被打断。
        所以这里独立节流（默认 1s）——**读取本身就充当防抖**：
        拖选过程中的内容几乎不会被读到，用户停下来之后那一版才会被采到。
        """
        # `_paused` 是「暂停采集」的总开关，两条通道都得认。
        # 只在 `_run` 里判外层不够：`on_native_event` 与测试都是直接调本方法的。
        if self._paused or not self._monitor_primary or self._backend is None:
            return
        now = self._clock()
        if now - self._primary_last_at < self._primary_interval:
            return
        self._primary_last_at = now

        item = self.read_primary()
        if item is None or item.is_empty:
            return
        fingerprint = self._fingerprint(item)
        with self._lock:
            if fingerprint == self._primary_last_hash:
                return
            self._primary_last_hash = fingerprint
            self._last_change_at = now

        self._capture_count += 1
        if self._on_capture:
            try:
                self._on_capture(item)
            except Exception:  # noqa: BLE001
                _log.exception("采集回调执行失败")

    def _fingerprint(self, item: ClipboardItem) -> str:
        if item.kind == "image":
            return item.hash
        return item.hash or content_hash(item.kind, item.content)

    # ---- 读取剪贴板 ----

    def read_current(self) -> Optional[ClipboardItem]:
        """读取当前剪贴板并转成 ClipboardItem。由 Qt 层实现 `read_clipboard`。"""
        reader = getattr(self._clipboard, "read_clipboard", None)
        if reader is None:
            return None
        try:
            if self._gui_invoke is not None:
                return self._gui_invoke(reader, True)  # type: ignore[return-value]
            return reader()
        except Exception:  # noqa: BLE001 - 读取失败（如剪贴板被占用）下一轮重试
            _log.debug("读取剪贴板失败", exc_info=True)
            return None

    def read_primary(self) -> Optional[ClipboardItem]:
        """读取主选择区（Linux 专有），供「即选即存」模式使用。"""
        if self._backend is None:
            return None
        text = ""
        try:
            text = self._backend.read_primary_text()
        except Exception:  # noqa: BLE001
            return None
        if not text or len(text) > 100_000:
            return None
        item = ClipboardItem(kind="text", content=text, hash=content_hash("text", text))
        # 标记来源通道，便于界面区分「复制来的」还是「选中就记下的」，
        # 也让这条记录在隐私判定里被正常对待（判定读的是内容，与此无关）。
        item.metadata["clipboard_channel"] = "primary"
        return item


class FocusTracker:
    """缓存前台窗口信息，减少原生调用频率。

    采集发生时只需要「上一条记录来自哪个应用」，
    每秒查询一次足够，没必要每次采集都调 Win32/X11。
    """

    def __init__(self, backend, interval: float = 1.0, clock: Callable[[], float] = time.time):
        self._backend = backend
        self._interval = interval
        self._clock = clock
        self._cached = FocusInfo()
        self._checked_at = 0.0

    def current(self) -> FocusInfo:
        now = self._clock()
        if now - self._checked_at < self._interval:
            return self._cached
        self._checked_at = now
        try:
            self._cached = self._backend.current_focus() if self._backend else FocusInfo()
        except Exception:  # noqa: BLE001
            self._cached = FocusInfo()
        return self._cached
