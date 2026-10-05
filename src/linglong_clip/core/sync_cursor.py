"""同步增量游标：记住「每个来源已经拉到哪」，避免每轮从头重扫。

为什么需要它：

`FolderSync.import_all()` 与 `LanSyncClient.sync_peer()` 原先每轮都从
`since=0` 开始，靠 hash 去重来保证不重复导入。记录少时无感，但剪贴板历史
天然会涨到上万条，于是每轮同步都在做同一件无用功：

- 服务端把全部历史重新序列化、加密、走一遍 HTTP
- 客户端把全部记录重新解析、解密、建 hash 集合
- 真正的新增可能只有一两条

游标把「已经拉到哪」持久化下来，下一轮只处理后面的记录。
收益随记录量线性放大，而代价只是一个几十字节的 JSON。

设计取舍：

- **按来源分别记游标**，而不是全局一个。同一个设备可能同时面对多个对端，
  每个对端的进度互不相干，共用一个游标会互相拖后腿。
- **两种来源的「位置」含义不同，所以分开存**：

  | 来源 | 位置 | 为什么 |
  | --- | --- | --- |
  | `folder:` | 日志条目序号（第几条，见 `entry_for`） | 日志是追加式的，序号天然单调 |
  | `lan:` | `(时间戳, id)`（见 `position_for`） | 这是协议的一部分，对端按它翻页 |

  合成一个数会得出一个对两种来源都不对的答案。
- **位置旁边还要存一份「锚点」**，因为序号会被重写。日志裁剪/重写之后，
  同一个序号指向的是**另一条**记录，而序号本身没有任何自证能力：
  「读到第 2 条」在裁剪前指 c1、裁剪后指 n0，两个位置长得一模一样。
  只比长度（`position > len`）挡不住长度恰好相等的重写 —— 那是最常见的
  稳态：日志被裁到上限之后就一直在上限附近打转。此时导入侧看到
  「游标 2 ≤ 长度 2」，安心地从第 2 条之后开始读，于是**每条新记录都被跳过**，
  而且一轮一轮地跳过，界面上写着「同步成功、0 条」。
  锚点就是「我读完时最后那条是什么」，读之前先拿它核对；
  对不上就退回全量重扫（hash 会去重，代价只是一轮多读）。
- **共享文件夹为什么不能用时间戳**：时间戳由写入方给、**不保证单调**
  （系统剪贴板历史导入、存储迁移、任何显式构造 `created_at=过去` 的路径
  都会写下更早的时间）。按它过滤时，新追加的记录恰好在游标之前，
  被永久跳过 —— 而导出侧只看 `hash` 去重、照发不误：
  两台机器各有一套自洽的说法（B 说「发了」，C 说「没收到」）。
  条目序号与记录里任何字段无关，是「这条是新的吗」唯一可靠的判据。
- **`lan:` 的游标是 `(时间戳, id)` 复合值，不是单独的时间戳**。同一时刻写入的
  多条记录共享 `created_at`（紧密循环写库、批量导入系统历史都会这样）。只记
  时间戳时，若这一时刻的记录多到塞满一页，游标就推不动了：下一页请求
  `created_at > T` 返回空，而剩下的记录并不是「更旧」，是**一样新** ——
  它们既不在这一页、也永远不在下一页，且没有任何报错。
  `id` 是 `(created_at, id)` 全序里的次级键，补上它翻页就总能前进。
  安全窗口（`SAFETY_MARGIN`）解决不了这个问题：回退一秒只会反复重读同一页。
- **只在导入成功后推进**。失败时保留旧游标，下轮重试；否则对端短暂不可达
  就会永久跳过那段时间里的记录。
- **文件损坏不阻断同步**。解析失败时丢弃游标从 0 重来，只是慢一轮，
  而不是让同步直接报错。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from ..utils.logger import get_logger
from ..utils import paths

_log = get_logger("sync-cursor")

#: 游标文件格式版本。3 起条目游标带锚点，2 起每项是 `[时间戳, id]`，
#: 1 是裸时间戳（仍可读）。旧版读到新字段会忽略它并退回全量重扫，只是慢一轮。
SCHEMA_VERSION = 3

#: 推进游标时回退的安全窗口（秒）。覆盖「同一时刻多条记录」的边界情况，
#: 代价只是下次多拉几条已被 hash 去重的记录。
SAFETY_MARGIN = 1.0

#: 游标文件默认文件名（放在数据目录下）
CURSOR_FILENAME = "sync_cursors.json"


class SyncCursorStore:
    """持久化每个同步来源的增量位置。

    文件形状刻意保持可读、可手删：删掉它最坏的结果是下一轮全量重扫，
    与升级前的行为等价，所以不需要任何迁移逻辑。
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else paths.data_dir() / CURSOR_FILENAME
        self._lock = threading.RLock()
        #: `key -> (created_at, id)`。id 为 0 表示「只按时间对齐」的旧记录。
        self._cursors: Dict[str, Tuple[float, int]] = {}
        #: `key -> 已读到的日志条目序号`（从 1 起，0 表示还没读）。
        #: 与 `_cursors` 分开：前者是**协议**位置（向对端请求到哪），
        #: 后者是**本地**位置（哪些记录已处理）。两个不同的问题。
        self._entries: Dict[str, int] = {}
        #: `key -> 读到第 entries 条时，第 entries 条的指纹`。
        #: 用来回答「这个序号现在还指同一条记录吗」—— 序号本身回答不了。
        #: 空串表示「当时那条没有指纹」，退化为旧行为（只比长度）。
        self._anchors: Dict[str, str] = {}
        self._loaded = False

    # ---- 载入 / 落盘 ----

    def load(self) -> None:
        """读取游标文件；不存在或损坏时按「全部从 0 开始」处理。"""
        with self._lock:
            self._loaded = True
            if not self.path.exists():
                self._cursors = {}
                self._entries = {}
                self._anchors = {}
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                # 损坏就重来，不抛异常：同步只是慢一轮，不应因此失败
                _log.warning("同步游标文件无法解析，已重置：%s", exc)
                self._cursors = {}
                self._entries = {}
                self._anchors = {}
                return
            cursors = raw.get("cursors") if isinstance(raw, dict) else None
            if not isinstance(cursors, dict):
                self._cursors = {}
                self._entries = {}
                self._anchors = {}
                return
            cleaned: Dict[str, Tuple[float, int]] = {}
            for key, value in cursors.items():
                parsed = _parse_cursor(value)
                if parsed is None:
                    continue
                cleaned[str(key)] = parsed
            self._cursors = cleaned
            #: 缺失即「还没读过」：老版本没有这个字段，退回全量重扫一轮
            #: 是安全的（hash 会去重），而不是把历史判成「已读过」。
            entries = raw.get("entries") if isinstance(raw, dict) else None
            self._entries = (
                {str(k): max(0, int(v or 0)) for k, v in entries.items()}
                if isinstance(entries, dict)
                else {}
            )
            #: 老版本没有锚点：缺了就当「无从核对」，等价于只比长度。
            #: 这会让升级后的第一轮把可疑的位置重扫一遍，之后锚点就补齐了。
            anchors = raw.get("anchors") if isinstance(raw, dict) else None
            self._anchors = (
                {str(k): str(v or "") for k, v in anchors.items()}
                if isinstance(anchors, dict)
                else {}
            )

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ---- 读写 ----

    @staticmethod
    def key_for(scope: str, source: str) -> str:
        """把「同步模式 + 来源标识」拼成游标键，如 `lan:192.168.1.9:48620`。"""
        return f"{scope}:{source}"

    def get(self, key: str) -> float:
        """游标位置的时间戳部分。"""
        return self.get_pair(key)[0]

    def get_pair(self, key: str) -> Tuple[float, int]:
        """游标位置 `(created_at, id)`。"""
        self._ensure_loaded()
        with self._lock:
            return self._cursors.get(key, (0.0, 0))

    def set(self, key: str, value: float, *, since_id: int = 0, persist: bool = True) -> None:
        self._ensure_loaded()
        with self._lock:
            self._cursors[key] = (max(0.0, float(value)), max(0, int(since_id or 0)))
            if persist:
                self._save_locked()

    def advance(
        self,
        key: str,
        value: float,
        *,
        since_id: int = 0,
        persist: bool = True,
    ) -> Tuple[float, int]:
        """把游标向前推进到 `(value, since_id)`（只增不减），返回推进后的值。

        回退保护很重要：多对端同步时若某个对端返回了较旧的位置，
        不应把已经同步过的进度反向拉低。比较用的是 `(created_at, id)`
        字典序 —— 同一时刻内 `id` 更大的才是更靠后的位置，
        只比时间戳会把这层信息丢掉，于是同刻记录永远推不过去。
        """
        self._ensure_loaded()
        target = (max(0.0, float(value)), max(0, int(since_id or 0)))
        with self._lock:
            current = self._cursors.get(key, (0.0, 0))
            updated = max(current, target)
            self._cursors[key] = updated
            if persist and updated != current:
                self._save_locked()
            return updated

    def entry_for(self, key: str) -> int:
        """该来源的「已读到第几条」。0 表示还没读过。"""
        self._ensure_loaded()
        with self._lock:
            return max(0, int(self._entries.get(key, 0) or 0))

    def anchor_for(self, key: str) -> str:
        """读到 `entry_for(key)` 时，那一条的指纹。空串表示无从核对。"""
        self._ensure_loaded()
        with self._lock:
            return str(self._anchors.get(key, "") or "")

    def advance_entry(self, key: str, value: int, *, anchor: Optional[str] = None) -> int:
        """推进条目游标，只增不减。

        ``anchor`` 是「第 value 条」的指纹，用来在下一轮回答
        「这个序号现在还指同一条记录吗」。不传时保留原有锚点 ——
        本地日志的追加不会改变已读过那些条目，锚点仍然成立。
        """
        self._ensure_loaded()
        target = max(0, int(value or 0))
        with self._lock:
            current = max(0, int(self._entries.get(key, 0) or 0))
            if target < current:
                # 只增不减。调用方若想回退，走 `reset_entry` ——
                # 那条路径会连带清掉锚点，语义是完整的「回到起点」。
                return current
            if anchor is None:
                self._anchors.setdefault(key, "")
            else:
                self._anchors[key] = str(anchor or "")
            self._entries[key] = target
            if target != current:
                self._save_locked()
            return target

    def entry_is_valid(self, key: str, total: int, anchor_at) -> bool:
        """游标指向的位置是否仍然成立。

        ``total`` 是对端日志当前的条目总数，``anchor_at`` 接受一个
        「给序号、返回指纹」的可调用对象（``anchor_at(index) -> str``），
        由调用方提供 —— 日志怎么读是同步层的事，游标只管位置。

        三种情况：

        - 还没读过（位置 0）→ 天然有效，从头读就是全量。
        - 日志比游标短 → 一定失效。重写过的日志整体前移，序号已经
          指不到原来的位置了。
        - 长度相等或更长 → **不能只看长度**。裁剪之后长度可能恰好
          和游标相等，此时序号指向的是另一条记录，而长度判据看不出来。
          拿锚点核对：对得上才算位置仍成立。

        锚点为空（老版本文件、或当时那条没有指纹）时退回只比长度 ——
        这是升级路径上的必要让步，不是判定逻辑的一部分。
        """
        self._ensure_loaded()
        with self._lock:
            position = max(0, int(self._entries.get(key, 0) or 0))
            if position <= 0:
                return True
            if position > max(0, int(total or 0)):
                return False
            expected = str(self._anchors.get(key, "") or "")
            if not expected:
                return True
            try:
                actual = str(anchor_at(position) or "")
            except Exception:  # noqa: BLE001 - 拿不到锚点就不敢判失效
                return True
            return actual == expected

    def reset_entry(self, key: str) -> int:
        """条目游标退回 0，并清掉锚点。

        对端日志被裁剪或重写之后必须这么做 —— 否则条目序号会整体前移，
        本地游标就会指向错误的位置，把没读过的记录判成「已读过」。
        """
        self._ensure_loaded()
        with self._lock:
            previous = max(0, int(self._entries.get(key, 0) or 0))
            self._entries[key] = 0
            self._anchors.pop(key, None)
            if previous:
                self._save_locked()
            return previous

    def entry_snapshot(self) -> Dict[str, int]:
        self._ensure_loaded()
        with self._lock:
            return dict(self._entries)

    def since_for(self, key: str) -> float:
        """返回该来源的请求起点：游标回退安全窗口后的值。"""
        return max(0.0, self.get(key) - SAFETY_MARGIN)

    def since_pair_for(self, key: str) -> Tuple[float, int]:
        """请求起点 `(since, since_id)`，含安全窗口与复合键。

        回退安全窗口后会顺带把 `id` 清零：回到「该时刻之前」意味着
        该时刻的记录要整批重读一次，重复由 hash 去重吃掉。
        不复位 `id` 的话，回退到 `T - 1` 却仍要求 `id > 500`，
        会漏掉 `T - 1` 到 `T` 之间 id 较小的记录。
        """
        ts, sid = self.get_pair(key)
        if ts <= 0:
            return (0.0, 0)
        shifted = max(0.0, ts - SAFETY_MARGIN)
        return (shifted, 0) if shifted != ts else (ts, sid)

    def forget(self, key: str, *, persist: bool = True) -> bool:
        """忘掉某个来源的全部位置。

        **三张表要一起清。** 一个来源的位置是三份东西：协议位置
        （`cursors`）、本地位置（`entries`）、以及本地位置的锚点
        （`anchors`）。只 pop `cursors` 会留下后两份 —— 而下次这个来源
        再出现时，`entry_for()` 仍返回旧值，`entry_is_valid()` 会拿一个
        **属于上次那个对端**的锚点去核对，核对结果只取决于巧合。
        """
        self._ensure_loaded()
        with self._lock:
            existed = self._cursors.pop(key, None) is not None
            existed = self._entries.pop(key, None) is not None or existed
            existed = self._anchors.pop(key, None) is not None or existed
            if existed and persist:
                self._save_locked()
            return existed

    def reset(self, *, persist: bool = True) -> None:
        """清空局域网游标（`cursors`），下一轮回到全量重扫。

        **只清 `cursors`，不碰 `entries` / `anchors`。** 这两种位置回答的是
        两个不同的问题：「向对端请求到哪」（局域网，协议的一部分）与
        「本地处理到哪」（共享文件夹，本地进度）。一次全清会让两种同步模式
        互相牵连 —— 清了共享文件夹的进度，下一轮就要把对端日志重读一遍。

        名字保留成 `reset` 是历史原因（它一直只做这件事）；需要「全都清掉」
        的调用方请用 `reset_all()`，不要往这里加参数。
        """
        with self._lock:
            self._loaded = True
            self._cursors = {}
            if persist:
                self._save_locked()

    def reset_entry_cursors(self, *, persist: bool = True) -> int:
        """清空共享文件夹的条目进度（含锚点），返回清掉的来源数。

        与 `reset()` 对称的另一半。日志被对端清空、或用户换了同步目录时，
        残留下来的条目进度会**永远清不掉**：判失效要先有日志文件才走得到
        `reset_entry()`，而日志已经不存在了。结果文件里长期留着一批
        指向不存在日志的位置，`--sync-cursor` 每次都把它们列出来。
        """
        self._ensure_loaded()
        with self._lock:
            count = len(self._entries)
            self._entries = {}
            self._anchors = {}
            if count and persist:
                self._save_locked()
            return count

    def reset_all(self, *, persist: bool = True) -> Tuple[int, int]:
        """清空全部位置，返回 `(局域网来源数, 共享文件夹来源数)`。

        给 `--sync-cursor-reset` 用：「重新全量扫描」就该是全量，
        包括那些日志已经不在本机的来源。
        """
        with self._lock:
            self._loaded = True
            lan = len(self._cursors)
            entries = len(self._entries)
            self._cursors = {}
            self._entries = {}
            self._anchors = {}
            if (lan or entries) and persist:
                self._save_locked()
            return lan, entries

    def snapshot(self) -> Dict[str, float]:
        """`key -> 时间戳`，保持既有形状（CLI / 用例按它取用）。"""
        self._ensure_loaded()
        with self._lock:
            return {key: pair[0] for key, pair in self._cursors.items()}

    def snapshot_pairs(self) -> Dict[str, Tuple[float, int]]:
        self._ensure_loaded()
        with self._lock:
            return dict(self._cursors)

    def status(self) -> Dict[str, object]:
        """给 CLI / 设置页看的摘要。

        两种来源**位置的含义不同**，所以一起报出来、由上层按前缀各自表述：

        - `folder:` 按「读到日志第几条」推进（`entries`）
        - `lan:` 按 `(时间戳, id)` 推进（`since` / `since_id`），那是协议的一部分

        合成一个数会得出一个对两种来源都不对的答案。
        """
        self._ensure_loaded()
        with self._lock:
            rows = []
            for key in sorted(set(self._cursors) | set(self._entries)):
                value = self._cursors.get(key, (0.0, 0))
                entries = max(0, int(self._entries.get(key, 0) or 0))
                rows.append(
                    {
                        "key": key,
                        "since": value[0],
                        "since_id": value[1],
                        "since_text": _format_ts(value[0]),
                        "entries": entries,
                        "anchor": str(self._anchors.get(key, "") or ""),
                        "position_text": (
                            f"第 {entries} 条" if key.startswith("folder:")
                            else _format_ts(value[0])
                        ),
                    }
                )
            lan_count = len(self._cursors)
            # `entry_count` 是**位置**个数而不是「有几条游标」：它按 key 数，
            # 与 `lan_count` 相加才是行数（同一来源两种位置只出一行）。
            # 报「清了 1 条、还剩 2 条」的根因就是拿 `len(snapshot())`
            # （只有局域网那份）当总数，而列表里排的是两份的并集。
            entry_count = len(self._entries)
            return {
                "path": str(self.path),
                "exists": self.path.exists(),
                "count": len(rows),
                "lan_count": lan_count,
                "entry_count": entry_count,
                "cursors": rows,
            }

    # ---- 落盘 ----

    def _save_locked(self) -> None:
        """原子写：临时文件 + `os.replace`，避免中途中断留下半截 JSON。"""
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": time.time(),
            # 写成 `[时间戳, id]`：旧版程序读到列表会解析失败并重置游标，
            # 结果只是慢一轮全量重扫，不会读错位置。
            "cursors": {key: [pair[0], pair[1]] for key, pair in self._cursors.items()},
            # 条目游标单独一格：它与 `cursors` 回答的是两个不同的问题
            # （本地处理到哪 / 向对端请求到哪），合并会让两者互相干扰。
            "entries": {key: int(value) for key, value in self._entries.items() if value},
            # 锚点与 entries 一一对应：少了它，「读到第几条」在日志被重写后
            # 就是个无法核对的数字（见 `entry_is_valid`）。
            "anchors": {
                key: anchor for key, anchor in self._anchors.items() if anchor
            },
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
            # 游标写不进去只是退化为全量重扫，不该影响同步本身
            _log.warning("同步游标写入失败：%s", exc)


def _parse_cursor(value: object) -> Optional[Tuple[float, int]]:
    """把文件里的一项还原成 `(created_at, id)`，无法识别时返回 `None`。

    兼容三种历史形状：`[ts, id]`（当前）、裸数字 `ts`（v1）、`{"since":..}`。
    解析不了就整项丢弃 —— 游标只是缓存，丢掉最坏是慢一轮。
    """
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return (max(0.0, float(value[0])), max(0, int(value[1])))
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        try:
            return (max(0.0, float(value.get("since", 0.0))), max(0, int(value.get("since_id", 0))))
        except (TypeError, ValueError):
            return None
    try:
        return (max(0.0, float(value)), 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _format_ts(value: float) -> str:
    if value <= 0:
        return "从未"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def build_cursor_store(explicit: Optional[Path] = None) -> SyncCursorStore:
    """构造并载入游标存储。"""
    store = SyncCursorStore(explicit)
    store.load()
    return store
