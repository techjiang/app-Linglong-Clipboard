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

    # 设置页「采集」：图片附件面板（占用 + 可回收的张数）
    # 造几张没人引用的图，让「有多少是垃圾」这件事在截图里看得见
    _seed_orphan_images(storage)
    image_dialog = SettingsDialog(_config({}), automation, storage=storage)
    image_dialog.resize(760, 760)
    _render(image_dialog, OUT / "settings-images.png")

    # 设置页「采集」：图片附件面板 —— 「同步过来但本机还没有那份字节」的样子。
    # 这一档此前在界面上完全看不见：用户只会在另一台机器上看到一张打不开的图。
    _seed_pending_images(storage, count=4)
    pending_image_dialog = SettingsDialog(_config({}), automation, storage=storage)
    pending_image_dialog.resize(760, 780)
    _render(pending_image_dialog, OUT / "settings-images-pending.png")

    # 命令行输出也做成图：`--images-pending` / `--sync-images` 这条链路
    # 没有界面可截，而它正是这一轮最需要被看见的部分。
    # 内容是**真实跑出来的**（同一份数据、同一条代码路径），不是手写的字面量。
    _render_terminal(
        _capture_image_cli(storage),
        OUT / "cli-image-fetch.png",
        title="linglong-clipboard — 图片按需补图",
    )

    storage.close()
    app.quit()
    print("完成。")
    return 0


def _capture_image_cli(storage) -> list:
    """真跑一遍「按需补图」的完整链路，把输出抓回来。

    为什么要「真跑」而不是照着输出写一份字面量：截图一旦与实现脱节就会误导
    读者，而这个仓库里已经吃过一次这个亏（README 承诺的入口实际不存在）。
    真的执行、真的捕获输出，图里的每一行就都是程序说的。

    **两台设备 + 真实 socket**：`--images-pending` 只读本地记录，
    单独截它说明不了「字节怎么过来的」；而这一段正是这轮的重点。
    所以这里真起一个对端服务，真走一次 HTTP 拉取。
    """
    import contextlib
    import io
    import socket as _socket
    import threading

    from linglong_clip.core.lan_sync import LanSync
    from linglong_clip.core.privacy import PrivacyGuard
    from linglong_clip.core.storage import Storage

    lines: list = []

    def run(args, label=None):
        from linglong_clip.__main__ import main as cli_main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            try:
                cli_main(args)
            except SystemExit:
                pass
        lines.append(f"$ linglong-clipboard {' '.join(args)}")
        lines.extend(buffer.getvalue().splitlines())
        lines.append("")

    run(["--images-pending"])

    # 起一个只读对端，把图片字节真的提供出去
    peer_home = Path(os.environ["LINGLONG_CLIPBOARD_HOME"]) / "peer-device"
    peer_home.mkdir(exist_ok=True)
    peer = Storage(peer_home / "history.db")
    from linglong_clip.core import image_transfer
    from linglong_clip.core.models import IMAGE_DIGEST_KEY, ClipboardItem, content_hash

    for index in range(1, 5):
        # 摘要必须**由内容算出来**：图片按内容寻址，写死的十六进制串与字节
        # 对不上时，对端会在外发前就拒绝（第一版截图正是这么错的 ——
        # 图里全是「内容与摘要不符」，反倒说明校验是有效的）。
        payload = _peer_image_payload(index)
        digest = image_transfer.digest_of(payload)
        (peer.images_dir / f"{digest}.png").write_bytes(payload)
        peer.add(
            ClipboardItem(
                kind="image",
                content=f"{digest}.png",
                preview="[图片 1920×1080]",
                hash=content_hash("image", f"peer{index}"),
                image_path=f"images/{digest}.png",
                metadata={"ocr": {"text": "", "backend": "stub"}},
            )
        )
    node = LanSync(storage=peer, device_name="desktop", token="", port=0,
                   guard=PrivacyGuard())
    node.start()

    # 让 CLI 走真实 HTTP：临时把配置指到刚起的这个对端
    from linglong_clip.utils.config import Config

    cfg_path = Config().path
    saved = None
    if cfg_path.exists():
        saved = cfg_path.read_text(encoding="utf-8")
    config = Config(cfg_path)
    config.update(
        {
            "sync.enabled": True,
            "sync.mode": "lan",
            "sync.lan_peers": [f"127.0.0.1:{node.status()['port']}"],
        }
    )
    try:
        run(["--sync-images"])
    finally:
        node.stop()
        peer.close()
        if saved is not None:
            cfg_path.write_text(saved, encoding="utf-8")
        elif cfg_path.exists():
            cfg_path.unlink()
    return lines


def _render_terminal(lines: list, target: Path, *, title: str = "") -> None:
    """把命令行输出渲染成一张图。

    用 PIL 而不是 Qt：这是一段纯文本，Qt 的富文本排版反而会让
    「等宽」这个前提失效，而命令行输出对不齐就完全看不懂了。
    """
    from PIL import Image, ImageDraw, ImageFont

    # WenQuanYi Zen Hei Mono：装了 CJK 字形的等宽字体。
    # 用 DejaVuSansMono 的话中文会全部变成豆腐块 —— 而这里的输出是中文的。
    font_path = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
    size = 15
    try:
        font = ImageFont.truetype(font_path, size)
    except OSError:  # 没装中文字体时退回 DejaVu，至少 ASCII 部分还看得见
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", size
        )

    line_height = int(size * 1.65)
    pad = 18
    width = 92
    text = "\n".join(lines)
    longest = max((len(line) for line in lines), default=40)

    image = Image.new("RGB", (1, 1))
    probe = ImageDraw.Draw(image)
    # 中文是双宽字符，按字符数估算宽度会切掉后半截；用真实测量。
    measured = max(
        (probe.textlength(line, font=font) for line in lines), default=600
    )
    width = int(max(measured + pad * 2, 620))
    height = line_height * (len(lines) + 1) + pad * 2

    image = Image.new("RGB", (width, height), (24, 26, 33))
    draw = ImageDraw.Draw(image)
    if title:
        draw.text((pad, pad // 2), title, fill=(120, 128, 148), font=font)
    y = pad + line_height
    for line in lines:
        # 命令与提示行染色，让「输入」和「输出」分得开
        color = (110, 200, 160) if line.startswith("$") else (198, 205, 220)
        if line.strip().startswith("缺失") or line.strip().startswith("其中"):
            color = (230, 200, 120)
        draw.text((pad, y), line, fill=color, font=font)
        y += line_height
    image.save(str(target))
    print(f"  ✓ {target.relative_to(ROOT)} ({image.width}x{image.height})")


def _peer_image_payload(index: int) -> bytes:
    """「对端那台设备」上第 `index` 张图的字节。

    抽成函数是为了让截图里的两台设备用**同一份**字节 —— 图片按内容寻址，
    造数据时两边对不上，症状会是「内容与摘要不符」，看着像代码坏了。
    """
    return b"\x89PNG\r\n\x1a\n" + bytes(2048 * index)


def _seed_pending_images(storage, count: int = 4) -> None:
    """造「同步过来的记录说有图、但本机还没有那份文件」的状态。

    这是按需补图的起点，也是这轮新增的那个可见的事实：
    字节不进同步日志，所以「记录有、字节没有」是一种**正常状态**，
    而不是数据损坏 —— 它必须在界面上看得见，否则另一台机器上
    只会出现一张打不开的图，用户无从下手。
    """
    from linglong_clip.core import image_transfer
    from linglong_clip.core.models import IMAGE_DIGEST_KEY, ClipboardItem, content_hash

    now = time.time()
    for index in range(count):
        # 摘要必须与「对端那台设备」上的字节对得上（图片按内容寻址），
        # 所以两边共用 `_peer_image_payload()`。各写一份字面量迟早对不上，
        # 而那时的症状是「内容与摘要不符」，看起来像 bug，其实是造的数据不成立。
        digest = image_transfer.digest_of(_peer_image_payload(index + 1))
        storage.add(
            ClipboardItem(
                kind="image",
                content=f"{digest}.png",
                preview=f"[图片 1920×1080] 来自桌面设备",
                hash=content_hash("image", f"pending{index}"),
                source_app="shared-clipboard",
                created_at=now - index * 180,
                metadata={IMAGE_DIGEST_KEY: digest},
            )
        )


def _seed_orphan_images(storage, count: int = 6) -> None:
    """往 `images/` 里放几张没有记录引用的图。

    「图片附件」面板最该展示的就是「有多少没被引用」，而这个数在
    seed 出来的干净数据里恒为 0 —— 不特意造出垃圾，截图就什么都说明不了。
    """
    images = storage.images_dir
    images.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        # 单张 ~420KB，让「占用多少」这件事在截图里看得出量级
        (images / f"orphan-{index}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(420_000))


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
