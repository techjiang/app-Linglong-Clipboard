"""Linux 后端：X11 / Wayland 双栈适配。

- Wayland：通过 wl-paste / wl-copy 读写，且能直接拿到 `text/uri-list`，
  不需要 X11 的 PRIMARY selection 轮询。会话类型从环境变量判断。
- X11：优先用 Xlib 拿 PRIMARY selection 与前台窗口 `_NET_ACTIVE_WINDOW`；
  Xlib 不可用时退回命令行工具。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Dict, List, Optional

from .base import ClipboardBackend, FocusInfo

_TIMEOUT = 2.0


def _run(args: List[str], input_bytes: Optional[bytes] = None) -> bytes:
    try:
        proc = subprocess.run(
            args,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return b""
    return proc.stdout if proc.returncode == 0 else b""


def _has(tool: str) -> bool:
    return shutil.which(tool) is not None


def session_type() -> str:
    """返回 `wayland`、`x11` 或空字符串。"""
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return os.environ.get("XDG_SESSION_TYPE", "").lower()


class LinuxBackend(ClipboardBackend):
    name = "linux"

    def __init__(self) -> None:
        self._session = session_type()
        self._has_wl = _has("wl-paste") and _has("wl-copy")
        self._xlib = None
        self._x_display = None
        # 已知的非剪贴板持有者（剪贴板管理器自身），避免自激循环
        self._self_pids: set[int] = {os.getpid()}
        if self._session == "x11":
            self._init_xlib()

    # ---- 可用性 ----

    def is_available(self) -> bool:
        if self._session == "wayland":
            # 注意 `self._xlib` 是三态：True=连通、False=试过但失败、None=没试过。
            # 之前写的是 `self._xlib is not None` —— 于是「试过且失败」也被算成
            # 可用，Wayland 下没有 wl-clipboard 又连不上 X 时会谎报可用。
            return self._has_wl or self._xlib is True
        return True

    def _init_xlib(self) -> None:
        try:
            from Xlib import display as xdisplay  # type: ignore

            self._x_display = xdisplay.Display()
            self._xlib = True
        except Exception:  # noqa: BLE001 - 无 Xlib 时走命令行工具
            self._xlib = False
            self._x_display = None

    def capabilities(self) -> Dict[str, bool]:
        return {
            "primary_selection": self._session != "wayland" and (
                self._xlib is True or _has("xclip") or _has("xsel")
            ),
            "native_history": False,
            "focus_detection": bool(self._xlib) or _has("xdotool"),
            "transient_clipboard": False,
            "wayland": self._session == "wayland",
        }

    # ---- 自检 ----

    def sanity_check(self):
        """自检用：确认本会话下至少有一条可用的读取路径。

        只读探测，不改写剪贴板；Wayland 与 X11 分别验证各自的工具链。
        """
        if self._session == "wayland":
            if self._has_wl:
                return True, "wl-paste 可用（Wayland 读写走 wl-clipboard）"
            if self._xlib:
                return True, "XWayland + python-xlib 可用"
            return False, "Wayland 会话下既无 wl-clipboard 也无 Xlib，读写将全部落在 Qt 层"
        if self._xlib is True:
            return True, "python-xlib 已连接 X 服务器"
        for tool in ("xclip", "xsel"):
            if _has(tool):
                return True, f"{tool} 可用（X11 命令行读写）"
        return False, "X11 会话下未找到 Xlib / xclip / xsel，读写将全部落在 Qt 层"

    # ---- 主选择区 ----

    def read_primary_text(self) -> str:
        if self._session == "wayland":
            if self._has_wl:
                return _run(["wl-paste", "--primary", "--no-newline"]).decode(
                    "utf-8", "replace"
                )
            return ""
        if self._xlib and self._x_display is not None:
            text = self._read_x11_selection("PRIMARY")
            if text:
                return text
        if _has("xclip"):
            return _run(["xclip", "-selection", "primary", "-o"]).decode("utf-8", "replace")
        if _has("xsel"):
            return _run(["xsel", "--primary", "--output"]).decode("utf-8", "replace")
        return ""

    def _read_x11_selection(self, selection: str) -> str:
        try:
            import Xlib.X  # type: ignore
            from Xlib import X  # type: ignore

            atom = self._x_display.get_atom(selection)  # type: ignore[union-attr]
            utf8 = self._x_display.get_atom("UTF8_STRING")  # type: ignore[union-attr]
            owner = self._x_display.get_selection_owner(atom)  # type: ignore[union-attr]
            if owner == X.NONE:
                return ""
            result = owner.convert_selection(atom, utf8, X.CurrentTime, X.CurrentTime)
            if result is None:
                return ""
            data = result.value
            result.delete()
            if isinstance(data, bytes):
                return data.decode("utf-8", "replace")
            return str(data)
        except Exception:  # noqa: BLE001
            return ""

    # ---- 前台窗口 ----

    def current_focus(self) -> FocusInfo:
        if self._xlib and self._x_display is not None:
            info = self._focus_via_xlib()
            if not info.is_empty:
                return info
        if _has("xdotool"):
            wid = _run(["xdotool", "getactivewindow"]).decode().strip()
            if wid:
                title = _run(["xdotool", "getwindowname", wid]).decode("utf-8", "replace").strip()
                pid_text = _run(["xdotool", "getwindowpid", wid]).decode().strip()
                name = ""
                if pid_text.isdigit():
                    name = _process_name(int(pid_text))
                return FocusInfo(
                    app_id=name,
                    app_name=name or title,
                    window_title=title,
                    process_id=int(pid_text) if pid_text.isdigit() else 0,
                )
        return FocusInfo()

    def _focus_via_xlib(self) -> FocusInfo:
        try:
            from Xlib import X  # type: ignore

            root = self._x_display.screen().root  # type: ignore[union-attr]
            net_active = self._x_display.get_atom("_NET_ACTIVE_WINDOW")  # type: ignore[union-attr]
            prop = root.get_full_property(net_active, X.AnyPropertyType)
            if not prop or not prop.value:
                return FocusInfo()
            window = self._x_display.create_resource_object("window", prop.value[0])  # type: ignore[union-attr]
            title = self._window_property(window, "_NET_WM_NAME") or self._window_property(
                window, "WM_NAME"
            )
            # WM_CLASS 的取值是 `实例名\0类名` 两段，要的是**类名**（后一段）。
            # 必须用保留 NUL 的原始读取：`_window_property()` 会把 NUL 换成空格
            # 方便展示，拿它再 split("\x00") 永远分不开，app_id 会变成
            # 「实例名 类名」整串 —— 而 app_id 是要喂给隐私判定的
            # （`is_password_manager(app_id)`），串了就等于放行密码管理器。
            raw_class = self._window_raw_property(window, "WM_CLASS")
            app_id = _wm_class_to_app_id(raw_class)
            return FocusInfo(app_id=app_id, app_name=app_id or title, window_title=title)
        except Exception:  # noqa: BLE001
            return FocusInfo()

    def _window_property(self, window, name: str) -> str:
        """读窗口属性，返回**可直接展示**的文本（NUL 换成空格）。"""
        raw = self._window_raw_property(window, name)
        return raw.replace("\x00", " ").strip()

    def _window_raw_property(self, window, name: str) -> str:
        """读窗口属性并**保留 NUL 分隔符**，供需要按段解析的调用方使用。"""
        try:
            atom = self._x_display.get_atom(name)  # type: ignore[union-attr]
            prop = window.get_full_property(atom, 0)
            if not prop or prop.value is None:
                return ""
            value = prop.value
            if isinstance(value, bytes):
                return value.decode("utf-8", "replace")
            return str(value)
        except Exception:  # noqa: BLE001
            return ""


def _wm_class_to_app_id(raw: str) -> str:
    """把 `WM_CLASS` 的原始值解析成应用标识。

    取值形如 `chrome\0Google-chrome`，后一段（类名）才稳定，因为它由
    WM_CLASS 的第二个字段决定、与实例名无关。只有一段时就用那一段。
    取不到时返回空串，交给调用方回落到窗口标题。
    """
    if not raw:
        return ""
    parts = [part.strip() for part in raw.split("\x00") if part.strip()]
    if not parts:
        return ""
    return parts[-1]


def _process_name(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""
