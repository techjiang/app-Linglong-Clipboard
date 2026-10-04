"""跨设备同步：导出、导入、加密、容错。"""

import json

import pytest

from linglong_clip.core.models import ClipboardItem, content_hash
from linglong_clip.core.privacy import PrivacyGuard
from linglong_clip.core.storage import Storage
from linglong_clip.core.sync import FolderSync, SyncCipher, SyncRecord


def _seed(storage, rows):
    for index, (content, kind) in enumerate(rows):
        storage.add(
            ClipboardItem(
                kind=kind, content=content, preview=content,
                hash=f"h{index}", source_app="chrome",
            )
        )


def test_export_then_import_between_devices(tmp_path, home):
    shared = tmp_path / "shared"
    source = Storage(tmp_path / "a.db")
    target = Storage(tmp_path / "b.db")
    _seed(source, [("hello", "text"), ("https://cnb.cool/x", "link")])

    exporter = FolderSync(source, shared, device_name="laptop")
    importer = FolderSync(target, shared, device_name="desktop", guard=PrivacyGuard())

    assert exporter.export() == 2
    assert importer.import_all() == 2
    assert target.count() == 2
    contents = {item.content for item in target.list()}
    assert contents == {"hello", "https://cnb.cool/x"}


def test_import_marks_records_as_synced(tmp_path, home):
    shared = tmp_path / "shared"
    source, target = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    _seed(source, [("x", "text")])

    FolderSync(source, shared, device_name="a").export()
    FolderSync(target, shared, device_name="b").import_all()

    item = target.list()[0]
    assert item.metadata.get("synced") is True
    assert item.device == "a"


def test_reimport_is_idempotent(tmp_path, home):
    shared = tmp_path / "shared"
    source, target = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    _seed(source, [("x", "text")])

    FolderSync(source, shared, device_name="a").export()
    importer = FolderSync(target, shared, device_name="b")
    assert importer.import_all() == 1
    assert importer.import_all() == 0


def test_sensitive_content_is_not_exported(tmp_path):
    shared = tmp_path / "shared"
    source = Storage(tmp_path / "a.db")
    item = ClipboardItem(kind="text", content="password: hunter2xyz", preview="x",
                         hash="sec", sensitive=True)
    source.add(item)

    assert FolderSync(source, shared, device_name="a").export() == 0


def test_sensitive_content_is_not_imported(tmp_path):
    shared = tmp_path / "shared"
    source, target = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    # 绕过导出侧过滤，直接构造一条敏感记录写进日志
    source.add(ClipboardItem(kind="text", content="password: hunter2xyz", preview="x",
                             hash="sec", sensitive=False))
    FolderSync(source, shared, device_name="a").export()

    importer = FolderSync(target, shared, device_name="b", guard=PrivacyGuard())
    assert importer.import_all() == 0


def test_image_records_export_only_the_digest(tmp_path):
    """图片记录**要**同步，但只带摘要不带字节。

    这是本轮的行为变更（此前整条跳过）。旧行为下对端连「这里曾经有一张图」
    都不知道，跨设备同步在截图这类最常用的内容上等于失效。
    """
    shared = tmp_path / "shared"
    source = Storage(tmp_path / "a.db")
    digest = "a" * 32
    source.add(
        ClipboardItem(
            kind="image",
            image_path=f"images/{digest}.png",
            hash="img",
            # 真机上采集管线会给图片补上 OCR 结果（哪怕图里没字）。
            # 没有这个标记的图被视为「没被读过」不会外发 ——
            # 见 `image_transfer.privacy_inspectable`。
            metadata={"ocr": {"text": "", "backend": "stub"}},
        )
    )
    sync = FolderSync(source, shared, device_name="a")
    assert sync.export() == 1
    record = list(sync._read_journal(sync.journal_path))[0]
    assert record.image_digest == digest
    # 字节一个都不带：`payload_b64` 仍为空
    assert record.payload_b64 == ""


def test_images_can_still_be_excluded_explicitly(tmp_path):
    """`include_images=False` 保留「这些图别传出去」的语义。"""
    shared = tmp_path / "shared"
    source = Storage(tmp_path / "a.db")
    source.add(
        ClipboardItem(
            kind="image",
            image_path="images/" + "a" * 32 + ".png",
            hash="img",
            metadata={"ocr": {"text": "", "backend": "stub"}},
        )
    )
    assert FolderSync(source, shared, device_name="a").export(include_images=False) == 0


def test_own_device_records_not_reimported(tmp_path):
    shared = tmp_path / "shared"
    storage = Storage(tmp_path / "a.db")
    _seed(storage, [("x", "text")])
    sync = FolderSync(storage, shared, device_name="a")
    sync.export()
    assert sync.import_all() == 0


def test_export_skips_already_exported(tmp_path):
    shared = tmp_path / "shared"
    source = Storage(tmp_path / "a.db")
    _seed(source, [("x", "text")])
    sync = FolderSync(source, shared, device_name="a")

    assert sync.export() == 1
    assert sync.export() == 0  # 同一批记录不重复写


def test_empty_export_returns_zero(tmp_path):
    shared = tmp_path / "shared"
    storage = Storage(tmp_path / "a.db")
    assert FolderSync(storage, shared, device_name="a").export() == 0


def test_import_without_folder(tmp_path):
    storage = Storage(tmp_path / "a.db")
    sync = FolderSync(storage, tmp_path / "does-not-exist" / "sub", device_name="a")
    assert sync.import_all() == 0


def test_status_reports_devices(tmp_path):
    shared = tmp_path / "shared"
    storage = Storage(tmp_path / "a.db")
    _seed(storage, [("x", "text")])
    sync = FolderSync(storage, shared, device_name="laptop")
    sync.export()
    status = sync.status()
    assert status["device"] == "laptop"
    assert "laptop" in status["devices"]
    assert status["ready"] is True


def test_device_name_is_sanitized_in_filename(tmp_path):
    shared = tmp_path / "shared"
    storage = Storage(tmp_path / "a.db")
    sync = FolderSync(storage, shared, device_name="my/laptop:2")
    assert "/" not in sync.journal_path.name
    assert ":" not in sync.journal_path.name


def test_multiple_exports_are_appendable(tmp_path, home):
    shared = tmp_path / "shared"
    source, target = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    sync = FolderSync(source, shared, device_name="a")
    importer = FolderSync(target, shared, device_name="b")

    _seed(source, [("one", "text")])
    sync.export()
    source.add(ClipboardItem(kind="text", content="two", preview="two", hash="h-two"))
    sync.export()

    assert importer.import_all() == 2


def test_corrupt_journal_chunk_is_skipped(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(parents=True)
    storage = Storage(tmp_path / "b.db")
    (shared / "broken.llsync").write_bytes(b"\x00\x00\x00\x10garbage-not-16")

    # 不应抛出，只是读不出记录
    assert FolderSync(storage, shared, device_name="b").import_all() == 0


def test_corrupt_json_line_is_skipped(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(parents=True)
    storage = Storage(tmp_path / "b.db")

    payload = b'{"hash": "good", "kind": "text", "content": "ok", "preview": "ok"}\nnot-json\n'
    blob = len(payload).to_bytes(4, "big") + payload
    (shared / "peer.llsync").write_bytes(blob)

    assert FolderSync(storage, shared, device_name="b").import_all() == 1


def test_compact_trims_old_entries(tmp_path):
    shared = tmp_path / "shared"
    storage = Storage(tmp_path / "a.db")
    sync = FolderSync(storage, shared, device_name="a")

    for index in range(6):
        storage.add(ClipboardItem(kind="text", content=f"c{index}", preview=f"c{index}",
                                  hash=f"h{index}", created_at=1000 + index))
        sync.export()

    assert sync.compact(keep=3) is True
    assert len(list(sync._read_journal(sync.journal_path))) == 3


def test_compact_noop_when_under_limit(tmp_path):
    shared = tmp_path / "shared"
    storage = Storage(tmp_path / "a.db")
    _seed(storage, [("x", "text")])
    sync = FolderSync(storage, shared, device_name="a")
    sync.export()
    assert sync.compact(keep=10) is False


def test_sync_record_roundtrip():
    record = SyncRecord(hash="h", kind="text", content="c", preview="p",
                        tags=["a"], device="dev")
    restored = SyncRecord.from_json(json.loads(json.dumps(record.to_json())))
    assert restored.hash == "h"
    assert restored.tags == ["a"]


def test_sync_record_rejects_missing_hash():
    assert SyncRecord.from_json({"kind": "text"}) is None
    assert SyncRecord.from_json("not a dict") is None


def test_sync_record_to_item():
    record = SyncRecord(hash="h", kind="link", content="https://x", preview="https://x",
                        device="peer")
    item = record.to_item()
    assert item.kind == "link"
    assert item.device == "peer"
    assert item.metadata["synced"] is True


def test_cipher_disabled_passthrough():
    cipher = SyncCipher(False)
    assert cipher.enabled is False
    assert cipher.encrypt(b"abc") == b"abc"
    assert cipher.decrypt(b"abc") == b"abc"


def test_cipher_roundtrip():
    cipher = SyncCipher(True, password="secret")
    if not cipher.enabled:
        pytest.skip("未安装 cryptography")
    token = cipher.encrypt(b"top secret")
    assert token != b"top secret"
    assert cipher.decrypt(token) == b"top secret"


def test_cipher_wrong_password_returns_none():
    first = SyncCipher(True, password="one")
    if not first.enabled:
        pytest.skip("未安装 cryptography")
    second = SyncCipher(True, password="two")
    assert second.decrypt(first.encrypt(b"x")) is None


def test_encrypted_export_import_roundtrip(tmp_path, home):
    shared = tmp_path / "shared"
    source, target = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    _seed(source, [("secret-ish", "text")])

    exporter_cipher = SyncCipher(True, password="shared-key")
    importer_cipher = SyncCipher(True, password="shared-key")
    if not exporter_cipher.enabled:
        pytest.skip("未安装 cryptography")

    FolderSync(source, shared, device_name="a", cipher=exporter_cipher).export()
    FolderSync(target, shared, device_name="b", cipher=importer_cipher).import_all()
    assert target.count() == 1

    # 明文不应出现在日志里
    raw = FolderSync(source, shared, device_name="a").journal_path.read_bytes()
    assert b"secret-ish" not in raw


# ---- 全量导出：历史超过 limit 时的完整性 ----
#
# 这一组守的是「设备历史比每页批量更大时，老记录不能丢」。
# 旧实现拿 `storage.list(limit=N)` 当「全部历史」，而它只返回界面排序下的
# 前 N 条；超过 N 的部分永远进不了日志，且会因为「已发送」集合而自我固化，
# 后续任何轮次都不会补上 —— 静默的永久丢数据。


def _seed_many(storage, total, *, prefix="h"):
    """写入 `total` 条内容各异的记录（hash 依次递增）。"""
    for index in range(total):
        storage.add(
            ClipboardItem(
                kind="text",
                content=f"item-{index}",
                preview=f"item-{index}",
                hash=f"{prefix}{index:05d}",
                created_at=1000 + index,
            )
        )


def test_export_covers_history_beyond_page_limit(tmp_path, home):
    """历史 800 条、默认每页 500：导出的日志必须含全部 800 条。"""
    shared = tmp_path / "shared"
    source = Storage(tmp_path / "a.db")
    _seed_many(source, 800)

    sync = FolderSync(source, shared, device_name="a")
    assert sync.export() == 800

    exported = {record.hash for record in sync._read_journal(sync.journal_path)}
    assert len(exported) == 800
    assert {f"h{index:05d}" for index in range(800)} == exported


def test_large_history_fully_transfers_to_fresh_peer(tmp_path, home):
    """新设备首同步必须拿到全部历史，而不是只有最新一页。"""
    shared = tmp_path / "shared"
    source, target = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    _seed_many(source, 800)

    FolderSync(source, shared, device_name="a").export()
    importer = FolderSync(target, shared, device_name="b")
    assert importer.import_all() == 800

    have = {item.hash for item in target.list(limit=10_000)}
    missing = {f"h{index:05d}" for index in range(800)} - have
    assert missing == set(), f"这些记录永久拿不到：{sorted(missing)[:5]}"


def test_export_is_still_incremental_for_new_records(tmp_path, home):
    """修成全量遍历之后，重复导出仍不该重发已导出过的记录。"""
    shared = tmp_path / "shared"
    source = Storage(tmp_path / "a.db")
    _seed_many(source, 600)
    sync = FolderSync(source, shared, device_name="a")

    assert sync.export() == 600
    assert sync.export() == 0  # 没有新内容，一条都不该重复写

    source.add(ClipboardItem(kind="text", content="new", preview="new", hash="brand-new"))
    assert sync.export() == 1  # 只补新增的这一条


def test_export_pages_are_not_affected_by_usage_order(tmp_path, home):
    """界面排序会被「使用」改写；导出走 id 序，翻页期间不该漏读。

    `used_at` 每次复制都会变，若导出依赖它排序，翻页时记录会上下移动，
    出现同一批被读两遍或某条被跳过。这里对最老的一条做一次 `touch()`，
    它在界面排序里会跳到最前，但导出结果必须仍是完整 800 条。
    """
    shared = tmp_path / "shared"
    source = Storage(tmp_path / "a.db")
    _seed_many(source, 800)

    oldest = source.list(limit=10_000)[-1]
    source.touch(oldest.id)

    sync = FolderSync(source, shared, device_name="a")
    assert sync.export() == 800
    assert len(set(r.hash for r in sync._read_journal(sync.journal_path))) == 800


def test_large_local_history_does_not_duplicate_peer_records(tmp_path, home):
    """本地历史超过 MAX_SYNC_ENTRIES 时，重放对端日志不该写出重复行。

    去重靠的 `seen_local` 以前也是 `list(limit=5000)`；本地 6000 条时，
    最老的 1000 条不在集合里，对端每次重放都会把它们再写一遍。
    这里用「清游标强制全量重放三次」把问题放大出来。
    """
    shared = tmp_path / "shared"
    peer, local = Storage(tmp_path / "p.db"), Storage(tmp_path / "l.db")

    _seed_many(peer, 600, prefix="p")
    FolderSync(peer, shared, device_name="p").export()

    _seed_many(local, 6000, prefix="l")

    sync = FolderSync(local, shared, device_name="l")
    sync.import_all()
    assert local.count() == 6600

    for _ in range(3):
        sync.cursors.reset()
        sync.import_all()

    assert local.count() == 6600, "重放对端日志写入了重复记录"
    counts: dict = {}
    for item in local.list(limit=100_000):
        counts[item.hash] = counts.get(item.hash, 0) + 1
    assert [h for h, n in counts.items() if n > 1] == []


def test_json_backend_export_covers_full_history(tmp_path, home):
    """JSON 后端与 SQLite 语义一致：同样要导全，不能只看最新一批。"""
    from linglong_clip.core.repository import JsonRepository

    shared = tmp_path / "shared"
    source = JsonRepository(tmp_path / "a.json")
    _seed_many(source, 800)

    sync = FolderSync(source, shared, device_name="a")
    assert sync.export() == 800

    target = JsonRepository(tmp_path / "b.json")
    assert FolderSync(target, shared, device_name="b").import_all() == 800
    assert {item.hash for item in target.list(limit=10_000)} == {
        f"h{index:05d}" for index in range(800)
    }


def test_unread_image_digest_is_not_advertised(tmp_path):
    """OCR 没跑过的图，连摘要都不报给对端。

    图片记录的 `content` 是文件名，隐私判定对它天然失效 —— 只有 OCR
    结果能穿透进去。没读过时我们没有任何依据说它不敏感，
    而报出摘要就等于告诉对端「这里有张图可以来取」。

    实测过这条路径：关掉 OCR 时，一张写着 `BEGIN RSA PRIVATE KEY` 的截图
    会以 `sensitive=False` 入库、进同步、字节可被对端取走，界面上一切正常。
    """
    shared = tmp_path / "shared"
    source = Storage(tmp_path / "a.db")
    unread = "a" * 32
    read = "b" * 32
    source.add(
        ClipboardItem(kind="image", image_path=f"images/{unread}.png", hash="unread")
    )
    source.add(
        ClipboardItem(
            kind="image",
            image_path=f"images/{read}.png",
            hash="read",
            metadata={"ocr": {"text": "", "backend": "stub"}},
        )
    )

    sync = FolderSync(source, shared, device_name="a")
    assert sync.export() == 2
    by_hash = {r.hash: r for r in sync._read_journal(sync.journal_path)}
    assert by_hash["unread"].image_digest == ""
    assert by_hash["read"].image_digest == read


def test_record_with_past_timestamp_is_still_imported(tmp_path, home):
    """新记录的时间戳可能早于上一次同步的时刻，它仍然必须被导入。

    位置原先只按 `created_at` 过滤，而时间戳**由写入方给、不保证单调**：
    从系统剪贴板历史导入、从别处迁移、以及任何显式构造
    `ClipboardItem(created_at=过去)` 的路径都会写下比位置更早的时间。

    这类记录恰好落在位置之前，于是被永久跳过 —— 而导出侧只看 `hash`
    去重，照发不误。两边都觉得自己是对的：B 说「发了」，C 说「没收到」。
    """
    import time

    shared = tmp_path / "shared"
    source, target = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    now = time.time()

    _seed(source, [("first", "text")])
    exporter = FolderSync(source, shared, device_name="a")
    importer = FolderSync(target, shared, device_name="b")
    assert exporter.export() == 1
    assert importer.import_all() == 1

    # 新记录，但时间戳在过去（历史导入的典型形状）
    source.add(
        ClipboardItem(
            kind="text", content="imported", preview="imported",
            hash="h-imported", created_at=now - 3600,
        )
    )
    assert exporter.export() == 1
    assert importer.import_all() == 1, "带过去时间戳的新记录被位置过滤掉了"
    assert {item.content for item in target.iter_all()} == {"first", "imported"}


def test_past_timestamp_record_arrives_in_later_round(tmp_path, home):
    """同一件事的第二轮：位置已经推过该时刻之后，才追加这条老时间戳记录。

    第一版用例里「过去的时间戳」仍然晚于首次导入的时刻，于是恰好落在
    安全窗口能覆盖的范围内、被顺带捞回来 —— 看起来是绿的，其实没测到。
    这里让位置先推进到现在，再追加一条一小时前的记录。
    """
    import time

    shared = tmp_path / "shared"
    source, target = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")

    _seed(source, [("early", "text")])
    exporter = FolderSync(source, shared, device_name="a")
    importer = FolderSync(target, shared, device_name="b")
    exporter.export()
    assert importer.import_all() == 1

    # 先让这一对同步到「现在」的位置
    source.add(ClipboardItem(kind="text", content="now", preview="now", hash="h-now"))
    exporter.export()
    assert importer.import_all() == 1

    # 现在追加一条一小时前的记录 —— 位置已经在它之后了
    source.add(
        ClipboardItem(
            kind="text", content="late-old", preview="late-old",
            hash="h-late-old", created_at=time.time() - 3600,
        )
    )
    assert exporter.export() == 1
    assert importer.import_all() == 1, "位置推进过之后，老时间戳的新记录被漏掉了"
    assert "late-old" in {item.content for item in target.iter_all()}


def test_import_is_incremental_by_record_not_by_clock(tmp_path, home):
    """增量语义是「只拉没见过的记录」，不是「只拉时间戳更晚的」。

    反方向守一条：别为了不漏而退化成每轮全量重扫 —— 重复导入仍须为 0。
    """
    shared = tmp_path / "shared"
    source, target = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    _seed(source, [("a", "text"), ("b", "text")])

    FolderSync(source, shared, device_name="s").export()
    importer = FolderSync(target, shared, device_name="t")
    assert importer.import_all() == 2
    assert importer.import_all() == 0
    assert importer.import_all() == 0


def test_trimmed_remote_journal_rescans_instead_of_skipping(tmp_path, home):
    """对端把日志裁剪过之后，条目序号会整体前移。

    此时本地游标指向的是**别人的**位置 —— 必须退回全量重扫，
    而不是照着旧序号往下读（那会把没读过的记录判成「已读过」）。
    """
    shared = tmp_path / "shared"
    source, target = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    exporter = FolderSync(source, shared, device_name="a")
    importer = FolderSync(target, shared, device_name="b")

    _seed(source, [("one", "text"), ("two", "text")])
    exporter.export()
    assert importer.import_all() == 2
    assert importer.cursors.entry_for("folder:a.llsync") == 2

    # 对端裁到只剩最后一条
    assert exporter.compact(keep=1) is True
    assert importer.import_all() == 0  # 剩下那条已经见过

    # 再新增一条，位置仍然要对得上
    source.add(ClipboardItem(kind="text", content="three", preview="three", hash="h3"))
    exporter.export()
    assert importer.import_all() == 1
    assert "three" in {item.content for item in target.iter_all()}


def test_folder_cursor_is_entry_based_not_time_based(tmp_path, home):
    """文件夹模式的位置是「读到第几条」，不是一个时间点。

    `status()` 里给出的也是条目数 —— 报一个恒为 0 的时间戳会让人误以为
    「一直没同步过」，而这正是这一轮要消灭的那类误导。
    """
    shared = tmp_path / "shared"
    source, target = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    exporter = FolderSync(source, shared, device_name="a")
    importer = FolderSync(target, shared, device_name="b")

    _seed(source, [("one", "text")])
    exporter.export()
    importer.import_all()

    assert importer.cursors.entry_for("folder:a.llsync") == 1
    row = next(
        item for item in importer.status()["device_details"] if item["device"] == "a"
    )
    assert row["entries"] == 1
    assert row["synced"] is True
    assert "since" not in row


def test_two_devices_do_not_share_sidecar_files(tmp_path, home):
    """同一个进程里模拟两台设备时，辅助文件必须各自独立。

    游标 / 墓碑 / 标记变更此前默认落在全局数据目录，两台设备于是共用
    同一份文件：A 的动作被当成 B 的，标记互相抵消。真机上两台设备是
    两个进程、两个数据目录，所以这个坑只在测试与「一台机器模拟两台」时
    才踩得到 —— 而它踩下去是静默的（标记永远同步不过去）。
    """
    shared = tmp_path / "shared"
    (tmp_path / "dev-a").mkdir()
    (tmp_path / "dev-b").mkdir()

    first = FolderSync(
        Storage(tmp_path / "dev-a" / "h.db"), shared, device_name="a"
    )
    second = FolderSync(
        Storage(tmp_path / "dev-b" / "h.db"), shared, device_name="b"
    )

    assert first.flag_changes.path != second.flag_changes.path
    assert first.tombstones.path != second.tombstones.path
    assert first.cursors.path != second.cursors.path


def test_flag_propagates_with_independent_sidecars(tmp_path, home):
    """上一条的功能面：辅助文件独立之后，收藏真的传得过去。

    同进程共用标记变更文件时，B 的收藏会被记成「本机动作」并被 A
    当成自己的意图 —— 于是永远不会有「对端发生了这个动作」这件事，
    收藏停在 B 上不动。
    """
    shared = tmp_path / "shared"
    (tmp_path / "dev-a").mkdir()
    (tmp_path / "dev-b").mkdir()
    left, right = Storage(tmp_path / "dev-a" / "h.db"), Storage(tmp_path / "dev-b" / "h.db")

    digest = content_hash("text", "共享片段")
    left.add(
        ClipboardItem(kind="text", content="共享片段", preview="共享片段", hash=digest)
    )
    sync_a = FolderSync(left, shared, device_name="a")
    sync_b = FolderSync(right, shared, device_name="b")

    sync_a.export()
    sync_b.import_all()
    right.update_fields(right.find_by_hash(digest).id, favorite=True)

    sync_b.export()
    sync_a.import_all()
    assert left.find_by_hash(digest).favorite is True


def test_compacted_journal_does_not_resend_old_records(tmp_path, home):
    """裁掉日志之后，不再重发那些已经发过的记录。

    `compact()` 每轮同步都会调（`Application` 里就是 `export → import → compact`），
    它把旧条目从日志里移除。而「发过没发过」此前只读日志 ——
    于是裁掉的记录被当成「没发过」重新导出。

    这在按时间过滤的年代只是浪费带宽（对端靠 hash 去重）；位置改成
    「读到第几条」之后它会**丢数据**：重发的旧记录追加在日志末尾，
    对端的位置却已经推过去了，那条记录于是永远读不到。
    """
    shared = tmp_path / "shared"
    source = Storage(tmp_path / "a.db")
    sync = FolderSync(source, shared, device_name="a")

    for index in range(6):
        source.add(
            ClipboardItem(
                kind="text", content=f"c{index}", preview=f"c{index}",
                hash=f"h{index}", created_at=1000 + index,
            )
        )
    assert sync.export() == 6
    assert sync.compact(keep=3) is True
    assert len(list(sync._read_journal(sync.journal_path))) == 3

    # 裁掉的三条不该被重新发出去
    assert sync.export() == 0, "裁掉的旧记录被重发了"
    assert len(list(sync._read_journal(sync.journal_path))) == 3


def test_resend_after_compaction_does_not_lose_records(tmp_path, home):
    """上一条的端到端后果：裁剪 + 新增之后，对端仍能收到新增的那条。"""
    shared = tmp_path / "shared"
    source, target = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    exporter = FolderSync(source, shared, device_name="a")
    importer = FolderSync(target, shared, device_name="b")

    for index in range(6):
        source.add(
            ClipboardItem(
                kind="text", content=f"c{index}", preview=f"c{index}",
                hash=f"h{index}", created_at=1000 + index,
            )
        )
    exporter.export()
    assert importer.import_all() == 6

    exporter.compact(keep=3)
    assert importer.import_all() == 0  # 什么都没变

    source.add(
        ClipboardItem(kind="text", content="new", preview="new", hash="h-new")
    )
    assert exporter.export() == 1
    assert importer.import_all() == 1, "裁剪之后新增的记录没传过去"
    assert "new" in {item.content for item in target.iter_all()}


def test_sent_ledger_stays_out_of_the_shared_folder(tmp_path, home):
    """「已发过」的账本是本机私有的，不该写进共享目录。

    共享目录可能是一个同步盘（OneDrive / iCloud / Syncthing），
    放进去会被带到别的机器上 —— 对方会看到一个自己看不懂的文件，
    而它对别的机器毫无意义（记的是「**我**发过什么」）。
    共享目录里只该有 `*.llsync` 这些对端要读的东西。
    """
    shared = tmp_path / "shared"
    (tmp_path / "dev-a").mkdir()
    sync = FolderSync(
        Storage(tmp_path / "dev-a" / "h.db"), shared, device_name="a"
    )
    _seed(sync._storage, [("x", "text")])
    assert sync.export() == 1

    # 账本落在数据目录，与数据库同级
    assert sync._sent_ledger_path.parent == tmp_path / "dev-a"
    assert not str(sync._sent_ledger_path).startswith(str(shared))
    # 共享目录里只有对端日志
    assert sorted(p.name for p in shared.iterdir()) == ["a.llsync"]


def test_sent_ledger_corruption_does_not_break_export(tmp_path, home):
    """账本损坏时不该让导出失败 —— 它只是「防止重发」的辅助信息。

    丢了最坏是多发一轮，对端靠 `hash` 去重。为它抛异常会把整轮同步带下去，
    代价与收益完全不成比例。
    """
    shared = tmp_path / "shared"
    sync = FolderSync(
        Storage(tmp_path / "a.db"), shared, device_name="a"
    )
    sync._sent_ledger_path.write_text("{ 这不是 JSON", encoding="utf-8")

    _seed(sync._storage, [("x", "text")])
    assert sync.export() == 1
    # 损坏被当成「还没发过」，于是下轮会重发一次 —— 可接受
    assert sync.export() == 0  # 日志里已有，仍不该重发
