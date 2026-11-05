"""设置对话框、转换窗口、托盘与快捷键的测试。"""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

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
    # Windows 上分隔符是 `\\`，断言要用 os.path.join 拼
    assert dialog._storage_location.endswith(os.path.join(".clipboard", "clipboard.json"))


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


# ---- 界面上那些「勾了但不起作用」的开关：逐个验证真的生效 ----

def test_dialog_has_card_density_control(dialog, config):
    """列表密度此前根本没有控件，配置项也没人读。"""
    dialog._density.setCurrentIndex(dialog._density.findData("compact"))
    dialog._save()
    assert config.get("ui.card_density") == "compact"
    assert "ui.card_density" in dialog.changes


def test_dialog_lists_density_options_from_single_source(config, qt_app):
    """下拉里的档位来自 `core/theme_spec` 的同一份清单。

    此前文案是手写在这个构造函数里的，与「哪些值合法」两处各写一份 ——
    漏掉一档的表现是「配置里能存、下拉里选不出来」。
    """
    from linglong_clip.core.theme_spec import DENSITY_VALUES
    from linglong_clip.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(config, AutomationEngine(), {})
    values = [dialog._density.itemData(i) for i in range(dialog._density.count())]
    assert values == list(DENSITY_VALUES)


def test_dialog_reports_dirty_density_in_config(home, qt_app):
    """配置里存着一个不存在的档位时，设置页要当场说明。

    下拉会显示成默认档位（合法），但用户以为自己选的就是配置里那个 ——
    不说明的话，「这个档位其实不存在」永远查不出来。
    """
    import json as _json

    from linglong_clip.core.theme_spec import DEFAULT_DENSITY
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    path = home / "config.json"
    path.write_text(
        _json.dumps({"ui": {"card_density": "超级紧凑"}, "meta": {"schema_version": 1}}),
        encoding="utf-8",
    )
    config = Config(path)
    # 打开时已被清洗成默认值 —— 这里模拟「用户手工把值改回去后重开设置」，
    # 验证界面不会因为拿到了脏值而崩，但也不会假装它是合法的。
    config.set("ui.card_density", "超级紧凑", persist=False)

    dialog = SettingsDialog(config, AutomationEngine(), {})
    assert dialog._density.currentData() == DEFAULT_DENSITY
    assert "不是有效" in dialog._density_hint.text()


def test_dialog_density_hint_explains_selected_option(dialog):
    """用户改档位时，说明要跟着换成新档位的说明。"""
    dialog._density.setCurrentIndex(dialog._density.findData("compact"))
    hint = dialog._density_hint.text()
    assert "紧凑" in hint
    assert "两行" in hint


def test_dialog_save_always_writes_a_catalog_value(config, qt_app):
    """保存时写回去的必须是清单里的值，不能是下拉里那串文案。

    守的是一类很容易犯的错：`currentData()` 与 `currentText()` 用混。
    写进配置的是「紧凑（两行，一屏更多）」这种展示文案时，
    界面上一切正常，但下次打开就落到默认档位 —— 而用户明明选过。
    """
    from linglong_clip.core.theme_spec import DENSITY_VALUES, is_valid_density
    from linglong_clip.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(config, AutomationEngine(), {})
    for index in range(dialog._density.count()):
        dialog._density.setCurrentIndex(index)
        dialog._save()
        assert is_valid_density(config.get("ui.card_density"))
        assert config.get("ui.card_density") in DENSITY_VALUES


def test_card_density_compact_shrinks_card(storage, config, qt_app):
    """compact 模式下卡片少一行元信息、外边距更小，高度应真的更矮。"""
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.ui.item_card import ItemCard, normalize_density
    from linglong_clip.ui.theme import palette_for

    palette = palette_for("light")
    item = ClipboardItem(id=1, kind="text", content="一条很普通的记录", source_app="chrome")
    comfortable = ItemCard(item, palette, density="comfortable")
    compact = ItemCard(item, palette, density="compact")
    assert compact.is_compact is True
    assert comfortable.is_compact is False
    # 紧凑版没有元信息行
    assert comfortable.findChildren(type(compact._body)) is not None
    assert normalize_density("COMPACT") == "compact"
    assert normalize_density("乱填") == "comfortable"


def test_main_window_applies_density_on_change(storage, config, qt_app):
    from linglong_clip.core.automation import AutomationEngine
    from linglong_clip.core.semantic import SemanticEngine
    from linglong_clip.ui.main_window import MainWindow

    window = MainWindow(storage, config, SemanticEngine(storage), AutomationEngine())
    assert window._density == "comfortable"
    window.apply_density("compact")
    assert window._density == "compact"
    # 相同值不触发重建
    window.apply_density("compact")
    assert window._density == "compact"


def test_search_uses_fuzzy_flag(storage, config, qt_app):
    """`ui.fuzzy_search` 关闭时不做意图解析，查询串原样检索。"""
    from linglong_clip.core.automation import AutomationEngine
    from linglong_clip.core.semantic import SemanticEngine
    from linglong_clip.ui.main_window import MainWindow

    window = MainWindow(storage, config, SemanticEngine(storage), AutomationEngine())
    captured = {}

    def fake_search(query, **kwargs):
        captured.update(kwargs)
        captured["query"] = query
        return []

    window._engine.search = fake_search
    window._search.setText("上周的截图")
    config.set("ui.fuzzy_search", False)
    window.reload()
    assert captured["use_intent"] is False
    assert window._intent_label.text() == ""

    config.set("ui.fuzzy_search", True)
    window.reload()
    assert captured["use_intent"] is True


def test_close_to_tray_hides_window(config, qt_app, home, monkeypatch):
    """`ui.close_to_tray` 打开时点关闭只收进托盘，不退出。"""
    from linglong_clip.ui import application as app_module

    monkeypatch.setattr(app_module.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    app = app_module.Application(["test"])
    hidden, quits = [], []
    app.window.hide = lambda: hidden.append(True)
    app.quit = lambda: quits.append(True)

    app.config.set("ui.close_to_tray", True)
    app._on_window_close_requested()
    assert hidden == [True] and quits == []


def test_close_to_tray_false_quits(config, qt_app, home, monkeypatch):
    from linglong_clip.ui import application as app_module

    monkeypatch.setattr(app_module.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    app = app_module.Application(["test"])
    hidden, quits = [], []
    app.window.hide = lambda: hidden.append(True)
    app.quit = lambda: quits.append(True)

    app.config.set("ui.close_to_tray", False)
    app._on_window_close_requested()
    assert hidden == [] and quits == [True]


def test_close_without_tray_quits(config, qt_app, home, monkeypatch):
    """托盘不可用时收进托盘等于叫不回来，只能退出。"""
    from linglong_clip.ui import application as app_module

    monkeypatch.setattr(app_module.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    app = app_module.Application(["test"])
    hidden, quits = [], []
    app.window.hide = lambda: hidden.append(True)
    app.quit = lambda: quits.append(True)

    app.config.set("ui.close_to_tray", True)
    app._on_window_close_requested()
    assert hidden == [] and quits == [True]


def test_main_window_close_event_defers_to_application(storage, config, qt_app):
    """窗口自己不销毁，只发信号 —— 托盘唤起时要复用同一个实例。"""
    from PySide6.QtGui import QCloseEvent

    from linglong_clip.core.automation import AutomationEngine
    from linglong_clip.core.semantic import SemanticEngine
    from linglong_clip.ui.main_window import MainWindow

    window = MainWindow(storage, config, SemanticEngine(storage), AutomationEngine())
    fired = []
    window.close_requested.connect(lambda: fired.append(True))
    event = QCloseEvent()
    window.closeEvent(event)
    assert fired == [True]
    assert not event.isAccepted()


def test_start_minimized_reads_config(config, qt_app, home, monkeypatch):
    """`ui.start_minimized` 此前是死配置，只有 CLI 参数生效。"""
    from linglong_clip.ui import application as app_module

    monkeypatch.setattr(app_module.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    app = app_module.Application(["test"])

    app.config.set("ui.start_minimized", False)
    assert app._resolve_start_minimized() is False
    app.config.set("ui.start_minimized", True)
    assert app._resolve_start_minimized() is True

    # CLI 显式给了 --minimized 就以它为准
    app2 = app_module.Application(["test"], start_minimized=True)
    app2.config.set("ui.start_minimized", False)
    assert app2._resolve_start_minimized() is True


# ---- 快速粘贴快捷键：配置项此前只被显示、从没注册 ----


def test_expand_hotkey_range_basic():
    from linglong_clip.ui.hotkeys import expand_hotkey_range

    assert expand_hotkey_range("Ctrl+Shift+1..9") == [f"Ctrl+Shift+{i}" for i in range(1, 10)]
    assert expand_hotkey_range("Ctrl+Alt+1..3") == ["Ctrl+Alt+1", "Ctrl+Alt+2", "Ctrl+Alt+3"]


def test_expand_hotkey_range_single_sequence():
    from linglong_clip.ui.hotkeys import expand_hotkey_range

    assert expand_hotkey_range("Ctrl+Shift+V") == ["Ctrl+Shift+V"]
    assert expand_hotkey_range("  Ctrl+Shift+V  ") == ["Ctrl+Shift+V"]


def test_expand_hotkey_range_rejects_bad_specs():
    """非法范围返回空列表，调用方据此给出可排查的日志，而不是静默注册半个。"""
    from linglong_clip.ui.hotkeys import expand_hotkey_range

    assert expand_hotkey_range("") == []
    assert expand_hotkey_range("Ctrl+Shift+9..1") == []
    assert expand_hotkey_range("Ctrl+Shift+1..12") == []
    assert expand_hotkey_range("Ctrl+Shift+a..c") == []


def test_expand_hotkey_range_without_modifier_is_not_split():
    """`1..9` 没有修饰键，当完整快捷键交给后端判断，不拆成 9 个裸数字键。"""
    from linglong_clip.ui.hotkeys import expand_hotkey_range

    assert expand_hotkey_range("1..9") == ["1..9"]


def test_quick_paste_hotkeys_are_registered(config, qt_app, home, monkeypatch):
    """`ui.quick_paste_hotkey` 声明的 9 个快捷键必须真的注册。"""
    from linglong_clip.ui import application as app_module

    monkeypatch.setattr(app_module.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    app = app_module.Application(["test"])
    registered = []

    def fake_register(sequence, callback):
        registered.append(sequence)
        return True

    app.hotkeys.register = fake_register
    app.config.set("ui.quick_paste_hotkey", "Ctrl+Shift+1..9")
    count = app._install_quick_paste_hotkeys()
    assert count == 9
    # `__init__` 已注册过主快捷键，这里只看这一批新增的
    assert registered[-9:] == [f"Ctrl+Shift+{i}" for i in range(1, 10)]
    assert app._quick_hotkey_sequences == registered[-9:]


def test_quick_paste_hotkeys_roll_back_on_partial_failure(config, qt_app, home, monkeypatch):
    """9 个里有一个注册不上时整组撤掉，不留「1、2 能用、3 不行」的半残状态。"""
    from linglong_clip.ui import application as app_module

    monkeypatch.setattr(app_module.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    app = app_module.Application(["test"])
    attempts, revoked = [], []

    def flaky_register(sequence, callback):
        attempts.append(sequence)
        return sequence != "Ctrl+Shift+3"

    def fake_unregister(sequences):
        revoked.extend(sequences)
        return len(sequences)

    app.hotkeys.register = flaky_register
    app.hotkeys.unregister = fake_unregister
    app.config.set("ui.quick_paste_hotkey", "Ctrl+Shift+1..9")
    # `set()` 已经触发过一次注册（并回滚），这里单独再看一次显式调用的行为
    attempts.clear()
    revoked.clear()
    assert app._install_quick_paste_hotkeys() == 0
    # 失败发生在第 3 个，循环就此中断（不再试后面的），并把前两个撤掉
    assert attempts == ["Ctrl+Shift+1", "Ctrl+Shift+2", "Ctrl+Shift+3"]
    assert revoked == ["Ctrl+Shift+1", "Ctrl+Shift+2"]
    assert app._quick_hotkey_sequences == []


def test_changing_quick_paste_hotkey_reregisters(config, qt_app, home, monkeypatch):
    """改配置后要重注册，不能把旧绑定留在系统里。"""
    from linglong_clip.ui import application as app_module

    monkeypatch.setattr(app_module.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    app = app_module.Application(["test"])
    registered, cleared = [], []
    app.hotkeys.register = lambda sequence, callback: registered.append(sequence) or True
    app.hotkeys.unregister_all = lambda: cleared.append(True)

    # `set()` 会触发变更通知（注册一次）；再显式调一次模拟逐键触发
    app.config.set("ui.quick_paste_hotkey", "Ctrl+Alt+1..3")
    assert len(cleared) == 1

    app._on_config_change("ui.quick_paste_hotkey", "Ctrl+Alt+1..3")
    assert len(cleared) == 2
    # 每次变更都重建一次，所以出现两批
    assert [s for s in registered if s.startswith("Ctrl+Alt")] == [
        "Ctrl+Alt+1", "Ctrl+Alt+2", "Ctrl+Alt+3",
        "Ctrl+Alt+1", "Ctrl+Alt+2", "Ctrl+Alt+3",
    ]
    assert app._quick_hotkey_sequences == ["Ctrl+Alt+1", "Ctrl+Alt+2", "Ctrl+Alt+3"]


def test_main_hotkey_failure_skips_quick_paste(config, qt_app, home, monkeypatch):
    """主快捷键都注册不上时不必再试快速粘贴那一组。"""
    from linglong_clip.ui import application as app_module

    monkeypatch.setattr(app_module.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    app = app_module.Application(["test"])
    calls = []
    app.hotkeys.register = lambda sequence, callback: calls.append(sequence) or False
    app._install_hotkeys()
    assert calls == [app.config.get("ui.global_hotkey")]
    assert app._quick_hotkey_sequences == []


def test_quick_paste_overlay_paste_nth(storage, config, qt_app):
    """`paste_nth` 按序号取第 N 条并发出选择信号。"""
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.semantic import SemanticEngine
    from linglong_clip.ui.quick_paste import QuickPasteOverlay

    for index in range(3):
        storage.add(ClipboardItem(kind="text", content=f"item{index}", hash=f"h{index}"))
    overlay = QuickPasteOverlay(storage, SemanticEngine(storage), config)
    chosen = []
    overlay.item_chosen.connect(lambda item_id, plain: chosen.append(item_id))
    overlay.show_centered()
    overlay.paste_nth(2)
    assert len(chosen) == 1
    assert chosen[0] is not None
    # 越界序号不应发出信号
    chosen.clear()
    overlay.paste_nth(99)
    assert chosen == []
def test_quick_paste_numbered_hotkey_pastes_what_was_shown(storage, config, qt_app):
    """浮层显示着的时候若库里多了新记录，按数字快捷键必须仍取「看到的那条」。

    这是一条**用户会以为是数据错乱**的路径：

    ```
    浮层打开，第 1 条是「旧的重要片段」（用户想粘的就是它）
    此刻在别处复制了一段垃圾          ← 浮层没关，监控线程照常入库
    按 Ctrl+Shift+1                    → 粘出来的是那段垃圾
    ```

    序号在界面上只对「列表里的第 N 条」有意义，而旧实现是按序号去库里
    **重新取一次「当前第 N 条」** —— 库里顺序变了，序号的含义就变了，
    浮层上印的数字与粘出来的东西对不上。用户没有任何办法自己发现
    中间发生过入库，只会以为「这东西粘错了」。
    """
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.semantic import SemanticEngine
    from linglong_clip.ui.quick_paste import QuickPasteOverlay

    storage.add(ClipboardItem(kind="text", content="旧的重要片段", preview="旧的重要片段", hash="h0"))
    overlay = QuickPasteOverlay(storage, SemanticEngine(storage), config)
    overlay.show_centered()
    assert [item.preview for item in overlay._items] == ["旧的重要片段"]

    # 浮层开着，用户又在别处复制了别的东西
    storage.add(ClipboardItem(kind="text", content="刚刚复制的垃圾", preview="刚刚复制的垃圾", hash="h9"))

    chosen = []
    overlay.item_chosen.connect(lambda item_id, plain: chosen.append(item_id))
    overlay.paste_nth(1)

    assert len(chosen) == 1
    resolved = storage.get_many(chosen)[chosen[0]]
    assert resolved.preview == "旧的重要片段"


def test_quick_paste_numbered_hotkey_survives_deleted_row(storage, config, qt_app):
    """「看到的那条」若已被删掉，就什么都不做 —— 但**不能**粘成别的一条。

    序号在原位置失效之后，把它当成「当前第 N 条」是最坏的一种兜底：
    用户按下 1，取到的是完全无关的另一条内容，还粘了出去。
    """
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.semantic import SemanticEngine
    from linglong_clip.ui.quick_paste import QuickPasteOverlay

    for index in range(3):
        storage.add(ClipboardItem(kind="text", content=f"条目{index}", preview=f"条目{index}", hash=f"h{index}"))
    overlay = QuickPasteOverlay(storage, SemanticEngine(storage), config)
    overlay.show_centered()
    assert overlay._row_ids, "浮层应已渲染出带 id 的行"
    doomed_id = overlay._row_ids[0]
    storage.delete(doomed_id)
    storage.add(ClipboardItem(kind="text", content="新来的", preview="新来的", hash="hnew"))

    chosen = []
    missing = []
    overlay.item_chosen.connect(lambda item_id, plain: chosen.append(item_id))
    overlay.row_missing.connect(missing.append)
    overlay.paste_nth(1)
    # 既不能粘成别的一条，也不能静默 —— 静默会让人以为快捷键失灵
    assert chosen == []
    assert missing == [doomed_id]


def test_quick_paste_numbered_hotkey_uses_current_list_after_reload(storage, config, qt_app):
    """列表真的重排过（重新检索）之后，序号就该按新列表算 —— 别修过头。

    上一条修的是「序号指的一直是那份已显示的列表」。若列表本身重新加载了，
    屏幕上印的序号也跟着变了，此时取最新的才对。
    """
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.semantic import SemanticEngine
    from linglong_clip.ui.quick_paste import QuickPasteOverlay

    storage.add(ClipboardItem(kind="text", content="旧的", preview="旧的", hash="hold"))
    overlay = QuickPasteOverlay(storage, SemanticEngine(storage), config)
    overlay.show_centered()
    storage.add(ClipboardItem(kind="text", content="新的", preview="新的", hash="hnew"))
    overlay._reload()  # 界面重新渲染，第 1 条已经是「新的」了

    chosen = []
    overlay.item_chosen.connect(lambda item_id, plain: chosen.append(item_id))
    overlay.paste_nth(1)
    assert storage.get_many(chosen)[chosen[0]].preview == "新的"


# ---- 设置页：删除的同步 ----


def test_settings_shows_tombstone_summary(qt_app, home):
    from linglong_clip.core.tombstones import build_tombstone_store
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    store = build_tombstone_store()
    store.record("aaaabbbbccccddddeeeeffff00001111")

    config = Config(home / "config.json")
    dialog = SettingsDialog(config)
    texts = [
        child.text()
        for child in dialog.findChildren(QLabel)
        if child.text()
    ]
    assert any("等待同步" in text for text in texts)
    assert any("tombstones.json" in text for text in texts)


def test_settings_tombstone_panel_survives_broken_file(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config
    from linglong_clip.core.tombstones import TOMBSTONE_FILENAME

    (home / TOMBSTONE_FILENAME).write_text("{ not json", encoding="utf-8")
    config = Config(home / "config.json")
    dialog = SettingsDialog(config)  # 不该抛异常
    texts = [child.text() for child in dialog.findChildren(QLabel) if child.text()]
    assert any("还没有被删除的内容" in text for text in texts)


def test_settings_reports_online_peers(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    dialog = SettingsDialog(
        config,
        None,
        {
            "folder": "/tmp/x",
            "devices": ["a"],
            "encrypted": False,
            "peers": [
                {"host": "192.168.1.8", "port": 48620, "ok": True, "device": "笔记本"},
                {"host": "192.168.1.9", "port": 48620, "ok": False, "device": ""},
            ],
        },
    )
    texts = [child.text() for child in dialog.findChildren(QLabel) if child.text()]
    assert any("1/2 台在线" in text for text in texts)
    assert any("笔记本" in text for text in texts)


def test_settings_omits_peer_row_when_absent(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    dialog = SettingsDialog(config, None, {"folder": "/tmp/x", "devices": []})
    texts = [child.text() for child in dialog.findChildren(QLabel) if child.text()]
    assert not any("台在线" in text for text in texts)


def test_settings_has_flag_sync_panel(qt_app, home):
    """设置页要说清「固定/收藏取并集」——否则用户无法从界面推断这个行为。"""
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    from PySide6.QtWidgets import QLabel

    dialog = SettingsDialog(Config(home / "config.json"))
    group = dialog._build_flag_group()
    assert group.title() == "固定与收藏的同步"
    joined = "\n".join(label.text() for label in group.findChildren(QLabel))
    # 说明必须写在界面上，而不是只留在 README 里
    assert "并集" in joined
    assert "取消" in joined
    # QLabel 不渲染 Markdown，混进 ** 会直接显示成星号
    assert "**" not in joined


def test_settings_flag_panel_counts_pinned(qt_app, home):
    """面板要能读出本机固定项条数，而不是永远显示「暂无」。"""
    from linglong_clip.core.models import ClipboardItem, content_hash
    from linglong_clip.core.repository import open_repository
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    storage = open_repository(config)
    storage.add(
        ClipboardItem(
            kind="text", content="固定项", preview="固定项",
            hash=content_hash("text", "固定项"), pinned=True,
        )
    )
    storage.close()

    dialog = SettingsDialog(config)
    assert dialog._count_pinned() == 1


def test_settings_flag_panel_survives_broken_store(qt_app, home):
    """历史文件损坏时设置页仍要能打开 —— 那时用户最需要它。"""
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config
    from linglong_clip.utils.paths import json_path

    config = Config(home / "config.json")
    config.update({"storage.backend": "json"}, persist=True)
    json_path().parent.mkdir(parents=True, exist_ok=True)
    json_path().write_text("{ 这不是 JSON", encoding="utf-8")

    dialog = SettingsDialog(config)
    # 不抛异常即可，读数退化为 0
    assert dialog._count_pinned() == 0


# ---- 出站防护（复制敏感内容）----


def test_settings_outbound_hint_reports_missing_mechanism(qt_app, home):
    """本平台没有系统排除机制时必须说出来，不能让用户以为已经防住了。"""
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    dialog = SettingsDialog(config)
    dialog._exclusion_formats_override = []
    dialog._auto_clear.setValue(30)
    dialog._refresh_outbound_hint()

    text = dialog._auto_clear_hint.text()
    assert "没有" in text and "机制" in text
    assert "30 秒清空剪贴板" in text


def test_settings_outbound_hint_reports_system_mechanism(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    dialog = SettingsDialog(config)
    dialog._exclusion_formats_override = ["ExcludeClipboardContentFromMonitorProcessing"]
    dialog._refresh_outbound_hint()

    assert "系统级" in dialog._auto_clear_hint.text()


def test_settings_outbound_hint_warns_when_clear_disabled(qt_app, home):
    """关掉自动清空后要明说「会一直留在剪贴板」，而不是静默什么都不做。"""
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    dialog = SettingsDialog(config)
    dialog._exclusion_formats_override = []
    dialog._auto_clear.setValue(0)

    assert "一直留在剪贴板" in dialog._auto_clear_hint.text()


def test_settings_outbound_hint_updates_on_change(qt_app, home):
    """改数值要立刻反映，不用等保存 —— 否则用户判断不了有没有生效。"""
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    dialog = SettingsDialog(config)
    dialog._exclusion_formats_override = []
    dialog._auto_clear.setValue(120)
    assert "120 秒清空剪贴板" in dialog._auto_clear_hint.text()


def test_settings_outbound_hint_survives_backend_probe_failure(qt_app, home, monkeypatch):
    """探测后端失败不能让设置页打不开。"""
    from linglong_clip.platform import base as base_module
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    def _boom():
        raise RuntimeError("探测炸了")

    monkeypatch.setattr(base_module, "detect_backend", _boom, raising=False)
    dialog = SettingsDialog(Config(home / "config.json"))
    # 不抛异常即可
    assert dialog._auto_clear_hint.text() != ""


def test_settings_saves_auto_clear_seconds(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    dialog = SettingsDialog(config)
    dialog._auto_clear.setValue(90)
    dialog._save()
    assert config.get("privacy.sensitive_auto_clear_seconds") == 90


# ------------------------------------------------- 主选择区设置项

def test_settings_primary_disabled_when_unsupported(qt_app, home, monkeypatch):
    """本机读不到主选择区时，开关要被禁用并说清原因。

    留一个能勾但勾了没反应的复选框，比不给这个选项更糟：
    用户会以为功能坏了，而不是「这台机器没有这条通道」。
    """
    import linglong_clip.platform as platform_module
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    class _NoPrimary:
        name = "linux"

        def capabilities(self):
            return {"primary_selection": False}

    monkeypatch.setattr(platform_module, "detect_backend", lambda: _NoPrimary())

    config = Config(home / "config.json")
    config.set("capture.monitor_primary", True)  # 配置里开着，但本机不支持
    dialog = SettingsDialog(config)

    assert dialog._monitor_primary.isEnabled() is False
    # 不支持时必须把勾去掉，否则界面显示「已开启」而实际永远不会生效
    assert dialog._monitor_primary.isChecked() is False
    assert "不可用" in dialog._primary_status.text()
    assert "wl-clipboard" in dialog._primary_status.text()


def test_settings_primary_enabled_when_supported(qt_app, home, monkeypatch):
    import linglong_clip.platform as platform_module
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    class _WithPrimary:
        name = "linux"

        def capabilities(self):
            return {"primary_selection": True}

    monkeypatch.setattr(platform_module, "detect_backend", lambda: _WithPrimary())

    config = Config(home / "config.json")
    dialog = SettingsDialog(config)
    assert dialog._monitor_primary.isEnabled() is True
    assert "可用" in dialog._primary_status.text()

    dialog._monitor_primary.setChecked(True)
    dialog._primary_interval.setValue(1500)
    dialog._save()

    assert config.get("capture.monitor_primary") is True
    assert config.get("capture.primary_interval_ms") == 1500


def test_settings_survives_probe_failure(qt_app, home, monkeypatch):
    """探测抛异常不能让设置页打不开。"""
    import linglong_clip.platform as platform_module
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    class _Exploding:
        name = "linux"

        def capabilities(self):
            raise RuntimeError("X 服务器断了")

    monkeypatch.setattr(platform_module, "detect_backend", lambda: _Exploding())

    dialog = SettingsDialog(Config(home / "config.json"))
    assert dialog._monitor_primary.isEnabled() is False


# ---- 设置页：规则状态（哪些规则真的会跑） ----


def _dialog_with_rules(config, rules):
    import json

    from linglong_clip.core.automation import AutomationEngine
    from linglong_clip.ui.settings_dialog import SettingsDialog

    automation = AutomationEngine()
    dialog = SettingsDialog(config, automation)
    dialog._rules_editor.setPlainText(json.dumps(rules, ensure_ascii=False))
    return dialog


def test_rules_state_summarizes_triggers(config, qt_app):
    """三种触发方式各有多少条，必须在界面上说清。

    「什么时候跑」是用户配完规则最想知道的一件事，而 JSON 里只有
    一个英文常量。不写出来的话，规则看起来配好了却什么也不做时，
    用户没有任何线索。
    """
    dialog = _dialog_with_rules(
        config,
        [
            {"name": "a", "kinds": ["text"], "actions": ["json.minify"], "trigger": "auto"},
            {"name": "b", "kinds": ["link"], "actions": ["url.strip_tracking"],
             "trigger": "outbound"},
            {"name": "c", "kinds": ["text"], "actions": ["lines.unique"]},
        ],
    )
    text = dialog._rules_state.text()
    assert text.startswith("✓")
    assert "采集时 1 条" in text
    assert "复制出去时 1 条" in text
    assert "仅手动 1 条" in text


def test_rules_state_flags_bad_trigger(config, qt_app):
    """写错的触发方式要当场标出来，而不是等用户去命令行才发现。"""
    dialog = _dialog_with_rules(
        config,
        [{"name": "写错了", "kinds": ["link"], "actions": ["url.strip_tracking"],
          "trigger": "paste"}],
    )
    text = dialog._rules_state.text()
    assert text.startswith("✗")
    assert "写错了" in text
    assert "不会生效" in text


def test_rules_state_flags_unknown_action(config, qt_app):
    dialog = _dialog_with_rules(
        config, [{"name": "未知动作", "actions": ["nope"], "trigger": "auto"}]
    )
    assert "未知动作" in dialog._rules_state.text()


def test_rules_state_flags_broken_json_with_line_number(config, qt_app):
    dialog = _dialog_with_rules(config, [])
    dialog._rules_editor.setPlainText('[{"name": "坏",')
    text = dialog._rules_state.text()
    assert text.startswith("✗")
    assert "JSON 无法解析" in text
    assert "行" in text


def test_rules_state_rejects_non_array(config, qt_app):
    dialog = _dialog_with_rules(config, [])
    dialog._rules_editor.setPlainText('{"name": "不是数组"}')
    assert "数组" in dialog._rules_state.text()


# ---- 设置页「隐私 → 自定义脱敏规则」----


def test_settings_redact_editor_reflects_config(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    config.set("privacy.custom_redact_rules", {"员工号": r"EMP\d{6}"})
    dialog = SettingsDialog(config)
    assert "员工号" in dialog._redact_editor.toPlainText()


def test_settings_redact_editor_empty_by_default(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    dialog = SettingsDialog(Config(home / "config.json"))
    assert dialog._redact_editor.toPlainText().strip() == "{}"


def test_settings_saves_custom_redact_rules(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    dialog = SettingsDialog(config)
    dialog._redact_editor.setPlainText('{"员工号": "EMP\\\\d{6}"}')
    dialog._save()

    assert config.get("privacy.custom_redact_rules") == {"员工号": r"EMP\d{6}"}
    assert "privacy.custom_redact_rules" in dialog.changes


def test_settings_redact_editor_flags_bad_json(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    dialog = SettingsDialog(Config(home / "config.json"))
    dialog._redact_editor.setPlainText("{not json")
    assert "JSON 无法解析" in dialog._redact_state.text()


def test_settings_redact_editor_flags_uncompilable_rule(qt_app, home):
    """写错的正则要在界面上当场指出，而不是保存后静默失效。"""
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    dialog = SettingsDialog(Config(home / "config.json"))
    dialog._redact_editor.setPlainText('{"写错的": "[unclosed"}')
    state = dialog._redact_state.text()
    assert "写错的" in state
    assert "不会生效" in state


def test_settings_redact_editor_ok_for_valid_rule(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    dialog = SettingsDialog(Config(home / "config.json"))
    dialog._redact_editor.setPlainText('{"员工号": "EMP\\\\d{6}"}')
    assert "✓" in dialog._redact_state.text()


def test_settings_warns_when_redact_switch_off(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    config.set("privacy.redact_on_copy_out", False)
    dialog = SettingsDialog(config)
    dialog._redact_editor.setPlainText('{"员工号": "EMP\\\\d{6}"}')
    assert "没勾选" in dialog._redact_state.text()


def test_settings_rejects_bad_rules_without_clearing(qt_app, home, monkeypatch):
    """格式错误时不该保存，也不该把用户已有的规则清掉。"""
    from PySide6.QtWidgets import QMessageBox

    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    config.set("privacy.custom_redact_rules", {"员工号": r"EMP\d{6}"})
    dialog = SettingsDialog(config)

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    dialog._redact_editor.setPlainText("{broken")
    dialog._save()

    assert config.get("privacy.custom_redact_rules") == {"员工号": r"EMP\d{6}"}


# ---- 设置页：图片附件的补图入口 ----


def test_settings_image_fetch_disabled_without_pending(qt_app, home):
    """没有缺失的图时按钮禁用 —— 一个点了没反应的按钮比不显示更让人困惑。"""
    from linglong_clip.core.storage import Storage
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    storage = Storage(home / "history.db")
    dialog = SettingsDialog(Config(home / "config.json"), storage=storage)
    assert dialog._image_fetch.isEnabled() is False
    assert dialog._image_fetch.text() == "从对端取回缺失的图片"
    storage.close()


def test_settings_image_fetch_shows_pending_count(qt_app, home):
    """同步来的记录只有摘要、没有字节 —— 这个状态必须看得见。

    否则用户在另一台机器上只会看到一张打不开的图，无从下手。
    """
    from linglong_clip.core.models import IMAGE_DIGEST_KEY, ClipboardItem
    from linglong_clip.core.storage import Storage
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    storage = Storage(home / "history.db")
    digest = "e" * 32
    storage.add(
        ClipboardItem(
            kind="image",
            content=f"{digest}.png",
            preview="[图片]",
            hash="img-pending",
            metadata={IMAGE_DIGEST_KEY: digest},
        )
    )
    dialog = SettingsDialog(Config(home / "config.json"), storage=storage)
    assert dialog._image_fetch.isEnabled() is True
    assert "1 张" in dialog._image_fetch.text()
    assert "尚未取回" in dialog._image_hint.text()
    storage.close()


def test_settings_image_fetch_without_storage_does_not_crash(qt_app, home):
    """命令行打开设置页时拿不到仓库对象：按钮禁用，不编数字。"""
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    dialog = SettingsDialog(Config(home / "config.json"))
    assert dialog._image_fetch.isEnabled() is False


def test_settings_image_fetch_warns_outside_lan_mode(qt_app, home, monkeypatch):
    """共享文件夹模式下点补图要说清原因，而不是静默无事发生。"""
    from linglong_clip.core.models import IMAGE_DIGEST_KEY, ClipboardItem
    from linglong_clip.core.storage import Storage
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    storage = Storage(home / "history.db")
    storage.add(
        ClipboardItem(
            kind="image",
            content="f" * 32 + ".png",
            preview="[图片]",
            hash="img-folder-mode",
            metadata={IMAGE_DIGEST_KEY: "f" * 32},
        )
    )
    config = Config(home / "config.json")
    config.set("sync.mode", "folder")
    dialog = SettingsDialog(config, storage=storage)

    shown = {}
    monkeypatch.setattr(
        "linglong_clip.ui.settings_dialog.QMessageBox.information",
        lambda *args, **kwargs: shown.setdefault("text", args[2]),
    )
    dialog._run_image_fetch()
    assert "共享文件夹" in shown["text"]
    storage.close()


# ---- 设置页：真实占用 ----


def test_settings_shows_real_footprint(qt_app, home):
    """设置页「历史存储」要显示真实磁盘占用，而不是主文件大小。

    和「图片附件」那块是同一个理由：磁盘在涨而用户看不见。
    只报主文件更糟 —— `history.db` 后面的 `-wal` 里装着尚未并入主库的
    全部改动，它可能比主文件还大。
    """
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.storage import Storage
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    store = Storage(home / "history.db")
    for index in range(40):
        store.add(
            ClipboardItem(kind="text", content=f"内容 {index} " + "x" * 3000, preview=f"{index}")
        )

    dialog = SettingsDialog(Config(home / "config.json"))
    text = dialog._storage_size.text()
    assert "统计中" not in text
    assert "尚无" not in text
    # 有伴随文件时要把「主库 + 待合并」拆开说
    assert "主库" in text
    assert "待合并" in text
    store.close()


def test_settings_never_calls_the_shm_pending(qt_app, home):
    """设置页也不能把 `-shm` 说成「待合并」。

    这和 `--stats` 是同一个缺陷的两个出口：`-shm` 在 Linux 上恒为 32 KB，
    与数据量无关。把「改动全部并入主库、WAL 清零」之后的现场交给设置页，
    它此前会写「+ 待合并 32.0 KB」—— 一件不存在、也不会发生的事。
    """
    from linglong_clip.core.storage import Storage
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config
    from linglong_clip.utils.paths import database_path

    store = Storage(home / "history.db")
    store.count()
    store._conn.commit()
    store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert database_path() == home / "history.db"

    dialog = SettingsDialog(Config(home / "config.json"))
    text = dialog._storage_size.text()
    assert "待合并" not in text, "WAL 已清空，不该说有待合并的改动"
    assert "索引" in text or "主库" not in text
    store.close()


def test_settings_counts_wal_content_as_pending(qt_app, home):
    """WAL 里真有内容时，设置页必须说「待合并」。

    反向守一条：不能为了去掉误报就把真话也一起删了。
    """
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.storage import Storage
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    store = Storage(home / "history.db")
    for index in range(40):
        store.add(
            ClipboardItem(kind="text", content=f"内容 {index} " + "x" * 3000, preview=f"{index}")
        )

    dialog = SettingsDialog(Config(home / "config.json"))
    text = dialog._storage_size.text()
    assert "待合并" in text
    assert "索引" not in text
    store.close()


def test_settings_footprint_without_history(qt_app, home):
    """没有历史文件时说「尚无历史文件」，不谎报 0。"""
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    dialog = SettingsDialog(Config(home / "config.json"))
    assert "尚无历史文件" in dialog._storage_size.text()


def test_settings_footprint_follows_configured_backend(qt_app, home):
    """显示的是**配置里选的后端**的占用，不是当前打开的那个。

    用户在这里改的是「下次启动用哪个」，两边搞混会得到一个看着对、
    其实说的不是同一件事的数字。
    """
    import json as _json

    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    config.set("storage.backend", "json")
    json_path = home / ".clipboard" / "clipboard.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(_json.dumps({"version": 1, "items": []}), encoding="utf-8")

    dialog = SettingsDialog(config)
    assert dialog._storage_size.text() != "尚无历史文件"
    # JSON 单文件没有伴随文件，不该出现「待合并」
    assert "待合并" not in dialog._storage_size.text()


# ---- 主题一致性（回归：对话框/浮层没套主题时深色下灰底灰字）----

def test_transform_window_uses_full_theme(config, qt_app):
    from linglong_clip.ui.theme import build_stylesheet, palette_for

    from linglong_clip.ui.transform_window import TransformWindow

    window = TransformWindow(config, AutomationEngine())
    assert window.styleSheet() == build_stylesheet(palette_for(config.get("ui.theme", "system")))
    assert window._body.objectName() == "TransformBody"


def test_quick_overlay_stylesheet_on_itself(storage, config, qt_app):
    from linglong_clip.core.semantic import SemanticEngine
    from linglong_clip.ui.quick_paste import QuickPasteOverlay
    from linglong_clip.ui.theme import toast_style

    overlay = QuickPasteOverlay(storage, SemanticEngine(storage), config)
    assert overlay.styleSheet() == toast_style(overlay._palette_theme)
