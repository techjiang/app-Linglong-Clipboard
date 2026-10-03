"""全库检索：候选集覆盖全库、结果与后端无关、相关度带命中位置。

这一组用例守的是同一条约束：**同一条记录、同一句查询，换个存储后端
必须得到同一个结果。** 分叉的表现不是报错，而是「我明明记得复制过，
搜就是搜不到」—— 而这些记录就在库里，换个后端又搜得到。
"""

from __future__ import annotations

import time

import pytest

from linglong_clip.core.models import ClipboardItem
from linglong_clip.core.repository import JsonRepository
from linglong_clip.core.search_index import (
    MAX_CANDIDATES,
    IndexHit,
    SearchIndex,
    SearchOutcome,
    is_searchable,
    relevance_map,
    scan_items,
    scan_repository,
    score_item_fields,
    tokenize,
)
from linglong_clip.core.semantic import SemanticEngine


def _item(index, **kwargs):
    defaults = {
        "id": index if index else None,
        "kind": "text",
        "content": f"content {index}",
        "preview": f"content {index}",
        "hash": f"h{index}",
    }
    defaults.update(kwargs)
    return ClipboardItem(**defaults)


# ---- 分词 ----


@pytest.mark.parametrize(
    "query,expected",
    [
        ("hello world", ["hello", "world"]),
        ("  前后空白  ", ["前后空白"]),
        ("a/b  c.d", ["a/b", "c.d"]),
        ("self.attr", ["self.attr"]),
        ("", []),
        ("   ", []),
    ],
)
def test_tokenize_keeps_punctuation(query, expected):
    """按空白切分，不按标点。

    剪贴板里搜的常常是路径、URL、代码片段，按标点切开会让
    `a/b`、`x.y`、`self.attr` 这类输入永远查不到 ——
    而用户输入的就是他复制过的那一串。
    """
    assert tokenize(query) == expected


# ---- 字段计分 ----


def test_score_item_fields_weighs_content_over_source():
    """正文命中比来源应用命中更有意义，两者不能同分。"""
    content_hit, _, _ = score_item_fields(_item(1, content="deploy"), ["deploy"])
    source_hit, _, _ = score_item_fields(_item(1, content="x", source_app="deploy"), ["deploy"])
    assert content_hit > source_hit


def test_score_item_fields_reports_matched_fields():
    _total, hits, fields = score_item_fields(
        _item(1, content="deploy", preview="deploy"), ["deploy"]
    )
    assert hits == 2
    assert set(fields) == {"content", "preview"}


def test_score_item_fields_caps_repetition():
    """同一字段里重复几百次不该把真正的目标挤下去。"""
    once, _, _ = score_item_fields(_item(1, content="deploy"), ["deploy"])
    many, _, _ = score_item_fields(_item(1, content="deploy " * 500), ["deploy"])
    # 有加成，但被上限压住，不会随出现次数线性膨胀
    assert many > once
    assert many < once * 3


def test_score_item_fields_ignores_empty_text():
    assert score_item_fields(
        ClipboardItem(kind="image", image_path="a.png", hash="i"), ["x"]
    ) == (0.0, 0, [])


# ---- 扫描 ----


def test_scan_items_returns_hits_with_position():
    items = [
        _item(1, content="hello world", preview="hello"),
        _item(2, content="say hello later"),
    ]
    scanned = [0]
    hits = scan_items(items, ["hello"], scanned_out=scanned)
    assert [hit.item.id for hit in hits] == [1, 2]
    assert scanned == [2]
    # 命中位置靠前的记录拿到更小的 position
    assert hits[0].position < hits[1].position


def test_scan_items_counts_scanned_not_hits():
    """`scanned` 报的是扫了多少条，不是命中多少条 —— 排查靠的是前者。"""
    items = [_item(i, content="无匹配") for i in range(5)]
    scanned = [0]
    hits = scan_items(items, ["absent"], scanned_out=scanned)
    assert hits == []
    assert scanned == [5]


def test_scan_items_skips_records_without_text():
    """图片、纯文件记录没有可检索文本，不该计入扫描量。"""
    items = [
        ClipboardItem(kind="image", content="", preview="", image_path="a.png", hash="img"),
        _item(2, content="hello"),
    ]
    scanned = [0]
    scan_items(items, ["hello"], scanned_out=scanned)
    assert scanned == [1]


def test_scan_items_is_case_insensitive():
    hits = scan_items([_item(1, content="HELLO")], ["hello"])
    assert len(hits) == 1


def test_scan_items_empty_tokens_returns_nothing():
    assert scan_items([_item(1)], []) == []


def test_scan_items_respects_limit():
    items = [_item(i, content="same") for i in range(50)]
    hits = scan_items(items, ["same"], limit=10)
    assert len(hits) == 10


def test_scan_items_matches_by_any_token():
    """多词查询默认任一命中即可：剪贴板里记不住原话，多打一个词就查空更糟。"""
    items = [_item(1, content="alpha"), _item(2, content="beta")]
    hits = scan_items(items, ["alpha", "beta"])
    assert {hit.item.id for hit in hits} == {1, 2}


def test_scan_items_require_all_narrows_to_every_token():
    items = [_item(1, content="alpha beta"), _item(2, content="alpha only")]
    hits = scan_items(items, ["alpha", "beta"], require_all=True)
    assert [hit.item.id for hit in hits] == [1]


def test_is_searchable():
    assert is_searchable(_item(1, content="x"))
    assert is_searchable(_item(2, content="", preview="p"))
    assert is_searchable(_item(3, content="", preview="", tags=["t"]))
    assert not is_searchable(
        ClipboardItem(kind="image", image_path="a.png", hash="i")
    )


# ---- 相关度 ----


def test_relevance_prefers_earlier_position():
    """同样命中一次，位置靠前的更相关 —— 这正是 BM25 在做的事。"""
    early = IndexHit(
        item=_item(1, content="target then a very long tail of text"), position=0, hits=1, score=3.0
    )
    late = IndexHit(
        item=_item(2, content="a very long tail of text then target"), position=30, hits=1, score=3.0
    )
    scores = relevance_map([early, late], ["target"])
    assert scores[1] > scores[2]


def test_relevance_prefers_stronger_field():
    strong = IndexHit(item=_item(1, content="target"), position=0, hits=1, score=3.0)
    weak = IndexHit(item=_item(2, content="", tags=["target"]), position=0, hits=1, score=1.5)
    scores = relevance_map([strong, weak], ["target"])
    assert scores[1] > scores[2]


def test_relevance_is_bounded():
    hit = IndexHit(
        item=_item(1, content="x" * 4 + "target", preview="target", tags=["target"], source_app="target"),
        position=4,
        hits=4,
        score=7.5,
    )
    assert relevance_map([hit], ["target"])[1] <= 1.0


def test_relevance_skips_items_without_id():
    hit = IndexHit(item=_item(1, content="target"), position=0, hits=1, score=3.0)
    hit.item.id = None
    assert relevance_map([hit], ["target"]) == {}


# ---- 后端无关的全库检索 ----


def test_scan_repository_reports_scanned_count(home):
    repo = JsonRepository(home / "clipboard.json")
    for index in range(30):
        repo.add(_item(index, content="unrelated"))
    outcome = scan_repository(repo, "unrelated")
    assert outcome.scanned == 30
    assert outcome.candidates == 30
    assert outcome.backend == "scan"
    repo.close()


def test_scan_repository_empty_query_returns_nothing(home):
    repo = JsonRepository(home / "clipboard.json")
    repo.add(_item(1, content="x"))
    outcome = scan_repository(repo, "   ")
    assert outcome.items == []
    assert outcome.scanned == 0
    repo.close()


def test_scan_repository_ranks_by_relevance(home):
    """正文命中排在只命中标签的前面，即便后者更新。"""
    repo = JsonRepository(home / "clipboard.json")
    now = time.time()
    repo.add(_item(1, content="deploy script", created_at=now - 900 * 86400))
    repo.add(_item(2, content="nothing", tags=["deploy"], created_at=now))
    outcome = scan_repository(repo, "deploy")
    assert [item.id for item in outcome.items][0] == 1
    repo.close()


def test_scan_repository_reaches_old_records(home):
    """最老的那条也能进候选集 —— 这正是本模块存在的理由。"""
    repo = JsonRepository(home / "clipboard.json")
    now = time.time()
    repo.add(_item(1, content="target", created_at=now - 900 * 86400))
    for index in range(2, 3000):
        repo.add(_item(index, content="noise", created_at=now - index))
    outcome = scan_repository(repo, "target")
    assert [item.id for item in outcome.items] == [1]
    assert outcome.scanned == 2999
    repo.close()


# ---- 与后端无关的统一检索 ----


def _seed_ranking_trap(store):
    """构造一个「只看最新一批就会答错」的库。

    真目标是**正文命中**但很老、低频；噪音是 400 条**只命中标签**的新记录。
    正文命中的相关度必须赢过标签命中，否则用户搜到的全是噪音。
    """
    now = time.time()
    store.add(
        _item(
            0,
            content="target UNIQUEPHRASE end",
            preview="target",
            hash="target",
            created_at=now - 900 * 86400,
        )
    )
    for index in range(1, 401):
        store.add(
            _item(
                index,
                content=f"pad {index}",
                preview=f"pad {index}",
                tags=["UNIQUEPHRASE"],
                hash=f"tag{index}",
                created_at=now - index,
            )
        )
    return now


@pytest.mark.parametrize("backend", ["sqlite", "json"])
def test_search_finds_field_precedence_target_on_both_backends(home, backend):
    """同一条数据、同一句查询，两个后端都要把正文命中的那条排在前面。

    这条用例来自一个只在「标签命中很多」时才出现的缺陷：候选集被
    `LIMIT` 卡在最新的一批标签命中上，正文命中的那条根本没进排序，
    于是用户搜到 20 条噪音，真正要找的记录不在其中 —— 而库里它就在那。
    """
    if backend == "sqlite":
        from linglong_clip.core.storage import Storage

        store = Storage(home / "history.db")
    else:
        store = JsonRepository(home / "clipboard.json")
    try:
        _seed_ranking_trap(store)
        engine = SemanticEngine(store)
        results = engine.search("UNIQUEPHRASE", limit=20)
        assert results, "至少该有结果"
        assert results[0].hash == "target", "正文命中的那条必须排第一"
    finally:
        store.close()


@pytest.mark.parametrize("backend", ["sqlite", "json"])
def test_search_reaches_records_outside_newest_page(home, backend):
    """3000 条历史里最老的那条也要能搜到。

    候选集若沿用「界面排序下最新 N 条」，更老的记录无论关键词多匹配
    都不会出现在结果里，而且没有任何提示。
    """
    if backend == "sqlite":
        from linglong_clip.core.storage import Storage

        store = Storage(home / "history.db")
    else:
        store = JsonRepository(home / "clipboard.json")
    try:
        now = time.time()
        store.add(_item(0, content="独一无二的老备注", hash="old", created_at=now - 900 * 86400))
        for index in range(1, 3000):
            store.add(_item(index, content=f"无关内容 {index}", created_at=now - index))
        engine = SemanticEngine(store)
        results = engine.search("独一无二")
        assert [item.hash for item in results] == ["old"]
    finally:
        store.close()


def test_search_outcome_explains_where_results_came_from(home):
    """结果要说清来路：扫了多少条、候选集多大。

    「为什么搜不到」的第一个问题永远是「它有没有进候选集」，
    而这个答案无法从结果本身看出来。
    """
    repo = JsonRepository(home / "clipboard.json")
    for index in range(40):
        repo.add(_item(index, content=f"hello {index}"))
    engine = SemanticEngine(repo)
    outcome = engine.search_outcome("hello")
    assert outcome.backend == "backend+scan"
    assert outcome.scanned == 40
    assert outcome.candidates == 40
    assert len(outcome.items) == 40
    repo.close()


def test_search_outcome_without_keywords_reports_filter_path(home):
    """没有关键词时走列表路径，不应被说成「扫描」了一遍。"""
    repo = JsonRepository(home / "clipboard.json")
    repo.add(_item(1, content="x"))
    engine = SemanticEngine(repo)
    outcome = engine.search_outcome("")
    assert outcome.backend == "filter"
    assert outcome.items
    repo.close()


def test_search_outcome_multi_token_tolerant(home):
    """多词查询任一命中即可，但全词命中的记录相关度更高。"""
    repo = JsonRepository(home / "clipboard.json")
    now = time.time()
    repo.add(_item(1, content="alpha beta", created_at=now))
    repo.add(_item(2, content="alpha only", created_at=now))
    engine = SemanticEngine(repo)
    results = engine.search("alpha beta")
    assert [item.id for item in results] == [1, 2]
    repo.close()


def test_search_rejects_missing_phrase_consistently(home):
    """两个后端对「查不到」的结论必须一致。

    分叉在这里最刺眼：同一个词，换存储方式就从「有 1 条」变成「没有」。
    """
    stores = []
    from linglong_clip.core.storage import Storage

    sqlite_store = Storage(home / "history.db")
    json_store = JsonRepository(home / "clipboard.json")
    stores.extend([sqlite_store, json_store])
    try:
        for store in stores:
            now = time.time()
            store.add(_item(0, content="target", hash="t", created_at=now - 900 * 86400))
            for index in range(1, 500):
                store.add(_item(index, content="noise", hash=f"n{index}", created_at=now - index))
        sqlite_hits = [item.hash for item in SemanticEngine(sqlite_store).search("target")]
        json_hits = [item.hash for item in SemanticEngine(json_store).search("target")]
        assert sqlite_hits == json_hits == ["t"]
    finally:
        for store in stores:
            store.close()


# ---- 快照缓存 ----


def test_search_index_caches_snapshot(home):
    repo = JsonRepository(home / "clipboard.json")
    repo.add(_item(1, content="x"))
    index = SearchIndex(repo)
    assert not index.loaded
    first = index.snapshot()
    assert index.loaded
    # 第二次不重新遍历：拿到的还是同一份
    assert index.snapshot() is first
    repo.close()


def test_search_index_invalidate_drops_snapshot(home):
    """数据变了就整份丢掉重取。

    刻意不做增量维护：那要在采集、删除、合并、同步导入、切换存储
    五条写入路径上各挂一次钩子，漏一条就变成「新记录搜不到」。
    """
    repo = JsonRepository(home / "clipboard.json")
    repo.add(_item(1, content="x"))
    index = SearchIndex(repo)
    assert len(index.snapshot()) == 1
    repo.add(_item(2, content="y"))
    assert len(index.snapshot()) == 1, "未失效时仍看到旧快照"
    index.invalidate()
    assert len(index.snapshot()) == 2
    repo.close()


def test_engine_uses_injected_index(home):
    """挂上快照后，检索不再自己遍历全库。"""
    repo = JsonRepository(home / "clipboard.json")
    repo.add(_item(1, content="hello"))
    index = SearchIndex(repo)
    engine = SemanticEngine(repo, index=index)
    assert engine.search("hello")
    assert index.loaded
    repo.close()


def test_engine_works_without_iter_all():
    """后端没有 `iter_all()` 时退化为「只问后端」，不炸。"""

    class _Minimal:
        def search(self, query, *, kind=None, limit=60):
            return [_item(1, content="hello")]

        def list(self, **kwargs):
            return [_item(1, content="hello")]

    engine = SemanticEngine(_Minimal())
    assert [item.id for item in engine.search("hello")] == [1]


def test_search_outcome_defaults():
    outcome = SearchOutcome()
    assert outcome.items == []
    assert outcome.scanned == 0
    assert outcome.candidates == 0
    assert outcome.backend == "scan"


def test_max_candidates_is_a_guardrail():
    """宽查询的护栏：扫描提前停下，不必遍历完整个库。"""
    assert MAX_CANDIDATES >= 1000
    items = [_item(index, content="same") for index in range(MAX_CANDIDATES + 500)]
    hits = scan_items(items, ["same"], limit=MAX_CANDIDATES)
    assert len(hits) == MAX_CANDIDATES


# ---- 全库扫描的代价边界 ----


def test_scan_handles_20k_records_within_budget():
    """2 万条量级必须在几十毫秒内扫完。

    这条用例不是「测性能」，是**把成本写进测试**：全库扫描是替代
    「最新一批近似全量」的代价，如果它涨到秒级，那个取舍就不成立了，
    届时要换的是方案本身（真正的倒排索引），而不是悄悄降低召回。

    阈值取得很宽（实测 30~95ms，这里放到 2 秒），CI 机器慢几个数量级
    也不该因此红灯 —— 它要拦住的是「复杂度写错导致的百倍退化」，
    不是几十毫秒的正常波动。
    """
    import time as _time

    rows = [
        _item(
            index,
            content=f"部署 脚本 会议纪要 token 备忘 配置 {index} " * 2,
        )
        for index in range(1, 20_001)
    ]
    tokens = tokenize("备忘")

    started = _time.perf_counter()
    hits = scan_items(rows, tokens, limit=500)
    elapsed = _time.perf_counter() - started

    assert hits, "2 万条里每条都含关键词，必须扫到"
    assert elapsed < 2.0, f"全库扫描退化了：{elapsed:.2f}s"


def test_scan_stops_at_candidate_limit():
    """护栏：宽查询提前停下，不必遍历完整个库。"""
    import time as _time

    rows = [_item(index, content="same") for index in range(1, MAX_CANDIDATES + 5_000)]
    started = _time.perf_counter()
    hits = scan_items(rows, ["same"], limit=200)
    elapsed = _time.perf_counter() - started

    assert len(hits) == 200
    assert elapsed < 1.0, f"命中够多时应早早停下：{elapsed:.2f}s"


def test_scan_is_case_insensitive_for_ascii_and_digit():
    rows = [
        _item(1, content="Token ABC123"),
        _item(2, content="token abc123"),
    ]
    for token in ("token", "TOKEN", "abc123", "ABC123"):
        assert len(scan_items(rows, [token])) == 2, token
