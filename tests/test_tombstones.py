"""删除标记：持久化、保留期、确认语义，以及与两种同步模式的端到端配合。

这里的用例集中在**边界**：删除后对端又送回来、墓碑过期后不再删别人、
对端陈旧时不该误删本地、发送失败必须重试。
"""

import json
import time

import pytest

from linglong_clip.core.models import ClipboardItem
from linglong_clip.core.lan_sync import LanSyncServer, PeerInfo
from linglong_clip.core.storage import Storage
from linglong_clip.core.sync import FolderSync, SyncCipher
from linglong_clip.core.sync_cursor import build_cursor_store
from linglong_clip.core.tombstones import (
    DEFAULT_RETENTION_SECONDS,
    contains_hash,
    Tombstone,
    TombstoneStore,
    build_tombstone_store,
    fingerprint,
    local_fingerprints,
)


def make_item(storage, text, *, kind="text", hash_=None):
    item = ClipboardItem(kind=kind, content=text, hash=hash_ or fingerprint(ClipboardItem(kind=kind, content=text)))
    return storage.add(item)


# ---------- 存储本身 ----------


def test_new_store_starts_empty(tmp_path):
    store = TombstoneStore(tmp_path / "t.json")
    store.load()
    assert store.snapshot() == {}
    assert store.pending() == []


def test_record_and_reload_round_trips(tmp_path):
    path = tmp_path / "t.json"
    store = TombstoneStore(path)
    store.record("abc", deleted_at=1000.0)

    reloaded = TombstoneStore(path)
    reloaded.load()
    assert reloaded.has("abc")
    assert reloaded.snapshot()["abc"] == 1000.0


def test_file_is_human_readable_and_has_no_content_field(tmp_path):
    path = tmp_path / "t.json"
    store = TombstoneStore(path)
    store.record("deadbeef", deleted_at=1000.0)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["retention_seconds"] == DEFAULT_RETENTION_SECONDS
    assert raw["deleted"] == [{"hash": "deadbeef", "deleted_at": 1000.0}]
    # 墓碑里不允许出现被删内容：否则「删除」等于留了一份副本
    assert "content" not in json.dumps(raw)


def test_record_is_idempotent(tmp_path):
    store = TombstoneStore(tmp_path / "t.json")
    assert store.record("abc", deleted_at=1.0) is True
    assert store.record("abc", deleted_at=2.0) is False
    assert store.snapshot()["abc"] == 1.0  # 首次删除时间不被覆盖


def test_record_ignores_empty_hash(tmp_path):
    store = TombstoneStore(tmp_path / "t.json")
    assert store.record("") is False
    assert store.record(None) is False
    assert store.snapshot() == {}


def test_record_many(tmp_path):
    store = TombstoneStore(tmp_path / "t.json")
    assert store.record_many(["a", "b", "a", ""], deleted_at=5.0) == 2
    assert set(store.snapshot()) == {"a", "b"}


def test_corrupted_file_resets_instead_of_raising(tmp_path):
    path = tmp_path / "t.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = TombstoneStore(path)
    store.load()
    assert store.snapshot() == {}
    # 之后还能正常写入
    assert store.record("abc") is True


def test_non_dict_payload_resets(tmp_path):
    path = tmp_path / "t.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    store = TombstoneStore(path)
    store.load()
    assert store.snapshot() == {}


def test_bad_entries_are_skipped(tmp_path):
    path = tmp_path / "t.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "deleted": [
                    {"hash": "ok", "deleted_at": 10},
                    {"hash": "", "deleted_at": 10},
                    "not-a-dict",
                    {"deleted_at": 10},
                    {"hash": "bad-ts", "deleted_at": "x"},
                ],
                "acked": {"ok": "nope"},
            }
        ),
        encoding="utf-8",
    )
    # 保留期调得足够大，避免用例的期望随时间漂移
    store = TombstoneStore(path, retention=10**12)
    store.load()
    assert set(store.snapshot()) == {"ok"}
    assert store.status()["pending"] == 1
    assert store.snapshot()["ok"] == 10.0


# ---------- 确认与传播 ----------


def test_pending_excludes_acknowledged(tmp_path):
    store = TombstoneStore(tmp_path / "t.json")
    store.record("a")
    store.record("b")
    assert {entry.hash for entry in store.pending()} == {"a", "b"}

    store.ack(["a"])
    assert {entry.hash for entry in store.pending()} == {"b"}
    # 已确认的仍在文件里（便于排查），只是不再传播
    assert store.has("a")


def test_ack_ignores_unknown_hash(tmp_path):
    store = TombstoneStore(tmp_path / "t.json")
    store.record("a")
    assert store.ack(["a", "never-seen"]) == 1


def test_pending_excludes_expired(tmp_path):
    store = TombstoneStore(tmp_path / "t.json", retention=100)
    store.record("old", deleted_at=1000.0)
    store.record("fresh", deleted_at=1050.0)

    # now = 1100：old 已过期（100 秒保留期），fresh 还在窗口内
    pending = {entry.hash for entry in store.pending(now=1100.0)}
    assert pending == {"fresh"}


def test_retention_zero_never_expires(tmp_path):
    store = TombstoneStore(tmp_path / "t.json", retention=0)
    store.record("x", deleted_at=1.0)
    assert {entry.hash for entry in store.pending(now=10**9)} == {"x"}


def test_prune_removes_only_expired(tmp_path):
    store = TombstoneStore(tmp_path / "t.json", retention=100)
    store.record("old", deleted_at=1000.0)
    store.record("fresh", deleted_at=1050.0)
    assert store.prune(now=1100.0) == 1
    assert set(store.snapshot()) == {"fresh"}


def test_prune_also_forgets_ack_state(tmp_path):
    store = TombstoneStore(tmp_path / "t.json", retention=10)
    store.record("a", deleted_at=1.0)
    store.ack(["a"])
    store.prune(now=1000.0)
    assert store.snapshot() == {}
    # 已清掉后重新删除同一条内容仍应被记录
    assert store.record("a", deleted_at=1001.0) is True


def test_reset_clears_everything(tmp_path):
    store = TombstoneStore(tmp_path / "t.json")
    store.record("a")
    store.record("b")
    assert store.reset() == 2
    assert store.snapshot() == {}


def test_status_reports_pending_and_retention(tmp_path):
    store = TombstoneStore(tmp_path / "t.json", retention=42)
    store.record("a", deleted_at=100.0)
    store.record("b", deleted_at=200.0)
    store.ack(["a"])
    status = store.status(now=220.0)  # 距 b 的删除刚过 20 秒，仍在 42 秒保留期内
    assert status["count"] == 2
    assert status["pending"] == 1
    assert status["retention_seconds"] == 42
    assert status["tombstones"][0]["hash"] == "b"  # 新的在前


def test_status_marks_expired_as_not_pending(tmp_path):
    store = TombstoneStore(tmp_path / "t.json", retention=42)
    store.record("b", deleted_at=200.0)
    # now = 250：已过 50 秒 > 42 秒保留期，不再传播
    assert store.status(now=250.0)["pending"] == 0


def test_unwritable_path_does_not_raise(tmp_path):
    target = tmp_path / "dir" / "t.json"
    target.parent.mkdir()
    target.parent.chmod(0o500)
    try:
        store = TombstoneStore(target)
        assert store.record("a") is True  # 落盘失败只告警
    finally:
        target.parent.chmod(0o700)


# ---------- 指纹 ----------


def test_fingerprint_prefers_existing_hash():
    item = ClipboardItem(kind="text", content="hello", hash="explicit")
    assert fingerprint(item) == "explicit"


def test_fingerprint_falls_back_to_content_hash():
    item = ClipboardItem(kind="text", content="hello")
    assert fingerprint(item) == fingerprint(ClipboardItem(kind="text", content="hello"))
    assert fingerprint(item) != fingerprint(ClipboardItem(kind="link", content="hello"))


def test_fingerprint_empty_content_is_empty():
    assert fingerprint(ClipboardItem(kind="text", content="")) == ""


def test_local_fingerprints_reads_storage(tmp_path):
    storage = Storage(tmp_path / "h.db")
    make_item(storage, "one")
    make_item(storage, "two")
    digests = local_fingerprints(storage)
    assert len(digests) == 2
    assert all(digests)
    storage.close()


def test_local_fingerprints_survives_broken_storage():
    class Boom:
        def list(self, **kwargs):
            raise RuntimeError("boom")

    assert local_fingerprints(Boom()) == []


# ---------- 与 FolderSync 的配合 ----------


def _folder_sync(storage, folder, device, tombstones=None, cursors=None):
    return FolderSync(
        storage,
        folder,
        device_name=device,
        cipher=SyncCipher(False),
        cursors=cursors or build_cursor_store(folder.parent / f"{device}-cursors.json"),
        tombstones=tombstones,
    )


def test_folder_sync_sends_tombstones(tmp_path):
    """内容已导出过，删除标记仍必须发出去。

    这是最容易写错的一处：导出侧本来就有「发过的内容不再重复发」的过滤，
    墓碑若沿用同一条过滤就会被全部拦掉 —— 正因为对端当年收到过这条内容，
    才需要被告知「它已经删了」。
    """
    src = Storage(tmp_path / "a.db")
    item = make_item(src, "secret-ish note")
    tombstones = build_tombstone_store(tmp_path / "a-tomb.json")

    folder = tmp_path / "shared"
    sync = _folder_sync(src, folder, "A", tombstones)
    assert sync.export() == 1
    tombstones.record(item.hash)
    assert sync.export() == 1  # 内容已发过，这一条只能是墓碑

    records = list(sync._read_journal(folder / "A.llsync"))
    assert [record.is_tombstone for record in records] == [False, True]
    assert records[1].hash == item.hash
    assert records[1].content == ""
    src.close()


def test_folder_sync_does_not_resend_acked_tombstones(tmp_path):
    src = Storage(tmp_path / "a.db")
    item = make_item(src, "note")
    tombstones = build_tombstone_store(tmp_path / "a-tomb.json")
    folder = tmp_path / "shared"
    sync = _folder_sync(src, folder, "A", tombstones)

    sync.export()
    tombstones.record(item.hash)
    sync.export()
    tombstones.ack([item.hash])

    size_before = (folder / "A.llsync").stat().st_size
    assert sync.export() == 0
    assert (folder / "A.llsync").stat().st_size == size_before
    src.close()


def test_folder_sync_applies_peer_tombstones(tmp_path):
    """核心场景：A 删除 → B 的副本跟着消失，且下一轮不会被写回来。"""
    folder = tmp_path / "shared"
    a = Storage(tmp_path / "a.db")
    b = Storage(tmp_path / "b.db")
    tombstones_a = build_tombstone_store(tmp_path / "a-tomb.json")
    tombstones_b = build_tombstone_store(tmp_path / "b-tomb.json")

    item = make_item(a, "旧密码片段")
    sync_a = _folder_sync(a, folder, "A", tombstones_a)
    sync_b = _folder_sync(b, folder, "B", tombstones_b)

    sync_a.export()
    assert sync_b.import_all() == 1
    assert b.count() == 1

    # A 上删除
    a.delete(item.id)
    tombstones_a.record(item.hash)
    assert sync_a.export() == 1
    assert sync_b.import_all() == 0

    assert b.count() == 0, "对端的删除没有传播过来"
    assert tombstones_b.has(item.hash)

    # 再同步几轮也不该复活：墓碑挡着
    assert sync_a.export() == 0
    assert sync_b.import_all() == 0
    assert b.count() == 0

    a.close()
    b.close()


def test_folder_sync_import_acknowledges_peer_tombstones(tmp_path):
    folder = tmp_path / "shared"
    a = Storage(tmp_path / "a.db")
    tombstones_a = build_tombstone_store(tmp_path / "a-tomb.json")
    tombstones_b = build_tombstone_store(tmp_path / "b-tomb.json")
    b = Storage(tmp_path / "b.db")

    item = make_item(a, "note")
    sync_a = _folder_sync(a, folder, "A", tombstones_a)
    sync_b = _folder_sync(b, folder, "B", tombstones_b)
    sync_a.export()
    sync_b.import_all()

    a.delete(item.id)
    tombstones_a.record(item.hash)
    sync_a.export()
    sync_b.import_all()

    # B 告诉 A「你那条删除我收到了」，A 不再复述
    assert tombstones_a.status()["pending"] == 0
    assert sync_a.export() == 0

    a.close()
    b.close()


def test_expired_tombstone_does_not_delete_peer_data(tmp_path):
    """对端 7 天没上线，本机删除不该追过去删掉它那边的数据。"""
    folder = tmp_path / "shared"
    a = Storage(tmp_path / "a.db")
    b = Storage(tmp_path / "b.db")
    tombstones_a = build_tombstone_store(tmp_path / "a-tomb.json", retention=100)
    tombstones_b = build_tombstone_store(tmp_path / "b-tomb.json", retention=100)

    item = make_item(a, "note")
    sync_a = _folder_sync(a, folder, "A", tombstones_a)
    sync_b = _folder_sync(b, folder, "B", tombstones_b)
    sync_a.export()
    sync_b.import_all()

    a.delete(item.id)
    tombstones_a.record(item.hash, deleted_at=time.time() - 10_000)
    assert sync_a.export() == 0
    assert sync_b.import_all() == 0
    assert b.count() == 1, "过期的删除不该删掉对端尚存的数据"

    a.close()
    b.close()


def test_recreated_content_is_not_resurrected_by_journal(tmp_path):
    """B 在本地重新复制过同一内容时，A 的删除只清掉「同步来的那份」。

    B 上那条是用户本轮的显式动作，不该被历史删除标记连带清掉；
    但它也不会再被 A 的日志写回来（墓碑挡着）。
    """
    folder = tmp_path / "shared"
    a = Storage(tmp_path / "a.db")
    b = Storage(tmp_path / "b.db")
    tombstones_a = build_tombstone_store(tmp_path / "a-tomb.json")
    tombstones_b = build_tombstone_store(tmp_path / "b-tomb.json")

    item = make_item(a, "一段文字")
    sync_a = _folder_sync(a, folder, "A", tombstones_a)
    sync_b = _folder_sync(b, folder, "B", tombstones_b)
    sync_a.export()
    sync_b.import_all()

    # A 删除并把墓碑送出，但 B 这轮没同步（离线）
    a.delete(item.id)
    tombstones_a.record(item.hash)
    sync_a.export()

    # B 上用户又复制了一遍同样的内容
    make_item(b, "一段文字")

    sync_b.import_all()
    assert b.count() == 1, "用户在 B 上重新复制的内容不该被历史删除连带清掉"
    assert tombstones_b.has(item.hash)

    # 后面几轮也不会被 A 的旧日志写回来（同一指纹本来也只留一条）
    sync_a.export()
    sync_b.import_all()
    assert b.count() == 1

    a.close()
    b.close()


def test_pinned_item_survives_peer_tombstone(tmp_path):
    """固定项是用户显式说「别动它」，删除传播不该越过这条线。"""
    folder = tmp_path / "shared"
    a = Storage(tmp_path / "a.db")
    b = Storage(tmp_path / "b.db")
    tombstones_a = build_tombstone_store(tmp_path / "a-tomb.json")
    tombstones_b = build_tombstone_store(tmp_path / "b-tomb.json")

    item = make_item(a, "要保留的片段")
    b.add(
        ClipboardItem(
            kind="text",
            content="要保留的片段",
            hash=item.hash,
            preview="要保留的片段",
            pinned=True,
        )
    )
    sync_a = _folder_sync(a, folder, "A", tombstones_a)
    sync_b = _folder_sync(b, folder, "B", tombstones_b)

    a.delete(item.id)
    tombstones_a.record(item.hash)
    sync_a.export()
    sync_b.import_all()

    assert b.count() == 1
    assert b.find_by_hash(item.hash).pinned is True
    # 但指纹仍被记下，避免后面又被写回来
    assert tombstones_b.has(item.hash)

    a.close()
    b.close()


def test_clear_records_tombstones_for_every_local_hash(tmp_path):
    """清空历史也必须留下墓碑，否则别的设备会把它整批送回来。"""
    storage = Storage(tmp_path / "a.db")
    tombstones = build_tombstone_store(tmp_path / "t.json")
    for text in ("one", "two", "three"):
        make_item(storage, text)

    digests = local_fingerprints(storage)
    storage.clear(keep_pinned=False)
    tombstones.record_many(digests)
    assert len(tombstones.snapshot()) == 3
    assert storage.count() == 0
    storage.close()


def test_local_fingerprints_reflects_what_survives(tmp_path):
    """清空历史时只给「真的被删掉的」记墓碑，固定项不该进名单。"""
    storage = Storage(tmp_path / "a.db")
    keep = make_item(storage, "keep")
    storage.update_fields(keep.id, pinned=True)
    dropped = make_item(storage, "dropped")

    before = set(local_fingerprints(storage))
    assert before == {keep.hash, dropped.hash}

    storage.clear(keep_pinned=True)
    survivors = set(local_fingerprints(storage))
    doomed = [digest for digest in before if digest not in survivors]

    tombstones = build_tombstone_store(tmp_path / "t.json")
    tombstones.record_many(doomed)

    assert keep.hash not in tombstones.all_hashes()
    assert dropped.hash in tombstones.all_hashes()
    assert storage.count() == 1
    storage.close()


# ---------- 与局域网同步的配合 ----------


def _lan_pair(tmp_path, retention=DEFAULT_RETENTION_SECONDS):
    """两个真实 socket 上跑的节点，各自持有独立的存储与墓碑。"""
    from linglong_clip.core.lan_sync import LanSyncClient, LanSyncServer
    from linglong_clip.core.sync import SyncCipher

    a = Storage(tmp_path / "a.db")
    b = Storage(tmp_path / "b.db")
    tombstones_a = build_tombstone_store(tmp_path / "a-tomb.json", retention=retention)
    tombstones_b = build_tombstone_store(tmp_path / "b-tomb.json", retention=retention)
    cipher = SyncCipher(False)
    cursors_a = build_cursor_store(tmp_path / "a-cursors.json")
    cursors_b = build_cursor_store(tmp_path / "b-cursors.json")

    server = LanSyncServer(
        a,
        device_name="A",
        host="127.0.0.1",
        port=0,
        cipher=cipher,
        tombstones=tombstones_a,
    )
    client = LanSyncClient(
        b,
        device_name="B",
        cipher=cipher,
        cursors=cursors_b,
        tombstones=tombstones_b,
    )
    assert server.start()
    return server, client, a, b, tombstones_a, tombstones_b


def test_lan_server_ships_tombstones(tmp_path):
    server, client, a, b, tombstones_a, _ = _lan_pair(tmp_path)
    try:
        item = make_item(a, "局域网里的片段")
        a.delete(item.id)
        tombstones_a.record(item.hash)

        peer = PeerInfo(host="127.0.0.1", port=server.bound_port)
        records = client.fetch(peer, since=0.0)
        assert records is not None
        tombstones = [record for record in records if record.is_tombstone]
        assert len(tombstones) == 1
        assert tombstones[0].hash == item.hash
        assert tombstones[0].content == ""
    finally:
        server.stop()
        a.close()
        b.close()


def test_lan_peer_tombstone_removes_local_copy(tmp_path):
    """局域网模式的核心场景：A 删除，B 的副本跟着消失且不再回来。"""
    server, client, a, b, tombstones_a, tombstones_b = _lan_pair(tmp_path)
    try:
        item = make_item(a, "要被删掉的片段")
        peer = PeerInfo(host="127.0.0.1", port=server.bound_port)

        assert client.sync_peer(peer) == 1
        assert b.count() == 1

        a.delete(item.id)
        tombstones_a.record(item.hash)
        assert client.sync_peer(peer) == 0

        assert b.count() == 0, "局域网对端的删除没有传播过来"
        assert tombstones_b.has(item.hash)

        # 再同步几轮也不复活
        for _ in range(3):
            client.sync_peer(peer)
        assert b.count() == 0
    finally:
        server.stop()
        a.close()
        b.close()


def test_lan_tombstone_removal_is_idempotent(tmp_path):
    """同一批里删除与内容同时到达时，删除必须赢，且重复同步不出错。"""
    server, client, a, b, tombstones_a, tombstones_b = _lan_pair(tmp_path)
    try:
        peer = PeerInfo(host="127.0.0.1", port=server.bound_port)
        item = make_item(a, "先有后无")
        client.sync_peer(peer)
        assert b.count() == 1

        a.delete(item.id)
        tombstones_a.record(item.hash)

        # 游标不会被下轮跳过的场景：服务端把删除和旧内容一起返回
        assert client.import_records(client.fetch(peer, since=0.0) or []).imported == 0
        assert b.count() == 0

        for _ in range(3):
            client.import_records(client.fetch(peer, since=0.0) or [])
        assert b.count() == 0
        assert tombstones_b.has(item.hash)
    finally:
        server.stop()
        a.close()
        b.close()


def test_lan_expired_tombstone_keeps_peer_data(tmp_path):
    server, client, a, b, tombstones_a, tombstones_b = _lan_pair(tmp_path, retention=100)
    try:
        peer = PeerInfo(host="127.0.0.1", port=server.bound_port)
        item = make_item(a, "对端离线很久")
        client.sync_peer(peer)
        assert b.count() == 1

        a.delete(item.id)
        tombstones_a.record(item.hash, deleted_at=time.time() - 10_000)
        # 服务端不再把过期的墓碑放进响应
        records = client.fetch(peer, since=0.0) or []
        assert not [record for record in records if record.is_tombstone]
        assert client.import_records(records).imported == 0
        assert b.count() == 1, "过期的删除不该删掉对端尚存的数据"
    finally:
        server.stop()
        a.close()
        b.close()


def test_lan_pinned_item_survives_peer_tombstone(tmp_path):
    server, client, a, b, tombstones_a, tombstones_b = _lan_pair(tmp_path)
    try:
        peer = PeerInfo(host="127.0.0.1", port=server.bound_port)
        item = make_item(a, "固定住的片段")
        b.add(
            ClipboardItem(
                kind="text",
                content="固定住的片段",
                hash=item.hash,
                preview="固定住的片段",
                pinned=True,
            )
        )
        a.delete(item.id)
        tombstones_a.record(item.hash)
        client.sync_peer(peer)

        assert b.count() == 1
        assert b.find_by_hash(item.hash).pinned is True
        assert tombstones_b.has(item.hash)
    finally:
        server.stop()
        a.close()
        b.close()


def test_lan_status_reports_probed_peers(tmp_path):
    """状态里要能看出「哪台对端在线、它叫什么」，而不只是一串地址。"""
    from linglong_clip.core.lan_sync import LanSync

    d = tmp_path
    other = Storage(tmp_path / "other.db")
    from linglong_clip.core.sync import SyncCipher

    peer_server = LanSyncServer(
        other, device_name="PEER", host="127.0.0.1", port=0, cipher=SyncCipher(False)
    )
    assert peer_server.start()
    try:
        node = LanSync(
            storage=Storage(tmp_path / "local.db"),
            device_name="LOCAL",
            port=0,
            peers=[f"127.0.0.1:{peer_server.bound_port}"],
            cipher=SyncCipher(False),
            cursors=build_cursor_store(tmp_path / "c.json"),
            tombstones=build_tombstone_store(tmp_path / "t.json"),
        )
        assert node.start()
        try:
            status = node.status()
            assert status["online"] == 1
            assert status["peers"][0]["ok"] is True
            assert status["peers"][0]["device"] == "PEER"
            assert status["pending_tombstones"] == 0
        finally:
            node.stop()
    finally:
        peer_server.stop()
        other.close()


def test_lan_status_marks_unreachable_peer_offline(tmp_path):
    from linglong_clip.core.lan_sync import LanSync

    node = LanSync(
        storage=Storage(tmp_path / "local.db"),
        device_name="LOCAL",
        port=0,
        peers=["127.0.0.1:1"],  # 保留端口，几乎不会有人监听
        cipher=None,
        cursors=build_cursor_store(tmp_path / "c.json"),
        tombstones=build_tombstone_store(tmp_path / "t.json"),
    )
    assert node.start()
    try:
        status = node.status()
        assert status["online"] == 0
        assert status["peers"][0]["ok"] is False
    finally:
        node.stop()


# ---------- CLI / 应用层：本地删除要留下可传播的痕迹 ----------


def test_cli_remove_records_tombstone(tmp_path, monkeypatch, capsys):
    from linglong_clip.__main__ import main

    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(tmp_path))
    storage = Storage()
    item = make_item(storage, "要被同步删除的内容")
    storage.close()

    assert main(["--remove", str(item.id)]) == 0
    out = capsys.readouterr().out
    assert "已删除记录" in out
    assert "删除标记" in out

    storage = Storage()
    assert storage.count() == 0
    storage.close()

    tombstones = build_tombstone_store()
    assert item.hash in tombstones.all_hashes()


def test_cli_remove_unknown_id_fails(tmp_path, monkeypatch, capsys):
    from linglong_clip.__main__ import main

    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(tmp_path))
    assert main(["--remove", "999"]) == 1
    assert "找不到" in capsys.readouterr().err


def test_cli_clear_records_tombstones(tmp_path, monkeypatch, capsys):
    from linglong_clip.__main__ import main

    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(tmp_path))
    storage = Storage()
    keep = make_item(storage, "固定住的")
    storage.update_fields(keep.id, pinned=True)
    dropped = make_item(storage, "会被清掉的")
    storage.close()

    assert main(["--clear"]) == 0
    out = capsys.readouterr().out
    assert "1 条删除标记" in out

    tombstones = build_tombstone_store()
    assert dropped.hash in tombstones.all_hashes()
    assert keep.hash not in tombstones.all_hashes(), "固定项还在，不该被当成删除"


def test_cli_tombstones_output_and_reset(tmp_path, monkeypatch, capsys):
    from linglong_clip.__main__ import main

    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(tmp_path))
    store = build_tombstone_store()
    store.record("aaaabbbbccccddddeeeeffff00001111")

    assert main(["--tombstones"]) == 0
    out = capsys.readouterr().out
    assert "待同步 1 条" in out
    assert "aaaabbbbcccc" in out

    assert main(["--tombstones", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1

    assert main(["--tombstones-reset"]) == 0
    assert "已清空 1 条删除标记" in capsys.readouterr().out
    assert build_tombstone_store().snapshot() == {}


def test_cli_purge_tombstones(tmp_path, monkeypatch, capsys):
    from linglong_clip.__main__ import main

    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(tmp_path))
    store = build_tombstone_store()
    store.record("old", deleted_at=time.time() - DEFAULT_RETENTION_SECONDS - 10)
    store.record("fresh")

    assert main(["--purge-tombstones"]) == 0
    assert "已清除 1 条超期删除标记" in capsys.readouterr().out
    assert set(build_tombstone_store().snapshot()) == {"fresh"}


def test_application_delete_records_tombstone(tmp_path, monkeypatch):
    """界面上的删除同样要留下删除标记，否则跨设备删除只在 CLI 下有效。"""
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(tmp_path))

    from PySide6.QtWidgets import QApplication

    from linglong_clip.ui.application import Application

    app = QApplication.instance() or QApplication([])
    assert app is not None

    storage = Storage()
    item = make_item(storage, "界面里删掉的内容")
    storage.close()

    instance = Application(["test"])
    try:
        instance.delete_item(item.id)
        assert instance.storage.count() == 0
        assert instance.tombstones.has(item.hash)
    finally:
        instance.quit()


# ---------- 小的边界与可读性 ----------


def test_contains_hash_handles_empty_input():
    assert contains_hash(None, "abc") is False
    assert contains_hash(["abc"], "") is False
    assert contains_hash(["abc"], None) is False
    assert contains_hash(["abc"], "abc") is True
    assert contains_hash([], "abc") is False


def test_contains_hash_matches_non_string_members():
    assert contains_hash([b"abc".decode()], "abc") is True


def test_status_formats_never_deleted_as_dash(tmp_path):
    store = TombstoneStore(tmp_path / "t.json")
    store.record("abc", deleted_at=0.0)
    store.load()
    # deleted_at=0 视为「没有删除时间」，展示成「从未」而不是 1970 年
    assert store.status()["tombstones"][0]["deleted_text"] == "从未"


def test_status_reports_file_existence(tmp_path):
    path = tmp_path / "t.json"
    store = TombstoneStore(path)
    assert store.status()["exists"] is False
    store.record("abc")
    assert store.status()["exists"] is True


def test_export_does_not_ack_when_journal_write_fails(tmp_path, monkeypatch):
    """写入失败时墓碑必须保持待发，否则删除静默丢失。"""
    src = Storage(tmp_path / "a.db")
    item = make_item(src, "写日志会失败的场景")
    tombstones = build_tombstone_store(tmp_path / "a-tomb.json")
    folder = tmp_path / "shared"
    sync = _folder_sync(src, folder, "A", tombstones)
    tombstones.record(item.hash)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", boom)
    with pytest.raises(OSError):
        sync.export()

    assert tombstones.status()["pending"] == 1, "写失败后仍应待重试"
    src.close()


def test_ack_only_marks_hashes_that_reached_the_journal(tmp_path):
    """确认送达的依据是「日志里真的有它」，不是「调用过 export」。"""
    src = Storage(tmp_path / "a.db")
    folder = tmp_path / "shared"
    tombstones = build_tombstone_store(tmp_path / "a-tomb.json")
    sync = _folder_sync(src, folder, "A", tombstones)

    # 日志里没有它 → 不该被确认
    tombstones.record("只在墓碑里、还没进日志的指纹")
    assert sync._ack_sent_tombstones() == 0
    assert tombstones.status()["pending"] == 1

    # 导出之后它确实写进日志了 → 允许确认
    item = make_item(src, "真正写进日志的")
    tombstones.record(item.hash)
    # 1 条内容 + 2 条删除标记
    assert sync.export() == 3
    status = tombstones.status()
    assert status["pending"] == 0, "写进日志的两条删除都该被确认"
    acked = {row["hash"] for row in status["tombstones"] if row["acknowledged"]}
    assert item.hash in acked
    src.close()
