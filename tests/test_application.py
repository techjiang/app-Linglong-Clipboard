"""应用控制器：装配、采集路由、线程调度、退出。"""

from __future__ import annotations

import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from linglong_clip.core.models import ClipboardItem, content_hash  # noqa: E402
from linglong_clip.ui.application import GuiThreadInvoker  # noqa: E402


@pytest.fixture()
def app_instance(home, monkeypatch, qt_app):
    """每个用例一份隔离的 Application 实例。"""
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home))
    from linglong_clip.ui.application import Application

    instance = Application(["test"])
    yield instance
    if not instance._shutting_down:
        instance.monitor.stop()
        instance.hotkeys.unregister_all()
        instance.storage.close()


# ---- 线程调度器 ----

def test_invoker_runs_inline_on_gui_thread(qt_app):
    from PySide6.QtCore import QObject

    owner = QObject()
    invoker = GuiThreadInvoker(owner)
    assert invoker.call(lambda: 42) == 42


def test_invoker_returns_none_after_cancel(qt_app):
    from PySide6.QtCore import QObject
    invoker = GuiThreadInvoker(QObject())
    invoker.cancel()
    assert invoker.call(lambda: 42) is None


def test_invoker_propagates_exception(qt_app):
    from PySide6.QtCore import QObject
    invoker = GuiThreadInvoker(QObject())

    def boom():
        raise ValueError("expected")

    with pytest.raises(ValueError):
        invoker.call(boom)


def test_invoker_cross_thread_delivers_result():
    from PySide6.QtCore import QObject, QTimer

    app = QApplication.instance() or QApplication([])
    invoker = GuiThreadInvoker(QObject())

    box = {}

    def worker():
        box["value"] = invoker.call(lambda: "from-gui", True)

    thread = threading.Thread(target=worker)
    thread.start()
    for _ in range(40):
        app.processEvents()
        time.sleep(0.02)
        if not thread.is_alive():
            break
    thread.join(timeout=1)
    assert box.get("value") == "from-gui"


def test_invoker_non_blocking_from_gui_thread_runs_inline(qt_app):
    """已在 GUI 线程时无需投递，直接执行并返回结果。"""
    from PySide6.QtCore import QObject

    invoker = GuiThreadInvoker(QObject())
    assert invoker.call(lambda: "x", blocking=False) == "x"


# ---- 装配 ----

def test_application_wires_core_objects(app_instance):
    assert app_instance.storage is not None
    assert app_instance.guard is not None
    assert app_instance.automation is not None
    assert app_instance.window is not None
    assert app_instance.preview is not None
    assert app_instance.tray is not None


def test_application_quit_is_idempotent(app_instance):
    app_instance.monitor.start()
    app_instance.quit()
    app_instance.quit()  # 第二次不应抛异常
    assert app_instance._shutting_down is True


# ---- 采集路由 ----

def test_capture_persists_through_application(app_instance):
    # 先清掉剪贴板，避免上一个用例的内容泄漏进来
    app_instance.qt_app.clipboard().clear()
    app_instance.monitor.start()
    deadline = time.time() + 2
    while time.time() < deadline and app_instance.storage.count() == 0:
        app_instance.qt_app.processEvents()
        time.sleep(0.05)

    app_instance.qt_app.clipboard().setText("经由控制器采集的内容")
    deadline = time.time() + 3
    while time.time() < deadline and app_instance.storage.count() == 0:
        app_instance.qt_app.processEvents()
        time.sleep(0.05)

    assert app_instance.storage.count() >= 1
    contents = [item.content for item in app_instance.storage.list()]
    assert any("经由控制器采集的内容" in text for text in contents)


def test_sensitive_capture_is_blocked(app_instance):
    app_instance.qt_app.clipboard().clear()
    app_instance.monitor.start()
    deadline = time.time() + 2
    while time.time() < deadline:
        app_instance.qt_app.processEvents()
        time.sleep(0.05)

    app_instance.qt_app.clipboard().setText("password: hunter2xyz")
    deadline = time.time() + 2
    while time.time() < deadline:
        app_instance.qt_app.processEvents()
        time.sleep(0.05)

    contents = [item.content for item in app_instance.storage.list()]
    assert not any("hunter2xyz" in text for text in contents)


def test_capture_callback_after_shutdown_is_ignored(app_instance):
    app_instance.quit()
    app_instance._on_capture_threadsafe(ClipboardItem(kind="text", content="late"))
    app_instance._on_captured_ui(ClipboardItem(kind="text", content="late"), "captured")


# ---- 剪贴板操作 ----

def test_copy_item_marks_self_write(app_instance):
    item = app_instance.storage.add(
        ClipboardItem(kind="text", content="copy me", preview="copy me", hash="h1")
    )
    app_instance.copy_item(item.id)
    app_instance.qt_app.processEvents()

    assert app_instance.qt_app.clipboard().text() == "copy me"
    # 自己的写入不应被再次采集
    app_instance.monitor._poll_once()
    assert app_instance.storage.count() == 1


def test_copy_item_bumps_use_count(app_instance):
    item = app_instance.storage.add(
        ClipboardItem(kind="text", content="x", preview="x", hash="h2")
    )
    app_instance.copy_item(item.id)
    assert app_instance.storage.get(item.id).use_count == 1


def test_copy_missing_item_is_noop(app_instance):
    app_instance.copy_item(9999)  # 不应抛出


def test_copy_text_helper(app_instance):
    app_instance.copy_text("direct text")
    assert app_instance.qt_app.clipboard().text() == "direct text"


def test_copy_redacts_when_enabled(app_instance):
    app_instance.config.set("privacy.redact_on_copy_out", True)
    app_instance.guard.reload_rules()
    item = app_instance.storage.add(
        ClipboardItem(kind="text", content="password: hunter2xyz", preview="p",
                      hash="h3", sensitive=True)
    )
    app_instance.copy_item(item.id)
    assert "hunter2xyz" not in app_instance.qt_app.clipboard().text()


def test_delete_item(app_instance):
    item = app_instance.storage.add(
        ClipboardItem(kind="text", content="bye", preview="bye", hash="h4")
    )
    app_instance.delete_item(item.id)
    assert app_instance.storage.get(item.id) is None


def test_toggle_capture_pauses_and_resumes(app_instance):
    assert app_instance.monitor.is_paused is False
    app_instance.toggle_capture()
    assert app_instance.monitor.is_paused is True
    app_instance.toggle_capture()
    assert app_instance.monitor.is_paused is False


def test_quick_paste_overlay_shows(app_instance):
    app_instance.show_quick_paste()
    assert app_instance.preview.isVisible() is True


def test_transform_window_shown(app_instance):
    item = app_instance.storage.add(
        ClipboardItem(kind="code", content="def f():\n    return 1", preview="def f()",
                      hash="h5")
    )
    result = app_instance.automation.apply("lines.number", item)
    app_instance._show_transform(item, "lines.number", result.output)
    assert app_instance.transformer.isVisible() is True


# ---- 配置联动 ----

def test_poll_interval_change_applies(app_instance):
    app_instance._on_config_change("capture.poll_interval_ms", 500)
    assert app_instance.monitor._interval == 0.5


def test_capture_enabled_change_applies(app_instance):
    app_instance._on_config_change("capture.enabled", False)
    assert app_instance.monitor.is_paused is True
    app_instance._on_config_change("capture.enabled", True)
    assert app_instance.monitor.is_paused is False


def test_sync_enabled_without_folder_is_safe(app_instance):
    app_instance.config.set("sync.enabled", True)
    app_instance.run_sync()  # 无目录时应提示而不是崩


def test_sweep_sensitive_runs(app_instance):
    app_instance._sweep_sensitive()  # 无 TTL 时应为 no-op


def test_device_name_falls_back_to_hostname(app_instance):
    assert app_instance._device_name()


def test_sync_build_skipped_without_folder(app_instance):
    app_instance.config.set("sync.folder", "")
    assert app_instance._build_sync() is None
