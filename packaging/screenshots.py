#!/usr/bin/env python3
"""重新生成文档用的界面截图。

为什么做成脚本而不是手工截屏：手工截的图会随代码改动过期，
而 README 里的截图一旦失真就会误导读者。这里走与界面测试相同的
Qt `offscreen` 渲染路径，因此 CI 里也能跑，代码改了重新执行即可。

用法：
    QT_QPA_PLATFORM=offscreen python packaging/screenshots.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
OUT = ROOT / "docs" / "screenshots"

# 必须在导入 Qt 之前设好，否则无头环境起不来
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LINGLONG_CLIPBOARD_HOME", tempfile.mkdtemp(prefix="linglong-shots-"))

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def seed_storage(storage) -> None:
    """造一批有代表性的记录，让截图看起来像真实使用场景。"""
    from linglong_clip.core.models import ClipboardItem, content_hash

    now = time.time()
    rows = [
        ("text", "会议纪要：下周二评审设计稿，记得带上交互说明", "chrome", False),
        ("link", "https://cnb.cool/asoe/TechSauce?utm_source=chat", "google-chrome", False),
        ("code", "def handler(event):\n    return event['key']", "code", True),
        ("color", "#4c8dff", "figma", False),
        ("text", "docker run -p 8080:80 -v $PWD:/app nginx:alpine", "terminal", False),
        ("image", "", "flameshot", False),
    ]
    for index, (kind, content, source, pinned) in enumerate(rows):
        storage.add(
            ClipboardItem(
                kind=kind,
                content=content,
                preview=content,
                hash=content_hash(kind, f"{kind}{index}"),
                source_app=source,
                created_at=now - index * 300,
                pinned=pinned,
            )
        )


def _render(widget, target: Path) -> None:
    from PySide6.QtWidgets import QApplication

    widget.show()
    widget.repaint()
    QApplication.processEvents()
    widget.grab().save(str(target))
    widget.hide()
    print(f"  ✓ {target.relative_to(ROOT)} ({widget.width()}x{widget.height()})")


def main() -> int:
    from PySide6.QtWidgets import QApplication, QTabWidget

    from linglong_clip.core.automation import AutomationEngine
    from linglong_clip.core.semantic import SemanticEngine
    from linglong_clip.core.storage import Storage
    from linglong_clip.ui.settings_dialog import SettingsDialog

    app = QApplication.instance() or QApplication([])
    OUT.mkdir(parents=True, exist_ok=True)

    storage = Storage()
    seed_storage(storage)
    _seed_form_item(storage)
    engine = SemanticEngine(storage)
    automation = AutomationEngine()

    print("生成截图中：")
    for theme, name in (("dark", "main-dark.png"), ("light", "main-light.png")):
        from linglong_clip.ui.main_window import MainWindow

        config = _config({"ui.theme": theme})
        window = MainWindow(storage, config, engine, automation)
        window.resize(1180, 720)
        _render(window, OUT / name)

    # 检索结果：体现「已理解」提示
    from linglong_clip.ui.main_window import MainWindow

    window = MainWindow(storage, _config({"ui.theme": "dark"}), engine, automation)
    window.resize(1180, 720)
    window.show()
    QApplication.processEvents()
    window._search.setText("最近的链接")
    # 搜索有 120ms 防抖，需要等定时器真的触发才会刷新结果
    window._search_timer.timeout.emit()
    QApplication.processEvents()
    _render(window, OUT / "search-dark.png")

    # 快速粘贴浮层
    from linglong_clip.ui.quick_paste import QuickPasteOverlay

    overlay = QuickPasteOverlay(storage, engine, _config({}))
    # show_centered() 才会触发加载；无头环境里定位到屏幕中心，随后抓图
    overlay.show_centered()
    _render(overlay, OUT / "quick-paste.png")

    # 设置页：体现新增的 OCR 与局域网选项
    dialog = SettingsDialog(_config({}), automation)
    dialog.resize(760, 640)
    _render(dialog, OUT / "settings-ocr.png")

    # 设置页「同步」页：固定/收藏的同步规则写在这里
    sync_dialog = SettingsDialog(
        _config({"sync.enabled": True, "sync.folder": "/tmp/sync"}),
        automation,
        device_status={"folder": "/tmp/sync", "devices": ["laptop", "desktop"]},
    )
    sync_dialog.resize(760, 780)
    sync_tabs = sync_dialog.findChild(QTabWidget)
    if sync_tabs is not None:
        for index in range(sync_tabs.count()):
            if "同步" in sync_tabs.tabText(index):
                sync_tabs.setCurrentIndex(index)
                break
    _render(sync_dialog, OUT / "settings-sync-flags.png")

    # 设置页「同步」页：有取消等待同步时的样子（这是本轮新增的可见性）
    _seed_pending_flag_change(storage)
    pending_dialog = SettingsDialog(
        _config({"sync.enabled": True, "sync.folder": "/tmp/sync"}),
        automation,
        device_status={"folder": "/tmp/sync", "devices": ["laptop", "desktop"]},
    )
    pending_dialog.resize(760, 780)
    pending_tabs = pending_dialog.findChild(QTabWidget)
    if pending_tabs is not None:
        for index in range(pending_tabs.count()):
            if "同步" in pending_tabs.tabText(index):
                pending_tabs.setCurrentIndex(index)
                break
    _render(pending_dialog, OUT / "settings-sync-flag-changes.png")

    # 设置页「界面」页：列表密度、关闭行为这些开关都在这里
    ui_dialog = SettingsDialog(_config({}), automation)
    ui_dialog.resize(760, 640)
    ui_tabs = ui_dialog.findChild(QTabWidget)
    if ui_tabs is not None:
        for index in range(ui_tabs.count()):
            if "外观" in ui_tabs.tabText(index):
                ui_tabs.setCurrentIndex(index)
                break
    _render(ui_dialog, OUT / "settings-interface.png")

    # 设置页「隐私」：出站防护的状态提示（本平台有什么机制、清空几秒）
    privacy_dialog = SettingsDialog(_config({}), automation)
    privacy_dialog.resize(760, 700)
    privacy_tabs = privacy_dialog.findChild(QTabWidget)
    if privacy_tabs is not None:
        for index in range(privacy_tabs.count()):
            if "隐私" in privacy_tabs.tabText(index):
                privacy_tabs.setCurrentIndex(index)
                break
    # 截图里并到「有系统机制」的那一档，否则图上看不出差别（Linux 上永远没有）。
    # 注意顺序：先设覆盖、再改数值触发刷新，否则显示的仍是探测结果。
    privacy_dialog._exclusion_formats_override = [
        "ExcludeClipboardContentFromMonitorProcessing"
    ]
    privacy_dialog._auto_clear.setValue(30)
    privacy_dialog._refresh_outbound_hint()
    _render(privacy_dialog, OUT / "settings-privacy-outbound.png")

    # 设置页「隐私 → 自定义脱敏规则」：边写边体检，写错的当场标出来。
    # 刻意放一条能命中的 + 一条写错的，让这张图说明它到底在做什么。
    rules_dialog = SettingsDialog(
        _config(
            {
                "privacy.redact_on_copy_out": True,
                "privacy.custom_redact_rules": {
                    "员工号": r"EMP\d{6}",
                    "内部工单": "[unclosed",
                },
            }
        ),
        automation,
    )
    rules_dialog.resize(760, 700)
    rules_tabs = rules_dialog.findChild(QTabWidget)
    if rules_tabs is not None:
        for index in range(rules_tabs.count()):
            if "隐私" in rules_tabs.tabText(index):
                rules_tabs.setCurrentIndex(index)
                break
    _render(rules_dialog, OUT / "settings-privacy-redact.png")

    # 列表密度对比：同一批记录在两种密度下的行高差异
    from linglong_clip.ui.main_window import MainWindow as _Window

    for density, name in (("comfortable", "density-comfortable.png"),
                          ("compact", "density-compact.png")):
        window = _Window(
            storage,
            _config({"ui.theme": "dark", "ui.card_density": density}),
            engine,
            automation,
        )
        window.resize(1180, 720)
        _render(window, OUT / name)

    # 设置页「采集」：主选择区开关与系统历史导入都在这一页
    capture_dialog = SettingsDialog(
        _config({"capture.monitor_primary": True}), automation
    )
    capture_dialog.resize(760, 700)
    # 截图里并到「支持」的那一档，否则 Linux 无头环境下永远显示禁用。
    # 注意顺序：先禁用门控、再勾选，否则 setChecked 会被探测结果抹掉。
    capture_dialog._monitor_primary.setEnabled(True)
    capture_dialog._monitor_primary.setChecked(True)
    capture_dialog._primary_status.setText(
        "可用 · 选中文字即进历史，与剪贴板记录走同一套隐私判定"
    )
    _render(capture_dialog, OUT / "settings-capture-primary.png")

    # 设置页「自动化」：体现「每条规则的时机」与「不会生效的当场标出来」
    rules_dialog = SettingsDialog(_config({}), automation)
    rules_dialog.resize(760, 640)
    rules_dialog._rules_editor.setPlainText(
        json.dumps(
            [
                {
                    "name": "自动压缩 JSON",
                    "kinds": ["text", "code"],
                    "pattern": r"^\s*[\{\[]",
                    "actions": ["json.minify"],
                    "trigger": "auto",
                },
                {
                    "name": "复制时清理链接跟踪参数",
                    "kinds": ["link"],
                    "contains": "utm_",
                    "actions": ["url.strip_tracking"],
                    "trigger": "outbound",
                },
                {
                    "name": "粘贴前转成表格",
                    "kinds": ["text"],
                    "actions": ["csv.to_markdown"],
                },
                {
                    "name": "写错触发方式的规则",
                    "kinds": ["link"],
                    "actions": ["url.strip_tracking"],
                    "trigger": "on_copy",
                },
            ],
            ensure_ascii=False,
            indent=2,
        )
    )
    rules_tabs = rules_dialog.findChild(QTabWidget)
    if rules_tabs is not None:
        rules_tabs.setCurrentIndex(4)  # 「自动化」在采集/隐私/同步/外观之后
    _render(rules_dialog, OUT / "settings-automation.png")

    # 设置页「关于」：体现原生能力自检面板
    from linglong_clip.utils.selfcheck import run_self_check

    about = SettingsDialog(_config({}), automation, native_report=run_self_check())
    about.resize(760, 640)
    tabs = about.findChild(QTabWidget)
    if tabs is not None:
        tabs.setCurrentIndex(tabs.count() - 1)  # 「关于」是最后一页
    _render(about, OUT / "settings-native.png")

    # 设置页「采集」：历史保留里新增的「收下各种写法」开关
    keep_dialog = SettingsDialog(_config({"capture.monitor_primary": True}), automation)
    keep_dialog.resize(760, 760)
    keep_dialog._monitor_primary.setEnabled(True)
    keep_dialog._monitor_primary.setChecked(True)
    keep_dialog._primary_status.setText(
        "可用 · 选中文字即进历史，与剪贴板记录走同一套隐私判定"
    )
    _render(keep_dialog, OUT / "settings-capture-formats.png")

    storage.close()
    app.quit()
    print("完成。")
    return 0


def _seed_form_item(storage) -> None:
    """造一条「剪贴板里还有别的写法」的记录，让截图体现形式取用。

    没有这条记录的话，右键菜单里那一组项在截图与文档里都无从体现 ——
    而它正是这一轮做的东西。
    """
    from linglong_clip.core.models import ClipboardItem, content_hash

    storage.add(
        ClipboardItem(
            kind="text",
            content="项目评审要点\n1. 交互稿下周二前定稿\n2. 接口文档同步更新",
            preview="项目评审要点 1. 交互稿下周二前定稿",
            source_app="chrome",
            hash=content_hash("text", "form-shot"),
            pinned=True,
            metadata={
                "formats": {
                    "data": {
                        "text/html": (
                            "<p>项目评审要点</p><ol><li>交互稿下周二前"
                            '<a href="https://cnb.cool/design">定稿</a></li>'
                            "<li>接口文档同步更新</li></ol>"
                        )
                    }
                },
                "formats_summary": "text/html",
            },
        )
    )


def _seed_pending_flag_change(storage) -> None:
    """造一条「等待同步的取消」，让截图体现同步进度而不是空状态。

    空状态看不出这条通道存在与否 —— 而「有没有传出去」正是用户最想知道的。
    """
    from linglong_clip.core.flag_changes import FLAG_PINNED, build_flag_change_store

    item = storage.list(limit=1)
    if not item:
        return
    store = build_flag_change_store()
    store.record(item[0].hash, FLAG_PINNED, False, device="")


def _config(overrides: dict):
    from linglong_clip.utils.config import Config

    home = Path(os.environ["LINGLONG_CLIPBOARD_HOME"])
    config = Config(home / f"shots-{abs(hash(str(overrides)))}.json")
    for key, value in overrides.items():
        config.set(key, value, persist=False)
    return config


if __name__ == "__main__":
    sys.exit(main())
