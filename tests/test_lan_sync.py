"""局域网直连同步：协议、鉴权、加密与隐私边界。

用真实 socket 在 127.0.0.1 上起服务，验证的是端到端链路，
不是对 mock 的断言——端口用 0 让系统分配，避免测试互相抢端口。
"""

from __future__ import annotations

import json
import socket
import time
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


# ---------- 用户标记（固定 / 收藏）跨设备 ----------


def _add_flagged(storage, content, *, pinned=False, favorite=False, digest=None):
    from linglong_clip.core.models import content_hash

    digest = digest or content_hash("text", content)
    storage.add(
        ClipboardItem(
            kind="text",
            content=content,
            preview=content[:40],
            hash=digest,
            pinned=pinned,
            favorite=favorite,
            created_at=time.time(),
        )
    )
    return digest


def test_lan_transfers_flags_with_new_record(nodes):
    make, local, remote = nodes
    _add_flagged(local, "局域网里的收藏项", pinned=True, favorite=True)

    a, b = make(local, "laptop", token="t"), make(remote, "desktop", token="t")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]

    assert b.sync_all() == 1
    item = remote.list()[0]
    assert item.pinned is True
    assert item.favorite is True


def test_lan_flag_set_after_first_sync_propagates(nodes):
    """内容已拉过，之后才做的标记也必须传过去。

    局域网这条路径的导入逻辑与共享文件夹是分开实现的，所以这个用例
    不能省 —— 两条路各写一套，只测一条等于没测。
    """
    make, local, remote = nodes
    digest = _add_flagged(local, "先同步内容，再收藏")

    a, b = make(local, "laptop", token="t"), make(remote, "desktop", token="t")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]
    assert b.sync_all() == 1

    # 对端在 B 上固定它
    item = remote.find_by_hash(digest)
    remote.update_fields(item.id, pinned=True, favorite=True)

    # B 作为服务端，A 反过来拉 B
    a.peers = [f"127.0.0.1:{b.status()['port']}"]
    outcome = a.sync_all_detailed()

    assert outcome.imported == 0
    assert outcome.flags_updated == 1
    assert local.find_by_hash(digest).pinned is True
    assert local.find_by_hash(digest).favorite is True


def test_lan_status_reports_peer_before_sync(nodes):
    """未同步过的对端不该被算作「已同步」—— 游标为 0。"""
    make, local, remote = nodes
    _add_flagged(local, "x")
    a = make(local, "a", token="t")
    assert a.status()["port"] > 0


def test_lan_detailed_outcome_defaults_to_zero(nodes):
    """对端不可达时明细全 0，而不是抛异常。"""
    make, local, remote = nodes
    b = make(remote, "b", token="t")
    b.peers = ["127.0.0.1:1"]  # 必然不可达
    a = make(local, "a", token="t")
    outcome = a.sync_all_detailed()
    assert (outcome.imported, outcome.flags_updated) == (0, 0)


def test_lan_sync_all_keeps_int_signature(nodes):
    """`sync_all` 的整数返回是既有公开形状，不能被明细接口改掉。"""
    make, local, remote = nodes
    _add_flagged(local, "签名不变")
    a, b = make(local, "laptop", token="t"), make(remote, "desktop", token="t")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]
    assert b.sync_all() == 1


def test_lan_no_repeat_pull_when_flags_unchanged(nodes):
    """标记没变时游标正常推进，不重复拉取。"""
    make, local, remote = nodes
    _add_flagged(local, "只拉一次", pinned=True)
    a, b = make(local, "laptop", token="t"), make(remote, "desktop", token="t")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]

    assert b.sync_all() == 1
    assert b.sync_all() == 0


# ---------- 固定标记的来源：删除保护要能分辨「谁设的固定」 ----------
# 与共享文件夹模式同一条规则，两条路径各写一套用例 —— 只测一条等于没测。


def test_lan_received_pin_records_origin_device(nodes):
    make, local, remote = nodes
    digest = _add_flagged(local, "局域网里被固定的项", pinned=True)
    a, b = make(local, "laptop", token="t"), make(remote, "desktop", token="t")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]
    b.sync_all()

    assert remote.find_by_hash(digest).pinned is True
    assert remote.find_by_hash(digest).pinned_by == "laptop"


def test_lan_pin_from_deleter_does_not_block_delete(nodes):
    """B 固定 → 同步给 A → B 删除：A 上的固定是 B 设的，不该继续拦着。"""
    make, local, remote = nodes
    digest = _add_flagged(local, "别复活我")
    a, b = make(local, "laptop", token="t"), make(remote, "desktop", token="t")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]
    a.peers = [f"127.0.0.1:{b.status()['port']}"]

    # B 先拿到这条
    b.sync_all()
    # B 固定它，A 反向拉回，于是 A 上也有（来源是 desktop）
    remote.update_fields(remote.find_by_hash(digest).id, pinned=True, metadata={})
    a.sync_all()
    assert local.find_by_hash(digest).pinned_by == "desktop"

    # B 删掉它并广播
    remote.delete(remote.find_by_hash(digest).id)
    b.tombstones.record(digest)
    outcome = a.sync_all_detailed()
    assert outcome.removed == 1
    assert local.find_by_hash(digest) is None


def test_lan_own_pin_then_delete_propagates(nodes):
    """A 自己固定、随后自己删除：这个固定是 laptop 设的，删除应当传过去。

    与 `test_lan_pin_from_deleter_does_not_block_delete` 是同一条规则的两面：
    固定标记只在对它**仍有效**时保护记录，发起固定的那台自己反悔了就不该保护。
    """
    make, local, remote = nodes
    digest = _add_flagged(local, "自删自的", pinned=True)
    a, b = make(local, "laptop", token="t"), make(remote, "desktop", token="t")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]
    b.sync_all()
    assert remote.find_by_hash(digest).pinned_by == "laptop"

    local.delete(local.find_by_hash(digest).id)
    a.tombstones.record(digest)
    outcome = b.sync_all_detailed()
    assert outcome.removed == 1
    assert remote.find_by_hash(digest) is None


def test_lan_local_tombstone_blocks_reimport(nodes):
    """B 已记墓碑的指纹，不该从对端再读回来。"""
    make, local, remote = nodes
    digest = _add_flagged(local, "删了就别回来")
    a, b = make(local, "laptop", token="t"), make(remote, "desktop", token="t")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]
    b.sync_all()
    assert remote.find_by_hash(digest) is not None

    remote.delete(remote.find_by_hash(digest).id)
    b.tombstones.record(digest)
    assert b.sync_all() == 0
    assert remote.find_by_hash(digest) is None


# ---------- 标记变更（取消固定/收藏）的传播 ----------
# 局域网与共享文件夹共用同一份解算规则，但入口完全独立，
# 所以每条都要在两条路径上各测一次 —— 只测一条等于没测。


def _make_lan(storage, home, name, changes, started):
    """建一个带独立变更存储的节点（把 store 同时接到服务端与客户端）。"""
    from linglong_clip.core.flag_changes import FlagChangeStore

    node = LanSync(
        storage=storage,
        device_name=name,
        port=0,
        flag_changes=FlagChangeStore(home / f"{name}-flags.json"),
    )
    assert node.start()
    started.append(node)
    return node


@pytest.fixture()
def lan_pair(storage, home):
    """两个带独立标记变更存储的节点，配对完成。"""
    from linglong_clip.core.storage import Storage

    other = Storage(home / "peer-flag.db")
    started = []
    a = _make_lan(storage, home, "laptop", None, started)
    b = _make_lan(other, home, "desktop", None, started)
    b.peers = [f"127.0.0.1:{a.status()['port']}"]
    yield a, b, storage, other
    for node in started:
        node.stop()
    other.close()


def test_lan_cancel_propagates(lan_pair):
    """**本轮主场景**：A 固定后取消，B 端要看到取消。"""
    from linglong_clip.core.flag_changes import FLAG_PINNED

    a, b, local, remote = lan_pair
    digest = _add_flagged(local, "局域网里的取消", pinned=True)
    a.flag_changes.record(digest, FLAG_PINNED, True, at=1000.0)

    b.sync_all()
    assert remote.find_by_hash(digest).pinned is True

    local.update_fields(local.find_by_hash(digest).id, pinned=False)
    a.flag_changes.record(digest, FLAG_PINNED, False, at=2000.0)

    b.sync_all()
    assert remote.find_by_hash(digest).pinned is False, "取消必须能传过去"


def test_lan_cancel_is_idempotent(lan_pair):
    """取消传过去之后再同步多轮，不该反复改数据。"""
    from linglong_clip.core.flag_changes import FLAG_PINNED

    a, b, local, remote = lan_pair
    digest = _add_flagged(local, "局域网取消后稳定", pinned=True)
    a.flag_changes.record(digest, FLAG_PINNED, True, at=1000.0)
    b.sync_all()

    local.update_fields(local.find_by_hash(digest).id, pinned=False)
    a.flag_changes.record(digest, FLAG_PINNED, False, at=2000.0)
    b.sync_all()
    assert remote.find_by_hash(digest).pinned is False

    assert b.sync_all() == 0
    assert remote.find_by_hash(digest).pinned is False


def test_lan_cancel_does_not_break_other_device_pin(lan_pair):
    """B 自己显式设的固定，不该被 A 的取消抹掉（并集这条语义仍成立）。"""
    from linglong_clip.core.flag_changes import FLAG_PINNED

    a, b, local, remote = lan_pair
    digest = _add_flagged(local, "两台都固定过")
    b.sync_all()

    # B 显式固定它，并拉回给 A
    b.flag_changes.record(digest, FLAG_PINNED, True, at=1000.0)
    remote.update_fields(remote.find_by_hash(digest).id, pinned=True)
    a.peers = [f"127.0.0.1:{b.status()['port']}"]
    a.sync_all()
    assert local.find_by_hash(digest).pinned is True

    # A 取消（A 从未设过），并推给 B
    a.flag_changes.record(digest, FLAG_PINNED, False, at=2000.0)
    b.sync_all()
    assert remote.find_by_hash(digest).pinned is True, "B 的显式固定必须留着"


def test_lan_cancel_arrives_without_content_record(lan_pair):
    """变更到达时同批没有内容记录 —— 只靠内容循环永远碰不到它。"""
    from linglong_clip.core.flag_changes import FLAG_PINNED

    a, b, local, remote = lan_pair
    digest = _add_flagged(local, "只发变更不发内容")
    # 内容先同步过去
    b.sync_all()
    assert remote.find_by_hash(digest) is not None

    # A 取消（A 从未固定过，但这也是一条显式动作）
    a.flag_changes.record(digest, FLAG_PINNED, True, at=1000.0)
    a.flag_changes.record(digest, FLAG_PINNED, False, at=2000.0)
    outcome = b.sync_all_detailed()
    assert outcome.flag_changes >= 1


def test_lan_flag_changes_are_acked_after_serve(lan_pair):
    """服务端写出响应之后要确认送达 —— 否则每轮都在重传全部历史事件。

    这正是「已送达的不再复述」在局域网模式下从未生效的那个洞。
    """
    from linglong_clip.core.flag_changes import FLAG_PINNED

    a, b, local, remote = lan_pair
    digest = _add_flagged(local, "确认送达")
    a.flag_changes.record(digest, FLAG_PINNED, True, at=1000.0)

    b.sync_all()
    # 已经交付过，不该再待发
    assert a.flag_changes.pending() == []


def test_lan_tombstones_are_acked_after_serve(lan_pair):
    """墓碑同理：局域网是拉取模型，写出成功即视为送达。"""
    a, b, local, remote = lan_pair
    digest = _add_flagged(local, "删除也要确认送达")
    b.sync_all()

    local.delete(local.find_by_hash(digest).id)
    a.tombstones.record(digest)

    assert b.sync_all() == 0  # 对方没有新内容，只有一条删除
    assert a.tombstones.pending() == [], "写出成功后不该还待发"


# ---------- 整库传输：记录数超过一页时不能丢 ----------
#
# 与共享文件夹那条路径同一类缺陷：客户端原先只拉一页，服务端又只看
# 最新 `_max_entries` 条。结果是「新设备首同步只拿到一部分历史」，
# 游标被推到第一页的最大时间戳，剩下的因为都比它旧而永远不再被请求。


def _add_series(st, total, *, prefix="h"):
    """写入 `total` 条时间递增、内容各异的记录。"""
    for index in range(total):
        st.add(
            ClipboardItem(
                kind="text",
                content=f"item-{index}",
                preview=f"item-{index}",
                created_at=1000 + index,
                hash=f"{prefix}{index:05d}",
            )
        )


def test_lan_sync_pages_through_whole_history(nodes):
    """800 条 > 单页 500：一次同步必须把 800 条全拉过来。"""
    make, local, remote = nodes
    _add_series(local, 800)

    a, b = make(local, "a", token="t"), make(remote, "b", token="t")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]

    assert b.sync_all() == 800
    assert remote.count() == 800
    assert {item.hash for item in remote.list(limit=10_000)} == {
        f"h{index:05d}" for index in range(800)
    }


def test_lan_sync_paging_is_idempotent(nodes):
    """翻页修好之后，重复同步仍不该重复导入。"""
    make, local, remote = nodes
    _add_series(local, 800)

    a, b = make(local, "a", token="t"), make(remote, "b", token="t")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]

    assert b.sync_all() == 800
    assert b.sync_all() == 0
    assert remote.count() == 800


def test_lan_sync_incremental_after_paging(nodes):
    """翻页之后，增量语义不变：只补新增的那几条。"""
    make, local, remote = nodes
    _add_series(local, 800)

    a, b = make(local, "a", token="t"), make(remote, "b", token="t")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]
    b.sync_all()

    _add_series(local, 805)  # index 800..804 是新出现的 5 条
    assert b.sync_all() == 5
    assert remote.count() == 805


def test_lan_server_serves_records_beyond_one_page(nodes):
    """服务端本身要能供出超过一页的记录，而不是只认最新一批。"""
    make, local, remote = nodes
    _add_series(local, 620)

    server = LanSyncServer(local, device_name="a")
    first = server.collect_records(limit=500)
    assert len(first) == 500

    # 用第一页最大时间戳继续问，必须还能拿到后面的。
    # `since` 是排他语义，所以边界那一条会被再发一次 —— 这是刻意的：
    # 宁可重复（hash 去重吃掉），也不能因为边界相等而漏掉。
    page = [SyncRecord.from_json(json.loads(blob)) for blob in first]
    newest = max(record.created_at for record in page if record is not None)
    rest = server.collect_records(since=newest, limit=500)
    assert 120 <= len(rest) <= 121
    assert len(set(first) & set(rest)) <= 1  # 至多重复边界一条


def test_lan_same_timestamp_records_are_not_lost(nodes):
    """同一时刻的多条记录不能因为排序不稳而漏读。"""
    make, local, remote = nodes
    for index in range(30):
        local.add(
            ClipboardItem(
                kind="text", content=f"same-{index}", preview=f"same-{index}",
                created_at=2000.0, hash=f"s{index:03d}",
            )
        )

    a, b = make(local, "a", token="t"), make(remote, "b", token="t")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]

    # 30 条同刻记录 < 一页，应当一次全到
    assert b.sync_all() == 30
    assert remote.count() == 30


def test_lan_paging_still_delivers_tombstone_in_first_page(nodes):
    """翻页之后，删除标记仍要在同一轮里生效（它在第一页随内容一起发）。"""
    make, local, remote = nodes
    _add_series(local, 800)

    a, b = make(local, "a", token="t"), make(remote, "b", token="t")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]
    b.sync_all()

    # A 删掉其中较早的一条，并把删除标记传给 B
    victim = local.find_by_hash("h00100")
    local.delete(victim.id)
    a.tombstones.record("h00100")

    assert b.sync_all() == 0  # 没有新内容，只有删除
    assert remote.find_by_hash("h00100") is None
    assert remote.count() == 799


def test_lan_paging_with_deletes_and_new_records(nodes):
    """一整轮：800 条历史 + 删除 + 新增，游标只推进一次且结果正确。"""
    make, local, remote = nodes
    _add_series(local, 800)

    a, b = make(local, "a", token="t"), make(remote, "b", token="t")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]
    assert b.sync_all() == 800

    victim = local.find_by_hash("h00700")
    local.delete(victim.id)
    a.tombstones.record("h00700")
    local.add(
        ClipboardItem(
            kind="text", content="after-delete", preview="after-delete",
            created_at=9000.0, hash="hnew",
        )
    )

    assert b.sync_all() == 1  # 只新增这一条
    assert remote.find_by_hash("h00700") is None
    assert remote.find_by_hash("hnew") is not None
    assert remote.count() == 800  # 800 - 1 + 1


def test_lan_partial_paging_failure_keeps_progress_and_retries(nodes, monkeypatch):
    """翻到一半对端挂了：已导入的不丢，游标不越过已收到的位置，下轮补上。

    这里把第二页的请求打掉，模拟对端中途不可达。关键是不能因为
    「已经导了第一页」就把游标推到第一页的最大时间戳 —— 那样第二页起
    的内容会永久跳过。正确行为是保留旧游标（或只推到已确认的位置），
    下一轮从同一位置重来，重复由指纹去重吃掉。
    """
    make, local, remote = nodes
    _add_series(local, 800)

    a, b = make(local, "a", token="t"), make(remote, "b", token="t")
    b.peers = [f"127.0.0.1:{a.status()['port']}"]

    original_fetch = b._client.fetch
    calls = {"n": 0}

    def flaky_fetch(peer, *, since=0.0, limit=None, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            return None  # 第二页开始不可达
        return original_fetch(peer, since=since, limit=limit or 500, **kwargs)

    monkeypatch.setattr(b._client, "fetch", flaky_fetch)
    first_round = b.sync_all()
    assert first_round == 500, "第一页应当导入成功"
    assert remote.count() == 500
    assert b._client.cursors.snapshot() == {}, "中途失败不该推进游标"

    # 恢复网络后重来，剩下的 300 条要能补齐
    monkeypatch.setattr(b._client, "fetch", original_fetch)
    assert b.sync_all() == 300
    assert remote.count() == 800
