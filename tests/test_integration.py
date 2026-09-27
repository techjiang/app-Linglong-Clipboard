"""端到端：从剪贴板读取到落库、检索、转换、写回。"""

from __future__ import annotations

import os
import time

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
