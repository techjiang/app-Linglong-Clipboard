"""存储层：SQLite + FTS5 全文索引。

要点：
- 单连接 + `check_same_thread=False` + 可重入锁，够用且避免连接池复杂度；
- FTS5 不可用时自动退回 `LIKE` 查询，功能不降级；
- `trigram` 分词器对中文/代码片段友好，不可用时退回 `unicode61`。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..utils.logger import get_logger
from ..utils.paths import data_dir
from .models import ClipboardItem

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
        with self._lock:
            self._conn.execute(
                "UPDATE items SET used_at = ?, use_count = use_count + 1 WHERE id = ?",
                (time.time(), item_id),
            )
            self._conn.commit()

    def update_fields(self, item_id: int, **fields: Any) -> None:
        allowed = {"pinned", "favorite", "sensitive", "tags", "metadata", "kind"}
        payload = {key: value for key, value in fields.items() if key in allowed}
        if not payload:
            return
        import json as _json

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
        if not ids:
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
        with self._lock:
            row = self._conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return ClipboardItem.from_row(row) if row else None

    def find_by_hash(self, digest: str) -> Optional[ClipboardItem]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM items WHERE hash = ? {_ORDER_SQL} LIMIT 1", (digest,)
            ).fetchone()
        return ClipboardItem.from_row(row) if row else None

    def latest(self) -> Optional[ClipboardItem]:
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

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        sql = f"SELECT * FROM items {where} {_ORDER_SQL} LIMIT ? OFFSET ?"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [ClipboardItem.from_row(row) for row in rows]

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
        with self._lock:
            return int(self._conn.execute("SELECT count(*) FROM items").fetchone()[0])

    def count_by_kind(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, count(*) AS n FROM items GROUP BY kind"
            ).fetchall()
        return {row["kind"]: int(row["n"]) for row in rows}

    def top_sources(self, limit: int = 10) -> List[Tuple[str, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_app, count(*) AS n FROM items "
                "WHERE source_app <> '' GROUP BY source_app ORDER BY n DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(row["source_app"], int(row["n"])) for row in rows]

    def dedupe(self) -> int:
        """删除重复内容（保留首条），用于「合并重复项」策略的收尾清理。"""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM items WHERE id NOT IN ("
                "  SELECT MIN(id) FROM items GROUP BY hash"
                ") AND hash <> ''"
            )
            self._conn.commit()
            return cursor.rowcount

    def enforce_limits(self, max_items: int, max_days: int = 0) -> int:
        """按条数与天数淘汰历史，固定项不受影响。"""
        removed = 0
        with self._lock:
            if max_days > 0:
                cutoff = time.time() - max_days * 86400
                cursor = self._conn.execute(
                    "DELETE FROM items WHERE pinned = 0 AND created_at < ?", (cutoff,)
                )
                removed += cursor.rowcount
            if max_items > 0:
                cursor = self._conn.execute(
                    "DELETE FROM items WHERE pinned = 0 AND id NOT IN ("
                    "  SELECT id FROM items WHERE pinned = 0 "
                    "  ORDER BY COALESCE(NULLIF(used_at,0), created_at) DESC LIMIT ?"
                    ")",
                    (max_items,),
                )
                removed += cursor.rowcount
            if removed:
                self._conn.commit()
        return removed

    def purge_expired_sensitive(self, ttl_seconds: int) -> int:
        """清除超期的敏感记录（不落库内容，直接从历史移除）。"""
        if ttl_seconds <= 0:
            return 0
        cutoff = time.time() - ttl_seconds
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM items WHERE sensitive = 1 AND created_at < ?", (cutoff,)
            )
            self._conn.commit()
            return cursor.rowcount

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def optimize(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.execute("VACUUM")
            self._conn.commit()

    def stats(self) -> Dict[str, Any]:
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "items": self.count(),
            "kinds": self.count_by_kind(),
            "sources": self.top_sources(5),
            "db_size": size,
            "fts": self.fts_enabled,
            "tokenizer": self.fts_tokenizer,
        }

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except sqlite3.Error:
                pass
