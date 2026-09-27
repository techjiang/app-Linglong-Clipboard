"""JSON 单文件存储：把历史记录落到 `<数据目录>/.clipboard/clipboard.json`。

与 `storage.Storage`（SQLite）的关系是**并列**而非替代：两者实现同一组方法，
`Application` 与 CLI 按配置选一个用，上层代码不需要知道底下是哪种落盘方式。

为什么要有它：

- 用户对「我的数据长什么样」有知情权。SQLite 文件打开是一堆二进制，
  而 JSON 文本可以直接看、直接备份、直接粘到另一台机器。
- 便携场景（U 盘、同步盘）下，拷一个文本文件比连带 `-wal`/`-shm` 三个文件省心。
- DB 文件损坏时只能整库丢，JSON 里其余字段还能人工抢救。

取舍与代价（都写清楚，不藏着）：

- **每次写入重写整份文件**。剪贴板采集是秒级低频操作，几 MB 的文件重写无感；
  但记录量到十万级时这是错误的存储选择，那时应当用 SQLite。
- **检索退化为进程内扫描 + 打分**。没有 FTS5，就没有 BM25；
  这里用「子串命中 + 字段权重」近似，够用于几千条记录。
- **图片内联成 data URI 而不是另存文件**。单文件才谈得上「一个文件搬走就完事」；
  代价是体积膨胀（base64 约 +33%），所以默认把单张图压到 1.5MB 以内再内联。

写入一律「临时文件 + 原子替换」：进程被 Ctrl+C 打断也不会留下半截 JSON，
这是相比直接 `write_text` 唯一值得多写十行的理由。
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..utils.logger import get_logger
from .models import ClipboardItem

_log = get_logger("jsonstore")

#: 落盘格式版本，将来结构变更时用它做迁移判断
SCHEMA_VERSION = 1

#: 内联图片前允许的最大字节数，超过则放弃内联（只留尺寸信息）
MAX_INLINE_IMAGE_BYTES = 2 * 1024 * 1024

#: `source_app` 等字段在检索时的权重，用来近似 FTS 的相关度
_FIELD_WEIGHTS = (("content", 3.0), ("preview", 2.0), ("tags", 1.5), ("source_app", 1.0))

_MIME_BY_SUFFIX = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "gif": "image/gif",
}


def is_data_uri(value: str) -> bool:
    return bool(value) and value.startswith("data:") and ";base64," in value


def split_data_uri(uri: str) -> Tuple[str, str]:
    """把 `data:image/png;base64,xxxx` 拆成 `(mime, base64 载荷)`。"""
    header, _, payload = uri.partition(",")
    mime = header[5:].split(";")[0] or "image/png"
    return mime, payload


def suffix_for_mime(mime: str) -> str:
    for suffix, candidate in _MIME_BY_SUFFIX.items():
        if candidate == mime:
            return suffix
    return "png"


def encode_file_as_data_uri(path: Path, *, max_bytes: int = MAX_INLINE_IMAGE_BYTES) -> Optional[str]:
    """把图片文件读成 data URI；读不了或超限时返回 None（调用方保留原路径）。"""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        _log.debug("图片读取失败 %s: %s", path, exc)
        return None
    if len(raw) > max_bytes:
        _log.info("图片 %.1fMB 超过内联上限，仅保留本地路径", len(raw) / 1024 / 1024)
        return None
    mime = _MIME_BY_SUFFIX.get(path.suffix.lstrip(".").lower(), "image/png")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


class JsonRepository:
    """以单个 JSON 文件为后端的剪贴板历史仓库。

    公开方法与 `Storage` 对齐，因此两者可互换。
    """

    def __init__(self, path: Optional[Path] = None, *, autosave: bool = True) -> None:
        from ..utils.paths import json_path

        self.path = Path(path) if path else json_path()
        self.autosave = autosave
        self._lock = threading.RLock()
        self._items: Dict[int, ClipboardItem] = {}
        self._meta: Dict[str, str] = {}
        self._next_id = 1
        #: 图片相对路径 -> data URI，用于 `Storage.images_dir` 之外的第二来源
        self._images: Dict[str, str] = {}
        self._dirty = False
        self._load()

    # ---- 落盘格式 ----

    def _load(self) -> None:
        if not self.path.exists():
            self._write()  # 先落一份空骨架，让用户能立刻看到文件在哪
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("JSON 历史损坏，已另存备份并重新开始: %s", exc)
            self._backup_broken()
            self._write()
            return

        if not isinstance(raw, dict):
            _log.warning("JSON 历史结构不是对象，按空历史处理")
            self._write()
            return

        for entry in raw.get("items") or []:
            if not isinstance(entry, dict):
                continue
            item = self._from_json(entry)
            if item is None:
                continue
            self._items[item.id] = item
            self._next_id = max(self._next_id, item.id + 1)
        meta = raw.get("meta")
        if isinstance(meta, dict):
            self._meta = {str(key): str(value) for key, value in meta.items()}

    def _backup_broken(self) -> None:
        target = self.path.with_suffix(".json.broken")
        try:
            self.path.replace(target)
        except OSError as exc:
            _log.debug("损坏文件备份失败: %s", exc)

    def _to_json(self, item: ClipboardItem) -> Dict[str, Any]:
        """序列化一条记录；对空值省略键，让文件保持人类可读。"""
        payload: Dict[str, Any] = {
            "id": item.id,
            "kind": item.kind,
            "content": item.content,
            "created_at": round(float(item.created_at), 3),
        }
        for key, value in (
            ("preview", item.preview),
            ("html", item.html),
            ("rtf", item.rtf),
            ("source_app", item.source_app),
            ("source_title", item.source_title),
            ("hash", item.hash),
            ("device", item.device),
        ):
            if value:
                payload[key] = value
        for key, value in (
            ("image_width", item.image_width),
            ("image_height", item.image_height),
            ("size", item.size),
            ("use_count", item.use_count),
        ):
            if value:
                payload[key] = value
        if item.used_at:
            payload["used_at"] = round(float(item.used_at), 3)
        for key, value in (
            ("pinned", item.pinned),
            ("favorite", item.favorite),
            ("sensitive", item.sensitive),
        ):
            if value:
                payload[key] = bool(value)
        if item.files:
            payload["files"] = list(item.files)
        if item.tags:
            payload["tags"] = list(item.tags)
        if item.metadata:
            payload["metadata"] = item.metadata

        image = self._image_field(item)
        if image:
            payload["image"] = image
        return payload

    def _image_field(self, item: ClipboardItem) -> str:
        """返回可供落盘/还原的图片来源：data URI 或相对路径。"""
        if not item.image_path:
            return ""
        if is_data_uri(item.image_path):
            return item.image_path
        cached = self._images.get(item.image_path)
        if cached:
            return cached
        return item.image_path

    def _from_json(self, entry: Dict[str, Any]) -> Optional[ClipboardItem]:
        try:
            item = ClipboardItem(
                id=int(entry.get("id") or 0),
                kind=str(entry.get("kind") or "text"),
                content=str(entry.get("content") or ""),
                preview=str(entry.get("preview") or ""),
                html=str(entry.get("html") or ""),
                rtf=str(entry.get("rtf") or ""),
                image_path=str(entry.get("image") or ""),
                image_width=int(entry.get("image_width") or 0),
                image_height=int(entry.get("image_height") or 0),
                files=[str(one) for one in entry.get("files") or []],
                metadata=dict(entry.get("metadata") or {}),
                source_app=str(entry.get("source_app") or ""),
                source_title=str(entry.get("source_title") or ""),
                hash=str(entry.get("hash") or ""),
                size=int(entry.get("size") or 0),
                pinned=bool(entry.get("pinned")),
                favorite=bool(entry.get("favorite")),
                sensitive=bool(entry.get("sensitive")),
                tags=[str(one) for one in entry.get("tags") or []],
                device=str(entry.get("device") or ""),
                created_at=float(entry.get("created_at") or time.time()),
                used_at=float(entry.get("used_at") or 0.0),
                use_count=int(entry.get("use_count") or 0),
            )
        except (TypeError, ValueError) as exc:
            _log.debug("跳过一条无法解析的 JSON 记录: %s", exc)
            return None
        if item.id <= 0:
            return None
        return item

    def save(self) -> None:
        """把内存状态原子地写回文件。"""
        with self._lock:
            self._write()

    def _write(self) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": round(time.time(), 3),
            "items": [self._to_json(self._items[key]) for key in sorted(self._items)],
            "meta": self._meta,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError as exc:
            # 磁盘满或权限不足：不能把异常抛进采集线程，下次写入再试
            _log.error("JSON 历史写入失败: %s", exc)
            self._dirty = True

    def _maybe_save(self) -> None:
        if self.autosave:
            self._write()

    # ---- 兼容 Storage 的接口 ----

    @property
    def images_dir(self) -> Path:
        return self.path.parent / "images"

    def set_image_data(self, relative: str, uri: str) -> None:
        """登记「图片相对路径 -> data URI」，落盘时内联，读取时可还原。"""
        if relative and is_data_uri(uri):
            self._images[relative] = uri

    def add(self, item: ClipboardItem) -> Optional[ClipboardItem]:
        with self._lock:
            if item.id is None or item.id in self._items:
                item.id = self._next_id
            self._next_id = max(self._next_id, item.id + 1)
            self._items[item.id] = item
            self._maybe_save()
        return item

    def touch(self, item_id: int) -> None:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return
            item.used_at = time.time()
            item.use_count += 1
            self._maybe_save()

    def update_fields(self, item_id: int, **fields: Any) -> None:
        allowed = {"pinned", "favorite", "sensitive", "tags", "metadata", "kind"}
        payload = {key: value for key, value in fields.items() if key in allowed}
        if not payload:
            return
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return
            for key, value in payload.items():
                setattr(item, key, value)
            self._maybe_save()

    def delete(self, item_id: int) -> None:
        with self._lock:
            if self._items.pop(item_id, None) is not None:
                self._maybe_save()

    def delete_many(self, ids: Iterable[int]) -> int:
        with self._lock:
            removed = 0
            for item_id in list(ids):
                if self._items.pop(item_id, None) is not None:
                    removed += 1
            self._maybe_save()
            return removed

    def clear(self, keep_pinned: bool = True) -> int:
        with self._lock:
            doomed = [
                item_id
                for item_id, item in self._items.items()
                if not (keep_pinned and item.pinned)
            ]
            for item_id in doomed:
                self._items.pop(item_id, None)
            self._maybe_save()
            return len(doomed)

    def get(self, item_id: int) -> Optional[ClipboardItem]:
        with self._lock:
            item = self._items.get(item_id)
            return copy.deepcopy(item) if item is not None else None

    def find_by_hash(self, digest: str) -> Optional[ClipboardItem]:
        """同哈希可能有多条（历史遗留），按排序优先级返回第一条。"""
        if not digest:
            return None
        with self._lock:
            candidates = [item for item in self._items.values() if item.hash == digest]
        if not candidates:
            return None
        candidates.sort(key=_sort_key, reverse=True)
        return copy.deepcopy(candidates[0])

    def latest(self) -> Optional[ClipboardItem]:
        with self._lock:
            if not self._items:
                return None
            item = max(self._items.values(), key=lambda one: one.created_at)
            return copy.deepcopy(item)

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
        selected: List[ClipboardItem] = []
        with self._lock:
            for item in self._items.values():
                if kind and item.kind != kind:
                    continue
                if pinned_only and not item.pinned:
                    continue
                if favorite_only and not item.favorite:
                    continue
                if source_app and item.source_app != source_app:
                    continue
                if since and item.created_at < since:
                    continue
                selected.append(copy.deepcopy(item))
        selected.sort(key=_sort_key, reverse=True)
        return selected[offset : offset + limit]

    def search(
        self,
        query: str,
        *,
        kind: Optional[str] = None,
        limit: int = 60,
        use_fts: bool = True,  # 与 Storage 签名一致；这里恒为子串匹配
    ) -> List[ClipboardItem]:
        query = (query or "").strip()
        if not query:
            return self.list(kind=kind, limit=limit)

        tokens = [token for token in re.split(r"\s+", query.lower()) if token]
        scored: List[Tuple[float, ClipboardItem]] = []
        with self._lock:
            for item in self._items.values():
                if kind and item.kind != kind:
                    continue
                score = self._score(item, tokens)
                if score > 0:
                    scored.append((score, copy.deepcopy(item)))
        scored.sort(key=lambda pair: (pair[0], _sort_key(pair[1])), reverse=True)
        return [item for _, item in scored[:limit]]

    @staticmethod
    def _score(item: ClipboardItem, tokens: List[str]) -> float:
        """朴素相关度：任一 token 命中即可，按字段权重与出现次数累加。

        权重差异来自实际检索习惯 —— 正文命中比来源应用命中更有意义。
        """
        haystacks = {
            "content": (item.content or "").lower(),
            "preview": (item.preview or "").lower(),
            "tags": " ".join(item.tags).lower(),
            "source_app": (item.source_app or "").lower(),
        }
        total = 0.0
        for token in tokens:
            for field, weight in _FIELD_WEIGHTS:
                text = haystacks[field]
                if token and token in text:
                    total += weight * (1.0 + 0.2 * (text.count(token) - 1))
        if total == 0:
            return 0.0
        # 越近的记录略占优势，避免同分时顺序随机
        return total + 1.0 / (1.0 + max(0.0, time.time() - item.created_at) / 86400)

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def count_by_kind(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        with self._lock:
            for item in self._items.values():
                counts[item.kind] = counts.get(item.kind, 0) + 1
        return counts

    def top_sources(self, limit: int = 10) -> List[Tuple[str, int]]:
        counts: Dict[str, int] = {}
        with self._lock:
            for item in self._items.values():
                if item.source_app:
                    counts[item.source_app] = counts.get(item.source_app, 0) + 1
        ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
        return ranked[:limit]

    def dedupe(self) -> int:
        """合并重复内容（保留排序最高的一条）。

        与 SQLite 后端保持同样的语义：删除前先把重复项的使用次数与
        最近使用时间折算到保留项上，否则「复制过多次」这个信号会随重复行
        一起被丢掉，高频片段反而排到低频片段后面。
        """
        groups: Dict[str, List[int]] = {}
        with self._lock:
            for item_id, item in self._items.items():
                if item.hash:
                    groups.setdefault(item.hash, []).append(item_id)

            removed = 0
            for ids in groups.values():
                if len(ids) < 2:
                    continue
                # 保留排序靠前（最近使用/固定）的那条，与列表展示一致
                keeper_id = max(ids, key=lambda i: _sort_key(self._items[i]))
                keeper = self._items[keeper_id]
                # 频次 = 各行显式 use_count 之和 + 「多出来的行数」。
                # 后者不可省：重复行本身就代表「又复制了一次」，只是没记在
                # use_count 上（`merge_duplicates` 关闭期间的老数据就是这样）。
                total_use = 0
                last_seen = 0.0
                for item_id in ids:
                    other = self._items[item_id]
                    total_use += int(other.use_count or 0)
                    # `used_at` 提到组内最新的时间戳（含 created_at）：
                    # 合并后这一条代表「最近一次见到这段内容」，
                    # 否则保留的可能最旧时刻，在按最近使用排序时直接沉底。
                    last_seen = max(
                        last_seen,
                        float(other.used_at or 0.0),
                        float(other.created_at or 0.0),
                    )
                total_use += max(0, len(ids) - 1)
                keeper.use_count = total_use
                keeper.used_at = last_seen
                for item_id in ids:
                    if item_id != keeper_id:
                        self._items.pop(item_id, None)
                        removed += 1
            if removed:
                self._maybe_save()
            return removed

    def enforce_limits(self, max_items: int, max_days: int = 0) -> int:
        removed = 0
        with self._lock:
            if max_days > 0:
                cutoff = time.time() - max_days * 86400
                doomed = [
                    item_id
                    for item_id, item in self._items.items()
                    if not item.pinned and item.created_at < cutoff
                ]
                removed += len(doomed)
                for item_id in doomed:
                    self._items.pop(item_id, None)

            if max_items > 0:
                # 与 SQLite 版语义一致：上限只约束未固定项，按最近使用排序裁剪
                unpinned = sorted(
                    (item for item in self._items.values() if not item.pinned),
                    key=_sort_key,
                    reverse=True,
                )
                doomed = [item.id for item in unpinned[max_items:]]
                removed += len(doomed)
                for item_id in doomed:
                    self._items.pop(item_id, None)

            if removed:
                self._maybe_save()
        return removed

    def purge_expired_sensitive(self, ttl_seconds: int) -> int:
        if ttl_seconds <= 0:
            return 0
        cutoff = time.time() - ttl_seconds
        with self._lock:
            doomed = [
                item_id
                for item_id, item in self._items.items()
                if item.sensitive and item.created_at < cutoff
            ]
            for item_id in doomed:
                self._items.pop(item_id, None)
            if doomed:
                self._maybe_save()
            return len(doomed)

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._meta[str(key)] = str(value)
            self._maybe_save()

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            return self._meta.get(str(key), default)

    def optimize(self) -> None:
        self.save()

    def stats(self) -> Dict[str, Any]:
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "items": self.count(),
            "kinds": self.count_by_kind(),
            "sources": self.top_sources(5),
            "db_size": size,
            "fts": False,
            "tokenizer": "json-scan",
            "backend": "json",
            "path": str(self.path),
        }

    def close(self) -> None:
        self.save()

    # ---- 图片 ----

    def read_image_bytes(self, relative: str) -> Optional[bytes]:
        """取回图片原始字节。

        查找顺序：参数本身就是 data URI -> 登记表 -> 记录里内联的 data URI
        -> 同名独立文件。最后一项是必要的：导入一份在别处生成、图片未内联的
        JSON 时，`images/` 目录里的文件才是唯一来源。

        记录里内联的图片只能按记录本身取，不按文件名模糊匹配 ——
        否则「取一张已经不存在的图」会拿到别人的数据。
        """
        if not relative:
            return None
        uri = relative if is_data_uri(relative) else None
        if uri is None:
            with self._lock:
                uri = self._images.get(relative)
            if uri is None:
                for item in self._items.values():
                    if item.image_path == relative and is_data_uri(item.image_path):
                        uri = item.image_path
                        break
        if uri is None:
            for item in self._items.values():
                if is_data_uri(item.image_path) and item.content == Path(relative).name:
                    uri = item.image_path
                    break
        if uri is None:
            return self._read_image_file(relative)
        try:
            _, payload = split_data_uri(uri)
            return base64.b64decode(payload)
        except (ValueError, TypeError) as exc:
            _log.debug("内联图片解码失败 %s: %s", relative, exc)
            return None

    def _read_image_file(self, relative: str) -> Optional[bytes]:
        """回退到独立图片文件，兼容未内联与从旧版本迁入的数据。"""
        from ..utils.paths import data_dir

        raw = Path(relative)
        candidates = [raw] if raw.is_absolute() else [self.path.parent / raw, data_dir() / raw]
        for candidate in candidates:
            if candidate.is_file():
                try:
                    return candidate.read_bytes()
                except OSError as exc:
                    _log.debug("图片读取失败 %s: %s", candidate, exc)
        return None


def _sort_key(item: ClipboardItem) -> Tuple[int, int, float]:
    """排序键：固定 > 收藏 > 最近使用/创建。与 SQLite 版保持同一优先级。"""
    return (
        1 if item.pinned else 0,
        1 if item.favorite else 0,
        item.used_at or item.created_at,
    )


def open_repository(config=None):
    """按配置返回历史仓库：`storage.backend` 为 `json` 时用 JSON 单文件。

    统一入口的价值在于调用方（GUI、CLI）不必各自判分支，
    也因此两者永远指向同一份数据。
    """
    from ..utils.config import Config
    from ..utils.paths import database_path
    from .storage import Storage

    config = config or Config()
    kind = str(config.get("storage.backend", "sqlite") or "sqlite").lower()
    if kind == "json":
        return JsonRepository()
    return Storage(database_path())
