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
        # 「最近图片」的「图片」前面只有时间词、没有「的」：仍算类型查询
        ("最近图片", ["image"], "", False, False, True),
        ("最近的图片", ["image"], "", False, False, True),
        ("所有代码", ["code"], "", False, False, False),
        # 裸名词不是类型查询 —— 用户要的是含这两个字的记录
        ("脚本", [], "", False, False, False),
        ("链接", [], "", False, False, False),
        ("剪贴板里的路径", ["file"], "", False, False, False),
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


# ---- 日常名词不该被当成类型过滤 ----
#
# `KIND_KEYWORDS` 把「脚本」映射到「代码」本身没错，但「脚本」这两个字
# 也常常就是用户想找的内容。此前它被无条件吃掉，于是「看下脚本」变成
# {类型: 代码, 关键词: '看下'}，含「脚本」二字的记录一条都不出现 ——
# 用户看到的是「没有匹配的记录」，而那条记录就在库里。


@pytest.mark.parametrize(
    "query,keywords",
    [
        ("脚本", "脚本"),
        ("链接", "链接"),
        ("图片", "图片"),
        ("部署 脚本", "部署 脚本"),
        ("看下脚本", "脚本"),        # 「看下」是停用词，会被剔掉
        ("路径问题", "路径问题"),
        ("文件在哪", "文件在哪"),
        ("那段代码有问题", "代码有问题"),
    ],
)
def test_bare_nouns_stay_as_keywords(query, keywords):
    intent = parse_intent(query)
    assert intent.kinds == [], f"{query!r} 不该被解析成类型过滤"
    assert intent.keywords == keywords


@pytest.mark.parametrize(
    "query,kind",
    [
        ("收藏的代码", "code"),
        ("最近的链接", "link"),
        ("来自 Chrome 的链接", "link"),
        ("所有文件", "file"),
        ("任意图片", "image"),
        ("剪贴板里的路径", "file"),
    ],
)
def test_structured_phrases_still_resolve_kind(query, kind):
    """有类型化结构的句子照旧按类型解析 —— 这是自然语言检索的主要用法。

    判据是「有没有被结构组织过」，不是「出现了名词没有」：
    这条线画错任何一边都会让一类真实输入失效。
    """
    assert kind in parse_intent(query).kinds


def test_multi_noun_query_is_not_silently_narrowed():
    """「链接 代码」此前会解析成两个类型、关键词为空，把无关的记录也捞进来。

    两个日常名词并列时，用户想搜的是这两个词，不是「这两类的最新一批」。
    """
    intent = parse_intent("链接 代码")
    assert intent.kinds == []
    assert "链接" in intent.keywords and "代码" in intent.keywords


def test_parse_intent_strips_stop_words():
    intent = parse_intent("帮我找一下 那个 内容")
    assert "帮我" not in intent.keywords
    assert "那个" not in intent.keywords


def test_intent_has_filters_flag():
    assert parse_intent("上周的截图").has_filters is True
    assert parse_intent("hello").has_filters is False


def test_search_by_natural_language_kind(engine, storage):
    """「上周的截图」按类型 + 时间收窄（有「的」这个结构信号）。"""
    _seed(storage)
    results = engine.search("上周的截图")
    assert [item.hash for item in results] == ["img"]


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


def test_bare_kind_noun_searches_text_not_kind(engine, storage):
    """搜「链接」应当按**文字**匹配，而不是筛出所有链接类记录。

    种子里有一条 `content="https://cnb.cool/x"` 的链接 —— 它不含「链接」二字，
    所以不该出现在结果里；而「含链接二字的文本」即便类型不是 link 也该出现。
    这一条钉住的是「名词当关键词」这个改动本身。
    """
    _seed(storage)
    storage.add(
        ClipboardItem(
            kind="text",
            content="把链接发我",
            preview="把链接发我",
            hash="ask",
            source_app="wechat",
        )
    )
    hashes = {item.hash for item in engine.search("链接", limit=50)}
    assert hashes == {"ask"}


def test_search_time_range_excludes_old(engine, storage):
    """「最近」限定 3 天内，30 天前的记录必须被排除。"""
    _seed(storage)
    hashes = {item.hash for item in engine.search("最近的链接", limit=50)}
    assert "lnk" in hashes
    assert "old" not in hashes


def test_search_multi_kind_filter(engine, storage):
    """两个类型名带结构地并列时取并集（用「的」把它们组织起来）。"""
    _seed(storage)
    results = engine.search("所有链接 所有代码", limit=50)
    assert {item.kind for item in results} <= {"link", "code"}
    assert {item.kind for item in results} == {"link", "code"}


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
