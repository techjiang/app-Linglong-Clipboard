"""同步增量游标：持久化、推进语义、失败不推进、与两条同步路径的集成。"""

import json
import time

import pytest

from linglong_clip.core.models import ClipboardItem
from linglong_clip.core.storage import Storage
from linglong_clip.core.sync import FolderSync, SyncCipher
from linglong_clip.core.sync_cursor import (
    SAFETY_MARGIN,
    SyncCursorStore,
    build_cursor_store,
)


# ---------- 存储本身 ----------


def test_new_store_starts_empty(tmp_path):
    store = SyncCursorStore(tmp_path / "c.json")
    store.load()
    assert store.snapshot() == {}
    assert store.get("lan:a:1") == 0.0


def test_set_and_reload_round_trips(tmp_path):
    path = tmp_path / "c.json"
    store = SyncCursorStore(path)
    store.set("lan:1.2.3.4:5000", 1234.5)

    reloaded = SyncCursorStore(path)
    reloaded.load()
    assert reloaded.get("lan:1.2.3.4:5000") == 1234.5


def test_cursor_file_is_human_readable(tmp_path):
    path = tmp_path / "c.json"
    store = SyncCursorStore(path)
    store.set("folder:peer.llsync", 99.0)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 2
    # 每项是 `[时间戳, id]`：只记时间戳时，同刻记录塞满一页会让游标推不动
    assert raw["cursors"]["folder:peer.llsync"] == [99.0, 0]
    assert "updated_at" in raw


def test_advance_is_monotonic(tmp_path):
    store = SyncCursorStore(tmp_path / "c.json")
    store.load()
    assert store.advance("k", 100.0) == (100.0, 0)
    # 反向推进被拒绝：多对端同步时不能被旧时间戳拖回去
    assert store.advance("k", 50.0) == (100.0, 0)
    assert store.get("k") == 100.0
    # 向前推进正常生效
    assert store.advance("k", 200.0) == (200.0, 0)


def test_advance_compares_created_at_then_id(tmp_path):
    """同一时刻内按 id 比较，而不是被当成「没有变化」。"""
    store = SyncCursorStore(tmp_path / "c.json")
    store.load()
    assert store.advance("k", 100.0, since_id=10) == (100.0, 10)
    # 时间相同、id 更大 -> 仍是向前推进
    assert store.advance("k", 100.0, since_id=42) == (100.0, 42)
    assert store.get_pair("k") == (100.0, 42)
    # 时间相同、id 更小 -> 拒绝回退
    assert store.advance("k", 100.0, since_id=7) == (100.0, 42)
    # 时间更早 -> 一律拒绝，哪怕 id 很大
    assert store.advance("k", 99.0, since_id=999) == (100.0, 42)


def test_since_pair_resets_id_when_margin_shifts_time(tmp_path):
    """回退安全窗口后必须把 id 归零。

    否则「回到 T-1 时刻」却仍要求 id > 500，会漏掉 T-1 到 T 之间
    id 较小的记录 —— 回退是为了多读，不能反而读漏。
    """
    store = SyncCursorStore(tmp_path / "c.json")
    store.load()
    store.set("k", 1000.0, since_id=500)
    since, since_id = store.since_pair_for("k")
    assert since == pytest.approx(1000.0 - SAFETY_MARGIN)
    assert since_id == 0


def test_since_pair_keeps_id_when_no_margin_applied(tmp_path):
    """游标为 0 时不回退，id 也保持原样。"""
    store = SyncCursorStore(tmp_path / "c.json")
    store.load()
    assert store.since_pair_for("missing") == (0.0, 0)


def test_reads_legacy_scalar_cursor_file(tmp_path):
    """旧的裸时间戳格式仍能读进来，按 id=0 处理。"""
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps({"version": 1, "cursors": {"lan:a:1": 1234.5}}), encoding="utf-8"
    )
    store = SyncCursorStore(path)
    store.load()
    assert store.get_pair("lan:a:1") == (1234.5, 0)


def test_advance_skips_write_when_unchanged(tmp_path):
    path = tmp_path / "c.json"
    store = SyncCursorStore(path)
    store.advance("k", 100.0)
    first_mtime = path.stat().st_mtime_ns

    store.advance("k", 100.0)  # 无变化，不应落盘
    assert path.stat().st_mtime_ns == first_mtime


def test_since_for_applies_safety_margin(tmp_path):
    store = SyncCursorStore(tmp_path / "c.json")
    store.load()
    store.set("k", 1000.0)
    assert store.since_for("k") == pytest.approx(1000.0 - SAFETY_MARGIN)


def test_since_for_never_negative(tmp_path):
    store = SyncCursorStore(tmp_path / "c.json")
    store.load()
    assert store.since_for("missing") == 0.0


def test_corrupt_file_resets_instead_of_raising(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{ this is not json", encoding="utf-8")

    store = SyncCursorStore(path)
    store.load()  # 不应抛异常
    assert store.snapshot() == {}


def test_bad_cursor_values_are_dropped(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps({"version": 1, "cursors": {"good": 1.0, "bad": "abc", "none": None}}),
        encoding="utf-8",
    )
    store = SyncCursorStore(path)
    store.load()
    assert store.snapshot() == {"good": 1.0}


def test_non_dict_cursors_resets(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"version": 1, "cursors": [1, 2]}), encoding="utf-8")
    store = SyncCursorStore(path)
    store.load()
    assert store.snapshot() == {}


def test_reset_clears_all(tmp_path):
    path = tmp_path / "c.json"
    store = SyncCursorStore(path)
    store.set("a", 1.0)
    store.set("b", 2.0)
    store.reset()
    assert store.snapshot() == {}

    reloaded = SyncCursorStore(path)
    reloaded.load()
    assert reloaded.snapshot() == {}


def test_forget_removes_single_key(tmp_path):
    store = SyncCursorStore(tmp_path / "c.json")
    store.set("a", 1.0)
    store.set("b", 2.0)
    assert store.forget("a") is True
    assert store.forget("a") is False
    assert store.snapshot() == {"b": 2.0}


def test_key_for_composes_scope_and_source():
    assert SyncCursorStore.key_for("lan", "1.2.3.4:5000") == "lan:1.2.3.4:5000"
    assert SyncCursorStore.key_for("folder", "x.llsync") == "folder:x.llsync"


def test_status_reports_rows(tmp_path):
    store = SyncCursorStore(tmp_path / "c.json")
    store.set("lan:a:1", 1700000000.0)
    status = store.status()
    assert status["count"] == 1
    assert status["cursors"][0]["key"] == "lan:a:1"
    assert status["cursors"][0]["since"] == 1700000000.0
    assert status["cursors"][0]["since_id"] == 0
    assert status["cursors"][0]["since_text"] != "从未"


def test_status_marks_never_synced(tmp_path):
    store = SyncCursorStore(tmp_path / "c.json")
    store.set("k", 0.0)
    assert store.status()["cursors"][0]["since_text"] == "从未"


def test_unwritable_path_does_not_raise(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    store = SyncCursorStore(blocker / "c.json")
    store.set("k", 1.0)  # 写失败只记日志，不应影响同步流程
    assert store.get("k") == 1.0


def test_build_cursor_store_loads(tmp_path):
    path = tmp_path / "c.json"
    SyncCursorStore(path).set("k", 7.0)
    store = build_cursor_store(path)
    assert store.get("k") == 7.0


# ---------- 与 FolderSync 集成 ----------


def _seed(storage, rows):
    for index, (content, created) in enumerate(rows):
        storage.add(
            ClipboardItem(
                kind="text",
                content=content,
                preview=content,
                created_at=created,
                hash=f"h{index}",
            )
        )


def _folder_pair(tmp_path):
    shared = tmp_path / "shared"
    source = Storage(tmp_path / "a.db")
    target = Storage(tmp_path / "b.db")
    exporter = FolderSync(source, shared, device_name="laptop")
    importer = FolderSync(
        target,
        shared,
        device_name="desktop",
        cursors=SyncCursorStore(tmp_path / "cursor.json"),
    )
    return source, target, exporter, importer


def test_folder_import_only_pulls_new_records(tmp_path):
    source, target, exporter, importer = _folder_pair(tmp_path)
    _seed(source, [("one", 1000.0), ("two", 1001.0)])
    exporter.export()
    assert importer.import_all() == 2

    # 无新数据时游标命中，不再逐条比对
    assert importer.import_all() == 0

    # 新增一条后，只有它会被导入
    source.add(
        ClipboardItem(kind="text", content="three", preview="three", created_at=1002.0, hash="h2")
    )
    exporter.export()
    assert importer.import_all() == 1
    assert target.count() == 3


def test_folder_cursor_isolated_per_peer(tmp_path):
    shared = tmp_path / "shared"
    target = Storage(tmp_path / "c.db")
    importer = FolderSync(
        target,
        shared,
        device_name="desktop",
        cursors=SyncCursorStore(tmp_path / "cursor.json"),
    )

    for offset, name in enumerate(("laptop", "phone")):
        source = Storage(tmp_path / f"{name}.db")
        source.add(
            ClipboardItem(
                kind="text",
                content=f"{name}-1",
                preview=f"{name}-1",
                created_at=1000.0 + offset,
                hash=f"{name}-hash",
            )
        )
        FolderSync(source, shared, device_name=name).export()

    assert importer.import_all() == 2
    # 两个对端各自推进，互不覆盖。位置是「读到日志第几条」，
    # 不是时间戳 —— 时间戳由写入方给、不保证单调，
    # 拿它当位置判据会让带过去时间戳的新记录被永久跳过。
    assert importer.cursors.entry_for("folder:laptop.llsync") == 1
    assert importer.cursors.entry_for("folder:phone.llsync") == 1


def test_folder_no_cursor_store_still_works(tmp_path):
    """不传游标时退化为旧行为（每轮全量扫描），保证向后兼容。"""
    shared = tmp_path / "shared"
    source = Storage(tmp_path / "a.db")
    target = Storage(tmp_path / "b.db")
    _seed(source, [("x", 1000.0)])
    FolderSync(source, shared, device_name="laptop").export()

    importer = FolderSync(target, shared, device_name="desktop")
    assert importer.import_all() == 1
    # 即使没有游标，第二轮仍靠 hash 去重返回 0
    assert importer.import_all() == 0


def test_folder_status_reports_cursor_progress(tmp_path):
    source, target, exporter, importer = _folder_pair(tmp_path)
    _seed(source, [("one", 1000.0)])
    exporter.export()
    importer.import_all()

    details = importer.status()["device_details"]
    row = next(item for item in details if item["device"] == "laptop")
    assert row["synced"] is True
    assert row["entries"] == 1
    assert "cursor_file" in importer.status()


# ---------- 与局域网客户端集成 ----------


def test_lan_sync_peer_advances_cursor(tmp_path, home):
    from linglong_clip.core.lan_sync import LanSync, PeerInfo
    from linglong_clip.core.sync import SyncCipher

    server_storage = Storage(home / "server.db")
    client_storage = Storage(home / "client.db")
    cipher = SyncCipher(False)

    server = LanSync(storage=server_storage, device_name="srv", port=0, cipher=cipher)
    assert server.start()
    try:
        port = server.status()["port"]
        client = LanSync(
            storage=client_storage,
            device_name="cli",
            port=0,
            cipher=cipher,
            cursors=SyncCursorStore(home / "cursor.json"),
        )
        peer = PeerInfo(host="127.0.0.1", port=port)
        sync = client._client

        for index in range(3):
            server_storage.add(
                ClipboardItem(
                    kind="text",
                    content=f"lan {index}",
                    preview=f"lan {index}",
                    created_at=2000.0 + index,
                    hash=f"L{index}",
                )
            )

        assert sync.sync_peer(peer) == 3
        assert sync.sync_peer(peer) == 0  # 游标命中，无新记录
        assert sync.cursors.get(f"lan:127.0.0.1:{port}") == 2002.0

        server_storage.add(
            ClipboardItem(kind="text", content="lan 3", preview="lan 3", created_at=2010.0, hash="L3")
        )
        assert sync.sync_peer(peer) == 1
    finally:
        server.stop()


def test_lan_failure_does_not_advance_cursor(tmp_path, home):
    from linglong_clip.core.lan_sync import LanSync, PeerInfo
    from linglong_clip.core.sync import SyncCipher

    node = LanSync(
        storage=Storage(home / "c.db"),
        device_name="cli",
        port=0,
        cipher=SyncCipher(False),
        cursors=SyncCursorStore(home / "cursor.json"),
    )
    dead = PeerInfo(host="127.0.0.1", port=9)
    assert node._client.sync_peer(dead) == 0
    assert node._client.cursors.snapshot() == {}


def test_lan_explicit_since_overrides_cursor(tmp_path, home):
    from linglong_clip.core.lan_sync import LanSync, PeerInfo
    from linglong_clip.core.sync import SyncCipher

    server_storage = Storage(home / "srv.db")
    cipher = SyncCipher(False)
    server = LanSync(storage=server_storage, device_name="srv", port=0, cipher=cipher)
    assert server.start()
    try:
        port = server.status()["port"]
        client = LanSync(
            storage=Storage(home / "cli.db"),
            device_name="cli",
            port=0,
            cipher=cipher,
            cursors=SyncCursorStore(home / "cursor.json"),
        )
        peer = PeerInfo(host="127.0.0.1", port=port)
        for index in range(3):
            server_storage.add(
                ClipboardItem(
                    kind="text",
                    content=f"c{index}",
                    preview=f"c{index}",
                    created_at=100.0 + index,
                    hash=f"c{index}",
                )
            )
        # 显式 since 跳过最早的两条
        assert client._client.sync_peer(peer, since=101.5) == 1
    finally:
        server.stop()


def test_status_exposes_id_part_of_cursor(tmp_path):
    """状态输出要带上 `since_id`：只显示时间戳会让人以为同刻记录没在推进。"""
    store = SyncCursorStore(tmp_path / "c.json")
    store.set("lan:a:1", 1700000000.0, since_id=321)
    row = store.status()["cursors"][0]
    assert row["since"] == 1700000000.0
    assert row["since_id"] == 321


def test_snapshot_keeps_timestamp_only_shape(tmp_path):
    """`snapshot()` 保持 `key -> 时间戳` 的既有形状，CLI 与旧用例都按它取用。"""
    store = SyncCursorStore(tmp_path / "c.json")
    store.set("k", 100.0, since_id=9)
    assert store.snapshot() == {"k": 100.0}
    assert store.snapshot_pairs() == {"k": (100.0, 9)}


def test_pairs_round_trip_through_file(tmp_path):
    """复合游标要能原样落盘再读回。"""
    path = tmp_path / "c.json"
    SyncCursorStore(path).set("lan:a:1", 1700000000.5, since_id=77)

    reloaded = SyncCursorStore(path)
    reloaded.load()
    assert reloaded.get_pair("lan:a:1") == (1700000000.5, 77)


def test_id_part_survives_reset_and_reload(tmp_path):
    path = tmp_path / "c.json"
    store = SyncCursorStore(path)
    store.reset()
    assert store.get_pair("k") == (0.0, 0)


def test_multi_peer_cursors_advance_independently_with_ids(tmp_path):
    """多个对端各自推进，`id` 部分互不干扰。"""
    store = SyncCursorStore(tmp_path / "c.json")
    store.advance("lan:a:1", 100.0, since_id=5)
    store.advance("lan:b:2", 100.0, since_id=900)
    assert store.get_pair("lan:a:1") == (100.0, 5)
    assert store.get_pair("lan:b:2") == (100.0, 900)
    # a 推进到同一时刻但 id 更大
    store.advance("lan:a:1", 100.0, since_id=6)
    assert store.get_pair("lan:a:1") == (100.0, 6)
    assert store.get_pair("lan:b:2") == (100.0, 900)


def test_malformed_pair_entries_are_dropped(tmp_path):
    """`[ts, id]` 写坏时只丢这一项，不连累其它游标。"""
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "cursors": {
                    "good": [100.0, 5],
                    "bad_short": [100.0],
                    "bad_text": ["x", "y"],
                    "bad_null": None,
                },
            }
        ),
        encoding="utf-8",
    )
    store = SyncCursorStore(path)
    store.load()
    assert store.get_pair("good") == (100.0, 5)
    assert store.get_pair("bad_short") == (0.0, 0)
    assert store.get_pair("bad_text") == (0.0, 0)
    assert store.get_pair("bad_null") == (0.0, 0)
