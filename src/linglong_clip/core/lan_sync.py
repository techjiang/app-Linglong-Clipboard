"""局域网直连同步：不经过任何服务器，设备之间直接交换记录。

与 `FolderSync` 的关系：
- 复用 `SyncRecord` 作为传输单元、`SyncCipher` 作为可选加密，
  两条同步路径的隐私判定（`PrivacyGuard`）与去重逻辑完全一致，避免出现
  「共享文件夹拦住了、局域网漏了」这类不一致。
- `FolderSync` 适合「用户已有同步盘」；`LanSync` 适合同一 Wi-Fi 下
  想把几台机器直接连起来、不想依赖第三方网盘的用户。

设计取舍：
- **用标准库 `http.server`，不引入 aiohttp/requests**。同步是几十毫秒级的
  小请求，标准库够用；少一个依赖就少一份打包体积与供应链风险。
- **拉取模型（pull）而非推送（push）**。每台设备只需暴露一个只读接口，
  自己决定何时去拉对端，天然避免并发写同一份数据，也不需要处理对端离线。
- **发现靠显式配置 + 可选 UDP 广播**。mDNS/zeroconf 要额外依赖，
  且不少企业 Wi-Fi 会拦截组播；默认让用户填地址，能接受广播时再开自动发现。
- **鉴权用一个共享口令**。同网段不等于可信（咖啡馆 Wi-Fi），
  没有口令的话同网段任何人都能读到剪贴板。口令不匹配直接 401，
  且用 `hmac.compare_digest` 做定时安全比较。
- **只暴露读接口**。接收方主动拉取，发送方不接收写入，
  攻击面因此只剩「读数」一条路径。
- **版本要协商，不能只是印在产品上**。`/ping` 报出本机的协议版本，
  `/records` 校验对端的版本，说不拢时用 426 明确拒绝并给出理由。
  此前 `PROTOCOL_VERSION` 这个常量出现在四个地方、**没有一处会读它**，
  于是版本不一致的表现不是报错，而是「同步成功、一条都没有」——
  详见 `core/protocol.py`。
"""

from __future__ import annotations

import hmac
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from . import image_transfer, protocol
from .models import ClipboardItem
from .privacy import PrivacyGuard
from .storage import Storage
from .sync import (
    MAX_SYNC_ENTRIES,
    FlagSnapshot,
    FolderSync,
    SyncCipher,
    SyncRecord,
    _apply_resolved_flags,
    _pinned_by_patch,
    _resolved_flags,
    apply_flags,
    pin_is_stale,
)
from .sync_cursor import SyncCursorStore
from .tombstones import Tombstone, TombstoneStore, contains_hash
from .flag_changes import FlagChangeStore

_log = get_logger("lan_sync")

#: 协议版本。定义在 `core/protocol.py` —— 它要同时被服务端、客户端、
#: 发现广播与 CLI 读到，四处各持一份是前几轮反复出事的结构。
#: 这里转发一次只是为了不破坏 `from .lan_sync import PROTOCOL_VERSION` 的既有用法。
PROTOCOL_VERSION = protocol.PROTOCOL_VERSION

#: 单次拉取的最大记录数，防止一台设备一次把对端整库拉空
DEFAULT_PAGE_SIZE = 500

#: 网络请求超时（秒）。局域网内超过这个时间说明对端不可达
DEFAULT_HTTP_TIMEOUT = 5.0

#: UDP 发现广播的默认端口与固定魔术串
DISCOVERY_PORT = 48621
DISCOVERY_MAGIC = "LINGLONG_CLIPBOARD_DISCOVER"


@dataclass
class PeerInfo:
    """一个对端的描述。"""

    host: str
    port: int
    device: str = ""
    last_seen: float = 0.0
    ok: bool = False
    error: str = ""
    #: 对端自报的协议版本（``""`` 表示还没握过手，或对端没报）。
    #:
    #: 它是**握手拿到的**，不是猜的：`/ping` 与 UDP 广播都带这个字段。
    #: 没有它的时候，「版本不一致」只能表现成「同步成功、一条都没有」。
    protocol: str = ""
    #: 兼容性判定，取值见 `core/protocol.py`。空串表示尚未判定。
    compatibility: str = ""

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def usable(self) -> bool:
        """这个对端能不能按本机的语义去读。

        「还没握手」不算不能用 —— 握手是同步第一步会做的事，
        在这里拦会把「首次同步」也一起拦掉。
        """
        if not self.compatibility:
            return True
        return protocol.is_usable(self.compatibility)

    def to_json(self) -> Dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "device": self.device,
            "last_seen": self.last_seen,
            "ok": self.ok,
            "error": self.error,
            "protocol": self.protocol,
            "compatible": self.usable,
            "compatibility": self.compatibility,
        }


def _record_payload(record: SyncRecord) -> Dict[str, object]:
    """局域网响应里的单条载荷。

    与 `SyncRecord.to_json()` 的唯一区别是**多带一个 `seq`**：它是发送方库内
    的自增 id，只服务于翻页游标的次级键，不必也不该写进共享日志
    （那会改变日志格式，还要处理老版本读到未知字段的情形）。
    `seq` 只在记录真的来自内容行时才写：墓碑与标记变更没有对应的内容行。
    """
    payload = record.to_json()
    if record.seq:
        payload["seq"] = int(record.seq)
    return payload


def _encode_record(record: SyncRecord) -> str:
    return json.dumps(_record_payload(record), ensure_ascii=False)


def parse_peer(spec: str, default_port: int) -> Optional[Tuple[str, int]]:
    """解析 `host:port` / `host` / `http://host:port` 形式的对端地址。"""
    if not spec:
        return None
    text = spec.strip()
    for prefix in ("http://", "https://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    text = text.rstrip("/")
    if not text:
        return None
    # IPv6 形如 [::1]:8080
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return None
        host = text[1:end]
        rest = text[end + 1 :]
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else default_port
        return host, port
    if ":" in text:
        host, _, raw_port = text.rpartition(":")
        if raw_port.isdigit():
            return host, int(raw_port)
    return text, default_port


class _PeerHandler(BaseHTTPRequestHandler):
    """对端只读接口。挂在 `LanSyncServer` 上，通过 `server.owner` 取数据。"""

    protocol_version = "HTTP/1.1"
    server_version = "LinglongLanSync/" + PROTOCOL_VERSION

    # 覆盖默认日志，避免每来一个请求就往 stderr 打一行
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        _log.debug("lan-sync http: " + fmt, *args)

    # ---- 鉴权 ----

    def _authorized(self) -> bool:
        owner: "LanSyncServer" = self.server.owner  # type: ignore[attr-defined]
        expected = owner.token
        if not expected:
            return True
        supplied = self.headers.get("X-Linglong-Token", "")
        return hmac.compare_digest(str(supplied), str(expected))

    def _send_json(self, status: int, payload: Dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        owner: "LanSyncServer" = self.server.owner  # type: ignore[attr-defined]
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/ping":
            # 探测接口不校验口令：只回设备名与版本，不含任何内容。
            # 这是**握手**：客户端从这里知道对端说的是哪个协议版本，
            # 从而在拉数据之前就能判断「我能不能读懂它」。
            payload = {
                "ok": True,
                "device": owner.device_name,
                "auth_required": bool(owner.token),
            }
            payload.update(protocol.advertised_payload())
            self._send_json(200, payload)
            return

        if not self._authorized():
            _log.warning("局域网同步请求鉴权失败：%s", self.client_address[0])
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        if path == "/records":
            params = urllib.parse.parse_qs(parsed.query)
            # 版本协商必须排在**前面**：一份我们读不懂的载荷，无论后面
            # 怎么解析都不会变对，而「解析出来 0 条」会被当成「对端没东西」。
            # 所以这里先问版本，说不拢就直接拒掉，并且说清拒的原因。
            verdict = protocol.peer_speaks_our_version(params.get("protocol", [""])[0])
            if not protocol.is_usable(verdict):
                detail = protocol.describe(params.get("protocol", [""])[0], verdict)
                _log.warning(
                    "拒绝 %s 的同步请求：%s", self.client_address[0], detail
                )
                self._send_json(
                    # 426 Upgrade Required：语义正是「你的协议对不上，请换一个」。
                    # 用 400 会让客户端以为是它把参数写错了，而这件事
                    # 客户端自己改不了 —— 得人去把两台机器升到同一版本。
                    426,
                    {
                        "ok": False,
                        "error": "protocol-mismatch",
                        "detail": detail,
                        "server_protocol": PROTOCOL_VERSION,
                        "client_protocol": str(params.get("protocol", [""])[0] or ""),
                    },
                )
                return
            try:
                since = float(params.get("since", ["0"])[0])
            except ValueError:
                since = 0.0
            try:
                since_id = int(params.get("since_id", ["0"])[0])
            except ValueError:
                since_id = 0
            try:
                limit = int(params.get("limit", [str(DEFAULT_PAGE_SIZE)])[0])
            except ValueError:
                limit = DEFAULT_PAGE_SIZE
            limit = max(1, min(limit, MAX_SYNC_ENTRIES))
            records, sent_tombstones, sent_changes = owner.collect_records_detailed(
                since=since, since_id=max(0, since_id), limit=limit
            )
            self._send_json(
                200,
                {
                    "ok": True,
                    "device": owner.device_name,
                    "encrypted": owner.encrypted,
                    "records": records,
                    **protocol.advertised_payload(),
                },
            )
            # 写出成功之后才确认送达：`_send_json` 里的 `wfile.write` 失败会
            # 抛异常，走不到这一行，事件保持待发、下轮重传。
            # 之前这里没有确认，于是「已送达的不再复述」在局域网模式下
            # 从未生效 —— 每轮同步都在重传全部历史删除。
            owner.ack_delivered(sent_tombstones, sent_changes)
            return

        if path.startswith("/image/"):
            digest = urllib.parse.unquote(path[len("/image/"):])
            data = owner.image_bytes(digest)
            if data is None:
                # 不区分「没有这张图」与「有但不该外发」：区分开来就等于
                # 告诉请求方「这台机器上确实有这么一个文件」，
                # 而它本就不该知道。
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Linglong-Digest", digest)
            self.end_headers()
            self.wfile.write(data)
            return

        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        # 接收端不提供写入接口：只有被拉取，攻击面因此只剩读取
        self._send_json(405, {"ok": False, "error": "read-only peer"})


class LanSyncServer:
    """把本机记录以 HTTP 形式暴露给同网段的对端。

    生命周期由 `start()` / `stop()` 管理，均可重复调用。
    """

    def __init__(
        self,
        storage: Storage,
        *,
        device_name: str = "",
        token: str = "",
        host: str = "0.0.0.0",
        port: int = 0,
        cipher: Optional[SyncCipher] = None,
        guard: Optional[PrivacyGuard] = None,
        max_entries: int = MAX_SYNC_ENTRIES,
        tombstones: Optional[TombstoneStore] = None,
        flag_changes: Optional[FlagChangeStore] = None,
    ) -> None:
        self._storage = storage
        self.device_name = device_name or socket.gethostname()
        self.token = token
        self.host = host
        self.port = int(port)
        self._cipher = cipher or SyncCipher(False)
        self._guard = guard
        self._max_entries = max_entries
        self.tombstones = tombstones if tombstones is not None else TombstoneStore()
        #: 标记变更通道：让「取消固定/取消收藏」也能跨设备传播。
        self.flag_changes = (
            flag_changes if flag_changes is not None else FlagChangeStore()
        )
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

    # ---- 属性 ----

    @property
    def encrypted(self) -> bool:
        return self._cipher.enabled

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def bound_port(self) -> int:
        """实际监听端口（`port=0` 时由系统分配）。"""
        if self._server is None:
            return self.port
        return int(self._server.server_address[1])

    # ---- 数据 ----

    def collect_records(
        self,
        *,
        since: float = 0.0,
        since_id: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> List[str]:
        """把最近记录编码成 `SyncRecord` JSON 串，供 HTTP 响应携带。

        返回字符串列表而不是对象，是因为加密后的载荷就是字节串，
        序列化成 base64 由客户端解——明文模式下则是普通 JSON 串。

        真正要确认送达的是 ``collect_records_detailed``，见它。
        """
        return self.collect_records_detailed(since=since, since_id=since_id, limit=limit)[0]

    def collect_records_detailed(
        self,
        *,
        since: float = 0.0,
        since_id: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> tuple:
        """同 `collect_records`，但额外返回「本批带出去的墓碑与标记变更」。

        为什么要返回它们：局域网是**拉取模型**，服务端只有在写出响应之后
        才知道对端拿到了数据。确认送达必须发生在写成功之后（见
        `_handle_records`），而这些对象正是那一批的内容 —— 不返回它们，
        后面的 `ack` 就无从下手，于是「已送达的不再复述」这条约定在
        局域网模式下从未生效，每轮都在重传全部历史删除。
        """
        sent_tombstones: List[Tombstone] = []
        sent_changes: List[FlagChange] = []
        items = self._eligible_items(since=since, since_id=since_id, limit=limit)

        payload = [
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
                # 固定的来源以「发送方」为基准：本机设的（来源为空）补上自己的
                # 设备名，对端才知道这条固定是谁设的。不补的话对端会把它
                # 理解成自己的意图，于是收到删除标记时拒绝删除 —— 与共享
                # 文件夹模式同一条规则，两处不能各写一套。
                pinned_by=(item.pinned_by or self.device_name) if item.pinned else "",
                created_at=item.created_at,
                # 有图就把摘要带上（**不带字节**）。按 `image_path` 判断而不是
                # 按 `kind`：富文本里贴的图 `kind` 是 `rich`，只看 kind 会漏掉它。
                #
                # **未经内容判定的图不报摘要。** 图片记录的 `content` 是文件名，
                # 隐私判定对它天然失效，只有 OCR 结果能穿透进去；OCR 没跑过时
                # 我们没有任何依据说它不敏感，而报出摘要就等于告诉对端
                # 「这里有张图可以来取」。详见 `image_transfer.privacy_inspectable`。
                image_digest=(
                    ""
                    if not image_transfer.privacy_inspectable(item)
                    else image_transfer.digest_of_item(item)
                ),
                # 翻页游标的次级键：同一时刻的记录靠它区分先后，
                # 见 `SyncRecord.seq` 的说明。
                seq=int(item.id or 0),
            )
            for item in items
        ]

        # 删除标记：客户端按游标推进请求范围，若墓碑也受 `since` 过滤，
        # 一个刚离线过的对端就会永远收不到这段区间里的删除。
        # 所以墓碑单独成批，和内容一起放进同一个响应（客户端先应用删除再导入内容）。
        pending = self.tombstones.pending()
        sent_tombstones.extend(pending)
        payload.extend(
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

        # 标记变更（取消固定/收藏）：与墓碑同理，不能受 `since` 过滤 ——
        # 取消要传达的前提正是「对端当年收到过这个标记」，按游标裁掉就会
        # 永远传不出去。变更的 `device` 在这里补上发送方设备名，
        # 对端才能按设备取最新意图（见 flag_changes.resolve_flag）。
        pending_changes = self.flag_changes.pending()
        sent_changes.extend(pending_changes)
        payload.extend(
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

        if not payload:
            return [], sent_tombstones, sent_changes

        body = "\n".join(
            _encode_record(record) for record in payload
        ).encode("utf-8")

        if not self._cipher.enabled:
            return (
                [_encode_record(record) for record in payload],
                sent_tombstones,
                sent_changes,
            )

        # 加密模式：整块加密后 base64 成一行，客户端按同一密钥解
        import base64

        blob = self._cipher.encrypt(body)
        return (
            ["ENC:" + base64.b64encode(blob).decode("ascii")],
            sent_tombstones,
            sent_changes,
        )

    def image_bytes(self, digest: str) -> Optional[bytes]:
        """按摘要读出图片字节，供 `/image/<digest>` 返回。

        **判据是「这张图有没有被一条可外发的记录引用」，不是「文件在不在」。**
        这个区别不是洁癖：接口按摘要取文件，如果只检查文件存在，
        就等于给同网段的任何人（拿到口令的情况下）开了一个「按名字读本机文件」
        的口子 —— 而 `images/` 目录里除了剪贴板的截图，用户也可能顺手放了别的东西。

        走一遍隐私判定而不是直接读文件，是为了让「敏感截图不外发」这条
        在图片通道上和记录通道上**是同一条规则**。两处各写一套的话，
        迟早出现「记录拦住了、图还能下载」这种自相矛盾的状态。

        摘要格式先校验：它会参与拼路径，放行 `../` 就是路径穿越。
        """
        if not image_transfer.is_valid_digest(digest):
            return None

        # 找到引用这张图的记录。只认可外发的那部分（非敏感、过隐私判定）。
        owner: Optional[ClipboardItem] = None
        for item in self._storage.iter_all(batch=256):
            if image_transfer.digest_of_item(item) != digest:
                continue
            if item.sensitive:
                continue
            if self._guard is not None:
                verdict = self._guard.inspect(item)
                if not verdict.allowed or verdict.sensitive:
                    continue
            # 图片记录的内容判定天然失效（`content` 是文件名）。只有当 OCR
            # 真的看过这张图（`metadata["ocr"]` 存在）时，我们才有依据说
            # 「它不敏感」。没看过就不外发 —— 理由不是「它一定敏感」，
            # 而是「我们没有任何依据说它不敏感」，而报出去的是
            # 「这台机器上有张没人读过的图、拿到口令就能取走」。
            if image_transfer.digest_of_item(item) and not image_transfer.privacy_inspectable(item):
                _log.debug("图片未经内容判定（无 OCR 结果），拒绝外发：%s", digest)
                continue
            owner = item
            break
        if owner is None:
            _log.debug("请求的图片没有可外发记录引用：%s", digest)
            return None

        path = self._storage.images_dir / f"{digest}.png"
        try:
            data = path.read_bytes()
        except OSError as exc:
            _log.debug("图片读取失败 %s: %s", digest, exc)
            return None
        # 文件内容与文件名不符说明这份文件被人换过或写坏过。
        # 与其把一份无法校验的东西发给对端，不如不认它。
        if not image_transfer.verify(data, digest):
            _log.warning("图片内容与摘要不符，拒绝外发：%s", digest)
            return None
        return data

    def ack_delivered(self, tombstones: Iterable, changes: Iterable) -> None:
        """确认这一批事件已经写进响应、交给对端。

        只在 HTTP 写出成功之后调用：写失败时 `wfile.write` 抛异常，
        调用方根本走不到这里，事件保持待发、下轮重传 —— 宁可重复告知，
        也不能漏（漏一次就是删除/取消没有传播）。
        """
        tombstone_list = list(tombstones)
        if tombstone_list:
            self.tombstones.ack([entry.hash for entry in tombstone_list])
        change_list = list(changes)
        if change_list:
            self.flag_changes.ack(
                [self.flag_changes._key(change) for change in change_list]
            )

    def _eligible_items(
        self,
        *,
        since: float,
        since_id: int = 0,
        limit: int,
    ) -> List[ClipboardItem]:
        """挑选可外发的记录：过隐私、跳过图片与敏感项，且晚于 `(since, since_id)`。

        遍历用 `iter_since`（按时间升序、`id` 兜同刻），而不是
        `list(limit=self._max_entries)`。后者有两个问题，都会导致对端拿不全：

        1. **只看最新 `_max_entries` 条**。记录数超过这个上限时，更老的
           永远不在候选里，对端怎么拉都拉不到。
        2. **界面排序不稳定**。`list()` 按 `pinned/used_at` 排，用户复制一下
           记录就会换位；对端按时间游标翻页时，翻着翻着就错位了。

        客户端会带着推进后的 `(since, since_id)` 反复请求，直到某页不足
        `limit`；所以这里只要保证「给定游标的下一条是确定的」即可。

        `since_id` 必须参与过滤：全部记录共享同一 `created_at` 时
        （紧密循环写库、批量导入系统历史都会这样），只比时间戳的游标
        在第一页之后就推不动了 —— 剩下的记录不是更旧，是**一样新**，
        于是被永久跳过且毫无提示。
        """
        out: List[ClipboardItem] = []
        for item in self._storage.iter_since(
            since, since_id=since_id, batch=max(limit * 2, 64)
        ):
            if not item.hash:
                continue
            # 图片记录**要**发，但只发摘要、不发字节。
            # 此前这里整条跳过，于是对端连「这张图存在过」都不知道；
            # 现在带着 `image_digest` 过去，对端需要时再走 `/image/<digest>`
            # 按需拉取。体积代价是几十字节，而这是剪贴板里最常用的一类内容。
            if item.sensitive:
                continue
            if self._guard is not None:
                verdict = self._guard.inspect(item)
                if not verdict.allowed or verdict.sensitive:
                    continue
            out.append(item)
            if len(out) >= limit:
                break
        return out

    # ---- 生命周期 ----

    def start(self) -> bool:
        with self._lock:
            if self._server is not None:
                return True
            try:
                server = ThreadingHTTPServer((self.host, self.port), _PeerHandler)
            except OSError as exc:
                _log.warning("局域网同步服务启动失败（%s:%s）：%s", self.host, self.port, exc)
                return False
            server.owner = self  # type: ignore[attr-defined]
            server.daemon_threads = True
            self._server = server
            self._thread = threading.Thread(
                target=server.serve_forever, name="lan-sync-server", daemon=True
            )
            self._thread.start()
            _log.info("局域网同步服务已启动：%s:%s", self.host, self.bound_port)
            return True

    def stop(self, timeout: float = 2.0) -> None:
        with self._lock:
            server, thread = self._server, self._thread
            self._server, self._thread = None, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout)
        _log.info("局域网同步服务已停止")

    def status(self) -> Dict[str, object]:
        return {
            "running": self.running,
            "host": self.host,
            "port": self.bound_port,
            "device": self.device_name,
            "encrypted": self.encrypted,
            "auth_required": bool(self.token),
        }


@dataclass
@dataclass
class ImageFetchOutcome:
    """一次「按需补图」的结果。"""

    missing: int = 0
    """同步过来、但本机还没有字节的图片张数（本轮开始前的账面缺口）。"""

    fetched: int = 0
    """成功取回并已落到某条记录上的张数。"""

    adopted: int = 0
    """因此被补上 `image_path` 的记录条数（一张图可能对应多条记录）。"""

    orphaned: int = 0
    """字节取回了、但当前没有记录引用它（记录可能已被删除或淘汰）。"""

    failed: int = 0
    """没取到的张数：对端都没有、鉴权失败或校验不过。"""

    truncated: bool = False
    """是否因单次上限提前停下 —— 停下不代表补齐了。"""

    def as_dict(self) -> Dict[str, object]:
        return {
            "missing": self.missing,
            "fetched": self.fetched,
            "adopted": self.adopted,
            "orphaned": self.orphaned,
            "failed": self.failed,
            "truncated": self.truncated,
        }


class LanImportOutcome:
    """一次局域网导入的明细。"""

    imported: int = 0
    removed: int = 0
    tombstones: int = 0
    #: 因对端带来了本地没有的标记（固定/收藏）而被更新的条数
    flags_updated: int = 0
    #: 因对端带来了本机不知道的标记变更（含取消）而入账的条数
    flag_changes: int = 0


class LanSyncClient:
    """从对端拉取记录并写入本地库。"""

    def __init__(
        self,
        storage: Storage,
        *,
        device_name: str = "",
        token: str = "",
        cipher: Optional[SyncCipher] = None,
        guard: Optional[PrivacyGuard] = None,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
        cursors: Optional[SyncCursorStore] = None,
        tombstones: Optional[TombstoneStore] = None,
        flag_changes: Optional[FlagChangeStore] = None,
    ) -> None:
        self._storage = storage
        self.device_name = device_name or socket.gethostname()
        self.token = token
        self._cipher = cipher or SyncCipher(False)
        self._guard = guard
        self._timeout = timeout
        self.cursors = cursors if cursors is not None else SyncCursorStore()
        self.tombstones = tombstones if tombstones is not None else TombstoneStore()
        #: 标记变更通道（与共享文件夹模式共用同一份规则）
        self.flag_changes = (
            flag_changes if flag_changes is not None else FlagChangeStore()
        )
        #: 单次同步最多翻多少页。只是防呆保险丝：
        #: 正常情况下服务端某页不满就会让循环退出。
        self._max_pages = max(1, int((MAX_SYNC_ENTRIES // DEFAULT_PAGE_SIZE) + 2))

    def handshake(self, peer: PeerInfo) -> bool:
        """向对端要一次协议版本，判定双方能不能说上话。

        为什么要单独一步而不是「拉数据的时候顺带看看」：拉回来的一份
        读不懂的载荷，长得和「对端没有东西可同步」**一模一样** ——
        解密成功、每行都是合法 JSON、字段读不到，于是逐条跳过，
        导入 0 条、HTTP 200、`peer.ok = True`。用户看到的是
        「同步成功了」，而不是「这两台机器说的不是同一种话」。

        所以版本判定必须发生在**读数据之前**，并且能单独回答
        「是不兼容，还是真的没东西」。

        `/ping` 不需要口令，所以握手在口令配错时也能成功 —— 那是有意的：
        「口令不对」和「版本不对」是两件事，不该互相掩盖。
        """
        request = urllib.request.Request(f"{peer.base_url}/ping", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            peer.error = f"握手失败：HTTP {exc.code}"
            _log.debug("握手 %s 失败：HTTP %s", peer.host, exc.code)
            return False
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            peer.error = f"握手失败：{exc}"
            _log.debug("握手 %s 失败：%s", peer.host, exc)
            return False

        raw_version = data.get("protocol", "")
        peer.protocol = str(raw_version or "")
        peer.device = str(data.get("device", peer.device))
        peer.compatibility = protocol.peer_speaks_our_version(raw_version)
        if not protocol.is_usable(peer.compatibility):
            # 这句话要能直接印给用户看：谁、什么版本、差在哪、该干什么
            peer.error = protocol.describe(raw_version, peer.compatibility)
            _log.warning("对端 %s 协议不兼容：%s", peer.host, peer.error)
        else:
            peer.error = ""
        # 登记排在 `error` 之后：说明也要一起记，只记版本的话，
        # 下次枚举出来的对端会写着「协议 2、不兼容」而错误栏是空的。
        self._note_protocol(peer)
        return not peer.error

    def fetch(
        self,
        peer: PeerInfo,
        *,
        since: float = 0.0,
        since_id: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> Optional[List[SyncRecord]]:
        """拉取一个对端的记录；不可达或鉴权失败时返回 `None`。"""
        # 报上自己的版本，并在读数据**之前**判定兼容性：
        # 一份读不懂的载荷解析出来是 0 条，而那会被当成「对端没东西」。
        known_version, known_verdict, known_error = self.peer_protocol(peer)
        if known_verdict and not protocol.is_usable(known_verdict):
            peer.ok = False
            peer.protocol = peer.protocol or known_version
            peer.compatibility = known_verdict
            peer.error = peer.error or known_error or protocol.describe(
                known_version, known_verdict
            )
            return None
        query = urllib.parse.urlencode(
            {
                "since": f"{since:.6f}",
                "since_id": max(0, int(since_id or 0)),
                "limit": limit,
                "protocol": PROTOCOL_VERSION,
            }
        )
        url = f"{peer.base_url}/records?{query}"
        request = urllib.request.Request(url, method="GET")
        if self.token:
            request.add_header("X-Linglong-Token", self.token)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
                code = response.status
        except urllib.error.HTTPError as exc:
            peer.ok = False
            if exc.code == 426:
                # 服务端明确说了「协议对不上」—— 把它的理由原样带出来，
                # 比自己编一句「HTTP 426」有用得多。
                peer.error = self._read_mismatch_detail(exc) or (
                    f"对端拒绝：协议版本不一致（本机 {PROTOCOL_VERSION}）"
                )
                _log.warning("对端 %s 拒绝同步：%s", peer.host, peer.error)
                return None
            peer.error = f"HTTP {exc.code}"
            if exc.code == 401:
                _log.warning("对端 %s 拒绝访问：口令不匹配", peer.host)
            return None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            peer.ok = False
            peer.error = str(exc)
            _log.debug("拉取 %s 失败：%s", url, exc)
            return None

        if code != 200:
            peer.ok = False
            peer.error = f"HTTP {code}"
            return None

        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            peer.ok = False
            peer.error = f"响应不可解析：{exc}"
            return None

        peer.device = str(data.get("device", peer.device))
        peer.last_seen = time.time()
        peer.ok = True
        peer.error = ""
        # 对端在响应里也报了版本。握手已经判定过一次，这里只是兜底：
        # 万一调用方绕过 `handshake()` 直接 `fetch()`，「说的是哪个版本」
        # 这个事实也不该丢 —— 丢了它，下次排查又得从头猜。
        raw_version = data.get("protocol", "")
        if raw_version and not peer.protocol:
            peer.protocol = str(raw_version)
            peer.compatibility = protocol.peer_speaks_our_version(raw_version)
        self._note_protocol(peer)
        return self._decode_records(data.get("records") or [])

    def _note_protocol(self, peer: PeerInfo) -> None:
        """把「这台对端说的是哪个版本」记下来。

        记在客户端自己的字典里，而不是只写在传进来的 `PeerInfo` 上 ——
        对端对象是**每次枚举现造**的，只写对象等于写在便签上然后扔掉。
        键与游标一致（``host:port``），因为排查问题时这两张表要对得上。

        **连 `error` 一起记**。只记版本的话，下次枚举出来的对端会写着
        「协议 2、不兼容」而错误栏是空的 —— 状态页于是显示成一台
        「不兼容但一切正常」的机器，用户不知道该改什么。
        """
        known = getattr(self, "_known_protocols", None)
        if known is None:
            known = {}
            self._known_protocols = known
        if peer.protocol or peer.compatibility:
            known[f"{peer.host}:{peer.port}"] = (
                peer.protocol,
                peer.compatibility,
                peer.error,
            )

    def peer_protocol(self, peer: PeerInfo) -> tuple:
        """这台对端的「版本事实」：先看对象上有没有，没有就查登记表。

        返回 ``(版本, 判定, 说明)``，三者都可能为空串。
        """
        if peer.protocol or peer.compatibility:
            return peer.protocol, peer.compatibility, peer.error
        return getattr(self, "_known_protocols", {}).get(
            f"{peer.host}:{peer.port}", ("", "", "")
        )

    @staticmethod
    def _read_mismatch_detail(exc: urllib.error.HTTPError) -> str:
        """从 426 响应里取出对端写的那句人话；取不到就返回空串。"""
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - 拿不到理由不该影响主流程
            return ""
        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("error") or ""
            return str(detail)
        return ""

    def fetch_image(self, peer: PeerInfo, digest: str) -> Optional[bytes]:
        """向对端按需拉一张图。返回**已通过摘要校验**的字节，失败返回 `None`。

        `None` 与「空字节」是两回事，调用方要分开：前者是「这次没拿到，
        下轮再试」，后者是「对端给了一张空图」（会在这里就被校验挡掉，
        同样返回 `None`）。

        校验放在这里而不是落盘处：无论是谁调这个函数，拿到的字节都已经
        和摘要对上了。落盘处再校验一次是有意的重复 —— 见 `image_transfer.store`。
        """
        if not image_transfer.is_valid_digest(digest):
            return None
        url = f"{peer.base_url}/image/{urllib.parse.quote(digest)}"
        request = urllib.request.Request(url, method="GET")
        if self.token:
            request.add_header("X-Linglong-Token", self.token)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                if response.status != 200:
                    return None
                # 先按上限拒绝，再读 —— 对端可能被改过，不能假定它给的一定合规。
                # `Content-Length` 缺失时（分块传输）只能靠读了之后判断，
                # 所以下面还有一次长度检查。
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > image_transfer.MAX_IMAGE_BYTES:
                    _log.warning("对端图片超过上限，拒绝：%s", digest)
                    return None
                data = response.read(image_transfer.MAX_IMAGE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                _log.warning("对端 %s 拒绝取图：口令不匹配", peer.host)
            return None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _log.debug("取图失败 %s：%s", digest, exc)
            return None

        if not data or len(data) > image_transfer.MAX_IMAGE_BYTES:
            return None
        if not image_transfer.verify(data, digest):
            # 内容寻址白送的好处：不需要额外约定校验字段，
            # 对端给错东西（或被中间人换过）在这里就会暴露。
            _log.warning("对端返回的图片与摘要不符，丢弃：%s", digest)
            return None
        return data

    def _decode_records(self, raw_records: Sequence[object]) -> List[SyncRecord]:
        """把对端返回的载荷还原成记录，兼容明文与加密两种形态。"""
        records: List[SyncRecord] = []
        for entry in raw_records:
            if not isinstance(entry, str):
                continue
            if entry.startswith("ENC:"):
                records.extend(self._decode_encrypted(entry[4:]))
                continue
            try:
                records.append(SyncRecord.from_json(json.loads(entry)))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        return [record for record in records if record is not None]

    def _decode_encrypted(self, b64: str) -> List[SyncRecord]:
        import base64

        try:
            blob = base64.b64decode(b64.encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            return []
        plain = self._cipher.decrypt(blob)
        if plain is None:
            _log.warning("解密对端记录失败：密钥不一致或数据损坏")
            return []
        out: List[SyncRecord] = []
        for line in plain.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                record = SyncRecord.from_json(json.loads(line.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if record is not None:
                out.append(record)
        return out

    def import_records(self, records: Iterable[SyncRecord]) -> "LanImportOutcome":
        """写入本地库，跳过重复、自己的记录与隐私策略不允许的内容。

        先处理全部删除标记，再处理内容：同一个响应里内容排在墓碑之前，
        若顺序反了，刚被删除的指纹会被紧随其后的内容记录写回来。
        """
        records = [record for record in records if record.hash]
        # 本地已有指纹，用于「对端也发过这条，别重复写」。必须覆盖整库：
        # 原先写的是 `list(limit=MAX_SYNC_ENTRIES)`，本地历史超过该上限时
        # 更老的记录不在集合里，对端每轮重放都会把它们再写一遍，产生重复行。
        seen = {item.hash for item in self._storage.iter_all()}
        # 注意：判「指纹是否已在本地」一律用 contains_hash()，
        # 不用 `in` —— 见 tombstones.contains_hash() 的说明
        outcome = LanImportOutcome()

        for record in records:
            if not record.is_tombstone:
                continue
            if record.device == self.device_name:
                continue
            outcome.tombstones += 1
            if self._apply_tombstone(record, seen):
                outcome.removed += 1

        # 标记变更要先于内容处理：一条「取消固定」的事件必须先落进变更存储，
        # 后面同批里的内容记录才能算出正确的标记终值。顺序反了的话，
        # 取消会被同批内容的并集盖回去 —— 与墓碑「先删后写」同理。
        for record in records:
            if not record.is_flag_change:
                continue
            if record.device == self.device_name:
                continue
            if self.flag_changes.record(
                record.hash,
                record.flag_change,
                record.flag_value,
                at=record.created_at,
                device=record.device,
            ):
                outcome.flag_changes += 1
            if self._apply_flag_change(record.hash, seen):
                outcome.flags_updated += 1

        # 本地已记墓碑的指纹不再写回：否则本机删掉的记录会从对端日志里复活。
        # 与共享文件夹模式同一条规则，两处不能各写一套。
        deleted_in_store = self.tombstones.all_hashes()

        for record in records:
            if record.is_tombstone or record.is_flag_change:
                continue
            if contains_hash(deleted_in_store, record.hash):
                _log.debug("跳过已删除的记录（本地有墓碑）：%s", record.hash)
                continue
            if contains_hash(seen, record.hash):
                # 内容本地已有 —— 但**标记可能还没有**。此前这里直接跳过，
                # 于是对端后加的固定/收藏永远传不过来。与共享文件夹模式
                # 共用同一份 apply_flags，两条路径不能各写一套规则。
                if apply_flags(self._storage, record, seen, self.flag_changes):
                    outcome.flags_updated += 1
                continue
            if record.device == self.device_name:
                continue
            item = record.to_item()
            if self._guard is not None:
                verdict = self._guard.inspect(item)
                if not verdict.allowed or verdict.sensitive:
                    continue
            self._storage.add(item)
            seen.add(record.hash)
            apply_flags(self._storage, record, seen, self.flag_changes)
            outcome.imported += 1
        return outcome

    def _apply_flag_change(self, target_hash: str, seen: set) -> bool:
        """把某指纹上解出来的标记终值落到本地记录上。

        与共享文件夹模式共用同一份解算规则（`_resolved_flags` /
        `_apply_resolved_flags`），两条路径不能各写一套。

        为什么不能只靠内容记录：内容只发一次，标记变化是之后的动作，
        所以变更到达时同批里往往没有那条的内容记录 —— 只靠内容循环里的
        `apply_flags` 永远碰不到它。本地没有这条内容时无处可落，
        等它随内容记录一起来时再处理。
        """
        existing = self._storage.find_by_hash(target_hash)
        if existing is None:
            return False
        resolved = _resolved_flags(self.flag_changes, target_hash)
        if resolved is None:
            return False
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
        # `metadata` 是**合并补丁**，传 `None` 才表示删掉该键。
        # 这里此前传的是「整份拷出来的字典」：合并语义下「少了一个键」与
        # 「没提到这个键」无法区分，于是 `pinned_by` 一旦写上就再也清不掉 ——
        # 取消固定后来源仍留着，删除保护会据此把本设备的删除判成「非过期残留」
        # 而拒绝执行。与共享文件夹模式共用同一个 `_pinned_by_patch`，
        # 两条路径不能各写一套（这是它们第 N 次分叉，见 CONTRIBUTING）。
        self._storage.update_fields(
            existing.id,
            pinned=merged.pinned,
            favorite=merged.favorite,
            metadata=_pinned_by_patch(merged),
        )
        seen.add(target_hash)
        return True

    def _apply_tombstone(self, record: SyncRecord, seen: set) -> bool:
        """落地一条对端删除标记，并清掉本地同指纹的记录。

        规则与共享文件夹模式完全一致，两处不能各写一套：
        过期的不动本地数据、固定项不删、同一指纹只处理一次。
        固定项的判定同样共用 `pin_is_stale` —— 由发起删除的设备自己设下的
        固定标记是过期残留，不该阻断删除（详见该函数说明）。
        """
        if self.tombstones.has(record.hash):
            return False
        self.tombstones.record(record.hash, deleted_at=record.deleted_at)
        seen.add(record.hash)

        if record.deleted_at > 0 and self.tombstones.is_expired(
            Tombstone(hash=record.hash, deleted_at=record.deleted_at)
        ):
            return False

        existing = self._storage.find_by_hash(record.hash)
        if existing is None:
            return False
        if existing.pinned and not pin_is_stale(existing, record):
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

    @staticmethod
    def cursor_key(peer: PeerInfo) -> str:
        """每个对端一个游标：`lan:<host>:<port>`。"""
        return SyncCursorStore.key_for("lan", f"{peer.host}:{peer.port}")

    def sync_peer(self, peer: PeerInfo, *, since: Optional[float] = None) -> int:
        """拉取并导入单个对端，返回导入条数（失败时 0）。"""
        return self.sync_peer_detailed(peer, since=since).imported

    def sync_peer_detailed(
        self, peer: PeerInfo, *, since: Optional[float] = None
    ) -> "LanImportOutcome":
        """同 `sync_peer`，但返回明细（含只更新标记的条数）。

        `since` 显式传入时按传入值拉取（并同步推进游标），为 `None` 时
        取自持久化游标——这是「每轮只拉新记录」的关键。

        **先握手，再拉数据**。握手失败（版本说不拢、对端不可达）时一条不拉，
        理由写进 `peer.error` —— 「不兼容」必须和「对端没东西」分得开，
        否则用户看到的永远是后者。

        **翻页直到对端给完**。此前只拉一页：对端可外发记录超过
        `DEFAULT_PAGE_SIZE` 时，第一页之后的部分**永远不会再来第二趟** ——
        游标被推到第一页的最大时间戳，而剩余记录都比它旧，于是被永久跳过。
        表现为「新设备首同步丢了一半历史」，且没有任何报错。

        **游标用 `(created_at, seq)` 复合值，`seq` 不可省**。上一条修完
        翻页之后，还有一条更难发现的死路：同一时刻的记录多到塞满一页时，
        首页返回满页、时间戳全是 `T`，下一页请求 `created_at > T` 拿到 0 条 ——
        剩下的记录不是「更旧」，是**一样新**。它们既不在这一页、也永远
        不在下一页，同样没有任何报错。剪贴板里这并不罕见：紧密循环写库、
        批量导入系统剪贴板历史，都会让同一毫秒挤进大量记录。
        带上 `seq`（发送方库内自增 id）后，「同一时刻内读到哪一条」也能表达，
        翻页于是总能前进。持久化游标只在**整轮翻页结束**后推进一次：
        中途失败保留旧位置，下轮从同一处重来（重复由 hash 去重吃掉）。
        """
        # 先握手：说不拢就**一条都不拉**，并把理由写在 `peer.error` 上。
        # 不握手的话，不兼容的对端会返回一份本机读不懂的载荷，解析结果
        # 是 0 条 —— 那看起来和「对端没有东西可同步」完全一样。
        if not self.handshake(peer):
            peer.ok = False
            if not peer.error:
                peer.error = protocol.describe(peer.protocol, peer.compatibility)
            return LanImportOutcome()

        key = self.cursor_key(peer)
        if since is None:
            requested, requested_id = self.cursors.since_pair_for(key)
        else:
            requested, requested_id = max(0.0, float(since)), 0

        total = LanImportOutcome()
        advanced: Optional[Tuple[float, int]] = None
        pages = 0
        #: 翻页上限，纯粹的保险丝：正常情况下「某页不满」就会退出。
        #: 万一服务端行为异常（比如始终返回同一批），这里兜住不让进程空转。
        max_pages = self._max_pages

        while pages < max_pages:
            records = self.fetch(
                peer, since=requested, since_id=requested_id, limit=DEFAULT_PAGE_SIZE
            )
            if records is None:
                # 对端不可达或鉴权失败：不推进游标，下轮重试这一段。
                # 已经导完的几页不会白做 —— 游标没动，但内容已按 hash 去重。
                return total if pages else LanImportOutcome()
            if not records:
                break
            pages += 1

            page_outcome = self.import_records(records)
            total.imported += page_outcome.imported
            total.removed += page_outcome.removed
            total.tombstones += page_outcome.tombstones
            total.flags_updated += page_outcome.flags_updated
            total.flag_changes += page_outcome.flag_changes

            # 本页的「最后一条」= 服务端全序里的最大 `(created_at, seq)`。
            # 服务端 `since` 是排他语义，直接把游标推到最后一条即可，
            # 不会重读这一条。取 max 而不是末元素，是不假设响应内的顺序。
            page_last = max(
                (self._page_position(record) for record in records),
                default=(0.0, 0),
            )
            if page_last <= (requested, requested_id):
                # 位置没有前进：对端没带 `seq`（旧版本）且同刻记录塞满一页。
                # 再问下去只会拿到同一批，只能结束本轮 —— 剩下的记录
                # 要靠对端升级后补齐。这里保持原状而不是死循环。
                _log.warning(
                    "对端 %s 的游标无法前进（同刻记录已满一页且响应无 seq）："
                    "本轮提前结束，剩余记录需对端升级后补齐",
                    peer.host,
                )
                break
            requested, requested_id = page_last
            advanced = page_last

            # 不满一页说明对端已经给完了，不必再多发一次请求
            if len(records) < DEFAULT_PAGE_SIZE:
                break

        if advanced is not None:
            self.cursors.advance(key, advanced[0], since_id=advanced[1])
        return total

    @staticmethod
    def _page_position(record: SyncRecord) -> Tuple[float, int]:
        """一条记录在服务端全序里的位置 `(created_at, seq)`。

        `seq` 缺失时按 0 处理：这时它等价于旧版本的纯时间戳游标。
        返回值与 `Storage.iter_since` 的字典序完全对应，两边不能各写一套。
        """
        return (
            float(getattr(record, "created_at", 0.0) or 0.0),
            max(0, int(getattr(record, "seq", 0) or 0)),
        )


class DiscoveryService:
    """可选的 UDP 广播发现：同网段设备互相通告 `host:port`。

    默认不启用——不少 Wi-Fi 会拦组播/广播，显式填地址更可靠。
    """

    def __init__(
        self,
        device_name: str,
        port: int,
        *,
        discovery_port: int = DISCOVERY_PORT,
        token_required: bool = False,
    ) -> None:
        self.device_name = device_name
        self.port = port
        self.discovery_port = discovery_port
        self.token_required = token_required
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._peers: Dict[str, PeerInfo] = {}
        self._lock = threading.RLock()

    def start(self) -> bool:
        with self._lock:
            if self._sock is not None:
                return True
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.bind(("", self.discovery_port))
                sock.settimeout(0.5)
            except OSError as exc:
                _log.warning("局域网发现启动失败：%s", exc)
                return False
            self._sock = sock
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="lan-discovery", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        with self._lock:
            sock, thread = self._sock, self._thread
            self._sock, self._thread = None, None
        self._stop.set()
        if sock is not None:
            sock.close()
        if thread is not None:
            thread.join(1.0)

    @property
    def peers(self) -> List[PeerInfo]:
        with self._lock:
            return list(self._peers.values())

    def _payload(self) -> bytes:
        payload = {
            "magic": DISCOVERY_MAGIC,
            "device": self.device_name,
            "port": self.port,
            "auth_required": self.token_required,
        }
        # 与 `/ping` 同一份字段：广播说协议 2、握手说协议 1 这种自相矛盾
        # 是无从排查的，所以两处共用 `protocol.advertised_payload()`。
        payload.update(protocol.advertised_payload())
        return json.dumps(payload).encode("utf-8")

    def announce(self) -> None:
        """主动广播一次自身信息（也用于让对端知道我们在线）。"""
        sock = self._sock
        if sock is None:
            return
        try:
            sock.sendto(self._payload(), ("255.255.255.255", self.discovery_port))
        except OSError as exc:
            _log.debug("广播发现包失败：%s", exc)

    def _loop(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                self.announce()
                continue
            except OSError:
                return
            peer = self._parse(data, addr)
            if peer is not None:
                with self._lock:
                    self._peers[f"{peer.host}:{peer.port}"] = peer

    def _parse(self, data: bytes, addr) -> Optional[PeerInfo]:
        try:
            payload = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("magic") != DISCOVERY_MAGIC:
            return None
        device = str(payload.get("device", ""))
        if not device or device == self.device_name:
            return None  # 自己发的包
        try:
            port = int(payload.get("port") or 0)
        except (TypeError, ValueError):
            return None
        if port <= 0:
            return None
        # 广播里也带协议版本，所以**没握手也能先知道**对端说的是哪个版本 ——
        # 状态页因此能在同步发生之前就提示「这台机器和你不是一个版本」。
        raw_version = payload.get("protocol", "")
        verdict = protocol.peer_speaks_our_version(raw_version)
        return PeerInfo(
            host=addr[0],
            port=port,
            device=device,
            last_seen=time.time(),
            ok=True,
            protocol=str(raw_version or ""),
            compatibility=verdict,
        )


@dataclass
class LanSync:
    """局域网同步门面：把服务端、客户端与配置串成一次可调用的同步。

    刻意与 `FolderSync` 保持同样的对外形状（`is_ready` / `status` / 计数返回），
    这样 `Application` 里两套模式可以互换而不必分支。
    """

    storage: Storage
    device_name: str = ""
    token: str = ""
    port: int = 0
    peers: List[str] = field(default_factory=list)
    auto_discovery: bool = False
    cipher: Optional[SyncCipher] = None
    guard: Optional[PrivacyGuard] = None
    cursors: Optional[SyncCursorStore] = None
    tombstones: Optional[TombstoneStore] = None
    flag_changes: Optional[FlagChangeStore] = None
    _server: Optional[LanSyncServer] = field(default=None, init=False)
    _client: Optional[LanSyncClient] = field(default=None, init=False)
    _discovery: Optional[DiscoveryService] = field(default=None, init=False)

    def __post_init__(self) -> None:
        name = self.device_name or socket.gethostname()
        self.device_name = name
        cipher = self.cipher or SyncCipher(False)
        tombstones = self.tombstones if self.tombstones is not None else TombstoneStore()
        self.tombstones = tombstones
        flag_changes = (
            self.flag_changes if self.flag_changes is not None else FlagChangeStore()
        )
        self.flag_changes = flag_changes
        self._server = LanSyncServer(
            self.storage,
            device_name=name,
            token=self.token,
            port=self.port,
            cipher=cipher,
            guard=self.guard,
            tombstones=tombstones,
            flag_changes=flag_changes,
        )
        cursors = self.cursors if self.cursors is not None else SyncCursorStore()
        self.cursors = cursors
        self._client = LanSyncClient(
            self.storage,
            device_name=name,
            token=self.token,
            cipher=cipher,
            guard=self.guard,
            cursors=cursors,
            tombstones=tombstones,
            flag_changes=flag_changes,
        )
        # 端口为 0 时先不固定，等 start() 拿到系统分配的端口再用于发现广播
        self._cipher = cipher

    # ---- 生命周期 ----

    def start(self) -> bool:
        assert self._server is not None
        if not self._server.start() and self.port != 0:
            return False
        if self.auto_discovery:
            self._discovery = DiscoveryService(
                self.device_name,
                self._server.bound_port,
                token_required=bool(self.token),
            )
            self._discovery.start()
        return True

    def stop(self) -> None:
        if self._discovery is not None:
            self._discovery.stop()
            self._discovery = None
        if self._server is not None:
            self._server.stop()

    @property
    def running(self) -> bool:
        return bool(self._server and self._server.running)

    # ---- 同步 ----

    def is_ready(self) -> bool:
        return self._server is not None

    def discover(self) -> List[PeerInfo]:
        """返回当前可用的对端列表：配置里显式给的 + 广播发现的。

        每次调用都会重新构造 `PeerInfo`，所以**上次握手得到的版本信息
        会由这里补回去**：不然一轮同步结束后再问状态，屏幕上写的是
        「协议未知、兼容」—— 而刚才明明已经查明它不兼容。
        握过手的结论是这台设备的事实，不该因为重新枚举一次对端就丢掉。
        """
        found: List[PeerInfo] = []
        seen = set()
        known = getattr(self, "_known_protocols", {})
        assert self._server is not None
        for spec in self.peers:
            parsed = parse_peer(spec, self._server.bound_port)
            if parsed is None:
                continue
            key = f"{parsed[0]}:{parsed[1]}"
            if key in seen:
                continue
            seen.add(key)
            peer = PeerInfo(host=parsed[0], port=parsed[1])
            self._remember_protocol(key, peer)
            found.append(peer)
        if self._discovery is not None:
            for peer in self._discovery.peers:
                key = f"{peer.host}:{peer.port}"
                if key in seen:
                    continue
                seen.add(key)
                found.append(peer)
        return found

    def _remember_protocol(self, key: str, peer: PeerInfo) -> None:
        """把「这台对端说的是哪个版本」记下来，供后续重新枚举时补回。

        登记表就放在客户端上（`LanSyncClient._known_protocols`）——
        门面自己再存一份的话，两边迟早分叉：一处记着「协议 2、不兼容」，
        另一处什么都不知道，而屏幕上最终印出来的是**后者**。
        """
        assert self._client is not None
        if peer.protocol or peer.compatibility:
            self._client._note_protocol(peer)
            return
        version, verdict, reason = self._client.peer_protocol(peer)
        if version or verdict:
            peer.protocol, peer.compatibility = version, verdict
            # 说明也要补回：否则状态页会出现「不兼容但错误栏是空的」，
            # 用户能看到有问题、却看不到问题是哪一台机器的哪个版本。
            peer.error = peer.error or reason

    def sync_all(self, *, since: Optional[float] = None) -> int:
        """对所有已知对端拉取一轮，返回导入总条数。

        `since` 为 `None`（默认）时逐对端使用各自的持久化游标，
        即每轮只拉取上次同步之后的新记录；显式传入则对所有对端生效。
        """
        return self.sync_all_detailed(since=since).imported

    def sync_all_detailed(self, *, since: Optional[float] = None) -> "LanImportOutcome":
        """同 `sync_all`，但返回明细。

        保留 `sync_all` 的整数返回是刻意的：它是既有公开形状，改签名会
        影响所有调用方与用例。新加的「标记更新数」不该让老调用点被迫改写，
        但 CLI 需要看到它 —— 否则「同步了 0 条」既可能是「没什么可同步」，
        也可能是「只更新了标记」，两者对用户意味着完全不同的事。
        """
        assert self._client is not None
        total = LanImportOutcome()
        for peer in self.discover():
            try:
                outcome = self._client.sync_peer_detailed(peer, since=since)
            except Exception as exc:  # noqa: BLE001 - 单个对端异常不影响其它对端
                _log.debug("同步对端 %s 出错：%s", peer.host, exc)
                continue
            # 对端下的结论（尤其「协议不兼容」）要带回门面里登记：
            # 这里每轮都新建 `PeerInfo`，不登记的话下一次问状态时
            # 屏幕上又会写成「协议未知、兼容」—— 而刚刚才查明它不兼容。
            self._remember_protocol(f"{peer.host}:{peer.port}", peer)
            total.imported += outcome.imported
            total.removed += outcome.removed
            total.tombstones += outcome.tombstones
            total.flags_updated += outcome.flags_updated
            total.flag_changes += outcome.flag_changes
        return total

    def fetch_missing_images(self, *, limit: int = 20) -> "ImageFetchOutcome":
        """对缺失的图片逐张向对端索取，直到全部补齐或达到单次上限。

        为什么需要单次上限：历史里可能积了几千张图（换过机器、同步盘丢过
        文件），一次全拉会把界面卡住、也会把对端打满。上限之外的部分
        下一轮继续 —— 库里没有的那张图，只要对端还在，随时都能再要。

        顺序是「先本地已有的对端，再其它对端」，并且**一张图只问一台**：
        同一张图在多个对端都有时（同一份剪贴板同步到了三台设备），
        挨个问一遍只是白白多跑两次网络。
        """
        assert self._client is not None
        outcome = ImageFetchOutcome()
        peers = self.discover()
        if not peers:
            return outcome

        images_dir = self.storage.images_dir
        wanted = image_transfer.pending_digests(
            self.storage.iter_all(batch=512), images_dir
        )
        outcome.missing = len(wanted)
        if not wanted:
            return outcome

        for digest in wanted:
            if outcome.fetched + outcome.failed >= limit:
                outcome.truncated = True
                break
            data = None
            for peer in peers:
                # 每个对端都试一次，是因为「哪个对端有这张图」本机并不知道 ——
                # 记录是从某台设备同步来的，但图可能后来在别的设备上也存在。
                # 失败一次不算结论：可能只是那台恰好没这张图。
                data = self._client.fetch_image(peer, digest)
                if data is not None:
                    break
            if data is None:
                outcome.failed += 1
                continue
            result = image_transfer.store(images_dir, digest, data)
            if not result.stored:
                outcome.failed += 1
                continue
            adopted = self._adopt_image(digest, result.path)
            if adopted:
                outcome.fetched += 1
                outcome.adopted += adopted
            else:
                # 字节拿到了、但没有记录引用它 —— 落盘是为了下次用，
                # 计入 `orphaned` 而不是 `fetched`，免得报告看着像补齐了记录。
                outcome.orphaned += 1
        return outcome

    def _adopt_image(self, digest: str, relative: str) -> int:
        """图片字节到位后，给引用它的记录补上 `image_path`。

        为什么需要这一步：同步过来的记录只带 `image_digest`，`image_path`
        是空的（那时本机确实没有那份文件）。字节落地后不回填，界面就永远
        停在「这条图片打不开」—— 而这正是本轮要消灭的症状。

        只动**确实引用这个摘要、且当前没有 `image_path`** 的记录：
        本机自己采集的图早已有路径，回填会是一次无意义的写入
        （还会顺带把 `updated_at` 推新，让列表顺序莫名其妙地动）。
        """
        storage = self.storage
        count = 0
        for item in storage.iter_all(batch=512):
            if image_transfer.digest_of_item(item) != digest:
                continue
            if getattr(item, "image_path", ""):
                continue
            if image_transfer.normalize_digest(item.metadata.get("image_digest", "")) != digest:
                continue
            storage.update_fields(item.id, image_path=relative)
            count += 1
        return count

    def probe(self, peer: PeerInfo) -> bool:
        """探测某对端是否在线（用 `/ping`，不需要口令）。

        `/ping` 同时也是握手，所以这里顺手把协议版本判定下来 ——
        状态页因此能在**同步发生之前**就说清「这台机器和你不是一个版本」，
        而不是等用户发现「同步永远是 0 条」之后才回头猜。
        """
        url = f"{peer.base_url}/ping"
        try:
            with urllib.request.urlopen(url, timeout=2.0) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
            peer.ok = False
            return False
        peer.ok = True
        peer.device = str(data.get("device", ""))
        peer.last_seen = time.time()
        raw_version = data.get("protocol", "")
        peer.protocol = str(raw_version or "")
        peer.compatibility = protocol.peer_speaks_our_version(raw_version)
        if not protocol.is_usable(peer.compatibility):
            peer.error = protocol.describe(raw_version, peer.compatibility)
        elif not peer.error:
            peer.error = ""
        assert self._client is not None
        self._client._note_protocol(peer)
        return True

    def status(self, *, probe: bool = True) -> Dict[str, object]:
        """同步状态摘要。

        `probe=True`（默认）时逐对端打一次 `/ping`。此前每个对端的
        `ok` / `device` 永远是空的 —— `probe()` 写好了却从来没人调用，
        于是状态页只能列出地址，说不出「哪台在线、它是谁」。
        """
        assert self._server is not None
        peers = self.discover()
        if probe:
            for peer in peers:
                try:
                    self.probe(peer)
                except Exception as exc:  # noqa: BLE001 - 探测失败只能说明不在线
                    _log.debug("探测对端 %s 失败：%s", peer.host, exc)
                    peer.ok = False
        return {
            "mode": "lan",
            "running": self.running,
            "device": self.device_name,
            "port": self._server.bound_port,
            "encrypted": self._server.encrypted,
            "auth_required": bool(self.token),
            "auto_discovery": self.auto_discovery,
            "peers": [peer.to_json() for peer in peers],
            "online": sum(1 for peer in peers if peer.ok),
            "tombstone_file": str(self.tombstones.path),
            "pending_tombstones": len(self.tombstones.pending()),
            "cursor_file": str(self.cursors.path) if self.cursors is not None else "",
        }


def build_lan_sync(
    storage: Storage,
    config,
    guard: Optional[PrivacyGuard] = None,
    cursors: Optional[SyncCursorStore] = None,
    tombstones: Optional[TombstoneStore] = None,
    flag_changes: Optional[FlagChangeStore] = None,
) -> Optional[LanSync]:
    """按配置构造局域网同步；未启用时返回 `None`。"""
    if not config.get("sync.enabled", False):
        return None
    if str(config.get("sync.mode", "folder") or "folder").lower() != "lan":
        return None

    device_name = str(config.get("sync.device_name", "") or "").strip() or socket.gethostname()
    cipher = SyncCipher(
        bool(config.get("sync.encrypt", True)),
        password=str(config.get("sync.lan_token", "") or device_name),
    )
    raw_peers = config.get("sync.lan_peers", []) or []
    if isinstance(raw_peers, str):
        raw_peers = [line for line in raw_peers.splitlines() if line.strip()]

    return LanSync(
        storage=storage,
        device_name=device_name,
        token=str(config.get("sync.lan_token", "") or ""),
        port=int(config.get("sync.lan_port", 48620) or 0),
        peers=[str(peer) for peer in raw_peers],
        auto_discovery=bool(config.get("sync.lan_discovery", False)),
        cipher=cipher,
        guard=guard,
        cursors=cursors,
        tombstones=tombstones,
        flag_changes=flag_changes,
    )
