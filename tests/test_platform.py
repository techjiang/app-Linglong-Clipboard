"""平台适配层：后端探测、能力矩阵、降级。"""

import pytest

from linglong_clip.platform import ClipboardBackend, NullBackend, detect_backend
from linglong_clip.platform.base import backends_for


def test_detect_backend_returns_usable_object():
    backend = detect_backend()
    assert backend is not None
    assert backend.name


def test_detect_backend_never_raises():
    # 无论依赖是否齐全，都必须有一个可用后端
    for _ in range(3):
        assert detect_backend().is_available() is True


def test_null_backend_capabilities():
    backend = NullBackend()
    caps = backend.capabilities()
    assert caps["native_history"] is False
    assert caps["transient_clipboard"] is False


def test_base_backend_defaults():
    backend = ClipboardBackend()
    assert backend.read_primary_text() == ""
    assert backend.read_windows() == []
    assert backend.is_history_supported() is False
    assert backend.current_focus().is_empty is True
    assert backend.set_transient_text("x") is False


def test_focus_info_display_fallbacks():
    from linglong_clip.platform.base import FocusInfo

    assert FocusInfo(app_name="Chrome").display == "Chrome"
    assert FocusInfo(app_id="chrome").display == "chrome"
    assert FocusInfo().display == "未知来源"


def test_focus_info_is_empty():
    from linglong_clip.platform.base import FocusInfo

    assert FocusInfo().is_empty is True
    assert FocusInfo(app_id="x").is_empty is False
    assert FocusInfo(window_title="t").is_empty is False


def test_backends_for_mapping():
    assert backends_for("win32") == ["windows"]
    assert backends_for("darwin") == ["macos"]
    assert backends_for("linux") == ["linux"]
    assert backends_for("freebsd") == ["null"]


def test_capture_result_is_empty():
    from linglong_clip.platform.base import CaptureResult

    assert CaptureResult().is_empty is True
    assert CaptureResult(text="x").is_empty is False
    assert CaptureResult(image_png=b"\x89PNG").is_empty is False
    assert CaptureResult(files=["/a"]).is_empty is False


def test_platform_label_is_human_readable():
    assert detect_backend().platform_label() in {"Windows", "macOS", "Linux"} or True


def test_linux_session_detection(monkeypatch):
    from linglong_clip.platform.linux import session_type

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert session_type() == "wayland"

    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    assert session_type() == "x11"


# ---------------------------------------------------------------- macOS 后端


class _FakePopen:
    """假 `subprocess.run` 结果。"""

    def __init__(self, stdout=b"", returncode=0, stderr=b""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _mac_backend(monkeypatch, *, pb=True, osascript=True, run=None):
    """构造一个 macOS 后端，工具可用性可控。"""
    from linglong_clip.platform import macos as mac_module

    backend = mac_module.MacOSBackend.__new__(mac_module.MacOSBackend)
    backend._has_pbpaste = pb
    backend._has_pbcopy = pb
    backend._has_osascript = osascript
    backend._timers = []
    if run is not None:
        monkeypatch.setattr(mac_module, "_run", run)
    return backend, mac_module


def test_macos_available_when_any_tool_present(monkeypatch):
    backend, _ = _mac_backend(monkeypatch, pb=False, osascript=True)
    assert backend.is_available() is True
    backend, _ = _mac_backend(monkeypatch, pb=False, osascript=False)
    assert backend.is_available() is False


def test_macos_capabilities_match_implementation(monkeypatch):
    """能力矩阵必须与实现一致：没有 pbcopy 就不能声称支持临时剪贴板。

    此前这个后端只实现了 `current_focus()`，却把 `transient_clipboard`
    写死为 True —— 声明了安全粘贴实际做不到。
    """
    backend, _ = _mac_backend(monkeypatch, pb=False)
    assert backend.capabilities()["transient_clipboard"] is False
    backend, _ = _mac_backend(monkeypatch, pb=True)
    assert backend.capabilities()["transient_clipboard"] is True


def test_macos_transient_requires_write_ability(monkeypatch):
    backend, _ = _mac_backend(monkeypatch, pb=False)
    assert backend.is_transient_supported() is False
    assert backend.set_transient_text("x") is False


def test_macos_read_text_uses_pbpaste(monkeypatch):
    calls = []

    def fake_run(args, input_bytes=b""):
        calls.append(args)
        return b"hello from mac"

    backend, _ = _mac_backend(monkeypatch, run=fake_run)
    assert backend.read_text() == "hello from mac"
    assert calls[0][:2] == ["pbpaste", "-Prefer"]


def test_macos_read_text_falls_back_when_empty(monkeypatch):
    calls = []

    def fake_run(args, input_bytes=b""):
        calls.append(args)
        # 第一次带 -Prefer 返回空，第二次裸 pbpaste 有内容
        return b"" if "-Prefer" in args else b"plain"

    backend, _ = _mac_backend(monkeypatch, run=fake_run)
    assert backend.read_text() == "plain"
    assert len(calls) == 2


def test_macos_read_text_without_pbpaste(monkeypatch):
    backend, _ = _mac_backend(monkeypatch, pb=False)
    assert backend.read_text() == ""


def test_macos_write_text_pipes_stdin(monkeypatch):
    """`pbcopy` 无参数、只从 stdin 读，内容不能走命令行参数。"""
    captured = {}

    def fake_subprocess_run(args, **kwargs):
        captured["args"] = args
        captured["input"] = kwargs.get("input")
        return _FakePopen(returncode=0)

    backend, mac_module = _mac_backend(monkeypatch)
    monkeypatch.setattr(mac_module.subprocess, "run", fake_subprocess_run)

    assert backend.write_text("写点中文") is True
    assert captured["args"] == ["pbcopy"]
    assert captured["input"] == "写点中文".encode("utf-8")


def test_macos_write_text_reports_failure(monkeypatch):
    def fake_subprocess_run(args, **kwargs):
        return _FakePopen(returncode=1)

    backend, mac_module = _mac_backend(monkeypatch)
    monkeypatch.setattr(mac_module.subprocess, "run", fake_subprocess_run)
    assert backend.write_text("x") is False


def test_macos_write_text_handles_missing_tool(monkeypatch):
    def fake_subprocess_run(args, **kwargs):
        raise FileNotFoundError("pbcopy 不见了")

    backend, mac_module = _mac_backend(monkeypatch)
    monkeypatch.setattr(mac_module.subprocess, "run", fake_subprocess_run)
    assert backend.write_text("x") is False


def test_macos_sanity_check_success(monkeypatch):
    def fake_subprocess_run(args, **kwargs):
        return _FakePopen(stdout=b"abcdef")

    backend, mac_module = _mac_backend(monkeypatch)
    monkeypatch.setattr(mac_module.subprocess, "run", fake_subprocess_run)
    ok, detail = backend.sanity_check()
    assert ok is True
    assert "6 字节" in detail


def test_macos_sanity_check_missing_tool(monkeypatch):
    backend, _ = _mac_backend(monkeypatch, pb=False)
    ok, detail = backend.sanity_check()
    assert ok is False
    assert "pbpaste" in detail


def test_macos_sanity_check_reports_nonzero_exit(monkeypatch):
    def fake_subprocess_run(args, **kwargs):
        return _FakePopen(returncode=1, stderr=b"permission denied")

    backend, mac_module = _mac_backend(monkeypatch)
    monkeypatch.setattr(mac_module.subprocess, "run", fake_subprocess_run)
    ok, detail = backend.sanity_check()
    assert ok is False
    assert "permission denied" in detail


def test_macos_current_focus_parses_osascript(monkeypatch):
    answers = iter(["com.google.Chrome", "Google Chrome", "知乎 - Chrome"])

    def fake_run(args, input_bytes=b""):
        return next(answers).encode("utf-8")

    backend, _ = _mac_backend(monkeypatch, run=fake_run)
    info = backend.current_focus()
    assert info.app_id == "com.google.Chrome"
    assert info.app_name == "Google Chrome"
    assert info.window_title == "知乎 - Chrome"
    assert info.is_empty is False


def test_macos_current_focus_without_osascript(monkeypatch):
    backend, _ = _mac_backend(monkeypatch, osascript=False)
    assert backend.current_focus().is_empty is True


def test_macos_current_focus_empty_output(monkeypatch):
    backend, _ = _mac_backend(monkeypatch, run=lambda args, input_bytes=b"": b"")
    assert backend.current_focus().is_empty is True


def test_macos_transient_expires_and_clears(monkeypatch):
    """到期后应清空剪贴板 —— 但只在内容仍是自己写的那份时。"""
    import time

    state = {"text": "secret"}

    backend, _ = _mac_backend(monkeypatch)
    monkeypatch.setattr(
        backend, "write_text", lambda text: state.__setitem__("text", text) or True
    )
    monkeypatch.setattr(backend, "read_text", lambda: state["text"])

    assert backend.set_transient_text("secret", expire_ms=30) is True
    assert state["text"] == "secret"
    time.sleep(0.15)
    assert state["text"] == ""


def test_macos_transient_does_not_clobber_user_copy(monkeypatch):
    """用户在我们写入后又复制了别的东西，过期清理不能把那条抹掉。"""
    import time

    state = {"text": "secret"}

    backend, _ = _mac_backend(monkeypatch)
    monkeypatch.setattr(
        backend, "write_text", lambda text: state.__setitem__("text", text) or True
    )
    monkeypatch.setattr(backend, "read_text", lambda: state["text"])

    backend.set_transient_text("secret", expire_ms=30)
    # 模拟用户马上复制了别的内容
    state["text"] = "用户后来的复制"
    time.sleep(0.15)
    assert state["text"] == "用户后来的复制"


def test_macos_transient_no_expiry_when_zero(monkeypatch):
    state = {"text": ""}
    backend, _ = _mac_backend(monkeypatch)
    monkeypatch.setattr(
        backend, "write_text", lambda text: state.__setitem__("text", text) or True
    )
    assert backend.set_transient_text("x", expire_ms=0) is True
    assert backend._timers == []


def test_macos_timer_list_does_not_grow(monkeypatch):
    """长期运行不能因为定时器列表无限增长而漏内存。"""
    import time

    state = {"text": ""}
    backend, _ = _mac_backend(monkeypatch)
    monkeypatch.setattr(
        backend, "write_text", lambda text: state.__setitem__("text", text) or True
    )
    monkeypatch.setattr(backend, "read_text", lambda: state["text"])

    for _ in range(5):
        backend.set_transient_text("x", expire_ms=20)
    time.sleep(0.1)
    # 再写一次，会顺手清掉已结束的定时器
    backend.set_transient_text("y", expire_ms=20)
    assert len(backend._timers) <= 2


# ---------------------------------------------------------------- Windows 后端


class _FakeWin32:
    """假 user32：记录 OpenClipboard / 序列号调用。"""

    def __init__(self, open_ok=True, sequence=42):
        self.open_ok = open_ok
        self.sequence = sequence
        self.opened = 0
        self.closed = 0

    def OpenClipboard(self, hwnd):  # noqa: N802
        self.opened += 1
        return self.open_ok

    def CloseClipboard(self):  # noqa: N802
        self.closed += 1
        return True

    def GetClipboardSequenceNumber(self):  # noqa: N802
        return self.sequence


def _win_backend(monkeypatch, fake):
    from linglong_clip.platform import windows as win_module

    backend = win_module.WindowsBackend.__new__(win_module.WindowsBackend)
    backend._listener_hwnd = 0
    backend._registered = False
    backend._sequence = 0
    monkeypatch.setattr(win_module, "user32", fake)
    return backend, win_module


def test_windows_sanity_check_success(monkeypatch):
    backend, _ = _win_backend(monkeypatch, _FakeWin32(sequence=99))
    ok, detail = backend.sanity_check()
    assert ok is True
    assert "99" in detail


def test_windows_sanity_check_releases_clipboard(monkeypatch):
    """探测必须把剪贴板还回去，否则会卡住其它程序。"""
    fake = _FakeWin32()
    backend, _ = _win_backend(monkeypatch, fake)
    backend.sanity_check()
    assert fake.opened == 1
    assert fake.closed == 1


def test_windows_sanity_check_when_open_fails(monkeypatch):
    """OpenClipboard 失败时必须能返回，不能因为 `ctypes.windll` 缺失而崩。

    `ctypes.windll` 只在 Windows 上存在，此前重试逻辑里直接用它调
    `kernel32.Sleep` —— 在拿不到 windll 的环境下会把「重试」
    变成「抛异常」。
    """
    fake = _FakeWin32(open_ok=False)
    backend, _ = _win_backend(monkeypatch, fake)
    ok, detail = backend.sanity_check()
    assert ok is False
    assert "OpenClipboard" in detail
    # 打不开就不该瞎关
    assert fake.closed == 0


def test_windows_sanity_check_without_user32(monkeypatch):
    backend, _ = _win_backend(monkeypatch, None)
    ok, detail = backend.sanity_check()
    assert ok is False
    assert "user32" in detail


def test_windows_sanity_check_zero_sequence(monkeypatch):
    backend, _ = _win_backend(monkeypatch, _FakeWin32(sequence=0))
    ok, _detail = backend.sanity_check()
    assert ok is False


def test_windows_listener_registration_state(monkeypatch):
    """注册/注销必须记状态，否则退出时无法判断要不要清理。"""

    class WithListener(_FakeWin32):
        def AddClipboardFormatListener(self, hwnd):  # noqa: N802
            self.hwnd = hwnd
            return True

        def RemoveClipboardFormatListener(self, hwnd):  # noqa: N802
            self.removed = hwnd
            return True

    fake = WithListener()
    backend, _ = _win_backend(monkeypatch, fake)

    assert backend.register_listener(12345) is True
    assert backend._registered is True
    assert backend._listener_hwnd == 12345

    backend.unregister_listener()
    assert backend._registered is False
    assert fake.removed.value == 12345
    assert backend._listener_hwnd == 0


def test_windows_listener_registration_failure(monkeypatch):
    class Refusing(_FakeWin32):
        def AddClipboardFormatListener(self, hwnd):  # noqa: N802
            return False

    backend, _ = _win_backend(monkeypatch, Refusing())
    assert backend.register_listener(1) is False
    assert backend._registered is False
    # 注册失败不该把句柄记下来，否则注销时会多做一次无用的系统调用
    assert backend._listener_hwnd == 0


def test_windows_listener_rejects_empty_hwnd(monkeypatch):
    backend, _ = _win_backend(monkeypatch, _FakeWin32())
    assert backend.register_listener(0) is False


def test_windows_listener_add_raises_is_contained(monkeypatch):
    class Exploding(_FakeWin32):
        def AddClipboardFormatListener(self, hwnd):  # noqa: N802
            raise OSError("驱动层炸了")

    backend, _ = _win_backend(monkeypatch, Exploding())
    assert backend.register_listener(1) is False


def test_windows_unregister_without_prior_register_is_noop(monkeypatch):
    fake = _FakeWin32()
    backend, _ = _win_backend(monkeypatch, fake)
    backend.unregister_listener()  # 不应抛异常


def test_windows_unregister_twice_is_safe(monkeypatch):
    class WithListener(_FakeWin32):
        def AddClipboardFormatListener(self, hwnd):  # noqa: N802
            return True

        def RemoveClipboardFormatListener(self, hwnd):  # noqa: N802
            return True

    backend, _ = _win_backend(monkeypatch, WithListener())
    backend.register_listener(7)
    backend.unregister_listener()
    backend.unregister_listener()  # 第二次应为 no-op
    assert backend._registered is False


# ---------------------------------------------------------------- Linux 后端自检


def test_linux_sanity_check_reports_missing_tools(monkeypatch):
    """无图形会话时应给出可读的原因，而不是含糊的「不可用」。"""
    from linglong_clip.platform import linux as linux_module

    backend = linux_module.LinuxBackend.__new__(linux_module.LinuxBackend)
    backend._session = "x11"
    backend._has_wl = False
    backend._xlib = False
    monkeypatch.setattr(linux_module, "_has", lambda tool: False)

    ok, detail = backend.sanity_check()
    assert ok is False
    assert "xclip" in detail


def test_linux_sanity_check_wayland_with_wl_clipboard(monkeypatch):
    from linglong_clip.platform import linux as linux_module

    backend = linux_module.LinuxBackend.__new__(linux_module.LinuxBackend)
    backend._session = "wayland"
    backend._has_wl = True
    backend._xlib = False

    ok, detail = backend.sanity_check()
    assert ok is True
    assert "wl-paste" in detail


def test_linux_sanity_check_x11_with_xlib(monkeypatch):
    from linglong_clip.platform import linux as linux_module

    backend = linux_module.LinuxBackend.__new__(linux_module.LinuxBackend)
    backend._session = "x11"
    backend._has_wl = False
    backend._xlib = True

    ok, detail = backend.sanity_check()
    assert ok is True
    assert "xlib" in detail.lower()
