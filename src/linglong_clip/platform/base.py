"""平台后端的公共接口与探测逻辑。

设计原则：任何原生能力都必须可降级。探测失败时退回 `NullBackend`，
应用仍能通过 Qt 自带剪贴板与轮询工作。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


class PayloadKind(str):
    """载荷类型常量（继承 str 便于直接参与集合与比较）。"""


TEXT = "text"
RICH = "rich"
IMAGE = "image"
FILE = "file"
LINK = "link"
COLOR = "color"


@dataclass
class FocusInfo:
    """当前前台窗口信息，用于来源标注与隐私过滤。"""

    app_id: str = ""
    app_name: str = ""
    window_title: str = ""
    process_id: int = 0

    @property
    def display(self) -> str:
        return self.app_name or self.app_id or "未知来源"

    @property
    def is_empty(self) -> bool:
        return not (self.app_id or self.app_name or self.window_title)


@dataclass
class CaptureResult:
    """一次剪贴板读取的原始结果。"""

    kind: str = TEXT
    text: str = ""
    html: str = ""
    rtf: str = ""
    image_png: bytes = b""
    image_hash: str = ""
    image_size: Tuple[int, int] = (0, 0)
    files: List[str] = field(default_factory=list)
    source: FocusInfo = field(default_factory=FocusInfo)
    mime_types: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.text or self.html or self.rtf or self.image_png or self.files)


class ClipboardBackend:
    """平台后端基类。

    子类只需覆盖自己支持的能力，其余走默认实现（即「不支持」）。
    """

    name = "base"
    #: 该后端是否需要在 UI 线程内轮询剪贴板
    requires_polling = True

    def is_available(self) -> bool:
        return True

    # ---- 剪贴板读取/写入 ----

    def read_primary_text(self) -> str:
        """读取「主选择区」文本（X11 的 PRIMARY selection）。"""
        return ""

    def read_windows(self, limit: int = 30) -> List[CaptureResult]:
        """读取系统剪贴板历史（仅 Windows 原生提供）。

        返回项按时间由新到旧排列。空列表表示不支持。
        """
        return []

    def is_history_supported(self) -> bool:
        return False

    # ---- 焦点与来源 ----

    def current_focus(self) -> FocusInfo:
        return FocusInfo()

    # ---- 提示解密工具：粘贴时还原 ----

    def set_transient_text(self, text: str, expire_ms: int = 0) -> bool:
        """写入会被系统自动清除的临时剪贴板内容。

        `expire_ms > 0` 时尽力让内容在指定时间后自动消失（安全粘贴）。
        返回 False 表示后端做不到，调用方需自行降级。
        """
        return False

    def is_transient_supported(self) -> bool:
        return False

    # ---- 辅助 ----

    def platform_label(self) -> str:
        if sys.platform.startswith("win"):
            return "Windows"
        if sys.platform == "darwin":
            return "macOS"
        if sys.platform.startswith("linux"):
            return "Linux"
        return sys.platform

    def capabilities(self) -> Dict[str, bool]:
        """能力矩阵，用于设置页展示与功能开关降级。"""
        return {
            "primary_selection": False,
            "native_history": self.is_history_supported(),
            "focus_detection": False,
            "transient_clipboard": self.is_transient_supported(),
            "wayland": False,
        }


class NullBackend(ClipboardBackend):
    """兜底后端：不做任何原生调用，全部能力由 Qt 层提供。"""

    name = "null"


def detect_backend() -> ClipboardBackend:
    """按当前平台挑选最合适的后端。"""
    try:
        if sys.platform.startswith("linux"):
            from .linux import LinuxBackend

            backend: ClipboardBackend = LinuxBackend()
        elif sys.platform == "darwin":
            from .macos import MacOSBackend

            backend = MacOSBackend()
        elif sys.platform.startswith("win"):
            from .windows import WindowsBackend

            backend = WindowsBackend()
        else:
            backend = NullBackend()
    except Exception:  # noqa: BLE001 - 原生依赖缺失时静默降级
        backend = NullBackend()

    if not backend.is_available():
        return NullBackend()
    return backend


def backends_for(platform: str) -> List[str]:
    """平台字符串 -> 后端模块名，供打包脚本与文档引用。"""
    mapping = {
        "win32": ["windows"],
        "darwin": ["macos"],
        "linux": ["linux"],
    }
    for prefix, names in mapping.items():
        if platform.startswith(prefix):
            return names
    return ["null"]
