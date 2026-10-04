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
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..utils.logger import get_logger
from .models import IMAGE_DIGEST_KEY, PINNED_BY_KEY, ClipboardItem
from . import image_transfer
from .privacy import PrivacyGuard
from .storage import Storage
from ..utils import paths
from . import flag_changes as flag_changes_module
from . import sync_cursor
from .sync_cursor import SyncCursorStore, build_cursor_store
from .tombstones import (
    TOMBSTONE_FILENAME,
    Tombstone,
    TombstoneStore,
    build_tombstone_store,
    contains_hash,
)
from .flag_changes import (
    FLAG_FAVORITE,
    FLAG_PINNED,
    FlagChange,
    FlagChangeStore,
    build_flag_change_store,
    resolve_flag,
)

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
    #: 发送方库内的自增 id，**仅用于局域网翻页**，不参与内容判重。
    #:
    #: 局域网用 `(created_at, seq)` 做翻页游标：同一时刻的记录多到塞满一页时，
    #: 只记时间戳的游标在首页之后就推不动了 —— 下一页请求 `created_at > T`
    #: 返回空，剩下的记录不是「更旧」而是**一样新**，于是被永久跳过。
    #: `seq` 就是那个能让翻页前进的次级键。
    #:
    #: 共享文件夹模式的游标**不用它** —— 那边用「读到日志第几条」。
    #: 日志是追加式的，条目序号天然单调，且与记录里的任何字段无关；
    #: 而 `seq` 是发送方的库内编号，同一个 id 会被「内容记录」和它的
    #: 「标记补发」复用，拿它当位置判据会把后续补发全过滤掉。
    seq: int = 0
    #: 图片等二进制内容不进同步（体积大且隐私风险高）
    payload_b64: str = ""
    #: 这条记录引用的图片摘要（文件名去掉扩展名）。**只传「有这张图」和
    #: 「它是哪一张」，不传字节** —— 接收方据此知道该去对端按需拉取。
    #:
    #: 为什么不干脆把字节一起传：同步目录会因此膨胀到几百 MB，而用户选
    #: 同步目录时想的是「放个笔记本」；何况大部分图你根本不会在第二台
    #: 机器上打开，全传是拿带宽和磁盘换一个多数时候用不上的保证。
    #: 详见 `core/image_transfer.py`。
    image_digest: str = ""
    #: 非 0 表示这是一条**删除标记**而非内容记录，值为删除时刻。
    #: 用同一根日志承载删除，是因为「传了什么」和「删了什么」必须在
    #: 同一条有序通道上，否则顺序颠倒会让删除被后面的新增覆盖掉。
    deleted_at: float = 0.0
    #: 标记变更记录：非空表示这是一条「取消/设置固定或收藏」的事件，
    #: 值为标记名（``pinned`` / ``favorite``）。与墓碑同理走同一条有序通道，
    #: 否则「取消」会被同批里的内容记录盖回去。
    flag_change: str = ""
    #: 该变更的目标值（True=设置，False=取消）。
    flag_value: bool = False

    @property
    def is_tombstone(self) -> bool:
        return self.deleted_at > 0

    @property
    def is_flag_change(self) -> bool:
        return bool(self.flag_change)

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
        # 只有真有图时才写：老版本读到缺字段即视为「没有图」，
        # 与它此前的行为（图片不参与同步）完全一致，向后兼容。
        if self.image_digest:
            payload["image_digest"] = self.image_digest
        # 只有为真时才写：老版本读到缺字段即视为 False，向后兼容
        if self.pinned:
            payload["pinned"] = True
            if self.pinned_by:
                payload["pinned_by"] = self.pinned_by
        if self.favorite:
            payload["favorite"] = True
        if self.deleted_at > 0:
            payload["deleted_at"] = self.deleted_at
        if self.flag_change:
            payload["flag_change"] = self.flag_change
            payload["flag_value"] = bool(self.flag_value)
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
            # 局域网响应会带上它（翻页游标的次级键）；共享日志里没有这个键，
            # 缺省为 0，两种载入路径共用同一份解析。见 `SyncRecord.seq`。
            seq=int(data.get("seq") or 0),
            payload_b64=str(data.get("payload_b64", "")),
            image_digest=str(data.get("image_digest", "") or ""),
            deleted_at=float(data.get("deleted_at") or 0.0),
            flag_change=str(data.get("flag_change", "") or ""),
            flag_value=bool(data.get("flag_value", False)),
        )

    def to_item(self) -> ClipboardItem:
        metadata: Dict[str, object] = {"synced": True}
        if self.pinned and self.pinned_by:
            metadata[PINNED_BY_KEY] = self.pinned_by
        if self.image_digest:
            # 记下「这条记录原本有图、摘要是什么」，但**故意不写 `image_path`**:
            # 本机此刻并没有那份字节，写上一个指向不存在文件的路径，
            # 会让界面以为图就在那儿，点开时才发现是空的。
            # 摘要留给 `--sync-images` 去对端按需取，取回来才补上 `image_path`。
            metadata[IMAGE_DIGEST_KEY] = self.image_digest
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
            metadata=metadata,
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
    #: 因对端带来了本机不知道的标记变更（含取消）而入账的条数
    flag_changes: int = 0


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


def _sidecar_dir(storage: Storage) -> Path:
    """这台设备的辅助文件（游标 / 墓碑 / 标记变更）该放哪。

    放在**数据库所在目录**，与 `images/` 同级 —— 它们是「这台设备的历史」
    的一部分，不是全局共享的配置。

    此前它们默认落在 `paths.data_dir()`：单机看起来没问题，但**同一个进程里
    两台设备**（测试、以及任何在一台机器上模拟两台的场景）会共用同一份文件，
    于是 A 的动作被当成 B 的、标记互相抵消。判据也很直白 ——
    辅助文件的归属应当与它描述的数据一致，而那份数据就在 `storage.path` 旁边。
    """
    try:
        return Path(storage.path).parent  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        return paths.data_dir()


def _default_cursors(storage: Storage) -> SyncCursorStore:
    return build_cursor_store(_sidecar_dir(storage) / sync_cursor.CURSOR_FILENAME)


def _default_tombstones(storage: Storage) -> TombstoneStore:
    return build_tombstone_store(_sidecar_dir(storage) / TOMBSTONE_FILENAME)


def _default_flag_changes(storage: Storage) -> FlagChangeStore:
    return build_flag_change_store(
        _sidecar_dir(storage) / flag_changes_module.FLAG_CHANGE_FILENAME
    )


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
        flag_changes: Optional[FlagChangeStore] = None,
    ) -> None:
        self._storage = storage
        self.folder = Path(folder)
        self.device_name = device_name or socket.gethostname()
        self._cipher = cipher or SyncCipher(False)
        self._guard = guard
        self.cursors = cursors if cursors is not None else _default_cursors(storage)
        self.tombstones = (
            tombstones if tombstones is not None else _default_tombstones(storage)
        )
        #: 标记变更（取消固定/取消收藏）的传播通道。与墓碑并列，
        #: 见 `core/flag_changes.py` 的说明。
        self.flag_changes = (
            flag_changes
            if flag_changes is not None
            else _default_flag_changes(storage)
        )
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

    def export(self, limit: int = 500, include_images: bool = True) -> int:
        """把本地尚未导出过的记录追加到自己的日志文件。

        `include_images` 现在默认**开启**，但导出的只是图片的**摘要**，
        不是字节。这是本轮的一个行为变更，理由如下：

        此前 `include_images=False` 意味着图片记录整条不进同步，
        于是对端列表里连「这里曾经有一张图」都不知道 —— 跨设备同步在
        截图这类最常用的内容上等于失效。而打开它就整条传字节，
        体积代价又大到没人愿意付。两难来自「元数据」和「字节」被绑在了一起。

        现在拆开：记录（含摘要）一律同步，代价是几十字节；字节走
        `core/image_transfer.py` 的按需拉取，用户真要看那张图时才发生。
        `include_images=False` 仍然保留「这些图别传出去」的语义供显式使用。

        `limit` 是**每页扫描批量**，不是「最多导出多少条」—— 遍历会一直走到
        历史末尾。这点与旧版不同，也是本轮修掉的一个静默丢数据的缺陷：

        旧实现是 `self._storage.list(limit=limit)`，而 `list()` 只返回界面排序
        下的**前 limit 条**。于是设备上历史超过 500 条时，第 501 条起的老记录
        永远进不了日志。对端游标为 0（新设备首同步）时只会拿到最新的 500 条，
        更老的**永久缺失**：没有报错、没有提示，`--sync-status` 也看不出异常，
        因为「导出了 500 条」本身就是它以为的全部工作。

        更糟的是它会自我固化：`_local_hashes()` 把那 500 条记为「已发送」，
        下一轮仍然是「取最新 500 条 → 全部已发送 → 一条都不导出」，
        于是那 300 条一辈子都在视野之外。加多少轮同步都不会补上。

        改成按 `id DESC` 遍历整库（`Storage.iter_all`），不随界面排序漂移。
        """
        if not self.is_ready():
            _log.warning("同步目录不可用：%s", self.folder)
            return 0

        already = self._local_hashes()
        sent_flags = self._local_flag_state()
        records: List[SyncRecord] = []
        for item in self._storage.iter_all(batch=limit):
            if item.sensitive and not self._allow_sensitive():
                continue
            if item.kind == "image" and not include_images:
                continue
            # 没有指纹的记录不参与同步比对，必须**在这里**跳过：
            # 后面所有判断（`already` 比对、标记补发）都以指纹为键，
            # 让空指纹走进去等于依赖集合里恰好没有空串，属于侥幸。
            if not item.hash:
                continue
            # 有图就带上摘要（**不带字节**）。`image` 类型的记录本来就有，
            # 但别的类型也可能带图（富文本里贴的图），所以按 `image_path`
            # 判断而不是按 `kind` —— 只看 kind 会漏掉后者，
            # 表现就是「有图的那条记录同步过去了，但图取不到且没人知道」。
            image_digest = image_transfer.digest_of_item(item)
            # 未经内容判定的图不报摘要：图片记录的 `content` 是文件名，
            # 隐私判定对它天然失效，只有 OCR 结果能穿透进去。
            # OCR 没跑过时我们**没有任何依据**说这张图不敏感，
            # 所以宁可不提它 —— 提了就等于告诉对端「这里有张图可以来取」。
            # 详见 `image_transfer.privacy_inspectable`。
            if image_digest and not image_transfer.privacy_inspectable(item):
                _log.debug("图片未经内容判定（无 OCR 结果），不报摘要：%s", image_digest)
                image_digest = ""

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
                    image_digest=image_digest,
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

        # 标记变更接在删除之后：两者都是「事件」而非「内容」。
        # 与墓碑一样**不能**按 `already` 过滤 —— 取消要传达的前提正是
        # 「对端当年收到过这个标记」，按「内容已导出」过滤会把取消全拦掉。
        # 变更的 `device` 在这里补上自己的设备名：本机动作在存储里是空串，
        # 而对端必须知道「是谁取消的」才能按设备取最新意图。
        pending_changes = list(self.flag_changes.pending())
        records.extend(
            SyncRecord(
                hash=change.hash,
                kind="flag_change",
                content="",
                preview="",
                device=self.device_name,
                created_at=change.at,
                flag_change=change.flag,
                flag_value=change.value,
            )
            for change in pending_changes
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
        # 写入成功后才确认送达：写失败时异常向上抛，墓碑与变更保持待发状态，
        # 下轮重试。确认之后不再复述，否则每轮同步都要重传一遍历史事件。
        #
        # 「已发过」同时落一份到旁路文件：`compact()` 裁掉旧日志之后，
        # 只看日志会把裁掉的当成「没发过」而重发 —— 而位置改成
        # 「读到第几条」之后，重发的旧记录追加在末尾，对端位置已推过去，
        # 于是永远读不到（见 `_local_hashes` 的说明）。
        content_hashes = [
            record.hash
            for record in records
            if not record.is_tombstone and not record.is_flag_change
        ]
        if content_hashes:
            self._remember_sent(content_hashes)
        if pending:
            self._ack_sent_tombstones()
        if pending_changes:
            self._ack_sent_flag_changes(pending_changes)
        _log.info("导出 %d 条记录到 %s", len(records), self.journal_path.name)
        return len(records)

    def _local_hashes(self) -> set:
        """**已经发出去过**的 hash，避免重复导出。

        判据是「历史上发过」，不是「日志现在还留着」—— `compact()` 会把
        旧条目裁掉，裁掉的记录如果在这里判成「没发过」，就会被重新导出一次。

        这在按时间过滤的年代只是浪费一点带宽（对端靠 hash 去重）；
        而位置改成「读到第几条」之后它会**丢数据**：重发的旧记录追加在日志
        末尾，对端的位置却已经推过去了，于是这条永远读不到。
        而 `compact()` 是每轮同步都会调的，所以这不是边角情形。
        """
        hashes: set = set()
        for record in self._read_journal(self.journal_path):
            hashes.add(record.hash)
        # 裁掉的那些也算「发过」：落盘记录与日志取并集。
        hashes |= self._sent_hashes()
        return hashes

    # ---- 「发过哪些」的落盘记录 ----

    @property
    def _sent_ledger_path(self) -> Path:
        """本机「已发过哪些」的账本。

        放在**数据目录**（与游标、墓碑同级），不放共享目录：它是本机的私有
        记账，跟着同步盘跑到别的机器上毫无意义，还会给对方留下一个
        看不懂的文件。共享目录里只该有 `*.llsync` 这些对端要读的东西。
        """
        return _sidecar_dir(self._storage) / "exported_hashes.json"

    def _sent_hashes(self) -> set:
        """读「已发过的指纹」。文件不存在或损坏都按空集处理。

        它只是**防止重发**的辅助信息：丢了最坏是多发一轮，对端靠 hash 去重。
        所以不做迁移、不抛异常 —— 损坏就当没有。
        """
        path = self._sent_ledger_path
        if not path.exists():
            return set()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            _log.warning("已发送指纹文件无法解析，忽略：%s", exc)
            return set()
        if not isinstance(raw, dict):
            return set()
        hashes = raw.get("hashes")
        return {str(one) for one in hashes} if isinstance(hashes, list) else set()

    def _remember_sent(self, hashes: Iterable[str]) -> None:
        """把这些指纹追加进「已发过」的记录（原子写）。"""
        merged = self._sent_hashes() | {str(one) for one in hashes if one}
        path = self._sent_ledger_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(
                    {"version": 1, "hashes": sorted(merged)},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError as exc:
            # 写不进去只是下次可能重发一轮，不该让导出失败
            _log.warning("已发送指纹写入失败：%s", exc)

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
        # 本地已有的指纹集合，用于「这条对端也发过，不用重复写」。
        # 必须遍历**全部**历史：这里原先写的是 `list(limit=MAX_SYNC_ENTRIES)`，
        # 而 `list()` 只返回界面排序下的前 N 条 —— 本地历史超过 5000 条时，
        # 更老的那些不在集合里，于是对端每次重放这些记录都会被当成新记录写入，
        # 一轮多一份重复。用 `iter_all()` 保证集合完整。
        seen_local = {item.hash for item in self._storage.iter_all()}
        acked = False
        for path in sorted(self.folder.glob("*.llsync")):
            # 自己的日志也读，用于补齐「另一台机器先导入过」的场景
            outcome = self._import_journal(path, seen_local)
            total.imported += outcome.imported
            total.removed += outcome.removed
            total.tombstones += outcome.tombstones
            total.flags_updated += outcome.flags_updated
            total.flag_changes += outcome.flag_changes
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

    def _ack_sent_flag_changes(self, sent: List[FlagChange]) -> int:
        """把「已经写进自己日志的」待发变更标为已送达。

        与墓碑同理：只有真的写进去了才敢确认，否则（磁盘满、权限不足）
        下轮还得重发。这里直接拿写进去的那批对象来确认 ——
        `export()` 已经在写入成功后调用本方法，不必再从日志里反查。
        """
        if not sent:
            return 0
        return self.flag_changes.ack([self.flag_changes._key(change) for change in sent])

    def _import_journal(self, path: Path, seen_local: set) -> "ImportOutcome":
        """导入单份对端日志，并推进其游标。返回本轮统计。

        **位置用「读到第几条」，不用时间戳。** 日志是追加式的，条目序号
        天然单调；而 `created_at` 由写入方给、不保证单调 —— 历史导入、
        存储迁移都会写下过去的时间戳。按时间过滤时，这些新追加的记录
        恰好落在游标之前，于是被永久跳过，而导出侧只看 `hash` 去重、
        照发不误。两台机器都觉得自己是对的。
        """
        key = self.cursor_key(path)
        position = max(0, int(self.cursors.entry_for(key) or 0))
        outcome = ImportOutcome()
        numbered = self._read_journal_numbered(path)

        # 对端把日志裁剪/重写过之后，条目序号会整体前移 —— 此时本地游标
        # 指向的是**别人的**位置。判据：日志比游标短了，说明这条位置已经
        # 不可信，退回全量重扫（hash 会去重，代价只是一轮多读）。
        #
        # 这里必须**显式清零**，不能只把 `position` 局部改成 0：
        # `advance_entry()` 是只增不减的（多来源互相覆盖时要防倒退），
        # 局部改 0 之后推上去的仍是旧的大值，游标就永远卡在失效位置上。
        if position > len(numbered):
            _log.info(
                "对端日志 %s 比游标短（%d < %d），游标已失效，退回全量重扫",
                path.name, len(numbered), position,
            )
            self.cursors.reset_entry(key)
            position = 0

        records = [record for _index, record in numbered[position:]]
        newest = position + len(records)

        # 同一份日志里被判过删除的指纹：后面的内容记录一律不再写入。
        # 顺序至关重要 —— 日志是追加式的，「先有内容、后有删除」；
        # 若反过来让内容记录覆盖删除，用户删掉的东西会被同一条日志复活。
        # 位置按条目序号推进后，这两条仍可能落在同一批里（同一次
        # 追加写入的多条记录共享一个块），所以这个「批内去重」不是可选项。
        deleted_in_batch: set = set()
        for record in records:
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

        # 标记变更同样要先于内容处理：一条「取消固定」的事件必须先落进
        # 变更存储，后面同批里的内容记录才能算出正确的标记终值。
        # 顺序反了的话，取消会被同批内容的并集盖回去 —— 这与墓碑
        # 「先删后写」的批内顺序问题是同一类。
        for record in records:
            if not record.is_flag_change:
                continue
            if record.device == self.device_name:
                # 自己写进日志的那条不算「对端发生的动作」，但它是
                # 本机动作的权威记录：把它补进变更存储，本机才知道
                # 「这个标记已经按日志发出去了」，不必重复补发。
                continue
            recorded = self.flag_changes.record(
                record.hash,
                record.flag_change,
                record.flag_value,
                at=record.created_at,
                device=record.device,
            )
            if recorded:
                outcome.flag_changes += 1
            if self._apply_flag_change(target_hash=record.hash, seen_local=seen_local):
                outcome.flags_updated += 1

        for record in records:
            if record.is_tombstone or record.is_flag_change:
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
                if apply_flags(self._storage, record, seen_local, self.flag_changes):
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
            apply_flags(self._storage, record, seen_local, self.flag_changes)
            outcome.imported += 1
        # 只有在完整读完后才推进游标：解析中途出错不该跳过剩余记录
        self.cursors.advance_entry(key, newest)
        return outcome

    def _apply_flag_change(
        self, *, target_hash: str, seen_local: Optional[set] = None
    ) -> bool:
        """把某指纹上解出来的标记终值落到本地记录上。

        为什么需要这一步，而不是只依赖内容记录：

        内容记录**只发一次**（发过的不再重发），而标记变化是之后的动作。
        所以「对端固定了某条」这件事到达时，同批里根本没有那条的内容记录 ——
        只靠内容循环里的 `apply_flags` 就永远碰不到它。取消同理，
        而且取消更容易被漏（它是「记录上什么都没变」的反向信息）。

        只动本地已存在的记录：本地没有这条内容时无处可落，
        等它随内容记录一起来时再处理。
        """
        existing = self._storage.find_by_hash(target_hash)
        if existing is None:
            return False
        resolved = _resolved_flags(self.flag_changes, target_hash)
        if resolved is None:
            return False
        # 本地值可能是「对端带来的并集」之外的信息（比如本机用户刚固定过），
        # 所以先把本地的固定意图并进去，再让解出来的终值覆盖 —— 与
        # `apply_flags` 里的顺序一致，两条路径不能各写一套。
        local = FlagSnapshot(
            pinned=existing.pinned,
            favorite=existing.favorite,
            pinned_by=existing.pinned_by,
        )
        merged = _apply_resolved_flags(local, self.flag_changes, target_hash)
        if (
            merged.pinned == existing.pinned
            and merged.favorite == existing.favorite
            and merged.pinned_by == existing.pinned_by
        ):
            return False
        # `update_fields` 的 `metadata` 是**合并补丁**：传 `None` 表示删掉该键。
        # 这里不能传整份拷出来的字典 —— 合并语义下那样做既删不掉旧键，
        # 又会让「本来只想改 pinned_by」变成一次全量覆盖。
        self._storage.update_fields(
            existing.id,
            pinned=merged.pinned,
            favorite=merged.favorite,
            metadata=_pinned_by_patch(merged),
        )
        if seen_local is not None:
            seen_local.add(target_hash)
        return True

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
        return [record for _index, record in self._read_journal_numbered(path)]

    def _read_journal_numbered(self, path: Path) -> List[tuple]:
        """同 `_read_journal`，但每条带**在日志里的条目序号**（从 1 起）。

        为什么需要序号：游标要回答「这份日志我读到第几条了」。
        `created_at` 回答不了这个问题 —— 它由写入方给，**不保证单调**
        （系统剪贴板历史导入、存储迁移都会写下过去的时间戳），
        于是新追加的记录会「落在游标之前」而被永久跳过。

        日志是追加式的，所以**条目序号**天然单调：新记录一定排在后面。
        它不依赖记录里的任何字段，也不需要改变文件格式。
        """
        if not path.exists():
            return []
        records: List[tuple] = []
        index = 0
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
                    index += 1
                    records.append((index, record))
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
        entries = self.cursors.entry_snapshot()
        devices = []
        for path in files:
            key = self.cursor_key(path)
            devices.append(
                {
                    "device": path.stem,
                    "journal": path.name,
                    # 「同步到哪」的权威答案是**条目游标**：文件夹模式按
                    # 「读到第几条」推进。这里**不报时间戳** —— 它不是这个
                    # 模式的判据，报出来只会是一个恒为 0 的数，
                    # 让人误以为「一直没同步过」。
                    "entries": entries.get(key, 0),
                    "synced": entries.get(key, 0) > 0,
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
    flag_changes: Optional[FlagChangeStore] = None,
) -> bool:
    """把对端记录上的用户标记合并到本地同指纹的记录上。

    两条同步模式共用一个实现：``FolderSync._import_journal`` 与
    ``LanSyncClient.import_records`` 若各写一套，迟早分叉
    （此前的墓碑处理就踩过这个坑，所以那次专门抽了同一个规则）。

    合并方向**默认是取并集**（见 ``FlagSnapshot.merge``）：固定与收藏都是
    加法式的用户意图，取并集是唯一不会把用户的显式标记悄悄弄丢的选择。

    但并集有一个已知代价：**取消传不出去**。有了 ``flag_changes`` 之后，
    只要某台设备**显式取消过**（这是一个带时间戳的事实），就可以压过并集 ——
    具体规则见 `flag_changes.resolve_flag`：设备内取最新、设备间取并集。

    优先级刻意是「变更存储 > 记录上的并集」：变更存储里带着「谁在什么时候
    取消了」，信息量严格大于一个布尔值。记录上的 `pinned` 只说明「有人设过」，
    它没有能力表达取消。

    ``flag_changes`` 为 None 时退化为纯并集，行为与改造前完全一致 ——
    调用方（尤其第三方集成与既有用例）不传就还是老样子。

    返回是否真的改动了本地数据 —— 调用方用它统计「有多少条只更新了标记」，
    这个数字要能被看到，否则又是「数据变了但没人知道」。
    """
    if record.is_tombstone or record.is_flag_change:
        return False
    if not record.flags().any():
        # 记录上没有标记。**仍然要看变更存储** —— 「取消」恰恰是以
        # 「记录上什么都没有」的形式表达的，直接返回会把取消吞掉，
        # 那本轮修的东西就不存在了。有存储时继续往下走。
        if flag_changes is None:
            return False
        resolved_any = _resolved_flags(flag_changes, record.hash)
        if resolved_any is None:
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

    if flag_changes is not None:
        merged = _apply_resolved_flags(merged, flag_changes, record.hash)

    if (
        merged.pinned == existing.pinned
        and merged.favorite == existing.favorite
        and merged.pinned_by == existing.pinned_by
    ):
        return False

    storage.update_fields(
        existing.id,
        pinned=merged.pinned,
        favorite=merged.favorite,
        metadata=_pinned_by_patch(merged),
    )
    if seen_local is not None:
        seen_local.add(record.hash)
    return True


def _pinned_by_patch(merged: FlagSnapshot) -> dict:
    """算出 `pinned_by` 这一个键该怎么改，作为 `metadata` 合并补丁。

    两条规则：

    - 有固定标记且来源已知 → 写成来源设备名（删除传播要据此判断固定是否已过期）
    - 其余情况 → 传 `None` 删掉这个键。没有固定标记时不留来源，
      避免旧来源在后续取消/重设之间串味。
    """
    if merged.pinned and merged.pinned_by:
        return {PINNED_BY_KEY: merged.pinned_by}
    return {PINNED_BY_KEY: None}


def _resolved_flags(flag_changes: FlagChangeStore, digest: str) -> Optional[FlagSnapshot]:
    """把变更存储里该指纹的终值解出来；没有任何变更时返回 None。"""
    pinned = resolve_flag(flag_changes.changes_for(digest), FLAG_PINNED)
    favorite = resolve_flag(flag_changes.changes_for(digest), FLAG_FAVORITE)
    if pinned is None and favorite is None:
        return None
    return FlagSnapshot(
        pinned=bool(pinned),
        favorite=bool(favorite),
    )


def _apply_resolved_flags(
    merged: FlagSnapshot,
    flag_changes: FlagChangeStore,
    digest: str,
) -> FlagSnapshot:
    """用变更存储的终值覆盖并集结果（只覆盖有人明确表过态的那些标记）。"""
    changes = flag_changes.changes_for(digest)
    if not changes:
        return merged
    pinned = resolve_flag(changes, FLAG_PINNED)
    favorite = resolve_flag(changes, FLAG_FAVORITE)
    result = FlagSnapshot(
        pinned=merged.pinned if pinned is None else bool(pinned),
        favorite=merged.favorite if favorite is None else bool(favorite),
        pinned_by=merged.pinned_by,
    )
    if not result.pinned:
        # 取消固定后来源一并清掉，否则旧来源会跟着一个「未固定」的记录
        # 漂到后续同步里，删除保护的判断会读到过期的设备名。
        result.pinned_by = ""
    return result
