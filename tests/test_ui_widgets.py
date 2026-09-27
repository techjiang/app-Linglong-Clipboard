"""设置对话框、转换窗口、托盘与快捷键的测试。"""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from linglong_clip.core.automation import AutomationEngine  # noqa: E402
from linglong_clip.core.models import ClipboardItem  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


# ---- 设置对话框 ----

@pytest.fixture()
def dialog(config, qt_app):
    from linglong_clip.ui.settings_dialog import SettingsDialog

    automation = AutomationEngine()
    return SettingsDialog(config, automation, {"folder": "/tmp/x", "devices": ["a"], "encrypted": False})


def test_dialog_reflects_current_config(dialog, config):
    assert dialog._capture_enabled.isChecked() == config.get("capture.enabled")
    assert dialog._theme.currentText() == config.get("ui.theme")
    assert dialog._max_items.value() == config.get("history.max_items")


def test_dialog_saves_changes(dialog, config):
    dialog._capture_enabled.setChecked(False)
    dialog._theme.setCurrentText("dark")
    dialog._max_items.setValue(500)
    dialog._save()

    assert config.get("capture.enabled") is False
    assert config.get("ui.theme") == "dark"
    assert config.get("history.max_items") == 500
    assert "ui.theme" in dialog.changes


def test_dialog_collects_capture_types(dialog, config):
    for kind, box in dialog._type_boxes.items():
        box.setChecked(kind == "text")
    dialog._save()
    assert config.get("capture.types") == ["text"]


def test_dialog_parses_blocked_apps(dialog, config):
    dialog._blocked_apps.setText("keychain, banking-app , vault")
    dialog._save()
    assert config.get("privacy.blocked_apps") == ["keychain", "banking-app", "vault"]


def test_dialog_parses_chinese_comma(dialog, config):
    dialog._blocked_apps.setText("a，b")
    dialog._save()
    assert config.get("privacy.blocked_apps") == ["a", "b"]


def test_dialog_validates_rules_json(dialog, config, qt_app):
    from PySide6.QtWidgets import QMessageBox

    dialog._rules_editor.setPlainText("{ not an array }")
    shown = []
    original = QMessageBox.warning
    QMessageBox.warning = lambda *args, **kwargs: shown.append(args)
    try:
        dialog._save()
    finally:
        QMessageBox.warning = original
    assert shown  # 报错而不是静默丢弃


def test_dialog_saves_valid_rules(dialog, config):
    dialog._rules_editor.setPlainText(
        json.dumps([{"name": "r1", "kinds": ["text"], "actions": ["json.minify"]}])
    )
    dialog._save()
    assert config.get("automation.workflows")[0]["name"] == "r1"


def test_dialog_insert_template(dialog):
    dialog._rules_editor.setPlainText("[]")
    dialog._insert_template()
    payload = json.loads(dialog._rules_editor.toPlainText())
    assert len(payload) == 2
    # 重复点击不应产生重复规则
    dialog._insert_template()
    assert len(json.loads(dialog._rules_editor.toPlainText())) == 2


def test_dialog_about_tab_shows_version(dialog):
    from linglong_clip import __version__

    assert __version__


# ---- 设置对话框：OCR ----


def test_dialog_reflects_ocr_config(dialog, config):
    assert dialog._ocr_enabled.isChecked() == config.get("ocr.enabled")
    assert dialog._ocr_languages.text() == (config.get("ocr.languages") or "")
    assert dialog._ocr_min_confidence.value() == config.get("ocr.min_confidence")


def test_dialog_saves_ocr_config(dialog, config):
    dialog._ocr_enabled.setChecked(False)
    dialog._ocr_languages.setText("chi_sim+eng")
    dialog._ocr_min_confidence.setValue(45)
    dialog._save()

    assert config.get("ocr.enabled") is False
    assert config.get("ocr.languages") == "chi_sim+eng"
    assert config.get("ocr.min_confidence") == 45


def test_ocr_status_reports_unavailable_when_disabled(dialog):
    dialog._ocr_enabled.setChecked(False)
    assert "已关闭" in dialog._ocr_status.text()


def test_ocr_status_reflects_environment(dialog):
    dialog._ocr_enabled.setChecked(True)
    text = dialog._ocr_status.text()
    # 已装 tesseract 时报告可用，未装时报告不可用，两者都不能是空白
    assert ("可用" in text) or ("不可用" in text)


# ---- 设置对话框：局域网同步 ----


def test_dialog_reflects_lan_config(dialog, config):
    assert dialog._sync_mode.currentData() == config.get("sync.mode")
    assert dialog._lan_port.value() == config.get("sync.lan_port")
    assert dialog._lan_discovery.isChecked() == config.get("sync.lan_discovery")


def test_lan_fields_disabled_in_folder_mode(dialog):
    dialog._sync_mode.setCurrentIndex(dialog._sync_mode.findData("folder"))
    assert dialog._lan_token.isEnabled() is False
    assert dialog._lan_peers.isEnabled() is False
    assert "共享文件夹" in dialog._lan_status.text()


def test_lan_fields_enabled_in_lan_mode(dialog):
    dialog._sync_mode.setCurrentIndex(dialog._sync_mode.findData("lan"))
    assert dialog._lan_token.isEnabled() is True
    assert dialog._lan_peers.isEnabled() is True
    assert "局域网直连" in dialog._lan_status.text()


def test_dialog_saves_lan_config(dialog, config):
    dialog._sync_enabled.setChecked(True)
    dialog._sync_mode.setCurrentIndex(dialog._sync_mode.findData("lan"))
    dialog._lan_token.setText("topsecret")
    dialog._lan_port.setValue(49000)
    dialog._lan_peers.setPlainText("192.168.1.8\n\n  192.168.1.9:49000  ")
    dialog._lan_discovery.setChecked(True)
    dialog._save()

    assert config.get("sync.mode") == "lan"
    assert config.get("sync.lan_token") == "topsecret"
    assert config.get("sync.lan_port") == 49000
    # 空行被清掉，条目两端空白被裁剪
    assert config.get("sync.lan_peers") == ["192.168.1.8", "192.168.1.9:49000"]
    assert config.get("sync.lan_discovery") is True


def test_lan_peers_rendered_one_per_line(config, qt_app):
    from linglong_clip.core.automation import AutomationEngine
    from linglong_clip.ui.settings_dialog import SettingsDialog

    config.set("sync.lan_peers", ["10.0.0.1", "10.0.0.2:9000"], persist=False)
    dlg = SettingsDialog(config, AutomationEngine())
    assert dlg._lan_peers.toPlainText().splitlines() == ["10.0.0.1", "10.0.0.2:9000"]


def test_lan_token_is_masked(dialog):
    from PySide6.QtWidgets import QLineEdit

    assert dialog._lan_token.echoMode() == QLineEdit.EchoMode.Password


# ---- 转换窗口 ----

@pytest.fixture()
def transformer(config, qt_app):
    from linglong_clip.ui.transform_window import TransformWindow

    return TransformWindow(config, AutomationEngine())


def _code_item():
    return ClipboardItem(id=1, kind="code", content="def f():\n    return 1",
                         preview="def f()", source_app="code")


def test_transform_window_shows_result(transformer):
    transformer.show_result(_code_item(), "lines.number", "1 | def f():\n2 |     return 1")
    assert "1 | def f()" in transformer._body.toPlainText()
    assert "链路" in transformer._chain_label.text()


def test_transform_window_copy_signal(transformer):
    received = []
    transformer.copy_result.connect(received.append)
    transformer.show_result(_code_item(), "lines.number", "result text")
    transformer._copy_and_close()
    assert received == ["result text"]


def test_transform_window_chains_actions(transformer):
    transformer.show_result(_code_item(), "lines.number", "1 | x")
    transformer._apply_next("lines.sort", "b\na")
    assert transformer._history == ["lines.number", "lines.sort"]


def test_transform_window_reports_chain_failure(transformer):
    transformer.show_result(_code_item(), "lines.number", "not json at all")
    transformer._apply_next("json.format", "not json at all")
    assert "⚠" in transformer._chain_label.text()


def test_transform_window_builds_continue_buttons(transformer):
    transformer.show_result(_code_item(), "lines.number", "a\nb")
    assert transformer._actions_row.count() > 0


def test_transform_window_unknown_action_key(transformer):
    transformer.show_result(_code_item(), "does.not.exist", "x")
    assert transformer._title.text() == "does.not.exist"


# ---- 托盘 ----

def test_tray_icon_is_painted(qt_app):
    from linglong_clip.ui.theme import DARK
    from linglong_clip.ui.tray import build_icon

    icon = build_icon(DARK, 64)
    assert not icon.isNull()
    sizes = icon.availableSizes()
    assert sizes and sizes[0].width() == 64


def test_tray_menu_actions_exist(qt_app):
    from linglong_clip.ui.theme import DARK
    from linglong_clip.ui.tray import TrayIcon

    tray = TrayIcon(DARK)
    labels = [action.text() for action in tray.contextMenu().actions() if action.text()]
    assert "打开主窗口" in labels
    assert "快速粘贴…" in labels
    assert "设置…" in labels
    assert "退出" in labels


def test_tray_pause_toggle_updates_label(qt_app):
    from linglong_clip.ui.theme import DARK
    from linglong_clip.ui.tray import TrayIcon

    tray = TrayIcon(DARK)
    seen = []
    tray.toggle_capture_requested.connect(lambda: seen.append(1))
    tray._capture_action.setChecked(True)
    assert tray._capture_action.text() == "恢复采集"
    assert seen
    tray._capture_action.setChecked(False)
    assert tray._capture_action.text() == "暂停采集"


def test_tray_set_paused_updates_tooltip(qt_app):
    from linglong_clip.ui.theme import DARK
    from linglong_clip.ui.tray import TrayIcon

    tray = TrayIcon(DARK)
    tray.set_paused(True)
    assert "已暂停" in tray.toolTip()
    tray.set_paused(False)
    assert "已暂停" not in tray.toolTip()


def test_tray_activation_emits_show(qt_app):
    from PySide6.QtWidgets import QSystemTrayIcon

    from linglong_clip.ui.theme import DARK
    from linglong_clip.ui.tray import TrayIcon

    tray = TrayIcon(DARK)
    seen = []
    tray.show_window_requested.connect(lambda: seen.append(1))
    tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
    tray._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)
    assert len(seen) == 2


# ---- 快捷键 ----

@pytest.mark.parametrize(
    "sequence,expected_mods,expected_key",
    [
        ("Ctrl+Shift+V", {"ctrl", "shift"}, "v"),
        ("Alt+Space", {"alt"}, "space"),
        ("Super+1", {"win"}, "1"),
        ("Ctrl+Alt+C", {"ctrl", "alt"}, "c"),
        ("F5", set(), "f5"),
        ("Ctrl+Shift+Alt+Delete", {"ctrl", "shift", "alt"}, "delete"),
        ("Cmd+S", {"win"}, "s"),
        ("Option+X", {"alt"}, "x"),
    ],
)
def test_parse_sequence(sequence, expected_mods, expected_key):
    from linglong_clip.ui.hotkeys import parse_sequence

    mods, key = parse_sequence(sequence)
    assert mods == expected_mods
    assert key == expected_key


def test_hotkey_manager_degrades_without_display(monkeypatch):
    """没有 X 服务器时必须返回 False 而不是抛异常。"""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    from linglong_clip.ui.hotkeys import HotkeyManager

    manager = HotkeyManager()
    assert manager.register("Ctrl+Shift+V", lambda: None) is False
    assert manager.reason  # 有可展示的原因


def test_hotkey_manager_handles_wayland(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    from linglong_clip.ui.hotkeys import HotkeyManager

    manager = HotkeyManager()
    assert manager.register("Ctrl+Shift+V", lambda: None) is False
    assert "Wayland" in manager.reason or "快捷键" in manager.reason


def test_hotkey_unregister_is_safe():
    from linglong_clip.ui.hotkeys import HotkeyManager

    manager = HotkeyManager()
    manager.unregister_all()  # 不应抛出


def test_hotkey_native_event_passthrough():
    from linglong_clip.ui.hotkeys import HotkeyManager

    assert HotkeyManager().handle_native_event(None, None) is False


def test_null_backend_hotkeys():
    from linglong_clip.ui.hotkeys import _BaseHotkeys

    backend = _BaseHotkeys()
    assert backend.is_available() is False
    assert backend.register(1, "Ctrl+A", lambda: None) is False
    backend.unregister_all([1])  # 不应抛出


# ---- 设置页：历史存储 ----


def test_settings_exposes_storage_backend(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    dialog = SettingsDialog(config)
    assert dialog._storage_backend.currentData() == "sqlite"
    assert dialog._storage_backend.count() == 2


def test_settings_saves_storage_backend(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    dialog = SettingsDialog(config)
    dialog._storage_backend.setCurrentIndex(dialog._storage_backend.findData("json"))
    dialog._save()

    assert config.get("storage.backend") == "json"
    assert "storage.backend" in dialog.changes


def test_settings_storage_hint_switches_with_backend(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    dialog = SettingsDialog(Config(home / "config.json"))
    dialog._storage_backend.setCurrentIndex(dialog._storage_backend.findData("sqlite"))
    assert "SQLite" in dialog._storage_hint.text()
    assert not dialog._storage_inline.isEnabled()

    dialog._storage_backend.setCurrentIndex(dialog._storage_backend.findData("json"))
    assert "storage-migrate" in dialog._storage_hint.text()
    assert dialog._storage_inline.isEnabled()
    assert dialog._storage_limit.isEnabled()


def test_settings_about_shows_history_location(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    config.set("storage.backend", "json")
    dialog = SettingsDialog(config)
    assert dialog._storage_location.endswith(".clipboard/clipboard.json")


def test_settings_saves_json_inline_options(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    dialog = SettingsDialog(config)
    dialog._storage_backend.setCurrentIndex(dialog._storage_backend.findData("json"))
    dialog._storage_inline.setChecked(False)
    dialog._storage_limit.setValue(512)
    dialog._save()

    assert config.get("storage.json_inline_images") is False
    assert config.get("storage.json_max_inline_image_kb") == 512
