"""端到端：从剪贴板读取到落库、检索、转换、写回。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from linglong_clip.core.automation import AutomationEngine  # noqa: E402
from linglong_clip.core.collector import CAPTURED, SKIPPED_PRIVACY, Collector  # noqa: E402
from linglong_clip.core.models import ClipboardItem  # noqa: E402
from linglong_clip.core.privacy import PrivacyGuard  # noqa: E402
from linglong_clip.core.semantic import SemanticEngine  # noqa: E402
from linglong_clip.platform.base import FocusInfo  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def pipeline(storage, config, home, monkeypatch):
    from linglong_clip.ui.clipboard_qt import QtClipboardAdapter

    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home))
    app = QApplication.instance() or QApplication([])
    adapter = QtClipboardAdapter(app.clipboard(), home / "images")
    collector = Collector(storage, config, PrivacyGuard(config), device_name="itest")
    return app, adapter, collector, storage, config


def test_full_capture_to_search_flow(pipeline):
    app, adapter, collector, storage, config = pipeline

    app.clipboard().setText("端到端测试：https://cnb.cool/e2e")
    item = adapter.read_clipboard()
    outcome, saved = collector.process(
        item, FocusInfo(app_id="chrome", app_name="Chrome", window_title="CNB")
    )

    assert outcome == CAPTURED
    assert saved.source_app == "chrome"

    engine = SemanticEngine(storage)
    assert len(engine.search("端到端")) == 1
    # 这句话整体是文本（URL 只是其中一段），不是纯链接
    assert saved.kind == "text"

    # 换成纯链接才应归类为 link，并被「来自 chrome 的链接」命中
    app.clipboard().setText("https://cnb.cool/pure-link")
    pure = adapter.read_clipboard()
    _, link_item = collector.process(pure, FocusInfo(app_id="google-chrome"))
    assert link_item.kind == "link"
    assert engine.search("来自 chrome 的链接")


def test_capture_image_then_transform_not_applicable(pipeline):
    app, adapter, collector, storage, config = pipeline
    from PySide6.QtGui import QColor, QImage

    image = QImage(30, 20, QImage.Format.Format_RGB32)
    image.fill(QColor("#4c8dff"))
    app.clipboard().setImage(image)

    item = adapter.read_clipboard()
    outcome, saved = collector.process(item)
    assert outcome == CAPTURED
    assert saved.image_width == 30

    automation = AutomationEngine()
    result = automation.apply("json.format", saved)
    assert result.ok is False  # 图片没有可转换的文本


def test_sensitive_flow_is_blocked_end_to_end(pipeline):
    app, adapter, collector, storage, config = pipeline

    app.clipboard().setText("password: hunter2xyz")
    item = adapter.read_clipboard()
    outcome, _ = collector.process(item)

    assert outcome == SKIPPED_PRIVACY
    assert storage.count() == 0


def test_roundtrip_through_clipboard_preserves_hash(pipeline):
    app, adapter, collector, storage, config = pipeline

    app.clipboard().setText("往返内容 roundtrip")
    first = adapter.read_clipboard()
    collector.process(first)

    # 从历史取出，写回剪贴板，再读一次：指纹必须一致
    saved = storage.list()[0]
    app.clipboard().clear()
    adapter.write_item(saved)
    second = adapter.read_clipboard()
    assert second.hash == saved.hash


def test_dedupe_across_two_reads(pipeline):
    app, adapter, collector, storage, config = pipeline

    app.clipboard().setText("重复内容")
    collector.process(adapter.read_clipboard())
    collector.process(adapter.read_clipboard())

    assert storage.count() == 1


def test_ocr_metadata_style_enrichment(pipeline):
    """链接类记录应带上域名元数据，供来源分组与内网判定使用。"""
    app, adapter, collector, storage, config = pipeline

    app.clipboard().setText("https://cnb.cool/internal?utm_source=chat")
    _, saved = collector.process(adapter.read_clipboard())

    assert saved.kind == "link"
    assert saved.metadata["host"] == "cnb.cool"


def test_auto_rule_applies_on_capture(pipeline):
    app, adapter, collector, storage, config = pipeline
    automation = AutomationEngine()
    automation.load_from_config(
        [
            {
                "name": "压缩 JSON",
                "kinds": ["text", "code"],
                "pattern": r"^\s*[\{\[]",
                "actions": ["json.minify"],
                "trigger": "auto",
            }
        ]
    )

    app.clipboard().setText('{ "a" : 1, "b" : 2 }')
    _, saved = collector.process(adapter.read_clipboard())

    results = automation.auto_actions_for(saved)
    assert len(results) == 1
    assert results[0].output == '{"a":1,"b":2}'


def test_tags_survive_storage_roundtrip(pipeline):
    app, adapter, collector, storage, config = pipeline

    app.clipboard().setText("#bug 复现步骤见附件 🔥")
    _, saved = collector.process(adapter.read_clipboard())

    reloaded = storage.get(saved.id)
    assert "bug" in reloaded.tags
    assert "urgent" in reloaded.tags


# ---- OCR 端到端：截图 → 文字 → 检索 ----


class _StubOcrBackend:
    """按文件名决定识别结果，避免端到端测试依赖系统装了 tesseract。"""

    name = "stub"

    def __init__(self, text: str):
        self.text = text

    def available(self) -> bool:
        return True

    def languages(self):
        return ("eng",)

    def recognize(self, image_path, languages):
        from linglong_clip.core.ocr import OcrResult

        return OcrResult(text=self.text, confidence=93.0, language=languages, backend=self.name)


def _image_item(home, name="shot.png", digest="img"):
    """造一张真实的 PNG，让 OcrEngine 的「文件存在」检查能通过。"""
    from PySide6.QtGui import QColor, QImage

    target = home / name
    image = QImage(32, 32, QImage.Format.Format_RGB32)
    image.fill(QColor("#ffffff"))
    image.save(str(target))
    return ClipboardItem(
        kind="image", image_path=str(target), hash=digest, preview="", image_width=32, image_height=32
    )


@pytest.fixture()
def ocr_pipeline(storage, config, home):
    from linglong_clip.core.ocr import OcrEngine

    collector = Collector(
        storage, config, PrivacyGuard(config), ocr=OcrEngine(backend=_StubOcrBackend("release notes 2024"))
    )
    return collector, storage


def test_screenshot_text_is_searchable(ocr_pipeline):
    collector, storage = ocr_pipeline

    outcome, saved = collector.process(_image_item(Path(storage.path).parent))
    assert outcome == CAPTURED
    assert saved.kind == "image"
    # 识别文本进入 content 后，即可被全文检索命中
    assert saved.content == "release notes 2024"
    assert "release" in saved.preview

    engine = SemanticEngine(storage)
    hits = engine.search("release")
    assert len(hits) == 1
    assert hits[0].kind == "image"


def test_screenshot_with_secret_is_blocked(storage, config, home):
    """截图里是密钥时必须和纯文本一样被拦下，不能因为「是图片」就放行。"""
    from linglong_clip.core.ocr import OcrEngine

    collector = Collector(
        storage,
        config,
        PrivacyGuard(config),
        ocr=OcrEngine(backend=_StubOcrBackend("token: sk-abcdefghijklmnopqrstuvwxyz")),
    )

    outcome, saved = collector.process(_image_item(home, "secret.png", "img-secret"))
    assert outcome == SKIPPED_PRIVACY
    assert saved is None
    assert storage.count() == 0


def test_screenshot_without_ocr_still_captured(storage, config, home):
    collector = Collector(storage, config, PrivacyGuard(config))
    outcome, saved = collector.process(_image_item(home, "plain.png", "img-plain"))

    assert outcome == CAPTURED
    assert saved.content == ""
    assert saved.preview == "[图片]"


def test_semantic_search_mixes_text_and_ocr_results(ocr_pipeline):
    collector, storage = ocr_pipeline
    collector.process(_image_item(Path(storage.path).parent, "a.png", "img-a"))
    collector.process(ClipboardItem(kind="text", content="release 计划", hash="t-1"))

    engine = SemanticEngine(storage)
    hits = engine.search("release")
    assert {hit.kind for hit in hits} == {"image", "text"}


# ---- 归一化去重端到端：采集 -> 检索 -> 合并 ----


def test_channel_links_collapse_end_to_end(storage, config):
    """同一页面从三个渠道各复制一次，最终只有一条，且检索得到。"""
    collector = Collector(storage, config)
    for url in (
        "https://shop.example.com/item/42?utm_source=weibo&spm=a1z.123",
        "https://shop.example.com/item/42?utm_source=wechat&ref=abc",
        "https://shop.example.com/item/42",
    ):
        collector.process(ClipboardItem(kind="link", content=url, preview=url))

    assert storage.count() == 1
    hits = SemanticEngine(storage).search("item/42")
    assert len(hits) == 1
    assert hits[0].use_count == 2


def test_legacy_records_repaired_by_cli_flow(storage, config):
    """升级前入库的重复链接，经「重算指纹 + 合并」后收敛成一条。

    这是 recanonicalize 的完整用途：新规则只对新数据生效，
    历史数据必须走一遍重算才能被正确判重。
    """
    from linglong_clip.core.models import content_hash

    urls = [
        "https://a.com/p?utm_source=weibo",
        "https://a.com/p?utm_source=wechat",
        "https://a.com/p",
    ]
    for index, url in enumerate(urls):
        storage.add(
            ClipboardItem(
                kind="link", content=url, preview=url,
                hash=content_hash("link", url), created_at=100 + index,
            )
        )

    assert storage.recanonicalize() == 2
    assert storage.dedupe() == 2
    assert storage.count() == 1
    # 原始文本未被改写
    assert storage.list()[0].content in urls


# ------------------------------------------------- 主选择区（选中即记录）

class _PrimaryOnlyBackend:
    """只提供主选择区读取的后端，模拟「选中一段文字，没按 Ctrl+C」。"""

    name = "linux"

    def __init__(self, text: str = ""):
        self.text = text

    def read_primary_text(self):
        return self.text

    def capabilities(self):
        return {"primary_selection": True}

    def current_focus(self):
        return FocusInfo()


def test_primary_selection_reaches_storage_and_search(storage, config, home):
    """端到端：选中即被记下，并且能被检索到。

    这是整条链路的重点 —— 之前 `read_primary()` 只被测试调用过，
    monitor 从来不读主选择区，所以「选中即记录」从来没有真正发生过。
    """
    from linglong_clip.core.monitor import ClipboardMonitor

    backend = _PrimaryOnlyBackend("随手选中的一段配置说明")
    collector = Collector(storage, config, PrivacyGuard(config), device_name="itest")
    captured = []
    clock = [1000.0]

    monitor = ClipboardMonitor(
        type("C", (), {"read_clipboard": lambda self: None})(),
        interval_ms=60,
        backend=backend,
        monitor_primary=True,
        primary_interval_ms=500,
        on_capture=lambda item: captured.append(
            collector.process(item, FocusInfo(app_id="gedit"))
        ),
        clock=lambda: clock[0],
    )

    monitor._poll_primary_once()
    assert len(captured) == 1
    outcome, saved = captured[0]
    assert outcome == CAPTURED
    assert saved.source_app == "gedit"

    engine = SemanticEngine(storage)
    assert len(engine.search("随手选中")) == 1


def test_primary_selection_goes_through_privacy_guard(storage, config, home):
    """主选择区不是绕过隐私判定的后门。

    选中一段私钥时，行为和「复制一段私钥」必须完全一致 ——
    两条通道最终都走 `Collector.process()`，这才是有保证的。
    """
    from linglong_clip.core.monitor import ClipboardMonitor

    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    backend = _PrimaryOnlyBackend(pem)
    collector = Collector(storage, config, PrivacyGuard(config), device_name="itest")
    results = []
    clock = [1000.0]

    monitor = ClipboardMonitor(
        type("C", (), {"read_clipboard": lambda self: None})(),
        interval_ms=60,
        backend=backend,
        monitor_primary=True,
        primary_interval_ms=500,
        on_capture=lambda item: results.append(
            collector.process(item, FocusInfo(app_id="vim"))
        ),
        clock=lambda: clock[0],
    )
    monitor._poll_primary_once()

    assert len(results) == 1
    assert results[0][0] == SKIPPED_PRIVACY
    assert storage.count() == 0


def test_primary_selection_dedupes_against_clipboard(storage, config, home):
    """同一段内容先被选中、再被复制，应合并成一条而不是两条。"""
    from linglong_clip.core.monitor import ClipboardMonitor
    from linglong_clip.core.models import content_hash

    collector = Collector(storage, config, PrivacyGuard(config), device_name="itest")
    text = "同一段内容"
    clock = [1000.0]

    backend = _PrimaryOnlyBackend(text)
    monitor = ClipboardMonitor(
        type("C", (), {"read_clipboard": lambda self: None})(),
        interval_ms=60,
        backend=backend,
        monitor_primary=True,
        primary_interval_ms=500,
        on_capture=lambda item: collector.process(item),
        clock=lambda: clock[0],
    )
    monitor._poll_primary_once()

    # 随后用户按了 Ctrl+C，走的还是同一套指纹
    from linglong_clip.core.models import ClipboardItem

    copied = ClipboardItem(kind="text", content=text, hash=content_hash("text", text))
    outcome, _ = collector.process(copied)
    assert outcome == "merged"
    assert storage.count() == 1
