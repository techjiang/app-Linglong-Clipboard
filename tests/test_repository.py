"""JSON 单文件存储：落盘格式、接口对齐、检索与图片内联。"""

import base64
import json
import threading
import time

import pytest

from linglong_clip.core.models import ClipboardItem
from linglong_clip.core.repository import (
    JsonRepository,
    encode_file_as_data_uri,
    is_data_uri,
    open_repository,
    split_data_uri,
    suffix_for_mime,
)


def _add(repo, **kwargs):
    defaults = {"kind": "text", "content": "content", "preview": "content"}
    defaults.update(kwargs)
    defaults.setdefault(
        "hash", f"h-{kwargs.get('content', 'content')}-{kwargs.get('created_at', 0)}"
    )
    return repo.add(ClipboardItem(**defaults))


@pytest.fixture()
def repo(tmp_path):
    instance = JsonRepository(tmp_path / ".clipboard" / "clipboard.json")
    yield instance
    instance.close()


# ---- 落盘格式 ----


def test_file_is_created_readable_json(repo):
    _add(repo, content="玲珑 中国", hash="a")
    raw = json.loads(repo.path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["items"][0]["content"] == "玲珑 中国"


def test_file_is_human_readable_without_escaping(repo):
    _add(repo, content="中文内容", hash="a")
    text = repo.path.read_text(encoding="utf-8")
    assert "中文内容" in text
    assert "\\u4e2d" not in text


def test_empty_values_are_omitted(repo):
    _add(repo, content="x", hash="a")
    entry = json.loads(repo.path.read_text(encoding="utf-8"))["items"][0]
    # 空字符串、空列表不该出现在文件里，否则文件难读
    assert "html" not in entry
    assert "tags" not in entry
    assert "files" not in entry


def test_roundtrip_preserves_all_fields(tmp_path):
    path = tmp_path / "clipboard.json"
    first = JsonRepository(path)
    first.add(
        ClipboardItem(
            kind="link",
            content="https://cnb.cool",
            preview="cnb",
            html="<a>x</a>",
            files=["/tmp/a.txt"],
            metadata={"host": "cnb.cool"},
            source_app="chrome",
            source_title="标题",
            hash="h",
            size=12,
            pinned=True,
            favorite=True,
            sensitive=True,
            tags=["x", "y"],
            device="dev1",
            created_at=123.0,
            used_at=456.0,
            use_count=3,
        )
    )
    first.close()

    second = JsonRepository(path)
    loaded = second.get(1)
    assert loaded.kind == "link"
    assert loaded.content == "https://cnb.cool"
    assert loaded.html == "<a>x</a>"
    assert loaded.files == ["/tmp/a.txt"]
    assert loaded.metadata == {"host": "cnb.cool"}
    assert loaded.source_app == "chrome"
    assert loaded.source_title == "标题"
    assert loaded.pinned and loaded.favorite and loaded.sensitive
    assert loaded.tags == ["x", "y"]
    assert loaded.device == "dev1"
    assert loaded.use_count == 3
    assert loaded.used_at == 456.0
    second.close()


def test_id_counter_survives_reopen(tmp_path):
    path = tmp_path / "clipboard.json"
    first = JsonRepository(path)
    first.add(ClipboardItem(content="a", hash="a"))
    first.add(ClipboardItem(content="b", hash="b"))
    first.close()

    second = JsonRepository(path)
    third = second.add(ClipboardItem(content="c", hash="c"))
    assert third.id == 3
    second.close()


def test_broken_file_is_backed_up_not_lost(tmp_path):
    path = tmp_path / "clipboard.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")

    repo = JsonRepository(path)
    assert repo.count() == 0
    assert path.with_suffix(".json.broken").exists()
    repo.close()


def test_non_object_payload_is_reset(tmp_path):
    path = tmp_path / "clipboard.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    repo = JsonRepository(path)
    assert repo.count() == 0
    repo.close()


def test_entries_without_id_are_skipped(tmp_path):
    path = tmp_path / "clipboard.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"items": [{"content": "no id"}, {"id": 5, "content": "ok"}]}),
        encoding="utf-8",
    )
    repo = JsonRepository(path)
    assert repo.count() == 1
    assert repo.get(5).content == "ok"
    repo.close()


def test_atomic_write_leaves_no_temp_file(repo):
    _add(repo, content="x", hash="a")
    assert not repo.path.with_suffix(".json.tmp").exists()


# ---- 与 Storage 的接口对齐 ----


def test_add_get_count(repo):
    saved = _add(repo, content="hello", hash="h1")
    assert saved.id is not None
    assert repo.get(saved.id).content == "hello"
    assert repo.count() == 1


def test_get_returns_copy_not_reference(repo):
    saved = _add(repo, content="hello", hash="h1")
    repo.get(saved.id).content = "mutated"
    assert repo.get(saved.id).content == "hello"


def test_touch_updates_usage(repo):
    saved = _add(repo, hash="h")
    repo.touch(saved.id)
    loaded = repo.get(saved.id)
    assert loaded.use_count == 1
    assert loaded.used_at > 0


def test_update_fields_and_unknown_keys(repo):
    saved = _add(repo, hash="h")
    repo.update_fields(saved.id, pinned=True, tags=["x"], content="hacked", bogus=1)
    loaded = repo.get(saved.id)
    assert loaded.pinned is True
    assert loaded.tags == ["x"]
    assert loaded.content != "hacked"


def test_delete_and_delete_many(repo):
    a = _add(repo, content="a", hash="a")
    b = _add(repo, content="b", hash="b")
    repo.delete(a.id)
    assert repo.count() == 1
    repo.delete_many([b.id])
    assert repo.count() == 0
    assert repo.delete_many([]) == 0


def test_clear_keeps_pinned(repo):
    _add(repo, content="keep", hash="k", pinned=True)
    _add(repo, content="drop", hash="d")
    assert repo.clear(keep_pinned=True) == 1
    assert repo.count() == 1
    assert repo.list()[0].content == "keep"


def test_find_by_hash_and_latest(repo):
    _add(repo, content="dup", hash="same", created_at=1)
    _add(repo, content="dup2", hash="same", created_at=2)
    assert repo.find_by_hash("same").content == "dup2"
    assert repo.find_by_hash("missing") is None
    assert repo.find_by_hash("") is None
    assert repo.latest().content == "dup2"


def test_get_many_returns_only_existing(repo):
    """两个后端的 `get_many` 语义必须一致：只返回存在的 id，顺序无关。"""
    first = _add(repo, content="a", hash="a")
    second = _add(repo, content="b", hash="b")
    found = repo.get_many([first.id, second.id, 999999])
    assert set(found) == {first.id, second.id}
    assert found[first.id].content == "a"
    assert repo.get_many([]) == {}


def test_get_many_ignores_unusable_ids(repo):
    """id 来自界面与同步日志，可能带 `None` 或字符串 —— 判不出数就跳过，不炸。"""
    item = _add(repo, content="a", hash="a")
    found = repo.get_many([None, "abc", item.id, str(item.id)])
    assert set(found) == {item.id}


def test_pinned_and_favorite_sort_first(repo):
    now = time.time()
    _add(repo, content="plain", hash="1", created_at=now)
    _add(repo, content="pinned", hash="2", created_at=now - 100, pinned=True)
    _add(repo, content="favorite", hash="3", created_at=now - 50, favorite=True)
    assert [item.content for item in repo.list()][:2] == ["pinned", "favorite"]


def test_list_filters_and_paging(repo):
    for index, kind in enumerate(["text", "link", "text"]):
        _add(repo, content=f"c{index}", kind=kind, hash=f"h{index}", source_app="chrome")
    assert len(repo.list(kind="text")) == 2
    assert len(repo.list(source_app="chrome")) == 3
    assert len(repo.list(source_app="firefox")) == 0
    assert len(repo.list(limit=2)) == 2
    assert len(repo.list(limit=2, offset=2)) == 1
    assert len(repo.list(favorite_only=True)) == 0
    _add(repo, content="fav", hash="fav", favorite=True)
    assert len(repo.list(favorite_only=True)) == 1
    assert len(repo.list(since=time.time() + 100)) == 0


def test_pinned_only(repo):
    _add(repo, content="p", hash="p", pinned=True)
    _add(repo, content="n", hash="n")
    assert [item.content for item in repo.list(pinned_only=True)] == ["p"]


def test_count_by_kind_and_top_sources(repo):
    _add(repo, content="a", kind="text", hash="a", source_app="chrome")
    _add(repo, content="b", kind="link", hash="b", source_app="chrome")
    _add(repo, content="c", kind="text", hash="c", source_app="code")
    assert repo.count_by_kind() == {"text": 2, "link": 1}
    assert repo.top_sources(1) == [("chrome", 2)]


def test_dedupe(repo):
    _add(repo, content="same", hash="dup")
    _add(repo, content="same", hash="dup")
    _add(repo, content="other", hash="other")
    assert repo.dedupe() == 1
    assert repo.count() == 2


def test_dedupe_folds_usage_into_keeper(repo):
    """与 SQLite 后端语义一致：合并重复项时要保留复制次数。"""
    for index in range(3):
        _add(repo, content="hot", hash="dup", created_at=100 + index)
    _add(repo, content="cold", hash="cold", created_at=100)

    assert repo.dedupe() == 2
    assert repo.count() == 2
    assert repo.find_by_hash("dup").use_count == 2


def test_dedupe_is_idempotent(repo):
    for index in range(3):
        _add(repo, content="same", hash="dup", created_at=index)
    repo.dedupe()
    count = repo.find_by_hash("dup").use_count
    assert repo.dedupe() == 0
    assert repo.find_by_hash("dup").use_count == count


def test_dedupe_keeps_last_used_at(repo):
    for created in (100.0, 500.0, 300.0):
        _add(repo, content="same", hash="dup", created_at=created, used_at=created)
    repo.dedupe()
    assert repo.find_by_hash("dup").used_at == 500.0


def test_recanonicalize_rewrites_legacy_hash(repo):
    from linglong_clip.core.models import ClipboardItem, content_hash

    legacy = "https://a.com/p?utm_source=weibo"
    repo.add(ClipboardItem(kind="link", content=legacy, preview=legacy,
                           hash=content_hash("link", legacy)))
    assert repo.recanonicalize() == 1
    assert repo.list()[0].content == legacy


def test_recanonicalize_then_dedupe_merges_legacy_duplicates(repo):
    from linglong_clip.core.models import ClipboardItem, content_hash

    urls = [
        "https://a.com/p?utm_source=weibo",
        "https://a.com/p?utm_source=wechat",
        "https://a.com/p",
    ]
    for index, url in enumerate(urls):
        repo.add(ClipboardItem(kind="link", content=url, preview=url,
                               hash=content_hash("link", url), created_at=100 + index))

    assert repo.count() == 3
    assert repo.recanonicalize() == 2
    assert repo.dedupe() == 2
    assert repo.count() == 1
    assert repo.list()[0].use_count == 2


def test_recanonicalize_is_idempotent(repo):
    repo.recanonicalize()
    assert repo.recanonicalize() == 0


def test_dedupe_ignores_empty_hash(repo):
    _add(repo, content="a", hash="")
    _add(repo, content="b", hash="")
    assert repo.dedupe() == 0
    assert repo.count() == 2


def test_enforce_limits_by_count_skips_pinned(repo):
    now = time.time()
    for index in range(10):
        _add(repo, content=f"c{index}", hash=f"h{index}", created_at=now + index)
    _add(repo, content="pinned", hash="p", pinned=True, created_at=0)

    assert repo.enforce_limits(max_items=3) == 7
    remaining = [item.content for item in repo.list(limit=100)]
    assert len(remaining) == 4
    assert {"c9", "c8", "c7", "pinned"} == set(remaining)


def test_enforce_limits_by_days(repo):
    now = time.time()
    _add(repo, content="old", hash="old", created_at=now - 40 * 86400)
    _add(repo, content="new", hash="new", created_at=now)
    assert repo.enforce_limits(max_items=0, max_days=30) == 1
    assert repo.count() == 1


def test_purge_expired_sensitive(repo):
    now = time.time()
    _add(repo, content="s", hash="s1", sensitive=True, created_at=now - 100)
    _add(repo, content="s2", hash="s2", sensitive=True, created_at=now)
    assert repo.purge_expired_sensitive(50) == 1
    assert repo.count() == 1
    assert repo.purge_expired_sensitive(0) == 0


def test_meta_kv(repo):
    assert repo.get_meta("missing", "fallback") == "fallback"
    repo.set_meta("k", "v1")
    repo.set_meta("k", "v2")
    assert repo.get_meta("k") == "v2"


def test_stats_reports_backend(repo):
    _add(repo, content="x", hash="1")
    stats = repo.stats()
    assert stats["items"] == 1
    assert stats["backend"] == "json"
    assert stats["fts"] is False
    assert stats["path"].endswith("clipboard.json")
    assert stats["db_size"] > 0


def test_optimize_and_close_are_safe(repo):
    _add(repo, content="x", hash="1")
    repo.optimize()
    repo.close()
    repo.close()  # 幂等


# ---- 检索 ----


def test_search_substring(repo):
    _add(repo, content="玲珑剪切板的历史记录", hash="a")
    _add(repo, content="another clipboard entry", hash="b")
    assert len(repo.search("玲珑剪")) == 1
    assert len(repo.search("clipboard")) == 1
    assert len(repo.search("nonexistent-xyz")) == 0


def test_search_is_case_insensitive(repo):
    _add(repo, content="Hello World", hash="a")
    assert len(repo.search("hello")) == 1


def test_search_empty_query_lists_all(repo):
    _add(repo, content="one", hash="1")
    _add(repo, content="two", hash="2")
    assert len(repo.search("")) == 2
    assert len(repo.search("   ")) == 2


def test_search_matches_tags_and_source(repo):
    _add(repo, content="plain", hash="t", tags=["json", "config"])
    _add(repo, content="plain2", hash="s", source_app="chrome")
    assert len(repo.search("config")) == 1
    assert len(repo.search("chrome")) == 1


def test_search_prefers_content_over_source(repo):
    _add(repo, content="target", hash="a", source_app="other")
    _add(repo, content="something", hash="b", source_app="target")
    results = repo.search("target")
    assert results[0].content == "target"


def test_search_filtered_by_kind(repo):
    _add(repo, content="cnb.cool", kind="link", hash="l")
    _add(repo, content="cnb.cool notes", kind="text", hash="t")
    results = repo.search("cnb", kind="link")
    assert len(results) == 1
    assert results[0].kind == "link"


def test_search_respects_limit(repo):
    for index in range(5):
        _add(repo, content=f"same {index}", hash=f"h{index}")
    assert len(repo.search("same", limit=2)) == 2


def test_search_special_characters_do_not_raise(repo):
    _add(repo, content='a "b" * c : d (e)', hash="x")
    for query in ['"', "*", ":", "(", ")", "NEAR", "a AND b", "***"]:
        assert isinstance(repo.search(query), list)


# ---- 图片内联 ----


def test_data_uri_helpers():
    assert is_data_uri("data:image/png;base64,AAAA")
    assert not is_data_uri("data:image/png,AAAA")
    assert not is_data_uri("images/a.png")
    assert split_data_uri("data:image/jpeg;base64,AAAA") == ("image/jpeg", "AAAA")
    assert suffix_for_mime("image/jpeg") == "jpg"
    assert suffix_for_mime("image/unknown") == "png"


def test_encode_file_as_data_uri(tmp_path):
    target = tmp_path / "a.png"
    target.write_bytes(b"\x89PNGdata")
    uri = encode_file_as_data_uri(target)
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.partition(",")[2]) == b"\x89PNGdata"


def test_encode_file_skips_oversized(tmp_path):
    target = tmp_path / "big.png"
    target.write_bytes(b"x" * 20)
    assert encode_file_as_data_uri(target, max_bytes=10) is None


def test_encode_file_missing_returns_none(tmp_path):
    assert encode_file_as_data_uri(tmp_path / "nope.png") is None


def test_inline_image_survives_reopen(tmp_path):
    """图片内联后，整份 JSON 拷到另一台机器仍能取回原图。"""
    path = tmp_path / "clipboard.json"
    payload = b"\x89PNG\r\n\x1a\nfake-image-bytes"
    uri = "data:image/png;base64," + base64.b64encode(payload).decode("ascii")

    first = JsonRepository(path)
    first.set_image_data("images/abc.png", uri)
    first.add(
        ClipboardItem(
            kind="image",
            content="abc.png",
            image_path="images/abc.png",
            hash="img",
            preview="[图片 1×1]",
        )
    )
    first.close()

    moved = tmp_path / "elsewhere.json"
    moved.write_bytes(path.read_bytes())
    second = JsonRepository(moved)
    loaded = second.get(1)
    assert is_data_uri(loaded.image_path)
    assert second.read_image_bytes("images/abc.png") == payload
    assert second.read_image_bytes("") is None
    assert second.read_image_bytes("images/missing.png") is None
    second.close()


def test_image_field_keeps_relative_path_when_not_inlined(repo):
    _add(repo, kind="image", content="a.png", image_path="images/a.png", hash="i")
    entry = json.loads(repo.path.read_text(encoding="utf-8"))["items"][0]
    assert entry["image"] == "images/a.png"


def test_corrupt_inline_image_returns_none(repo):
    _add(
        repo,
        kind="image",
        content="a.png",
        image_path="data:image/png;base64,!!!not-base64!!!",
        hash="i",
    )
    assert repo.read_image_bytes("data:image/png;base64,!!!not-base64!!!") is None


def test_images_dir_next_to_json_file(repo):
    assert repo.images_dir == repo.path.parent / "images"


# ---- 并发与统一入口 ----


def test_concurrent_adds_keep_ids_unique(repo):
    def work(offset):
        for index in range(20):
            repo.add(ClipboardItem(content=f"c-{offset}-{index}", hash=f"h-{offset}-{index}"))

    threads = [threading.Thread(target=work, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    ids = [item.id for item in repo.list(limit=1000)]
    assert len(ids) == len(set(ids)) == 80
    assert repo.count() == 80


def test_open_repository_picks_backend(home):
    from linglong_clip.core.storage import Storage
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    assert isinstance(open_repository(config), Storage)

    config.set("storage.backend", "json")
    assert isinstance(open_repository(config), JsonRepository)

    config.set("storage.backend", "SQLITE")  # 大小写不敏感
    assert isinstance(open_repository(config), Storage)


def test_json_path_follows_env_override(home, monkeypatch):
    from linglong_clip.utils import paths

    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home))
    assert paths.json_path() == home / ".clipboard" / "clipboard.json"

    custom = home / "custom"
    monkeypatch.setenv("LINGLONG_CLIPBOARD_JSON_HOME", str(custom))
    assert paths.json_dir() == custom


def test_enforce_limits_pin_immune_false_counts_pinned(repo):
    """JSON 后端与 SQLite 语义对齐：关闭豁免后固定项也占限额。"""
    now = time.time()
    for index in range(10):
        _add(repo, content=f"c{index}", hash=f"h{index}", created_at=now + index)
    _add(repo, content="pinned", hash="p", pinned=True, created_at=0)

    assert repo.enforce_limits(max_items=3, pin_immune=False) == 8
    remaining = {item.content for item in repo.list(limit=100)}
    assert remaining == {"pinned", "c9", "c8"}


def test_enforce_limits_pin_immune_false_by_days(repo):
    now = time.time()
    _add(repo, content="old-pinned", hash="op", pinned=True, created_at=now - 40 * 86400)
    _add(repo, content="new", hash="new", created_at=now)

    assert repo.enforce_limits(max_items=0, max_days=30, pin_immune=False) == 1
    assert [item.content for item in repo.list(limit=100)] == ["new"]


def test_enforce_limits_default_is_pin_immune(repo):
    now = time.time()
    _add(repo, content="pinned", hash="p", pinned=True, created_at=0)
    for index in range(5):
        _add(repo, content=f"c{index}", hash=f"h{index}", created_at=now + index)

    repo.enforce_limits(max_items=1)
    assert {item.content for item in repo.list(limit=100)} == {"pinned", "c4"}


# ---- 关闭之后 ----

def test_close_is_idempotent(repo, tmp_path):
    """`close()` 会落盘；重复调用不该把文件再写一遍或报错。"""
    repo.add(ClipboardItem(kind="text", content="hello", preview="hello", hash="h"))
    repo.close()
    stamp = repo.path.stat().st_mtime_ns
    repo.close()
    repo.close()
    assert repo.closed is True
    assert repo.path.stat().st_mtime_ns == stamp


def test_writes_after_close_do_not_touch_disk(repo):
    """关闭后再来的写入一律忽略。

    持久化任务（限时清空、退出收尾）本来就可能晚一步到达，
    让它们安静地什么都不做，好过对着一个已经落过盘的对象再写一次。
    """
    repo.add(ClipboardItem(kind="text", content="hello", preview="hello", hash="h"))
    repo.close()
    before = repo.path.read_text(encoding="utf-8")

    repo.add(ClipboardItem(kind="text", content="late", preview="late", hash="h2"))
    repo.delete(1)
    repo.save()
    assert repo.path.read_text(encoding="utf-8") == before


# ---- 全量遍历（iter_all）----


def test_iter_all_returns_every_item(repo):
    for index in range(40):
        _add(repo, content=f"c{index}", hash=f"h{index}", created_at=1000 + index)

    seen = [item.hash for item in repo.iter_all()]
    assert len(seen) == 40
    assert set(seen) == {f"h{index}" for index in range(40)}


def test_iter_all_matches_storage_semantics(repo):
    """与 `Storage.iter_all` 同语义：id 倒序、批量参数不影响结果。"""
    for index in range(12):
        _add(repo, content=f"c{index}", hash=f"h{index}", created_at=1000 + index)

    assert [item.hash for item in repo.iter_all(batch=1)] == [
        f"h{index}" for index in range(11, -1, -1)
    ]
    assert [item.hash for item in repo.iter_all(batch=1000)] == [
        f"h{index}" for index in range(11, -1, -1)
    ]


def test_iter_all_on_empty_repo(repo):
    assert list(repo.iter_all()) == []


def test_iter_all_after_close_matches_list(repo):
    """关闭后仍可读 —— 与 JSON 后端 `list()` 的既有语义保持一致。

    这里刻意不写「关闭后返回空」：`JsonRepository` 的数据本就在内存里，
    `close()` 只是停止落盘；`list()` 关闭后照样返回记录。
    让 `iter_all` 单独变成空集会造成两个只读接口行为不一致。
    """
    _add(repo, content="x", hash="h")
    repo.close()

    assert len(list(repo.iter_all())) == len(repo.list())


# ---- 同刻遍历（iter_since + since_id）----
#
# 与 `Storage.iter_since` 必须逐条同语义：同步模块不区分后端，
# 两边行为一旦分叉，同刻记录塞满一页时 JSON 后端会静默丢记录。


def test_iter_since_orders_by_time_then_id(repo):
    for index in range(5):
        _add(repo, content=f"c{index}", hash=f"h{index}", created_at=1000.0)

    assert [item.hash for item in repo.iter_since(0.0)] == [
        f"h{index}" for index in range(5)
    ]


def test_iter_since_is_exclusive_on_time(repo):
    for index in range(3):
        _add(repo, content=f"c{index}", hash=f"h{index}", created_at=1000.0 + index)

    assert [item.hash for item in repo.iter_since(1001.0)] == ["h2"]


def test_iter_since_id_resumes_within_same_instant(repo):
    """同一时刻内靠 `since_id` 续读 —— 同刻记录塞满一页时的唯一出路。"""
    for index in range(10):
        _add(repo, content=f"c{index}", hash=f"h{index}", created_at=1000.0)

    items = list(repo.iter_since(0.0))
    page = items[:4]
    assert [item.hash for item in page] == ["h0", "h1", "h2", "h3"]

    last = page[-1]
    assert [item.hash for item in repo.iter_since(last.created_at, since_id=last.id)] == [
        "h4", "h5", "h6", "h7", "h8", "h9"
    ]


def test_iter_since_id_at_end_returns_empty(repo):
    for index in range(4):
        _add(repo, content=f"c{index}", hash=f"h{index}", created_at=1000.0)

    items = list(repo.iter_since(0.0))
    last = items[-1]
    assert list(repo.iter_since(last.created_at, since_id=last.id)) == []


def test_iter_since_id_excludes_earlier_ids_in_same_instant(repo):
    """`since_id` 是「字典序严格大于」：同刻里 id 更小的不再重读。"""
    for index in range(4):
        _add(repo, content=f"c{index}", hash=f"h{index}", created_at=1000.0)

    middle = list(repo.iter_since(0.0))[1]
    after = [item.hash for item in repo.iter_since(middle.created_at, since_id=middle.id)]
    assert after == ["h2", "h3"]


def test_iter_since_matches_storage_on_mixed_timestamps(repo):
    """时间跨度与同刻混在一起时，整体仍是 (时间, id) 全序。"""
    _add(repo, content="a", hash="ha", created_at=1000.0)
    _add(repo, content="b", hash="hb", created_at=1000.0)
    _add(repo, content="c", hash="hc", created_at=1001.0)
    _add(repo, content="d", hash="hd", created_at=1001.0)
    _add(repo, content="e", hash="he", created_at=1002.0)

    assert [item.hash for item in repo.iter_since(0.0)] == [
        "ha", "hb", "hc", "hd", "he"
    ]
    # `since` 排他：给 1001.0 时，恰好落在该时刻的两条按语义被排除
    assert [item.hash for item in repo.iter_since(1001.0)] == ["he"]

    # 想读到 1001.0 上的记录，得给「1000.0 + 该条之前的 id」
    first_at_1001 = repo.find_by_hash("hc")
    assert first_at_1001 is not None
    rest = [item.hash for item in repo.iter_since(1000.0, since_id=first_at_1001.id - 1)]
    assert rest == ["hc", "hd", "he"]


def test_iter_since_matches_storage_backend(repo, storage):
    """两个后端在 `(since, since_id)` 上必须给出**逐条相同**的结果。

    同步模块不区分后端（`iter_since` 是共用接口）。一旦分叉，
    同刻记录塞满一页时只有其中一个后端会静默丢记录 —— 而这类缺陷
    没有任何报错，只会表现为「换个存储后端就少一段历史」。
    """
    from linglong_clip.core.models import ClipboardItem

    rows = [
        ("ha", 1000.0),
        ("hb", 1000.0),
        ("hc", 1000.0),
        ("hd", 1001.0),
        ("he", 1001.0),
        ("hf", 1002.0),
    ]
    for digest, created in rows:
        _add(repo, content=digest, hash=digest, created_at=created)
        storage.add(
            ClipboardItem(
                kind="text", content=digest, preview=digest,
                hash=digest, created_at=created,
            )
        )

    # 覆盖三档游标：起点、同刻中部（带 id）、跨时刻
    probes = [(0.0, 0), (1000.0, 0), (1000.0, 2), (1000.0, 3), (1001.0, 0), (1001.0, 5)]
    for since, since_id in probes:
        json_side = [
            item.hash for item in repo.iter_since(since, since_id=since_id)
        ]
        sqlite_side = [
            item.hash for item in storage.iter_since(since, since_id=since_id)
        ]
        assert json_side == sqlite_side, f"since={since} since_id={since_id}"
