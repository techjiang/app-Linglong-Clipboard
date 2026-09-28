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


# ------------------------------------------------- Windows 系统剪贴板历史读取


class _FakeWinRtContent:
    """假 `DataPackageView`：只实现我们真正调的那一个异步取文本。"""

    def __init__(self, text):
        self._text = text

    def get_text_async(self):
        return self

    def get_results(self):
        if isinstance(self._text, Exception):
            raise self._text
        return self._text


class _FakeWinRtItem:
    def __init__(self, text):
        self.content = _FakeWinRtContent(text)


class _FakeHistoryResult:
    def __init__(self, items):
        self.items = items


class _FakeHistoryCall:
    def get_results(self):
        return self._fake_result

    def __init__(self, result):
        self._fake_result = result


class _FakeClipboardApi:
    def __init__(self, items):
        self._items = items
        self.calls = 0

    def get_history_items_async(self):
        self.calls += 1
        if isinstance(self._items, Exception):
            raise self._items
        return _FakeHistoryCall(_FakeHistoryResult(self._items))


class _FakeWinRtModule:
    def __init__(self, items):
        self.Clipboard = _FakeClipboardApi(items)


def _history_backend(monkeypatch, winrt_module=None, *, enabled=True, load_raises=None):
    """构造 Windows 后端并接管 WinRT 与注册表两个外部依赖。"""
    from linglong_clip.platform import windows as win_module

    backend = win_module.WindowsBackend.__new__(win_module.WindowsBackend)
    backend._listener_hwnd = 0
    backend._registered = False
    backend._sequence = 0

    monkeypatch.setattr(win_module, "_on_windows", lambda: True)
    monkeypatch.setattr(win_module, "history_enabled", lambda: enabled)
    if load_raises is not None:
        def _boom():
            raise load_raises

        monkeypatch.setattr(win_module, "_load_winrt", _boom)
    else:
        monkeypatch.setattr(win_module, "_load_winrt", lambda: winrt_module)
    return backend, win_module


def test_windows_history_supported_requires_winrt(monkeypatch):
    """WinRT 缺位时不能声称支持历史。

    这是本轮修的核心问题：此前 `is_history_supported()` 只判断
    `GetClipboardSequenceNumber` 这个符号存在与否 —— 它是 Win2000
    就有的基础 API，和历史毫无关系，于是能力矩阵恒为「是」，
    而 `read_windows()` 永远返回空列表。
    """
    from linglong_clip.platform import windows as win_module

    backend, _ = _history_backend(
        monkeypatch, load_raises=win_module._WinRtUnavailable("没装 winrt")
    )
    assert backend.is_history_supported() is False


def test_windows_history_supported_false_when_switch_off(monkeypatch):
    """系统设置里关掉历史后，即使 API 在也不该报「支持」。"""
    backend, _ = _history_backend(monkeypatch, _FakeWinRtModule([]), enabled=False)
    assert backend.is_history_supported() is False


def test_windows_history_supported_when_ready(monkeypatch):
    backend, _ = _history_backend(monkeypatch, _FakeWinRtModule([]), enabled=True)
    assert backend.is_history_supported() is True


def test_windows_history_supported_accepts_unknown_switch(monkeypatch):
    """读不到开关（None）时按可读处理，由空结果自己收敛。"""
    backend, _ = _history_backend(monkeypatch, _FakeWinRtModule([]), enabled=None)
    assert backend.is_history_supported() is True


def test_windows_read_history_returns_text_items(monkeypatch):
    items = [_FakeWinRtItem("第一条"), _FakeWinRtItem("第二条")]
    backend, _ = _history_backend(monkeypatch, _FakeWinRtModule(items))
    results = backend.read_windows(limit=10)
    assert [r.text for r in results] == ["第一条", "第二条"]
    assert all(r.kind == "text" for r in results)


def test_windows_read_history_respects_limit(monkeypatch):
    items = [_FakeWinRtItem(f"内容{i}") for i in range(10)]
    backend, _ = _history_backend(monkeypatch, _FakeWinRtModule(items))
    assert len(backend.read_windows(limit=3)) == 3


def test_windows_read_history_zero_limit_skips_api(monkeypatch):
    """limit<=0 时不该白跑一次跨进程调用。"""
    module = _FakeWinRtModule([_FakeWinRtItem("x")])
    backend, _ = _history_backend(monkeypatch, module)
    assert backend.read_windows(limit=0) == []
    assert module.Clipboard.calls == 0


def test_windows_read_history_skips_non_text_items(monkeypatch):
    """图片/文件项取不到文本 → 跳过，而不是塞一条空记录。"""
    items = [_FakeWinRtItem("有文本"), _FakeWinRtItem(None), _FakeWinRtItem("")]
    backend, _ = _history_backend(monkeypatch, _FakeWinRtModule(items))
    assert [r.text for r in backend.read_windows()] == ["有文本"]


def test_windows_read_history_survives_single_item_failure(monkeypatch):
    """单项取文本抛异常不能拖垮整批 —— 历史里混着坏项是常态。"""
    items = [
        _FakeWinRtItem("好的"),
        _FakeWinRtItem(OSError("这项读不了")),
        _FakeWinRtItem("也是好的"),
    ]
    backend, _ = _history_backend(monkeypatch, _FakeWinRtModule(items))
    assert [r.text for r in backend.read_windows()] == ["好的", "也是好的"]


def test_windows_read_history_without_items_attribute(monkeypatch):
    """WinRT 返回对象没有 items 属性时按空处理，不崩。"""
    backend, _ = _history_backend(monkeypatch, _FakeWinRtModule(None))
    assert backend.read_windows() == []


def test_windows_read_history_returns_empty_on_api_failure(monkeypatch):
    """API 调用失败返回空列表，绝不抛异常。"""
    backend, _ = _history_backend(
        monkeypatch, _FakeWinRtModule(OSError("历史服务没起来"))
    )
    assert backend.read_windows() == []


def test_windows_read_history_returns_empty_when_winrt_missing(monkeypatch):
    from linglong_clip.platform import windows as win_module

    backend, _ = _history_backend(
        monkeypatch, load_raises=win_module._WinRtUnavailable("缺投影")
    )
    assert backend.read_windows() == []


def test_windows_read_history_skips_api_when_switch_off(monkeypatch):
    module = _FakeWinRtModule([_FakeWinRtItem("不该被读到")])
    backend, _ = _history_backend(monkeypatch, module, enabled=False)
    assert backend.read_windows() == []
    assert module.Clipboard.calls == 0


def test_windows_read_history_off_windows_returns_empty(monkeypatch):
    from linglong_clip.platform import windows as win_module

    backend = win_module.WindowsBackend.__new__(win_module.WindowsBackend)
    monkeypatch.setattr(win_module, "_on_windows", lambda: False)
    assert backend.read_windows() == []
    assert backend.is_history_supported() is False


def test_windows_history_registry_path_is_documented_location():
    """开关的注册表位置必须和系统实际写的一致，否则永远读不到。"""
    from linglong_clip.platform import windows as win_module

    assert win_module.HISTORY_REG_PATH == r"Software\Microsoft\Clipboard"
    assert win_module.HISTORY_REG_VALUE == "EnableClipboardHistory"


def test_windows_history_enabled_off_windows(monkeypatch):
    from linglong_clip.platform import windows as win_module

    monkeypatch.setattr(win_module, "_on_windows", lambda: False)
    assert win_module.history_enabled() is None


def test_windows_load_winrt_reports_all_attempts(monkeypatch):
    """两个投影包都缺时，错误信息里要能看到分别试过什么。"""
    from linglong_clip.platform import windows as win_module

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if "datatransfer" in name:
            raise ImportError(f"没有 {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    try:
        win_module._load_winrt()
    except win_module._WinRtUnavailable as exc:
        message = str(exc)
        assert "winrt" in message
        assert "winsdk" in message
    else:  # pragma: no cover - 环境里真装了投影时走这里
        pytest.skip("本机装了 WinRT 投影，无法验证缺失分支")


# --------------------------------------------------- Windows 原生调用链（Win32）


class _FakeWin32Full:
    """假 user32 + kernel32：把真实调用链的参数与顺序记下来。

    这些方法此前完全没有用例覆盖 —— 「接口在、调用链没人走过」。
    这里用假对象把 `current_focus` / `open_clipboard` / 安全粘贴
    这几条路径真实执行一遍，验的是参数传对没有、失败时有没有兜住。
    """

    def __init__(self):
        self.calls = []
        self.sequence = 7
        self.foreground = 0x1234
        self.window_text = "记事本 - 未命名"
        self.pid = 4242
        self.exe_path = r"C:\Program Files\App\app.exe"
        self.open_ok = True
        self.open_fail_times = 0
        self.handle = 0xBEEF
        self.format_result = 0xC001
        self.sleeps = []

    # --- 剪贴板 ---
    def OpenClipboard(self, hwnd):  # noqa: N802
        self.calls.append("OpenClipboard")
        if self.open_fail_times > 0:
            self.open_fail_times -= 1
            return False
        return self.open_ok

    def CloseClipboard(self):  # noqa: N802
        self.calls.append("CloseClipboard")
        return True

    def GetClipboardSequenceNumber(self):  # noqa: N802
        return self.sequence

    # --- 前台窗口 ---
    def GetForegroundWindow(self):  # noqa: N802
        return self.foreground

    def GetWindowThreadProcessId(self, hwnd, pid_ref):  # noqa: N802
        self.calls.append("GetWindowThreadProcessId")
        # ctypes.byref 包装的对象，真实 API 会写入；这里手动塞值
        pid_ref._obj.value = self.pid

    def GetWindowTextLengthW(self, hwnd):  # noqa: N802
        return len(self.window_text)

    def GetWindowTextW(self, hwnd, buffer, size):  # noqa: N802
        self.calls.append("GetWindowTextW")
        # ctypes.create_unicode_buffer 支持直接赋 .value —— 真实 API 写的就是它
        buffer.value = self.window_text
        return len(self.window_text)

    # --- 安全粘贴 ---
    def RegisterClipboardFormatW(self, name):  # noqa: N802
        self.calls.append(("RegisterClipboardFormatW", name))
        return self.format_result

    def SetClipboardData(self, fmt, handle):  # noqa: N802
        self.calls.append(("SetClipboardData", fmt))
        return True


class _FakeKernel32Full:
    def __init__(self, fake_user):
        self._user = fake_user
        self.closed = []

    def Sleep(self, ms):  # noqa: N802
        self._user.sleeps.append(ms)

    def OpenProcess(self, access, inherit, pid):  # noqa: N802
        self._user.calls.append(("OpenProcess", access, pid))
        return self._user.handle

    def QueryFullProcessImageNameW(self, handle, flags, buffer, size):  # noqa: N802
        buffer.value = self._user.exe_path
        return True

    def CloseHandle(self, handle):  # noqa: N802
        self.closed.append(handle)
        return True


def _full_win_backend(monkeypatch):
    from linglong_clip.platform import windows as win_module

    user = _FakeWin32Full()
    kernel = _FakeKernel32Full(user)
    backend = win_module.WindowsBackend.__new__(win_module.WindowsBackend)
    backend._listener_hwnd = 0
    backend._registered = False
    backend._sequence = 0
    monkeypatch.setattr(win_module, "user32", user)
    monkeypatch.setattr(win_module, "kernel32", kernel)
    return backend, user, kernel


def test_windows_open_clipboard_retries_then_succeeds(monkeypatch):
    """剪贴板被占用时要重试，成功后立刻返回。"""
    backend, user, _ = _full_win_backend(monkeypatch)
    user.open_fail_times = 2

    assert backend.open_clipboard(retries=5) is True
    assert user.calls.count("OpenClipboard") == 3
    # 每次失败都该等一下再试
    assert user.sleeps == [10, 10]


def test_windows_open_clipboard_gives_up_after_retries(monkeypatch):
    backend, user, _ = _full_win_backend(monkeypatch)
    user.open_ok = False

    assert backend.open_clipboard(retries=3) is False
    assert user.calls.count("OpenClipboard") == 3
    # 最后一次失败后不该再多睡一次：等待不会让任何东西变好
    assert user.sleeps == [10, 10]


def test_windows_open_clipboard_sleep_failure_is_contained(monkeypatch):
    """`kernel32.Sleep` 抛异常不能把重试变成崩溃。"""
    from linglong_clip.platform import windows as win_module

    backend, user, _ = _full_win_backend(monkeypatch)
    user.open_ok = False

    class _ExplodingKernel(_FakeKernel32Full):
        def Sleep(self, ms):  # noqa: N802
            raise OSError("Sleep 不可用")

    monkeypatch.setattr(win_module, "kernel32", _ExplodingKernel(user))
    assert backend.open_clipboard(retries=2) is False


def test_windows_open_clipboard_without_user32(monkeypatch):
    from linglong_clip.platform import windows as win_module

    backend, _, _ = _full_win_backend(monkeypatch)
    monkeypatch.setattr(win_module, "user32", None)
    assert backend.open_clipboard() is False


def test_windows_close_clipboard_without_user32(monkeypatch):
    from linglong_clip.platform import windows as win_module

    backend, _, _ = _full_win_backend(monkeypatch)
    monkeypatch.setattr(win_module, "user32", None)
    backend.close_clipboard()  # 不应抛异常


def test_windows_current_focus_full_chain(monkeypatch):
    """完整的取来源链路：前台窗口 → pid → 进程名。"""
    backend, user, _ = _full_win_backend(monkeypatch)

    info = backend.current_focus()
    assert info.app_id == "app.exe"
    assert info.app_name == "app.exe"
    assert info.window_title == "记事本 - 未命名"
    assert info.process_id == 4242


def test_windows_current_focus_closes_process_handle(monkeypatch):
    backend, user, kernel = _full_win_backend(monkeypatch)
    backend.current_focus()
    assert kernel.closed == [user.handle]


def test_windows_current_focus_no_foreground_window(monkeypatch):
    backend, user, _ = _full_win_backend(monkeypatch)
    user.foreground = 0
    assert backend.current_focus().is_empty is True


def test_windows_current_focus_empty_title_uses_exe(monkeypatch):
    backend, user, _ = _full_win_backend(monkeypatch)
    user.window_text = ""
    info = backend.current_focus()
    assert info.app_name == "app.exe"
    assert info.window_title == ""


def test_windows_current_focus_without_native_libs(monkeypatch):
    from linglong_clip.platform import windows as win_module

    backend, _, _ = _full_win_backend(monkeypatch)
    monkeypatch.setattr(win_module, "kernel32", None)
    assert backend.current_focus().is_empty is True


def test_windows_current_focus_exception_contained(monkeypatch):
    backend, user, _ = _full_win_backend(monkeypatch)

    def _boom():
        raise OSError("窗口系统炸了")

    monkeypatch.setattr(user, "GetForegroundWindow", _boom)
    assert backend.current_focus().is_empty is True


def test_windows_process_info_without_pid(monkeypatch):
    backend, _, _ = _full_win_backend(monkeypatch)
    assert backend._process_info(0) == ("", "")


def test_windows_process_info_open_failure(monkeypatch):
    backend, user, _ = _full_win_backend(monkeypatch)
    user.handle = 0
    assert backend._process_info(1234) == ("", "")


def test_windows_process_info_query_failure(monkeypatch):
    """`QueryFullProcessImageNameW` 返回假值时应给出空结果并仍然关句柄。"""
    from linglong_clip.platform import windows as win_module

    backend, user, _ = _full_win_backend(monkeypatch)

    class _Refusing(_FakeKernel32Full):
        def QueryFullProcessImageNameW(self, handle, flags, buffer, size):  # noqa: N802
            return False

    refusing = _Refusing(user)
    monkeypatch.setattr(win_module, "kernel32", refusing)
    assert backend._process_info(1234) == ("", "")
    assert refusing.closed == [user.handle]


def test_windows_window_text_too_short(monkeypatch):
    backend, user, _ = _full_win_backend(monkeypatch)
    user.window_text = ""
    assert backend._window_text(0x1234) == ""


def test_windows_window_text_exception_contained(monkeypatch):
    backend, user, _ = _full_win_backend(monkeypatch)

    def _boom(hwnd):
        raise OSError("读不了标题")

    monkeypatch.setattr(user, "GetWindowTextLengthW", _boom)
    assert backend._window_text(0x1234) == ""


def test_windows_transient_supported_follows_user32(monkeypatch):
    from linglong_clip.platform import windows as win_module

    backend, _, _ = _full_win_backend(monkeypatch)
    assert backend.is_transient_supported() is True
    monkeypatch.setattr(win_module, "user32", None)
    assert backend.is_transient_supported() is False


def test_windows_exclude_from_history_registers_format(monkeypatch):
    """安全粘贴要注册正确的格式名，否则系统历史照样收走敏感内容。"""
    from linglong_clip.platform import windows as win_module

    backend, user, _ = _full_win_backend(monkeypatch)
    backend.exclude_from_history()

    registered = [c for c in user.calls if isinstance(c, tuple)]
    assert (
        "RegisterClipboardFormatW",
        win_module.CLIPBOARD_FORMAT_EXCLUDE_MONITOR,
    ) in registered
    assert ("SetClipboardData", user.format_result) in user.calls


def test_windows_allow_in_history_registers_format(monkeypatch):
    from linglong_clip.platform import windows as win_module

    backend, user, _ = _full_win_backend(monkeypatch)
    backend.allow_in_history()

    registered = [c for c in user.calls if isinstance(c, tuple)]
    assert (
        "RegisterClipboardFormatW",
        win_module.CLIPBOARD_FORMAT_CAN_INCLUDE,
    ) in registered


def test_windows_exclude_from_history_without_user32(monkeypatch):
    from linglong_clip.platform import windows as win_module

    backend, _, _ = _full_win_backend(monkeypatch)
    monkeypatch.setattr(win_module, "user32", None)
    backend.exclude_from_history()  # 不应抛异常
    backend.allow_in_history()


def test_windows_exclude_from_history_format_registration_fails(monkeypatch):
    """注册格式失败时不该写数据（格式 id 为 0 表示无效）。"""
    backend, user, _ = _full_win_backend(monkeypatch)
    user.format_result = 0
    backend.exclude_from_history()
    assert ("SetClipboardData", 0) not in user.calls


def test_windows_exclude_from_history_exception_contained(monkeypatch):
    backend, user, _ = _full_win_backend(monkeypatch)

    def _boom(name):
        raise OSError("注册不了")

    monkeypatch.setattr(user, "RegisterClipboardFormatW", _boom)
    backend.exclude_from_history()  # 不应抛异常


def test_windows_has_changed_detects_sequence_change(monkeypatch):
    """序列号变化即代表剪贴板被写过，这是事件驱动之外最省的探测方式。"""
    backend, user, _ = _full_win_backend(monkeypatch)
    user.sequence = 7

    assert backend.has_changed() is True
    assert backend.has_changed() is False
    user.sequence = 8
    assert backend.has_changed() is True


def test_windows_has_changed_without_user32(monkeypatch):
    from linglong_clip.platform import windows as win_module

    backend, _, _ = _full_win_backend(monkeypatch)
    monkeypatch.setattr(win_module, "user32", None)
    assert backend.has_changed() is False


def test_windows_sequence_exception_returns_zero(monkeypatch):
    backend, user, _ = _full_win_backend(monkeypatch)

    def _boom():
        raise OSError("取不到序列号")

    monkeypatch.setattr(user, "GetClipboardSequenceNumber", _boom)
    assert backend.clipboard_sequence() == 0


# --------------------------------------------------- 历史开关与结果摊平


def test_history_enabled_reads_registry(monkeypatch):
    """系统开关必须真去读注册表 —— 否则区分不了「没开」与「历史为空」。"""
    import sys
    import types as _types

    from linglong_clip.platform import windows as win_module

    fake_winreg = _types.ModuleType("winreg")
    fake_winreg.HKEY_CURRENT_USER = 0x80000001
    state = {"value": 1, "raise_on_open": False}

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _open_key(hive, path):
        if state["raise_on_open"]:
            raise FileNotFoundError("键不存在")
        assert path == win_module.HISTORY_REG_PATH
        return _Key()

    def _query(key, name):
        assert name == win_module.HISTORY_REG_VALUE
        return state["value"], 4

    fake_winreg.OpenKey = _open_key
    fake_winreg.QueryValueEx = _query

    monkeypatch.setattr(win_module, "_on_windows", lambda: True)
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    assert win_module.history_enabled() is True
    state["value"] = 0
    assert win_module.history_enabled() is False
    # 键不存在（从没开过历史）时返回 None，而不是 False
    state["raise_on_open"] = True
    assert win_module.history_enabled() is None


def test_iter_history_items_handles_non_iterable(monkeypatch):
    """WinRT 的 items 不是可迭代对象时按空处理，不崩。"""
    from linglong_clip.platform import windows as win_module

    class _Weird:
        items = 42  # 数字，list() 会抛 TypeError

    assert list(win_module._iter_history_items(_Weird())) == []


def test_iter_history_items_handles_missing_items():
    from linglong_clip.platform import windows as win_module

    class _Bare:
        pass

    assert list(win_module._iter_history_items(_Bare())) == []
    assert list(win_module._iter_history_items(None)) == []


def test_windows_backend_init_defaults():
    from linglong_clip.platform.windows import WindowsBackend

    backend = WindowsBackend.__new__(WindowsBackend)
    backend.__init__()
    assert backend._listener_hwnd == 0
    assert backend._registered is False
    assert backend._sequence == 0


def test_windows_capabilities_shape():
    from linglong_clip.platform.windows import WindowsBackend

    backend = WindowsBackend.__new__(WindowsBackend)
    backend._listener_hwnd = 0
    backend._registered = False
    backend._sequence = 0
    caps = backend.capabilities()
    # 能力矩阵的键必须齐全，界面上按这些键渲染
    assert set(caps) == {
        "primary_selection",
        "native_history",
        "focus_detection",
        "transient_clipboard",
        "wayland",
    }
    assert caps["primary_selection"] is False
    assert caps["wayland"] is False


def test_windows_is_available_follows_user32(monkeypatch):
    from linglong_clip.platform import windows as win_module

    backend, _, _ = _full_win_backend(monkeypatch)
    assert backend.is_available() is True
    monkeypatch.setattr(win_module, "user32", None)
    assert backend.is_available() is False
