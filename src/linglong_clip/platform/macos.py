"""macOS 后端。

用 osascript/pbpaste 探测前台应用（不依赖 pyobjc，降低打包体积）；
剪贴板读写在 Qt 层完成，这里只补充「来源应用」与「安全粘贴」能力。
"""

from __future__ import annotations

import os
import subprocess
from typing import Dict

from .base import ClipboardBackend, FocusInfo

_TIMEOUT = 2.0


def _osascript(script: str) -> str:
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", "replace").strip()


class MacOSBackend(ClipboardBackend):
    name = "macos"

    def capabilities(self) -> Dict[str, bool]:
        return {
            "primary_selection": False,
            "native_history": False,
            "focus_detection": True,
            "transient_clipboard": True,
            "wayland": False,
        }

    def current_focus(self) -> FocusInfo:
        bundle_id = _osascript(
            'tell application "System Events" to get bundle identifier of '
            "first application process whose frontmost is true"
        )
        app_name = _osascript(
            'tell application "System Events" to get name of '
            "first application process whose frontmost is true"
        )
        if not app_name and not bundle_id:
            return FocusInfo()
        return FocusInfo(app_id=bundle_id, app_name=app_name or bundle_id)

    def is_transient_supported(self) -> bool:
        return True
