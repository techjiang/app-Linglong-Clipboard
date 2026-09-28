"""Windows 后端。

通过 ctypes 调用 Win32 API，不引入第三方依赖：
- `GetClipboardSequenceNumber` 低成本判断剪贴板是否变化；
- `GetForegroundWindow` + `QueryFullProcessImageNameW` 取来源进程；
- `AddClipboardFormatListener` 支持事件驱动（由 Qt 层的 nativeEventFilter 调用）；
- `OpenClipboard` 时用 `CF_CLIPBOARD_VIEWER_IGNORE` 防止成为剪贴板查看器；
- 写入时附加 `ExcludeClipboardContentFromMonitorProcessing`，让安全内容不进历史；
- 用 WinRT `Clipboard.GetHistoryItemsAsync()` 读系统剪贴板历史，
  补齐本工具**启动之前**复制的内容。
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import threading
from ctypes import wintypes
from typing import Any, Dict, List, Optional, Sequence

from .base import ClipboardBackend, FocusInfo

user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
shell32 = ctypes.WinDLL("shell32", use_last_error=True) if os.name == "nt" else None

WM_CLIPBOARDUPDATE = 0x031D
HWND_MESSAGE = -3

CF_CLIPBOARD_VIEWER_IGNORE = 0x03CB
CLIPBOARD_FORMAT_EXCLUDE_MONITOR = "ExcludeClipboardContentFromMonitorProcessing"
CLIPBOARD_FORMAT_CAN_INCLUDE = "CanIncludeInClipboardHistory"


# ------------------------------------------------------------------ WinRT 历史读取

#: WinRT 历史接口的注册表开关：系统设置里关掉历史后，API 能调但永远返回空，
#: 必须读这个值才能区分「没开历史」与「历史是空的」。
HISTORY_REG_PATH = r"Software\Microsoft\Clipboard"
HISTORY_REG_VALUE = "EnableClipboardHistory"


def _on_windows() -> bool:
    """当前是否运行在 Windows 上。

    抽成函数而不是内联 `os.name == "nt"`，是为了让测试能替换它 ——
    CI 跑在 Linux 上，内联判断会让整个历史读取分支永远探不到。
    """
    return os.name == "nt"


class _WinRtUnavailable(RuntimeError):
    """WinRT 投影不可用（非 Windows、缺 `winrt`/`winsdk` 包、系统过旧）。"""


def _load_winrt() -> Any:
    """惰性加载 WinRT 投影。

    依次尝试两个已知的包名：官方 `winrt` 与社区打包的 `winsdk`。
    都不在时抛 `_WinRtUnavailable`，由调用方降级 —— 历史读取是增强能力，
    不是启动依赖。
    """
    errors = []
    for module_name in ("winrt.windows.applicationmodel.datatransfer",
                        "winsdk.windows.applicationmodel.datatransfer"):
        try:
            return __import__(module_name, fromlist=["Clipboard"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{module_name}: {exc!r}")
    raise _WinRtUnavailable("；".join(errors) or "未找到 WinRT 投影")


def history_enabled() -> Optional[bool]:
    """系统「剪贴板历史」开关状态。

    返回 `True` / `False` / `None`（读不到，例如非 Windows 或无权限）。
    """
    if not _on_windows():
        return None
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, HISTORY_REG_PATH) as key:
            value, _ = winreg.QueryValueEx(key, HISTORY_REG_VALUE)
        return bool(value)
    except Exception:  # noqa: BLE001 - 键不存在 = 从未开启过
        return None


def _iter_history_items(raw: Any) -> Sequence[Any]:
    """把 WinRT 的异步结果摊平成可迭代列表。"""
    if raw is None:
        return []
    items = getattr(raw, "items", None)
    if items is None:
        return []
    try:
        return list(items)
    except TypeError:
        return []


def _history_item_to_result(item: Any) -> Optional["CaptureResult"]:
    """把一条 WinRT 历史项转成 `CaptureResult`。

    只取文本：历史里的图片/文件需要额外的 `DataPackageView` 解包，
    且系统历史本身只缓存缩略图，拿不到原图 —— 与其给出半份数据，
    不如明确只支持文本。取不到文本的项返回 `None` 由调用方跳过。
    """
    from .base import CaptureResult

    content = getattr(item, "content", None)
    if content is None:
        return None
    try:
        text = content.get_text_async().get_results()
    except Exception:  # noqa: BLE001 - 单项失败不该拖垮整批
        return None
    if not text:
        return None
    return CaptureResult(kind="text", text=str(text))


class WindowsBackend(ClipboardBackend):
    name = "windows"
    requires_polling = False

    def __init__(self, listener_hwnd: int = 0) -> None:
        self._listener_hwnd = listener_hwnd
        self._registered = False
        self._sequence = 0
        #: 限时清空的定时器。长期运行必须清掉已结束的，否则列表无限增长
        self._timers: List[threading.Timer] = []

    def is_available(self) -> bool:
        return user32 is not None

    def capabilities(self) -> Dict[str, bool]:
        return {
            "primary_selection": False,
            "native_history": self.is_history_supported(),
            "focus_detection": True,
            # 必须跟着实现走：此前这里写死 True，而 `set_transient_text`
            # 从未实现，Linux 上跑不到的这条声明也就没人发现 ——
            # 与 macOS 那个写死 `transient_clipboard: True` 是同一个毛病。
            "transient_clipboard": self.is_transient_supported(),
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
        for attempt in range(retries):
            if user32.OpenClipboard(None):
                return True
            # 最后一次失败后直接放弃，不再白等一个间隔 ——
            # 那一觉没有任何东西会因此变好。
            if attempt == retries - 1:
                break
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
                # 必须用 ntpath 而不是 os.path：这里处理的一定是 Windows 路径。
                # `os.path.basename` 的行为随宿主平台变 —— 在非 Windows 上跑
                # （例如 CI 里的单元测试）它不认反斜杠，会把整条路径原样返回，
                # 于是 app_id 变成 `C:\\Program Files\\App\\app.exe`。
                # 而 app_id 要喂给密码管理器判定，这种「只在别的平台上出错」的
                # 解析正是最该避免的。
                return path, ntpath.basename(path)
            return "", ""
        finally:
            kernel32.CloseHandle(handle)

    # ---- 安全粘贴 ----

    def is_transient_supported(self) -> bool:
        """能否做到「写入后限时清除」。

        这里必须与 `set_transient_text()` 的实现同步：此前它只判断
        `user32 is not None`，却在 Linux 上给出「支持」的结论 ——
        和 macOS 那个写死 `transient_clipboard: True` 是同一个毛病。
        """
        return user32 is not None and kernel32 is not None

    def history_exclusion_formats(self) -> List[str]:
        """Windows 的系统级「不进剪贴板历史」标记。

        挂上这个格式后，系统会跳过剪贴板历史与云同步 —— 这是目前
        唯一能**在写入时**声明「别记这条」的机制。内容一旦进了历史，
        任何工具都取不出来，所以只能靠这一步。
        """
        return [CLIPBOARD_FORMAT_EXCLUDE_MONITOR]

    def set_transient_text(self, text: str, expire_ms: int = 0) -> bool:
        """写入剪贴板，让系统在 `expire_ms` 后丢弃它。

        与 macOS 版本的两处关键差别：

        1. **期限交给系统**。凭据上调用 `SetClipboardData` 时附带
           `CF_DSP` 语义的延迟渲染不可靠，所以这里用 Win32 的
           `OpenClipboard` + `EmptyClipboard` 原生写入，并在写入前
           挂上排除格式，避免内容先被历史记一份。
        2. **清理仍是进程内的**。Windows 没有「到点自动清空」的公开接口，
           所以到期清空仍由本地定时器执行，进程退出后不再发生。
           这一点必须说清，不能当作「密钥擦除」。

        返回 False 表示原生写入不可用，调用方应退回 Qt 写入 + 自行定时。
        """
        if user32 is None or kernel32 is None:
            return False
        if not self.open_clipboard():
            return False
        try:
            if not self._write_text_native(text):
                return False
            # 排除标记必须与内容在同一次打开里挂上 —— 关掉剪贴板后再挂
            # 就晚了，系统已经读过一遍内容。
            self._mark_excluded()
        finally:
            self.close_clipboard()

        if expire_ms <= 0:
            return True

        def _expire() -> None:
            # 只在内容仍是我们写的那份时才清，避免误删用户后来的复制
            if self.read_text() == text:
                self.set_text_native("")

        self._schedule_expiry(expire_ms, _expire)
        return True

    def _write_text_native(self, text: str) -> bool:
        """在剪贴板已打开的前提下写入 CF_UNICODETEXT。"""
        try:
            if not user32.EmptyClipboard():
                return False
            data = ctypes.create_unicode_buffer(text)
            handle = kernel32.GlobalAlloc(0x0002, ctypes.sizeof(data))  # GMEM_MOVEABLE
            if not handle:
                return False
            locked = kernel32.GlobalLock(handle)
            if not locked:
                kernel32.GlobalFree(handle)
                return False
            try:
                ctypes.memmove(locked, ctypes.byref(data), ctypes.sizeof(data))
            finally:
                kernel32.GlobalUnlock(handle)
            CF_UNICODETEXT = 13
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                return False
            # 所有权已交给系统，不能再 GlobalFree
            return True
        except Exception:  # noqa: BLE001
            return False

    def _mark_excluded(self) -> None:
        """在剪贴板已打开的前提下挂上「不进历史」格式。"""
        if user32 is None:
            return
        try:
            fmt = user32.RegisterClipboardFormatW(CLIPBOARD_FORMAT_EXCLUDE_MONITOR)
            if fmt:
                user32.SetClipboardData(fmt, None)
        except Exception:  # noqa: BLE001
            pass

    def set_text_native(self, text: str) -> bool:
        """原生写入纯文本，不经过 Qt。自检与限时清空都用它。"""
        if user32 is None:
            return False
        if not self.open_clipboard():
            return False
        try:
            return self._write_text_native(text)
        finally:
            self.close_clipboard()

    def read_text(self) -> str:
        """读剪贴板纯文本。

        存在的理由：限时清空必须先确认「剪贴板里还是不是我写的那份」，
        否则用户复制了别的内容我们再清一次，删掉的是他自己的东西。
        此前 Windows 后端根本没有读剪贴板的能力 —— 条件清理也就无从谈起。
        """
        if user32 is None or kernel32 is None:
            return ""
        if not self.open_clipboard():
            return ""
        try:
            return self._read_text_native()
        finally:
            self.close_clipboard()

    @staticmethod
    def _read_text_native() -> str:
        """在剪贴板已打开的前提下读 CF_UNICODETEXT。"""
        try:
            CF_UNICODETEXT = 13
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            try:
                # 句柄指向一个以 NUL 结尾的宽字符缓冲
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        except Exception:  # noqa: BLE001
            return ""

    def _schedule_expiry(self, expire_ms: int, action) -> None:
        """安排一次限时清空，顺带清掉已结束的定时器。"""
        self._timers = [item for item in self._timers if item.is_alive()]
        timer = threading.Timer(expire_ms / 1000.0, action)
        timer.daemon = True
        self._timers.append(timer)
        timer.start()

    def exclude_from_history(self) -> None:
        """在剪贴板已打开的前提下，标记内容不进历史与云同步。"""
        self._mark_excluded()

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

    # ---- 系统剪贴板历史 ----

    def is_history_supported(self) -> bool:
        """系统剪贴板历史是否**真的**可读。

        此前这里只判断 `GetClipboardSequenceNumber` 这个符号存不存在。
        那个函数是 Win2000 就有的基础 API，和「历史」没有半点关系 ——
        于是能力矩阵里 `native_history` 恒为「是」，而 `read_windows()`
        一直返回空列表。声明了能力却没有实现，比不声明更糟。

        现在改成按真实链路探测：WinRT 投影能否导入 + 系统开关是否打开。
        """
        if not _on_windows():
            return False
        try:
            _load_winrt()
        except _WinRtUnavailable:
            return False
        # 开关明确关闭时不算支持；读不到开关（None）时按可读处理，
        # 由 read_windows() 自己按空结果收敛。
        return history_enabled() is not False

    def read_windows(self, limit: int = 30) -> List[CaptureResult]:
        """读系统剪贴板历史，补齐本工具启动前复制的内容。

        Windows 10 1809+ 才提供该接口。返回项按系统给出的顺序（新到旧），
        只含**文本**条目 —— 系统历史对图片只存缩略图，转成我们的图片记录
        会失真，不如不收。

        任何一步失败都返回空列表，不抛异常：历史导入是增强能力，
        不该让主流程启动不了。
        """
        from .base import CaptureResult

        if not _on_windows() or limit <= 0:
            return []
        if history_enabled() is False:
            # 系统设置里关掉了历史。此时 API 可调但必然为空，
            # 直接返回，省掉一次无意义的跨进程调用。
            return []
        try:
            winrt = _load_winrt()
            clipboard = winrt.Clipboard
            raw = clipboard.get_history_items_async().get_results()
        except Exception:  # noqa: BLE001 - WinRT 缺失/系统过旧/权限被拒
            return []

        results: List[CaptureResult] = []
        for item in _iter_history_items(raw):
            if len(results) >= limit:
                break
            converted = _history_item_to_result(item)
            if converted is not None:
                results.append(converted)
        return results
