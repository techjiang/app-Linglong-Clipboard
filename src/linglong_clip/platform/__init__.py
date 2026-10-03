"""平台适配层：把窗口系统、焦点检测、剪贴板原生能力差异收敛到这里。"""

from .base import (
    ClipboardBackend,
    FocusInfo,
    NullBackend,
    PayloadKind,
    backends_for,
    detect_backend,
)

__all__ = [
    "ClipboardBackend",
    "FocusInfo",
    "NullBackend",
    "PayloadKind",
    "backends_for",
    "detect_backend",
]
