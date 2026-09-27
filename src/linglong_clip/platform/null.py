"""无原生依赖的平台后端，用于测试与未知平台。"""

from .base import NullBackend

__all__ = ["NullBackend"]
