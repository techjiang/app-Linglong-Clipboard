"""采集调度：基线、去重、暂停、自写入抑制。"""

import time

from linglong_clip.core.models import ClipboardItem, content_hash
from linglong_clip.core.monitor import ClipboardMonitor, FocusTracker
from linglong_clip.platform.base import FocusInfo


class FakeClipboard:
    """模拟 Qt 剪贴板适配器的 `read_clipboard`。"""

    def __init__(self):
        self.text = ""
        self.broken = False
        self.calls = 0

    def read_clipboard(self):
        self.calls += 1
        if self.broken:
            raise RuntimeError("clipboard busy")
        if not self.text:
            return None
        return ClipboardItem(
            kind="text", content=self.text, hash=content_hash("text", self.text)
        )


def test_prime_ignores_existing_content():
    clipboard = FakeClipboard()
    clipboard.text = "already here"
    captured = []
    monitor = ClipboardMonitor(clipboard, interval_ms=60, on_capture=captured.append)

    monitor._prime()
    monitor._poll_once()
    assert captured == []


def test_captures_new_content():
    clipboard = FakeClipboard()
    captured = []
    monitor = ClipboardMonitor(clipboard, interval_ms=60, on_capture=captured.append)

    monitor._prime()
    clipboard.text = "new content"
    monitor._poll_once()
    assert [item.content for item in captured] == ["new content"]


def test_same_content_not_captured_twice():
    clipboard = FakeClipboard()
    captured = []
    monitor = ClipboardMonitor(clipboard, interval_ms=60, on_capture=captured.append)

    monitor._prime()
    clipboard.text = "x"
    monitor._poll_once()
    monitor._poll_once()
    assert len(captured) == 1
    assert monitor.capture_count == 1


def test_mark_self_write_suppresses_capture():
    clipboard = FakeClipboard()
    captured = []
    monitor = ClipboardMonitor(clipboard, interval_ms=60, on_capture=captured.append)

    monitor._prime()
    clipboard.text = "written by us"
    monitor.mark_self_write("written by us")
    monitor._poll_once()
    assert captured == []


def test_paused_monitor_skips():
    clipboard = FakeClipboard()
    captured = []
    monitor = ClipboardMonitor(clipboard, interval_ms=60, on_capture=captured.append)

    monitor._prime()
    monitor.pause()
    clipboard.text = "while paused"
    monitor.on_native_event()
    assert captured == []


def test_resume_resets_baseline():
    clipboard = FakeClipboard()
    captured = []
    monitor = ClipboardMonitor(clipboard, interval_ms=60, on_capture=captured.append)

    monitor._prime()
    monitor.pause()
    monitor.resume()
    clipboard.text = "after resume"
    monitor._poll_once()
    assert [item.content for item in captured] == ["after resume"]


def test_empty_clipboard_is_ignored():
    clipboard = FakeClipboard()
    captured = []
    monitor = ClipboardMonitor(clipboard, interval_ms=60, on_capture=captured.append)
    monitor._prime()
    monitor._poll_once()
    assert captured == []


def test_read_failure_is_swallowed():
    clipboard = FakeClipboard()
    clipboard.text = "x"
    clipboard.broken = True
    monitor = ClipboardMonitor(clipboard, interval_ms=60, on_capture=lambda item: None)
    assert monitor.read_current() is None  # 不应抛出


def test_callback_exception_does_not_propagate():
    clipboard = FakeClipboard()
    monitor = ClipboardMonitor(
        clipboard, interval_ms=60, on_capture=lambda item: 1 / 0
    )
    monitor._prime()
    clipboard.text = "boom"
    monitor._poll_once()  # 不应抛出


def test_thread_lifecycle():
    clipboard = FakeClipboard()
    captured = []
    monitor = ClipboardMonitor(clipboard, interval_ms=60, on_capture=captured.append)

    assert monitor.is_running is False
    monitor.start()
    assert monitor.is_running is True

    time.sleep(0.1)
    clipboard.text = "from thread"
    time.sleep(0.35)
    monitor.stop()

    assert monitor.is_running is False
    assert [item.content for item in captured] == ["from thread"]


def test_gui_invoke_used_for_reads():
    """剪贴板读取必须走 gui_invoke（Qt 对象不能跨线程）。"""
    clipboard = FakeClipboard()
    clipboard.text = "x"
    calls = []

    def invoker(func, blocking=True):
        calls.append(blocking)
        return func()

    monitor = ClipboardMonitor(
        clipboard, interval_ms=60, on_capture=lambda item: None, gui_invoke=invoker
    )
    monitor.read_current()
    assert calls == [True]


def test_gui_invoke_failure_is_swallowed():
    def broken(func, blocking=True):
        raise RuntimeError("no gui thread")

    monitor = ClipboardMonitor(
        FakeClipboard(), interval_ms=60, on_capture=lambda item: None, gui_invoke=broken
    )
    assert monitor.read_current() is None


def test_set_interval():
    monitor = ClipboardMonitor(FakeClipboard(), interval_ms=500, on_capture=lambda i: None)
    monitor.set_interval(1000)
    assert monitor._interval == 1.0
    # 下限保护，避免把自己转成忙等
    monitor.set_interval(1)
    assert monitor._interval == 0.06


class FakeBackend:
    def __init__(self, focus=None, primary=""):
        self._focus = focus or FocusInfo()
        self._primary = primary
        self.calls = 0

    def current_focus(self):
        self.calls += 1
        return self._focus

    def read_primary_text(self):
        return self._primary


def test_focus_tracker_caches():
    clock = [100.0]
    backend = FakeBackend(FocusInfo(app_id="chrome"))
    tracker = FocusTracker(backend, interval=1.0, clock=lambda: clock[0])

    assert tracker.current().app_id == "chrome"
    tracker.current()
    assert backend.calls == 1  # 缓存命中

    clock[0] += 2.0
    tracker.current()
    assert backend.calls == 2


def test_focus_tracker_handles_backend_error():
    class Broken:
        def current_focus(self):
            raise RuntimeError("no x11")

    tracker = FocusTracker(Broken())
    assert tracker.current().is_empty is True


def test_read_primary_returns_item():
    backend = FakeBackend(primary="selected text")
    monitor = ClipboardMonitor(FakeClipboard(), interval_ms=60, backend=backend)
    item = monitor.read_primary()
    assert item is not None and item.content == "selected text"


def test_read_primary_skips_empty():
    monitor = ClipboardMonitor(FakeClipboard(), interval_ms=60, backend=FakeBackend())
    assert monitor.read_primary() is None
