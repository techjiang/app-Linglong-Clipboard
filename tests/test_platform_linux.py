"""Linux 后端：X11 / Wayland 两条读写路径。

存在的理由：`platform/linux.py` 此前只有 35% 覆盖率，因为 CI 容器里
装不上 Xlib / wl-clipboard，剩下的分支从来没人跑过 —— 包括
`_read_x11_selection` 与 `_focus_via_xlib` 这两条真正干活的路径。
接口在、逻辑没验过，和上一轮 Windows 历史读取是同一类问题。

这里用假 `Xlib` 模块与假 `_run` 把两条路径都走通，
验的是「拿到数据后怎么解析」，而不是「环境里有没有装」。
"""

from __future__ import annotations

import sys
import types

import pytest

from linglong_clip.platform import linux as linux_module
from linglong_clip.platform.linux import LinuxBackend


# --------------------------------------------------------------- 假 Xlib


class _FakeAtom(str):
    """假 atom：Xlib 里 atom 是整数，这里字符串化便于断言。"""


class _FakeValue:
    def __init__(self, value):
        self.value = value


class _FakeProperty:
    def __init__(self, value):
        self.value = value


class _FakeScreen:
    def __init__(self, root):
        self.root = root


class _FakeRoot:
    """假 root window：`_NET_ACTIVE_WINDOW` 从这里取。"""

    def __init__(self, active=None):
        self._active = active

    def get_full_property(self, atom, type_):
        if self._active is None:
            return None
        return _FakeProperty([self._active])


class _FakeWindow:
    """假 window：按属性名返回预设值。"""

    def __init__(self, props):
        self._props = props

    def get_full_property(self, atom, type_):
        if atom in self._props:
            return _FakeValue(self._props[atom])
        return None


class _FakeSelectionResult:
    def __init__(self, value):
        self.value = value
        self.deleted = False

    def delete(self):
        self.deleted = True


class _FakeOwner:
    def __init__(self, value):
        self._value = value

    def convert_selection(self, atom, target, a, b):
        if isinstance(self._value, Exception):
            raise self._value
        if self._value is None:
            return None
        return _FakeSelectionResult(self._value)


class _FakeDisplay:
    """假 X 连接：只实现后端真正调用的那几个方法。"""

    def __init__(self, *, root=None, owner=None, atoms=None, windows=None):
        self._root = root
        self._owner = owner
        self._atoms = atoms or {}
        self._windows = windows or {}
        self._resources = {}

    def get_atom(self, name):
        return self._atoms.get(name, _FakeAtom(name))

    def get_selection_owner(self, atom):
        if atom == "NONE":
            return None
        return self._owner

    def screen(self):
        return _FakeScreen(self._root)

    def create_resource_object(self, kind, ident):
        return self._windows.get(ident, _FakeWindow({}))


def _install_fake_xlib(monkeypatch, display):
    """把假 Xlib 塞进 sys.modules，让函数内的 `import Xlib` 拿到它。"""
    xlib_pkg = types.ModuleType("Xlib")
    x_mod = types.ModuleType("Xlib.X")

    class _X:
        NONE = 0
        CurrentTime = 0
        AnyPropertyType = 0

    # 真实用法有 `from Xlib import X` 与 `import Xlib.X` 两种
    x_mod.NONE = 0
    x_mod.CurrentTime = 0
    x_mod.AnyPropertyType = 0
    xlib_pkg.X = x_mod
    xlib_pkg.display = types.ModuleType("Xlib.display")
    xlib_pkg.display.Display = lambda *a, **k: display

    monkeypatch.setitem(sys.modules, "Xlib", xlib_pkg)
    monkeypatch.setitem(sys.modules, "Xlib.X", x_mod)
    monkeypatch.setitem(sys.modules, "Xlib.display", xlib_pkg.display)


def _backend(monkeypatch, *, session="x11", xlib=True, has_wl=False, display=None):
    """构造后端并替换掉所有外部依赖（不真连 X、不真跑命令）。"""
    backend = LinuxBackend.__new__(LinuxBackend)
    backend._session = session
    backend._has_wl = has_wl
    backend._xlib = xlib
    backend._x_display = display
    backend._self_pids = {0}
    return backend


# --------------------------------------------------------------- 会话探测


def test_session_type_prefers_wayland_env(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.setenv("DISPLAY", ":0")
    assert linux_module.session_type() == "wayland"


def test_session_type_xdg_wayland(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert linux_module.session_type() == "wayland"


def test_session_type_x11(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    assert linux_module.session_type() == "x11"


def test_session_type_empty_without_display(monkeypatch):
    for name in ("WAYLAND_DISPLAY", "DISPLAY", "XDG_SESSION_TYPE"):
        monkeypatch.delenv(name, raising=False)
    assert linux_module.session_type() == ""


# --------------------------------------------------------------- 能力矩阵


def test_wayland_capabilities_include_wl_clipboard(monkeypatch):
    backend = _backend(monkeypatch, session="wayland", has_wl=True)
    caps = backend.capabilities()
    assert caps["wayland"] is True
    # Wayland 下不该声称支持 PRIMARY selection
    assert caps["primary_selection"] is False


def test_x11_capabilities_report_primary_selection(monkeypatch):
    backend = _backend(monkeypatch, session="x11", xlib=True)
    assert backend.capabilities()["primary_selection"] is True


def test_capabilities_focus_detection_via_xlib(monkeypatch):
    backend = _backend(monkeypatch, session="x11", xlib=True)
    assert backend.capabilities()["focus_detection"] is True


def test_is_available_wayland_requires_something(monkeypatch):
    backend = _backend(monkeypatch, session="wayland", xlib=False, has_wl=False)
    assert backend.is_available() is False
    backend = _backend(monkeypatch, session="wayland", xlib=False, has_wl=True)
    assert backend.is_available() is True


def test_is_available_x11_always(monkeypatch):
    backend = _backend(monkeypatch, session="x11", xlib=False)
    assert backend.is_available() is True


# --------------------------------------------------------------- _read_x11_selection


def test_read_x11_selection_decodes_bytes(monkeypatch):
    """正常路径：X 返回 bytes，应解码成 str。"""
    owner = _FakeOwner("主选择区内容".encode("utf-8"))
    display = _FakeDisplay(owner=owner)
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)

    assert backend._read_x11_selection("PRIMARY") == "主选择区内容"


def test_read_x11_selection_delete_is_called(monkeypatch):
    """用一个记录 delete 调用的 owner 明确验证清理。"""
    calls = {"deleted": 0}

    class _TrackingOwner:
        def convert_selection(self, *args):
            class _R:
                value = b"data"

                def delete(self_inner):
                    calls["deleted"] += 1

            return _R()

    display = _FakeDisplay(owner=_TrackingOwner())
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)

    assert backend._read_x11_selection("PRIMARY") == "data"
    assert calls["deleted"] == 1


def test_read_x11_selection_no_owner(monkeypatch):
    display = _FakeDisplay(owner=None)
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)
    assert backend._read_x11_selection("PRIMARY") == ""


def test_read_x11_selection_none_result(monkeypatch):
    """对端返回 None（超时/拒绝）时按空处理，不崩。"""
    display = _FakeDisplay(owner=_FakeOwner(None))
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)
    assert backend._read_x11_selection("PRIMARY") == ""


def test_read_x11_selection_exception_contained(monkeypatch):
    """X 连接中途断掉是常事，必须兜住。"""
    display = _FakeDisplay(owner=_FakeOwner(OSError("X 连接没了")))
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)
    assert backend._read_x11_selection("PRIMARY") == ""


def test_read_x11_selection_non_bytes_value(monkeypatch):
    """有些实现返回 str 而非 bytes，也不能崩。"""
    display = _FakeDisplay(owner=_FakeOwner("已经是字符串"))
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)
    assert backend._read_x11_selection("PRIMARY") == "已经是字符串"


# --------------------------------------------------------------- read_primary_text


def test_read_primary_wayland_uses_wl_paste(monkeypatch):
    calls = []

    def fake_run(args, input_bytes=None):
        calls.append(args)
        return "微信输入的内容".encode("utf-8")

    backend = _backend(monkeypatch, session="wayland", has_wl=True)
    monkeypatch.setattr(linux_module, "_run", fake_run)
    assert backend.read_primary_text() == "微信输入的内容"
    assert calls[0][0] == "wl-paste"
    assert "--primary" in calls[0]


def test_read_primary_wayland_wl_paste_reports_newline_flag(monkeypatch):
    """必须带 --no-newline，否则每次多一个换行，去重直接失效。"""
    calls = []
    monkeypatch.setattr(
        linux_module, "_run", lambda args, input_bytes=None: calls.append(args) or b"x"
    )
    backend = _backend(monkeypatch, session="wayland", has_wl=True)
    backend.read_primary_text()
    assert "--no-newline" in calls[0]


def test_read_primary_wayland_without_wl(monkeypatch):
    backend = _backend(monkeypatch, session="wayland", has_wl=False)
    assert backend.read_primary_text() == ""


def test_read_primary_x11_via_xlib(monkeypatch):
    display = _FakeDisplay(owner=_FakeOwner("通过 Xlib 读到的".encode("utf-8")))
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)
    assert backend.read_primary_text() == "通过 Xlib 读到的"


def test_read_primary_x11_falls_back_to_xclip(monkeypatch):
    """Xlib 取到空时应回落命令，而不是直接认输。"""
    display = _FakeDisplay(owner=_FakeOwner(None))
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)

    monkeypatch.setattr(linux_module, "_has", lambda tool: tool == "xclip")
    monkeypatch.setattr(
        linux_module, "_run", lambda args, input_bytes=None: "xclip 读到的".encode("utf-8")
    )
    assert backend.read_primary_text() == "xclip 读到的"


def test_read_primary_x11_falls_back_to_xsel(monkeypatch):
    display = _FakeDisplay(owner=_FakeOwner(None))
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)

    monkeypatch.setattr(linux_module, "_has", lambda tool: tool == "xsel")
    monkeypatch.setattr(
        linux_module, "_run", lambda args, input_bytes=None: "xsel 读到的".encode("utf-8")
    )
    assert backend.read_primary_text() == "xsel 读到的"


def test_read_primary_x11_no_tools_returns_empty(monkeypatch):
    backend = _backend(monkeypatch, xlib=False)
    monkeypatch.setattr(linux_module, "_has", lambda tool: False)
    assert backend.read_primary_text() == ""


# --------------------------------------------------------------- _focus_via_xlib


def _focus_display(active, props):
    root = _FakeRoot(active=active)
    windows = {active: _FakeWindow(props)} if active is not None else {}
    return _FakeDisplay(root=root, windows=windows)


def test_focus_via_xlib_reads_wm_class(monkeypatch):
    """WM_CLASS 是 `实例\\0类名` 两段，取最后一段作为 app_id。"""
    display = _focus_display(
        42,
        {
            "_NET_WM_NAME": "某网页标题".encode("utf-8"),
            "WM_NAME": "备用标题".encode("utf-8"),
            "WM_CLASS": b"chrome\x00Google-chrome",
        },
    )
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)

    info = backend._focus_via_xlib()
    assert info.app_id == "Google-chrome"
    assert info.window_title == "某网页标题"


def test_focus_via_xlib_falls_back_to_wm_name(monkeypatch):
    """没有 `_NET_WM_NAME` 时退到 `WM_NAME`；没有 WM_CLASS 则 app_id 留空。

    app_id 不拿标题去顶：标题是给人看的，拿去喂隐私判定会引入误判
    （随便一个网页标题都可能含「密码」）。
    """
    display = _focus_display(7, {"WM_NAME": "只有 WM_NAME".encode("utf-8")})
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)

    info = backend._focus_via_xlib()
    assert info.window_title == "只有 WM_NAME"
    assert info.app_id == ""
    assert info.is_empty is False


def test_focus_via_xlib_no_active_window(monkeypatch):
    display = _focus_display(None, {})
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)
    assert backend._focus_via_xlib().is_empty is True


def test_focus_via_xlib_property_without_value(monkeypatch):
    root = _FakeRoot(active=1)
    display = _FakeDisplay(root=root, windows={1: _FakeWindow({})})
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)
    info = backend._focus_via_xlib()
    # 取不到任何属性时返回空对象，而不是一条「未知窗口」的脏数据
    assert info.is_empty is True


def test_focus_via_xlib_exception_contained(monkeypatch):
    class _ExplodingRoot:
        def get_full_property(self, *args):
            raise OSError("X 断了")

    display = _FakeDisplay(root=_ExplodingRoot(), windows={})
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)
    assert backend._focus_via_xlib().is_empty is True


# --------------------------------------------------------------- _window_property


def test_window_property_decodes_bytes_and_strips_nul(monkeypatch):
    """WM_CLASS 里的 \\0 分隔符要被换成空格，不能留在标题里。"""
    display = _FakeDisplay()
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)

    window = _FakeWindow({"WM_CLASS": b"code\x00Code"})
    assert backend._window_property(window, "WM_CLASS") == "code Code"


def test_window_property_missing_returns_empty(monkeypatch):
    display = _FakeDisplay()
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)
    assert backend._window_property(_FakeWindow({}), "WM_NAME") == ""


def test_window_property_exception_returns_empty(monkeypatch):
    class _Exploding:
        def get_full_property(self, atom, type_):
            raise OSError("属性读不了")

    display = _FakeDisplay()
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)
    assert backend._window_property(_Exploding(), "WM_NAME") == ""


# --------------------------------------------------------------- current_focus 完整链路


def test_current_focus_prefers_xlib(monkeypatch):
    display = _focus_display(
        1, {"_NET_WM_NAME": "Xlib 拿到标题".encode("utf-8"), "WM_CLASS": b"x\x00App"}
    )
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)
    # xdotool 即使在也不该被用到
    monkeypatch.setattr(linux_module, "_has", lambda tool: True)

    info = backend.current_focus()
    assert info.window_title == "Xlib 拿到标题"
    assert info.app_id == "App"


def test_current_focus_wayland_without_xlib_is_empty(monkeypatch):
    """Wayland 拿不到前台窗口是协议设计，返回空而非报错。"""
    backend = _backend(monkeypatch, session="wayland", xlib=False)
    monkeypatch.setattr(linux_module, "_has", lambda tool: False)
    assert backend.current_focus().is_empty is True


def test_current_focus_xdotool_fallback(monkeypatch):
    """Xlib 不可用时用 xdotool，且要解析出 pid 对应的进程名。"""
    backend = _backend(monkeypatch, xlib=False)
    monkeypatch.setattr(linux_module, "_has", lambda tool: tool == "xdotool")

    answers = {
        ("xdotool", "getactivewindow"): b"12345",
        ("xdotool", "getwindowname", "12345"): "记事本 - 未命名".encode("utf-8"),
        ("xdotool", "getwindowpid", "12345"): b"999",
    }

    def fake_run(args, input_bytes=None):
        return answers.get(tuple(args), b"")

    monkeypatch.setattr(linux_module, "_run", fake_run)
    monkeypatch.setattr(linux_module, "_process_name", lambda pid: "notepad")

    info = backend.current_focus()
    assert info.window_title == "记事本 - 未命名"
    assert info.app_id == "notepad"
    assert info.process_id == 999


def test_current_focus_xdotool_non_numeric_pid(monkeypatch):
    """xdotool 拿不到 pid 时会输出空/报错文本，不能当数字解析。"""
    backend = _backend(monkeypatch, xlib=False)
    monkeypatch.setattr(linux_module, "_has", lambda tool: tool == "xdotool")

    def fake_run(args, input_bytes=None):
        if args[1] == "getactivewindow":
            return b"1"
        if args[1] == "getwindowname":
            return "标题".encode("utf-8")
        return b"not-a-number"

    monkeypatch.setattr(linux_module, "_run", fake_run)
    info = backend.current_focus()
    assert info.process_id == 0
    assert info.window_title == "标题"


def test_current_focus_xdotool_no_active_window(monkeypatch):
    backend = _backend(monkeypatch, xlib=False)
    monkeypatch.setattr(linux_module, "_has", lambda tool: tool == "xdotool")
    monkeypatch.setattr(linux_module, "_run", lambda args, input_bytes=None: b"")
    assert backend.current_focus().is_empty is True


def test_current_focus_without_any_tool(monkeypatch):
    backend = _backend(monkeypatch, xlib=False)
    monkeypatch.setattr(linux_module, "_has", lambda tool: False)
    assert backend.current_focus().is_empty is True


# --------------------------------------------------------------- _process_name


def test_process_name_reads_proc_comm(monkeypatch):
    """Linux 上没有 psutil，直接读 /proc 最省事。"""
    import builtins

    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if path == "/proc/4242/comm":
            import io

            return io.StringIO("firefox\n")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert linux_module._process_name(4242) == "firefox"


def test_process_name_missing_process(monkeypatch):
    """进程已退出时 /proc/<pid> 就没了，返回空串而不是抛异常。"""
    assert linux_module._process_name(999_999_999) == ""


# --------------------------------------------------------------- _init_xlib


def test_init_xlib_without_xlib_degrades(monkeypatch):
    """没装 python-xlib 时要静默降级，不能把后端初始化搞崩。"""
    backend = LinuxBackend.__new__(LinuxBackend)
    monkeypatch.setitem(sys.modules, "Xlib", None)
    monkeypatch.setitem(sys.modules, "Xlib.display", None)
    backend._init_xlib()
    assert backend._xlib is False
    assert backend._x_display is None


def test_constructor_degrades_without_display(monkeypatch):
    """无图形会话下构造后端不应抛异常。"""
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "tty")
    monkeypatch.delenv("DISPLAY", raising=False)
    backend = LinuxBackend()
    assert backend.name == "linux"


# --------------------------------------------------------------- 回归守卫


def test_wm_class_parsing_keeps_class_name():
    """`WM_CLASS` 是 `实例\\0类名`，要类名。

    这里曾经出过 bug：`_window_property()` 会把 NUL 换成空格便于展示，
    `_focus_via_xlib()` 却还拿换过之后的值 split("\\x00") —— 分隔符早没了，
    于是 app_id 变成「实例名 类名」整串。app_id 会喂给
    `is_password_manager()`，串了就等于放行密码管理器。
    """
    assert linux_module._wm_class_to_app_id("chrome\x00Google-chrome") == "Google-chrome"
    assert linux_module._wm_class_to_app_id("code\x00Code") == "Code"


def test_wm_class_parsing_single_segment():
    """只有一段（不规范实现）时就那一段。"""
    assert linux_module._wm_class_to_app_id("solo") == "solo"


def test_wm_class_parsing_empty_and_blank():
    assert linux_module._wm_class_to_app_id("") == ""
    assert linux_module._wm_class_to_app_id("\x00") == ""
    assert linux_module._wm_class_to_app_id("  \x00  ") == ""


def test_wm_class_parsing_ignores_extra_segments():
    """多段时取最后一段：类名恒在末尾。"""
    assert linux_module._wm_class_to_app_id("a\x00b\x00c") == "c"


def test_is_available_treats_failed_xlib_as_unavailable(monkeypatch):
    """`_xlib = False` 表示「试过但失败」，不能算可用。

    回归用例：此前写的是 `self._xlib is not None`，False 也满足该条件，
    于是 Wayland 下既没有 wl-clipboard 又连不上 X 时会谎报可用 ——
    用户看到「正常」，实际两条读写路径都不通。
    """
    backend = _backend(monkeypatch, session="wayland", xlib=False, has_wl=False)
    assert backend.is_available() is False


def test_is_available_true_when_xlib_connected(monkeypatch):
    backend = _backend(monkeypatch, session="wayland", xlib=True, has_wl=False)
    assert backend.is_available() is True


def test_focus_app_id_feeds_password_manager_detection(monkeypatch):
    """端到端确认：X11 的 app_id 解析正确，能喂给密码管理器判定。

    用例挑的是「解析错就会漏判」的组合：`1Password` 这类 WM_CLASS
    把品牌名放在**后**一段。若像旧实现那样把两段拼成一串，
    `is_password_manager()` 依然会命中 —— 所以这里额外断言
    app_id 就是类名本身，防止「碰巧命中」掩盖解析错误。
    """
    from linglong_clip.core.classify import is_password_manager

    display = _focus_display(3, {"WM_CLASS": "1password\x001Password".encode("utf-8")})
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)

    info = backend._focus_via_xlib()
    assert info.app_id == "1Password"
    assert is_password_manager(info.app_id) is True


def test_focus_app_id_does_not_leak_instance_segment(monkeypatch):
    """实例名不应留在 app_id 里：它会造成反向的**误拦**。

    实例名常是随机串或进程名，把它们拼进 app_id 后，
    `blocked_apps` 里的短词（例如 "pass"）可能被意外命中。
    这里用「只有实例名含关键词」的构造证明两者确实被分开了。
    """
    from linglong_clip.core.classify import is_password_manager

    # 实例名带 pass，类名不带 —— 正确解析应当**不**命中
    display = _focus_display(9, {"WM_CLASS": "mypass-tool\0MyEditor".encode("utf-8")})
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)

    info = backend._focus_via_xlib()
    assert info.app_id == "MyEditor"
    assert is_password_manager(info.app_id) is False


# --------------------------------------------------------------- 剩余分支


def test_run_returns_stdout_on_success(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = b"hello"

    monkeypatch.setattr(linux_module.subprocess, "run", lambda *a, **k: _Proc())
    assert linux_module._run(["echo", "hello"]) == b"hello"


def test_run_returns_empty_on_nonzero_exit(monkeypatch):
    class _Proc:
        returncode = 1
        stdout = "不该被采用".encode("utf-8")

    monkeypatch.setattr(linux_module.subprocess, "run", lambda *a, **k: _Proc())
    assert linux_module._run(["false"]) == b""


def test_run_returns_empty_on_missing_tool(monkeypatch):
    """工具不在时 subprocess 抛 FileNotFoundError，必须吞掉。"""
    monkeypatch.setattr(
        linux_module.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("没这个命令"))
    )
    assert linux_module._run(["nope"]) == b""


def test_run_returns_empty_on_timeout(monkeypatch):
    """超时会抛 TimeoutExpired，同样不能让调用方崩。"""
    import subprocess as _sp

    monkeypatch.setattr(
        linux_module.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(_sp.TimeoutExpired("cmd", 2.0)),
    )
    assert linux_module._run(["slow"]) == b""


def test_read_x11_selection_owner_is_none_atom(monkeypatch):
    """`owner == X.NONE` 表示没人持有选择区，直接返回空。"""
    display = _FakeDisplay(owner=None)
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)

    # 让 get_selection_owner 明确返回 X.NONE（0）
    monkeypatch.setattr(display, "get_selection_owner", lambda atom: 0)
    assert backend._read_x11_selection("PRIMARY") == ""


def test_window_raw_property_keeps_nul(monkeypatch):
    """原始读取必须保留 NUL —— 这是 `_wm_class_to_app_id` 能工作的前提。"""
    display = _FakeDisplay()
    _install_fake_xlib(monkeypatch, display)
    backend = _backend(monkeypatch, display=display)

    window = _FakeWindow({"WM_CLASS": b"inst\x00Class"})
    assert backend._window_raw_property(window, "WM_CLASS") == "inst\x00Class"
    # 展示用的读取则换成空格
    assert backend._window_property(window, "WM_CLASS") == "inst Class"


def test_sanity_check_x11_with_xclip(monkeypatch):
    backend = _backend(monkeypatch, session="x11", xlib=False)
    monkeypatch.setattr(linux_module, "_has", lambda tool: tool == "xclip")
    ok, detail = backend.sanity_check()
    assert ok is True
    assert "xclip" in detail


def test_sanity_check_wayland_falls_to_xwayland(monkeypatch):
    """Wayland 下没 wl-clipboard 但有 XWayland + Xlib 仍算可用。"""
    backend = _backend(monkeypatch, session="wayland", xlib=True, has_wl=False)
    ok, detail = backend.sanity_check()
    assert ok is True
    assert "XWayland" in detail


def test_sanity_check_wayland_nothing_available(monkeypatch):
    backend = _backend(monkeypatch, session="wayland", xlib=False, has_wl=False)
    monkeypatch.setattr(linux_module, "_has", lambda tool: False)
    ok, detail = backend.sanity_check()
    assert ok is False
    assert "Wayland" in detail


def test_init_xlib_success_path(monkeypatch):
    """X 连接成功时 `_xlib` 置 True、句柄留存。"""
    display = _FakeDisplay()
    _install_fake_xlib(monkeypatch, display)
    backend = LinuxBackend.__new__(LinuxBackend)
    backend._init_xlib()
    assert backend._xlib is True
    assert backend._x_display is display


def test_init_xlib_display_raises_degrades(monkeypatch):
    """X 连接抛异常（DISPLAY 指向不存在的服务器）要降级而不是崩。"""
    display_module = types.ModuleType("Xlib.display")

    def _boom(*a, **k):
        raise OSError("无法连接 X 服务器")

    display_module.Display = _boom
    xlib_pkg = types.ModuleType("Xlib")
    xlib_pkg.display = display_module
    monkeypatch.setitem(sys.modules, "Xlib", xlib_pkg)
    monkeypatch.setitem(sys.modules, "Xlib.display", display_module)

    backend = LinuxBackend.__new__(LinuxBackend)
    backend._init_xlib()
    assert backend._xlib is False
    assert backend._x_display is None
