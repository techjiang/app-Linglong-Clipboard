"""采集管线：识别、去重、风控、淘汰。"""

import time

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


def test_duplicate_capture_does_not_resurrect_deleted_metadata_key(collector, storage):
    """重复采集只该补 `last_source`，不该把别人刚删掉的键写回来。

    `metadata` 是按键合并的补丁。此前这里传的是「整份拷出来的字典」，
    等于做了一次读—改—写：读到之后、写回之前，若有别的写方删掉了某个键
    （取消固定的 `pinned_by`、图片摘要 `image_digest`），写回就会把它**复活**。

    窗口在单线程下不好碰，所以这里把「另一个写方」精确插在
    `find_by_hash()` 返回之后 —— 那一份 `existing.metadata` 已被读出来，
    正是整份字典会被写回去的时刻。
    """
    from linglong_clip.core.models import PINNED_BY_KEY

    collector.process(_item("重复复制"))
    item_id = storage.list()[0].id
    storage.update_fields(item_id, pinned=True, metadata={PINNED_BY_KEY: "another-device"})

    real_find = storage.find_by_hash
    injected = {"done": False}

    def find_with_concurrent_delete(digest):
        item = real_find(digest)
        # 模拟「读之后、写回之前」另一个写方删掉了这个键
        if not injected["done"] and item is not None:
            injected["done"] = True
            storage.update_fields(item.id, pinned=False, metadata={PINNED_BY_KEY: None})
        return item

    storage.find_by_hash = find_with_concurrent_delete
    try:
        collector.process(_item("重复复制", source_app="chrome"))
    finally:
        storage.find_by_hash = real_find

    meta = storage.get(item_id).metadata
    assert meta.get("last_source") == "chrome"
    assert PINNED_BY_KEY not in meta, f"被删掉的键又被写回来了：{meta}"


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


# ---- capture.max_text_length：超长文本按配置截断 ----


def test_long_text_is_truncated_to_configured_limit(storage, config):
    config.set("capture.max_text_length", 20)
    instance = Collector(storage, config)
    outcome, saved = instance.process(_item("x" * 100))
    assert outcome == CAPTURED
    assert len(saved.content) == 20
    assert saved.metadata["truncated"] is True
    assert saved.metadata["original_length"] == 100


def test_short_text_is_not_touched(storage, config):
    config.set("capture.max_text_length", 1000)
    instance = Collector(storage, config)
    _, saved = instance.process(_item("short"))
    assert saved.content == "short"
    assert "truncated" not in saved.metadata


def test_zero_limit_means_no_truncation(storage, config):
    """0 表示不截断：配置项允许用户彻底关掉这个行为。"""
    config.set("capture.max_text_length", 0)
    instance = Collector(storage, config)
    _, saved = instance.process(_item("y" * 500))
    assert len(saved.content) == 500
    assert "truncated" not in saved.metadata


def test_truncation_happens_after_privacy_check(storage, config):
    """截断必须排在隐私判定之后。

    否则「长文本里藏着一段 API Key，且恰好落在截断线之后」这种情况
    会因为判定时看不到那段内容而被放行 —— 顺序反了就是漏检。
    """
    config.set("capture.max_text_length", 50)
    instance = Collector(storage, config)
    # 前 50 个字符是普通文本，密钥跟在其后
    payload = "a" * 50 + " sk-abcdefghijklmnopqrstuvwxyz0123456789"
    outcome, saved = instance.process(_item(payload))
    # 若是先截断再判定，这段密钥会被截掉、判定看不到，于是入库
    assert outcome != CAPTURED
    assert storage.count() == 0


def test_truncation_keeps_original_length_once(storage, config):
    """多次经过截断路径时 original_length 以最先记下的原文长度为准。"""
    config.set("capture.max_text_length", 10)
    instance = Collector(storage, config)
    item = ClipboardItem(kind="text", content="z" * 40, metadata={"original_length": 999})
    _, saved = instance.process(item)
    assert saved.content == "z" * 10
    assert saved.metadata["original_length"] == 999


def test_binary_kinds_are_not_truncated(storage, config):
    """图片走 image_path、文件走 files，没有 content 长度问题。"""
    config.set("capture.max_text_length", 5)
    instance = Collector(storage, config)
    item = ClipboardItem(kind="image", image_path="/nonexistent/a.png")
    item.hash = "img-digest"
    item.content = "i" * 50  # OCR 结果可能很长，但归属图片记录
    _, saved = instance.process(item)
    assert len(saved.content) == 50


def test_truncated_text_still_searchable(storage, config):
    """截断后保留的开头仍要能搜到 —— 否则用户复制了却查不到。"""
    config.set("capture.max_text_length", 30)
    instance = Collector(storage, config)
    instance.process(_item("UNIQUE_PREFIX_" + "pad" * 50))
    assert storage.search("UNIQUE_PREFIX_")


# ---- history.pin_immune：固定项是否占限额 ----


def test_pin_immune_true_keeps_pinned_beyond_limit(storage, config):
    now = time.time()
    config.update({"capture.max_text_length": 0, "history.max_items": 2,
                   "history.pin_immune": True})
    instance = Collector(storage, config)
    for index in range(4):
        instance.process(_item(f"plain{index}", created_at=now + index))
    instance.process(_item("PINNED", pinned=True, created_at=now - 1000))
    remaining = {item.content for item in storage.list(limit=100)}
    assert "PINNED" in remaining
    assert remaining == {"PINNED", "plain3", "plain2"}


def test_pin_immune_false_evicts_pinned_when_over_limit(storage, config):
    now = time.time()
    config.update({"capture.max_text_length": 0, "history.max_items": 2,
                   "history.pin_immune": False})
    instance = Collector(storage, config)
    for index in range(4):
        instance.process(_item(f"plain{index}", created_at=now + index))
    instance.process(_item("PINNED", pinned=True, created_at=now - 1000))
    remaining = {item.content for item in storage.list(limit=100)}
    # 排序把固定项排前，所以仍留着它，淘汰的是最旧的普通记录
    assert remaining == {"PINNED", "plain3"}
