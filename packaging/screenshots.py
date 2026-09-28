#!/usr/bin/env python3
"""重新生成文档用的界面截图。

为什么做成脚本而不是手工截屏：手工截的图会随代码改动过期，
而 README 里的截图一旦失真就会误导读者。这里走与界面测试相同的
Qt `offscreen` 渲染路径，因此 CI 里也能跑，代码改了重新执行即可。

用法：
    QT_QPA_PLATFORM=offscreen python packaging/screenshots.py
"""

from __future__ import annotations

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

    # 设置页「关于」：体现原生能力自检面板
    from linglong_clip.utils.selfcheck import run_self_check

    about = SettingsDialog(_config({}), automation, native_report=run_self_check())
    about.resize(760, 640)
    tabs = about.findChild(QTabWidget)
    if tabs is not None:
        tabs.setCurrentIndex(tabs.count() - 1)  # 「关于」是最后一页
    _render(about, OUT / "settings-native.png")

    storage.close()
    app.quit()
    print("完成。")
    return 0


def _config(overrides: dict):
    from linglong_clip.utils.config import Config

    home = Path(os.environ["LINGLONG_CLIPBOARD_HOME"])
    config = Config(home / f"shots-{abs(hash(str(overrides)))}.json")
    for key, value in overrides.items():
        config.set(key, value, persist=False)
    return config


if __name__ == "__main__":
    sys.exit(main())
