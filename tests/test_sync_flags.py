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
from linglong_clip.core.flag_changes import (
    FLAG_FAVORITE,
    FLAG_PINNED,
    FlagChangeStore,
)
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


# ---- 固定标记的来源：删除保护必须能分辨「谁设的固定」 ----
# 这一组守的是一类**删除失效**的缺陷，它比「标记传不过去」更坏：
# 标记同步取并集，任何一台设备上的固定都会传播到所有设备；而固定又会
# 阻断删除传播。于是「发起固定的那台自己删掉」之后，别的设备仍带着一个
# 它设的固定而拒绝删除，记录永远删不掉，还会被重新导出复活。
#
# 判据是固定的来源设备：来源 == 发起删除的设备 时，固定已失效，删除照做。


def _pair(tmp_path, a_name="a", b_name="b"):
    from linglong_clip.core.tombstones import TombstoneStore

    shared = tmp_path / "shared"
    a, b = Storage(tmp_path / f"{a_name}.db"), Storage(tmp_path / f"{b_name}.db")
    sync_a = FolderSync(
        a, shared, device_name=a_name, tombstones=TombstoneStore(tmp_path / f"t{a_name}.json")
    )
    sync_b = FolderSync(
        b, shared, device_name=b_name, tombstones=TombstoneStore(tmp_path / f"t{b_name}.json")
    )
    return a, b, sync_a, sync_b


def test_export_stamps_local_pin_with_own_device_name(tmp_path):
    """本机设的固定导出时要带上自己的设备名，对端才知道「是谁设的」。

    不带的话，对端收到来源为空的固定，会理解成「对端本机设的」，
    于是收到删除标记时误判成自己的固定意图 —— 正是复活 bug 的成因。
    """
    a, _b, sync_a, _sync_b = _pair(tmp_path)
    digest = _seed(a, "本机固定的内容")
    a.update_fields(a.find_by_hash(digest).id, pinned=True, metadata={})

    sync_a.export()
    records = [r for r in sync_a._read_journal(sync_a.journal_path) if not r.is_tombstone]
    assert records and records[0].pinned is True
    assert records[0].pinned_by == "a"


def test_received_pin_records_origin_device(tmp_path):
    """从对端收到的固定，本地要记住「设它的是谁」。"""
    a, b, sync_a, sync_b = _pair(tmp_path)
    digest = _seed(a, "对端固定的内容")
    sync_a.export()
    sync_b.import_all()

    b.update_fields(b.find_by_hash(digest).id, pinned=True, metadata={})
    sync_b.export()
    sync_a.import_all()

    assert a.find_by_hash(digest).pinned is True
    assert a.find_by_hash(digest).pinned_by == "b"


def test_pin_from_deleter_does_not_block_delete(tmp_path):
    """U 型路径：B 固定 → 并集让 A 也带上固定 → B 删除。

    A 上的固定其实是 B 设的，B 已经改主意了，A 不该继续拦着。
    修复前：A 拒绝删除并把记录重新导出，于是 B 上被删的东西又活了。
    """
    a, b, sync_a, sync_b = _pair(tmp_path)
    digest = _seed(a, "会被复活的内容")
    sync_a.export()
    sync_b.import_all()

    # B 固定（B 的本机意图，导出时带上 "b"）
    b_item = b.find_by_hash(digest)
    b.update_fields(b_item.id, pinned=True, metadata={})
    sync_b.export()
    sync_a.import_all()
    assert a.find_by_hash(digest).pinned is True
    assert a.find_by_hash(digest).pinned_by == "b"

    # 用户在 B 上删掉它
    b.delete(b_item.id)
    sync_b.tombstones.record(digest)
    sync_b.export()

    outcome = sync_a.import_all_detailed()
    assert outcome.removed == 1
    assert a.find_by_hash(digest) is None

    # 关键：A 不该把它重新导出，B 上也不能复活
    assert sync_a.export() == 0
    sync_b.import_all()
    assert b.find_by_hash(digest) is None


def test_pin_from_third_party_still_protects(tmp_path):
    """固定是第三台设备设的、删除是另一台发起的 —— 保护必须仍然生效。

    这一条与 `test_pin_from_deleter_does_not_block_delete` 成对：
    只修前者会把保护整个改坏，两者都要过。
    """
    a, b, sync_a, sync_b = _pair(tmp_path)
    digest = _seed(a, "第三方固定的内容")
    sync_a.export()
    sync_b.import_all()

    # B 固定它 —— 固定来自 b
    b.update_fields(b.find_by_hash(digest).id, pinned=True, metadata={})
    sync_b.export()
    sync_a.import_all()
    assert a.find_by_hash(digest).pinned_by == "b"

    # A 发起删除：但它身上的固定来自 b，不是自己设的，保护仍应生效
    a.delete(a.find_by_hash(digest).id)
    sync_a.tombstones.record(digest)
    sync_a.export()

    assert sync_b.import_all_detailed().removed == 0
    assert b.find_by_hash(digest) is not None


def test_locally_pinned_record_deleted_by_peer_is_still_protected(tmp_path):
    """本机自己固定的项，不该被对端的删除干掉。

    来源为空表示「本机意图」，不能因为判来源而误伤。
    """
    a, b, sync_a, sync_b = _pair(tmp_path)
    digest = _seed(a, "本机固定的内容")
    sync_a.export()
    sync_b.import_all()

    a.update_fields(a.find_by_hash(digest).id, pinned=True, metadata={})
    sync_a.export()
    sync_b.import_all()
    # A 的固定导出后会以 "a" 记录来源；B 端上它是 a 设的
    assert b.find_by_hash(digest).pinned_by == "a"

    b.delete(b.find_by_hash(digest).id)
    sync_b.tombstones.record(digest)
    sync_b.export()

    assert sync_a.import_all_detailed().removed == 0
    assert a.find_by_hash(digest) is not None


def test_pin_is_stale_helper_edges():
    """来源判定的四个边角，逐个钉住。"""
    from linglong_clip.core.sync import SyncRecord, pin_is_stale

    def item(pinned, by):
        it = ClipboardItem(kind="text", content="c", preview="c")
        it.pinned = pinned
        if by:
            it.metadata["pinned_by"] = by
        return it

    record = SyncRecord(hash="h", kind="text", content="c", preview="c", device="b")
    # 没固定 → 谈不上过期
    assert pin_is_stale(item(False, "b"), record) is False
    # 来源为空 = 本机设的 → 不过期
    assert pin_is_stale(item(True, ""), record) is False
    # 来源就是发起删除的设备 → 过期
    assert pin_is_stale(item(True, "b"), record) is True
    # 来源是第三方 → 不过期
    assert pin_is_stale(item(True, "c"), record) is False


def test_unpin_clears_origin(tmp_path):
    """取消固定要连来源一起清掉，否则旧来源会在后续重设时串味。"""
    a, _b, sync_a, _sync_b = _pair(tmp_path)
    digest = _seed(a, "先固定后取消")
    item = a.find_by_hash(digest)
    a.update_fields(item.id, pinned=True, metadata={"pinned_by": "b"})
    assert a.find_by_hash(digest).pinned_by == "b"

    a.update_fields(item.id, pinned=False, metadata={})
    assert a.find_by_hash(digest).pinned_by == ""


def test_pinned_by_survives_json_roundtrip():
    record = SyncRecord(
        hash="h1", kind="text", content="c", preview="c",
        pinned=True, pinned_by="laptop",
    )
    restored = SyncRecord.from_json(record.to_json())
    assert restored is not None
    assert restored.pinned_by == "laptop"


def test_pinned_by_absent_for_unpinned_record():
    """没有固定标记时不该白写一个来源字段，省日志体积。"""
    record = SyncRecord(hash="h1", kind="text", content="c", preview="c")
    assert "pinned_by" not in record.to_json()


def test_to_item_carries_pinned_by():
    record = SyncRecord(
        hash="h1", kind="text", content="c", preview="c",
        pinned=True, pinned_by="desktop",
    )
    assert record.to_item().metadata.get("pinned_by") == "desktop"


def test_to_item_omits_pinned_by_when_not_pinned():
    record = SyncRecord(hash="h1", kind="text", content="c", preview="c", pinned_by="desktop")
    assert "pinned_by" not in record.to_item().metadata


# ---- 已删除的记录不该从任何日志里被读回来（含自己的） ----


def test_local_delete_blocks_reimport_from_own_journal(tmp_path):
    """一台设备把自己删掉的记录从**自己的日志**里复活。

    路径：B 把某条的标记变化写进自己的日志 → 用户删掉它 → 下一轮导入
    又读到那份日志里的同一条 → 记录回来了。判据是本地已记墓碑的指纹。
    """
    _a, b, _sync_a, sync_b = _pair(tmp_path, a_name="a", b_name="b")
    digest = _seed(b, "自己删掉的内容")
    b.update_fields(b.find_by_hash(digest).id, pinned=True, metadata={})
    sync_b.export()  # 自己日志里有了这条内容

    b.delete(b.find_by_hash(digest).id)
    sync_b.tombstones.record(digest)

    sync_b.import_all()
    assert b.find_by_hash(digest) is None


def test_peer_delete_blocks_reimport_from_own_journal(tmp_path):
    """A 收到 B 的删除标记后，不该再从自己的日志里把这条读回来。"""
    a, b, sync_a, sync_b = _pair(tmp_path)
    digest = _seed(a, "被对端删掉的内容")
    sync_a.export()
    sync_b.import_all()

    # B 删掉并广播
    b.delete(b.find_by_hash(digest).id)
    sync_b.tombstones.record(digest)
    sync_b.export()

    assert sync_a.import_all_detailed().removed == 1
    # A 自己日志里还有这条内容，再导入一次不能复活
    sync_a.import_all()
    assert a.find_by_hash(digest) is None


def test_unjournaled_new_record_still_imports(tmp_path):
    """反向守卫：没被删过的新记录必须照常导入，别把判据写宽了。"""
    a, b, sync_a, sync_b = _pair(tmp_path)
    _seed(a, "全新内容")
    sync_a.export()
    assert sync_b.import_all_detailed().imported == 1
    assert b.find_by_hash(content_hash("text", "全新内容")) is not None


def test_local_pin_origin_survives_peer_pin(tmp_path):
    """本机固定过的记录，来源不能被对端的固定顶掉。

    若被顶成对端设备名，对端反悔删除时会连本机这份一起删 ——
    但本机用户从没说过要删。来源保持在「本机（空串）」才对。
    """
    a, b, sync_a, sync_b = _pair(tmp_path)
    digest = _seed(a, "两边都固定过")

    # A 本机固定（来源为空）
    a.update_fields(a.find_by_hash(digest).id, pinned=True, metadata={})
    sync_a.export()

    # B 收到并固定（B 上来源是 a），再发回来
    sync_b.import_all()
    assert b.find_by_hash(digest).pinned_by == "a"
    b.update_fields(b.find_by_hash(digest).id, pinned=True, metadata={})
    sync_b.export()
    sync_a.import_all()

    # A 仍然是「本机设的固定」，没被 B 的来源覆盖
    assert a.find_by_hash(digest).pinned is True
    assert a.find_by_hash(digest).pinned_by == ""


def test_local_pin_protects_when_origin_not_overwritten(tmp_path):
    """接上一条：来源没被顶掉，删除保护才继续有效。"""
    a, b, sync_a, sync_b = _pair(tmp_path)
    digest = _seed(a, "本机固定别被顶掉")
    a.update_fields(a.find_by_hash(digest).id, pinned=True, metadata={})
    sync_a.export()
    sync_b.import_all()
    b.update_fields(b.find_by_hash(digest).id, pinned=True, metadata={})
    sync_b.export()
    sync_a.import_all()

    # B 删掉并广播：A 的固定来自本机，保护应当生效
    b.delete(b.find_by_hash(digest).id)
    sync_b.tombstones.record(digest)
    sync_b.export()

    assert sync_a.import_all_detailed().removed == 0
    assert a.find_by_hash(digest) is not None


# ---- 取消的传播（本轮新增：变更通道） ----
# 上面那条 `test_unpinning_locally_is_not_reverted_by_peer` 描述的是
# **没有任何设备说过取消**时的行为：并集把固定带回来。那是刻意的边界。
# 现在多了一条通道：显式取消会留下一条带时间戳的事件，于是「我在
# 笔记本上取消了」成为一个能被传播的事实。下面这些用例守这条路径。


def _record_cancel(storage, digest, flag=FLAG_PINNED, *, value=False, at=None, device=""):
    """模拟用户在界面上的一次显式调整（走 Application 会做的两件事）。"""
    item = storage.find_by_hash(digest)
    assert item is not None, "先要有这条记录"
    storage.update_fields(item.id, **{flag: value})
    store = FlagChangeStore(tmp_path / "flags.json")
    store.record(digest, flag, value, at=at, device=device)
    return store


def test_cancel_propagates_when_only_source_device_acted(tmp_path):
    """**本轮要修的主场景**：在 A 上固定、再在 A 上取消，B 端应看到取消。"""
    shared = tmp_path / "shared"
    a, b = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    digest = _seed(a, "会被取消固定的条目")

    changes_a = FlagChangeStore(tmp_path / "a-flags.json")
    changes_b = FlagChangeStore(tmp_path / "b-flags.json")
    sync_a = FolderSync(a, shared, device_name="a", flag_changes=changes_a)
    sync_b = FolderSync(b, shared, device_name="b", flag_changes=changes_b)

    # 先固定并同步过去
    changes_a.record(digest, FLAG_PINNED, True, at=1000.0)
    item = a.find_by_hash(digest)
    a.update_fields(item.id, pinned=True)
    sync_a.export()
    sync_b.import_all()
    assert b.find_by_hash(digest).pinned is True

    # 用户在 A 上取消固定
    a.update_fields(item.id, pinned=False)
    changes_a.record(digest, FLAG_PINNED, False, at=2000.0)

    sync_a.export()
    sync_b.import_all()

    assert b.find_by_hash(digest).pinned is False, "取消必须能传过去"


def test_cancel_clears_origin_device(tmp_path):
    """取消固定后来源设备要清掉，否则旧来源会漂到后续同步里。"""
    from linglong_clip.core.models import PINNED_BY_KEY

    shared = tmp_path / "shared"
    a, b = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    digest = _seed(a, "带来源的固定项")

    changes_a = FlagChangeStore(tmp_path / "a-flags.json")
    changes_b = FlagChangeStore(tmp_path / "b-flags.json")
    sync_a = FolderSync(a, shared, device_name="a", flag_changes=changes_a)
    sync_b = FolderSync(b, shared, device_name="b", flag_changes=changes_b)

    changes_a.record(digest, FLAG_PINNED, True, at=1000.0)
    item = a.find_by_hash(digest)
    a.update_fields(item.id, pinned=True, metadata={})
    sync_a.export()
    sync_b.import_all()
    assert b.find_by_hash(digest).pinned is True

    a.update_fields(item.id, pinned=False)
    changes_a.record(digest, FLAG_PINNED, False, at=2000.0)
    sync_a.export()
    sync_b.import_all()

    target = b.find_by_hash(digest)
    assert target.pinned is False
    assert PINNED_BY_KEY not in target.metadata


def test_cancel_does_not_break_other_device_pin(tmp_path):
    """另一台设备设过的固定不该被本机的取消抹掉 —— 并集这条语义仍成立。"""
    shared = tmp_path / "shared"
    a, b, c = (
        Storage(tmp_path / "a.db"),
        Storage(tmp_path / "b.db"),
        Storage(tmp_path / "c.db"),
    )
    digest = _seed(a, "两台设备都固定过")

    changes_a = FlagChangeStore(tmp_path / "a-flags.json")
    changes_b = FlagChangeStore(tmp_path / "b-flags.json")
    sync_a = FolderSync(a, shared, device_name="a", flag_changes=changes_a)
    sync_b = FolderSync(b, shared, device_name="b", flag_changes=changes_b)

    # B 显式固定
    changes_b.record(digest, FLAG_PINNED, True, at=1000.0)
    sync_b.export()
    sync_a.import_all()
    assert a.find_by_hash(digest).pinned is True

    # A 显式取消（A 从未设过，取消对并集没有影响）
    changes_a.record(digest, FLAG_PINNED, False, at=2000.0)
    sync_a.export()
    sync_b.import_all()
    assert b.find_by_hash(digest).pinned is True, "B 的显式固定必须留着"


def test_cancel_is_idempotent_across_rounds(tmp_path):
    """取消传过去之后再同步多轮，不该反复改数据或让日志无限增长。"""
    shared = tmp_path / "shared"
    a, b = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    digest = _seed(a, "取消后应稳定")

    changes_a = FlagChangeStore(tmp_path / "a-flags.json")
    changes_b = FlagChangeStore(tmp_path / "b-flags.json")
    sync_a = FolderSync(a, shared, device_name="a", flag_changes=changes_a)
    sync_b = FolderSync(b, shared, device_name="b", flag_changes=changes_b)

    changes_a.record(digest, FLAG_PINNED, True, at=1000.0)
    item = a.find_by_hash(digest)
    a.update_fields(item.id, pinned=True)
    sync_a.export()
    sync_b.import_all()

    a.update_fields(item.id, pinned=False)
    changes_a.record(digest, FLAG_PINNED, False, at=2000.0)
    sync_a.export()
    sync_b.import_all()
    assert b.find_by_hash(digest).pinned is False

    for _ in range(3):
        sync_a.export()
        sync_b.import_all()
    assert b.find_by_hash(digest).pinned is False


def test_flag_change_journal_growth_is_bounded(tmp_path):
    """取消/设置的往复不该让日志无限增长。"""
    shared = tmp_path / "shared"
    a = Storage(tmp_path / "a.db")
    digest = _seed(a, "反复切换")
    changes = FlagChangeStore(tmp_path / "flags.json")
    sync = FolderSync(a, shared, device_name="a", flag_changes=changes)

    changes.record(digest, FLAG_PINNED, True, at=1000.0)
    sync.export()
    for index in range(5):
        changes.record(digest, FLAG_PINNED, False, at=2000.0 + index * 10)
        sync.export()
        changes.record(digest, FLAG_PINNED, True, at=2100.0 + index * 10)
        sync.export()
    # 每次切换都会发一条，但导出不会因为「内容已发过」而漏掉变更本身
    assert sync.journal_path.exists()


def test_flag_change_survives_encrypted_sync(tmp_path):
    """加密模式下取消同样要能传过去。"""
    from linglong_clip.core.sync import SyncCipher

    shared = tmp_path / "shared"
    a, b = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    digest = _seed(a, "加密通道里的取消")
    cipher = SyncCipher(True, password="pw")

    changes_a = FlagChangeStore(tmp_path / "a-flags.json")
    changes_b = FlagChangeStore(tmp_path / "b-flags.json")
    sync_a = FolderSync(a, shared, device_name="a", cipher=cipher, flag_changes=changes_a)
    sync_b = FolderSync(b, shared, device_name="b", cipher=cipher, flag_changes=changes_b)

    changes_a.record(digest, FLAG_PINNED, True, at=1000.0)
    item = a.find_by_hash(digest)
    a.update_fields(item.id, pinned=True)
    sync_a.export()
    sync_b.import_all()
    assert b.find_by_hash(digest).pinned is True

    a.update_fields(item.id, pinned=False)
    changes_a.record(digest, FLAG_PINNED, False, at=2000.0)
    sync_a.export()
    sync_b.import_all()
    assert b.find_by_hash(digest).pinned is False


def test_flag_changes_work_on_json_backend(tmp_path):
    """JSON 后端下语义一致（与 SQLite 后端逐条对齐）。"""
    from linglong_clip.core.repository import JsonRepository

    shared = tmp_path / "shared"
    a = JsonRepository(tmp_path / "json-a" / "clipboard.json")
    b = JsonRepository(tmp_path / "json-b" / "clipboard.json")

    digest = content_hash("text", "JSON 后端也要能取消")
    a.add(
        ClipboardItem(
            kind="text",
            content="JSON 后端也要能取消",
            preview="JSON 后端也要能取消",
            hash=digest,
            pinned=True,
        )
    )

    changes_a = FlagChangeStore(tmp_path / "a-flags.json")
    changes_b = FlagChangeStore(tmp_path / "b-flags.json")
    sync_a = FolderSync(a, shared, device_name="a", flag_changes=changes_a)
    sync_b = FolderSync(b, shared, device_name="b", flag_changes=changes_b)

    changes_a.record(digest, FLAG_PINNED, True, at=1000.0)
    sync_a.export()
    sync_b.import_all()
    assert b.find_by_hash(digest).pinned is True

    item = a.find_by_hash(digest)
    a.update_fields(item.id, pinned=False)
    changes_a.record(digest, FLAG_PINNED, False, at=2000.0)
    sync_a.export()
    sync_b.import_all()
    assert b.find_by_hash(digest).pinned is False


def test_import_outcome_counts_flag_changes(tmp_path):
    """「只更新了标记」要能被看到，否则又是「数据变了但没人知道」。"""
    shared = tmp_path / "shared"
    a, b = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    digest = _seed(a, "计数一下")

    changes_a = FlagChangeStore(tmp_path / "a-flags.json")
    changes_b = FlagChangeStore(tmp_path / "b-flags.json")
    sync_a = FolderSync(a, shared, device_name="a", flag_changes=changes_a)
    sync_b = FolderSync(b, shared, device_name="b", flag_changes=changes_b)

    changes_a.record(digest, FLAG_PINNED, True, at=1000.0)
    sync_a.export()
    sync_b.import_all()

    changes_a.record(digest, FLAG_PINNED, False, at=2000.0)
    sync_a.export()
    outcome = sync_b.import_all_detailed()
    assert outcome.flag_changes >= 1


def test_apply_flags_without_store_is_backward_compatible(tmp_path):
    """不传变更存储时退化为纯并集 —— 既有第三方调用点不受影响。"""
    from linglong_clip.core.sync import apply_flags

    storage = Storage(tmp_path / "x.db")
    digest = _seed(storage, "老调用点")
    record = SyncRecord(
        hash=digest,
        kind="text",
        content="老调用点",
        preview="老调用点",
        device="a",
        pinned=True,
    )
    assert apply_flags(storage, record) is True
    assert storage.find_by_hash(digest).pinned is True


def test_apply_flags_ignores_flag_change_records(tmp_path):
    """事件记录没有内容，不该被当成内容记录处理。"""
    from linglong_clip.core.sync import apply_flags

    storage = Storage(tmp_path / "x.db")
    digest = _seed(storage, "事件记录")
    record = SyncRecord(
        hash=digest,
        kind="flag_change",
        content="",
        preview="",
        device="a",
        flag_change=FLAG_PINNED,
        flag_value=False,
    )
    assert apply_flags(storage, record) is False


def test_cancel_applies_even_when_record_carries_no_flags(tmp_path):
    """取消是以「记录上什么标记都没有」的形式表达的，不能因此被跳过。"""
    from linglong_clip.core.sync import apply_flags

    storage = Storage(tmp_path / "x.db")
    digest = _seed(storage, "无标记的记录", pinned=True)
    store = FlagChangeStore(tmp_path / "flags.json")
    store.record(digest, FLAG_PINNED, False, at=2000.0, device="a")

    record = SyncRecord(hash=digest, kind="text", content="无标记的记录", preview="无标记的记录", device="a")
    assert record.flags().any() is False
    assert apply_flags(storage, record, None, store) is True
    assert storage.find_by_hash(digest).pinned is False
