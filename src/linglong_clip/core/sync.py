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
from .models import PINNED_BY_KEY, ClipboardItem
from .privacy import PrivacyGuard
from .storage import Storage
from .sync_cursor import SyncCursorStore
from .tombstones import Tombstone, TombstoneStore, contains_hash

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
    #: 用户显式标记的「别动它 / 常看它」。此前只传内容与 tags，
    #: 这两个标记跨设备就丢了 —— 详见 ``FlagSnapshot`` 的说明。
    pinned: bool = False
    favorite: bool = False
    #: 固定标记是谁设的。收到删除标记时要用它判断「这个固定是否已失效」，
    #: 详见 ``models.PINNED_BY_KEY`` 的说明。
    pinned_by: str = ""
    created_at: float = field(default_factory=time.time)
    #: 图片等二进制内容不进同步（体积大且隐私风险高）
    payload_b64: str = ""
    #: 非 0 表示这是一条**删除标记**而非内容记录，值为删除时刻。
    #: 用同一根日志承载删除，是因为「传了什么」和「删了什么」必须在
    #: 同一条有序通道上，否则顺序颠倒会让删除被后面的新增覆盖掉。
    deleted_at: float = 0.0

    @property
    def is_tombstone(self) -> bool:
        return self.deleted_at > 0

    def to_json(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
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
        # 只有为真时才写：老版本读到缺字段即视为 False，向后兼容
        if self.pinned:
            payload["pinned"] = True
            if self.pinned_by:
                payload["pinned_by"] = self.pinned_by
        if self.favorite:
            payload["favorite"] = True
        if self.deleted_at > 0:
            payload["deleted_at"] = self.deleted_at
        return payload

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
            pinned=bool(data.get("pinned", False)),
            favorite=bool(data.get("favorite", False)),
            pinned_by=str(data.get("pinned_by", "") or ""),
            created_at=float(data.get("created_at") or time.time()),
            payload_b64=str(data.get("payload_b64", "")),
            deleted_at=float(data.get("deleted_at") or 0.0),
        )

    def to_item(self) -> ClipboardItem:
        return ClipboardItem(
            kind=self.kind,
            content=self.content,
            preview=self.preview,
            source_app=self.source_app,
            device=self.device,
            tags=list(self.tags),
            pinned=self.pinned,
            favorite=self.favorite,
            hash=self.hash,
            created_at=self.created_at,
            metadata=(
                {"synced": True, PINNED_BY_KEY: self.pinned_by}
                if self.pinned and self.pinned_by
                else {"synced": True}
            ),
        )

    def flags(self) -> "FlagSnapshot":
        """本条记录携带的用户标记（含固定的来源设备）。"""
        return FlagSnapshot(
            pinned=self.pinned,
            favorite=self.favorite,
            pinned_by=self.pinned_by,
        )


@dataclass
class FlagSnapshot:
    """一条记录上的用户标记（固定 / 收藏）。

    为什么要单独成类型：这两个标记是**用户显式动作**的结果，
    与内容本身同等重要 —— 内容丢了可以重新复制，标记丢了用户不知道，
    只会发现「我明明收藏过，怎么另一台机器上没了」。

    为什么同步此前会丢掉它们：导出侧只挑了内容相关字段
    （``content`` / ``preview`` / ``tags``），标记不在其中；而导入侧
    又按指纹跳过「已见过」的记录，于是**标记的后续变化永远传不出去**。
    结果就是跨设备单向、且只对首次同步生效。
    """

    pinned: bool = False
    favorite: bool = False
    #: 固定标记的来源设备（空串表示本机设的 / 未知）。取并集时跟着一起并，
    #: 否则「谁设的固定」会丢，删除保护就无法判断是否已失效。
    pinned_by: str = ""

    def any(self) -> bool:
        return self.pinned or self.favorite

    def merge(self, other: "FlagSnapshot") -> "FlagSnapshot":
        """合并两份标记，取并集。

        取并集而不是取「最后写入者」：这两个标记都是**加法式**的用户意图
        （「我要它留着」/「我要常看它」），没有对应的主动取消动作能可靠跨设备表达 ——
        因为无法区分「对端取消过」和「对端从未设过」。取并集是唯一不会
        把用户的显式标记悄悄弄丢的选择。想真正取消，需要的是单独的
        取消传播通道（与墓碑同性质），不是在这里猜。

        `pinned_by` 跟固定标记一起合并，规则是**优先保留 `self` 的来源**：

        - `self` 已固定 → 用 `self.pinned_by`（空串也是有效值，表示「本机设的」）。
          这条很关键：本机用户亲手固定的记录，来源必须保持空串，才不会被
          对端带来的来源顶掉 —— 一旦被顶成对端设备名，对端反悔删除时会
          连着本机这份一起删掉，而本机用户并没有说要删。
        - `self` 没固定、`other` 固定了 → 用 `other.pinned_by`（谁设的答案在那边）。
        """
        pinned = self.pinned or other.pinned
        if not pinned:
            pinned_by = ""
        elif self.pinned:
            pinned_by = self.pinned_by
        else:
            pinned_by = other.pinned_by
        return FlagSnapshot(
            pinned=pinned,
            favorite=self.favorite or other.favorite,
            pinned_by=pinned_by,
        )




@dataclass
class ImportOutcome:
    """一次日志导入的明细，供调用方统计与日志。"""

    imported: int = 0
    removed: int = 0
    tombstones: int = 0
    #: 因对端带来了本地没有的标记而被更新的条数
    flags_updated: int = 0


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
        cursors: Optional[SyncCursorStore] = None,
        tombstones: Optional[TombstoneStore] = None,
    ) -> None:
        self._storage = storage
        self.folder = Path(folder)
        self.device_name = device_name or socket.gethostname()
        self._cipher = cipher or SyncCipher(False)
        self._guard = guard
        self.cursors = cursors if cursors is not None else SyncCursorStore()
        self.tombstones = tombstones if tombstones is not None else TombstoneStore()
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
        sent_flags = self._local_flag_state()
        records: List[SyncRecord] = []
        for item in self._storage.list(limit=limit):
            if item.sensitive and not self._allow_sensitive():
                continue
            if item.kind == "image" and not include_images:
                continue
            if not item.hash:
                continue

            # 固定的来源要**以「发送方」为基准**落进日志：本机设的固定（来源为空）
            # 在这里补上自己的设备名，对端才会知道「这条固定是这台设备设的」。
            # 不补的话，来源为空在对端语义里等于「对端本机设的」，于是对端
            # 收到删除标记时会误判成「自己的固定意图」而拒绝删除 —— 这正是
            # 「删了的东西在另一台机器上复活」的成因。来源非空的原样转发，
            # 保持「谁设的」这个事实一路可追溯。
            pinned_by = item.pinned_by or self.device_name
            flags = FlagSnapshot(
                pinned=item.pinned,
                favorite=item.favorite,
                pinned_by=pinned_by,
            )
            if contains_hash(already, item.hash):
                # 内容已经发过了。此前这里直接跳过，于是**标记的后续变化永远传不出去** ——
                # 用户在设备 B 上收藏了某条，A 端永远不知道。
                # 只有「本地标记比上次发出去的多」时才补发一条，避免每轮重传全部历史。
                #
                # 比的是布尔标记本身，不含 `pinned_by`：来源变化（比如先在本机固定、
                # 后来对端也固定）不该单独触发重传，它只有在标记状态真的变了的时候
                # 才有意义。否则每轮同步都会因为来源来回覆盖而反复补发。
                previous = sent_flags.get(item.hash, FlagSnapshot())
                if (
                    flags.merge(previous).pinned == previous.pinned
                    and flags.merge(previous).favorite == previous.favorite
                ):
                    # 对端已知的标记已经覆盖了本地这些，没有新东西要传
                    continue
                _log.debug("补发标记变化：%s", item.hash)
            records.append(
                SyncRecord(
                    hash=item.hash,
                    kind=item.kind,
                    content=item.content,
                    preview=item.preview,
                    source_app=item.source_app,
                    device=self.device_name,
                    tags=list(item.tags),
                    pinned=item.pinned,
                    favorite=item.favorite,
                    pinned_by=pinned_by,
                    created_at=item.created_at,
                )
            )

        # 删除标记接在新增之后：同一份日志内「先有后无」的顺序，
        # 才能让对端按读取顺序得到正确结论（而不是反过来把删除当成新增）。
        #
        # 注意这里**不能**按 `already` 过滤。`already` 是「内容已导出过」的集合，
        # 而「导出过内容」恰恰是删除标记要传达的前提：正因为对端当年收到过这一条，
        # 才需要被告知「它已经删了」。按 `already` 过滤会把墓碑全拦掉，
        # 删除也就永远不会传播 —— 这个 bug 有单独用例守着。
        pending = list(self.tombstones.pending())
        records.extend(
            SyncRecord(
                hash=entry.hash,
                kind="tombstone",
                content="",
                preview="",
                device=self.device_name,
                created_at=entry.deleted_at,
                deleted_at=entry.deleted_at,
            )
            for entry in pending
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
        # 写入成功后才确认送达：写失败时异常向上抛，墓碑保持待发状态，下轮重试。
        # 确认之后不再复述，否则每轮同步都要重传一遍历史删除。
        if pending:
            self._ack_sent_tombstones()
        _log.info("导出 %d 条记录到 %s", len(records), self.journal_path.name)
        return len(records)

    def _local_hashes(self) -> set:
        """已在本地日志里出现过的 hash，避免重复导出。"""
        hashes: set = set()
        for record in self._read_journal(self.journal_path):
            hashes.add(record.hash)
        return hashes

    def _local_flag_state(self) -> Dict[str, FlagSnapshot]:
        """日志里每个指纹**已经发出去过**的标记。

        用于判断「本地标记是不是比对端已知的更多了」。同一指纹可能有多条
        记录（后续补发的标记变化），取并集 —— 只要发出去过，就不必重发。
        """
        state: Dict[str, FlagSnapshot] = {}
        for record in self._read_journal(self.journal_path):
            if record.is_tombstone:
                continue
            current = state.get(record.hash, FlagSnapshot())
            state[record.hash] = current.merge(record.flags())
        return state

    def _allow_sensitive(self) -> bool:
        return False

    # ---- 导入 ----

    def cursor_key(self, path: Path) -> str:
        """每个对端日志文件对应一个游标键（文件名即对端设备标识）。"""
        return SyncCursorStore.key_for("folder", path.name)

    def import_all(self) -> int:
        """读取所有设备的日志，把本地没有的记录补进来。"""
        return self.import_all_detailed().imported

    def import_all_detailed(self) -> "ImportOutcome":
        """同 `import_all`，但返回明细（含只更新标记的条数）。

        每份对端日志各自维护增量游标：只解析晚于游标的记录。
        游标未推进到位（例如首次同步）时退化为全量扫描，行为与旧版一致。

        保留 `import_all` 的整数返回是刻意的：它是既有公开形状，
        改签名会波及所有调用方与用例；而 CLI 需要看到「标记更新数」——
        否则「导入 0 条」既可能是「没什么可同步」，也可能是「只更新了标记」。
        """
        total = ImportOutcome()
        if not self.is_ready():
            return total
        seen_local = {item.hash for item in self._storage.list(limit=MAX_SYNC_ENTRIES)}
        acked = False
        for path in sorted(self.folder.glob("*.llsync")):
            # 自己的日志也读，用于补齐「另一台机器先导入过」的场景
            outcome = self._import_journal(path, seen_local)
            total.imported += outcome.imported
            total.removed += outcome.removed
            total.tombstones += outcome.tombstones
            total.flags_updated += outcome.flags_updated
            acked = acked or bool(outcome.tombstones)
        if acked:
            # 对端确实接收过删除标记：把本机待发的那些标为已送达，之后不再复述。
            # 放在循环外统一做一次，避免每读一份日志就重写一遍墓碑文件。
            self._ack_sent_tombstones()
        if total.imported:
            _log.info("从同步目录导入 %d 条记录", total.imported)
        return total

    def _ack_sent_tombstones(self) -> int:
        """把「已经写进自己日志的」待发墓碑标为已送达。

        判定依据是本地日志里确实存在这些指纹 —— 只有真的写进去了才敢确认，
        否则（磁盘满、权限不足导致写入失败）下轮还得重发。
        """
        already = self._local_hashes()
        delivered = [
            entry.hash
            for entry in self.tombstones.pending()
            if contains_hash(already, entry.hash)
        ]
        if not delivered:
            return 0
        return self.tombstones.ack(delivered)

    def _import_journal(self, path: Path, seen_local: set) -> "ImportOutcome":
        """导入单份对端日志，并推进其游标。返回本轮统计。"""
        key = self.cursor_key(path)
        since = self.cursors.since_for(key)
        outcome = ImportOutcome()
        newest = since
        records = [
            record
            for record in self._read_journal(path)
            if float(getattr(record, "created_at", 0.0) or 0.0) > since
        ]

        # 同一份日志里被判过删除的指纹：后面的内容记录一律不再写入。
        # 顺序至关重要 —— 日志是追加式的，「先有内容、后有删除」；
        # 若反过来让内容记录覆盖删除，用户删掉的东西会被同一条日志复活。
        # 游标的安全窗口（回退 1 秒）会让这两条落在同一批里，
        # 所以这个「批内去重」不是可选项。
        deleted_in_batch: set = set()
        for record in records:
            newest = max(newest, float(getattr(record, "created_at", 0.0) or 0.0))
            if record.is_tombstone:
                # 删除标记只认别的设备发出的：自己写进日志的那条不算
                if record.device == self.device_name:
                    continue
                outcome.tombstones += 1
                deleted_in_batch.add(record.hash)
                if self._apply_tombstone(record, seen_local):
                    outcome.removed += 1

        # 本地已经记过墓碑的指纹：不管来源是哪份日志（**包括自己的**），
        # 都不能再写回来。少了这一条，一台设备会把自己删掉的记录从自己的
        # 日志里复活 —— 只要它此前往日志里写过这条的标记变化就够了。
        # `deleted_in_batch` 只覆盖「同一批里先有删除」，管不到「上一批删过、
        # 这一批日志里又读到」这种情况，两者缺一不可。
        deleted_in_store = self.tombstones.all_hashes()

        for record in records:
            if record.is_tombstone:
                continue
            if contains_hash(deleted_in_batch, record.hash):
                # 同一批里它已被删除，别再写回来
                continue
            if contains_hash(deleted_in_store, record.hash):
                _log.debug("跳过已删除的记录（本地有墓碑）：%s", record.hash)
                continue
            if contains_hash(seen_local, record.hash):
                # 内容本地已经有了。此前这里直接跳过，于是**对端后加的固定/收藏
                # 永远传不过来**（导出侧虽补发了标记变化，导入侧却不看）。
                # 标记是用户的显式动作，内容重复不代表这个动作可以丢。
                if apply_flags(self._storage, record, seen_local):
                    outcome.flags_updated += 1
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
            apply_flags(self._storage, record, seen_local)
            outcome.imported += 1
        # 只有在完整读完后才推进游标：解析中途出错不该跳过剩余记录
        if newest > since:
            self.cursors.advance(key, newest)
        return outcome

    def _apply_tombstone(self, record: SyncRecord, seen_local: Optional[set] = None) -> bool:
        """收到对端的删除标记：记下它，并清掉本地同一指纹的记录。

        三条约束，顺序也就是优先级：

        1. **指纹已在本地墓碑里** → 幂等，直接返回。重复收到同一条删除不该有任何副作用。
        2. **超出保留期** → 只记指纹、不动本地记录。对端离线太久，它那边删掉的
           东西这边早已「活」了很久，此时再删就是在丢本机自己的数据。
        3. **固定项** → 只记指纹、不动记录。用户显式标了「别动它」，
           删除传播不该越过这条线 —— 但**只在本机的固定意图仍然有效时**。
           若这个固定标记正是「正在删除的那台设备」设的（`pinned_by` 相同），
           说明发起方自己已经改主意删掉了，这个固定是它留下的过期残留，
           删除照做。详见 ``pin_is_stale``。

        注意判定用的是 `record.deleted_at`（对端的删除时刻），不是收到时刻 ——
        否则一台设备离线很久再上线时，本该过期的删除会被当成刚发生的。
        """
        if self.tombstones.has(record.hash):
            return False
        self.tombstones.record(record.hash, deleted_at=record.deleted_at)
        if seen_local is not None:
            seen_local.add(record.hash)

        if record.deleted_at > 0 and self.tombstones.is_expired(
            Tombstone(hash=record.hash, deleted_at=record.deleted_at)
        ):
            return False

        existing = self._storage.find_by_hash(record.hash)
        if existing is None:
            return False
        if existing.pinned and not pin_is_stale(existing, record):
            # 固定项是用户显式标记「别动它」的，删除传播不该越过这条线
            _log.info("保留固定项，跳过对端删除标记：%s", record.hash)
            return False
        if existing.pinned:
            _log.info(
                "固定标记来自发起删除的设备 %s，已失效，按删除处理：%s",
                record.device,
                record.hash,
            )
        self._storage.delete(existing.id)
        return True

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
        cursors = self.cursors.snapshot()
        devices = []
        for path in files:
            key = self.cursor_key(path)
            devices.append(
                {
                    "device": path.stem,
                    "journal": path.name,
                    "since": cursors.get(key, 0.0),
                    "synced": cursors.get(key, 0.0) > 0,
                }
            )
        return {
            "folder": str(self.folder),
            "ready": self.is_ready(),
            "devices": [path.stem for path in files],
            "device_details": devices,
            "encrypted": self._cipher.enabled,
            "device": self.device_name,
            "cursor_file": str(self.cursors.path),
        }


def pin_is_stale(item: ClipboardItem, record: SyncRecord) -> bool:
    """本地这条的固定标记，是不是「正在删除它的那台设备」留下的过期残留。

    为什么需要这个判断：固定标记会阻断删除传播，理由是「用户显式说了别动它」。
    但标记同步取并集，任何一台设备上的固定都会传播到所有设备 —— 包括
    发起固定的那台自己。于是出现了这条路径：

        设备 B 固定了某条  →  并集把固定传给了 A（A 上也变成已固定）
        用户在 B 上删掉它  →  B 发出删除标记
        A 收到标记，看到自己是「已固定」→ 拒绝删除
        A 把它重新导出     →  B 上又被复活

    用户删了的东西在自己另一台机器上活过来，且永远删不掉 —— 因为并集语义下
    这个固定标记没有任何办法被清除。

    判据就是**固定的来源设备**：`pinned_by` 等于发起删除的设备时，说明
    这个固定正是它设的、而它现在已经删掉了，固定意图不复存在，删除应当生效。

    两类要放行（不能误判成过期）的情形：

    - `pinned_by` 为空：固定是本机设的（老版本数据也没有这个字段），
      本机用户的「别动它」意图仍然有效，必须保留。
    - `pinned_by` 与发起删除的设备不同：固定是第三台设备设的，
      它的用户并没有改主意，删除传播不该越过它。
    """
    if not item.pinned:
        return False
    source = item.pinned_by
    if not source:
        return False
    return source == record.device


def apply_flags(
    storage: Storage,
    record: SyncRecord,
    seen_local: Optional[set] = None,
) -> bool:
    """把对端记录上的用户标记合并到本地同指纹的记录上。

    两条同步模式共用一个实现：``FolderSync._import_journal`` 与
    ``LanSyncClient.import_records`` 若各写一套，迟早分叉
    （此前的墓碑处理就踩过这个坑，所以那次专门抽了同一个规则）。

    合并方向是**取并集**（见 ``FlagSnapshot.merge``）：固定与收藏都是
    加法式的用户意图，取并集是唯一不会把用户的显式标记悄悄弄丢的选择。

    返回是否真的改动了本地数据 —— 调用方用它统计「有多少条只更新了标记」，
    这个数字要能被看到，否则又是「数据变了但没人知道」。
    """
    if record.is_tombstone or not record.flags().any():
        return False
    existing = storage.find_by_hash(record.hash)
    if existing is None:
        return False

    local = FlagSnapshot(
        pinned=existing.pinned,
        favorite=existing.favorite,
        pinned_by=existing.pinned_by,
    )
    remote = record.flags()
    # `merge` 的 `other` 语义是「后到的一份」，对端记录就是后到的
    merged = local.merge(remote)
    if (
        merged.pinned == existing.pinned
        and merged.favorite == existing.favorite
        and merged.pinned_by == existing.pinned_by
    ):
        return False

    metadata = dict(existing.metadata)
    if merged.pinned and merged.pinned_by:
        metadata[PINNED_BY_KEY] = merged.pinned_by
    else:
        # 没有固定标记时不留来源，避免旧来源在后续取消/重设之间串味
        metadata.pop(PINNED_BY_KEY, None)
    storage.update_fields(
        existing.id,
        pinned=merged.pinned,
        favorite=merged.favorite,
        metadata=metadata,
    )
    if seen_local is not None:
        seen_local.add(record.hash)
    return True
