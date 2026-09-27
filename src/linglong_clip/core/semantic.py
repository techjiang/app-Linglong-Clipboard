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
    "查", "看看", "一下", "那个", "这个", "复制", "粘贴", "内容", "什么", "项", "些", "个",
    "记录", "条目", "东西", "帮我", "之前", "以前",
    "the", "a", "an", "from", "find", "search", "show", "me", "my", "in", "of", "for",
    "copy", "copied", "clipboard", "please", "item", "items", "one",
}

_SOURCE_RE = re.compile(
    r"(?:来自|从|在|from|in)\s*([A-Za-z0-9_.\-\u4e00-\u9fff]{2,40})"
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


def parse_intent(query: str) -> QueryIntent:
    """把自然语言查询拆成「过滤条件 + 检索关键词」。"""
    intent = QueryIntent(raw=query or "")
    text = (query or "").strip()
    if not text:
        return intent

    lowered = text.lower()
    remaining = f" {text} "

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
            remaining = re.sub(re.escape(phrase), " ", remaining, flags=re.IGNORECASE)
            break

    # 2. 类型
    for kind, words in KIND_KEYWORDS.items():
        for word in sorted(words, key=len, reverse=True):
            pattern = re.compile(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", re.IGNORECASE)
            if pattern.search(remaining):
                if kind not in intent.kinds:
                    intent.kinds.append(kind)
                    intent.explain.append(f"类型：{word}")
                remaining = pattern.sub(" ", remaining)
                break

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
    for word in sorted(_STOP_WORDS, key=len, reverse=True):
        if len(word) >= 2 and word in remaining:
            remaining = remaining.replace(word, " ")
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

    def __init__(self, storage, weights: Optional[RankingWeights] = None) -> None:
        self._storage = storage
        self.weights = weights or RankingWeights()

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
        query = (query or "").strip()
        if not query:
            return self._storage.list(
                kind=kind,
                pinned_only=pinned_only,
                favorite_only=favorite_only,
                source_app=source,
                since=since,
                limit=limit,
            )

        intent = parse_intent(query) if use_intent else QueryIntent(keywords=query)
        effective_kind = kind or (intent.kinds[0] if len(intent.kinds) == 1 else None)
        effective_since = since or intent.since
        effective_source = source or intent.source
        # 关键点：`intent.keywords` 为空表示整句都被解析成了过滤条件
        # （如「上周的截图」），此时不能退回原串去全文检索 ——
        # 图片、文件类记录本身没有可检索文本，退回必然查空。
        keywords = intent.keywords
        if not keywords and not intent.has_filters:
            keywords = query

        # 有来源过滤时把候选集放大：别名匹配只能在内存里做，
        # 先按类型/时间收窄再筛，条数不够会让结果凭空消失。
        candidate_limit = max(limit * 4, 200)
        if effective_source:
            candidate_limit = max(candidate_limit, 1000)
        if not keywords:
            # 不把来源透传给 SQL：库里存的是 `google-chrome`，
            # 用户说的是「chrome」，精确匹配必然查空。
            # 来源一律留到下面的内存过滤里按别名判定。
            candidates = self._storage.list(
                kind=effective_kind,
                pinned_only=pinned_only or intent.pinned_only,
                favorite_only=favorite_only or intent.favorite_only,
                since=max(effective_since, 0) or None,
                limit=candidate_limit,
            )
        else:
            # 类型多选时先放宽到全量候选，再在内存里过滤
            candidates = self._storage.search(
                keywords, kind=effective_kind, limit=candidate_limit
            )
            if not candidates and keywords != query:
                # 关键词被停用词吃空时，用原串再查一次
                candidates = self._storage.search(
                    query, kind=effective_kind, limit=candidate_limit
                )

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

        relevance = self._relevance_map(filtered, keywords)
        return self.rank(filtered, keywords=keywords, relevance=relevance)[:limit]

    @staticmethod
    def _relevance_map(items: Sequence[ClipboardItem], keywords: str) -> Dict[int, float]:
        """用子串命中位置与长度粗略估计相关度，供排序加权。"""
        if not keywords:
            return {}
        probe = keywords.lower()
        result: Dict[int, float] = {}
        for item in items:
            if item.id is None:
                continue
            content = (item.preview or item.content).lower()
            if probe == content.strip():
                result[item.id] = 1.0
            elif probe in content:
                # 命中越靠前越相关
                position = content.index(probe)
                result[item.id] = max(0.35, 0.9 - position / max(len(content), 1) * 0.5)
            else:
                result[item.id] = 0.25
        return result

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
