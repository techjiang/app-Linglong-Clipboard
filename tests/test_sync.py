"""跨设备同步：导出、导入、加密、容错。"""

import json

import pytest

from linglong_clip.core.models import ClipboardItem
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


def test_import_marks_records_as_synced(tmp_path):
    shared = tmp_path / "shared"
    source, target = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    _seed(source, [("x", "text")])

    FolderSync(source, shared, device_name="a").export()
    FolderSync(target, shared, device_name="b").import_all()

    item = target.list()[0]
    assert item.metadata.get("synced") is True
    assert item.device == "a"


def test_reimport_is_idempotent(tmp_path):
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


def test_multiple_exports_are_appendable(tmp_path):
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


def test_encrypted_export_import_roundtrip(tmp_path):
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
