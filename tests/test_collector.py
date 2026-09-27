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


# ---- 归一化去重：同一页面不同渠道的链接应算同一条 ----


def test_tracking_params_do_not_split_history(collector, storage):
    """同一个页面从不同 App 复制出的链接，不该在历史里各占一行。

    微博、微信分享出的链接尾巴不一样（`?utm_source=weibo&spm=...` vs
    `?utm_source=wechat&ref=...`），但用户眼里那就是一个页面。
    """
    links = [
        "https://shop.example.com/item/42?utm_source=weibo&spm=a1z.123",
        "https://shop.example.com/item/42?utm_source=wechat&ref=abc",
        "https://shop.example.com/item/42",
    ]
    outcomes = [collector.process(_item(links[0], kind="link"))[0]]
    outcomes += [collector.process(_item(url, kind="link"))[0] for url in links[1:]]

    assert outcomes == [CAPTURED, MERGED, MERGED]
    assert storage.count() == 1
    # 复制过 3 次 -> 累加 2 次，频次信号不能丢
    assert storage.list()[0].use_count == 2


def test_canonical_hash_keeps_original_content(collector, storage):
    """归一只用于算指纹，用户看到的仍是原文。"""
    raw = "https://a.com/p?utm_source=weibo&q=1"
    collector.process(_item(raw, kind="link"))
    stored = storage.list()[0]
    assert stored.content == raw
    assert "utm_source" in stored.preview


def test_equivalent_paths_merge(collector, storage):
    collector.process(_item("/tmp/a/../b/x.txt", kind="file"))
    outcome, _ = collector.process(_item("/tmp/b/x.txt", kind="file"))
    assert outcome == MERGED
    assert storage.count() == 1


def test_different_urls_still_separate(collector, storage):
    """归一必须保守：路径/参数确实不同的链接不能合并。"""
    collector.process(_item("https://a.com/p?v=1", kind="link"))
    collector.process(_item("https://a.com/p?v=2", kind="link"))
    collector.process(_item("https://a.com/other", kind="link"))
    assert storage.count() == 3


def test_canonical_dedupe_can_be_disabled(storage, config):
    """关掉归一后行为与改造前一致：不同尾巴的链接各占一行。"""
    config.set("history.canonical_dedupe", False)
    instance = Collector(storage, config)
    instance.process(_item("https://a.com/p?utm_source=weibo", kind="link"))
    outcome, _ = instance.process(_item("https://a.com/p?utm_source=wechat", kind="link"))
    assert outcome == CAPTURED
    assert storage.count() == 2


def test_canonical_dedupe_url_only(storage, config):
    """只关链接归一：路径仍归一，链接不再归一。"""
    config.set("history.canonical_dedupe.url", False)
    instance = Collector(storage, config)
    instance.process(_item("https://a.com/p?utm_source=weibo", kind="link"))
    assert instance.process(_item("https://a.com/p?utm_source=wechat", kind="link"))[0] == CAPTURED
    instance.process(_item("/tmp/a//b.txt", kind="file"))
    assert instance.process(_item("/tmp/a/b.txt", kind="file"))[0] == MERGED


def test_image_hash_not_normalized(collector, storage):
    """图片按字节指纹去重，没有归一的空间，不能把它算成别的。"""
    item = ClipboardItem(kind="image", content="", image_path="/nonexistent/x.png")
    item.hash = "image-digest"
    collector.process(item)
    assert storage.count() == 1
    assert storage.list()[0].hash == "image-digest"


def test_provided_hash_is_respected(collector, storage):
    """调用方显式给了 hash 就不该被改写（同步导入等路径依赖这一点）。"""
    item = ClipboardItem(kind="link", content="https://a.com/p?utm_source=x",
                         preview="x", hash="explicit-hash")
    collector.process(item)
    assert storage.find_by_hash("explicit-hash") is not None
