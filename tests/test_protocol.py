"""协议协商：让 `PROTOCOL_VERSION` 成为一句会被读到的话。

这个缺陷的形状：`PROTOCOL_VERSION = "1"` 出现在四个地方（HTTP 的
`Server` 头、`/ping` 响应、`/records` 响应、UDP 广播），而**没有任何
一处会读它**。它是印在产品上的版本号，不是一句参与判断的话。

后果不是「不兼容」，而是**不兼容被伪装成正常**：未来字段名的载荷在本机
解密成功、每行都是合法 JSON、字段读不到，于是逐条跳过 —— 导入 0 条、
HTTP 200、`peer.ok = True`。用户看到「同步成功了」，而不是
「这两台机器说的不是同一种话」。
"""

from __future__ import annotations

import base64
import json
import threading

import pytest

from linglong_clip.core import protocol
from linglong_clip.core.lan_sync import (
    PROTOCOL_VERSION,
    LanSync,
    LanSyncClient,
    PeerInfo,
)
from linglong_clip.core.models import ClipboardItem
from linglong_clip.core.privacy import PrivacyGuard
from linglong_clip.core.sync import SyncCipher


# ---------- 版本解析：故意窄 ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", (1, 0)),
        ("1.0", (1, 0)),
        ("2.3", (2, 3)),
        (" 2 ", (2, 0)),  # 两侧空白不算错
        ("1.0.0", None),  # 段数超过 2：认不出来
        ("v1", None),
        ("", None),
        ("   ", None),
        (None, None),
        (1, None),  # 数字而不是字符串
        ([1], None),
        ("１", None),  # 全角数字：isdigit 会认，必须再挡一层
        ("1.-1", None),
    ],
)
def test_parse_version_is_deliberately_narrow(raw, expected):
    assert protocol.parse_version(raw) == expected


def test_version_constant_is_read_from_one_place():
    """`lan_sync.PROTOCOL_VERSION` 与 `protocol.PROTOCOL_VERSION` 是同一个值。

    两处各持一份是前几轮反复出事的结构（`metadata["copies"]`、
    `native_history`、`DENSITY_OPTIONS` 都是同一个形状）：
    口径没收在一处，而错的那一处恰好不报错。
    """
    assert PROTOCOL_VERSION == protocol.PROTOCOL_VERSION


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", protocol.COMPATIBLE),
        ("1.0", protocol.COMPATIBLE),
        ("1.9", protocol.COMPATIBLE),  # 次版本只增不改语义
        ("2", protocol.INCOMPATIBLE),
        ("99", protocol.INCOMPATIBLE),
        ("", protocol.UNREPORTED),
        (None, protocol.UNREPORTED),
        ("v1", protocol.MALFORMED),
        ("1.0.0", protocol.MALFORMED),
    ],
)
def test_verdict(raw, expected):
    assert protocol.peer_speaks_our_version(raw) == expected


def test_unreported_is_usable_but_malformed_is_not():
    """没报版本 = 协议 1 时期的老版本，按 1 处理；认不出来的一律不认。

    这两档刻意分开：老版本是**确定**的（历史上只有过协议 1），
    垃圾值是不确定的。把不确定的也当「说得来」，就是按自己的心意
    去读一份不知道是什么的东西 —— 那正是要消灭的事。
    """
    assert protocol.is_usable(protocol.UNREPORTED)
    assert protocol.is_usable(protocol.COMPATIBLE)
    assert not protocol.is_usable(protocol.INCOMPATIBLE)
    assert not protocol.is_usable(protocol.MALFORMED)


def test_describe_says_which_and_what_to_do():
    """给人看的话里要有：对端版本、本机版本、该干什么。"""
    text = protocol.describe("2")
    assert "2" in text and PROTOCOL_VERSION in text
    assert "升到同一版本" in text

    unknown = protocol.describe("v1")
    assert "v1" in unknown, "要说清对端报的是什么，用户才知道该去看哪台机器"


# ---------- 服务端：说不拢就不给数据 ----------


@pytest.fixture()
def live_server(storage, home):
    """一个真起在那里的对端节点，返回 (节点, 端口, 存储)。"""
    from linglong_clip.core.storage import Storage

    remote = Storage(home / "peer.db")
    for index in range(3):
        remote.add(
            ClipboardItem(
                kind="text",
                content=f"记录 {index}",
                preview=f"{index}",
                hash=f"h{index}",
            )
        )
    node = LanSync(
        storage=remote,
        device_name="laptop",
        token="pw",
        port=0,
        cipher=SyncCipher(False),
        guard=PrivacyGuard(),
    )
    assert node.start()
    yield node, node.status()["port"], remote
    node.stop()
    remote.close()


def _get(port: int, path: str, token: str = "pw"):
    import urllib.error
    import urllib.request

    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token:
        request.add_header("X-Linglong-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_ping_reports_the_version_it_will_actually_enforce(live_server):
    """`/ping` 报的版本必须就是 `/records` 会拿来判的版本。

    此前它只往响应里塞一个字符串，没有任何一处会读 —— 于是「报的版本」
    与「实际按哪个版本解析」可以毫不相干，而且看不出来。
    """
    _node, port, _remote = live_server
    status, body = _get(port, "/ping", token="")
    assert status == 200
    assert body["protocol"] == PROTOCOL_VERSION


@pytest.mark.parametrize("raw", ["1", "1.0", "", None])
def test_records_accepts_our_version_and_silent_old_clients(live_server, raw):
    """本机版本与「不报版本的老客户端」都要放行。

    反向守一条：不能为了拦不兼容，把唯一存在过的老版本也一起拦掉。
    """
    _node, port, _remote = live_server
    query = f"protocol={raw}&since=0&since_id=0&limit=10" if raw else "since=0&since_id=0&limit=10"
    status, body = _get(port, f"/records?{query}")
    assert status == 200
    assert len(body["records"]) == 3


@pytest.mark.parametrize("raw", ["2", "99", "v1", "1.0.0"])
def test_records_refuses_what_it_cannot_read(live_server, raw):
    """读不懂的版本直接拒，并说清为什么 —— 而不是给一份读不懂的数据。

    最关键的一条：**拒的时候不能给 records**。给了的话客户端会解析出
    0 条，然后把它当成「对端没有东西可同步」—— 那就又回到了
    「不兼容被伪装成正常」。
    """
    _node, port, _remote = live_server
    status, body = _get(port, f"/records?protocol={raw}&since=0&since_id=0&limit=10")
    assert status == 426, "426 Upgrade Required 的语义正是「你的协议对不上」"
    assert body["error"] == "protocol-mismatch"
    assert "records" not in body
    assert body["server_protocol"] == PROTOCOL_VERSION
    assert body["client_protocol"] == raw
    assert body["detail"], "必须给出能直接印给用户看的理由"


def test_refusal_happens_before_reading_anything(live_server):
    """版本校验排在读取之前：拒掉的请求不该碰数据，也不该确认送达。"""
    node, port, _remote = live_server
    node.tombstones.record("h-deleted")
    before = len(node.tombstones.pending())
    assert before == 1

    status, _body = _get(port, "/records?protocol=99&since=0&since_id=0&limit=10")
    assert status == 426
    assert len(node.tombstones.pending()) == before, "被拒的请求不该把删除标记当成已送达"


def test_status_reports_the_refusal_reason(live_server):
    """状态摘要要能说清「哪台不兼容、为什么」。"""
    node, _port, _remote = live_server
    peer = PeerInfo(host="127.0.0.1", port=node.status()["port"])
    assert node.probe(peer) is True
    payload = peer.to_json()
    assert payload["protocol"] == PROTOCOL_VERSION
    assert payload["compatible"] is True


# ---------- 客户端：假对端 ----------


def _fake_peer(records, *, version="1", device="future", encrypted=False, raw_body=None):
    """起一个只回固定内容的对端，返回 (地址, 收到的请求列表)。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: A003
            pass

        def do_GET(self):  # noqa: N802
            seen.append(self.path)
            if self.path.startswith("/ping"):
                body = json.dumps(
                    {
                        "ok": True,
                        "device": device,
                        "auth_required": False,
                        "protocol": version,
                    }
                ).encode()
            elif raw_body is not None:
                body = raw_body
            else:
                body = json.dumps(
                    {
                        "ok": True,
                        "device": device,
                        "protocol": version,
                        "encrypted": encrypted,
                        "records": records,
                    }
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return ("127.0.0.1", server.server_address[1]), seen, server


def test_future_peer_with_renamed_fields_is_not_reported_as_success(home):
    """未来字段名的载荷，**不能**被报成「同步成功、对端没东西」。

    这是本轮要修的那个缺陷的原样复现：解密成功、每行都是合法 JSON、
    字段读不到，于是逐条跳过 —— 导入 0 条、peer.ok=True、error=""。
    用户看到的是「同步成功了」，而不是「这两台机器说的不是同一种话」。
    """
    from linglong_clip.core.storage import Storage

    cipher = SyncCipher(True, password="shared")
    # 未来版本：一行一条不变，但字段名从 hash/content 换成了别的
    future = "\n".join(
        json.dumps({"fingerprint": f"h{index}", "text": f"未来的记录 {index}"})
        for index in range(3)
    ).encode()
    payload = ["ENC:" + base64.b64encode(cipher.encrypt(future)).decode()]

    (host, port), seen, server = _fake_peer(payload, version="2", encrypted=True)
    store = Storage(home / "local.db")
    client = LanSyncClient(store, device_name="me", cipher=cipher)
    peer = PeerInfo(host=host, port=port)

    outcome = client.sync_peer_detailed(peer)

    assert outcome.imported == 0
    assert peer.ok is False, "不能报成「在线且一切正常」"
    assert peer.compatibility == protocol.INCOMPATIBLE
    assert peer.protocol == "2"
    assert "2" in peer.error and PROTOCOL_VERSION in peer.error
    # 一条都没拉：不兼容时根本不该发 /records
    assert not any(path.startswith("/records") for path in seen), seen
    store.close()
    server.shutdown()


def test_handshake_runs_before_reading_data(home):
    """握手排在读数据之前，且 `/records` 会带上自己的版本。"""
    from linglong_clip.core.storage import Storage

    (host, port), seen, server = _fake_peer([])
    store = Storage(home / "local.db")
    client = LanSyncClient(store, device_name="me")
    peer = PeerInfo(host=host, port=port)
    client.sync_peer_detailed(peer)

    assert seen[0].startswith("/ping"), "第一步必须是握手"
    records_calls = [path for path in seen if path.startswith("/records")]
    assert records_calls, seen
    assert f"protocol={PROTOCOL_VERSION}" in records_calls[0], (
        "报上自己的版本，对端才有依据判断能不能把自己读明白"
    )
    store.close()
    server.shutdown()


def test_server_refusal_reaches_the_user_as_a_sentence(home):
    """对端用 426 拒绝时，理由是服务端写的那句人话，不是 `HTTP 426`。"""
    import urllib.error

    from linglong_clip.core.storage import Storage

    store = Storage(home / "local.db")
    client = LanSyncClient(store, device_name="me")
    peer = PeerInfo(host="127.0.0.1", port=1)

    detail = "对端说的是协议 2，本机是 1 —— 版本不一致，请把两台机器升到同一版本"
    body = json.dumps(
        {"ok": False, "error": "protocol-mismatch", "detail": detail}
    ).encode()
    exc = urllib.error.HTTPError("http://x/records", 426, "Upgrade Required", {}, None)
    exc.read = lambda: body  # type: ignore[method-assign]
    assert LanSyncClient._read_mismatch_detail(exc) == detail
    store.close()


def test_learned_version_survives_reenumeration(home):
    """握过手的结论不能因为重新枚举一次对端就丢掉。

    `discover()` 每次都新建 `PeerInfo`，只在对象上写版本等于写在便签上。
    症状是：一轮同步刚查明「不兼容」，紧接着问状态，屏幕上写着
    「协议未知、兼容」—— 而它刚刚才被查出来不兼容。
    """
    from linglong_clip.core.storage import Storage

    (host, port), _seen, server = _fake_peer([], version="2")
    store = Storage(home / "local.db")
    node = LanSync(storage=store, device_name="me", port=0, guard=PrivacyGuard())
    assert node.start()
    node.peers = [f"{host}:{port}"]

    assert node.sync_all() == 0
    # 重新枚举之后仍然记得
    peers = node.discover()
    assert len(peers) == 1
    assert peers[0].protocol == "2"
    assert peers[0].compatibility == protocol.INCOMPATIBLE
    assert peers[0].usable is False
    assert peers[0].error, "不兼容但错误栏是空的，用户就不知道该改什么"

    node.stop()
    store.close()
    server.shutdown()


def test_status_page_sees_incompatibility_before_any_sync(home):
    """状态页要能**在同步发生之前**就看出不兼容。

    只在同步时才发现的话，用户要等一轮跑完、看到「0 条」才有线索 ——
    而 0 条本身完全看不出问题。`probe()` 走的就是 `/ping`，顺手把
    版本判定下来。
    """
    from linglong_clip.core.storage import Storage

    (host, port), _seen, server = _fake_peer([], version="99")
    store = Storage(home / "local.db")
    node = LanSync(storage=store, device_name="me", port=0, guard=PrivacyGuard())
    assert node.start()
    node.peers = [f"{host}:{port}"]

    payload = node.status()["peers"][0]
    assert payload["compatible"] is False
    assert payload["protocol"] == "99"
    assert "99" in payload["error"]

    node.stop()
    store.close()
    server.shutdown()
