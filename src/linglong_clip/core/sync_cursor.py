"""同步增量游标：记住「每个来源已经拉到哪」，避免每轮从头重扫。

为什么需要它：

`FolderSync.import_all()` 与 `LanSyncClient.sync_peer()` 原先每轮都从
`since=0` 开始，靠 hash 去重来保证不重复导入。记录少时无感，但剪贴板历史
天然会涨到上万条，于是每轮同步都在做同一件无用功：

- 服务端把全部历史重新序列化、加密、走一遍 HTTP
- 客户端把全部记录重新解析、解密、建 hash 集合
- 真正的新增可能只有一两条

游标把「已同步到的时间点」持久化下来，下一轮只请求晚于该时间点的记录。
收益随记录量线性放大，而代价只是一个几十字节的 JSON。

设计取舍：

- **按来源分别记游标**，而不是全局一个。同一个设备可能同时面对多个对端，
  每个对端的进度互不相干，共用一个游标会互相拖后腿。
- **时间游标而非序号游标**。对端的记录量与本地的 `max_items` 淘汰策略无关，
  用自增序号会在对端清理日志后错位；时间戳单调且跨设备语义一致。
- **只在导入成功后推进**。失败时保留旧游标，下轮重试；否则对端短暂不可达
  就会永久跳过那段时间里的记录。
- **`created_at` 相同的记录可能漏掉**。同一毫秒写入的多条记录共享时间戳，
  若游标恰好落在该时刻，边界记录会被 `<=` 过滤。为此游标回退一个安全窗口
  （`SAFETY_MARGIN`），用少量重复换取不漏——去重本就是幂等的。
- **文件损坏不阻断同步**。解析失败时丢弃游标从 0 重来，只是慢一轮，
  而不是让同步直接报错。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from ..utils.logger import get_logger
from ..utils import paths

_log = get_logger("sync-cursor")

#: 游标文件格式版本
SCHEMA_VERSION = 1

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
        self._cursors: Dict[str, float] = {}
        self._loaded = False

    # ---- 载入 / 落盘 ----

    def load(self) -> None:
        """读取游标文件；不存在或损坏时按「全部从 0 开始」处理。"""
        with self._lock:
            self._loaded = True
            if not self.path.exists():
                self._cursors = {}
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                # 损坏就重来，不抛异常：同步只是慢一轮，不应因此失败
                _log.warning("同步游标文件无法解析，已重置：%s", exc)
                self._cursors = {}
                return
            cursors = raw.get("cursors") if isinstance(raw, dict) else None
            if not isinstance(cursors, dict):
                self._cursors = {}
                return
            cleaned: Dict[str, float] = {}
            for key, value in cursors.items():
                try:
                    cleaned[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
            self._cursors = cleaned

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ---- 读写 ----

    @staticmethod
    def key_for(scope: str, source: str) -> str:
        """把「同步模式 + 来源标识」拼成游标键，如 `lan:192.168.1.9:48620`。"""
        return f"{scope}:{source}"

    def get(self, key: str) -> float:
        self._ensure_loaded()
        with self._lock:
            return float(self._cursors.get(key, 0.0))

    def set(self, key: str, value: float, *, persist: bool = True) -> None:
        self._ensure_loaded()
        with self._lock:
            self._cursors[key] = max(0.0, float(value))
            if persist:
                self._save_locked()

    def advance(self, key: str, value: float, *, persist: bool = True) -> float:
        """把游标向前推进到 `value`（只增不减），返回推进后的值。

        回退保护很重要：多对端同步时若某个对端返回了较旧的时间戳，
        不应把已经同步过的进度反向拉低。
        """
        self._ensure_loaded()
        with self._lock:
            current = float(self._cursors.get(key, 0.0))
            updated = max(current, float(value))
            self._cursors[key] = updated
            if persist and updated != current:
                self._save_locked()
            return updated

    def since_for(self, key: str) -> float:
        """返回该来源的请求起点：游标回退安全窗口后的值。"""
        return max(0.0, self.get(key) - SAFETY_MARGIN)

    def forget(self, key: str, *, persist: bool = True) -> bool:
        self._ensure_loaded()
        with self._lock:
            existed = self._cursors.pop(key, None) is not None
            if existed and persist:
                self._save_locked()
            return existed

    def reset(self, *, persist: bool = True) -> None:
        """清空全部游标（下一轮回到全量重扫）。"""
        with self._lock:
            self._loaded = True
            self._cursors = {}
            if persist:
                self._save_locked()

    def snapshot(self) -> Dict[str, float]:
        self._ensure_loaded()
        with self._lock:
            return dict(self._cursors)

    def status(self) -> Dict[str, object]:
        """给 CLI / 设置页看的摘要。"""
        self._ensure_loaded()
        with self._lock:
            rows = []
            for key, value in sorted(self._cursors.items()):
                rows.append(
                    {
                        "key": key,
                        "since": value,
                        "since_text": _format_ts(value),
                    }
                )
            return {
                "path": str(self.path),
                "exists": self.path.exists(),
                "count": len(rows),
                "cursors": rows,
            }

    # ---- 落盘 ----

    def _save_locked(self) -> None:
        """原子写：临时文件 + `os.replace`，避免中途中断留下半截 JSON。"""
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": time.time(),
            "cursors": self._cursors,
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


def _format_ts(value: float) -> str:
    if value <= 0:
        return "从未"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def build_cursor_store(explicit: Optional[Path] = None) -> SyncCursorStore:
    """构造并载入游标存储。"""
    store = SyncCursorStore(explicit)
    store.load()
    return store
