"""语义检索：意图识别 + 多信号排序。

不引入嵌入模型，理由是模型体积与冷启动延迟对「按下快捷键就要出结果」
这类交互不可接受。这里改用两条可解释的路径：

1. **查询理解**：把 `上周的截图`、`来自 Chrome 的链接` 这类自然语言
   解析成结构化过滤条件（时间范围、类型、来源）。
2. **多信号打分**：全文相关度 + 时间衰减 + 使用频次 + 固定/收藏加权，
   最后按分数排序。四项权重都在 `RankingWeights` 里可调。
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .models import (
    CODE,
    COLOR,
    FILE,
    IMAGE,
    LINK,
    RICH,
    SENSITIVE,
    TEXT,
    ClipboardItem,
)
from .search_index import (
    BACKEND_CANDIDATE_LIMIT,
    MAX_CANDIDATES,
    IndexHit,
    SearchIndex,
    SearchOutcome,
    relevance_map,
    scan_items,
    score_item_fields,
    tokenize,
)

#: 中文/英文关键词 -> 载荷类型
KIND_KEYWORDS: Dict[str, List[str]] = {
    IMAGE: ["图片", "图像", "截图", "照片", "图", "image", "picture", "screenshot", "photo"],
    LINK: ["链接", "网址", "url", "link", "网页", "地址栏", "website"],
    FILE: ["文件", "路径", "目录", "文件夹", "file", "path", "folder", "document"],
    COLOR: ["颜色", "色值", "色彩", "color", "colour", "hex", "rgb"],
    CODE: ["代码", "函数", "脚本", "片段", "code", "snippet", "function", "script"],
    RICH: ["富文本", "格式", "样式", "rich", "formatted", "html"],
    TEXT: ["文本", "文字", "纯文本", "text", "plain"],
    SENSITIVE: ["密码", "敏感", "密钥", "password", "secret", "key"],
}

#: 这些词同时是**日常名词**，单独出现时按检索关键词处理，不当类型过滤。
#:
#: 问题是这么来的：`KIND_KEYWORDS` 把 `脚本` 映射到「代码」，机制上没问题，
#: 但「脚本」这两个字本身也常常就是用户想找的内容 —— 而用户搜的往往正是
#: 他自己刚复制过的那几个字：
#:
#: ```
#: 用户输入「脚本」      实际意图：搜含「脚本」的记录
#: 此前行为              {类型: 代码, 关键词: ''}
#:                       → 只返回「代码类」里最新的一批，一条含「脚本」二字
#:                         的文本记录都不出现，且界面只说「没有匹配的记录」
#: 用户输入「看下脚本」   此前行为 {类型: 代码, 关键词: '看下'} → 查空
#: 用户输入「路径问题」   此前行为 {类型: 文件, 关键词: '问题'} → 查空
#: ```
#:
#: 所以判据是「这个词有没有被**结构**组织成一个类型查询」，而不是它出现了没有：
#:
#: | 输入 | 判定 | 为什么 |
#: | --- | --- | --- |
#: | `脚本` | 关键词 | 裸名词，用户要的就是这两个字 |
#: | `看下脚本` | 关键词 | 上面没有类型化结构，`看下` 会被停用词吃掉 |
#: | `收藏的代码` | 类型 | 有「收藏的 + 名词」的定中结构 |
#: | `来自 Chrome 的链接` | 类型 | 有「来自 X 的 + 名词」结构 |
#: | `上周的截图` | 类型 | 有「时间 + 的 + 名词」结构 |
#: | `所有代码` / `只要图片` | 类型 | 有量化的限定词 |
#:
#: 代价是「光输一个名词就想筛某一类」不再成立。这个代价很小：界面左侧
#: 本来就有类型筛选，不需要让搜索框兼职做它；而搜不到自己刚复制的字
#: 是实打实的故障。真要按类型收窄，用 `linglong-clipboard --kind code`。
AMBIGUOUS_KIND_WORDS = frozenset(
    {
        "文本", "文字", "纯文本",
        "文件", "路径", "目录", "文件夹",
        "代码", "脚本", "片段",
        "图片", "图像", "截图", "照片", "图",
        "链接", "网址", "网页",
        "颜色", "色值", "色彩",
        "密码", "密钥",
    }
)

#: 让一个词「像类型」的结构信号。命中任意一条即认为它是被组织过的类型词。
#:
#: 这个名单刻意写得小：每加一条都要对应一个真实输入，否则
#: 「看起来更像自然语言了」这种模糊的理由会把判据越放越松，
#: 最后又回到「名词一律当类型」的老问题。
_KIND_STRUCTURE_PATTERNS = (
    # 「收藏的代码」「最近的截图」「来自 Chrome 的链接」
    re.compile(r"[的之]\s*$"),
    # 「所有代码」「只要图片」「任意文件」
    re.compile(r"(?:所有|全部|任何|任意|各个|每个|哪些|哪个|只要|仅有|只有)\s*$"),
    # 时间词直接接名词（不带「的」）：「最近图片」「今天截图」
    re.compile(r"(?:今天|昨天|本周|这周|上周|本周内|本月|这个月|上个月|最近)\s*$"),
)

#: 出现这些词时，句子明显是在描述「内容本身」而不是在挑选类型。
#: 与 `_STOP_WORDS` 不同：停用词是被删掉的，这些是**否决类型解析**的信号。
_CONTENT_HINT_WORDS = (
    "看下", "看看", "找下", "找找", "在哪", "在哪呢", "问题", "报错", "里写的",
    "里写", "的内容", "那段", "这段", "这行", "那行",
)


def _is_type_query(
    text: str, word: str, before: str, *, time_words_seen: bool = False
) -> bool:
    """这个类型词是被**结构**组织过的，还是只是碰巧出现的日常名词。

    `before` 是这个词之前的原文，`text` 是整句。判定顺序有讲究：

    1. **整句就是这个词** → 不是类型。「脚本」「截图」这种输入，
       用户要的是含这两个字的记录。
    2. **句子里有描述内容的词**（「问题」「在哪」「看下」）→ 不是类型。
       这类句子的目标是内容本身：「路径问题」想找的是那条写着路径问题的记录。
    3. **前面有类型化结构**（「收藏的」「来自 X 的」「上周的」「所有」）→ 是类型。
    4. 其余 → 不是类型。**默认不当类型**是关键：宁可多召回几条，
       也不要把用户刚复制的那段文字从结果里筛掉。
    """
    stripped = text.strip()
    if stripped.lower() == word.lower():
        return False
    for hint in _CONTENT_HINT_WORDS:
        if hint in stripped and hint != word:
            return False
    if any(pattern.search(before.strip()) for pattern in _KIND_STRUCTURE_PATTERNS):
        return True
    # 「最近图片」：时间词已被摘掉，剩下的 `before` 是空白。
    # 只看 `before` 会漏掉这种写法，所以把「句中出现过时间词」也算结构 ——
    # 「最近 + 名词」在中文里就是「最近的那批名词」，是类型查询。
    if time_words_seen and not before.strip():
        return True
    return False


#: 词 -> 类型 的反查表，让类型解析不必遍历 `KIND_KEYWORDS` 的嵌套结构。
#: 一词多类型（如 `key` 同时属于 sensitive 与 code）时，取 `KIND_KEYWORDS`
#: 里的先出现者 —— 与旧的「按字典序先匹配到谁算谁」行为一致。
_KIND_BY_WORD: Dict[str, str] = {}
for _kind, _words in KIND_KEYWORDS.items():
    for _word in _words:
        _KIND_BY_WORD.setdefault(_word.lower(), _kind)

#: 类型词在句中的匹配位置，按「长词优先」返回。
#:
#: 长词优先是为了让「截图」先于「图」被匹配到 —— 否则「截图」会被拆成
#: 「截」+「图」，判成图片类型的同时把「截」留成关键词，搜出一堆无关内容。
_KIND_WORD_RE = re.compile(
    "|".join(
        re.escape(word)
        for word in sorted(_KIND_BY_WORD, key=len, reverse=True)
    ),
    re.IGNORECASE,
)


def _iter_kind_word_matches(text: str) -> List[Tuple[str, int, int]]:
    """扫描出句中所有的类型词及其位置。

    用 `finditer` 而不是逐个 `sub`：一次遍历就能拿到全部匹配，
    且不会因为「替换了前一个词导致后一个词的上下文错位」而漏判。
    """
    found: List[Tuple[str, int, int]] = []
    for match in _KIND_WORD_RE.finditer(text):
        word = match.group(0)
        # 英文词要求两侧不是字母，避免 `chrome` 里的 `ch` 被当成类型词
        if word.isascii() and word.isalpha():
            before_ok = match.start() == 0 or not text[match.start() - 1].isalpha()
            after_ok = match.end() >= len(text) or not text[match.end()].isalpha()
            if not (before_ok and after_ok):
                continue
        found.append((word, match.start(), match.end()))
    return found


#: 时间范围关键词 -> (起始时间偏移秒数, 结束时间偏移秒数)；None 表示不限
_TIME_RANGES: Dict[str, tuple] = {
    "今天": ("today", None),
    "today": ("today", None),
    "昨天": ("yesterday", None),
    "yesterday": ("yesterday", None),
    "本周": ("week", None),
    "这周": ("week", None),
    "this week": ("week", None),
    "上周": ("last_week", "week"),
    "last week": ("last_week", "week"),
    "本月": ("month", None),
    "这个月": ("month", None),
    "this month": ("month", None),
    "上个月": ("last_month", "month"),
    "last month": ("last_month", "month"),
    "最近": ("recent", None),
    "recent": ("recent", None),
    "recently": ("recent", None),
}

#: 只在语义层生效的修饰词，不进全文检索
_STOP_WORDS = {
    "的", "了", "我", "你", "他", "她", "它", "从", "来自", "在", "是", "有", "找", "搜索",
    "查", "看看", "看下", "找下", "找找", "一下", "那个", "这个", "复制", "粘贴",
    "内容", "什么", "项", "些", "个",
    # 量化限定词：它们只用来判断「这是个类型查询」，本身不进检索
    "所有", "全部", "任何", "任意", "各个", "每个", "哪些", "哪个",
    "只要", "仅有", "只有", "来个", "那段", "这段", "这行", "那行",
    "记录", "条目", "东西", "帮我", "之前", "以前",
    "the", "a", "an", "from", "find", "search", "show", "me", "my", "in", "of", "for",
    "copy", "copied", "clipboard", "please", "item", "items", "one",
}

#: 来源应用识别（「来自 Chrome」「in firefox」）。
#:
#: 英文介词必须卡词边界。此前 `in` 没有边界约束，`index` 被拆成
#: 介词 `in` + 来源 `dex`、`install` 拆成 `in` + `stall` —— 整条查询
#: 被当成「来自 dex 的记录」，关键词全丢（来源匹配不上就退化成不过滤），
#: 界面把全部记录倒出来，用户那条索引内容反而消失。
#: 与停用词子串剥离是同一类病：把「词」当成了「子串」。
#:
#: 中文介词（来自/从/在）不加边界：中文没有词边界，加了反而会漏掉
#: 「来自Chrome」这种不带空格的写法。
_SOURCE_RE = re.compile(
    r"(?:来自|从|在|(?<![A-Za-z0-9])from(?![A-Za-z0-9])|(?<![A-Za-z0-9])in(?![A-Za-z0-9]))"
    r"\s*([A-Za-z0-9_.\-\u4e00-\u9fff]{2,40})"
    r"(?:里|中|的|复制|拷走)?",
    re.IGNORECASE,
)

#: 常见应用名 -> 关键词，用于「来自 Chrome」这类查询
APP_ALIASES: Dict[str, List[str]] = {
    "chrome": ["chrome", "google-chrome", "chromium"],
    "edge": ["msedge", "edge"],
    "firefox": ["firefox"],
    "safari": ["safari"],
    "vscode": ["code", "vscode", "visual studio code"],
    "idea": ["idea", "intellij"],
    "terminal": ["terminal", "konsole", "gnome-terminal", "alacritty", "kitty", "iterm"],
    "word": ["winword", "word"],
    "excel": ["excel"],
    "微信": ["wechat", "weixin"],
    "企业微信": ["wxwork"],
    "钉钉": ["dingtalk"],
    "飞书": ["feishu", "lark"],
    "qq": ["qq"],
    "telegram": ["telegram"],
    "slack": ["slack"],
    "discord": ["discord"],
}


@dataclass
class QueryIntent:
    """解析后的查询意图。"""

    raw: str = ""
    keywords: str = ""
    kinds: List[str] = field(default_factory=list)
    source: str = ""
    since: float = 0.0
    until: float = 0.0
    pinned_only: bool = False
    favorite_only: bool = False
    limit: int = 60
    explain: List[str] = field(default_factory=list)

    @property
    def has_filters(self) -> bool:
        return bool(self.kinds or self.source or self.since or self.until or self.pinned_only)


@dataclass
class RankingWeights:
    """排序权重。默认值来自实测：相关度为主，时间为辅。"""

    relevance: float = 1.0
    recency: float = 0.45
    frequency: float = 0.3
    pinned: float = 0.6
    favorite: float = 0.35
    exact_bonus: float = 0.8
    #: 时间衰减半衰期（天）
    half_life_days: float = 7.0


def _day_start(offset_days: int = 0) -> float:
    now = time.localtime(time.time() - offset_days * 86400)
    start = time.mktime(
        (now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, now.tm_isdst)
    )
    return start


def _resolve_range(token: str) -> tuple:
    """把范围关键词解析成 (since, until) 时间戳。"""
    now = time.time()
    if token == "today":
        return _day_start(0), 0.0
    if token == "yesterday":
        return _day_start(1), _day_start(0)
    if token == "week":
        weekday = time.localtime().tm_wday
        return _day_start(weekday), 0.0
    if token == "last_week":
        weekday = time.localtime().tm_wday
        return _day_start(weekday + 7), _day_start(weekday)
    if token == "month":
        local = time.localtime()
        return time.mktime((local.tm_year, local.tm_mon, 1, 0, 0, 0, 0, 0, -1)), 0.0
    if token == "last_month":
        local = time.localtime()
        year, month = local.tm_year, local.tm_mon - 1
        if month == 0:
            year, month = year - 1, 12
        start = time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1))
        end = time.mktime((local.tm_year, local.tm_mon, 1, 0, 0, 0, 0, 0, -1))
        return start, end
    if token == "recent":
        return now - 3 * 86400, 0.0
    return 0.0, 0.0


def _strip_stop_words(text: str) -> str:
    """把停用词按子串剔除，但**不碰拉丁词内部**。

    中文没有空格分词，「看看之前的所有链接」整串连写，停用词只能按子串剥。
    但同一条规则套到英文词上就是灾难：停用词表里有 `in`、`of`、`a` 这类
    两字母词，子串匹配会把 `nginx` 削成 `ng x`、`index` 整词消失
    （`in` + `de` + `x`）、`format` 削成 `mat`。

    症状分两种，都很隐蔽：

    - **假阳性**：`nginx` 变成 `ng x`，于是「index 的页面」也被搜了出来。
    - **假阴性**：`index` 剥成空串，整个查询退化成「无关键词」，
      界面把全部记录倒出来，用户那条明明存在的记录反而找不到了 ——
      看起来像「搜索坏了」，实际是关键词被吃掉了。

    所以判定标准是**字符类边界**而非空格：只有当停用词的两侧不是
    拉丁字母/数字时才算一次命中。这样 `nginx` 里的 `in` 两侧都是字母，
    直接跳过；而「所有的链接」里的「的」两侧是汉字，照剥不误。
    """
    if not text:
        return text
    for word in sorted(_STOP_WORDS, key=len, reverse=True):
        if len(word) < 2 or word not in text:
            continue
        # 两侧不能是拉丁字母/数字 —— 这就是「不落在词内部」的判据。
        # 中文词与英文词共用同一条规则：删除位置的邻居是汉字时，
        # 断言天然成立（汉字不是 ASCII）；只有贴到 `nginx` 的 `in`、
        # URL 里 `/info` 的 `in` 这类位置才会被挡住。
        pattern = re.compile(
            r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        text = pattern.sub(" ", text)
    return text


def parse_intent(query: str) -> QueryIntent:
    """把自然语言查询拆成「过滤条件 + 检索关键词」。"""
    intent = QueryIntent(raw=query or "")
    text = (query or "").strip()
    if not text:
        return intent

    lowered = text.lower()
    remaining = f" {text} "
    time_words_seen = False

    # 1. 时间范围（长词优先，避免「上周」被「周」抢先匹配）
    for phrase in sorted(_TIME_RANGES, key=len, reverse=True):
        if phrase in lowered:
            since, until = _resolve_range(_TIME_RANGES[phrase][0])
            if since:
                intent.since = since
                intent.until = until or intent.until
                intent.explain.append(f"时间：{phrase}")
            if _TIME_RANGES[phrase][1]:
                _, until2 = _resolve_range(_TIME_RANGES[phrase][1])
                intent.until = intent.until or until2
            # 占位成与时间词等长的空白，而不是一个空格：位置对齐后，
            # 后面判断「类型词前面写了什么」时才能分清「前面是时间词」
            # 与「前面什么都没有」—— 「最近图片」是类型查询，
            # 「图片」不是。
            remaining = re.sub(
                re.escape(phrase), " " * len(phrase), remaining, flags=re.IGNORECASE
            )
            time_words_seen = True
            break

    # 2. 类型。歧义词要「被结构组织过」才算类型，见 `_is_type_query()`
    #
    #    这里对所有类型词（含非歧义的）统一处理：`_is_type_query` 只对
    #    歧义词生效，其余一律照旧解析，所以「链接」这类同义词的行为不变。
    for word_position in _iter_kind_word_matches(remaining):
        word, start, end = word_position
        kind = _KIND_BY_WORD.get(word.lower())
        if kind is None:
            continue
        if word.lower() in AMBIGUOUS_KIND_WORDS and not _is_type_query(
            text, word, remaining[:start], time_words_seen=time_words_seen
        ):
            continue  # 留作检索关键词
        if kind not in intent.kinds:
            intent.kinds.append(kind)
            intent.explain.append(f"类型：{word}")
        # 逐个替换而不是 `pattern.sub`：一次只处理一个类型词，
        # 才不会把「所有链接 所有代码」里的第二个词当成第一个的后续内容跳过
        remaining = remaining[:start] + " " * (end - start) + remaining[end:]

    # 3. 固定 / 收藏
    pinned_pattern = re.compile(r"固定|置顶|pinned?", re.IGNORECASE)
    if pinned_pattern.search(remaining):
        intent.pinned_only = True
        intent.explain.append("仅固定项")
        remaining = pinned_pattern.sub(" ", remaining)
    favorite_pattern = re.compile(r"收藏|favorite|starred?", re.IGNORECASE)
    if favorite_pattern.search(remaining):
        intent.favorite_only = True
        intent.explain.append("仅收藏项")
        remaining = favorite_pattern.sub(" ", remaining)

    # 4. 来源应用
    match = _SOURCE_RE.search(remaining)
    if match:
        candidate = match.group(1).strip()
        if candidate and candidate.lower() not in _STOP_WORDS:
            intent.source = candidate
            intent.explain.append(f"来源：{candidate}")
            remaining = remaining[: match.start()] + " " + remaining[match.end() :]

    # 5. 剩余部分是关键词。中文没有空格分词，除按空格切分外，
    #    还要把停用词按子串剔除，否则「帮我找一下」会整段留下。
    remaining = _strip_stop_words(remaining)
    words = [word for word in remaining.split() if word.lower() not in _STOP_WORDS]
    intent.keywords = " ".join(words).strip()
    return intent


def resolve_source_aliases(source: str) -> List[str]:
    """把「Chrome」这类别名展开成可能的应用标识片段。"""
    lowered = (source or "").lower().strip()
    if not lowered:
        return []
    for key, aliases in APP_ALIASES.items():
        if lowered in key or lowered in aliases or key in lowered:
            return aliases + [lowered]
    return [lowered]


def source_matches(item: ClipboardItem, source: str) -> bool:
    if not source:
        return True
    haystack = f"{item.source_app} {item.source_title}".lower()
    return any(alias in haystack for alias in resolve_source_aliases(source))


class ScoreBreakdown(dict):
    """打分明细，方便调试与在 UI 上解释排序原因。"""

    @property
    def total(self) -> float:
        return float(self.get("total", 0.0))


def score_item(
    item: ClipboardItem,
    *,
    keywords: str = "",
    relevance: float = 0.0,
    weights: Optional[RankingWeights] = None,
    now: Optional[float] = None,
) -> ScoreBreakdown:
    """计算单条记录的综合得分。

    `relevance` 由调用方（全文检索）提供，取值范围随意，最终会被归一化。
    """
    weights = weights or RankingWeights()
    now = now or time.time()

    recency_raw = 1.0
    age_days = max(0.0, (now - max(item.created_at, item.used_at)) / 86400)
    if weights.half_life_days > 0:
        recency_raw = math.pow(0.5, age_days / weights.half_life_days)

    # 使用频次取对数，避免高频复制项长期压制其它结果
    frequency_raw = math.log1p(item.use_count) / math.log1p(20)
    frequency_raw = min(1.0, frequency_raw)

    bonus = weights.pinned * (1.0 if item.pinned else 0.0)
    bonus += weights.favorite * (1.0 if item.favorite else 0.0)

    exact = 0.0
    if keywords:
        lowered = item.content.lower()
        if lowered.strip() == keywords.lower().strip():
            exact = weights.exact_bonus
        elif keywords.lower() in lowered:
            exact = weights.exact_bonus * 0.4

    rel = weights.relevance * min(1.0, relevance) if relevance else 0.0
    total = (
        rel
        + weights.recency * recency_raw
        + weights.frequency * frequency_raw
        + bonus
        + exact
    )
    return ScoreBreakdown(
        {
            "total": total,
            "relevance": rel,
            "recency": weights.recency * recency_raw,
            "frequency": weights.frequency * frequency_raw,
            "bonus": bonus,
            "exact": exact,
        }
    )


class SemanticEngine:
    """把检索、意图解析、排序串起来。

    依赖注入 `storage`，因此可在测试中换成内存库。
    """

    def __init__(
        self,
        storage,
        weights: Optional[RankingWeights] = None,
        *,
        index: Optional[SearchIndex] = None,
    ) -> None:
        self._storage = storage
        self.weights = weights or RankingWeights()
        #: 可选的跨查询全库快照，见 `use_index()`
        self._index = index

    def rank(
        self,
        candidates: Sequence[ClipboardItem],
        *,
        keywords: str = "",
        relevance: Optional[Dict[int, float]] = None,
    ) -> List[ClipboardItem]:
        """按综合得分重排候选集。"""
        relevance = relevance or {}
        now = time.time()
        scored = [
            (
                score_item(
                    item,
                    keywords=keywords,
                    relevance=relevance.get(item.id or -1, 0.0),
                    weights=self.weights,
                    now=now,
                )["total"],
                item,
            )
            for item in candidates
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored]

    def search(
        self,
        query: str,
        *,
        kind: Optional[str] = None,
        source: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
        limit: int = 60,
        pinned_only: bool = False,
        favorite_only: bool = False,
        use_intent: bool = True,
    ) -> List[ClipboardItem]:
        """统一检索入口：自然语言或直接关键词都能用。"""
        return self.search_outcome(
            query,
            kind=kind,
            source=source,
            since=since,
            until=until,
            limit=limit,
            pinned_only=pinned_only,
            favorite_only=favorite_only,
            use_intent=use_intent,
        ).items

    def search_outcome(
        self,
        query: str,
        *,
        kind: Optional[str] = None,
        source: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
        limit: int = 60,
        pinned_only: bool = False,
        favorite_only: bool = False,
        use_intent: bool = True,
    ) -> SearchOutcome:
        """检索并**说明结果是怎么来的**：候选集多大、扫了多少条、用的哪条路径。

        `search()` 只是它的 `.items`。分成两个方法是为了让
        「为什么搜不到」这类排查能拿到扫描量 —— 结果本身看不出这件事：
        「没有匹配」与「没进候选集」在界面上长得完全一样。
        """
        query = (query or "").strip()
        intent = parse_intent(query) if query and use_intent else QueryIntent(keywords=query)
        effective_kind = kind or (intent.kinds[0] if len(intent.kinds) == 1 else None)
        effective_since = since or intent.since
        effective_source = source or intent.source
        # 关键点：`intent.keywords` 为空表示整句都被解析成了过滤条件
        # （如「上周的截图」），此时不能退回原串去全文检索 ——
        # 图片、文件类记录本身没有可检索文本，退回必然查空。
        keywords = intent.keywords
        if query and not keywords and not intent.has_filters:
            keywords = query

        if not keywords:
            # 无关键词：按过滤条件列出。`list` 的 `limit` 是界面意义上的
            # 「前 N 条」，这里不该装作是全量 —— 但也不该把候选收得比
            # 请求的条数还少，否则「只看固定项」这类查询会凭空少结果。
            candidate_limit = max(limit * 4, 200)
            if effective_source:
                # 来源别名匹配只能在内存里做，先多取一些
                candidate_limit = max(candidate_limit, 1000)
            candidates = self._storage.list(
                kind=effective_kind,
                pinned_only=pinned_only or intent.pinned_only,
                favorite_only=favorite_only or intent.favorite_only,
                since=max(effective_since, 0) or None,
                limit=candidate_limit,
            )
            filtered = self._filter(
                candidates,
                effective_kind=effective_kind,
                intent=intent,
                effective_since=effective_since,
                until=until,
                effective_source=effective_source,
                pinned_only=pinned_only,
                favorite_only=favorite_only,
            )
            return SearchOutcome(
                items=filtered[:limit],
                backend="filter",
                scanned=len(candidates),
                candidates=len(filtered),
            )

        candidates, backend, scanned = self._candidates(
            keywords, query=query, kind=effective_kind
        )
        filtered = self._filter(
            candidates,
            effective_kind=effective_kind,
            intent=intent,
            effective_since=effective_since,
            until=until,
            effective_source=effective_source,
            pinned_only=pinned_only,
            favorite_only=favorite_only,
        )
        relevance = self.relevance_from(keywords, filtered)
        return SearchOutcome(
            items=self.rank(filtered, keywords=keywords, relevance=relevance)[:limit],
            backend=backend,
            scanned=scanned,
            candidates=len(filtered),
            terms=tokenize(keywords),
        )

    def _candidates(
        self, keywords: str, *, query: str, kind: Optional[str]
    ) -> tuple:
        """取候选集：先问后端（SQLite 有 FTS5 这个真索引），再全库补齐。

        为什么要补这一刀：后端检索把 `LIMIT` 截在**结果集**上，
        于是「最近这一批记录里有没有」决定了最终还能看到什么。实测过一个
        会误导人的场景 —— 真目标是正文命中但很老，另有 400 条只命中标签的
        新记录：

        ```
        后端只返回标签命中的那 400 条（正文命中的那条被 LIMIT 切掉了）
        语义层再按相关度排序，也只能在这 400 条噪音里排
        用户看到 20 条噪音，真正要找的记录不在其中 —— 而库里它就在那
        ```

        更糟的是两个后端不一致：JSON 那边是纯子串扫描、排序规则不同，
        同一条记录换个存储方式就搜得到。结果不该取决于数据存在哪。

        所以 FTS 命中就够多时走快路径（省掉一次全库扫描），
        不够时用全库扫描补齐，两边合并去重。
        """
        found = self._storage.search(keywords, kind=kind, limit=BACKEND_CANDIDATE_LIMIT)
        if not found and keywords != query:
            # 关键词被停用词吃空时，用原串再查一次
            found = self._storage.search(query, kind=kind, limit=BACKEND_CANDIDATE_LIMIT)
        if len(found) >= BACKEND_CANDIDATE_LIMIT:
            # 后端给满了上限，说明它还有更多没给我们；这时不再全库扫描，
            # 但要如实说明走的是哪条路（`--search --json` 里看得到）
            return found, "backend", len(found)

        scanned: List[int] = [0]
        hits = scan_items(
            self._all_items(),
            tokenize(keywords),
            limit=MAX_CANDIDATES,
            scanned_out=scanned,
        )
        merged: Dict[int, ClipboardItem] = {}
        for item in found:
            if item.id is not None:
                merged[item.id] = item
        for hit in hits:
            if hit.item.id is not None:
                merged.setdefault(hit.item.id, hit.item)
        return list(merged.values()), "backend+scan", scanned[0]

    def _all_items(self) -> Sequence[ClipboardItem]:
        """全库视图：优先用外部注入的快照，其次按需遍历一次。"""
        if self._index is not None:
            return self._index.snapshot()
        iter_all = getattr(self._storage, "iter_all", None)
        if iter_all is None:
            return []
        return list(iter_all())

    def use_index(self, index: Optional[SearchIndex]) -> None:
        """挂上一个跨查询复用的全库快照。

        界面按输入逐字触发检索，没有它每次都要重新遍历全库。
        不挂也能工作（每次查询自己遍历），只是多花一点。
        """
        self._index = index

    def relevance_from(
        self, keywords: str, items: Sequence[ClipboardItem]
    ) -> Dict[int, float]:
        """给候选集算相关度：有字段明细时用字段权重，否则退回位置估计。"""
        tokens = tokenize(keywords)
        if not tokens:
            return {}
        hits: List[IndexHit] = []
        for item in items:
            score, hit_count, matched = score_item_fields(item, tokens)
            if not hit_count:
                continue
            hits.append(
                IndexHit(
                    item=item,
                    position=self._content_position(item, tokens),
                    hits=hit_count,
                    fields=matched,
                    score=score,
                )
            )
        return relevance_map(hits, tokens)

    @staticmethod
    def _content_position(item: ClipboardItem, tokens: Sequence[str]) -> int:
        text = (item.preview or item.content or "").lower()
        positions = [text.find(token.lower()) for token in tokens]
        positions = [pos for pos in positions if pos >= 0]
        return min(positions) if positions else 0

    def _filter(
        self,
        candidates: Sequence[ClipboardItem],
        *,
        effective_kind: Optional[str],
        intent: QueryIntent,
        effective_since: float,
        until: Optional[float],
        effective_source: str,
        pinned_only: bool,
        favorite_only: bool,
    ) -> List[ClipboardItem]:
        """按意图里的过滤条件收窄候选集。

        过滤收在这里而不是散在 SQL 里，是因为**来源别名匹配只能在内存里做**：
        库里存的是 `google-chrome`，用户说的是「chrome」，
        把它透传给 SQL 的精确匹配必然查空。
        """
        filtered: List[ClipboardItem] = []
        for item in candidates:
            if effective_kind and item.kind != effective_kind:
                continue
            if intent.kinds and len(intent.kinds) > 1 and item.kind not in intent.kinds:
                continue
            if effective_since and max(item.created_at, item.used_at) < effective_since:
                continue
            if until and item.created_at > until:
                continue
            if effective_source and not source_matches(item, effective_source):
                continue
            if pinned_only or intent.pinned_only:
                if not item.pinned:
                    continue
            if favorite_only or intent.favorite_only:
                if not item.favorite:
                    continue
            filtered.append(item)
        return filtered

    def suggest(self, query: str, *, limit: int = 5) -> List[str]:
        """为查询框提供补全建议，来源是历史里的高频词与标签。"""
        suggestions: List[str] = []
        prefix = (query or "").strip().lower()
        for kind, words in KIND_KEYWORDS.items():
            for word in words:
                if word.lower().startswith(prefix) and word not in suggestions:
                    suggestions.append(word)
        for _, aliases in APP_ALIASES.items():
            for alias in aliases:
                if alias.startswith(prefix) and alias not in suggestions:
                    suggestions.append(alias)
        return suggestions[:limit]

    def explain(self, query: str) -> List[str]:
        """返回查询被如何理解，用于 UI 提示「已按…筛选」。"""
        return parse_intent(query).explain
