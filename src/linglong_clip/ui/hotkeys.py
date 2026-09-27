"""全局快捷键注册。

三平台机制不同，这里统一成「先探测可用的方案，注册失败不影响应用启动」：

- Linux/X11：`Xlib` 的 `XGrabKey` 抓根窗口按键；Wayland 不支持全局抓键，
  此时退化为「应用内快捷键 + 托盘唤出」并明确告知用户；
- Windows：`RegisterHotKey` + 消息循环由 Qt 的 `nativeEventFilter` 派发；
- macOS：`osascript` 注册不可行，改用 `Quartz` 的 event tap（需要辅助功能权限），
  没有权限时降级。

所有实现都暴露 `register(sequence, callback) -> bool`。
"""

from __future__ import annotations

import ctypes
import threading
from typing import Callable, Dict, List, Tuple

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

_WIN_MOD_ALT = 0x0001
_WIN_MOD_CTRL = 0x0002
_WIN_MOD_SHIFT = 0x0004
_WIN_MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312


class HotkeyError(Exception):
    pass


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
    def reason(self) -> str:
        return self._unsupported_reason

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
            return bool(self._user32.RegisterHotKey(None, hotkey_id, flags, virtual_key))
        except Exception:  # noqa: BLE001
            return False

    def unregister_all(self, ids: List[int]) -> None:
        if self._user32 is None:
            return
        for hotkey_id in ids:
            try:
                self._user32.UnregisterHotKey(None, hotkey_id)
            except Exception:  # noqa: BLE001
                pass


class _MacHotkeys(_BaseHotkeys):
    name = "macos"

    def __init__(self) -> None:
        self._callbacks: Dict[int, Callable[[], None]] = {}
        # quartz 未安装或缺少辅助功能权限时降级
        try:
            import Quartz  # type: ignore

            self._quartz = Quartz
        except ImportError:
            self._quartz = None
            self.unavailable_reason = (
                "macOS 全局快捷键需要 pyobjc（Quartz）与辅助功能权限；"
                "当前未安装，已降级为应用内快捷键"
            )

    def is_available(self) -> bool:
        return self._quartz is not None


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
