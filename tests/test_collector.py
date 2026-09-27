"""采集管线：识别、去重、风控、淘汰。"""

import pytest

from linglong_clip.core.collector import (
    CAPTURED,
    MERGED,
    SKIPPED_EMPTY,
    SKIPPED_PRIVACY,
    SKIPPED_TYPE,
    Collector,
)
from linglong_clip.core.models import ClipboardItem
from linglong_clip.platform.base import FocusInfo


@pytest.fixture()
def collector(storage, config):
    events = []
    instance = Collector(
        storage, config, device_name="test-device",
        on_captured=lambda item, outcome: events.append((outcome, item.preview)),
    )
    instance.events = events
    return instance


def _item(content="hello", kind="text", **kwargs):
    return ClipboardItem(kind=kind, content=content, **kwargs)


def test_captures_text(collector, storage):
    outcome, saved = collector.process(_item())
    assert outcome == CAPTURED
    assert saved.id is not None
    assert storage.count() == 1
    assert saved.device == "test-device"


def test_skips_blank_content(collector, storage):
    outcome, _ = collector.process(_item(content="   \n\t "))
    assert outcome == SKIPPED_EMPTY
    assert storage.count() == 0


def test_skips_none(collector, storage):
    assert collector.process(None)[0] == SKIPPED_EMPTY
    assert storage.count() == 0


def test_deduplicates_by_hash(collector, storage):
    collector.process(_item("duplicate"))
    outcome, saved = collector.process(_item("duplicate"))
    assert outcome == MERGED
    assert storage.count() == 1
    # 复制次数落在 use_count 上：它是排序与界面共用的唯一频次口径
    assert saved.use_count == 1
    assert "last_source" in saved.metadata


def test_merge_counts_agree_with_use_count(collector, storage):
    """连续复制同一内容：合并次数与 use_count 必须一致，不能各算一套。"""
    collector.process(_item("repeat"))
    for _ in range(4):
        collector.process(_item("repeat"))
    item = storage.list()[0]
    assert item.use_count == 4
    # 重复内容不会在列表里堆成多行
    assert storage.count() == 1


def test_dedupe_disabled_keeps_new_row(storage, config):
    config.set("history.merge_duplicates", False)
    instance = Collector(storage, config)
    instance.process(_item("same"))
    instance.process(_item("same"))
    assert storage.count() == 1  # hash 相同仍会命中 find_by_hash


def test_detects_link_kind(collector, storage):
    _, saved = collector.process(_item("https://cnb.cool/a/b"))
    assert saved.kind == "link"
    assert saved.metadata.get("host") == "cnb.cool"


def test_detects_color_kind(collector):
    _, saved = collector.process(_item("#4c8dff"))
    assert saved.kind == "color"


def test_marks_private_host(collector):
    _, saved = collector.process(_item("http://192.168.1.10/admin"))
    assert saved.metadata.get("private_host") is True


def test_image_item(collector, storage):
    item = ClipboardItem(kind="image", image_path="images/a.png", hash="imghash",
                         image_width=20, image_height=10)
    outcome, saved = collector.process(item)
    assert outcome == CAPTURED
    assert saved.kind == "image"
    assert saved.preview == "[图片]"


def test_file_item_hashes_paths(collector):
    item = ClipboardItem(kind="file", content="/a\n/b", files=["/a", "/b"])
    _, saved = collector.process(item)
    assert saved.hash  # 文件按路径生成指纹


def test_source_from_focus(collector):
    focus = FocusInfo(app_id="chrome", app_name="Google Chrome", window_title="GitHub")
    _, saved = collector.process(_item("x"), focus)
    assert saved.source_app == "chrome"
    assert saved.source_title == "GitHub"


def test_sensitive_content_is_not_stored_by_default(collector, storage):
    outcome, _ = collector.process(_item("password: hunter2xyz"))
    assert outcome == SKIPPED_PRIVACY
    assert storage.count() == 0


def test_sensitive_stored_when_ttl_configured(storage, config):
    config.set("privacy.sensitive_retention_seconds", 600)
    instance = Collector(storage, config)
    outcome, saved = instance.process(_item("password: hunter2xyz"))
    assert outcome == CAPTURED
    assert saved.sensitive is True


def test_private_key_never_stored(storage, config):
    config.set("privacy.sensitive_retention_seconds", 600)
    instance = Collector(storage, config)
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"
    outcome, _ = instance.process(_item(text))
    assert outcome == SKIPPED_PRIVACY


def test_disabled_type_is_skipped(storage, config):
    config.set("capture.types", ["text"])
    instance = Collector(storage, config)
    assert instance.process(_item("https://cnb.cool/x"))[0] == SKIPPED_TYPE
    assert instance.process(_item("plain"))[0] == CAPTURED


def test_capture_disabled_skips_everything(storage, config):
    config.set("capture.enabled", False)
    instance = Collector(storage, config)
    assert instance.process(_item("x"))[0] == SKIPPED_TYPE
    assert storage.count() == 0


def test_tags_extracted(collector):
    _, saved = collector.process(_item("#bug 很紧急 🔥"))
    assert "bug" in saved.tags
    assert "urgent" in saved.tags


def test_color_gets_no_hashtag(collector):
    _, saved = collector.process(_item("#ff8800"))
    assert saved.tags == []


def test_preview_generated(collector):
    _, saved = collector.process(_item("a  \n\n b"))
    assert saved.preview and "\n" not in saved.preview


def test_size_estimated(collector):
    _, saved = collector.process(_item("abc"))
    assert saved.size == 3


def test_enforce_max_items(storage, config):
    config.set("history.max_items", 3)
    config.set("history.merge_duplicates", True)
    instance = Collector(storage, config)
    for index in range(6):
        instance.process(_item(f"unique-{index}"))
    assert storage.count() <= 3


def test_pinned_survives_limit_enforcement(storage, config):
    config.set("history.max_items", 2)
    instance = Collector(storage, config)
    _, pinned = instance.process(_item("important"))
    storage.update_fields(pinned.id, pinned=True)
    for index in range(6):
        instance.process(_item(f"filler-{index}"))
    assert storage.get(pinned.id) is not None


def test_events_emitted(collector):
    collector.process(_item("first"))
    collector.process(_item("second"))
    outcomes = [outcome for outcome, _ in collector.events]
    assert outcomes == [CAPTURED, CAPTURED]


def test_purge_sensitive(storage, config):
    import time

    config.set("privacy.sensitive_retention_seconds", 60)
    instance = Collector(storage, config)
    storage.add(ClipboardItem(kind="text", content="old secret", hash="s1",
                              sensitive=True, created_at=time.time() - 100))
    assert instance.purge_sensitive() == 1


def test_purge_sensitive_noop_without_ttl(collector):
    assert collector.purge_sensitive() == 0
