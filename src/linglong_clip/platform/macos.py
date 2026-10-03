"""macOS 后端。

只用系统自带命令，不依赖 pyobjc（降低打包体积）：

- `pbpaste` 读剪贴板（带 `-Prefer txt` 拿到纯文本）；
- `pbcopy` 写剪贴板，配合 `osascript` 实现「写入即过期」的临时剪贴板；
- `osascript` + System Events 取前台应用，作为记录的来源标注。

此前这个后端只实现了 `current_focus()`，能力矩阵却写着
`transient_clipboard: True` —— 声明了安全粘贴却做不到。
现在补上读写与临时剪贴板的真实实现。
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from typing import Dict

from .base import ClipboardBackend, FocusInfo

_TIMEOUT = 2.0


def _run(args, input_bytes: bytes = b"") -> bytes:
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


def _osascript(script: str) -> str:
    return _run(["osascript", "-e", script]).decode("utf-8", "replace").strip()


class MacOSBackend(ClipboardBackend):
    name = "macos"
    #: `pbcopy`/`pbpaste` 是即时调用，不需要轮询
    requires_polling = True

    def __init__(self) -> None:
        self._has_pbpaste = _has("pbpaste")
        self._has_pbcopy = _has("pbcopy")
        self._has_osascript = _has("osascript")
        #: 临时剪贴板的过期定时器句柄，避免被 GC 提前回收
        self._timers: list[threading.Timer] = []

    # ---- 可用性 ----

    def is_available(self) -> bool:
        # osascript 缺失也能工作（来源标注会退化），两者都没有才算不可用
        return self._has_pbpaste or self._has_pbcopy or self._has_osascript

    def capabilities(self) -> Dict[str, bool]:
        return {
            "primary_selection": False,
            "native_history": False,
            "focus_detection": self._has_osascript,
            "transient_clipboard": self.is_transient_supported(),
            "wayland": False,
        }

    # ---- 剪贴板读写 ----

    def read_text(self) -> str:
        """读剪贴板纯文本。优先 `-Prefer txt`，避免拿到富文本的拼装结果。"""
        if not self._has_pbpaste:
            return ""
        text = _run(["pbpaste", "-Prefer", "txt"]).decode("utf-8", "replace")
        if text:
            return text
        return _run(["pbpaste"]).decode("utf-8", "replace")

    def write_text(self, text: str) -> bool:
        """写剪贴板。`pbcopy` 无参数、只从 stdin 读，所以走 input。"""
        if not self._has_pbcopy:
            return False
        try:
            proc = subprocess.run(
                ["pbcopy"],
                input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def sanity_check(self):
        """自检用：确认 `pbpaste` 能真实调起。只读，不改写剪贴板。"""
        if not self._has_pbpaste:
            return False, "找不到 pbpaste（macOS 系统自带，缺失通常说明 PATH 被改过）"
        # 直接调一次：即使剪贴板为空，命令本身也应成功返回
        try:
            proc = subprocess.run(
                ["pbpaste", "-Prefer", "txt"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"pbpaste 调用失败：{exc}"
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()[:120]
            return False, f"pbpaste 返回码 {proc.returncode}：{err}"
        length = len(proc.stdout)
        return True, f"pbpaste 可用（当前剪贴板 {length} 字节）"

    # ---- 前台应用 ----

    def current_focus(self) -> FocusInfo:
        if not self._has_osascript:
            return FocusInfo()
        bundle_id = _osascript(
            'tell application "System Events" to get bundle identifier of '
            "first application process whose frontmost is true"
        )
        app_name = _osascript(
            'tell application "System Events" to get name of '
            "first application process whose frontmost is true"
        )
        window_title = _osascript(
            'tell application "System Events" to get name of front window of '
            "(first application process whose frontmost is true)"
        )
        if not app_name and not bundle_id:
            return FocusInfo()
        return FocusInfo(
            app_id=bundle_id,
            app_name=app_name or bundle_id,
            window_title=window_title,
        )

    # ---- 临时剪贴板（安全粘贴）----

    def is_transient_supported(self) -> bool:
        return self._has_pbcopy

    def set_transient_text(self, text: str, expire_ms: int = 0) -> bool:
        """写入剪贴板，并在 `expire_ms` 后尽力清除。

        macOS 没有「一次性剪贴板」的公开接口（`NSPasteboard` 的
        `prepareForNewContents` 只能标记，不能定时）。这里退一步：
        到期后用 `pbcopy` 写入空内容。

        有两个必须说清的局限，不能当作「安全」使用：

        1. 清除是**带条件的**：会先比对当前剪贴板内容是否仍是当初写入的那份，
           只有一致才清。否则用户在这期间复制了别的东西，我们一清就清错了。
        2. 进程退出后定时器就没了，**过期清理不会发生**。
           所以这只是「减少暴露窗口」，不是「擦除痕迹」。
           真要处理密钥，用「敏感内容压根不落盘 + 复制出站时打码」那条路径。
        """
        if not self.write_text(text):
            return False
        if expire_ms <= 0:
            return True

        def _expire() -> None:
            # 只在内容仍然是我们写的那份时才清，避免误删用户后来的复制
            if self.read_text() == text:
                self.write_text("")

        timer = threading.Timer(expire_ms / 1000.0, _expire)
        timer.daemon = True
        # 清掉已结束的定时器，避免长期运行后列表无限增长
        self._timers = [item for item in self._timers if item.is_alive()]
        self._timers.append(timer)
        timer.start()
        return True

