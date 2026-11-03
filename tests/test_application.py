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


# ---- 出站防护：复制敏感内容 ----


def _add_sensitive(app_instance, content="password: hunter2xyz", hash_="hs1"):
    return app_instance.storage.add(
        ClipboardItem(kind="text", content=content, preview="p",
                      hash=hash_, sensitive=True)
    )


def test_copy_sensitive_marks_platform_exclusion(app_instance, qt_app):
    """敏感内容复制出去时要挂上系统排除标记（本机没有则自然跳过）。

    这里不假设平台一定支持，而是比对「编排层认为该挂」与「实际有没有挂」：
    只要后端声明了格式名，剪贴板数据里就必须出现。
    """
    item = _add_sensitive(app_instance)
    app_instance.copy_item(item.id)

    formats = app_instance.qt_app.clipboard().mimeData().formats()
    expected = app_instance.backend.history_exclusion_formats()
    for name in expected:
        assert name in formats
    # 内容照常写出，标记不等于改写
    assert "hunter2xyz" in app_instance.qt_app.clipboard().text()


def test_copy_normal_item_has_no_exclusion_mark(app_instance):
    """普通内容不该被打标记 —— 系统历史正是用户找回它的地方。"""
    item = app_instance.storage.add(
        ClipboardItem(kind="text", content="plain", preview="plain", hash="hn1")
    )
    app_instance.copy_item(item.id)

    formats = app_instance.qt_app.clipboard().mimeData().formats()
    assert all(
        name not in formats
        for name in app_instance.backend.history_exclusion_formats()
    )


def test_copy_sensitive_schedules_clear(app_instance, monkeypatch):
    """配了自动清空就要真的排上，而不是只写进配置。"""
    app_instance.config.set("privacy.sensitive_auto_clear_seconds", 30)
    item = _add_sensitive(app_instance, hash_="hs2")

    scheduled = {}

    def _fake_schedule(text, seconds):
        scheduled["text"] = text
        scheduled["seconds"] = seconds
        return True

    monkeypatch.setattr(app_instance.releaser, "schedule", _fake_schedule)
    app_instance.copy_item(item.id)

    assert scheduled["seconds"] == 30
    assert scheduled["text"] == "password: hunter2xyz"


def test_copy_sensitive_without_clear_setting_does_not_schedule(app_instance, monkeypatch):
    app_instance.config.set("privacy.sensitive_auto_clear_seconds", 0)
    item = _add_sensitive(app_instance, hash_="hs3")

    calls = []
    monkeypatch.setattr(
        app_instance.releaser, "schedule", lambda *a, **k: calls.append(a) or True
    )
    app_instance.copy_item(item.id)
    assert calls == []


def test_copy_sensitive_below_floor_does_not_schedule(app_instance, monkeypatch):
    """低于下限按「不清空」处理 —— 粘贴还没发生内容就没了。"""
    app_instance.config.set("privacy.sensitive_auto_clear_seconds", 2)
    item = _add_sensitive(app_instance, hash_="hs4")

    calls = []
    monkeypatch.setattr(
        app_instance.releaser, "schedule", lambda *a, **k: calls.append(a) or True
    )
    app_instance.copy_item(item.id)
    assert calls == []


def test_copy_sensitive_redacted_still_marks_and_clears(app_instance, monkeypatch):
    """脱敏后仍要挂标记、仍要排清空：正则漏一处就漏一处，兜底不能省。"""
    app_instance.config.set("privacy.redact_on_copy_out", True)
    app_instance.config.set("privacy.sensitive_auto_clear_seconds", 30)
    app_instance.guard.reload_rules()
    item = _add_sensitive(app_instance, hash_="hs5")

    scheduled = {}
    monkeypatch.setattr(
        app_instance.releaser,
        "schedule",
        lambda text, seconds: scheduled.update(text=text, seconds=seconds) or True,
    )
    app_instance.copy_item(item.id)

    assert "hunter2xyz" not in app_instance.qt_app.clipboard().text()
    assert scheduled["seconds"] == 30
    assert "[已脱敏:密码字段]" in scheduled["text"]


def test_copy_sensitive_image_reports_no_clear(app_instance, monkeypatch):
    """图片走不通限时清空（读不到文本无从比对），要明说而不是静默。"""
    app_instance.config.set("privacy.sensitive_auto_clear_seconds", 30)
    app_instance.config.set("privacy.redact_on_copy_out", False)
    item = app_instance.storage.add(
        ClipboardItem(kind="image", content="", preview="img",
                      hash="hi1", sensitive=True)
    )

    calls = []
    monkeypatch.setattr(
        app_instance.releaser, "schedule", lambda *a, **k: calls.append(a) or True
    )
    notes = []
    monkeypatch.setattr(app_instance.window, "notify", lambda *a, **k: notes.append(a))
    app_instance.copy_item(item.id)

    assert calls == []
    assert any("图片" in str(note) for note in notes)


def test_copy_text_marks_sensitive_transform_output(app_instance, monkeypatch):
    """转换结果也可能是密钥（「提取 API Key」就产出它），同样要过出站判定。"""
    app_instance.config.set("privacy.sensitive_auto_clear_seconds", 30)
    scheduled = {}
    monkeypatch.setattr(
        app_instance.releaser,
        "schedule",
        lambda text, seconds: scheduled.update(text=text, seconds=seconds) or True,
    )
    app_instance.copy_text("sk-abcdefghijklmnopqrstuvwxyz012345")

    expected = app_instance.backend.history_exclusion_formats()
    formats = app_instance.qt_app.clipboard().mimeData().formats()
    for name in expected:
        assert name in formats
    assert scheduled["seconds"] == 30


def test_copy_text_normal_does_not_schedule(app_instance, monkeypatch):
    calls = []
    monkeypatch.setattr(
        app_instance.releaser, "schedule", lambda *a, **k: calls.append(a) or True
    )
    app_instance.copy_text("just some ordinary text")
    assert calls == []


def test_outbound_plan_uses_guard_config(app_instance):
    """自动清空时长取自隐私配置，改配置立刻生效。"""
    app_instance.config.set("privacy.sensitive_auto_clear_seconds", 77)
    assert app_instance._auto_clear_seconds() == 77
    app_instance.config.set("privacy.sensitive_auto_clear_seconds", 0)
    assert app_instance._auto_clear_seconds() == 0


def test_quit_cancels_pending_releases(app_instance, monkeypatch):
    """退出时要取消待清理任务，不能留个定时器在进程退出后还去动剪贴板。"""
    cancelled = []
    monkeypatch.setattr(
        app_instance.releaser, "cancel_all", lambda: cancelled.append(True)
    )
    app_instance.quit()
    assert cancelled == [True]


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


# ---- 历史存储装配 ----

def test_application_uses_sqlite_by_default(app_instance):
    from linglong_clip.core.storage import Storage

    assert isinstance(app_instance.storage, Storage)


def test_application_uses_json_backend_when_configured(home, monkeypatch, qt_app):
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home))
    from linglong_clip.core.repository import JsonRepository
    from linglong_clip.ui.application import Application
    from linglong_clip.utils.config import Config

    Config(home / "config.json").set("storage.backend", "json")
    instance = Application(["test"])
    try:
        assert isinstance(instance.storage, JsonRepository)
        assert instance.storage.path == home / ".clipboard" / "clipboard.json"
        # 图片目录跟着 JSON 走，采集出来的图片与历史文件同处一地
        assert instance.clipboard._images_dir == instance.storage.images_dir
    finally:
        instance.monitor.stop()
        instance.hotkeys.unregister_all()
        instance.storage.close()


def test_json_backend_creates_file_on_start(home, monkeypatch, qt_app):
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home))
    from linglong_clip.ui.application import Application
    from linglong_clip.utils.config import Config

    Config(home / "config.json").set("storage.backend", "json")
    instance = Application(["test"])
    try:
        assert (home / ".clipboard" / "clipboard.json").is_file()
    finally:
        instance.monitor.stop()
        instance.hotkeys.unregister_all()
        instance.storage.close()


def test_image_saved_is_inlined_for_json_backend(home, monkeypatch, qt_app):
    """JSON 后端下，图片落盘后应立刻以 data URI 内联进历史。"""
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home))
    from linglong_clip.ui.application import Application
    from linglong_clip.utils.config import Config

    Config(home / "config.json").set("storage.backend", "json")
    instance = Application(["test"])
    try:
        payload = b"\x89PNG fake image"
        instance._on_image_saved("images/abc.png", payload)
        assert instance.storage._images["images/abc.png"].startswith("data:image/png;base64,")
        assert instance.storage.read_image_bytes("images/abc.png") == payload
    finally:
        instance.monitor.stop()
        instance.hotkeys.unregister_all()
        instance.storage.close()


def test_image_inlining_can_be_disabled(home, monkeypatch, qt_app):
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home))
    from linglong_clip.ui.application import Application
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    config.set("storage.backend", "json")
    config.set("storage.json_inline_images", False)
    instance = Application(["test"])
    try:
        instance._on_image_saved("images/abc.png", b"data")
        assert "images/abc.png" not in instance.storage._images
    finally:
        instance.monitor.stop()
        instance.hotkeys.unregister_all()
        instance.storage.close()


def test_oversized_image_is_not_inlined(home, monkeypatch, qt_app):
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home))
    from linglong_clip.ui.application import Application
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    config.set("storage.backend", "json")
    config.set("storage.json_max_inline_image_kb", 1)
    instance = Application(["test"])
    try:
        instance._on_image_saved("images/big.png", b"x" * 4096)
        assert "images/big.png" not in instance.storage._images
    finally:
        instance.monitor.stop()
        instance.hotkeys.unregister_all()
        instance.storage.close()


def test_sqlite_backend_does_not_inline_images(app_instance):
    app_instance._on_image_saved("images/abc.png", b"data")
    assert not hasattr(app_instance.storage, "set_image_data")


# ---- 固定/收藏标记的写入路径 ----
# 界面只发意图（见 test_ui.py 的 flag_toggled 用例），真正写入在这里。
# 写入要顺带处理「固定是谁设的」，那是界面拿不到的信息。


def test_toggle_flag_pins_and_clears_origin(app_instance):
    item = app_instance.storage.add(
        ClipboardItem(kind="text", content="pin me", preview="pin me", hash="pf1")
    )
    # 先制造一个「对端设的固定」残留
    app_instance.storage.update_fields(item.id, pinned=True, metadata={"pinned_by": "peer"})
    assert app_instance.storage.get(item.id).pinned_by == "peer"

    # 本机用户取消固定：来源要一起清掉
    app_instance.toggle_flag(item.id, "pinned", False)
    assert app_instance.storage.get(item.id).pinned is False
    assert app_instance.storage.get(item.id).pinned_by == ""

    # 本机用户重新固定：来源为空（= 本机意图，永不被判为过期）
    app_instance.toggle_flag(item.id, "pinned", True)
    assert app_instance.storage.get(item.id).pinned is True
    assert app_instance.storage.get(item.id).pinned_by == ""


def test_toggle_favorite_does_not_touch_pinned_by(app_instance):
    item = app_instance.storage.add(
        ClipboardItem(kind="text", content="fav me", preview="fav me", hash="pf2")
    )
    app_instance.storage.update_fields(item.id, metadata={"pinned_by": "peer"})
    app_instance.toggle_flag(item.id, "favorite", True)
    stored = app_instance.storage.get(item.id)
    assert stored.favorite is True
    # 收藏与固定的来源是两码事，不该互相干扰
    assert stored.pinned_by == "peer"


def test_toggle_flag_ignores_unknown_field_and_missing_item(app_instance):
    item = app_instance.storage.add(
        ClipboardItem(kind="text", content="x", preview="x", hash="pf3")
    )
    app_instance.toggle_flag(item.id, "bogus", True)  # 不认识的字段，静默忽略
    assert app_instance.storage.get(item.id).pinned is False

    app_instance.toggle_flag(99999, "pinned", True)  # 不存在的记录，不抛异常


def test_toggle_flag_metadata_is_not_shared_between_items(app_instance):
    """元数据是 dict，改动要落在副本上，不能串到别的记录。"""
    one = app_instance.storage.add(
        ClipboardItem(kind="text", content="one", preview="one", hash="pf4")
    )
    two = app_instance.storage.add(
        ClipboardItem(kind="text", content="two", preview="two", hash="pf5")
    )
    app_instance.toggle_flag(one.id, "pinned", True)
    assert app_instance.storage.get(two.id).pinned is False


def test_window_flag_signal_is_wired_to_application(app_instance):
    """端到端：窗口发信号 → Application 写库。这条守着「信号有没有接上」。"""
    item = app_instance.storage.add(
        ClipboardItem(kind="text", content="wired", preview="wired", hash="pf6")
    )
    app_instance.window.reload()
    app_instance.window._select(item.id)
    app_instance.window._toggle_pin_selected()
    assert app_instance.storage.get(item.id).pinned is True
    assert app_instance.storage.get(item.id).pinned_by == ""


# ---- 清理未固定记录：必须记删除标记，否则别的设备上会复活 ----
# 界面上那个「清理未固定记录」按钮此前直接 storage.clear()，墓碑一条都不记。
# 表现是「在这台清了、别的设备上还在」，下一轮同步又原样回来 ——
# 与「删一条」的传播漏掉是同一个错误，只是入口不同。


def test_clear_unpinned_records_tombstones(app_instance):
    alive = app_instance.storage.add(
        ClipboardItem(kind="text", content="会被清掉", preview="会被清掉", hash="cl1")
    )
    kept = app_instance.storage.add(
        ClipboardItem(kind="text", content="固定项留着", preview="固定项留着", hash="cl2", pinned=True)
    )
    assert app_instance.clear_unpinned() is None
    assert app_instance.storage.get(alive.id) is None
    assert app_instance.storage.get(kept.id) is not None

    hashes = app_instance.tombstones.all_hashes()
    assert "cl1" in hashes
    # 固定项没消失，不能被算成删除，否则会误删别的设备上那份
    assert "cl2" not in hashes


def test_clear_unpinned_with_nothing_to_clear(app_instance):
    app_instance.clear_unpinned()  # 空库不该抛异常


def test_window_clear_request_is_wired(app_instance):
    """端到端：窗口发信号 → Application 清理并记墓碑。"""
    item = app_instance.storage.add(
        ClipboardItem(kind="text", content="清我", preview="清我", hash="cl3")
    )
    app_instance.window.clear_requested.emit()
    assert app_instance.storage.get(item.id) is None
    assert "cl3" in app_instance.tombstones.all_hashes()


def test_window_compact_request_is_wired(app_instance):
    app_instance.storage.add(
        ClipboardItem(kind="text", content="x", preview="x", hash="cl4")
    )
    app_instance.window.compact_requested.emit()  # 不抛异常即可


def test_window_clear_button_does_not_delete_before_confirmation(app_instance):
    """清理按钮先弹确认框；用户选「否」时不该有任何副作用。

    这条同时守着「界面不自己写库」：清理由 Application 在确认之后执行。
    """
    from PySide6.QtWidgets import QMessageBox

    item = app_instance.storage.add(
        ClipboardItem(kind="text", content="别删我", preview="别删我", hash="cl5")
    )
    original = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    try:
        app_instance.window._clear_unpinned()
    finally:
        QMessageBox.question = original
    assert app_instance.storage.get(item.id) is not None
    assert "cl5" not in app_instance.tombstones.all_hashes()


def test_toggle_flag_records_cancel_for_sync(app_instance, tmp_path):
    """取消固定/收藏也要记进变更通道 —— 否则同步层分不出「取消过」和「从未设过」。

    这正是「我在笔记本上取消了，另一台还是已收藏」的根因：
    只写 `pinned=False` 时，同步层拿到的仍然只是一个布尔值。
    """
    from linglong_clip.core.flag_changes import FLAG_PINNED, FlagChangeStore

    app_instance.flag_changes = FlagChangeStore(tmp_path / "flags.json")
    item = app_instance.storage.add(
        ClipboardItem(kind="text", content="cancel me", preview="cancel me", hash="fc1")
    )

    app_instance.toggle_flag(item.id, "pinned", True)
    assert app_instance.flag_changes.effective("fc1", FLAG_PINNED) is True

    app_instance.toggle_flag(item.id, "pinned", False)
    assert app_instance.flag_changes.effective("fc1", FLAG_PINNED) is False


def test_toggle_flag_records_favorite_separately(app_instance, tmp_path):
    from linglong_clip.core.flag_changes import FLAG_FAVORITE, FlagChangeStore

    app_instance.flag_changes = FlagChangeStore(tmp_path / "flags.json")
    item = app_instance.storage.add(
        ClipboardItem(kind="text", content="fav", preview="fav", hash="fc2")
    )
    app_instance.toggle_flag(item.id, "favorite", True)
    assert app_instance.flag_changes.effective("fc2", FLAG_FAVORITE) is True


def test_record_flag_change_skips_item_without_fingerprint(app_instance, tmp_path):
    """没有指纹时不该记事件 —— 它永远匹配不上对端的任何记录。

    `storage.add` 会按内容补算指纹，所以这里直接传一个没有载荷的记录，
    验证守卫本身（它是防御性的，不是常见路径）。
    """
    from linglong_clip.core.flag_changes import FlagChangeStore

    app_instance.flag_changes = FlagChangeStore(tmp_path / "flags.json")
    blank = ClipboardItem(kind="text", content="", preview="", hash="")
    app_instance._record_flag_change(blank, "pinned", True)
    assert app_instance.flag_changes.all_changes() == []


def test_toggle_flag_survives_flag_change_write_failure(app_instance, monkeypatch):
    """变更记录写不进去只告警 —— 用户在这台机器上的操作不该因此失败。"""
    from linglong_clip.core.flag_changes import FlagChangeStore

    app_instance.flag_changes = FlagChangeStore()
    monkeypatch.setattr(
        FlagChangeStore,
        "record",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("磁盘满")),
    )
    item = app_instance.storage.add(
        ClipboardItem(kind="text", content="boom", preview="boom", hash="fc3")
    )
    app_instance.toggle_flag(item.id, "pinned", True)
    assert app_instance.storage.get(item.id).pinned is True


# ---- 出站规则：复制/粘贴出去时加工 ----


def _outbound_rule(name="去跟踪参数", trigger="outbound"):
    return {
        "name": name,
        "kinds": ["link"],
        "contains": "utm_",
        "actions": ["url.strip_tracking"],
        "trigger": trigger,
    }


def _add_link(app_instance, content="https://a.com/p?utm_source=x&id=1", hash_="ol1"):
    # 剪贴板是整个测试会话共用的（Qt 只允许一个 QApplication），
    # 上一个用例留下的内容会让「复制后剪贴板里是什么」变成假绿
    app_instance.qt_app.clipboard().clear()
    return app_instance.storage.add(
        ClipboardItem(kind="link", content=content, preview=content, hash=hash_)
    )


def _load_rules(app_instance, rules):
    app_instance.config.set("automation.workflows", rules, persist=False)
    app_instance.automation.load_from_config(rules)


def test_outbound_rule_rewrites_clipboard(app_instance):
    """复制前清理跟踪参数：界面配了规则，剪贴板里就必须是清理后的内容。"""
    _load_rules(app_instance, [_outbound_rule()])
    item = _add_link(app_instance)

    note = app_instance.apply_outbound_rules(item.id, False)

    assert app_instance.qt_app.clipboard().text() == "https://a.com/p?id=1"
    assert "去跟踪参数" in note


def test_outbound_rule_does_not_touch_history(app_instance):
    """加工的产物只进剪贴板：历史里留下的必须还是用户原本复制的那条。"""
    _load_rules(app_instance, [_outbound_rule()])
    item = _add_link(app_instance)

    app_instance.apply_outbound_rules(item.id, False)

    assert app_instance.storage.get(item.id).content == "https://a.com/p?utm_source=x&id=1"
    assert app_instance.storage.count() == 1


def test_outbound_rule_output_is_marked_as_self_write(app_instance):
    """加工产物不能被采集线程当成新内容记一遍。

    这里比对的是「标记的指纹」与「读取侧算出的指纹」—— 两边口径差一个字节，
    历史里就会凭空多出一条内容不同、来源不明的记录。
    """
    _load_rules(app_instance, [_outbound_rule()])
    item = _add_link(app_instance)
    app_instance.apply_outbound_rules(item.id, False)

    read = app_instance.clipboard.read_clipboard()
    assert read is not None
    assert app_instance.monitor._last_hash == read.hash

    app_instance.monitor._poll_once()
    assert app_instance.storage.count() == 1


def test_outbound_mark_uses_text_kind_like_reader(app_instance):
    """记录类型是 link，写进剪贴板的却是纯文本 —— 指纹必须按 text 算。

    这正是此前自我写入标记长期失效的原因：写的时候用记录自己的类型，
    读的时候按 text，两边永远对不上。
    """
    item = _add_link(app_instance)
    app_instance.qt_app.clipboard().setText(item.content)
    app_instance._mark_own_write(item, item.content)

    read = app_instance.clipboard.read_clipboard()
    assert read is not None
    assert app_instance.monitor._last_hash == read.hash


def test_no_outbound_rule_keeps_clipboard_unchanged(app_instance):
    item = _add_link(app_instance, content="https://a.com/clean", hash_="ol2")
    app_instance.copy_item(item.id)
    assert app_instance.apply_outbound_rules(item.id, False) == ""
    assert app_instance.qt_app.clipboard().text() == "https://a.com/clean"


def test_auto_rule_does_not_apply_on_copy(app_instance):
    """采集规则写的产物已经进历史了，复制时不该再加工一遍。"""
    _load_rules(app_instance, [_outbound_rule(trigger="auto")])
    item = _add_link(app_instance)
    app_instance.copy_item(item.id)

    assert app_instance.apply_outbound_rules(item.id, False) == ""
    assert app_instance.qt_app.clipboard().text() == "https://a.com/p?utm_source=x&id=1"


def test_outbound_rule_failure_still_copies(app_instance):
    """规则没跑成也要照原样复制 —— 点「复制」什么都没发生比不加工严重得多。"""
    _load_rules(
        app_instance,
        [{"name": "压缩 JSON", "kinds": ["text"], "actions": ["json.minify"],
          "trigger": "outbound"}],
    )
    app_instance.qt_app.clipboard().clear()
    item = app_instance.storage.add(
        ClipboardItem(kind="text", content="不是 JSON", preview="p", hash="oj1")
    )
    app_instance.copy_item(item.id)

    note = app_instance.apply_outbound_rules(item.id, False)

    assert app_instance.qt_app.clipboard().text() == "不是 JSON"
    assert "未生效" in note


def test_outbound_rules_ignore_missing_item(app_instance):
    assert app_instance.apply_outbound_rules(9999, False) == ""


def test_window_copy_paths_all_route_through_outbound(app_instance):
    """四条复制路径必须都发同一个出站信号。

    右键菜单、双击、回车/Ctrl+C、Ctrl+1..9 此前各写一遍，任何一条漏掉都会
    变成「同样的操作，有时加工了有时没加工」。
    """
    received = []
    app_instance.window.outbound_requested.connect(
        lambda item_id, plain: received.append((item_id, plain))
    )
    item = _add_link(app_instance, hash_="ol9")
    app_instance.window.reload()
    app_instance.window._select(item.id)

    app_instance.window._emit_copy(item.id, plain_text_only=True)
    app_instance.window._emit_paste(item.id)

    assert received == [(item.id, True), (item.id, False)]


def test_paste_item_applies_outbound_rule(app_instance, monkeypatch):
    """「直接粘贴到上次的窗口」也要走规则 —— 它同样是出站。"""
    _load_rules(app_instance, [_outbound_rule()])
    item = _add_link(app_instance, hash_="ol10")
    monkeypatch.setattr(app_instance, "_paste_to_previous_window", lambda: True)

    app_instance.apply_outbound_rules(item.id, False)

    assert app_instance.qt_app.clipboard().text() == "https://a.com/p?id=1"


def test_mark_own_write_takes_payload_not_hash(app_instance):
    """`mark_self_write()` 收的是载荷，不是算好的指纹。

    它内部会拿载荷拼上类型再哈希一次。这里若传了指纹，就会被哈希两遍，
    标记同样失效 —— 而现象与「口径不一致」一模一样（历史里多一条自己
    刚复制的内容），排查时极易走进岔路，所以专门守一条。
    """
    from linglong_clip.ui.clipboard_qt import clipboard_payload

    item = _add_link(app_instance)
    app_instance.qt_app.clipboard().setText(item.content)
    app_instance._mark_own_write(item, item.content)

    import hashlib

    expected = hashlib.blake2b(digest_size=16)
    expected.update("text".encode("utf-8"))
    expected.update(b"\x00")
    expected.update(clipboard_payload(item.content, "").encode("utf-8", "replace"))

    assert app_instance.monitor._last_hash == expected.hexdigest()


def test_clipboard_payload_prefers_text_over_html():
    """有纯文本时用纯文本，没有才退回 HTML —— 与读取侧完全一致。"""
    from linglong_clip.ui.clipboard_qt import clipboard_payload

    assert clipboard_payload("plain", "<b>rich</b>") == "plain"
    assert clipboard_payload("", "<b>rich</b>") == "<b>rich</b>"
    assert clipboard_payload("", "") == ""


def test_quit_stops_window_timers_before_closing_storage(app_instance):
    """退出时先停窗口定时器、再关存储，顺序不能反。

    搜索防抖定时器可能已经排上队；它落在已关闭的存储上时会从 Qt 事件循环里
    抛 `sqlite3.ProgrammingError`，而 Qt 只会打一行
    `Exceptions caught in Qt event loop`，追不到源头。
    """
    window = app_instance.window
    window._search.setText("会议纪要")
    assert window._search_timer.isActive()

    app_instance.quit()

    assert not window._search_timer.isActive()
    assert app_instance.storage.closed is True


def test_quit_does_not_leave_window_signals_connected(app_instance):
    """退出后窗口信号应已断开，避免死后还有人往已关的库上写。"""
    window = app_instance.window
    assert window._connected_external  # 装配后确实连过

    app_instance.quit()

    assert window._connected_external == []
    window.reload()  # 不该抛异常
    assert window._items == []


# ---- 检索快照的失效 ----
#
# 快照是「全库视图」的缓存，它没被丢掉的表现是**新记录搜不到** ——
# 不报错、不崩溃，只是搜索结果里少了刚复制的那条。所以每次写入路径
# 都要验一遍「失效真的发生了」，而不是只验「某个方法被调用过」。


def test_search_index_invalidated_after_capture(app_instance):
    app_instance.monitor.start()
    app_instance.search_index.snapshot()
    assert app_instance.search_index.loaded

    app_instance._on_captured_ui(
        ClipboardItem(id=1, kind="text", content="新内容", hash="h"), "captured"
    )
    assert not app_instance.search_index.loaded


def test_search_index_invalidated_after_delete(app_instance):
    item = app_instance.storage.add(
        ClipboardItem(kind="text", content="待删", preview="待删", hash="hd")
    )
    app_instance.search_index.snapshot()
    app_instance.delete_item(item.id)
    assert not app_instance.search_index.loaded


def test_search_index_invalidated_after_clear(app_instance):
    app_instance.storage.add(
        ClipboardItem(kind="text", content="待清", preview="待清", hash="hc")
    )
    app_instance.search_index.snapshot()
    app_instance.clear_unpinned()
    assert not app_instance.search_index.loaded


def test_freshly_captured_item_is_searchable(app_instance):
    """端到端：刚采到的内容立刻能被搜到。

    这是快照失效没做对时唯一会露出来的地方 —— 列表里 `reload()` 看得见它，
    但搜索走的是另一条路径，两者用的是不同的数据来源。
    """
    app_instance.storage.add(
        ClipboardItem(kind="text", content="可被检索的内容", preview="可被检索", hash="hs")
    )
    app_instance._invalidate_search_index()
    assert app_instance.engine.search("可被检索")


def test_stale_search_index_would_hide_new_item(app_instance):
    """不失效就会漏 —— 用例把「为什么要有失效」钉住。

    注意这里断言的是**候选集**而不是最终结果：后端那一刀（`storage.search`）
    会直接查库，所以最终结果是对的；一旦后端改成只走全库快照
    （缓存那条路），或者候选被先按时间收窄，这条就会漏。
    """
    app_instance.search_index.snapshot()  # 先建一份空快照
    app_instance.storage.add(
        ClipboardItem(kind="text", content="迟到的内容", preview="迟到", hash="hl")
    )
    assert app_instance.search_index.snapshot() == []

    app_instance._invalidate_search_index()
    assert [item.hash for item in app_instance.search_index.snapshot()] == ["hl"]


# ---- 拦截敏感内容要有回执（回归）----

def test_sensitive_capture_tells_user_it_was_blocked(app_instance):
    app_instance._on_captured_ui(
        ClipboardItem(kind="sensitive", content="", sensitive=True), "skipped_privacy"
    )
    assert "拦截" in app_instance.window._notice_label.text()


def test_blocked_capture_does_not_reload_list(app_instance, monkeypatch):
    calls = []
    monkeypatch.setattr(app_instance.window, "reload", lambda: calls.append(1))
    app_instance._on_captured_ui(
        ClipboardItem(kind="sensitive", content="", sensitive=True), "skipped_privacy"
    )
    assert calls == []
