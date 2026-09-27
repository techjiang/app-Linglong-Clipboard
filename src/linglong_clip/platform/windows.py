"""Windows 后端。

通过 ctypes 调用 Win32 API，不引入第三方依赖：
- `GetClipboardSequenceNumber` 低成本判断剪贴板是否变化；
- `GetForegroundWindow` + `QueryFullProcessImageNameW` 取来源进程；
- `AddClipboardFormatListener` 支持事件驱动（由 Qt 层的 nativeEventFilter 调用）；
- `OpenClipboard` 时用 `CF_CLIPBOARD_VIEWER_IGNORE` 防止成为剪贴板查看器；
- 写入时附加 `ExcludeClipboardContentFromMonitorProcessing`，让安全内容不进历史。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Dict, List

from .base import ClipboardBackend, FocusInfo

user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
shell32 = ctypes.WinDLL("shell32", use_last_error=True) if os.name == "nt" else None

WM_CLIPBOARDUPDATE = 0x031D
HWND_MESSAGE = -3

CF_CLIPBOARD_VIEWER_IGNORE = 0x03CB
CLIPBOARD_FORMAT_EXCLUDE_MONITOR = "ExcludeClipboardContentFromMonitorProcessing"
CLIPBOARD_FORMAT_CAN_INCLUDE = "CanIncludeInClipboardHistory"


class WindowsBackend(ClipboardBackend):
    name = "windows"
    requires_polling = False

    def __init__(self, listener_hwnd: int = 0) -> None:
        self._listener_hwnd = listener_hwnd
        self._registered = False
        self._sequence = 0

    def is_available(self) -> bool:
        return user32 is not None

    def capabilities(self) -> Dict[str, bool]:
        return {
            "primary_selection": False,
            "native_history": self.is_history_supported(),
            "focus_detection": True,
            "transient_clipboard": True,
            "wayland": False,
        }

    # ---- 剪贴板变化检测 ----

    def clipboard_sequence(self) -> int:
        """序列号变化即代表剪贴板被写过，比逐条比对内容便宜得多。"""
        if user32 is None:
            return 0
        try:
            return int(user32.GetClipboardSequenceNumber())
        except Exception:  # noqa: BLE001
            return 0

    def has_changed(self) -> bool:
        current = self.clipboard_sequence()
        if current == self._sequence:
            return False
        self._sequence = current
        return True

    # ---- 事件驱动 ----

    def register_listener(self, hwnd: int) -> bool:
        if user32 is None or not hwnd:
            return False
        try:
            ok = bool(user32.AddClipboardFormatListener(wintypes.HWND(hwnd)))
        except Exception:  # noqa: BLE001
            return False
        # 句柄只在注册成功后才记下来 —— 否则失败时 `unregister_listener`
        # 会以为「注册过但没记住句柄」，白白多做一次多余的注销尝试。
        if ok:
            self._registered = True
            self._listener_hwnd = hwnd
        return ok

    def unregister_listener(self) -> None:
        if user32 is None or not self._registered or not self._listener_hwnd:
            return
        handle = self._listener_hwnd
        try:
            user32.RemoveClipboardFormatListener(wintypes.HWND(handle))
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._registered = False
            self._listener_hwnd = 0

    # ---- 剪贴板打开/关闭 ----

    def open_clipboard(self, retries: int = 5) -> bool:
        """其它进程常短暂占用剪贴板，需要重试。"""
        if user32 is None:
            return False
        for _ in range(retries):
            if user32.OpenClipboard(None):
                return True
            # 用 kernel32 已加载的句柄，不再走 `ctypes.windll`——
            # 后者只在 Windows 上存在，一旦缺失就会把「重试」变成抛异常。
            if kernel32 is not None:
                try:
                    kernel32.Sleep(10)
                except Exception:  # noqa: BLE001
                    pass
        return False

    def close_clipboard(self) -> None:
        if user32 is not None:
            user32.CloseClipboard()

    # ---- 前台窗口 ----

    def current_focus(self) -> FocusInfo:
        if user32 is None or kernel32 is None:
            return FocusInfo()
        try:
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return FocusInfo()
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            title = self._window_text(hwnd)
            exe_path, exe_name = self._process_info(pid.value)
            return FocusInfo(
                app_id=exe_name,
                app_name=exe_name or title,
                window_title=title,
                process_id=int(pid.value),
            )
        except Exception:  # noqa: BLE001
            return FocusInfo()

    def _window_text(self, hwnd: int) -> str:
        try:
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return ""
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            return buffer.value
        except Exception:  # noqa: BLE001
            return ""

    def _process_info(self, pid: int) -> tuple[str, str]:
        """返回 (可执行文件全路径, 进程名)。"""
        if not pid:
            return "", ""
        access = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
        handle = kernel32.OpenProcess(access, False, pid)
        if not handle:
            return "", ""
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                path = buffer.value
                return path, os.path.basename(path)
            return "", ""
        finally:
            kernel32.CloseHandle(handle)

    # ---- 安全粘贴 ----

    def is_transient_supported(self) -> bool:
        return user32 is not None

    def exclude_from_history(self) -> None:
        """在剪贴板已打开的前提下，标记内容不进历史与云同步。"""
        if user32 is None:
            return
        try:
            fmt = user32.RegisterClipboardFormatW(CLIPBOARD_FORMAT_EXCLUDE_MONITOR)
            if fmt:
                user32.SetClipboardData(fmt, None)
        except Exception:  # noqa: BLE001
            pass

    def allow_in_history(self) -> None:
        if user32 is None:
            return
        try:
            fmt = user32.RegisterClipboardFormatW(CLIPBOARD_FORMAT_CAN_INCLUDE)
            if fmt:
                user32.SetClipboardData(fmt, None)
        except Exception:  # noqa: BLE001
            pass

    # ---- 自检 ----

    def sanity_check(self):
        """自检用：确认 Win32 剪贴板 API 能真实调起。

        只读探测：打开剪贴板、读一次序列号再关掉，不改写任何内容。
        """
        if user32 is None:
            return False, "user32 未加载（非 Windows 或加载被拒）"
        if not self.open_clipboard():
            return False, "OpenClipboard 失败（剪贴板被其它进程长期占用？）"
        try:
            watched = user32.GetClipboardSequenceNumber()
        finally:
            self.close_clipboard()
        if not watched:
            return False, "GetClipboardSequenceNumber 返回 0"
        return True, f"Win32 剪贴板可访问（序列号 {int(watched)}）"

    def is_history_supported(self) -> bool:
        """Win10 1809+ 起系统自带历史，此处仅探测可用性。"""
        if user32 is None:
            return False
        try:
            return bool(user32.GetClipboardSequenceNumber)
        except Exception:  # noqa: BLE001
            return False
