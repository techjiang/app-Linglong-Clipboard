"""语义检索：意图解析与排序。"""

import time

import pytest

from linglong_clip.core.models import ClipboardItem
from linglong_clip.core.semantic import (
    RankingWeights,
    SemanticEngine,
    parse_intent,
    score_item,
    source_matches,
)


@pytest.fixture()
def engine(storage):
    return SemanticEngine(storage)


def _seed(storage):
    now = time.time()
    rows = [
        ClipboardItem(kind="image", content="shot.png", preview="[图片]", hash="img",
                      created_at=now - 2 * 86400, source_app="flameshot"),
        ClipboardItem(kind="link", content="https://cnb.cool/x", preview="https://cnb.cool/x",
                      hash="lnk", created_at=now - 3600, source_app="google-chrome", use_count=5),
        ClipboardItem(kind="code", content="def foo():\n    return 1", preview="def foo()",
                      hash="cde", created_at=now - 100, pinned=True),
        ClipboardItem(kind="text", content="hello world", preview="hello world", hash="txt",
                      created_at=now - 50, source_app="gedit"),
        ClipboardItem(kind="link", content="https://old.com/y", preview="https://old.com/y",
                      hash="old", created_at=now - 30 * 86400, source_app="firefox"),
    ]
    for row in rows:
        storage.add(row)
    return rows


@pytest.mark.parametrize(
    "query,kinds,source,pinned,favorite,has_time",
    [
        ("上周的截图", ["image"], "", False, False, True),
        ("来自 Chrome 的链接", ["link"], "Chrome", False, False, False),
        ("固定项", [], "", True, False, False),
        ("收藏的代码", ["code"], "", False, True, False),
        ("今天的内容", [], "", False, False, True),
        ("最近图片", ["image"], "", False, False, True),
    ],
)
def test_parse_intent(query, kinds, source, pinned, favorite, has_time):
    intent = parse_intent(query)
    assert intent.kinds == kinds
    assert intent.source == source
    assert intent.pinned_only is pinned
    assert intent.favorite_only is favorite
    assert bool(intent.since) is has_time


def test_parse_intent_keeps_keywords_out_of_filters():
    intent = parse_intent("会议纪要 来自 chrome")
    assert intent.source.lower() == "chrome"
    assert "会议纪要" in intent.keywords


def test_parse_intent_strips_stop_words():
    intent = parse_intent("帮我找一下 那个 内容")
    assert "帮我" not in intent.keywords
    assert "那个" not in intent.keywords


def test_intent_has_filters_flag():
    assert parse_intent("上周的截图").has_filters is True
    assert parse_intent("hello").has_filters is False


def test_search_by_natural_language_kind(engine, storage):
    _seed(storage)
    results = engine.search("截图")
    assert len(results) == 1
    assert results[0].kind == "image"


def test_search_by_source(engine, storage):
    _seed(storage)
    results = engine.search("来自 chrome 的链接")
    assert len(results) == 1
    assert results[0].source_app == "google-chrome"


def test_search_pinned_only(engine, storage):
    _seed(storage)
    results = engine.search("固定项")
    assert len(results) == 1
    assert results[0].pinned is True


def test_search_keywords(engine, storage):
    _seed(storage)
    results = engine.search("hello")
    assert len(results) == 1
    assert results[0].content == "hello world"


def test_search_without_time_word_returns_all_matches(engine, storage):
    _seed(storage)
    hashes = {item.hash for item in engine.search("链接", limit=50)}
    assert hashes == {"lnk", "old"}


def test_search_time_range_excludes_old(engine, storage):
    """「最近」限定 3 天内，30 天前的记录必须被排除。"""
    _seed(storage)
    hashes = {item.hash for item in engine.search("最近的链接", limit=50)}
    assert "lnk" in hashes
    assert "old" not in hashes


def test_search_multi_kind_filter(engine, storage):
    _seed(storage)
    # 「链接 代码」解析出两个类型，取并集
    results = engine.search("链接 代码")
    assert {item.kind for item in results} <= {"link", "code"}


def test_search_empty_query_returns_recent(engine, storage):
    _seed(storage)
    results = engine.search("")
    assert len(results) == 5


def test_search_explain(engine):
    explanation = engine.explain("上周的截图")
    assert any("时间" in entry for entry in explanation)
    assert any("类型" in entry for entry in explanation)


def test_source_matches_alias():
    item = ClipboardItem(kind="link", content="x", source_app="google-chrome")
    assert source_matches(item, "chrome") is True
    assert source_matches(item, "Chrome") is True
    assert source_matches(item, "firefox") is False
    # 空来源表示不过滤
    assert source_matches(item, "") is True


def test_suggest(engine):
    assert "链接" in engine.suggest("链")
    assert isinstance(engine.suggest("zzz"), list)


def test_recency_weighting_prefers_newer(storage):
    now = time.time()
    fresh = ClipboardItem(kind="text", content="same text", hash="f", created_at=now)
    stale = ClipboardItem(kind="text", content="same text", hash="s", created_at=now - 60 * 86400)
    weights = RankingWeights()
    assert (
        score_item(fresh, keywords="same", weights=weights)["total"]
        > score_item(stale, keywords="same", weights=weights)["total"]
    )


def test_pinned_and_favorite_add_score():
    base = ClipboardItem(kind="text", content="x")
    pinned = ClipboardItem(kind="text", content="x", pinned=True)
    weights = RankingWeights()
    assert score_item(pinned, weights=weights)["total"] > score_item(base, weights=weights)["total"]


def test_exact_match_gets_bonus():
    weights = RankingWeights()
    exact = score_item(ClipboardItem(kind="text", content="abc"), keywords="abc", weights=weights)
    assert exact["exact"] > 0


def test_frequency_is_logarithmic():
    """高频使用的加成应当收敛，不能无限压制其它维度。"""
    weights = RankingWeights()
    low = score_item(ClipboardItem(kind="text", content="x", use_count=1), weights=weights)
    high = score_item(ClipboardItem(kind="text", content="x", use_count=10_000), weights=weights)
    assert high["frequency"] <= weights.frequency


def test_rank_orders_by_score(engine):
    now = time.time()
    items = [
        ClipboardItem(id=1, kind="text", content="target", created_at=now - 100 * 86400),
        ClipboardItem(id=2, kind="text", content="target", created_at=now, pinned=True),
    ]
    ordered = engine.rank(items, keywords="target", relevance={1: 0.5, 2: 1.0})
    assert ordered[0].id == 2
