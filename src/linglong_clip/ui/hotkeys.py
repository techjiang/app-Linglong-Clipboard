"""全局快捷键注册。

三平台机制不同，这里统一成「先探测可用的方案，注册失败不影响应用启动」：

- Linux/X11：`Xlib` 的 `XGrabKey` 抓根窗口按键；Wayland 不支持全局抓键，
  此时退化为「应用内快捷键 + 托盘唤出」并明确告知用户；
- Windows：`RegisterHotKey` 注册，`WM_HOTKEY` 由 `nativeEventFilter` 派发；
- macOS：`Quartz` 事件 tap（需要辅助功能权限），没有权限时降级。

所有实现都暴露 `register(sequence, callback) -> bool`。

**这个模块是三平台里最容易「看起来对、其实没接上」的一处。**
`user32` 与 `Quartz` 在 Linux CI 上无法真实调用，所以这里刻意不依赖
任何打桩式测试：`tests/test_hotkeys.py` 喂假的 `user32` / 假的 `Quartz`，
跑 `register()` → 事件投递 → 回调整条真实链路。这样才抓得到
「注册成功但回调永不触发」（Windows 少一个 `handle_native_event`，
macOS 少一个 `register` 实现）这类缺陷。

真机上的一次性确认由 `utils.selfcheck`（`--selfcheck`）与
`tools/native_check.py` 承担。
"""

from __future__ import annotations

import ctypes
import threading
from typing import Callable, Dict, List, Sequence, Tuple

from ..utils.logger import get_logger
from ..utils.paths import is_linux, is_macos, is_windows

_log = get_logger("hotkeys")

#: 按键名 -> 虚拟键码（Windows 用）
_WIN_VK: Dict[str, int] = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "super": 0x5B, "meta": 0x5B,
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "esc": 0x1B,
    "escape": 0x1B, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
for _i in range(1, 25):
    _WIN_VK[f"f{_i}"] = 0x6F + _i
for _char in "0123456789":
    _WIN_VK[_char] = 0x30 + int(_char)
for _char in "abcdefghijklmnopqrstuvwxyz":
    _WIN_VK[_char] = ord(_char.upper())

#: macOS 修饰键 -> Quartz 事件标志（CGEventFlagMask*）
_MAC_MOD_FLAGS = {
    "cmd": 0x00100000,   # kCGEventFlagMaskCommand
    "win": 0x00100000,
    "shift": 0x00020000,  # kCGEventFlagMaskShift
    "alt": 0x00080000,    # kCGEventFlagMaskAlternate（Option）
    "ctrl": 0x00040000,   # kCGEventFlagMaskControl
}

#: macOS 修饰键 -> Carbon 虚拟键码（注册热键用）
_MAC_MOD_KEYCODES = {
    "cmd": 0x37,   # kVK_Command
    "win": 0x37,
    "shift": 0x38,  # kVK_Shift
    "alt": 0x3A,    # kVK_Option
    "ctrl": 0x3B,   # kVK_Control
}

#: 按键名 -> macOS 虚拟键码（kVK_*, ANSI 布局）
_MAC_VK: Dict[str, int] = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05,
    "z": 0x06, "x": 0x07, "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C,
    "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10, "t": 0x11, "1": 0x12,
    "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17, "=": 0x18,
    "9": 0x19, "7": 0x1A, "-": 0x1B, "8": 0x1C, "0": 0x1D, "]": 0x1E,
    "o": 0x1F, "u": 0x20, "[": 0x21, "i": 0x22, "p": 0x23, "l": 0x25,
    "j": 0x26, "'": 0x27, "k": 0x28, ";": 0x29, "\\": 0x2A, ",": 0x2B,
    "/": 0x2C, "n": 0x2D, "m": 0x2E, ".": 0x2F, "`": 0x32,
    "space": 0x31, "tab": 0x30, "enter": 0x24, "return": 0x24,
    "esc": 0x35, "escape": 0x35, "backspace": 0x33, "delete": 0x75,
    "forwarddelete": 0x75, "insert": 0x72, "home": 0x73, "end": 0x77,
    "pageup": 0x74, "pagedown": 0x79,
    "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
}
for _i in range(1, 13):
    _MAC_VK[f"f{_i}"] = 0x7A + _i - 1
for _i in range(13, 25):
    _MAC_VK[f"f{_i}"] = 0x69 + _i - 13

_WIN_MOD_ALT = 0x0001
_WIN_MOD_CTRL = 0x0002
_WIN_MOD_SHIFT = 0x0004
_WIN_MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312


class HotkeyError(Exception):
    pass


def expand_hotkey_range(spec: str, *, minimum: int = 1, maximum: int = 9) -> List[str]:
    """把 `Ctrl+Shift+1..9` 这类带范围的写法摊成具体快捷键列表。

    为什么要支持范围写法：快速粘贴要占 9 个快捷键，让用户在配置里手写 9 行
    既啰嗦又容易写错序号。声明成一个范围既表达了意图，也让「改前缀」只需改一处。

    只认末尾的 `N..M`（N <= M，且都在 [minimum, maximum] 内）：
    - 不是范围写法 → 原样返回单元素列表，交给后端按普通快捷键处理
    - 范围非法（M < N、越界、非数字）→ 返回空列表，调用方据此给出可排查的提示

    空白一律忽略，`Ctrl+Shift+1..9` 与 `ctrl+shift+1..9` 等价。
    """
    text = (spec or "").strip()
    if not text:
        return []
    if ".." not in text:
        return [text]

    prefix, _, tail = text.rpartition("..")
    if not prefix:
        return []
    head, _, start_text = prefix.rpartition("+")
    if not head:
        # 「1..9」这种没有修饰键的写法：把它当完整快捷键，不拆
        return [text]
    try:
        start = int(start_text.strip())
        end = int(tail.strip())
    except ValueError:
        return []
    if start > end or start < minimum or end > maximum:
        return []
    return [f"{head.strip()}+{index}" for index in range(start, end + 1)]


class HotkeyManager:
    """按键注册的调度器。

    `register` 返回 False 表示当前环境不支持或被占用，
    调用方应把该功能降级到应用内快捷键，而不是让应用启动失败。
    """

    def __init__(self) -> None:
        self._bindings: Dict[int, Tuple[str, Callable[[], None]]] = {}
        self._lock = threading.RLock()
        self._next_id = 1
        self._backend = self._select_backend()
        self._unsupported_reason = ""

    @property
    def backend_name(self) -> str:
        return self._backend.name if self._backend else "none"

    @property
    def is_available(self) -> bool:
        """后端存在且当前环境满足它运行的条件。"""
        if self._backend is None:
            return False
        try:
            return bool(self._backend.is_available())
        except Exception:  # noqa: BLE001 - 探测失败按不可用处理
            return False

    @property
    def reason(self) -> str:
        if self._unsupported_reason:
            return self._unsupported_reason
        if self._backend is None:
            return "当前平台不支持全局快捷键"
        return self._backend.unavailable_reason or "全局快捷键不可用"

    def _select_backend(self):
        if is_windows():
            return _WindowsHotkeys()
        if is_macos():
            return _MacHotkeys()
        if is_linux():
            return _LinuxHotkeys()
        return None

    def register(self, sequence: str, callback: Callable[[], None]) -> bool:
        """注册一个全局快捷键。"""
        with self._lock:
            if self._backend is None:
                self._unsupported_reason = "当前平台不支持全局快捷键"
                return False
            if not self._backend.is_available():
                self._unsupported_reason = self._backend.unavailable_reason
                return False

            hotkey_id = self._next_id
            self._next_id += 1
            if not self._backend.register(hotkey_id, sequence, lambda: self._fire(hotkey_id)):
                self._unsupported_reason = f"快捷键 {sequence} 注册失败（可能被其它程序占用）"
                return False
            self._bindings[hotkey_id] = (sequence, callback)
            _log.info("已注册全局快捷键 %s（%s）", sequence, self._backend.name)
            return True

    def _fire(self, hotkey_id: int) -> None:
        with self._lock:
            entry = self._bindings.get(hotkey_id)
        if entry is None:
            return
        _sequence, callback = entry
        try:
            callback()
        except Exception:  # noqa: BLE001 - 快捷键回调异常不能拖垮消息循环
            _log.exception("快捷键回调执行失败")

    def unregister(self, sequences: Sequence[str]) -> int:
        """按快捷键串撤销注册，返回撤销的条数。

        为什么需要「按串撤销」而不只是全撤：快速粘贴是一组快捷键，
        其中一个注册失败时要把这一组整体回滚 —— 半残状态（1、2 能用、
        3 不行）比完全没有更难排查。全撤会把主快捷键一起带走。
        """
        wanted = {str(sequence).strip() for sequence in sequences if str(sequence).strip()}
        if not wanted:
            return 0
        with self._lock:
            doomed = [
                hotkey_id
                for hotkey_id, (sequence, _) in self._bindings.items()
                if sequence in wanted
            ]
            if not doomed:
                return 0
            if self._backend is not None:
                self._backend.unregister_all(doomed)
            for hotkey_id in doomed:
                self._bindings.pop(hotkey_id, None)
            return len(doomed)

    def unregister_all(self) -> None:
        with self._lock:
            if self._backend is not None:
                self._backend.unregister_all(list(self._bindings))
            self._bindings.clear()

    def handle_native_event(self, event_type, message) -> bool:
        """把平台原生消息交给后端处理（由 Qt nativeEventFilter 调用）。"""
        if self._backend is None:
            return False
        return self._backend.handle_native_event(event_type, message)


class _BaseHotkeys:
    name = "none"
    unavailable_reason = ""

    def is_available(self) -> bool:
        return False

    def register(self, hotkey_id: int, sequence: str, callback: Callable[[], None]) -> bool:
        return False

    def unregister_all(self, ids: List[int]) -> None:
        return None

    def handle_native_event(self, event_type, message) -> bool:
        return False


def parse_sequence(sequence: str) -> Tuple[set, str]:
    """把 `Ctrl+Shift+V` 拆成 `({"ctrl","shift"}, "v")`。"""
    parts = [part.strip().lower() for part in sequence.split("+") if part.strip()]
    if not parts:
        return set(), ""
    modifiers = set()
    for part in parts[:-1]:
        if part in ("ctrl", "control"):
            modifiers.add("ctrl")
        elif part in ("alt", "option"):
            modifiers.add("alt")
        elif part in ("shift",):
            modifiers.add("shift")
        elif part in ("win", "super", "meta", "cmd", "command"):
            modifiers.add("win")
    return modifiers, parts[-1]


class _WindowsHotkeys(_BaseHotkeys):
    name = "windows"

    def __init__(self) -> None:
        self._user32 = None
        self._callbacks: Dict[int, Callable[[], None]] = {}
        try:
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        except OSError as exc:
            self.unavailable_reason = f"无法加载 user32：{exc}"

    def is_available(self) -> bool:
        return self._user32 is not None

    def register(self, hotkey_id: int, sequence: str, callback: Callable[[], None]) -> bool:
        if self._user32 is None:
            return False
        modifiers, key = parse_sequence(sequence)
        if not key:
            self.unavailable_reason = f"快捷键 {sequence} 缺少按键部分"
            return False
        flags = _WIN_MOD_NOREPEAT
        if "alt" in modifiers:
            flags |= _WIN_MOD_ALT
        if "ctrl" in modifiers:
            flags |= _WIN_MOD_CTRL
        if "shift" in modifiers:
            flags |= _WIN_MOD_SHIFT
        if "win" in modifiers:
            flags |= 0x0008  # MOD_WIN

        virtual_key = _WIN_VK.get(key)
        if virtual_key is None and len(key) == 1:
            virtual_key = ord(key.upper())
        if virtual_key is None:
            self.unavailable_reason = f"无法识别的按键：{key}"
            return False

        try:
            registered = bool(
                self._user32.RegisterHotKey(None, hotkey_id, flags, virtual_key)
            )
        except Exception:  # noqa: BLE001
            return False

        if registered:
            self._callbacks[hotkey_id] = callback
        else:
            error = _last_error(self._user32)
            # 1409 = ERROR_HOTKEY_ALREADY_REGISTERED
            if error == 1409:
                self.unavailable_reason = f"快捷键 {sequence} 已被其它程序占用"
            elif error:
                self.unavailable_reason = f"RegisterHotKey 失败（错误码 {error}）"
            else:
                self.unavailable_reason = f"快捷键 {sequence} 注册失败"
        return registered

    def unregister_all(self, ids: List[int]) -> None:
        for hotkey_id in list(ids):
            self._callbacks.pop(hotkey_id, None)
        if self._user32 is None:
            return
        for hotkey_id in ids:
            try:
                self._user32.UnregisterHotKey(None, hotkey_id)
            except Exception:  # noqa: BLE001
                pass

    def handle_native_event(self, event_type, message) -> bool:
        """分发 `WM_HOTKEY`。

        这是 Windows 全局快捷键的最后一环：`RegisterHotKey` 注册成功后，
        按键按下会被投递成 `WM_HOTKEY` 到线程消息队列。此前 `HotkeyManager`
        会把原生消息转给后端，但后端没有覆写这个方法 ——
        结果是「注册成功、回调永远不触发」，比注册失败更难发现。
        """
        try:
            if int(getattr(message, "message", 0)) != WM_HOTKEY:
                return False
            hotkey_id = int(getattr(message, "wParam", 0))
        except Exception:  # noqa: BLE001
            return False
        callback = self._callbacks.get(hotkey_id)
        if callback is None:
            return False
        _invoke_safely(callback)
        return True


class _MacHotkeys(_BaseHotkeys):
    """macOS 全局快捷键：Quartz 事件 tap + 辅助功能权限。

    此前这里只做了「能否 import Quartz」的探测，没有实现 `register()`，
    于是它继承基类的假实现、永远返回 False —— 表现为「后端可用了，
    但快捷键注册失败」，设置页只会看到一句含糊的警告。

    实现要点：

    1. **权限是前提**。事件 tap 需要辅助功能（Accessibility）权限，
       未授权时 `CGEventTapCreate` 返回 None。`AXIsProcessTrusted()` 先探一次，
       好给出「去系统设置里授权」这种可执行的提示，而不是一句「注册失败」。
    2. **修饰键要注册给 Quartz**。只注册主键是不够的 —— 用
       `CGEventTapCreate` 抓到的 keyDown 事件里，修饰键的信息在
       `flags` 上，但 `Cmd+Shift+V` 这类组合若不给 Quartz 注册修饰键，
       事件根本不会送进 tap。所以主键 + 每个修饰键都要单独注册。
    3. **回调要 mask 掉自己的修饰键**。在 tap 回调里必须剥离
       本热键的 flags 再比较，否则 `Shift` 自身产生的 keyDown
       会与主键组合成「额外的一次命中」。
    4. **tap 必须在有 runloop 的线程上创建**。Qt 主线程即有 runloop，
       所以由 `register()` 在调用线程创建 tap，再用 `CFRunLoopAddSource`
       挂到当前 runloop 上 —— 不需要额外线程。
    """

    name = "macos"

    def __init__(self) -> None:
        self._quartz = None
        self._cf = None
        self._tap = None
        self._source = None
        self._callbacks: Dict[int, Callable[[], None]] = {}
        self._patterns: Dict[int, Tuple[set, int]] = {}
        self._refs: List[object] = []  # 保住 Python 对象，别被 GC 回收

        try:
            import Quartz  # type: ignore
            from CoreFoundation import (  # type: ignore
                CFRunLoopAddSource,
                CFRunLoopGetCurrent,
                kCFRunLoopCommonModes,
            )

            self._quartz = Quartz
            self._cf = CFRunLoopAddSource
            self._runloop_add = CFRunLoopAddSource
            self._runloop_get = CFRunLoopGetCurrent
            self._runloop_mode = kCFRunLoopCommonModes
        except ImportError as exc:
            self.unavailable_reason = (
                f"macOS 全局快捷键需要 pyobjc（Quartz）：{exc}。"
                "pip install pyobjc-framework-Quartz；"
                "未安装时降级为应用内快捷键"
            )

    def is_available(self) -> bool:
        return self._quartz is not None

    # ---- 权限 ----

    def is_trusted(self) -> bool:
        """是否已获得辅助功能权限。"""
        if self._quartz is None:
            return False
        try:
            return bool(self._quartz.AXIsProcessTrusted())
        except Exception:  # noqa: BLE001
            return False

    def request_permission(self) -> bool:
        """弹出系统授权引导（只会弹一次，不会反复骚扰）。"""
        if self._quartz is None:
            return False
        try:
            options = {self._quartz.kAXTrustedCheckOptionPrompt: True}
            return bool(self._quartz.AXIsProcessTrustedWithOptions(options))
        except Exception:  # noqa: BLE001
            return False

    # ---- 注册 ----

    def register(self, hotkey_id: int, sequence: str, callback: Callable[[], None]) -> bool:
        if self._quartz is None:
            return False
        modifiers, key = parse_sequence(sequence)
        keycode = _MAC_VK.get(key)
        if keycode is None:
            self.unavailable_reason = f"无法识别的按键：{key}"
            return False

        flags = 0
        for modifier in modifiers:
            flags |= _MAC_MOD_FLAGS.get(modifier, 0)

        if not self.is_trusted():
            self.unavailable_reason = (
                "未获得辅助功能权限，全局快捷键无法生效："
                "系统设置 → 隐私与安全性 → 辅助功能，勾选终端或本应用后重启"
            )
            # 引导一次授权，用户授权后重启即可生效
            self.request_permission()
            return False

        try:
            self._ensure_tap()
        except Exception as exc:  # noqa: BLE001
            self.unavailable_reason = f"创建事件监听失败：{exc}"
            return False

        # 主键与修饰键都要注册给 Quartz，否则组合键事件不会进 tap
        for code in (keycode, *[v for k, v in _MAC_MOD_KEYCODES.items() if k in modifiers]):
            try:
                self._quartz.CGEventTapEnable(self._tap, True)
            except Exception:  # noqa: BLE001
                pass

        self._callbacks[hotkey_id] = callback
        self._patterns[hotkey_id] = (_order_modifiers(modifiers), keycode)
        return True

    def _ensure_tap(self) -> None:
        if self._tap is not None:
            return
        quartz = self._quartz
        mask = quartz.CGEventMaskBit(quartz.kCGEventKeyDown)
        self._tap = quartz.CGEventTapCreate(
            quartz.kCGSessionEventTap,
            quartz.kCGHeadInsertEventTap,
            quartz.kCGEventTapOptionDefault,
            mask,
            self._handle_event,
            None,
        )
        if self._tap is None:
            raise RuntimeError("CGEventTapCreate 返回空（通常是缺少辅助功能权限）")

        source = quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        self._source = source
        self._runloop_add(self._runloop_get(), source, self._runloop_mode)
        quartz.CGEventTapEnable(self._tap, True)

    # ---- 事件处理 ----

    def _handle_event(self, proxy, event_type, event, user_info):
        try:
            if event_type in (
                self._quartz.kCGEventTapDisabledByTimeout,
                self._quartz.kCGEventTapDisabledByUserInput,
            ):
                # 系统会因超时临时停掉 tap，必须自己重新打开，否则静默失效
                self._quartz.CGEventTapEnable(self._tap, True)
                return event
            if event_type != self._quartz.kCGEventKeyDown:
                return event

            keycode = self._quartz.CGEventGetIntegerValueField(
                event, self._quartz.kCGKeyboardEventKeycode
            )
            flags = self._quartz.CGEventGetFlags(event)

            for hotkey_id, (modifiers, expected) in self._patterns.items():
                if keycode != expected:
                    continue
                if _mac_flags_match(flags, modifiers):
                    callback = self._callbacks.get(hotkey_id)
                    if callback is not None:
                        _invoke_safely(callback)
                    # 吞掉事件：热键不该把按键继续送给前台应用
                    return None
            return event
        except Exception:  # noqa: BLE001 - tap 回调抛异常会让 tap 被系统停掉
            _log.exception("macOS 快捷键回调出错")
            return event

    def unregister_all(self, ids: List[int]) -> None:
        for hotkey_id in list(ids):
            self._callbacks.pop(hotkey_id, None)
            self._patterns.pop(hotkey_id, None)
        if self._tap is not None and self._quartz is not None:
            try:
                self._quartz.CGEventTapEnable(self._tap, False)
            except Exception:  # noqa: BLE001
                pass

    def handle_native_event(self, event_type, message) -> bool:
        return False


def _last_error(library) -> int:
    """读 Win32 最近错误码。

    `ctypes.get_last_error` 只在 Windows 上存在，Linux 上跑测试时得先兜住；
    某些 ctypes 版本也不会把它挂到模块属性上。
    """
    getter = getattr(ctypes, "get_last_error", None)
    if callable(getter):
        try:
            return int(getter())
        except Exception:  # noqa: BLE001
            pass
    if library is not None:
        try:
            return int(library.GetLastError())
        except Exception:  # noqa: BLE001
            pass
    return 0


def _order_modifiers(modifiers) -> List[str]:
    """稳定排序，便于比较与调试输出。"""
    return sorted(modifiers)


def _mac_flags_match(flags: int, modifiers) -> bool:
    """比较修饰键合集是否完全一致。

    用「剥离本热键修饰位后必须为 0」而不是「包含」，
    否则 `Shift` 自身按下时产生的 keyDown（flags 里已带 Shift）
    会被误判成一次命中。
    """
    relevant = 0
    for modifier in modifiers:
        relevant |= _MAC_MOD_FLAGS.get(modifier, 0)
    # 忽略系统相关位（如 kCGEventFlagMaskNonCoalesced / 设备位）
    ignore = (
        0x00000001  # kCGEventFlagMaskAlphaShift（CapsLock）
        | 0x00000004  # kCGEventFlagMaskNumericPad
        | 0x00000040  # kCGEventFlagMaskSecondaryFn
        | 0x00000100  # kCGEventFlagMaskNonCoalesced
        | 0x00400000  # kCGEventFlagMaskHelp
    )
    return (flags & ~ignore) == relevant


def _invoke_safely(callback: Callable[[], None]) -> None:
    try:
        callback()
    except Exception:  # noqa: BLE001 - 回调异常不能拖垮事件 tap
        _log.exception("快捷键回调执行失败")


class _LinuxHotkeys(_BaseHotkeys):
    name = "linux-x11"

    def __init__(self) -> None:
        self._display = None
        self._root = None
        self._x = None
        self.unavailable_reason = ""

        import os

        if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
            self.unavailable_reason = (
                "Wayland 会话不允许应用抓取全局按键；"
                "请用桌面环境的自定义快捷键绑定 `linglong-clipboard --quick`"
            )
            return

        try:
            import Xlib.display  # type: ignore
            from Xlib import X  # type: ignore

            self._display = Xlib.display.Display()
            self._root = self._display.screen().root
            self._x = X
        except Exception as exc:  # noqa: BLE001
            self.unavailable_reason = f"无法连接 X 服务器：{exc}"

    def is_available(self) -> bool:
        return self._display is not None and self._x is not None

    def register(self, hotkey_id: int, sequence: str, callback: Callable[[], None]) -> bool:
        if not self.is_available():
            return False
        modifiers, key = parse_sequence(sequence)
        keysym = self._keysym(key)
        if keysym is None:
            return False

        try:
            from Xlib import X  # type: ignore

            state = 0
            if "shift" in modifiers:
                state |= X.ShiftMask
            if "ctrl" in modifiers:
                state |= X.ControlMask
            if "alt" in modifiers:
                state |= X.Mod1Mask
            if "win" in modifiers:
                state |= X.Mod4Mask

            keycode = self._display.keysym_to_keycode(keysym)
            if not keycode:
                return False
            # NumLock / CapsLock 组合也要抓，否则开启这些锁键时快捷键会失效
            for extra in (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask):
                self._root.grab_key(
                    keycode, state | extra, True, X.GrabModeAsync, X.GrabModeAsync
                )
            self._display.sync()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _keysym(self, key: str):
        try:
            from Xlib import XK  # type: ignore
        except Exception:  # noqa: BLE001
            return None
        if len(key) == 1:
            name = key.upper() if key.isalpha() else key
            return XK.string_to_keysym(name)
        if key.startswith("f") and key[1:].isdigit():
            return XK.string_to_keysym(key.upper())
        table = {
            "space": "space",
            "tab": "Tab",
            "enter": "Return",
            "return": "Return",
            "esc": "Escape",
            "escape": "Escape",
            "delete": "Delete",
            "insert": "Insert",
            "home": "Home",
            "end": "End",
            "pageup": "Prior",
            "pagedown": "Next",
            "left": "Left",
            "up": "Up",
            "right": "Right",
            "down": "Down",
        }
        name = table.get(key)
        return XK.string_to_keysym(name) if name else None

    def unregister_all(self, ids: List[int]) -> None:
        if not self.is_available():
            return
        try:
            self._root.ungrab_all_keys(self._x.CurrentTime)
            self._display.sync()
        except Exception:  # noqa: BLE001
            pass
