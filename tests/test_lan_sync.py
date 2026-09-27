"""局域网直连同步：协议、鉴权、加密与隐私边界。

用真实 socket 在 127.0.0.1 上起服务，验证的是端到端链路，
不是对 mock 的断言——端口用 0 让系统分配，避免测试互相抢端口。
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from linglong_clip.core.lan_sync import (
    PROTOCOL_VERSION,
    DiscoveryService,
    LanSync,
    LanSyncClient,
    LanSyncServer,
    PeerInfo,
    build_lan_sync,
    parse_peer,
)
from linglong_clip.core.models import ClipboardItem
from linglong_clip.core.privacy import PrivacyGuard
from linglong_clip.core.sync import SyncCipher, SyncRecord


# ---------- 地址解析 ----------


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("192.168.1.5", ("192.168.1.5", 48620)),
        ("192.168.1.5:9000", ("192.168.1.5", 9000)),
        ("http://192.168.1.5:9000", ("192.168.1.5", 9000)),
        ("http://192.168.1.5:9000/", ("192.168.1.5", 9000)),
        ("laptop.local", ("laptop.local", 48620)),
        ("[::1]:9000", ("::1", 9000)),
        ("[fe80::1]", ("fe80::1", 48620)),
        ("", None),
        ("   ", None),
        ("http://", None),
    ],
)
def test_parse_peer(spec, expected):
    assert parse_peer(spec, 48620) == expected


# ---------- 服务端 / 客户端 ----------


def _add(storage, content, *, kind="text", digest=None, sensitive=False):
    return storage.add(
        ClipboardItem(
            kind=kind,
            content=content,
            preview=content[:40],
            hash=digest or content,
            sensitive=sensitive,
        )
    )


@pytest.fixture()
def nodes(storage, home):
    """两个独立存储 + 两个节点，测试结束后统一关闭。"""
    from linglong_clip.core.storage import Storage

    other = Storage(home / "peer.db")
    started = []

    def make(st, name, token="", cipher=None):
        node = LanSync(
            storage=st, device_name=name, token=token, port=0, cipher=cipher,
            guard=PrivacyGuard(),
        )
        assert node.start()
        started.append(node)
        return node

    yield make, storage, other
    for node in started:
        node.stop()
    other.close()


def test_sync_transfers_records(nodes):
    make, local, remote = nodes
    _add(local, "会议纪要 draft", digest="h1")
    _add(local, "https://cnb.cool/x", kind="link", digest="h2")

    a, b = make(local, "laptop", token="pw"), make(remote, "desktop", token="pw")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]

    assert b.sync_all() == 2
    assert sorted(i.content for i in remote.list()) == ["https://cnb.cool/x", "会议纪要 draft"]


def test_sync_is_idempotent(nodes):
    make, local, remote = nodes
    _add(local, "only once", digest="h1")
    a, b = make(local, "a", token="t"), make(remote, "b", token="t")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]

    assert b.sync_all() == 1
    assert b.sync_all() == 0
    assert remote.count() == 1


def test_sensitive_and_image_are_not_shared(nodes):
    make, local, remote = nodes
    _add(local, "正常内容", digest="h1")
    _add(local, "身份证信息", digest="h2", sensitive=True)
    _add(local, "截图", kind="image", digest="h3")

    a, b = make(local, "a", token="t"), make(remote, "b", token="t")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]

    assert b.sync_all() == 1
    assert [i.content for i in remote.list()] == ["正常内容"]


def test_image_item_with_path_is_excluded(storage, home):
    """即使 kind 不是 image，只要带 image_path 也不外发。"""
    from linglong_clip.utils.config import Config

    node = LanSyncServer(storage, device_name="a", guard=PrivacyGuard(Config(home / "c.json")))
    storage.add(
        ClipboardItem(kind="text", content="x", preview="x", hash="h1", image_path="/tmp/a.png")
    )
    assert node.collect_records() == []


def test_wrong_token_is_rejected(nodes):
    make, local, remote = nodes
    _add(local, "不能外泄", digest="h1")

    a = make(local, "a", token="correct")
    b = make(remote, "b", token="wrong")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]

    assert b.sync_all() == 0
    assert remote.count() == 0

    # discover() 每次都返回全新对象，所以直接对客户端持有的同一对象断言
    peer = PeerInfo(host="127.0.0.1", port=a.status()["port"])
    assert b._client.fetch(peer) is None
    assert peer.ok is False
    assert peer.error == "HTTP 401"


def test_empty_token_allows_access(nodes):
    make, local, remote = nodes
    _add(local, "内网可信", digest="h1")
    a, b = make(local, "a"), make(remote, "b")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]

    assert b.sync_all() == 1


def test_ping_does_not_require_token(nodes):
    make, local, remote = nodes
    a = make(local, "laptop", token="secret")
    peer = PeerInfo(host="127.0.0.1", port=a.status()["port"])

    client = LanSyncClient(remote, device_name="other")
    assert client is not None
    assert a.probe(peer) is True
    assert peer.device == "laptop"


def test_unreachable_peer_degrades(storage, home):
    from linglong_clip.core.storage import Storage

    client = LanSyncClient(storage, device_name="me", timeout=0.4)
    # 选一个几乎必然没人监听的端口
    peer = PeerInfo(host="127.0.0.1", port=9)
    assert client.fetch(peer) is None
    assert peer.ok is False
    assert client.sync_peer(peer) == 0


def test_server_is_read_only(nodes):
    make, local, remote = nodes
    a = make(local, "a", token="")
    port = a.status()["port"]

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/records", data=b"{}", method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=3)
    assert excinfo.value.code == 405


def test_unknown_path_returns_404(nodes):
    make, local, remote = nodes
    a = make(local, "a", token="")
    port = a.status()["port"]
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=3)
    assert excinfo.value.code == 404


def test_collect_records_respects_since_and_limit(nodes):
    make, local, remote = nodes
    _add(local, "第一条", digest="h1")
    _add(local, "第二条", digest="h2")
    _add(local, "第三条", digest="h3")

    server = LanSyncServer(local, device_name="a")
    assert len(server.collect_records()) == 3
    assert len(server.collect_records(limit=2)) == 2

    # since 是游标语义（排他）：传最新一条的时间戳，结果应为空
    newest = max(i.created_at for i in local.list())
    assert server.collect_records(since=newest) == []
    # 传一个早于所有记录的时间戳，三条都该返回
    assert len(server.collect_records(since=0.0)) == 3


# ---------- 加密 ----------


def test_encrypted_transfer_round_trips(nodes):
    make, local, remote = nodes
    _add(local, "加密内容", digest="h1")

    cipher_a = SyncCipher(True, password="pw")
    cipher_b = SyncCipher(True, password="pw")
    a = make(local, "a", token="t", cipher=cipher_a)
    b = make(remote, "b", token="t", cipher=cipher_b)
    b.peers = [f"127.0.0.1:{a.status()['port']}"]

    assert b.sync_all() == 1
    assert [i.content for i in remote.list()] == ["加密内容"]


def test_encrypted_payload_is_not_plaintext(nodes):
    make, local, remote = nodes
    _add(local, "明文不该出现在报文里", digest="h1")

    server = LanSyncServer(local, device_name="a", cipher=SyncCipher(True, password="pw"))
    payload = server.collect_records()
    assert len(payload) == 1
    assert payload[0].startswith("ENC:")
    assert "明文不该出现" not in payload[0]


def test_mismatched_key_yields_nothing(nodes):
    make, local, remote = nodes
    _add(local, "读不出来才对", digest="h1")

    a = make(local, "a", token="t", cipher=SyncCipher(True, password="one"))
    b = make(remote, "b", token="t", cipher=SyncCipher(True, password="two"))
    b.peers = [f"127.0.0.1:{a.status()['port']}"]

    assert b.sync_all() == 0
    assert remote.count() == 0


def test_plaintext_peer_to_encrypted_client(nodes):
    """一方明文一方加密时不应崩溃，只是拿不到内容。"""
    make, local, remote = nodes
    _add(local, "跨模式", digest="h1")

    a = make(local, "a", token="t")  # 明文
    b = make(remote, "b", token="t", cipher=SyncCipher(True, password="pw"))
    b.peers = [f"127.0.0.1:{a.status()['port']}"]

    # 明文记录对加密客户端来说仍是明文，能正常导入
    assert b.sync_all() == 1


# ---------- 发现 ----------


def test_discovery_ignores_self():
    service = DiscoveryService("me", 1234)
    payload = json.dumps(
        {"magic": "LINGLONG_CLIPBOARD_DISCOVER", "device": "me", "port": 1234}
    ).encode()
    assert service._parse(payload, ("127.0.0.1", 1)) is None


def test_discovery_parses_valid_announcement():
    service = DiscoveryService("me", 1234)
    payload = json.dumps(
        {"magic": "LINGLONG_CLIPBOARD_DISCOVER", "device": "other", "port": 9000}
    ).encode()
    peer = service._parse(payload, ("192.168.1.9", 1))
    assert peer is not None
    assert (peer.host, peer.port, peer.device) == ("192.168.1.9", 9000, "other")


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"{}",
        json.dumps({"magic": "WRONG", "device": "x", "port": 1}).encode(),
        json.dumps({"magic": "LINGLONG_CLIPBOARD_DISCOVER", "device": "", "port": 1}).encode(),
        json.dumps({"magic": "LINGLONG_CLIPBOARD_DISCOVER", "device": "x", "port": 0}).encode(),
        json.dumps({"magic": "LINGLONG_CLIPBOARD_DISCOVER", "device": "x"}).encode(),
    ],
)
def test_discovery_rejects_bad_payloads(payload):
    assert DiscoveryService("me", 1234)._parse(payload, ("1.2.3.4", 1)) is None


def test_discovery_start_stop_is_idempotent():
    service = DiscoveryService("me", 1234, discovery_port=0)
    # 端口 0 会被系统分配；重复 start/stop 不应抛异常
    assert service.start() is True
    assert service.start() is True
    service.stop()
    service.stop()


# ---------- 配置构造 ----------


def test_build_lan_sync_disabled(config):
    config.set("sync.enabled", False, persist=False)
    assert build_lan_sync(None, config) is None


def test_build_lan_sync_ignores_folder_mode(config):
    config.update({"sync.enabled": True, "sync.mode": "folder"}, persist=False)
    assert build_lan_sync(None, config) is None


def test_build_lan_sync_reads_config(storage, config):
    config.update(
        {
            "sync.enabled": True,
            "sync.mode": "lan",
            "sync.lan_port": 0,
            "sync.lan_token": "s3cret",
            "sync.lan_peers": ["192.168.1.8", "192.168.1.9:9000"],
            "sync.device_name": "my-laptop",
        },
        persist=False,
    )
    node = build_lan_sync(storage, config, PrivacyGuard(config))
    assert node is not None
    assert node.device_name == "my-laptop"
    assert node.token == "s3cret"
    assert node.peers == ["192.168.1.8", "192.168.1.9:9000"]


def test_build_lan_sync_accepts_peer_string(storage, config):
    config.update(
        {
            "sync.enabled": True,
            "sync.mode": "lan",
            "sync.lan_peers": "10.0.0.1\n10.0.0.2:9000\n",
        },
        persist=False,
    )
    node = build_lan_sync(storage, config)
    assert node is not None
    assert node.peers == ["10.0.0.1", "10.0.0.2:9000"]


# ---------- 生命周期 ----------


def test_start_stop_repeatedly(storage):
    node = LanSync(storage=storage, device_name="a", port=0)
    assert node.running is False
    assert node.start() is True
    first = node.status()["port"]
    assert node.status()["running"] is True
    node.stop()
    assert node.running is False
    assert node.start() is True
    assert isinstance(node.status()["port"], int)
    assert first > 0
    node.stop()


def test_status_shape(storage):
    node = LanSync(storage=storage, device_name="a", port=0, token="t")
    node.start()
    status = node.status()
    assert status["mode"] == "lan"
    assert status["device"] == "a"
    assert status["auth_required"] is True
    assert isinstance(status["peers"], list)
    node.stop()


def test_discover_merges_config_and_broadcast(storage):
    node = LanSync(storage=storage, device_name="a", port=0, peers=["10.0.0.1", "10.0.0.1:80"])
    node.start()
    found = node.discover()
    # 同一个 host:port 不应重复出现
    assert len(found) == 2
    node.stop()


def test_bad_host_reports_error(storage, home):
    from linglong_clip.core.storage import Storage

    node = LanSync(storage=storage, device_name="a", port=0)
    node.start()
    node.peers = ["this-host-does-not-resolve.invalid"]
    assert node.sync_all() == 0
    node.stop()
