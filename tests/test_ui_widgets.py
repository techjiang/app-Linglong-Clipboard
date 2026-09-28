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


class _FakeTesseract:
    def __init__(self, langs):
        self._langs = langs

    def install(self, monkeypatch):
        import linglong_clip.core.ocr as ocr

        langs = self._langs

        class Backend(ocr.TesseractBackend):
            def path(self):
                return "/usr/bin/tesseract"

            def languages(self):
                return langs

        monkeypatch.setattr(ocr, "TesseractBackend", Backend)
        return self


def test_ocr_status_shows_actual_language(dialog, config, monkeypatch):
    """状态里必须出现「实际会用哪个语言」，而不只是已安装列表。

    此前只显示「可用 · 已安装语言：chi_sim、eng」，用户从界面上看不出
    实际请求的是另一个没装的串。
    """
    _FakeTesseract(("chi_sim", "eng")).install(monkeypatch)
    dialog._ocr_enabled.setChecked(True)
    dialog._refresh_ocr_status()
    text = dialog._ocr_status.text()
    assert "chi_sim" in text
    assert "识别语言" in text or "实际使用" in text


def test_ocr_status_warns_on_degraded_language(dialog, config, monkeypatch):
    """核心回归：期望语言没装时要显式警告，而不是报「可用」。"""
    _FakeTesseract(("chi_sim", "eng")).install(monkeypatch)
    dialog._ocr_enabled.setChecked(True)
    dialog._refresh_ocr_status()

    dialog._ocr_languages.setText("jpn")
    text = dialog._ocr_status.text()
    assert "⚠" in text
    assert "jpn" in text
    assert "eng" in text


def test_ocr_status_recomputes_on_language_edit(dialog, config, monkeypatch):
    """手填语言后立刻重算：不然用户改完要等保存才看得出没这个语言。"""
    _FakeTesseract(("chi_sim", "eng")).install(monkeypatch)
    dialog._ocr_enabled.setChecked(True)
    dialog._refresh_ocr_status()

    dialog._ocr_languages.setText("jpn")
    assert "⚠" in dialog._ocr_status.text()

    dialog._ocr_languages.setText("chi_sim")
    assert "⚠" not in dialog._ocr_status.text()


def test_ocr_status_reports_no_language_data(dialog, config, monkeypatch):
    _FakeTesseract(("osd",)).install(monkeypatch)
    dialog._ocr_enabled.setChecked(True)  # 已是 True，需显式触发刷新
    dialog._refresh_ocr_status()
    text = dialog._ocr_status.text()
    assert "不可用" in text
    assert "语言" in text


def test_ocr_status_survives_probe_failure(dialog, config, monkeypatch):
    """探测失败不能把设置页搞崩。"""
    import linglong_clip.core.ocr as ocr

    def boom(*_args, **_kwargs):
        raise RuntimeError("探测炸了")

    monkeypatch.setattr(ocr, "TesseractBackend", boom)
    dialog._ocr_enabled.setChecked(True)
    dialog._refresh_ocr_status()
    assert "无法探测" in dialog._ocr_status.text()


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


# ---- 归一化去重开关 ----


def test_dialog_reflects_canonical_options(dialog, config):
    assert dialog._canonical_url.isChecked() == bool(config.get("history.canonical_dedupe.url"))
    assert dialog._canonical_path.isChecked() == bool(config.get("history.canonical_dedupe.path"))


def test_dialog_saves_canonical_options(dialog, config):
    dialog._canonical_url.setChecked(False)
    dialog._canonical_path.setChecked(False)
    dialog._save()

    assert config.get("history.canonical_dedupe.url") is False
    assert config.get("history.canonical_dedupe.path") is False


# ---- 设置页：原生能力面板 ----


def _fake_native_report(**kwargs):
    from linglong_clip.utils.selfcheck import (
        DEGRADED,
        FAIL,
        OK,
        CheckResult,
        SelfCheckReport,
    )

    report = SelfCheckReport(platform_label="Test", python_version="3.x", backend="fake")
    report.results = [
        CheckResult("被选中的后端", OK, "fake"),
        CheckResult("剪贴板原生读取", DEGRADED, "调不通", "会回落到 Qt"),
    ]
    if kwargs.get("with_failure"):
        report.results.append(CheckResult("全局快捷键注册", FAIL, "炸了", "去授权"))
    report.notes = ["说明"]
    return report


def test_settings_native_panel_absent_without_report(qt_app, home):
    """不传报告时应跳过整个面板，而不是显示一个空壳。"""
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    dialog = SettingsDialog(Config(home / "config.json"))
    assert dialog._native_report is None


def test_settings_accepts_native_report(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    report = _fake_native_report()
    dialog = SettingsDialog(Config(home / "config.json"), native_report=report)
    assert dialog._native_report is report


def test_settings_native_panel_renders_checks(qt_app, home):
    """面板要把每项判定的名称与说明都摆出来。

    用户不该为了知道「快捷键为什么没生效」去翻日志或开终端。
    """
    from PySide6.QtWidgets import QLabel

    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    dialog = SettingsDialog(
        Config(home / "config.json"), native_report=_fake_native_report()
    )
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    joined = "\n".join(texts)

    assert "被选中的后端" in joined
    assert "剪贴板原生读取" in joined
    assert "调不通" in joined
    assert "--selfcheck" in joined


def test_settings_native_panel_shows_failure_hint(qt_app, home):
    from PySide6.QtWidgets import QLabel

    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    dialog = SettingsDialog(
        Config(home / "config.json"),
        native_report=_fake_native_report(with_failure=True),
    )
    joined = "\n".join(label.text() for label in dialog.findChildren(QLabel))
    # 失败项要带上可执行的提示，不能只说「失败了」
    assert "去授权" in joined


def test_settings_native_panel_does_not_break_saving(qt_app, home):
    """加了面板之后配置保存路径不能被带坏。"""
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    dialog = SettingsDialog(config, native_report=_fake_native_report())
    dialog._capture_enabled.setChecked(False)
    dialog._save()
    assert config.get("capture.enabled") is False


# --------------------------------------------------- 设置页：系统剪贴板历史


def test_settings_has_native_history_group(qt_app, home):
    """导入入口要在界面上，用户不必为一次性操作去开终端。"""
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    dialog = SettingsDialog(Config(home / "config.json"))
    assert hasattr(dialog, "_native_import_button")
    assert hasattr(dialog, "_native_import_status")
    assert dialog._native_import_button.text()


def test_native_history_button_disabled_when_unsupported(qt_app, home, monkeypatch):
    """平台不支持时按钮要禁用，并把原因写清楚 —— 别让人点了没反应。"""
    from linglong_clip.platform import base as base_module
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    class _Unsupported:
        name = "linux"

        def is_history_supported(self):
            return False

    monkeypatch.setattr(base_module, "detect_backend", lambda: _Unsupported())
    monkeypatch.setattr(
        "linglong_clip.platform.detect_backend", lambda: _Unsupported(), raising=False
    )

    dialog = SettingsDialog(Config(home / "config.json"))
    dialog._refresh_native_history_state()
    assert dialog._native_import_button.isEnabled() is False
    assert "不可用" in dialog._native_import_status.text()


def test_native_history_button_enabled_when_supported(qt_app, home, monkeypatch):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    class _Supported:
        name = "windows"

        def is_history_supported(self):
            return True

        def read_windows(self, limit=30):
            return []

    monkeypatch.setattr(
        "linglong_clip.platform.detect_backend", lambda: _Supported(), raising=False
    )
    dialog = SettingsDialog(Config(home / "config.json"))
    dialog._refresh_native_history_state()
    assert dialog._native_import_button.isEnabled() is True
    assert "可用" in dialog._native_import_status.text()


def test_native_history_state_survives_probe_failure(qt_app, home, monkeypatch):
    """探测抛异常时按「不可用」处理，不能把设置页搞崩。"""
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    def _boom():
        raise RuntimeError("探测炸了")

    monkeypatch.setattr("linglong_clip.platform.detect_backend", _boom, raising=False)
    dialog = SettingsDialog(Config(home / "config.json"))
    dialog._refresh_native_history_state()
    assert dialog._native_import_button.isEnabled() is False


def test_native_history_import_reports_result(qt_app, home, monkeypatch):
    """点导入要真的跑一遍并把结论显示出来（含隐私拦截条数）。"""
    from linglong_clip.platform.base import CaptureResult
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    class _Backend:
        name = "windows"

        def is_history_supported(self):
            return True

        def read_windows(self, limit=30):
            return [
                CaptureResult(kind="text", text="普通内容"),
                CaptureResult(
                    kind="text",
                    text="-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
                ),
            ]

    monkeypatch.setattr(
        "linglong_clip.platform.detect_backend", lambda: _Backend(), raising=False
    )
    dialog = SettingsDialog(Config(home / "config.json"))
    dialog._run_native_history_import()

    status = dialog._native_import_status.text()
    assert "导入 1" in status
    # 被拦下的条数必须让用户看见，否则「为什么少了一条」无从解释
    assert "1 条" in status


def test_native_history_import_handles_unsupported(qt_app, home, monkeypatch):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    class _Backend:
        name = "linux"

        def is_history_supported(self):
            return False

    monkeypatch.setattr(
        "linglong_clip.platform.detect_backend", lambda: _Backend(), raising=False
    )
    dialog = SettingsDialog(Config(home / "config.json"))
    dialog._run_native_history_import()
    assert dialog._native_import_status.text()


def test_native_history_import_handles_storage_failure(qt_app, home, monkeypatch):
    """打不开历史仓库时应显示原因，不是静默失败。"""
    from linglong_clip.core import repository as repo_module
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    def _boom(config):
        raise OSError("磁盘满了")

    monkeypatch.setattr(repo_module, "open_repository", _boom)
    dialog = SettingsDialog(Config(home / "config.json"))
    dialog._run_native_history_import()
    assert "磁盘满了" in dialog._native_import_status.text()
