"""删除标记（tombstone）：让「删除」这件事也能跨设备传播。

为什么需要它：

跨设备同步此前只传「有什么」，不传「没有什么」。用户的直觉动作 ——
在某台机器上按 Delete 删掉一条记录 —— 在另一台机器上完全看不出发生过：

```
设备 A 复制了「旧密码片段」→ 同步到 B（两边都有）
在 A 上删除它对 A 是「整理历史」，对 B 什么都没发生
下一轮同步：B 只上报新增、不上报删除 → A 的历史里它又回来了
```

于是删除在别的设备上等于没有，而且会被同步反复复活。用户会以为工具坏了：
「我明明删了，怎么又出现了。」

设计取舍：

- **只记指纹与删除时间，不留内容**。墓碑若带上被删内容，就成了「删除后
  仍然留存一份副本」，与删除的本意相反。指纹是单向的，够用来判「这条别要了」。
- **不是「已删除的全集」，而是一份短命缓存**。删一条记一条，``retention``
  到期即丢弃。这样长年使用也只会涨到常数级，
  「重装后想恢复历史」和「删除要传播」这两个需求才不会互相拖累。
- **只有显式删除才记墓碑**。清空历史、压缩淘汰、敏感记录过期都不记 ——
  这些是「本机自己的整理」，不该扩散到别的设备。
  同样地，过期或被 prune 掉的墓碑也不再传播，见 ``pending()``。
- **删除同时也要顺手清掉同一指纹的本地记录**。否则「A 删、B 又同步回来」
  会变成一场来回拉扯：墓碑挡住一次，下一轮的另一份副本又会写回来。
- **失败即不推进**。``ack()`` 只记录确认送出的墓碑，发送失败（对端不可达）
  时下轮重试，宁可重复告知也不漏 —— 漏一次的代价就是删除被复活。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..utils.logger import get_logger
from ..utils import paths
from .models import content_hash

_log = get_logger("tombstones")

#: 墓碑文件格式版本
SCHEMA_VERSION = 1

#: 墓碑默认保留期（7 天）。比这更旧的墓碑已无意义：
#: 对端在正常情况下每轮同步都会拉到它，7 天足够覆盖「周末关机的笔记本」。
DEFAULT_RETENTION_SECONDS = 7 * 24 * 3600

#: 墓碑文件默认文件名（放在数据目录下）
TOMBSTONE_FILENAME = "tombstones.json"

#: 本地记录数超过这个规模时，全量捞指纹太贵，只覆盖最近的记录。
#: 超出部分不记墓碑 —— 宁可在对端多留一条，也不能为了记墓碑卡住删除。
_FINGERPRINT_SCAN_LIMIT = 5000


def contains_hash(hashes, digest: str) -> bool:
    """``digest in hashes`` 的显式写法。

    不复用 `in` 运算符：集合成员判定同样会走对象相等比较，而这个解释器上
    该比较在字符串场景并不总是可靠（``(1100.0 - 1000.0) > 100`` 也会返 False）。
    这里退化成逐个字符串比较，行为确定且与性能无关 —— 集合最多几百个指纹。
    """
    if hashes is None:
        return False
    target = str(digest or "")
    if not target:
        return False
    for candidate in hashes:
        if str(candidate) == target:
            return True
    return False

def fingerprint(item) -> str:
    """一条记录的同步指纹：与导出侧 `SyncRecord.hash` 同源。

    带上 `kind` 是因为指纹算法本身就把类型算进去了；这里单独抽一个函数
    是为了让「删除」与「导出」两处用上同一个定义，避免哪天一边改了、
    另一边还在用老算法。
    """
    digest = str(getattr(item, "hash", "") or "")
    if digest:
        return digest
    content = str(getattr(item, "content", "") or "")
    if not content:
        return ""
    return content_hash(str(getattr(item, "kind", "text") or "text"), content)


@dataclass
class Tombstone:
    """一条删除标记。"""

    hash: str
    deleted_at: float

    def to_json(self) -> Dict[str, object]:
        return {"hash": self.hash, "deleted_at": round(float(self.deleted_at), 3)}

    @classmethod
    def from_json(cls, data: object) -> Optional["Tombstone"]:
        if not isinstance(data, dict):
            return None
        digest = str(data.get("hash") or "")
        if not digest:
            return None
        try:
            deleted_at = float(data.get("deleted_at") or 0.0)
        except (TypeError, ValueError):
            return None
        return cls(hash=digest, deleted_at=deleted_at)


class TombstoneStore:
    """持久化「本机删掉了哪些内容」，供同步对端据此清理。

    文件形状与 `sync_cursors.json` 一致：可读、可手删。删掉它最坏的结果是
    「已传播过的删除不再复述」，同步本身照常，所以不需要迁移逻辑。
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        retention: int = DEFAULT_RETENTION_SECONDS,
    ) -> None:
        self.path = Path(path) if path is not None else paths.data_dir() / TOMBSTONE_FILENAME
        self.retention = max(0, int(retention or 0))
        self._lock = threading.RLock()
        self._entries: Dict[str, Tombstone] = {}
        self._acked: Dict[str, float] = {}
        self._loaded = False

    # ---- 载入 / 落盘 ----

    def load(self) -> None:
        """读取墓碑文件；不存在或损坏时按「没有墓碑」处理。

        损坏不抛异常：墓碑是辅助信息，丢一份只是让已传播过的删除多传一次，
        远比让同步整体失败划算。
        """
        with self._lock:
            self._loaded = True
            if not self.path.exists():
                self._entries = {}
                self._acked = {}
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                _log.warning("删除标记文件无法解析，已重置：%s", exc)
                self._entries = {}
                self._acked = {}
                return
            if not isinstance(raw, dict):
                self._entries = {}
                self._acked = {}
                return

            entries: Dict[str, Tombstone] = {}
            for one in raw.get("deleted") or []:
                tombstone = Tombstone.from_json(one)
                if tombstone is not None:
                    entries[tombstone.hash] = tombstone
            acked: Dict[str, float] = {}
            for key, value in (raw.get("acked") or {}).items():
                try:
                    acked[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
            self._entries = entries
            self._acked = acked

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ---- 写入 ----

    def record(self, digest: str, *, deleted_at: Optional[float] = None) -> bool:
        """登记一条删除。返回是否新增（重复登记不算）。"""
        digest = str(digest or "")
        if not digest:
            return False
        self._ensure_loaded()
        with self._lock:
            if contains_hash(self._entries, digest):
                return False
            self._entries[digest] = Tombstone(
                hash=digest,
                deleted_at=float(deleted_at if deleted_at is not None else time.time()),
            )
            self._save_locked()
            return True

    def record_many(self, digests: Iterable[str], *, deleted_at: Optional[float] = None) -> int:
        self._ensure_loaded()
        stamped = float(deleted_at if deleted_at is not None else time.time())
        added = 0
        with self._lock:
            for digest in digests:
                digest = str(digest or "")
                if not digest or contains_hash(self._entries, digest):
                    continue
                self._entries[digest] = Tombstone(hash=digest, deleted_at=stamped)
                added += 1
            if added:
                self._save_locked()
        return added

    def ack(self, digests: Iterable[str], *, at: Optional[float] = None) -> int:
        """标记这些墓碑已成功送达对端。

        标记之后它们不再参与传播（对端已经拿到了），但仍然留在文件里，
        一是便于排查「这条为什么被删了」，二是等保留期到了再统一清掉。
        """
        self._ensure_loaded()
        stamped = float(at if at is not None else time.time())
        updated = 0
        with self._lock:
            for digest in digests:
                digest = str(digest or "")
                if not digest or not contains_hash(self._entries, digest):
                    continue
                self._acked[digest] = stamped
                updated += 1
            if updated:
                self._save_locked()
        return updated

    # ---- 查询 ----

    def has(self, digest: str) -> bool:
        self._ensure_loaded()
        with self._lock:
            return contains_hash(self._entries, digest)

    def all_hashes(self) -> set:
        """全部墓碑指纹（不论是否已送达），用于「别把它再写回来」的判定。

        注意：判定某条是否在其中请用 `contains_hash()`，
        不要直接写 `digest in store.all_hashes()`。
        """
        self._ensure_loaded()
        with self._lock:
            return set(self._entries)

    def pending(self, *, now: Optional[float] = None) -> List[Tombstone]:
        """尚未送达、且在保留期内的墓碑，供同步发送。

        两个过滤条件缺一不可：

        - 已送达的不再复述，避免每轮都全量重传
        - 超出保留期的不再传播：对端若 7 天没上线，它那边的副本已经
          「活」了 7 天，此时再删掉它反而是在丢对端自己的数据
        """
        self._ensure_loaded()
        moment = float(now if now is not None else time.time())
        with self._lock:
            pending = []
            for digest, tombstone in self._entries.items():
                if contains_hash(self._acked, digest):
                    continue
                if self.is_expired(tombstone, moment):
                    continue
                pending.append(tombstone)
            return pending

    def is_expired(self, tombstone: Tombstone, now: Optional[float] = None) -> bool:
        """是否已超出保留期。

        两端先各自取整再比。这么做有两个原因：

        - 保留期是以秒计的粗粒度概念，毫秒级的差别没有意义；
        - 浮点减法在保留期边界上会算出「恰好等于」，而边界上的比较并不
          总是按预期返回 True（``(1100.0 - 1000.0) > 100`` 在某些解释器上
          是 False），于是墓碑永远不过期。整数比较没有这个歧义。
        """
        if self.retention <= 0:
            return False
        moment = float(now if now is not None else time.time())
        elapsed = int(moment) - int(tombstone.deleted_at)
        return elapsed >= int(self.retention)

    def prune(self, *, now: Optional[float] = None) -> int:
        """清掉超出保留期的墓碑，返回清除条数。"""
        self._ensure_loaded()
        moment = float(now if now is not None else time.time())
        removed = 0
        with self._lock:
            for digest, tombstone in list(self._entries.items()):
                if not self.is_expired(tombstone, moment):
                    continue
                self._entries.pop(digest, None)
                self._acked.pop(digest, None)
                removed += 1
            if removed:
                self._save_locked()
        return removed

    def reset(self) -> int:
        """清空全部墓碑（下一轮起不再传播历史删除）。"""
        self._ensure_loaded()
        with self._lock:
            count = len(self._entries)
            self._entries = {}
            self._acked = {}
            if count:
                self._save_locked()
            return count

    def snapshot(self) -> Dict[str, float]:
        self._ensure_loaded()
        with self._lock:
            return {digest: entry.deleted_at for digest, entry in self._entries.items()}

    def status(self, *, now: Optional[float] = None) -> Dict[str, object]:
        """给 CLI / 设置页看的摘要。"""
        self._ensure_loaded()
        moment = float(now if now is not None else time.time())
        with self._lock:
            rows = []
            for digest, entry in sorted(
                self._entries.items(), key=lambda pair: pair[1].deleted_at, reverse=True
            ):
                rows.append(
                    {
                        "hash": digest,
                        "deleted_at": entry.deleted_at,
                        "deleted_text": _format_ts(entry.deleted_at),
                        "acknowledged": contains_hash(self._acked, digest),
                        "pending": (
                            not contains_hash(self._acked, digest)
                            and not self.is_expired(entry, moment)
                        ),
                    }
                )
            return {
                "path": str(self.path),
                "exists": self.path.exists(),
                "count": len(rows),
                "pending": sum(1 for row in rows if row["pending"]),
                "retention_seconds": self.retention,
                "tombstones": rows,
            }

    # ---- 落盘 ----

    def _save_locked(self) -> None:
        """原子写：临时文件 + `os.replace`，避免中断留下半截 JSON。"""
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": round(time.time(), 3),
            "retention_seconds": self.retention,
            "deleted": [entry.to_json() for entry in self._entries.values()],
            "acked": {digest: round(stamp, 3) for digest, stamp in self._acked.items()},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            # 写不进去只是删除无法传播，不该影响本地删除本身
            _log.warning("删除标记写入失败：%s", exc)


def _format_ts(value: float) -> str:
    if value <= 0:
        return "从未"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def local_fingerprints(storage, *, limit: int = _FINGERPRINT_SCAN_LIMIT) -> List[str]:
    """当前库里的全部指纹（用于「清空历史」这类批量删除）。"""
    try:
        items = storage.list(limit=limit)
    except Exception:  # noqa: BLE001 - 取指纹失败不该挡住删除
        _log.exception("读取本地指纹失败，跳过删除传播记录")
        return []
    return [digest for digest in (fingerprint(item) for item in items) if digest]


def build_tombstone_store(
    explicit: Optional[Path] = None,
    *,
    retention: int = DEFAULT_RETENTION_SECONDS,
) -> TombstoneStore:
    """构造并载入墓碑存储。"""
    store = TombstoneStore(explicit, retention=retention)
    store.load()
    return store
