"""标记变更（取消固定 / 取消收藏）：让「取消」也能跨设备传播。

为什么需要它：

固定与收藏的同步此前是**取并集** —— 任何一台设备设过，所有设备都变「已设」。
当时的理由写在 ``FlagSnapshot.merge`` 里：同步通道里「取消过」和「从未设过」
的表示完全相同（都是本地 False），没有额外信息就无法区分，于是只能取并集。
取并集是「不会悄悄弄丢用户显式标记」的选择，代价是**取消传不出去**：

```
在笔记本上收藏了某条  → 并集让台式机上也变成已收藏
在笔记本上取消收藏    → 台式机上看不出发生过，仍然是已收藏
```

用户会以为同步坏了 —— 他确实做了一次显式的取消动作。

现在补上那条「与墓碑同性质的通道」：取消本身成为一个**带时间戳的事件**记录
下来，随同步传出去。这样「我在笔记本上取消了」才是一个能被传播的事实，
而不是靠猜。

与墓碑的异同：

- **同**：都只记指纹与时间，不记内容；都随同一条同步日志走；都用「已送达」
  标记避免每轮复述；都是可牺牲的辅助数据（损坏即丢弃，最坏是取消多传一轮）。
- **异**：墓碑是「这条内容别要了」，只需单向传播；标记变更是「某条内容上的
  某个标记现在是什么」，**需要与并集语义共存**，且同一标记可以被反复设置/取消。

怎么和并集共存 —— 这是本模块最需要说清的地方：

并集本身没有错，错在「没有任何设备说过取消」。所以判定拆成两步：

1. **每个设备各自只看自己最近的意图**。某设备最新一次针对该标记的动作是
   取消，它对这个标记的贡献就是「未设」；是最新一次设置，贡献就是「已设」。
   同一台设备反复切换时，时间戳保证最新的那次生效。
2. **跨设备取并集**，但只并「贡献为已设」的那些设备。

于是「我在笔记本上取消」终于能生效 —— 前提是没有别的设备设过它。若台式机
也设过，并集仍会保留（那是台式机用户的显式意图，不该被笔记本的取消抹掉）。
这条边界写进了 README 与设置页：**取消只对自己那台生效，别的设备设过就还在**。

时间戳的可靠前提：设备之间不要求时钟同步，因为**同一台设备内的先后关系只由
它自己的时间戳决定**。跨设备的比较退化成并集，不依赖时钟。
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
from .tombstones import contains_hash

_log = get_logger("flag_changes")

#: 文件格式版本
SCHEMA_VERSION = 1

#: 默认文件名（放在数据目录下）
FLAG_CHANGE_FILENAME = "flag_changes.json"

#: 已知的标记名。用字符串而不是枚举，是为了让同步日志里这一字段
#: 一眼可读、且以后加标记时老版本读到未知名字能安全忽略。
FLAG_PINNED = "pinned"
FLAG_FAVORITE = "favorite"

KNOWN_FLAGS = (FLAG_PINNED, FLAG_FAVORITE)

#: 每个指纹最多保留多少条变更记录。同一指纹的变更天然是常数级
#: （用户不会把一条记录反复固定上万次），留这个上限只是防止异常写入把
#: 文件撑爆。超出后丢弃最旧的那些 —— 丢弃的后果是「那次取消可能不被传播」，
#: 对端会退回并集语义（保守地保留标记），不会弄丢内容。
MAX_CHANGES_PER_HASH = 64

#: 文件整体最多保留多少条变更，超出的按时间裁剪。
MAX_CHANGES = 20_000


@dataclass
class FlagChange:
    """一次显式的标记变更（设置或取消）。"""

    hash: str
    flag: str
    value: bool
    at: float
    #: 做出这个动作的设备名。空串表示本机动作 —— 语义上就是「这台设备」，
    #: 与墓碑里 `pinned_by` 的约定一致。
    device: str = ""

    def to_json(self) -> Dict[str, object]:
        return {
            "hash": self.hash,
            "flag": self.flag,
            "value": bool(self.value),
            "at": round(float(self.at), 3),
            "device": self.device,
        }

    @classmethod
    def from_json(cls, data: object) -> Optional["FlagChange"]:
        if not isinstance(data, dict):
            return None
        digest = str(data.get("hash") or "")
        flag = str(data.get("flag") or "")
        if not digest or flag not in KNOWN_FLAGS:
            return None
        try:
            at = float(data.get("at") or 0.0)
        except (TypeError, ValueError):
            return None
        if at <= 0:
            return None
        return cls(
            hash=digest,
            flag=flag,
            value=bool(data.get("value", False)),
            at=at,
            device=str(data.get("device") or ""),
        )


def resolve_flag(changes: Iterable[FlagChange], flag: str) -> Optional[bool]:
    """按「设备内取最新、设备间取并集」算出一个标记的最终值。

    返回 ``None`` 表示没有任何设备对这个标记表过态 —— 调用方应据此
    保留本地已有的值（不能当成 False，那会把老数据里本来就有的标记抹掉）。

    为什么设备内只看最新一条：同一台设备上的动作有明确的先后关系，
    先固定后取消就是「最终不要」，反之亦然。跨设备没有可靠的先后关系
    （时钟不同步），所以并集 —— 任何一台的「要」都算数。
    """
    per_device: Dict[str, FlagChange] = {}
    for change in changes:
        if change.flag != flag:
            continue
        current = per_device.get(change.device)
        if current is None or change.at >= current.at:
            per_device[change.device] = change
    if not per_device:
        return None
    # 任一设备的最新意图是「已设」 → 整体为真
    return any(change.value for change in per_device.values())


class FlagChangeStore:
    """持久化「本机做过哪些标记变更」，供同步对端据此更新。

    文件形状与 `tombstones.json` 一致：可读、可手删。删掉它最坏的结果是
    「已传播过的取消不再复述」，对端退回并集语义（保守保留标记），
    同步本身照常，所以不需要迁移逻辑。
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = (
            Path(path) if path is not None else paths.data_dir() / FLAG_CHANGE_FILENAME
        )
        self._lock = threading.RLock()
        self._entries: List[FlagChange] = []
        self._acked: Dict[str, float] = {}
        self._loaded = False

    # ---- 载入 / 落盘 ----

    def load(self) -> None:
        """读取变更文件；不存在或损坏时按「没有变更」处理。

        损坏不抛异常：它是辅助信息，丢一份只是让标记多传一轮，
        远比让同步整体失败划算。
        """
        with self._lock:
            self._loaded = True
            if not self.path.exists():
                self._entries = []
                self._acked = {}
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                _log.warning("标记变更文件无法解析，已重置：%s", exc)
                self._entries = []
                self._acked = {}
                return
            if not isinstance(raw, dict):
                self._entries = []
                self._acked = {}
                return
            entries: List[FlagChange] = []
            for one in raw.get("changes") or []:
                change = FlagChange.from_json(one)
                if change is not None:
                    entries.append(change)
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

    def record(
        self,
        digest: str,
        flag: str,
        value: bool,
        *,
        at: Optional[float] = None,
        device: str = "",
    ) -> bool:
        """登记一次显式变更。

        **幂等保护**：若同一个指纹 + 标记上，本设备最新一条记录的 `value`
        已经等于本次的值，则不新增 —— 否则「打开设置页时刷新一遍标记状态」
        这种无副作用的动作会不断往文件里追加重复事件。
        """
        digest = str(digest or "")
        flag = str(flag or "")
        if not digest or flag not in KNOWN_FLAGS:
            return False
        self._ensure_loaded()
        stamped = float(at if at is not None else time.time())
        with self._lock:
            latest = self._latest_for(digest, flag, device)
            if latest is not None and latest.value == bool(value):
                return False
            self._entries.append(
                FlagChange(
                    hash=digest,
                    flag=flag,
                    value=bool(value),
                    at=stamped,
                    device=str(device or ""),
                )
            )
            self._trim_locked()
            self._save_locked()
            return True

    def record_many(
        self,
        changes: Iterable[tuple],
        *,
        at: Optional[float] = None,
        device: str = "",
    ) -> int:
        """批量登记 ``(hash, flag, value)`` 三元组。返回真正新增的条数。"""
        self._ensure_loaded()
        stamped = float(at if at is not None else time.time())
        added = 0
        with self._lock:
            for entry in changes:
                try:
                    digest, flag, value = entry
                except (TypeError, ValueError):
                    continue
                digest = str(digest or "")
                flag = str(flag or "")
                if not digest or flag not in KNOWN_FLAGS:
                    continue
                latest = self._latest_for(digest, flag, device)
                if latest is not None and latest.value == bool(value):
                    continue
                self._entries.append(
                    FlagChange(
                        hash=digest,
                        flag=flag,
                        value=bool(value),
                        at=stamped,
                        device=str(device or ""),
                    )
                )
                added += 1
            if added:
                self._trim_locked()
                self._save_locked()
        return added

    def ack(self, keys: Iterable[str], *, at: Optional[float] = None) -> int:
        """标记这些变更已成功送达对端，之后不再复述。

        `keys` 是 ``FlagChange` 的唯一标识（见 ``_key``），而不是指纹 ——
        同一个指纹会有多条变更，按指纹确认会把尚未送达的也一并标掉。
        """
        self._ensure_loaded()
        stamped = float(at if at is not None else time.time())
        updated = 0
        with self._lock:
            known = {self._key(change) for change in self._entries}
            for key in keys:
                key = str(key or "")
                if not key or not contains_hash(known, key):
                    continue
                self._acked[key] = stamped
                updated += 1
            if updated:
                self._save_locked()
        return updated

    # ---- 查询 ----

    def all_changes(self) -> List[FlagChange]:
        self._ensure_loaded()
        with self._lock:
            return list(self._entries)

    def changes_for(self, digest: str) -> List[FlagChange]:
        self._ensure_loaded()
        with self._lock:
            return [change for change in self._entries if change.hash == digest]

    def effective(self, digest: str, flag: str) -> Optional[bool]:
        """该指纹上这个标记的最终值（含对端带来的变更）。None 表示无人表态。"""
        return resolve_flag(self.changes_for(digest), flag)

    def pending(self) -> List[FlagChange]:
        """尚未送达的变更，供同步发送。"""
        self._ensure_loaded()
        with self._lock:
            return [
                change
                for change in self._entries
                if not contains_hash(self._acked, self._key(change))
            ]

    def reset(self) -> int:
        """清空全部变更（下一轮起不再传播历史取消）。"""
        self._ensure_loaded()
        with self._lock:
            count = len(self._entries)
            self._entries = []
            self._acked = {}
            if count:
                self._save_locked()
            return count

    def prune(self, *, older_than: float = 0.0, now: Optional[float] = None) -> int:
        """清掉已经送达、且早于 ``older_than`` 的变更。返回清除条数。

        ``older_than <= 0`` 时不做任何事 —— 「不过期」是默认，
        与墓碑那种「必须过期」的需求不同：一条取消事件留得越久越安全，
        它只是几十字节。
        """
        if older_than <= 0:
            return 0
        self._ensure_loaded()
        moment = float(now if now is not None else time.time())
        cutoff = moment - float(older_than)
        removed = 0
        with self._lock:
            kept: List[FlagChange] = []
            for change in self._entries:
                key = self._key(change)
                if change.at < cutoff and contains_hash(self._acked, key):
                    self._acked.pop(key, None)
                    removed += 1
                    continue
                kept.append(change)
            if removed:
                self._entries = kept
                self._save_locked()
        return removed

    def status(self) -> Dict[str, object]:
        """给 CLI / 设置页看的摘要。"""
        self._ensure_loaded()
        with self._lock:
            rows = []
            for change in sorted(self._entries, key=lambda one: one.at, reverse=True):
                key = self._key(change)
                rows.append(
                    {
                        "hash": change.hash,
                        "flag": change.flag,
                        "value": change.value,
                        "device": change.device,
                        "at": change.at,
                        "at_text": _format_ts(change.at),
                        "acknowledged": contains_hash(self._acked, key),
                        "pending": not contains_hash(self._acked, key),
                    }
                )
            return {
                "path": str(self.path),
                "exists": self.path.exists(),
                "count": len(rows),
                "pending": sum(1 for row in rows if row["pending"]),
                "changes": rows,
            }

    # ---- 内部 ----

    def _key(self, change: FlagChange) -> str:
        """一条变更的稳定标识：同一条变更在多轮之间必须得到同一个 key。

        用「指纹 + 标记 + 时间戳 + 设备」而不是列表下标 —— 下标会随
        裁剪/重排漂移，已送达标记就会认错对象。
        """
        return f"{change.hash}|{change.flag}|{change.at:.3f}|{change.device}"

    def _latest_for(self, digest: str, flag: str, device: str) -> Optional[FlagChange]:
        """本设备在该指纹该标记上最新的一条变更。调用方需持有锁。"""
        target = str(device or "")
        latest: Optional[FlagChange] = None
        for change in self._entries:
            if change.hash != digest or change.flag != flag:
                continue
            if change.device != target:
                continue
            if latest is None or change.at >= latest.at:
                latest = change
        return latest

    def _trim_locked(self) -> None:
        """裁剪过量的变更，保留最近的那些。调用方需持有锁。"""
        if len(self._entries) > MAX_CHANGES_PER_HASH:
            per_hash: Dict[str, List[FlagChange]] = {}
            for change in self._entries:
                per_hash.setdefault(change.hash, []).append(change)
            kept: List[FlagChange] = []
            for group in per_hash.values():
                if len(group) <= MAX_CHANGES_PER_HASH:
                    kept.extend(group)
                    continue
                group.sort(key=lambda one: one.at)
                kept.extend(group[-MAX_CHANGES_PER_HASH:])
            self._entries = kept
        if len(self._entries) > MAX_CHANGES:
            self._entries.sort(key=lambda one: one.at)
            self._entries = self._entries[-MAX_CHANGES:]

    def _save_locked(self) -> None:
        """原子写：临时文件 + `os.replace`，避免中断留下半截 JSON。"""
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": round(time.time(), 3),
            "changes": [change.to_json() for change in self._entries],
            "acked": {key: round(stamp, 3) for key, stamp in self._acked.items()},
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
            # 写不进去只是取消无法传播，不该影响本地取消本身
            _log.warning("标记变更写入失败：%s", exc)


def _format_ts(value: float) -> str:
    if value <= 0:
        return "从未"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def build_flag_change_store(explicit: Optional[Path] = None) -> FlagChangeStore:
    """构造并载入变更存储。"""
    store = FlagChangeStore(explicit)
    store.load()
    return store
