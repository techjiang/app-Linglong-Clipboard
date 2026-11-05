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
    """「上周的截图」按类型 + 时间收窄（有「的」这个结构信号）。

    这条用例必须自己摆一个「确定落在上周」的时刻，不能靠 `_seed` 里的
    「两天前」—— 那样命中与否取决于今天星期几：周一跑空、周二跑对，
    只在周一周二通过。用 `_last_week_midday()` 把样本钉进上周区间，
    用例才与运行日期无关。
    """
    _seed(storage)
    previous = ClipboardItem(
        kind="image", content="last-week.png", preview="[图片]", hash="img",
        created_at=_last_week_midday(), source_app="flameshot",
    )
    storage.add(previous)

    results = engine.search("上周的截图")
    hashes = [item.hash for item in results]
    # `_seed` 里那张「两天前」的图是否算上周，取决于今天星期几；
    # 这里只断言「确定在上周的那张被搜到」，别把日期规律写死进断言。
    assert "img" in hashes
    assert all(item.kind == "image" for item in results)


def _last_week_midday() -> float:
    """上周三中午 —— 无论今天星期几，这个时刻都落在一个完整的上周区间内。

    注意 `_day_start(n)` 的参数是「往前推 n 天」，不是星期序号。
    """
    intent = parse_intent("上周")
    return intent.since + (2 * 86400)


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


#: 这些词都含有停用词作为**子串**，但本身是完整的技术关键词。
#: 停用词表里有 `in` / `of` / `me` / `my` / `a` 这类两字母词，
#: 早期实现按子串剥，把这些词削得面目全非（`nginx` -> `ng x`、
#: `index` -> 空串、`format` -> `mat`）。21% 的常见技术词中招。
_SUBSTRING_STOPWORD_VICTIMS = [
    "nginx", "index", "format", "main", "container", "branch", "login",
    "warning", "install", "mysql", "ansible", "terraform", "handler",
    "memory", "balance", "metric", "span", "profile", "merge", "inside",
    "intellij", "console", "prompt", "commit", "release",
]


@pytest.mark.parametrize("word", _SUBSTRING_STOPWORD_VICTIMS)
def test_latin_keyword_survives_stop_word_stripping(word):
    """拉丁词不能因为「碰巧含了停用词」就被拆开。

    这是本轮修掉的一处静默缺陷：关键词被削坏后，
    - 削成别的片段 → 假阳性（搜 `nginx` 捞出 `index` 的记录）
    - 削成空串 → 整个查询退化成「无关键词」，界面把全部记录倒出来，
      用户那条明明存在的记录反而找不到，看着就像「搜索坏了」
    """
    intent = parse_intent(word)
    assert intent.keywords == word
    assert intent.kinds == []
    assert intent.source == ""


@pytest.mark.parametrize(
    "word,damaged",
    [
        # 抽查几个「曾经被削成什么」，防止有人把子串剥法改回来
        ("nginx", "ng x"),
        ("format", "mat"),
        ("branch", "br ch"),
        ("profile", "pr ile"),
    ],
)
def test_latin_keyword_is_not_the_damaged_form(word, damaged):
    assert parse_intent(word).keywords != damaged


@pytest.mark.parametrize("word", ["index", "install"])
def test_latin_keyword_never_becomes_empty(word):
    """空关键词最危险：它等于关掉过滤，把全部记录倒出来。

    这两条还额外踩到第二个缺陷 —— `_SOURCE_RE` 里的英文介词 `in`
    没有词边界约束，`index` 被拆成「介词 in + 来源 dex」、
    `install` 拆成「in + stall」，于是关键词没了、来源还成了垃圾值。
    """
    intent = parse_intent(word)
    assert intent.keywords == word
    assert intent.source == ""


@pytest.mark.parametrize(
    "query",
    ["来自 Chrome 的链接", "来自 Chrome", "从 vscode 复制的代码", "in firefox", "from Safari"],
)
def test_source_detection_still_works_after_boundary_fix(query):
    """给英文介词加边界约束后，正常的来源识别必须照旧。

    卡边界是为了不让 `index` 里的 `in` 被当成介词，
    但真正的「in firefox」不能被一起挡掉 —— 那才是这条规则的用途。
    """
    assert parse_intent(query).source != ""


def test_chinese_stop_word_stripping_is_unchanged():
    """中文没有词边界，停用词仍按子串剥 —— 这条行为不能被英文的修复带偏。"""
    intent = parse_intent("看看之前的所有链接")
    assert "之前" not in intent.keywords
    assert "所有" not in intent.keywords
    assert "链接" in intent.keywords

    assert parse_intent("帮我找下那段代码").keywords.endswith("代码")
    assert "帮我" not in parse_intent("帮我找一下 部署").keywords


def test_stop_word_inside_url_is_not_stripped():
    """URL 里也藏着 `in` 这类片段（`/info`）；它们同样不该被剥。"""
    intent = parse_intent("https://example.com/info")
    assert "info" in intent.keywords


def test_search_finds_latin_keyword_end_to_end(storage):
    """端到端：这三个词此前分别命中 4 条（含 3 条无关）/ 0 条 / 0 条。"""
    engine = SemanticEngine(storage)
    rows = [
        "server { index index.html; }",
        "npm install --save lodash",
        "nginx -t 配置文件检查",
        "https://index.example.com/a",
        "一条完全无关的记录",
    ]
    for i, content in enumerate(rows):
        storage.add(ClipboardItem(kind="text", content=content, hash=f"h{i}", created_at=1000.0 + i))

    nginx = [item.content for item in engine.search("nginx")]
    assert nginx == ["nginx -t 配置文件检查"]

    index_hits = {item.content for item in engine.search("index")}
    assert "server { index index.html; }" in index_hits
    assert "一条完全无关的记录" not in index_hits
    assert "nginx -t 配置文件检查" not in index_hits

    assert [item.content for item in engine.search("install")] == ["npm install --save lodash"]
