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


def test_images_not_exported_by_default(tmp_path):
    shared = tmp_path / "shared"
    source = Storage(tmp_path / "a.db")
    source.add(ClipboardItem(kind="image", image_path="images/a.png", hash="img"))
    assert FolderSync(source, shared, device_name="a").export() == 0


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
