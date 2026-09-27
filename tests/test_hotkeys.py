"""全局快捷键：序列解析、三平台注册链路、回调分发。

这里的关键手法是**仿真原生层**而不是打桩断言：
Windows 分支喂一个记录调用的假 `user32`，macOS 分支喂一个假 `Quartz`，
然后走真实的 `register()` → 模拟系统投递事件 → `handle_native_event()` /
tap 回调的完整路径。这样能抓到「注册成功但回调不触发」这类只打桩会漏掉的缺陷。
"""

from __future__ import annotations

import ctypes
import sys
import types

import pytest

from linglong_clip.ui import hotkeys as hk
from linglong_clip.ui.hotkeys import (
    HotkeyManager,
    _MacHotkeys,
    _WindowsHotkeys,
    parse_sequence,
)


# ---------------------------------------------------------------- 序列解析


def test_parse_sequence_basic():
    modifiers, key = parse_sequence("Ctrl+Shift+V")
    assert modifiers == {"ctrl", "shift"}
    assert key == "v"


def test_parse_sequence_aliases():
    for text in ("Cmd+V", "Command+V", "Super+V", "Meta+V", "Win+V"):
        modifiers, key = parse_sequence(text)
        assert modifiers == {"win"}, text
        assert key == "v"


def test_parse_sequence_alt_option():
    assert parse_sequence("Option+X")[0] == {"alt"}
    assert parse_sequence("Alt+X")[0] == {"alt"}


def test_parse_sequence_control_alias():
    assert parse_sequence("Control+X")[0] == {"ctrl"}


def test_parse_sequence_whitespace_tolerant():
    modifiers, key = parse_sequence("  Ctrl + Shift + V ")
    assert modifiers == {"ctrl", "shift"}
    assert key == "v"


def test_parse_sequence_empty():
    assert parse_sequence("") == (set(), "")


def test_parse_sequence_key_only():
    assert parse_sequence("F5") == (set(), "f5")


def test_parse_sequence_unknown_modifier_ignored():
    modifiers, key = parse_sequence("Hyper+V")
    assert modifiers == set()
    assert key == "v"


# ---------------------------------------------------------------- Windows


class FakeUser32:
    """假 user32：记录调用，模拟注册结果与错误码。"""

    def __init__(self, register_result=True, error_code=0, available=True):
        self.available = available
        self.register_result = register_result
        self.error_code = error_code
        self.registered = []
        self.unregistered = []
        self.modal = None

    def RegisterHotKey(self, hwnd, hotkey_id, flags, virtual_key):  # noqa: N802
        if self.register_result:
            self.registered.append((hotkey_id, flags, virtual_key))
        return self.register_result

    def UnregisterHotKey(self, hwnd, hotkey_id):  # noqa: N802
        self.unregistered.append(hotkey_id)
        return True


@pytest.fixture()
def win_backend(monkeypatch):
    backend = _WindowsHotkeys.__new__(_WindowsHotkeys)
    backend._callbacks = {}
    backend.unavailable_reason = ""
    backend._user32 = FakeUser32()
    return backend


def test_windows_register_stores_callback(win_backend):
    fired = []
    assert win_backend.register(1, "Ctrl+Shift+V", lambda: fired.append("hit")) is True
    assert 1 in win_backend._callbacks
    assert win_backend._user32.registered


def test_windows_modifier_flags_mapping(win_backend):
    win_backend.register(7, "Ctrl+Alt+Shift+V", lambda: None)
    _id, flags, _vk = win_backend._user32.registered[0]
    assert flags & hk._WIN_MOD_CTRL
    assert flags & hk._WIN_MOD_ALT
    assert flags & hk._WIN_MOD_SHIFT
    assert flags & hk._WIN_MOD_NOREPEAT


def test_windows_win_key_uses_mod_win(win_backend):
    win_backend.register(7, "Win+V", lambda: None)
    _id, flags, _vk = win_backend._user32.registered[0]
    assert flags & 0x0008


def test_windows_virtual_key_letters_uppercased(win_backend):
    win_backend.register(7, "Ctrl+v", lambda: None)
    _id, _flags, vk = win_backend._user32.registered[0]
    assert vk == ord("V")


def test_windows_virtual_key_function_keys(win_backend):
    win_backend.register(7, "Ctrl+F24", lambda: None)
    _id, _flags, vk = win_backend._user32.registered[0]
    assert vk == hk._WIN_VK["f24"]


def test_windows_unknown_key_rejected(win_backend):
    assert win_backend.register(1, "Ctrl+NotAKey", lambda: None) is False
    assert "无法识别" in win_backend.unavailable_reason


def test_windows_conflict_reports_error_1409(monkeypatch):
    backend = _WindowsHotkeys.__new__(_WindowsHotkeys)
    backend._callbacks = {}
    backend.unavailable_reason = ""
    backend._user32 = FakeUser32(register_result=False, error_code=1409)

    monkeypatch.setattr(hk, "_last_error", lambda library: 1409)
    assert backend.register(1, "Ctrl+Shift+V", lambda: None) is False
    assert "占用" in backend.unavailable_reason


def test_windows_generic_error_reports_code(monkeypatch):
    backend = _WindowsHotkeys.__new__(_WindowsHotkeys)
    backend._callbacks = {}
    backend.unavailable_reason = ""
    backend._user32 = FakeUser32(register_result=False, error_code=5)

    monkeypatch.setattr(hk, "_last_error", lambda library: 5)
    assert backend.register(1, "Ctrl+Shift+V", lambda: None) is False
    assert "错误码 5" in backend.unavailable_reason


def test_last_error_survives_missing_get_last_error(monkeypatch):
    """Linux 上 `ctypes.get_last_error` 不存在，不能因此抛异常。"""
    monkeypatch.delattr(ctypes, "get_last_error", raising=False)
    assert hk._last_error(None) == 0


def test_last_error_falls_back_to_library(monkeypatch):
    monkeypatch.delattr(ctypes, "get_last_error", raising=False)

    class Lib:
        def GetLastError(self):  # noqa: N802
            return 87

    assert hk._last_error(Lib()) == 87


def test_windows_unregister_clears_callback(win_backend):
    win_backend.register(3, "Ctrl+Shift+V", lambda: None)
    win_backend.unregister_all([3])
    assert 3 not in win_backend._callbacks
    assert 3 in win_backend._user32.unregistered


def test_windows_wm_hotkey_dispatch(win_backend):
    """最容易漏的一环：注册成功后必须真的能收到 WM_HOTKEY。"""
    fired = []
    win_backend.register(5, "Ctrl+Shift+V", lambda: fired.append("hit"))

    message = types.SimpleNamespace(message=hk.WM_HOTKEY, wParam=5)
    handled = win_backend.handle_native_event(0, message)

    assert handled is True
    assert fired == ["hit"]


def test_windows_wm_hotkey_unknown_id_not_handled(win_backend):
    win_backend.register(5, "Ctrl+Shift+V", lambda: None)
    message = types.SimpleNamespace(message=hk.WM_HOTKEY, wParam=99)
    assert win_backend.handle_native_event(0, message) is False


def test_windows_other_message_not_handled(win_backend):
    message = types.SimpleNamespace(message=0x031D, wParam=5)  # WM_CLIPBOARDUPDATE
    assert win_backend.handle_native_event(0, message) is False


def test_windows_handler_survives_malformed_message(win_backend):
    assert win_backend.handle_native_event(0, object()) is False


def test_windows_callback_exception_does_not_propagate(win_backend):
    def boom():
        raise RuntimeError("callback blew up")

    win_backend.register(5, "Ctrl+Shift+V", boom)
    message = types.SimpleNamespace(message=hk.WM_HOTKEY, wParam=5)
    # 回调抛异常不能把消息循环拖死
    assert win_backend.handle_native_event(0, message) is True


def test_windows_unavailable_when_user32_missing(monkeypatch):
    backend = _WindowsHotkeys.__new__(_WindowsHotkeys)
    backend._callbacks = {}
    backend.unavailable_reason = ""
    backend._user32 = None
    assert backend.is_available() is False
    assert backend.register(1, "Ctrl+V", lambda: None) is False


# ---------------------------------------------------------------- macOS


class FakeQuartz:
    """假 Quartz：模拟事件 tap 的创建、启停与事件投递。"""

    kCGEventKeyDown = 10
    kCGEventTapDisabledByTimeout = 0xFFFFFFFE
    kCGEventTapDisabledByUserInput = 0xFFFFFFFF
    kCGSessionEventTap = 1
    kCGHeadInsertEventTap = 0
    kCGEventTapOptionDefault = 0
    kCGKeyboardEventKeycode = 9
    kAXTrustedCheckOptionPrompt = "AXTrustedCheckOptionPrompt"

    def __init__(self, trusted=True, tap_created=True, run_trusted=None):
        self.trusted = trusted
        self.tap_created = tap_created
        self.run_trusted = run_trusted
        self.tap_enabled = []
        self.prompt_requests = 0
        self.created_taps = 0

    def AXIsProcessTrusted(self):  # noqa: N802
        return self.trusted

    def AXIsProcessTrustedWithOptions(self, options):  # noqa: N802
        self.prompt_requests += 1
        return self.trusted if self.run_trusted is None else self.run_trusted

    def CGEventMaskBit(self, event_type):  # noqa: N802
        return 1 << event_type

    def CGEventTapCreate(self, *args):  # noqa: N802
        self.created_taps += 1
        return object() if self.tap_created else None

    def CGEventTapEnable(self, tap, enabled):  # noqa: N802
        self.tap_enabled.append(enabled)

    def CFMachPortCreateRunLoopSource(self, alloc, tap, order):  # noqa: N802
        return object()

    def CGEventGetIntegerValueField(self, event, field):  # noqa: N802
        return event.keycode

    def CGEventGetFlags(self, event):  # noqa: N802
        return event.flags


class FakeEvent:
    def __init__(self, keycode, flags):
        self.keycode = keycode
        self.flags = flags


@pytest.fixture()
def mac_backend(monkeypatch):
    backend = _MacHotkeys.__new__(_MacHotkeys)
    backend._quartz = FakeQuartz()
    backend._cf = None
    backend._tap = None
    backend._source = None
    backend._callbacks = {}
    backend._patterns = {}
    backend._refs = []
    backend.unavailable_reason = ""
    backend._runloop_add = lambda *a, **k: None
    backend._runloop_get = lambda: object()
    backend._runloop_mode = "common"
    return backend


def test_mac_register_succeeds_when_trusted(mac_backend):
    assert mac_backend.register(1, "Cmd+Shift+V", lambda: None) is True
    assert 1 in mac_backend._callbacks
    # `parse_sequence` 把 Cmd 归一到统一命名 `win`，与 Windows 的 Win 键同义
    assert mac_backend._patterns[1] == (["shift", "win"], hk._MAC_VK["v"])
    # 真注册要给 Quartz 打开 tap，否则事件永远不会进来
    assert True in mac_backend._quartz.tap_enabled


def test_mac_register_without_permission_returns_false(mac_backend):
    """没有辅助功能权限时必须明确失败，而不是「注册成功但永不触发」。"""
    mac_backend._quartz = FakeQuartz(trusted=False)
    assert mac_backend.register(1, "Cmd+Shift+V", lambda: None) is False
    assert "辅助功能" in mac_backend.unavailable_reason
    assert 1 not in mac_backend._callbacks


def test_mac_register_requests_permission_once(mac_backend):
    mac_backend._quartz = FakeQuartz(trusted=False)
    mac_backend.register(1, "Cmd+V", lambda: None)
    assert mac_backend._quartz.prompt_requests == 1


def test_mac_register_unknown_key(mac_backend):
    assert mac_backend.register(1, "Cmd+NotAKey", lambda: None) is False
    assert "无法识别" in mac_backend.unavailable_reason


def test_mac_tap_creation_failure_reported(mac_backend):
    mac_backend._quartz = FakeQuartz(tap_created=False)
    assert mac_backend.register(1, "Cmd+V", lambda: None) is False
    assert "事件监听" in mac_backend.unavailable_reason


def test_mac_event_tap_fires_callback(mac_backend):
    fired = []
    mac_backend.register(1, "Cmd+Shift+V", lambda: fired.append("hit"))

    event = FakeEvent(hk._MAC_VK["v"], hk._MAC_MOD_FLAGS["cmd"] | hk._MAC_MOD_FLAGS["shift"])
    result = mac_backend._handle_event(None, FakeQuartz.kCGEventKeyDown, event, None)

    assert fired == ["hit"]
    # 热键被吞掉，不再送给前台应用
    assert result is None


def test_mac_event_tap_ignores_wrong_key(mac_backend):
    fired = []
    mac_backend.register(1, "Cmd+Shift+V", lambda: fired.append("hit"))

    event = FakeEvent(hk._MAC_VK["x"], hk._MAC_MOD_FLAGS["cmd"] | hk._MAC_MOD_FLAGS["shift"])
    result = mac_backend._handle_event(None, FakeQuartz.kCGEventKeyDown, event, None)

    assert fired == []
    assert result is event


def test_mac_event_tap_ignores_extra_modifiers(mac_backend):
    """多按了一个修饰键就不该命中 —— 否则 Cmd+Shift+Alt+V 会误触发。"""
    fired = []
    mac_backend.register(1, "Cmd+Shift+V", lambda: fired.append("hit"))

    event = FakeEvent(
        hk._MAC_VK["v"],
        hk._MAC_MOD_FLAGS["cmd"] | hk._MAC_MOD_FLAGS["shift"] | hk._MAC_MOD_FLAGS["alt"],
    )
    mac_backend._handle_event(None, FakeQuartz.kCGEventKeyDown, event, None)
    assert fired == []


def test_mac_event_tap_ignores_missing_modifiers(mac_backend):
    fired = []
    mac_backend.register(1, "Cmd+Shift+V", lambda: fired.append("hit"))

    event = FakeEvent(hk._MAC_VK["v"], hk._MAC_MOD_FLAGS["cmd"])
    mac_backend._handle_event(None, FakeQuartz.kCGEventKeyDown, event, None)
    assert fired == []


def test_mac_capslock_does_not_break_hotkey(mac_backend):
    """CapsLock 开着不该让快捷键失效 —— 它不该参与比较。"""
    fired = []
    mac_backend.register(1, "Cmd+V", lambda: fired.append("hit"))

    event = FakeEvent(hk._MAC_VK["v"], hk._MAC_MOD_FLAGS["cmd"] | 0x00000001)
    mac_backend._handle_event(None, FakeQuartz.kCGEventKeyDown, event, None)
    assert fired == ["hit"]


def test_mac_numeric_pad_flag_ignored(mac_backend):
    fired = []
    mac_backend.register(1, "Cmd+V", lambda: fired.append("hit"))
    event = FakeEvent(hk._MAC_VK["v"], hk._MAC_MOD_FLAGS["cmd"] | 0x00000004)
    mac_backend._handle_event(None, FakeQuartz.kCGEventKeyDown, event, None)
    assert fired == ["hit"]


def test_mac_tap_reenabled_after_timeout(mac_backend):
    """系统超时会临时停掉 tap，代码必须自己重开，否则静默失效。"""
    mac_backend.register(1, "Cmd+V", lambda: None)
    mac_backend._quartz.tap_enabled.clear()

    event = FakeEvent(0, 0)
    mac_backend._handle_event(
        None, FakeQuartz.kCGEventTapDisabledByTimeout, event, None
    )
    assert True in mac_backend._quartz.tap_enabled


def test_mac_tap_reenabled_after_user_input_disable(mac_backend):
    mac_backend.register(1, "Cmd+V", lambda: None)
    mac_backend._quartz.tap_enabled.clear()

    mac_backend._handle_event(
        None, FakeQuartz.kCGEventTapDisabledByUserInput, FakeEvent(0, 0), None
    )
    assert True in mac_backend._quartz.tap_enabled


def test_mac_callback_exception_does_not_escape_tap(mac_backend):
    """tap 回调抛异常会让系统停掉整个 tap，必须兜住。"""

    def boom():
        raise RuntimeError("callback blew up")

    mac_backend.register(1, "Cmd+V", boom)
    event = FakeEvent(hk._MAC_VK["v"], hk._MAC_MOD_FLAGS["cmd"])
    # 不抛异常即通过
    mac_backend._handle_event(None, FakeQuartz.kCGEventKeyDown, event, None)


def test_mac_unregister_clears_state(mac_backend):
    mac_backend.register(1, "Cmd+V", lambda: None)
    mac_backend.unregister_all([1])
    assert mac_backend._callbacks == {}
    assert mac_backend._patterns == {}
    assert False in mac_backend._quartz.tap_enabled


def test_mac_quartz_missing_is_graceful(monkeypatch):
    """没装 pyobjc 时必须优雅降级，不是崩在导入上。"""
    backend = _MacHotkeys.__new__(_MacHotkeys)
    backend._quartz = None
    backend._cf = None
    backend._tap = None
    backend._source = None
    backend._callbacks = {}
    backend._patterns = {}
    backend._refs = []
    backend.unavailable_reason = "缺少 pyobjc"
    assert backend.is_available() is False
    assert backend.register(1, "Cmd+V", lambda: None) is False


def test_mac_handle_native_event_is_noop(mac_backend):
    # macOS 不走 Qt 的原生事件过滤器
    assert mac_backend.handle_native_event(0, None) is False


# ---------------------------------------------------------------- 调度器


@pytest.fixture()
def manager(monkeypatch):
    instance = HotkeyManager()

    class FakeBackend:
        name = "fake"

        def __init__(self):
            self.registered = {}
            self.available = True
            self.unavailable_reason = ""

        def is_available(self):
            return self.available

        def register(self, hotkey_id, sequence, callback):
            if not self.available:
                return False
            self.registered[hotkey_id] = (sequence, callback)
            return True

        def unregister_all(self, ids):
            for hotkey_id in ids:
                self.registered.pop(hotkey_id, None)

        # 供测试改状态用的钩子

        def handle_native_event(self, event_type, message):
            return False

    fake = FakeBackend()
    instance._backend = fake
    return instance


def test_manager_register_and_fire(manager):
    fired = []
    assert manager.register("Ctrl+Shift+V", lambda: fired.append("hit")) is True

    hotkey_id = next(iter(manager._bindings))
    manager._fire(hotkey_id)
    assert fired == ["hit"]


def test_manager_assigns_unique_ids(manager):
    manager.register("Ctrl+Shift+V", lambda: None)
    manager.register("Ctrl+Shift+C", lambda: None)
    assert len(manager._bindings) == 2


def test_manager_reports_unavailable(manager):
    manager._backend.available = False
    assert manager.is_available is False
    assert manager.register("Ctrl+V", lambda: None) is False
    assert manager.reason


def test_manager_no_backend_reason(monkeypatch):
    instance = HotkeyManager()
    instance._backend = None
    assert instance.backend_name == "none"
    assert instance.is_available is False
    assert instance.register("Ctrl+V", lambda: None) is False
    assert "不支持" in instance.reason


def test_manager_fire_unknown_id_is_noop(manager):
    manager._fire(9999)  # 不应抛异常


def test_manager_binding_exception_isolated(manager):
    def boom():
        raise RuntimeError("boom")

    manager.register("Ctrl+V", boom)
    hotkey_id = next(iter(manager._bindings))
    manager._fire(hotkey_id)  # 不应抛异常


def test_manager_unregister_all_clears(manager):
    manager.register("Ctrl+V", lambda: None)
    manager.unregister_all()
    assert manager._bindings == {}
    assert manager._backend.registered == {}


def test_manager_handle_native_event_without_backend():
    instance = HotkeyManager()
    instance._backend = None
    assert instance.handle_native_event(0, None) is False
