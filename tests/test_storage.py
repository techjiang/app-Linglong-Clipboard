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
