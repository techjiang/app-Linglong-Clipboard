"""存储层：SQLite + FTS5 全文索引。

要点：
- 单连接 + `check_same_thread=False` + 可重入锁，够用且避免连接池复杂度；
- FTS5 不可用时自动退回 `LIKE` 查询，功能不降级；
- `trigram` 分词器对中文/代码片段友好，不可用时退回 `unicode61`。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from ..utils.logger import get_logger
from ..utils.paths import data_dir
from .models import ClipboardItem, content_hash
from .normalize import canonical_key

_log = get_logger("storage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT    NOT NULL DEFAULT 'text',
    content       TEXT    NOT NULL DEFAULT '',
    preview       TEXT    NOT NULL DEFAULT '',
    html          TEXT    NOT NULL DEFAULT '',
    rtf           TEXT    NOT NULL DEFAULT '',
    image_path    TEXT    NOT NULL DEFAULT '',
    image_width   INTEGER NOT NULL DEFAULT 0,
    image_height  INTEGER NOT NULL DEFAULT 0,
    files         TEXT    NOT NULL DEFAULT '[]',
    metadata      TEXT    NOT NULL DEFAULT '{}',
    source_app    TEXT    NOT NULL DEFAULT '',
    source_title  TEXT    NOT NULL DEFAULT '',
    hash          TEXT    NOT NULL DEFAULT '',
    size          INTEGER NOT NULL DEFAULT 0,
    pinned        INTEGER NOT NULL DEFAULT 0,
    favorite      INTEGER NOT NULL DEFAULT 0,
    sensitive     INTEGER NOT NULL DEFAULT 0,
    tags          TEXT    NOT NULL DEFAULT '[]',
    device        TEXT    NOT NULL DEFAULT '',
    created_at    REAL    NOT NULL DEFAULT 0,
    used_at       REAL    NOT NULL DEFAULT 0,
    use_count     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_items_created  ON items(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_hash     ON items(hash);
CREATE INDEX IF NOT EXISTS idx_items_kind     ON items(kind);
CREATE INDEX IF NOT EXISTS idx_items_pinned   ON items(pinned, created_at DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: `pinned` / `favorite` 排前，随后按最近使用与创建时间
_ORDER_COLUMNS = "pinned DESC, favorite DESC, COALESCE(NULLIF(used_at,0), created_at) DESC"


def _order_sql(alias: str = "") -> str:
    """生成排序子句；带别名时自动加上前缀，避免手写字符串替换出错。"""
    if not alias:
        return f"ORDER BY {_ORDER_COLUMNS}"
    prefix = f"{alias}."
    columns = _ORDER_COLUMNS.replace("pinned", f"{prefix}pinned")
    columns = columns.replace("favorite", f"{prefix}favorite")
    columns = columns.replace("used_at", f"{prefix}used_at")
    columns = columns.replace("created_at", f"{prefix}created_at")
    return f"ORDER BY {columns}"


_ORDER_SQL = _order_sql()


class Storage:
    """剪贴板历史的持久化与检索。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else data_dir() / "history.db"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        #: 是否已 `close()`。关闭之后再来的调用一律当「空历史」处理，见 `_usable`。
        self._closed = False
        self.fts_enabled = False
        self.fts_tokenizer = ""
        self._images_dir = self.path.parent / "images"
        self._images_dir.mkdir(parents=True, exist_ok=True)

        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._setup_fts()
            self._conn.commit()

    # ---- 初始化 ----

    def _setup_fts(self) -> None:
        for tokenizer in ("trigram", "unicode61"):
            try:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5("
                    "content, preview, source_app, tags, "
                    f"content='items', content_rowid='id', tokenize='{tokenizer}')"
                )
                self.fts_enabled = True
                self.fts_tokenizer = tokenizer
                break
            except sqlite3.OperationalError as exc:
                _log.debug("FTS5 分词器 %s 不可用: %s", tokenizer, exc)

        if not self.fts_enabled:
            _log.warning("FTS5 不可用，全文检索将退化为 LIKE 匹配")
            return

        self._conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
                INSERT INTO items_fts(rowid, content, preview, source_app, tags)
                VALUES (new.id, new.content, new.preview, new.source_app, new.tags);
            END;
            CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
                INSERT INTO items_fts(items_fts, rowid, content, preview, source_app, tags)
                VALUES ('delete', old.id, old.content, old.preview, old.source_app, old.tags);
            END;
            CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
                INSERT INTO items_fts(items_fts, rowid, content, preview, source_app, tags)
                VALUES ('delete', old.id, old.content, old.preview, old.source_app, old.tags);
                INSERT INTO items_fts(rowid, content, preview, source_app, tags)
                VALUES (new.id, new.content, new.preview, new.source_app, new.tags);
            END;
            """
        )
        # 已有数据补索引（首次创建 FTS 表时）
        try:
            count = self._conn.execute("SELECT count(*) FROM items_fts").fetchone()[0]
            total = self._conn.execute("SELECT count(*) FROM items").fetchone()[0]
            if total and not count:
                self._conn.execute("INSERT INTO items_fts(items_fts) VALUES ('rebuild')")
                _log.info("FTS 索引重建完成，共 %d 条", total)
        except sqlite3.OperationalError as exc:
            _log.debug("FTS 重建跳过: %s", exc)

    @property
    def images_dir(self) -> Path:
        return self._images_dir

    # ---- 写入 ----

    def add(self, item: ClipboardItem) -> Optional[ClipboardItem]:
        """插入一条记录。命中「保留固定项」策略时置顶已有项而不是新增。"""
        if not self._usable():
            return None
        row = item.to_row()
        columns = ", ".join(row.keys())
        placeholders = ", ".join(f":{key}" for key in row)
        with self._lock:
            cursor = self._conn.execute(
                f"INSERT INTO items ({columns}) VALUES ({placeholders})", row
            )
            self._conn.commit()
            item.id = int(cursor.lastrowid)
        return item

    def touch(self, item_id: int) -> None:
        """记录一次使用，用于排序权重。"""
        if not self._usable():
            return
        with self._lock:
            self._conn.execute(
                "UPDATE items SET used_at = ?, use_count = use_count + 1 WHERE id = ?",
                (time.time(), item_id),
            )
            self._conn.commit()

    def update_fields(
        self,
        item_id: int,
        *,
        merge_metadata: bool = True,
        **fields: Any,
    ) -> None:
        """更新指定字段。

        `metadata` 默认按**键合并**而不是整体替换。原因是它是个「共享的信封」：
        采集路径往里写 `last_source`、表单取回往里写 `formats` / `text_bytes`、
        同步往里写 `pinned_by`，各自只关心自己那一个键。整体替换意味着
        「每多一个写方，其它写方的键就开始轮流消失」—— 而这种丢失是静默的：
        没有报错、界面照常、只有等到某天发现某个字段莫名其妙没了。
        """
        allowed = {"pinned", "favorite", "sensitive", "tags", "metadata", "kind"}
        payload = {key: value for key, value in fields.items() if key in allowed}
        if not payload or not self._usable():
            return
        import json as _json

        if merge_metadata and isinstance(payload.get("metadata"), dict):
            current = self.get(item_id)
            merged = dict(current.metadata) if current is not None else {}
            for key, value in payload["metadata"].items():
                # 值为 None 表示「删掉这个键」。没有这条约定的话，
                # `pinned_by` 这类字段一旦写上就再也清不掉 —— 而合并语义
                # 又禁止调用方「传一份少了某个键的字典」来表达删除。
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            payload["metadata"] = merged

        normalized = {
            key: _json.dumps(value, ensure_ascii=False)
            if isinstance(value, (list, dict))
            else value
            for key, value in payload.items()
        }
        assignments = ", ".join(f"{key} = :{key}" for key in normalized)
        normalized["item_id"] = item_id
        with self._lock:
            self._conn.execute(
                f"UPDATE items SET {assignments} WHERE id = :item_id", normalized
            )
            self._conn.commit()

    def delete(self, item_id: int) -> None:
        if not self._usable():
            return
        with self._lock:
            row = self._conn.execute(
                "SELECT image_path FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            self._conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
            self._conn.commit()
        if row and row["image_path"]:
            self._remove_image(row["image_path"])

    def delete_many(self, ids: Iterable[int]) -> int:
        ids = list(ids)
        if not ids or not self._usable():
            return 0
        marks = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT image_path FROM items WHERE id IN ({marks})", ids
            ).fetchall()
            cursor = self._conn.execute(f"DELETE FROM items WHERE id IN ({marks})", ids)
            self._conn.commit()
        for row in rows:
            if row["image_path"]:
                self._remove_image(row["image_path"])
        return cursor.rowcount

    def clear(self, keep_pinned: bool = True) -> int:
        if not self._usable():
            return 0
        with self._lock:
            if keep_pinned:
                rows = self._conn.execute(
                    "SELECT image_path FROM items WHERE pinned = 0"
                ).fetchall()
                cursor = self._conn.execute("DELETE FROM items WHERE pinned = 0")
            else:
                rows = self._conn.execute("SELECT image_path FROM items").fetchall()
                cursor = self._conn.execute("DELETE FROM items")
            self._conn.commit()
        for row in rows:
            if row["image_path"]:
                self._remove_image(row["image_path"])
        return cursor.rowcount

    def _remove_image(self, relative: str) -> None:
        try:
            (self.path.parent / relative).unlink(missing_ok=True)
        except OSError as exc:
            _log.debug("图片清理失败 %s: %s", relative, exc)

    # ---- 查询 ----

    def get(self, item_id: int) -> Optional[ClipboardItem]:
        if not self._usable():
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return ClipboardItem.from_row(row) if row else None

    def find_by_hash(self, digest: str) -> Optional[ClipboardItem]:
        if not self._usable():
            return None
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM items WHERE hash = ? {_ORDER_SQL} LIMIT 1", (digest,)
            ).fetchone()
        return ClipboardItem.from_row(row) if row else None

    def latest(self) -> Optional[ClipboardItem]:
        if not self._usable():
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM items ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return ClipboardItem.from_row(row) if row else None

    def list(
        self,
        *,
        kind: Optional[str] = None,
        pinned_only: bool = False,
        favorite_only: bool = False,
        source_app: Optional[str] = None,
        since: Optional[float] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> List[ClipboardItem]:
        clauses: List[str] = []
        params: List[Any] = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if pinned_only:
            clauses.append("pinned = 1")
        if favorite_only:
            clauses.append("favorite = 1")
        if source_app:
            clauses.append("source_app = ?")
            params.append(source_app)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)

        if not self._usable():
            return []
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        sql = f"SELECT * FROM items {where} {_ORDER_SQL} LIMIT ? OFFSET ?"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [ClipboardItem.from_row(row) for row in rows]

    def iter_all(self, *, batch: int = 500) -> Iterator[ClipboardItem]:
        """按 `id DESC` 遍历**全部**历史，供需要看完整库的调用方使用。

        为什么不能拿 `list(limit=N)` 代替：`list()` 走的是「按界面排序取前 N 条」，
        而界面排序含 `used_at`（每次复制都会变），既不稳定、又只看得到最新的 N 条。
        同步导出原先就是这么写的，于是记录超过 N 条后，更老的那些**永远拿不到**——
        没有报错、没有提示，只是那一段历史在别的设备上从不存在。

        这里改用 `id DESC` 做键集分页（keyset）：`id` 严格递增、不随用户操作变动，
        翻页期间记录被复制/固定也不会错位或漏读。游标条件用 `id < last_id`
        而不是 `OFFSET`，避免边翻边写时 `OFFSET` 跳页漏记录。
        """
        if not self._usable():
            return
        size = max(1, int(batch or 1))
        last_id: Optional[int] = None
        while True:
            where = "WHERE id < ?" if last_id is not None else ""
            params: List[Any] = [last_id] if last_id is not None else []
            params.append(size)
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT * FROM items {where} ORDER BY id DESC LIMIT ?", params
                ).fetchall()
            if not rows:
                return
            for row in rows:
                yield ClipboardItem.from_row(row)
            last_id = int(rows[-1]["id"])
            if len(rows) < size:
                return

    def iter_since(
        self,
        since: float = 0.0,
        *,
        since_id: int = 0,
        batch: int = 500,
    ) -> Iterator[ClipboardItem]:
        """按 `(created_at, id)` 升序遍历严格晚于 `(since, since_id)` 的记录。

        与 `iter_all` 的区别是**排序维度**：同步对端用时间戳做游标，
        所以这里必须按时间升序读，才能「读完一批就把游标推到这批的最大时间」。
        用 `iter_all` 的 `id DESC` 做不到这件事 —— 它天然是倒序，
        推到最大时间后剩余记录全被甩在游标之后。

        `id` 作为次级排序键保证**同一时刻的多条记录顺序稳定**：
        只按 `created_at` 排序时，同毫秒记录的相对次序由数据库决定，
        翻页可能重复或漏读。加上 `id` 就有了确定的全序。

        `since_id` 是**游标的一部分**，不是可选装饰。只按时间戳做游标时，
        「同一时刻的记录多到塞满一页」会让翻页彻底卡死：首页返回满页且
        时间都是 `T`，下一页请求 `created_at > T` 得到 0 条 —— 剩下的记录
        比这一页更旧吗？不是，是**一样新**。它们既不在这一页，也永远
        不在下一页，且没有任何报错。`since_id` 让「同一时刻内读到哪一条」
        也能表达，翻页于是总能前进。

        边界语义是 `(created_at, id)` **字典序严格大于** `(since, since_id)`：
        首页 `(since, since_id > 0)` 也不会重读 `since` 时刻那条 id 更小的记录。
        """
        if not self._usable():
            return
        size = max(1, int(batch or 1))
        first = float(since or 0.0)
        first_id = max(0, int(since_id or 0))
        # 键集续读：`(created_at, id)` 全序，条件从第一页起就是同一个，
        # 不必为「首页」单开一条不含 id 的语义（那样会漏掉同刻记录）。
        last_seen: Optional[float] = None if first_id == 0 else first
        last_id = first_id
        while True:
            if last_seen is None:
                where = "created_at > ?"
                params: List[Any] = [first]
            else:
                where = "created_at > ? OR (created_at = ? AND id > ?)"
                params = [last_seen, last_seen, last_id]
            params.append(size)
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT * FROM items WHERE {where} "
                    "ORDER BY created_at ASC, id ASC LIMIT ?",
                    params,
                ).fetchall()
            if not rows:
                return
            for row in rows:
                yield ClipboardItem.from_row(row)
            last_id = int(rows[-1]["id"])
            last_seen = float(rows[-1]["created_at"] or 0.0)
            if len(rows) < size:
                return

    def search(
        self,
        query: str,
        *,
        kind: Optional[str] = None,
        limit: int = 60,
        use_fts: bool = True,
    ) -> List[ClipboardItem]:
        """全文检索。查询串为空时退化为列表查询。

        FTS 查询串会做转义：把用户输入按 token 拆开并加引号，
        避免 `AND`、`*`、`:` 等 FTS 语法字符引发解析错误。
        """
        if not self._usable():
            return []
        query = (query or "").strip()
        if not query:
            return self.list(kind=kind, limit=limit)

        kind_clause = "AND i.kind = ?" if kind else ""
        kind_params: List[Any] = [kind] if kind else []

        if use_fts and self.fts_enabled:
            match = self._build_match_query(query)
            if match:
                sql = (
                    "SELECT i.* FROM items_fts f JOIN items i ON i.id = f.rowid "
                    f"WHERE items_fts MATCH ? {kind_clause} {_order_sql('i')} LIMIT ?"
                )
                try:
                    with self._lock:
                        rows = self._conn.execute(
                            sql, [match] + kind_params + [limit]
                        ).fetchall()
                    if rows:
                        return [ClipboardItem.from_row(row) for row in rows]
                except sqlite3.OperationalError as exc:
                    # 语法错误或索引损坏都走 LIKE，保证检索不中断。
                    _log.debug("FTS 查询失败，回退 LIKE: %s", exc)

        # FTS 未命中或不可用：短 token 在 trigram 下本就查不到，靠 LIKE 兜底。
        return self._search_like(query, kind=kind, limit=limit)

    def _search_like(
        self, query: str, *, kind: Optional[str], limit: int
    ) -> List[ClipboardItem]:
        if not self._usable():
            return []
        like = f"%{query}%"
        clauses = ["(content LIKE ? OR preview LIKE ? OR source_app LIKE ? OR tags LIKE ?)"]
        params: List[Any] = [like, like, like, like]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        sql = (
            f"SELECT * FROM items WHERE {' AND '.join(clauses)} {_ORDER_SQL} LIMIT ?"
        )
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [ClipboardItem.from_row(row) for row in rows]

    def _build_match_query(self, query: str) -> str:
        """把自由文本转成安全的 FTS5 MATCH 表达式。

        `trigram` 分词器按 3 字符切分，短于 3 字符的 token 匹配不到任何行，
        这类查询返回空串让调用方回退 LIKE；查询里只要有一个 token 命中即算命中。
        """
        tokens = [token for token in query.replace('"', " ").split() if token]
        if not tokens:
            return ""
        escaped: List[str] = []
        for token in tokens:
            cleaned = "".join(
                ch for ch in token if ch.isalnum() or ch in "_-./@:+@!"
            )
            if len(cleaned) >= 3:
                escaped.append(f'"{cleaned}"')
        if not escaped:
            return ""
        return " OR ".join(escaped)

    # ---- 统计与维护 ----

    def count(self) -> int:
        if not self._usable():
            return 0
        with self._lock:
            return int(self._conn.execute("SELECT count(*) FROM items").fetchone()[0])

    def count_by_kind(self) -> Dict[str, int]:
        if not self._usable():
            return {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, count(*) AS n FROM items GROUP BY kind"
            ).fetchall()
        return {row["kind"]: int(row["n"]) for row in rows}

    def top_sources(self, limit: int = 10) -> List[Tuple[str, int]]:
        if not self._usable():
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_app, count(*) AS n FROM items "
                "WHERE source_app <> '' GROUP BY source_app ORDER BY n DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(row["source_app"], int(row["n"])) for row in rows]

    def recanonicalize(self, options: Optional[Dict[str, Any]] = None) -> int:
        """按当前归一规则重算历史记录的指纹，返回被改写的条数。

        归一规则（链接去跟踪参数、路径收敛）是在某次升级里加进来的，加之前
        入库的记录指纹仍是原始内容算的。若不回头重算，「微博链接」与
        「地址栏链接」在库里就是两串不同的 hash，`dedupe()` 按 hash 分组
        自然看不出它们重复 —— 新规则只对新数据生效，老数据永远合不了。

        只改 `hash` 一个字段，`content` 不动：用户看到的仍是原文。
        改完由调用方接着跑 `dedupe()` 把同组收敛成一条。
        """
        options = options or {}
        changed = 0
        if not self._usable():
            return 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, content, files, hash FROM items"
            ).fetchall()
            for row in rows:
                try:
                    files = json.loads(row["files"]) if row["files"] else []
                except (ValueError, TypeError):
                    files = []
                payload = canonical_key(
                    str(row["kind"] or ""),
                    str(row["content"] or ""),
                    files=files if isinstance(files, list) else None,
                    options=options,
                )
                digest = content_hash(str(row["kind"] or ""), payload)
                if not digest or row["hash"] == digest:
                    continue
                self._conn.execute(
                    "UPDATE items SET hash = ? WHERE id = ?", (digest, row["id"])
                )
                changed += 1
            if changed:
                self._conn.commit()
        return changed

    def dedupe(self) -> int:
        """合并重复内容（保留首条），用于「合并重复项」策略的收尾清理。

        合并是**非破坏性**的：直接把重复行删掉会把「这段内容被复制了 3 次」
        变成「被复制了 1 次」——一个高频片段反而排在低频片段后面。
        所以删除前先把重复行的使用次数与最近使用时间折算到保留项上。
        """
        if not self._usable():
            return 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT hash, MIN(id) AS keep_id, COUNT(*) AS total_rows, "
                "       SUM(use_count) AS total_use, "
                "       MAX(MAX(COALESCE(used_at, 0), COALESCE(created_at, 0))) "
                "           AS last_seen "
                "FROM items WHERE hash <> '' GROUP BY hash HAVING COUNT(*) > 1"
            ).fetchall()
            if not rows:
                return 0
            removed = 0
            for row in rows:
                keep_id = int(row["keep_id"])
                # 频次 = 各行显式记录的 use_count 之和 + 「多出来的行数」。
                # 后者是关键：`merge_duplicates` 关闭期间（或历史遗留）产生的重复
                # 是**以行数**承载「复制了几次」这一信息的，行本身不记 use_count。
                # 只累加 use_count 会得到 0，等于把「复制过 3 次」合并成「从没复制」。
                total = int(row["total_use"] or 0) + max(0, int(row["total_rows"]) - 1)
                # `used_at` 提到整个重复组里最新的时间戳：合并后这一条代表的是
                # 「最近一次见到这段内容」，否则保留的可能是组里最旧的那个时刻，
                # 在按最近使用排序的列表里直接沉底。
                self._conn.execute(
                    "UPDATE items SET use_count = ?, "
                    "used_at = MAX(COALESCE(used_at, 0), ?) WHERE id = ?",
                    (total, float(row["last_seen"] or 0), keep_id),
                )
                cursor = self._conn.execute(
                    "DELETE FROM items WHERE hash = ? AND id <> ?",
                    (row["hash"], keep_id),
                )
                removed += cursor.rowcount
            self._conn.commit()
            return removed

    def enforce_limits(
        self, max_items: int, max_days: int = 0, pin_immune: bool = True
    ) -> int:
        """按条数与天数淘汰历史。

        `pin_immune` 控制固定项是否豁免：为真时固定项完全不占限额
        （淘汰 SQL 一律带 `pinned = 0`），这是老行为。关闭后固定项与普通
        记录一视同仁，但**排序仍把固定项排前**，所以先被淘汰的仍是久未使用的
        普通记录 —— 只有历史远超上限时才会轮到固定项。

        为什么保这个开关：上限是「历史别无限涨」的硬约束。若固定项永久豁免，
        把上万条都标为固定就等于上限失效，而用户未必意识到自己绕过了它。
        """
        removed = 0
        if not self._usable():
            return 0
        with self._lock:
            if max_days > 0:
                cutoff = time.time() - max_days * 86400
                if pin_immune:
                    sql = "DELETE FROM items WHERE pinned = 0 AND created_at < ?"
                else:
                    sql = "DELETE FROM items WHERE created_at < ?"
                cursor = self._conn.execute(sql, (cutoff,))
                removed += cursor.rowcount
            if max_items > 0:
                # 排序统一把固定项排前，所以关闭豁免时也是普通记录先被淘汰
                if pin_immune:
                    sql = (
                        "DELETE FROM items WHERE pinned = 0 AND id NOT IN ("
                        "  SELECT id FROM items WHERE pinned = 0 "
                        "  ORDER BY pinned DESC, "
                        "    COALESCE(NULLIF(used_at,0), created_at) DESC LIMIT ?"
                        ")"
                    )
                else:
                    sql = (
                        "DELETE FROM items WHERE id NOT IN ("
                        "  SELECT id FROM items "
                        "  ORDER BY pinned DESC, "
                        "    COALESCE(NULLIF(used_at,0), created_at) DESC LIMIT ?"
                        ")"
                    )
                cursor = self._conn.execute(sql, (max_items,))
                removed += cursor.rowcount
            if removed:
                self._conn.commit()
        return removed

    def purge_expired_sensitive(self, ttl_seconds: int) -> int:
        """清除超期的敏感记录（不落库内容，直接从历史移除）。"""
        if ttl_seconds <= 0 or not self._usable():
            return 0
        cutoff = time.time() - ttl_seconds
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM items WHERE sensitive = 1 AND created_at < ?", (cutoff,)
            )
            self._conn.commit()
            return cursor.rowcount

    def set_meta(self, key: str, value: str) -> None:
        if not self._usable():
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        if not self._usable():
            return default
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def optimize(self) -> None:
        if not self._usable():
            return
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.execute("VACUUM")
            self._conn.commit()

    def stats(self) -> Dict[str, Any]:
        size = self.path.stat().st_size if self.path.exists() else 0
        if not self._usable():
            return {"items": 0, "kinds": {}, "sources": [], "db_size": size,
                    "fts": False, "tokenizer": "", "closed": True}
        return {
            "items": self.count(),
            "kinds": self.count_by_kind(),
            "sources": self.top_sources(5),
            "db_size": size,
            "fts": self.fts_enabled,
            "tokenizer": self.fts_tokenizer,
        }

    def close(self) -> None:
        """关闭连接。可重复调用。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.commit()
                self._conn.close()
            except sqlite3.Error:
                pass

    @property
    def closed(self) -> bool:
        return self._closed

    def _usable(self) -> bool:
        """连接是否还能用。

        关闭之后仍有调用方找上来是常态，而不是异常：界面上的防抖定时器、
        采集线程、限时清空任务都可能晚一步。这些调用**不该抛异常** ——
        它们跑在 Qt 事件循环里，抛出的异常会被 Qt 吞掉，表现是
        「关窗时终端刷一屏 traceback」或「设置页莫名不刷新」，很难查回这里。
        所以关闭之后一律按「空历史」回话，与刚建好的空库行为一致。
        """
        return not self._closed
