"""存储层：增删改查、全文检索、淘汰策略。"""

import time

import pytest

from linglong_clip.core.models import ClipboardItem


def _add(storage, **kwargs):
    defaults = {"kind": "text", "content": "content", "preview": "content"}
    defaults.update(kwargs)
    defaults.setdefault("hash", f"h-{kwargs.get('content', 'content')}-{kwargs.get('created_at', 0)}")
    return storage.add(ClipboardItem(**defaults))


def test_fts_enabled_with_tokenizer(storage):
    assert storage.fts_enabled is True
    assert storage.fts_tokenizer in {"trigram", "unicode61"}


def test_add_and_get(storage):
    saved = _add(storage, content="hello", hash="h1")
    assert saved.id is not None
    assert storage.get(saved.id).content == "hello"
    assert storage.count() == 1


def test_search_chinese_and_english(storage):
    _add(storage, content="玲珑剪切板的历史记录", hash="a")
    _add(storage, content="another clipboard entry", hash="b")

    assert len(storage.search("玲珑剪")) == 1
    assert len(storage.search("clipboard")) == 1
    assert len(storage.search("nonexistent-xyz")) == 0


def test_search_short_query_falls_back_to_like(storage):
    # trigram 分词器查不到 2 字中文，必须由 LIKE 兜底
    _add(storage, content="玲珑剪切板的历史记录", hash="a")
    assert len(storage.search("剪切")) == 1


def test_search_empty_query_lists_all(storage):
    _add(storage, content="one", hash="1")
    _add(storage, content="two", hash="2")
    assert len(storage.search("")) == 2


def test_search_filtered_by_kind(storage):
    _add(storage, content="cnb.cool", kind="link", hash="l")
    _add(storage, content="cnb.cool notes", kind="text", hash="t")
    results = storage.search("cnb", kind="link")
    assert len(results) == 1
    assert results[0].kind == "link"


def test_search_ignores_fts_syntax_characters(storage):
    _add(storage, content="C++ AND OR NOT", hash="x")
    # 未转义的 AND/引号会让 FTS 抛语法错误，实现里做了转义
    assert isinstance(storage.search('AND "quoted" *'), list)


def test_search_special_chars_do_not_raise(storage):
    for query in ['"', "*", ":", "(", ")", "NEAR", "a AND b", "***"]:
        assert isinstance(storage.search(query), list)


def test_update_fields_and_touch(storage):
    saved = _add(storage, hash="h")
    storage.update_fields(saved.id, pinned=True, tags=["x"])
    storage.touch(saved.id)
    loaded = storage.get(saved.id)
    assert loaded.pinned is True
    assert loaded.tags == ["x"]
    assert loaded.use_count == 1
    assert loaded.used_at > 0


def test_update_fields_ignores_unknown_keys(storage):
    saved = _add(storage, hash="h")
    storage.update_fields(saved.id, content="hacked", bogus=1)
    assert storage.get(saved.id).content != "hacked"


def test_find_by_hash(storage):
    _add(storage, content="dup", hash="same")
    found = storage.find_by_hash("same")
    assert found is not None and found.content == "dup"
    assert storage.find_by_hash("missing") is None


def test_delete_and_delete_many(storage):
    a = _add(storage, content="a", hash="a")
    b = _add(storage, content="b", hash="b")
    storage.delete(a.id)
    assert storage.count() == 1
    storage.delete_many([b.id])
    assert storage.count() == 0


def test_clear_keeps_pinned(storage):
    _add(storage, content="keep", hash="k", pinned=True)
    _add(storage, content="drop", hash="d")
    removed = storage.clear(keep_pinned=True)
    assert removed == 1
    assert storage.count() == 1
    assert storage.list()[0].content == "keep"


def test_pinned_and_favorite_sort_first(storage):
    now = time.time()
    _add(storage, content="plain", hash="1", created_at=now)
    _add(storage, content="pinned", hash="2", created_at=now - 100, pinned=True)
    _add(storage, content="favorite", hash="3", created_at=now - 50, favorite=True)
    order = [item.content for item in storage.list()]
    assert order[0] == "pinned"
    assert order[1] == "favorite"


def test_enforce_limits_by_count_skips_pinned(storage):
    now = time.time()
    for index in range(10):
        _add(storage, content=f"c{index}", hash=f"h{index}", created_at=now + index)
    _add(storage, content="pinned", hash="p", pinned=True, created_at=0)

    removed = storage.enforce_limits(max_items=3)
    # 10 条未固定里保留最新的 3 条，淘汰 7 条；固定项不受影响
    assert removed == 7
    remaining = [item.content for item in storage.list(limit=100)]
    assert "pinned" in remaining
    assert len(remaining) == 4
    # 保留的必须是最近的三条
    assert {"c9", "c8", "c7"} <= set(remaining)


def test_enforce_limits_by_days(storage):
    now = time.time()
    _add(storage, content="old", hash="old", created_at=now - 40 * 86400)
    _add(storage, content="new", hash="new", created_at=now)
    removed = storage.enforce_limits(max_items=0, max_days=30)
    assert removed == 1
    assert storage.count() == 1


def test_purge_expired_sensitive(storage):
    now = time.time()
    _add(storage, content="secret", hash="s1", sensitive=True, created_at=now - 100)
    _add(storage, content="secret2", hash="s2", sensitive=True, created_at=now)
    removed = storage.purge_expired_sensitive(ttl_seconds=50)
    assert removed == 1
    assert storage.count() == 1


def test_dedupe(storage):
    _add(storage, content="same", hash="dup")
    _add(storage, content="same", hash="dup")
    _add(storage, content="other", hash="other")
    removed = storage.dedupe()
    assert removed == 1
    assert storage.count() == 2


def test_dedupe_folds_usage_into_keeper(storage):
    """合并重复项不能把「复制过多次」这个信号丢掉。

    3 条同内容重复行意味着这段内容被复制了 3 次（约定：N 次 -> N-1）。
    旧实现直接删掉多余行，保留项 use_count 仍是 0，高频片段反而排在低频片段之后。
    """
    for index in range(3):
        _add(storage, content="hot", hash="dup", created_at=100 + index)
    _add(storage, content="cold", hash="cold", created_at=100)

    assert storage.dedupe() == 2
    keeper = storage.find_by_hash("dup")
    assert keeper.use_count == 2
    # 高频片段应当排到列表最前
    assert storage.list()[0].content == "hot"


def test_dedupe_keeps_last_used_at(storage):
    for created in (100.0, 500.0, 300.0):
        _add(storage, content="same", hash="dup", created_at=created, used_at=created)
    storage.dedupe()
    keeper = storage.find_by_hash("dup")
    assert keeper.used_at == 500.0


def test_dedupe_accumulates_explicit_use_count(storage):
    """重复行里若已记过 use_count，合并时要一并累加，不能互相覆盖。"""
    first = _add(storage, content="same", hash="dup", created_at=1)
    _add(storage, content="same", hash="dup", created_at=2)
    storage.touch(first.id)
    storage.dedupe()
    keeper = storage.find_by_hash("dup")
    # 2 行（多 1 次）+ 1 次显式使用 = 2
    assert keeper.use_count == 2


def test_dedupe_is_idempotent(storage):
    for index in range(4):
        _add(storage, content="same", hash="dup", created_at=index)
    storage.dedupe()
    after_first = storage.find_by_hash("dup").use_count
    assert storage.dedupe() == 0
    assert storage.find_by_hash("dup").use_count == after_first


def test_dedupe_ignores_empty_hash(storage):
    """没有 hash 的记录无从判断是否重复，不该被合并掉。"""
    _add(storage, content="a", hash="")
    _add(storage, content="b", hash="")
    assert storage.dedupe() == 0
    assert storage.count() == 2


def test_dedupe_single_row_untouched(storage):
    _add(storage, content="only", hash="dup")
    assert storage.dedupe() == 0
    assert storage.find_by_hash("dup").use_count == 0


def test_recanonicalize_rewrites_legacy_hash(storage):
    """老记录的指纹是拿原始内容算的，重算后才能被归一分组识别出来。"""
    from linglong_clip.core.models import content_hash

    legacy = "https://a.com/p?utm_source=weibo"
    _add(storage, kind="link", content=legacy, hash=content_hash("link", legacy))
    assert storage.recanonicalize() == 1
    # 内容一个字节都没改，只换了指纹
    assert storage.list()[0].content == legacy


def test_recanonicalize_then_dedupe_merges_legacy_duplicates(storage):
    """升级前入库的「同一页面不同渠道链接」应当能合到一起。

    这是 recanonicalize 存在的理由：新规则只对新数据生效，
    历史里的三条链接若不重算指纹，永远合不了。
    """
    from linglong_clip.core.models import content_hash

    urls = [
        "https://a.com/p?utm_source=weibo",
        "https://a.com/p?utm_source=wechat",
        "https://a.com/p",
    ]
    for index, url in enumerate(urls):
        _add(storage, kind="link", content=url, hash=content_hash("link", url),
             created_at=100 + index)

    assert storage.count() == 3
    assert storage.recanonicalize() == 2
    assert storage.dedupe() == 2
    assert storage.count() == 1
    # 复制次数要保住：3 行 = 复制过 2 次
    assert storage.list()[0].use_count == 2


def test_recanonicalize_is_idempotent(storage):
    storage.recanonicalize()
    assert storage.recanonicalize() == 0


def test_recanonicalize_noop_on_clean_records(storage):
    from linglong_clip.core.models import content_hash
    from linglong_clip.core.normalize import canonical_content

    content = "已经归一的链接"
    _add(storage, kind="text", content=content,
         hash=content_hash("text", canonical_content("text", content)))
    assert storage.recanonicalize() == 0


def test_recanonicalize_respects_disabled_options(storage):
    from linglong_clip.core.models import content_hash

    legacy = "https://a.com/p?utm_source=weibo"
    _add(storage, kind="link", content=legacy, hash=content_hash("link", legacy))
    options = {"url_strip_tracking": False, "path_normalize": False}
    assert storage.recanonicalize(options) == 0


def test_meta_kv(storage):
    assert storage.get_meta("missing", "fallback") == "fallback"
    storage.set_meta("k", "v1")
    storage.set_meta("k", "v2")
    assert storage.get_meta("k") == "v2"


def test_stats(storage):
    _add(storage, content="x", kind="text", hash="1")
    _add(storage, content="y", kind="link", hash="2")
    stats = storage.stats()
    assert stats["items"] == 2
    assert stats["kinds"]["link"] == 1
    assert stats["fts"] is True


def test_fts_index_rebuilt_for_existing_rows(tmp_path):
    """先建库删掉索引再重开，老数据应重新进索引。"""
    from linglong_clip.core.storage import Storage

    path = tmp_path / "rebuild.db"
    first = Storage(path)
    first.add(ClipboardItem(kind="text", content="历史数据", hash="old"))
    first._conn.execute("DROP TABLE items_fts")
    first._conn.commit()
    first.close()

    second = Storage(path)
    assert second.fts_enabled
    assert len(second.search("历史数据")) == 1
    second.close()


def test_enforce_limits_pin_immune_false_counts_pinned(storage):
    """关闭固定项豁免后，固定项也计入上限。

    排序仍把固定项排前，所以先被淘汰的仍是久未使用的普通记录 ——
    只有当历史远超上限时才会轮到固定项。
    """
    now = time.time()
    for index in range(10):
        _add(storage, content=f"c{index}", hash=f"h{index}", created_at=now + index)
    _add(storage, content="pinned", hash="p", pinned=True, created_at=0)

    removed = storage.enforce_limits(max_items=3, pin_immune=False)
    remaining = [item.content for item in storage.list(limit=100)]
    assert removed == 8
    assert len(remaining) == 3
    # 固定项排前，最旧的普通记录先被淘汰
    assert {"pinned", "c9", "c8"} == set(remaining)


def test_enforce_limits_pin_immune_false_still_keeps_newest(storage):
    """关闭豁免时不因固定项存在而少淘汰，保留的仍是排序最前的那几条。"""
    now = time.time()
    _add(storage, content="pinned", hash="p", pinned=True, created_at=now - 1000)
    for index in range(4):
        _add(storage, content=f"n{index}", hash=f"hn{index}", created_at=now + index)

    storage.enforce_limits(max_items=2, pin_immune=False)
    remaining = {item.content for item in storage.list(limit=100)}
    assert remaining == {"pinned", "n3"}


def test_enforce_limits_pin_immune_false_by_days(storage):
    """按天数淘汰时，关闭豁免会让超期的固定项也被清掉。"""
    now = time.time()
    _add(storage, content="old-pinned", hash="op", pinned=True, created_at=now - 40 * 86400)
    _add(storage, content="new", hash="new", created_at=now)

    assert storage.enforce_limits(max_items=0, max_days=30, pin_immune=False) == 1
    remaining = [item.content for item in storage.list(limit=100)]
    assert remaining == ["new"]


def test_enforce_limits_default_is_pin_immune(storage):
    """不传 pin_immune 时保持老行为：固定项豁免。"""
    now = time.time()
    _add(storage, content="pinned", hash="p", pinned=True, created_at=0)
    for index in range(5):
        _add(storage, content=f"c{index}", hash=f"h{index}", created_at=now + index)

    storage.enforce_limits(max_items=1)
    remaining = {item.content for item in storage.list(limit=100)}
    assert remaining == {"pinned", "c4"}


# ---- 关闭之后 ----

def test_close_is_idempotent(storage):
    """`close()` 可能被退出路径与测试夹具各调一次，第二次不该报错。"""
    storage.close()
    storage.close()
    assert storage.closed is True


def test_queries_after_close_return_empty_instead_of_raising(storage):
    """关闭后仍有调用方找上来是常态，不能抛异常。

    防抖定时器、采集线程、限时清空任务都可能晚一步。异常跑在 Qt 事件循环里
    会被吞成一行 `Exceptions caught in Qt event loop`，很难追回这里。
    """
    _add(storage, content="hello", hash="h1")
    storage.close()

    assert storage.list(limit=10) == []
    assert storage.search("hello") == []
    assert storage.get(1) is None
    assert storage.find_by_hash("h1") is None
    assert storage.latest() is None
    assert storage.count() == 0
    assert storage.count_by_kind() == {}
    assert storage.top_sources() == []
    assert storage.stats()["closed"] is True
    assert storage.get_meta("nope", "fallback") == "fallback"


def test_writes_after_close_are_ignored(storage):
    """关闭后的写入一律忽略，而不是写进一个已关闭的连接。"""
    storage.close()

    assert storage.add(ClipboardItem(kind="text", content="x", preview="x", hash="h")) is None
    storage.touch(1)
    storage.update_fields(1, pinned=True)
    storage.delete(1)
    assert storage.delete_many([1, 2]) == 0
    assert storage.clear() == 0
    assert storage.dedupe() == 0
    assert storage.recanonicalize() == 0
    assert storage.enforce_limits(max_items=1) == 0
    assert storage.purge_expired_sensitive(ttl_seconds=60) == 0
    storage.set_meta("k", "v")
    storage.optimize()


# ---- 全量遍历（iter_all）----


def test_iter_all_returns_every_record(storage):
    """遍历要看到整库，不能像 `list()` 那样只给最新一批。"""
    for index in range(50):
        _add(storage, content=f"c{index}", hash=f"h{index}", created_at=1000 + index)

    seen = [item.hash for item in storage.iter_all(batch=7)]
    assert len(seen) == 50
    assert set(seen) == {f"h{index}" for index in range(50)}


def test_iter_all_is_newest_first(storage):
    """按 id 倒序：最后写入的排最前，与界面的「最新在前」直觉一致。"""
    for index in range(10):
        _add(storage, content=f"c{index}", hash=f"h{index}", created_at=1000 + index)

    assert [item.hash for item in storage.iter_all(batch=3)][:3] == ["h9", "h8", "h7"]


def test_iter_all_batch_size_does_not_change_result(storage):
    """批量只影响分页次数，不该改变结果集。"""
    for index in range(25):
        _add(storage, content=f"c{index}", hash=f"h{index}", created_at=1000 + index)

    for batch in (1, 4, 25, 1000):
        got = [item.hash for item in storage.iter_all(batch=batch)]
        assert got == [f"h{index}" for index in range(24, -1, -1)], f"batch={batch}"


def test_iter_all_is_immune_to_usage_order(storage):
    """`touch()` 会改写界面排序，但遍历用 id 序，不受影响。

    若遍历依赖 `used_at` 排序，翻页时被复制的记录会跳到最前，
    出现漏读或重复读 —— 同步导出正是靠这个保证不漏。
    """
    for index in range(30):
        _add(storage, content=f"c{index}", hash=f"h{index}", created_at=1000 + index)

    items = storage.list(limit=100)
    oldest = items[-1]
    storage.touch(oldest.id)

    seen = [item.hash for item in storage.iter_all(batch=4)]
    assert len(seen) == 30
    assert len(set(seen)) == 30
    assert oldest.hash in seen


def test_iter_all_on_empty_storage(storage):
    assert list(storage.iter_all()) == []


def test_iter_all_after_close_returns_nothing(storage):
    """关闭之后遍历要安静返回，不能抛 —— 与其余只读接口一致。"""
    _add(storage, content="x", hash="h")
    storage.close()
    assert list(storage.iter_all()) == []
