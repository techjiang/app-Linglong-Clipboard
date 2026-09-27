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
from .models import ClipboardItem
from .privacy import PrivacyGuard
from .storage import Storage
from .sync import MAX_SYNC_ENTRIES, FolderSync, SyncCipher, SyncRecord

_log = get_logger("lan_sync")

#: 协议版本，出现在 URL 与响应里，便于将来做兼容分支
PROTOCOL_VERSION = "1"

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

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def to_json(self) -> Dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "device": self.device,
            "last_seen": self.last_seen,
            "ok": self.ok,
            "error": self.error,
        }


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
            # 探测接口不校验口令：只回设备名与版本，不含任何内容
            self._send_json(
                200,
                {
                    "ok": True,
                    "protocol": PROTOCOL_VERSION,
                    "device": owner.device_name,
                    "auth_required": bool(owner.token),
                },
            )
            return

        if not self._authorized():
            _log.warning("局域网同步请求鉴权失败：%s", self.client_address[0])
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        if path == "/records":
            params = urllib.parse.parse_qs(parsed.query)
            try:
                since = float(params.get("since", ["0"])[0])
            except ValueError:
                since = 0.0
            try:
                limit = int(params.get("limit", [str(DEFAULT_PAGE_SIZE)])[0])
            except ValueError:
                limit = DEFAULT_PAGE_SIZE
            limit = max(1, min(limit, MAX_SYNC_ENTRIES))
            records = owner.collect_records(since=since, limit=limit)
            self._send_json(
                200,
                {
                    "ok": True,
                    "protocol": PROTOCOL_VERSION,
                    "device": owner.device_name,
                    "encrypted": owner.encrypted,
                    "records": records,
                },
            )
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
    ) -> None:
        self._storage = storage
        self.device_name = device_name or socket.gethostname()
        self.token = token
        self.host = host
        self.port = int(port)
        self._cipher = cipher or SyncCipher(False)
        self._guard = guard
        self._max_entries = max_entries
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

    def collect_records(self, *, since: float = 0.0, limit: int = DEFAULT_PAGE_SIZE) -> List[str]:
        """把最近记录编码成 `SyncRecord` JSON 串，供 HTTP 响应携带。

        返回字符串列表而不是对象，是因为加密后的载荷就是字节串，
        序列化成 base64 由客户端解——明文模式下则是普通 JSON 串。
        """
        items = self._eligible_items(since=since, limit=limit)
        if not items:
            return []

        payload = [
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
            for item in items
        ]
        body = "\n".join(
            json.dumps(record.to_json(), ensure_ascii=False) for record in payload
        ).encode("utf-8")

        if not self._cipher.enabled:
            return [json.dumps(record.to_json(), ensure_ascii=False) for record in payload]

        # 加密模式：整块加密后 base64 成一行，客户端按同一密钥解
        import base64

        blob = self._cipher.encrypt(body)
        return ["ENC:" + base64.b64encode(blob).decode("ascii")]

    def _eligible_items(self, *, since: float, limit: int) -> List[ClipboardItem]:
        """挑选可外发的记录：过隐私、跳过图片与敏感项，且晚于 `since`。"""
        out: List[ClipboardItem] = []
        for item in self._storage.list(limit=self._max_entries):
            if not item.hash or item.created_at <= since:
                continue
            if item.kind == "image" or item.image_path:
                continue  # 图片不进局域网，体积大且隐私风险高
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
    ) -> None:
        self._storage = storage
        self.device_name = device_name or socket.gethostname()
        self.token = token
        self._cipher = cipher or SyncCipher(False)
        self._guard = guard
        self._timeout = timeout

    def fetch(self, peer: PeerInfo, *, since: float = 0.0, limit: int = DEFAULT_PAGE_SIZE) -> Optional[List[SyncRecord]]:
        """拉取一个对端的记录；不可达或鉴权失败时返回 `None`。"""
        query = urllib.parse.urlencode({"since": f"{since:.6f}", "limit": limit})
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
        return self._decode_records(data.get("records") or [])

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

    def import_records(self, records: Iterable[SyncRecord]) -> int:
        """写入本地库，跳过重复、自己的记录与隐私策略不允许的内容。"""
        seen = {item.hash for item in self._storage.list(limit=MAX_SYNC_ENTRIES)}
        imported = 0
        for record in records:
            if not record.hash or record.hash in seen:
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
            imported += 1
        return imported

    def sync_peer(self, peer: PeerInfo, *, since: float = 0.0) -> int:
        """拉取并导入单个对端，返回导入条数（失败时 0）。"""
        records = self.fetch(peer, since=since)
        if not records:
            return 0
        return self.import_records(records)


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
        return json.dumps(
            {
                "magic": DISCOVERY_MAGIC,
                "protocol": PROTOCOL_VERSION,
                "device": self.device_name,
                "port": self.port,
                "auth_required": self.token_required,
            }
        ).encode("utf-8")

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
        return PeerInfo(host=addr[0], port=port, device=device, last_seen=time.time(), ok=True)


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
    _server: Optional[LanSyncServer] = field(default=None, init=False)
    _client: Optional[LanSyncClient] = field(default=None, init=False)
    _discovery: Optional[DiscoveryService] = field(default=None, init=False)

    def __post_init__(self) -> None:
        name = self.device_name or socket.gethostname()
        self.device_name = name
        cipher = self.cipher or SyncCipher(False)
        self._server = LanSyncServer(
            self.storage,
            device_name=name,
            token=self.token,
            port=self.port,
            cipher=cipher,
            guard=self.guard,
        )
        self._client = LanSyncClient(
            self.storage,
            device_name=name,
            token=self.token,
            cipher=cipher,
            guard=self.guard,
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
        """返回当前可用的对端列表：配置里显式给的 + 广播发现的。"""
        found: List[PeerInfo] = []
        seen = set()
        assert self._server is not None
        for spec in self.peers:
            parsed = parse_peer(spec, self._server.bound_port)
            if parsed is None:
                continue
            key = f"{parsed[0]}:{parsed[1]}"
            if key in seen:
                continue
            seen.add(key)
            found.append(PeerInfo(host=parsed[0], port=parsed[1]))
        if self._discovery is not None:
            for peer in self._discovery.peers:
                key = f"{peer.host}:{peer.port}"
                if key in seen:
                    continue
                seen.add(key)
                found.append(peer)
        return found

    def sync_all(self, *, since: float = 0.0) -> int:
        """对所有已知对端拉取一轮，返回导入总条数。"""
        assert self._client is not None
        imported = 0
        for peer in self.discover():
            try:
                imported += self._client.sync_peer(peer, since=since)
            except Exception as exc:  # noqa: BLE001 - 单个对端异常不影响其它对端
                _log.debug("同步对端 %s 出错：%s", peer.host, exc)
        return imported

    def probe(self, peer: PeerInfo) -> bool:
        """探测某对端是否在线（用 `/ping`，不需要口令）。"""
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
        return True

    def status(self) -> Dict[str, object]:
        assert self._server is not None
        return {
            "mode": "lan",
            "running": self.running,
            "device": self.device_name,
            "port": self._server.bound_port,
            "encrypted": self._server.encrypted,
            "auth_required": bool(self.token),
            "auto_discovery": self.auto_discovery,
            "peers": [peer.to_json() for peer in self.discover()],
        }


def build_lan_sync(storage: Storage, config, guard: Optional[PrivacyGuard] = None) -> Optional[LanSync]:
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
    )
