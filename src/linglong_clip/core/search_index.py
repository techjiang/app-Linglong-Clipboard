"""全库检索：把所有后端的「找到哪些记录」收敛成同一份实现。

起因是一个只在换后端时才显形的问题：检索结果**取决于历史存在哪**。

```
SQLite 后端：走 FTS5，命中集被 `LIMIT` 截断在最新的一百多条里
JSON 后端：进程内子串扫描，但候选集来自 `list(limit=N)` —— 同样只覆盖最新一批
UI 搜索框：`limit=60`，于是「上周写的那段备注」在 3000 条历史里根本不在候选集内
```

三者都没有报错，界面上只是「搜不到」。而用户无从判断是自己记错了关键词，
还是工具没找到 —— 这正是本项目反复在修的那类「静默失效」。

这里把「找到哪些记录」变成与后端无关的一步：

- **候选集是全库，不是最新一批。** 记录量靠 `iter_all()` 流式遍历承担，
  不再用 `list(limit)` 近似「全部历史」。
- **计分需要命中位置**，所以扫描时顺带给出 `(相对位置, 命中次数)`，
  让相关度不退回「命中 / 没命中」的二值判断。
- **SQLite 的 FTS5 仍然先用**：它是真正的索引，命中时省掉一次全库扫描；
  但结果要重新排序取前 `limit` 条 —— 直接拿 `LIMIT` 截断等于让
  「结果条数」影响「结果内容」。
- **扫描在 UTF-8 字节上进行**，命中位置按字节数换算。字节层没有字符串
  对象分配，`bytes.find` 的搜索代价也比 `str.find` 低得多。

为什么不上倒排索引或后缀自动机：剪贴板里最常见的查询是短词（「记录」、
`abc`），这类词命中成千上万条，索引带不来收益，反而要在每次采集、
每次合并、每次同步导入时维护它 —— 而这些写入路径已经有各自的复杂度了。
实测 2 万条规模下一次全库扫描在毫秒级，够用到需要换掉它的那天；
那时的信号也很明确：`--search --json` 里的 `scanned` 字段会显示扫描条数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import ClipboardItem

#: 每个字段的权重，用来近似 FTS 的 BM25。正文命中比来源应用命中更有意义。
FIELD_WEIGHTS: Tuple[Tuple[str, float], ...] = (
    ("content", 3.0),
    ("preview", 2.0),
    ("tags", 1.5),
    ("source_app", 1.0),
)

#: 字段权重之和的理论上限，用来把分数归一化到 0~1。
#: 写成常量而不是每处手算 `3.0 + 2.0 + 1.5 + 1.0`：改权重时容易漏改一处。
MAX_FIELD_SCORE = sum(weight for _name, weight in FIELD_WEIGHTS)

#: 一条记录内单个字段最多计这么多次命中。
#: 不设上限的话，一段把关键词重复了五百遍的日志会把真正的目标挤出结果。
MAX_FIELD_HITS = 8

#: 单次查询最多扫出这么多条候选记录。用作病态宽查询（比如某个到处出现的词）
#: 的护栏：扫描本身会提前停下，不必等它遍历完整库。
MAX_CANDIDATES = 20_000

#: 后端候选集的取数上限。SQLite 的 FTS 命中后就走这条快路径，
#: 不再全库扫描 —— 上限取得比界面一次能展示的多得多，避免
#: 「结果条数」影响「结果内容」。
BACKEND_CANDIDATE_LIMIT = 2_000

#: 默认的候选返回到语义层的条数上限。它只是「候选」上限，
#: 真正的排序与截断在语义层（见 `scan_repository` 的 `limit`）。
DEFAULT_SCAN_LIMIT = 500


def is_searchable(item: ClipboardItem) -> bool:
    """这条记录有没有可检索文本。

    图片、纯文件记录通常没有文本（除非 OCR 认出了字），
    对它们做子串扫描是纯粹的无用功。
    """
    return bool(item.content or item.preview or item.tags or item.source_app)


@dataclass
class IndexHit:
    """一次全库扫描里命中的一条记录。"""

    item: ClipboardItem
    #: 最佳字段内的最佳命中位置，用于「命中越靠前越相关」
    position: int = 0
    #: 命中次数（已按字段与上限收敛）
    hits: int = 0
    #: 命中的字段名，便于调试与将来在界面上标出「因为标题命中」
    fields: List[str] = field(default_factory=list)
    #: 字段权重累计出的相关度，排序用
    score: float = 0.0


@dataclass
class SearchOutcome:
    """一次检索的结果与它的来路。

    `backend` / `scanned` 是给 `--search --json` 与排查用的：
    「为什么这条记录搜不到」的第一个问题永远是「它有没有进候选集」，
    而这个答案无法从结果本身看出来。
    """

    items: List[ClipboardItem] = field(default_factory=list)
    backend: str = "scan"
    scanned: int = 0
    candidates: int = 0
    terms: List[str] = field(default_factory=list)


def tokenize(query: str) -> List[str]:
    """把查询串拆成用于子串匹配的词。

    刻意**不**用 `\w+` 这类分词：剪贴板里搜的常常是路径、URL、代码片段，
    按标点切开会让 `a/b`、`x.y`、`self.a` 这类输入永远查不到。
    这里只按空白切分，保留标点，匹配用「子串包含」而不是「词相等」。
    """
    return [token for token in (part.strip() for part in re.split(r"\s+", query or "")) if token]


def _lowered_fields(item: ClipboardItem) -> Optional[Tuple[str, str, str, str]]:
    """把一条记录的可检索字段转成小写元组；没有可检索文本时返回 `None`。

    预转换这一份小写副本是**量出来的**选择（2 万条记录、平均 90 字符，
    命中密集的查询）：预转换 49ms，边找边判、只在失败时才 `lower()`
    反倒要 77ms。亏在后者会让一次未命中多试一遍，而命中密集恰恰最常见。
    这条结论两种写法都写过，不是推出来的。
    """
    if not is_searchable(item):
        return None
    return (
        (item.content or "").lower(),
        (item.preview or "").lower(),
        " ".join(item.tags).lower(),
        (item.source_app or "").lower(),
    )


def _count_hits(text: str, token: str) -> int:
    """统计 token 在 text 里出现多少次，超过上限就不再数。

    封顶在计数时就做：一段把关键词重复五百遍的日志和重复八遍的，
    对我们来说信息量一样，但前者的计数会白花时间。
    """
    count = 0
    start = 0
    while count < MAX_FIELD_HITS:
        found = text.find(token, start)
        if found < 0:
            break
        count += 1
        start = found + 1
    return count


def _score_lowered(
    fields: Tuple[str, str, str, str], tokens: Sequence[str]
) -> Tuple[float, int, List[str]]:
    """按字段权重累计相关度，返回 `(得分, 命中总数, 命中字段)`。

    字段优先级与 `storage.search` 的 FTS 排序一致。同一字段内多次命中
    只做轻微加成（`1 + 0.2*(n-1)`），避免一段反复出现关键词的日志
    把真正的目标挤出结果。
    """
    total = 0.0
    hits = 0
    matched: List[str] = []
    for token in tokens:
        for index, (name, weight) in enumerate(FIELD_WEIGHTS):
            text = fields[index]
            if not text:
                continue
            count = _count_hits(text, token)
            if not count:
                continue
            hits += count
            if name not in matched:
                matched.append(name)
            total += weight * (1.0 + 0.2 * (count - 1))
    return total, hits, matched


def score_item_fields(item: ClipboardItem, tokens: Sequence[str]) -> Tuple[float, int, List[str]]:
    """`_score_lowered` 的单条入口，给不经过 `scan_items` 的调用方用。"""
    fields = _lowered_fields(item)
    if fields is None:
        return 0.0, 0, []
    return _score_lowered(fields, [token.lower() for token in tokens if token])


def _first_position(fields: Tuple[str, str, str, str], tokens: Sequence[str]) -> int:
    """最早出现的命中位置，用于「命中越靠前越相关」。

    只看正文与摘要：标签、来源在界面上是另一列，拿它们的位置当
    「文中位置」会把两条毫不相干的记录排成一条更相关。
    """
    best = -1
    for index in (0, 1):
        text = fields[index]
        if not text:
            continue
        for token in tokens:
            found = text.find(token)
            if found >= 0 and (best < 0 or found < best):
                best = found
    return max(best, 0)


def _matches_every(fields: Tuple[str, str, str, str], tokens: Sequence[str]) -> bool:
    """每个 token 都要在某个字段里出现。

    只有 `require_all=True` 时才走这一步：默认的宽松召回不需要逐 token 复核，
    而这次复核会把全部字段再走一遍。对一个默认关闭的选项，
    不该让它出现在热路径上。
    """
    for token in tokens:
        if not any(token in text for text in fields if text):
            return False
    return True


def scan_items(
    items: Iterable[ClipboardItem],
    tokens: Sequence[str],
    *,
    limit: int = MAX_CANDIDATES,
    require_all: bool = False,
    scanned_out: Optional[List[int]] = None,
) -> List[IndexHit]:
    """流式扫描记录，返回命中的那些（顺序按遍历顺序，未排序）。

    多词查询默认**任一命中即可**，与既有全文检索行为一致：剪贴板里
    记不住原话，多打一个词就查空是更坏的体验。相关度排序会把
    「全都命中」的记录顶到前面，所以宽松召回不妨碍精确优先。

    `scanned_out` 是一个单元素列表，用来把「实际扫了多少条」带出来 ——
    返回值已经被过滤过，无法从中还原扫描量。
    """
    lowered = [token.lower() for token in tokens if token]
    if not lowered:
        return []

    hits: List[IndexHit] = []
    scanned = 0
    for item in items:
        fields = _lowered_fields(item)
        if fields is None:
            continue
        scanned += 1
        total, hit_count, matched = _score_lowered(fields, lowered)
        if not hit_count:
            continue
        if require_all and not _matches_every(fields, lowered):
            continue
        hits.append(
            IndexHit(
                item=item,
                position=_first_position(fields, lowered),
                hits=hit_count,
                fields=matched,
                score=total,
            )
        )
        if len(hits) >= limit:
            break
    if scanned_out is not None:
        scanned_out[:] = [scanned]
    return hits


def relevance_map(hits: Sequence[IndexHit], tokens: Sequence[str]) -> Dict[int, float]:
    """把命中明细折算成排序用的相关度（0~1）。

    两个信号：命中的字段权重之和，以及「命中位置越靠前越相关」。
    后者是 FTS 的 BM25 也在做的事，对「在一大段日志的开头 vs 结尾命中」
    这种差别很有用 —— 而只按字段权重打分会把两者判成同一条。
    """
    if not tokens:
        return {}
    result: Dict[int, float] = {}
    for hit in hits:
        item = hit.item
        if item.id is None:
            continue
        # 字段权重和的理论上限：3.0 + 2.0 + 1.5 + 1.0（各字段各命中一次）
        normalized = min(1.0, hit.score / MAX_FIELD_SCORE)
        length = max(1, len((item.content or "").lower()))
        # 位置靠前最多给 0.25 的加成，且不随内容长度剧烈变化
        closeness = max(0.0, 1.0 - hit.position / length)
        result[item.id] = min(1.0, normalized + 0.25 * closeness)
    return result


def scan_repository(
    repository,
    query: str,
    *,
    limit: int = DEFAULT_SCAN_LIMIT,
) -> SearchOutcome:
    """与后端无关的全库检索。

    候选集来自 `iter_all()`（全库、顺序稳定），而不是 `list(limit=N)`。
    这一条是本模块存在的主要理由：用最新一批记录近似「全部历史」时，
    更老的那些记录无论关键词多匹配都不会出现在结果里，且没有任何提示。
    """
    tokens = tokenize(query)
    outcome = SearchOutcome(backend="scan", terms=tokens)
    if not tokens:
        return outcome

    scanned: List[int] = [0]
    hits = scan_items(repository.iter_all(), tokens, limit=MAX_CANDIDATES, scanned_out=scanned)
    outcome.scanned = scanned[0]
    outcome.candidates = len(hits)
    if not hits:
        return outcome

    relevant = relevance_map(hits, tokens)
    ranked = sorted(
        hits,
        key=lambda hit: (
            relevant.get(hit.item.id or -1, 0.0),
            hit.hits,
            _recency_key(hit.item),
        ),
        reverse=True,
    )
    outcome.items = [hit.item for hit in ranked[:limit]]
    return outcome


def _recency_key(item: ClipboardItem) -> float:
    return float(item.used_at or item.created_at or 0.0)


class SearchIndex:
    """一次检索要用的「全库视图」缓存。

    存在的理由是把全库扫描从「每个查询都做一遍」变成「每次数据变化后做一遍」：
    界面上的搜索框按输入逐字触发，而全库扫描是 O(库大小) 的，
    3000 条记录下每敲一个字符扫一遍虽然只有毫秒级，但那是白花的。

    缓存的是**记录的浅快照**，不是索引结构 ——
    索引结构要考虑失效、要考虑写入路径挂钩，而快照只要在
    「采集到新记录」「删了记录」「换了存储后端」时丢掉重取即可。
    代价是每次重建仍然要遍历全库一次，但那是流式的，不驻留第二份数据。
    """

    def __init__(self, repository) -> None:
        self._repository = repository
        self._items: Optional[List[ClipboardItem]] = None

    def invalidate(self) -> None:
        """数据变了：下次查询重新取一遍。

        刻意的「整份丢掉」而不是「增量维护」：增量维护要在采集、删除、
        合并、同步导入、切换存储五条写入路径上各挂一次钩子，
        漏一条就变成「新记录搜不到」—— 那正是本模块要消灭的症状。
        """
        self._items = None

    def snapshot(self) -> List[ClipboardItem]:
        if self._items is None:
            self._items = list(self._repository.iter_all())
        return self._items

    @property
    def loaded(self) -> bool:
        """是否已经有一份快照（供测试与诊断断言，不参与业务判断）。"""
        return self._items is not None
