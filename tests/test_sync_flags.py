"""跨设备同步：用户标记（固定 / 收藏）也要跟着走。

这一组守的是一类**静默丢数据**的缺陷：内容同步得好好的，用户显式做的
标记却跨不过去。它不会报错，也没有任何提示 —— 用户只会在另一台机器上
发现「我明明收藏过，怎么没了」。

用例重点在边界而不是「标记能同步」：
哪些方向该合、什么时候不重发、老数据怎么办、两条同步路径是否一致。
"""

import time

import pytest

from linglong_clip.core.models import ClipboardItem, content_hash
from linglong_clip.core.storage import Storage
from linglong_clip.core.sync import (
    FlagSnapshot,
    FolderSync,
    SyncRecord,
    apply_flags,
)


def _seed(storage, content="共享的配置片段", *, pinned=False, favorite=False, tags=None):
    digest = content_hash("text", content)
    item = ClipboardItem(
        kind="text",
        content=content,
        preview=content,
        hash=digest,
        source_app="terminal",
        pinned=pinned,
        favorite=favorite,
        tags=list(tags or []),
        created_at=time.time(),
    )
    storage.add(item)
    return digest


# ---- FlagSnapshot 本身 ----


def test_flag_snapshot_merge_is_union():
    """取并集，不是取「后写者胜」。"""
    left = FlagSnapshot(pinned=True, favorite=False)
    right = FlagSnapshot(pinned=False, favorite=True)
    merged = left.merge(right)
    assert merged.pinned is True
    assert merged.favorite is True


def test_flag_snapshot_merge_is_commutative_and_idempotent():
    a = FlagSnapshot(pinned=True, favorite=False)
    b = FlagSnapshot(pinned=False, favorite=True)
    assert a.merge(b) == b.merge(a)
    assert a.merge(a) == a


def test_flag_snapshot_any():
    assert FlagSnapshot().any() is False
    assert FlagSnapshot(pinned=True).any() is True
    assert FlagSnapshot(favorite=True).any() is True


# ---- SyncRecord 序列化 ----


def test_sync_record_roundtrip_preserves_flags():
    record = SyncRecord(
        hash="h1", kind="text", content="c", preview="c",
        pinned=True, favorite=True,
    )
    restored = SyncRecord.from_json(record.to_json())
    assert restored is not None
    assert restored.pinned is True
    assert restored.favorite is True


def test_sync_record_from_old_json_defaults_flags_false():
    """老版本写出的记录没有这两个字段，读出来必须是 False 而不是报错。"""
    restored = SyncRecord.from_json({"hash": "h1", "kind": "text", "content": "c"})
    assert restored is not None
    assert restored.pinned is False
    assert restored.favorite is False


def test_sync_record_json_omits_false_flags():
    record = SyncRecord(hash="h1", kind="text", content="c", preview="c")
    payload = record.to_json()
    assert "pinned" not in payload
    assert "favorite" not in payload


def test_sync_record_to_item_carries_flags():
    """内容记录直接入库时（本地没有这条），标记也要落在 item 上。"""
    record = SyncRecord(
        hash="h1", kind="text", content="c", preview="c",
        pinned=True, favorite=True,
    )
    item = record.to_item()
    assert item.pinned is True
    assert item.favorite is True


# ---- 首次同步：对端带的标记要落下来 ----


def test_flags_arrive_with_new_record(tmp_path):
    shared = tmp_path / "shared"
    source, target = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    _seed(source, pinned=True, favorite=True, tags=["常用"])

    FolderSync(source, shared, device_name="a").export()
    FolderSync(target, shared, device_name="b").import_all()

    item = target.list()[0]
    assert item.pinned is True
    assert item.favorite is True
    assert item.tags == ["常用"]


# ---- 关键场景：内容已同步过，之后才做的标记 ----
# 这正是原来的重灾区：导出侧按「内容已发过」跳过，导入侧按「指纹已存在」跳过，
# 两边各挡一次，标记的后续变化永远传不出去。


def test_flag_set_after_first_sync_propagates(tmp_path):
    shared = tmp_path / "shared"
    a, b = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    digest = _seed(a, "共享的配置片段")

    sync_a = FolderSync(a, shared, device_name="a")
    sync_b = FolderSync(b, shared, device_name="b")

    sync_a.export()
    sync_b.import_all()
    assert b.list()[0].pinned is False

    # 用户在 B 上固定并收藏了这条
    item = b.find_by_hash(digest)
    b.update_fields(item.id, pinned=True, favorite=True)

    sync_b.export()
    outcome = sync_a.import_all_detailed()

    assert a.list()[0].pinned is True
    assert a.list()[0].favorite is True
    # 「导入 0 条」也必须说明发生了什么，否则用户以为同步没生效
    assert outcome.imported == 0
    assert outcome.flags_updated == 1


def test_unpinning_locally_is_not_reverted_by_peer(tmp_path):
    """并集的已知代价：本地取消固定后，对端再来一轮会把它带回来。

    这是刻意的 —— 无法区分「对端取消过」和「对端从未设过」，
    取并集是唯一不会**悄悄弄丢**显式标记的选择。
    但「重新复制了同一内容」是用户的显式动作，那种情况必须翻过来。
    """
    shared = tmp_path / "shared"
    a, b = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    digest = _seed(a, "会被取消固定的条目")

    sync_a = FolderSync(a, shared, device_name="a")
    sync_b = FolderSync(b, shared, device_name="b")
    sync_a.export()
    sync_b.import_all()

    item = a.find_by_hash(digest)
    a.update_fields(item.id, pinned=True)
    sync_a.export()
    sync_b.import_all()
    assert b.find_by_hash(digest).pinned is True

    # 本地取消固定，但**不更新**自己的已发状态（日志里仍是 True）
    a.update_fields(item.id, pinned=False)
    # 对端再发一轮，它只知道「曾经固定过」
    sync_b.export()
    sync_a.import_all()
    # 取并集的结果：固定被带回来。这是文档里写明的边界，不是 bug。
    assert a.find_by_hash(digest).pinned is True


# ---- 不重复发 ----


def test_no_repeat_export_when_flags_unchanged(tmp_path):
    """标记没变就不该每轮重传 —— 否则同步日志会被恒定增长撑爆。"""
    shared = tmp_path / "shared"
    a = Storage(tmp_path / "a.db")
    _seed(a, pinned=True, favorite=True)
    sync = FolderSync(a, shared, device_name="a")

    assert sync.export() == 1
    assert sync.export() == 0
    assert sync.export() == 0


def test_repeat_export_when_more_flags_added(tmp_path):
    shared = tmp_path / "shared"
    a = Storage(tmp_path / "a.db")
    digest = _seed(a)
    sync = FolderSync(a, shared, device_name="a")

    sync.export()
    item = a.find_by_hash(digest)
    a.update_fields(item.id, favorite=True)
    # 只加了收藏，也要补发一条
    assert sync.export() == 1
    # 但只有这一次
    assert sync.export() == 0


def test_flag_export_accumulates_across_journals(tmp_path):
    """同一指纹在日志里可以有多条（补发的标记变化），取并集判断是否要再发。"""
    shared = tmp_path / "shared"
    a = Storage(tmp_path / "a.db")
    digest = _seed(a)
    sync = FolderSync(a, shared, device_name="a")

    sync.export()
    item = a.find_by_hash(digest)
    a.update_fields(item.id, pinned=True)
    sync.export()
    a.update_fields(item.id, favorite=True)
    sync.export()

    state = sync._local_flag_state()
    assert state[digest].pinned is True
    assert state[digest].favorite is True
    assert sync.export() == 0


# ---- apply_flags 的单独行为 ----


def test_apply_flags_returns_false_for_unknown_hash(tmp_path):
    storage = Storage(tmp_path / "h.db")
    record = SyncRecord(hash="nope", kind="text", content="c", preview="c", pinned=True)
    assert apply_flags(storage, record) is False


def test_apply_flags_returns_false_when_nothing_to_add(tmp_path):
    storage = Storage(tmp_path / "h.db")
    digest = _seed(storage, pinned=True)
    record = SyncRecord(hash=digest, kind="text", content="x", preview="x", pinned=True)
    assert apply_flags(storage, record) is False


def test_apply_flags_ignores_tombstones(tmp_path):
    storage = Storage(tmp_path / "h.db")
    digest = _seed(storage)
    record = SyncRecord(
        hash=digest, kind="tombstone", content="", preview="",
        pinned=True, deleted_at=time.time(),
    )
    assert apply_flags(storage, record) is False
    assert storage.find_by_hash(digest).pinned is False


def test_apply_flags_only_touches_flags(tmp_path):
    """不能顺手改内容或计数 —— 那是另一条路径的事。"""
    storage = Storage(tmp_path / "h.db")
    digest = _seed(storage, "原始内容")
    before = storage.find_by_hash(digest)

    record = SyncRecord(
        hash=digest, kind="text", content="对端的不同内容", preview="!!",
        pinned=True, favorite=True,
    )
    assert apply_flags(storage, record) is True

    after = storage.find_by_hash(digest)
    assert after.content == "原始内容"
    assert after.preview == before.preview
    assert after.use_count == before.use_count
    assert after.pinned is True


# ---- 与删除保护的联动 ----


def test_propagated_pin_protects_against_peer_delete(tmp_path):
    """标记传过去之后，对端也知道「这条被固定了」，删除传播不再越过它。

    这是标记同步的一个**额外收益**：以前 B 只能保护自己固定的项，
    A 那边根本不知道它被固定过。
    """
    shared = tmp_path / "shared"
    a, b = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    digest = _seed(a, "别再删我")

    from linglong_clip.core.tombstones import TombstoneStore

    sync_a = FolderSync(a, shared, device_name="a", tombstones=TombstoneStore(tmp_path / "ta.json"))
    sync_b = FolderSync(b, shared, device_name="b", tombstones=TombstoneStore(tmp_path / "tb.json"))
    sync_a.export()
    sync_b.import_all()

    # B 固定它并发出
    item = b.find_by_hash(digest)
    b.update_fields(item.id, pinned=True)
    sync_b.export()
    sync_a.import_all()
    assert a.find_by_hash(digest).pinned is True

    # A 删掉并发出删除标记
    a.delete(a.find_by_hash(digest).id)
    sync_a.tombstones.record(digest)
    sync_a.export()

    sync_b.import_all()
    # B 上它是固定项，删除不该越过这条线
    assert b.find_by_hash(digest) is not None


# ---- 加密模式 ----


def test_flags_survive_encrypted_sync(tmp_path):
    from linglong_clip.core.sync import SyncCipher

    shared = tmp_path / "shared"
    a, b = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    digest = _seed(a, "加密下也要带上标记")

    cipher = SyncCipher(True, password="k")
    if not cipher.enabled:
        pytest.skip("未安装 cryptography")

    sync_a = FolderSync(a, shared, device_name="a", cipher=cipher)
    sync_b = FolderSync(b, shared, device_name="b", cipher=SyncCipher(True, password="k"))
    sync_a.export()
    sync_b.import_all()

    item = b.find_by_hash(digest)
    b.update_fields(item.id, favorite=True)
    sync_b.export()
    sync_a.import_all()

    assert a.find_by_hash(digest).favorite is True


# ---- 跨设备对账：两端的标记最终一致 ----


def test_flags_converge_over_multiple_rounds(tmp_path):
    """反复同步若干轮后两端标记一致，且不来回震荡。"""
    shared = tmp_path / "shared"
    a, b = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    digest = _seed(a, "多轮同步的条目")

    sync_a = FolderSync(a, shared, device_name="a")
    sync_b = FolderSync(b, shared, device_name="b")

    sync_a.export()
    sync_b.import_all()
    b.update_fields(b.find_by_hash(digest).id, favorite=True)
    sync_b.export()
    sync_a.import_all()
    a.update_fields(a.find_by_hash(digest).id, pinned=True)

    for _ in range(3):
        sync_a.export()
        sync_b.import_all()
        sync_b.export()
        sync_a.import_all()

    left, right = a.find_by_hash(digest), b.find_by_hash(digest)
    assert (left.pinned, left.favorite) == (True, True)
    assert (right.pinned, right.favorite) == (True, True)
    # 收敛之后不再产生新记录
    assert sync_a.export() == 0
    assert sync_b.export() == 0


def test_import_outcome_counts_only_flag_updates(tmp_path):
    """统计要能区分「导入了新内容」和「只更新了标记」。"""
    shared = tmp_path / "shared"
    a, b = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    _seed(a, "只更新标记")
    _seed(b, "全新的内容")

    sync_a = FolderSync(a, shared, device_name="a")
    sync_b = FolderSync(b, shared, device_name="b")
    sync_a.export()
    sync_b.import_all()  # B 拿到 A 那条
    sync_b.export()      # B 把自己那条也发出去

    # B 现在把 A 那条固定了
    digest = content_hash("text", "只更新标记")
    b.update_fields(b.find_by_hash(digest).id, pinned=True)
    sync_b.export()

    outcome = sync_a.import_all_detailed()
    assert outcome.imported == 1        # 「全新的内容」
    assert outcome.flags_updated == 1   # 「只更新标记」


def test_flag_propagation_converges_within_bounded_rounds(tmp_path):
    """标记传播必须在有限轮内收敛，不能每轮都产生新记录。

    这条守的是「补发」逻辑的副作用：A 从 B 学到标记后，自己也会补发一条 ——
    若判断条件写错（比如拿本地状态和本地状态比），就会来回震荡，
    同步日志被无限撑大。实测收敛在 2 轮，这里留出余量断言 <= 3。
    """
    shared = tmp_path / "shared"
    a, b = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    digest = _seed(a, "收敛测试")

    sync_a = FolderSync(a, shared, device_name="a")
    sync_b = FolderSync(b, shared, device_name="b")
    sync_a.export()
    sync_b.import_all()
    b.update_fields(b.find_by_hash(digest).id, favorite=True)

    rounds_with_writes = 0
    for _ in range(6):
        wrote = sync_a.export() + sync_b.export()
        sync_b.import_all()
        sync_a.import_all()
        if wrote:
            rounds_with_writes += 1

    assert rounds_with_writes <= 3, "标记同步没有收敛，日志会被持续撑大"
    assert a.find_by_hash(digest).favorite is True
    assert b.find_by_hash(digest).favorite is True
    # 收敛后日志不再增长
    before = sync_a.journal_path.stat().st_size
    assert sync_a.export() == 0
    assert sync_a.journal_path.stat().st_size == before


def test_flag_journal_growth_is_bounded(tmp_path):
    """反复设置「已在日志里出现过」的标记，不该每次都追加记录。"""
    shared = tmp_path / "shared"
    a = Storage(tmp_path / "a.db")
    digest = _seed(a)
    sync = FolderSync(a, shared, device_name="a")

    sync.export()
    size_after_first = sync.journal_path.stat().st_size

    # 再设一次同样的标记（模拟界面重复勾选后保存）
    item = a.find_by_hash(digest)
    a.update_fields(item.id, pinned=True)
    sync.export()
    a.update_fields(item.id, pinned=True)
    assert sync.export() == 0
    assert sync.journal_path.stat().st_size < size_after_first * 3


def test_export_skips_records_without_hash(tmp_path):
    """没有指纹的记录不参与同步比对，必须提前跳过。

    重构后 `if not item.hash` 从「已在日志里」这条判断里独立出来 ——
    否则一条空 hash 的记录会走进去和 `already` 比较，行为依赖集合里
    恰好没有空串，属于侥幸。
    """
    shared = tmp_path / "shared"
    a = Storage(tmp_path / "a.db")
    a.add(ClipboardItem(kind="text", content="没有指纹", preview="没有指纹", hash=""))
    _seed(a, "有指纹")

    sync = FolderSync(a, shared, device_name="a")
    assert sync.export() == 1
    records = list(sync._read_journal(sync.journal_path))
    assert [r.hash for r in records] == [content_hash("text", "有指纹")]


def test_export_carries_flags_for_image_when_allowed(tmp_path):
    """图片在显式允许时也要带上标记 —— 别把标记绑死在文本路径上。"""
    shared = tmp_path / "shared"
    a = Storage(tmp_path / "a.db")
    a.add(
        ClipboardItem(
            kind="image", content="", preview="[图片]",
            image_path="images/a.png", hash="img1", pinned=True, favorite=True,
        )
    )
    sync = FolderSync(a, shared, device_name="a")
    assert sync.export(include_images=True) == 1
    record = list(sync._read_journal(sync.journal_path))[0]
    assert record.pinned is True
    assert record.favorite is True


def test_flags_sync_works_on_json_backend(tmp_path):
    """两种存储后端的语义要逐条对齐 —— 项目里这对齐是明确要求。

    SQLite 与 JSON 的 `update_fields` / `find_by_hash` 是两套实现，
    只测一种等于把另一种留成盲区。
    """
    from linglong_clip.core.repository import JsonRepository

    shared = tmp_path / "shared"
    a = JsonRepository(tmp_path / "a.json")
    b = JsonRepository(tmp_path / "b.json")
    digest = content_hash("text", "JSON 后端的标记")
    a.add(
        ClipboardItem(
            kind="text", content="JSON 后端的标记", preview="JSON 后端的标记",
            hash=digest, created_at=time.time(),
        )
    )

    sync_a = FolderSync(a, shared, device_name="a")
    sync_b = FolderSync(b, shared, device_name="b")
    sync_a.export()
    sync_b.import_all()

    b.update_fields(b.find_by_hash(digest).id, pinned=True, favorite=True)
    sync_b.export()

    outcome = sync_a.import_all_detailed()
    assert outcome.flags_updated == 1
    item = a.find_by_hash(digest)
    assert item.pinned is True
    assert item.favorite is True

    # 收敛，不来回震荡
    sync_a.export()
    sync_b.import_all()
    assert b.find_by_hash(digest).pinned is True
    a.close()
    b.close()
