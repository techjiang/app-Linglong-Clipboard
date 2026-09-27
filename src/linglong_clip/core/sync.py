"""跨设备同步：共享文件夹与局域网两种模式。

设计取舍：
- **不做中心服务器**。剪切板内容敏感，多数用户只需要「自己几台机器之间同步」，
  走用户已有的同步盘（OneDrive/iCloud/Syncthing）或局域网直连即可，
  既省去账号体系，也不引入第三方留存。
- **落盘格式是「每设备一个日志文件」**。各自只追加自己的记录，
  天然避免并发写同一文件的冲突；读取时合并所有设备的日志。
- **加密可选**（`sync.encrypt`）。开启时用 `cryptography` 的 Fernet，
  密钥来自用户口令 + 设备间共享盐，派生用 PBKDF2。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..utils.logger import get_logger
from .models import ClipboardItem
from .privacy import PrivacyGuard
from .storage import Storage

_log = get_logger("sync")

#: 同步文件里保留的最大记录数，避免日志文件无限增长
MAX_SYNC_ENTRIES = 5_000


@dataclass
class SyncRecord:
    """同步单元：够重建一条 ClipboardItem，且不含本地路径等机器相关字段。"""

    hash: str
    kind: str
    content: str
    preview: str
    source_app: str = ""
    device: str = ""
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    #: 图片等二进制内容不进同步（体积大且隐私风险高）
    payload_b64: str = ""

    def to_json(self) -> Dict[str, object]:
        return {
            "hash": self.hash,
            "kind": self.kind,
            "content": self.content,
            "preview": self.preview,
            "source_app": self.source_app,
            "device": self.device,
            "tags": self.tags,
            "created_at": self.created_at,
            "payload_b64": self.payload_b64,
        }

    @classmethod
    def from_json(cls, data: Dict[str, object]) -> Optional["SyncRecord"]:
        if not isinstance(data, dict) or not data.get("hash"):
            return None
        return cls(
            hash=str(data.get("hash", "")),
            kind=str(data.get("kind", "text")),
            content=str(data.get("content", "")),
            preview=str(data.get("preview", "")),
            source_app=str(data.get("source_app", "")),
            device=str(data.get("device", "")),
            tags=list(data.get("tags") or []),
            created_at=float(data.get("created_at") or time.time()),
            payload_b64=str(data.get("payload_b64", "")),
        )

    def to_item(self) -> ClipboardItem:
        return ClipboardItem(
            kind=self.kind,
            content=self.content,
            preview=self.preview,
            source_app=self.source_app,
            device=self.device,
            tags=list(self.tags),
            hash=self.hash,
            created_at=self.created_at,
            metadata={"synced": True},
        )


class SyncCipher:
    """可选的对称加密封装。

    `cryptography` 缺失时保持明文并在日志里告警一次 ——
    同步功能不应因为缺少可选依赖而完全不可用。
    """

    def __init__(self, enabled: bool, password: str = "", salt: bytes = b"linglong-v1") -> None:
        self.enabled = bool(enabled)
        self._fernet = None
        self._warned = False
        if not self.enabled:
            return
        try:
            import base64
            import hashlib

            from cryptography.fernet import Fernet  # type: ignore

            key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000, dklen=32)
            self._fernet = Fernet(base64.urlsafe_b64encode(key))
        except ImportError:
            self.enabled = False
            self._warned = True
            _log.warning("未安装 cryptography，同步将以明文进行")
        except Exception:  # noqa: BLE001
            self.enabled = False
            _log.exception("初始化同步加密失败，退化为明文")

    def encrypt(self, payload: bytes) -> bytes:
        if not self.enabled or self._fernet is None:
            return payload
        return self._fernet.encrypt(payload)

    def decrypt(self, payload: bytes) -> Optional[bytes]:
        if not self.enabled or self._fernet is None:
            return payload
        try:
            return self._fernet.decrypt(payload)
        except Exception:  # noqa: BLE001 - 密钥不匹配或数据损坏都跳过这条
            return None


class FolderSync:
    """基于共享文件夹的同步（OneDrive / iCloud / Syncthing / NFS 均可）。"""

    def __init__(
        self,
        storage: Storage,
        folder: Path,
        *,
        device_name: str = "device",
        cipher: Optional[SyncCipher] = None,
        guard: Optional[PrivacyGuard] = None,
    ) -> None:
        self._storage = storage
        self.folder = Path(folder)
        self.device_name = device_name or socket.gethostname()
        self._cipher = cipher or SyncCipher(False)
        self._guard = guard
        self._lock = threading.RLock()

    @property
    def journal_path(self) -> Path:
        safe_name = "".join(
            ch if ch.isalnum() or ch in "-_" else "_" for ch in self.device_name
        )
        return self.folder / f"{safe_name}.llsync"

    def is_ready(self) -> bool:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        return self.folder.is_dir()

    # ---- 导出 ----

    def export(self, limit: int = 500, include_images: bool = False) -> int:
        """把本地最近的记录追加到自己的日志文件。"""
        if not self.is_ready():
            _log.warning("同步目录不可用：%s", self.folder)
            return 0

        already = self._local_hashes()
        records: List[SyncRecord] = []
        for item in self._storage.list(limit=limit):
            if not item.hash or item.hash in already:
                continue
            if item.sensitive and not self._allow_sensitive():
                continue
            if item.kind == "image" and not include_images:
                continue
            records.append(
                SyncRecord(
                    hash=item.hash,
                    kind=item.kind,
                    content=item.content,
                    preview=item.preview,
                    source_app=item.source_app,
                    device=self.device_name,
                    tags=list(item.tags),
                    created_at=item.created_at,
                )
            )

        if not records:
            return 0

        payload = "\n".join(
            json.dumps(record.to_json(), ensure_ascii=False) for record in records
        ).encode("utf-8")
        blob = self._cipher.encrypt(payload)

        with self._lock:
            with open(self.journal_path, "ab") as handle:
                handle.write(len(blob).to_bytes(4, "big"))
                handle.write(blob)
        _log.info("导出 %d 条记录到 %s", len(records), self.journal_path.name)
        return len(records)

    def _local_hashes(self) -> set:
        """已在本地日志里出现过的 hash，避免重复导出。"""
        hashes: set = set()
        for record in self._read_journal(self.journal_path):
            hashes.add(record.hash)
        return hashes

    def _allow_sensitive(self) -> bool:
        return False

    # ---- 导入 ----

    def import_all(self) -> int:
        """读取所有设备的日志，把本地没有的记录补进来。"""
        if not self.is_ready():
            return 0
        imported = 0
        seen_local = {item.hash for item in self._storage.list(limit=MAX_SYNC_ENTRIES)}
        for path in sorted(self.folder.glob("*.llsync")):
            if path.name == self.journal_path.name:
                # 自己的日志也读，用于补齐「另一台机器先导入过」的场景
                pass
            for record in self._read_journal(path):
                if record.hash in seen_local:
                    continue
                if record.device == self.device_name:
                    continue
                item = record.to_item()
                if self._guard is not None:
                    verdict = self._guard.inspect(item)
                    if not verdict.allowed or (verdict.sensitive and not self._allow_sensitive()):
                        continue
                self._storage.add(item)
                seen_local.add(record.hash)
                imported += 1
        if imported:
            _log.info("从同步目录导入 %d 条记录", imported)
        return imported

    def _read_journal(self, path: Path) -> Iterable[SyncRecord]:
        """按「4 字节长度 + 载荷」的分块格式读取，单块损坏不影响其余。"""
        if not path.exists():
            return []
        records: List[SyncRecord] = []
        try:
            raw = path.read_bytes()
        except OSError as exc:
            _log.debug("读取同步日志失败 %s: %s", path, exc)
            return records

        offset = 0
        total = len(raw)
        while offset + 4 <= total:
            length = int.from_bytes(raw[offset : offset + 4], "big")
            offset += 4
            if length <= 0 or offset + length > total:
                break
            blob = raw[offset : offset + length]
            offset += length
            plain = self._cipher.decrypt(blob)
            if plain is None:
                continue
            for line in plain.split(b"\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = SyncRecord.from_json(json.loads(line.decode("utf-8")))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if record is not None:
                    records.append(record)
        return records

    # ---- 维护 ----

    def compact(self, keep: int = MAX_SYNC_ENTRIES) -> bool:
        """把自己日志里超出上限的旧记录裁掉。"""
        records = list(self._read_journal(self.journal_path))
        if len(records) <= keep:
            return False
        records.sort(key=lambda record: record.created_at)
        records = records[-keep:]
        payload = "\n".join(
            json.dumps(record.to_json(), ensure_ascii=False) for record in records
        ).encode("utf-8")
        blob = self._cipher.encrypt(payload)
        with self._lock:
            tmp = self.journal_path.with_suffix(".tmp")
            with open(tmp, "wb") as handle:
                handle.write(len(blob).to_bytes(4, "big"))
                handle.write(blob)
            tmp.replace(self.journal_path)
        return True

    def status(self) -> Dict[str, object]:
        files = sorted(self.folder.glob("*.llsync")) if self.folder.exists() else []
        return {
            "folder": str(self.folder),
            "ready": self.is_ready(),
            "devices": [path.stem for path in files],
            "encrypted": self._cipher.enabled,
            "device": self.device_name,
        }
