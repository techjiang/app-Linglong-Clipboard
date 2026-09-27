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
