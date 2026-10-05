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
    assert raw["version"] == 3
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


# ---------- 条目游标的锚点 ----------


def _anchor_at(records):
    """给 `entry_is_valid` 用的锚点取值器：序号从 1 起。"""
    return lambda index: records[index - 1] if 1 <= index <= len(records) else ""


def test_entry_without_anchor_falls_back_to_length(tmp_path):
    """老版本文件没有锚点：只比长度，不能凭空判失效。"""
    store = SyncCursorStore(tmp_path / "c.json")
    store.load()
    store.advance_entry("folder:a.llsync", 2)
    assert store.anchor_for("folder:a.llsync") == ""
    assert store.entry_is_valid("folder:a.llsync", 5, _anchor_at(["x"] * 5)) is True
    # 比游标短仍然判失效 —— 锚点缺位不该把这条也放过
    assert store.entry_is_valid("folder:a.llsync", 1, _anchor_at(["x"])) is False


def test_entry_anchor_detects_rewrite_at_same_length(tmp_path):
    """**本用例是本轮修复的核心**：长度相等、内容全换，必须判失效。

    只看长度的判据（`position > len`）看不出来：「读到第 2 条」在裁剪前
    指 c1、裁剪后指 n0，长度都是 2。判成「位置仍然有效」的后果是
    从第 2 条之后开始读，而每条新记录都落在那之前 —— 一轮一轮跳过，
    界面上写着「同步成功、0 条」。
    """
    store = SyncCursorStore(tmp_path / "c.json")
    store.load()
    store.advance_entry("folder:a.llsync", 2, anchor="c1")

    assert store.anchor_for("folder:a.llsync") == "c1"
    # 序号 2 现在指 n0（长度一样）→ 位置不再成立
    assert store.entry_is_valid("folder:a.llsync", 2, _anchor_at(["n0", "n1"])) is False
    # 还是原来那一条 → 位置仍然成立
    assert store.entry_is_valid("folder:a.llsync", 2, _anchor_at(["c0", "c1"])) is True


def test_entry_anchor_survives_reload(tmp_path):
    path = tmp_path / "c.json"
    store = SyncCursorStore(path)
    store.load()
    store.advance_entry("folder:a.llsync", 3, anchor="h3")

    reloaded = SyncCursorStore(path)
    reloaded.load()
    assert reloaded.entry_for("folder:a.llsync") == 3
    assert reloaded.anchor_for("folder:a.llsync") == "h3"
    assert reloaded.entry_is_valid("folder:a.llsync", 3, _anchor_at(["a", "b", "c"])) is False


def test_advance_entry_keeps_anchor_when_not_given(tmp_path):
    """只推数字、不给锚点时保留原锚点。

    本地日志是追加式的，已读过那些条目不会因为追加而改变。
    """
    store = SyncCursorStore(tmp_path / "c.json")
    store.load()
    store.advance_entry("k", 1, anchor="first")
    store.advance_entry("k", 2)
    assert store.anchor_for("k") == "first"
    assert store.entry_for("k") == 2


def test_advance_entry_refuses_retreat(tmp_path):
    """只增不减；想回退必须走 `reset_entry`（它会连带清掉锚点）。"""
    store = SyncCursorStore(tmp_path / "c.json")
    store.load()
    store.advance_entry("k", 5, anchor="h5")
    assert store.advance_entry("k", 2, anchor="h2") == 5
    assert store.anchor_for("k") == "h5"

    assert store.reset_entry("k") == 5
    assert store.entry_for("k") == 0
    assert store.anchor_for("k") == ""


def test_reset_only_clears_lan_cursors(tmp_path):
    """`reset()` 只管局域网的 `cursors`，不该动共享文件夹的条目进度。

    两种位置回答的是两个不同的问题（向对端请求到哪 / 本地处理到哪），
    一次全清会让两种同步模式互相牵连。这也是为什么清空命令走的是
    `reset_all()` 而不是给 `reset()` 加个参数 —— 语义不同，就不该合并。
    条目进度另由 `reset_entry()`（按来源回退）与 `reset_entry_cursors()`
    （全部清零）负责。
    """
    path = tmp_path / "c.json"
    store = SyncCursorStore(path)
    store.load()
    store.set("lan:1.2.3.4:5000", 100.0)
    store.advance_entry("folder:a.llsync", 2, anchor="h2")
    store.reset()

    reloaded = SyncCursorStore(path)
    reloaded.load()
    assert reloaded.snapshot() == {}
    assert reloaded.entry_for("folder:a.llsync") == 2
    assert reloaded.anchor_for("folder:a.llsync") == "h2"


def test_reset_entry_cursors_clears_progress_and_anchors(tmp_path):
    """`reset()` 的另一半：条目进度也要能单独清零。

    为什么需要它 —— 条目游标的失效判据要求在**读得到对端日志**时才能
    走到 `reset_entry()`（先 `entry_is_valid()` 判失效，再清零）。对端
    日志已经不在本机时（换了同步目录、对端换了机器名），那条路径永远
    走不到，于是文件里长期留着一批指向不存在日志的位置，而且没有任何
    命令能让它们归零 —— 用户只能手动删文件。
    """
    path = tmp_path / "c.json"
    store = SyncCursorStore(path)
    store.load()
    store.advance_entry("folder:a.llsync", 4, anchor="h4")
    store.advance_entry("folder:b.llsync", 9, anchor="h9")

    assert store.reset_entry_cursors() == 2

    reloaded = SyncCursorStore(path)
    reloaded.load()
    assert reloaded.entry_for("folder:a.llsync") == 0
    assert reloaded.anchor_for("folder:a.llsync") == ""
    assert reloaded.entry_snapshot() == {}


def test_reset_all_clears_both_kinds_and_writes_nothing_left(tmp_path):
    """「重新全量扫描」必须真的是全量：两种位置都清掉，文件里不留痕。

    回归闸门对应的是一个真实症状：`--sync-cursor-reset` 报「已清空 1 条」，
    而后紧跟着跑 `--sync-cursor` 仍然列出另外两条 —— 因为它数的是
    `len(snapshot())`（只有局域网那份），清的也只有那一份。
    """
    path = tmp_path / "c.json"
    store = SyncCursorStore(path)
    store.load()
    store.set("lan:1.2.3.4:5000", 100.0, since_id=3)
    for index in range(3):
        store.advance_entry(f"folder:peer-{index}.llsync", 5 + index, anchor=f"h{index}")

    lan, entries = store.reset_all()
    assert (lan, entries) == (1, 3)

    reloaded = SyncCursorStore(path)
    reloaded.load()
    assert reloaded.snapshot() == {}
    assert reloaded.entry_snapshot() == {}
    # 落盘的内容也要干净：留一个空 `anchors` 没关系，但不能留条目
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["cursors"] == {}
    assert payload["entries"] == {}
    assert payload["anchors"] == {}
    assert reloaded.status()["count"] == 0
    assert reloaded.status()["lan_count"] == 0
    assert reloaded.status()["entry_count"] == 0


def test_reset_all_on_empty_store_reports_zero(tmp_path):
    """空存储上重复清空不该报数，也不该写文件。"""
    path = tmp_path / "c.json"
    store = SyncCursorStore(path)
    store.load()
    assert store.reset_all() == (0, 0)
    assert not path.exists()


def test_status_count_is_the_number_of_rows(tmp_path):
    """`count` 要与 `cursors` 列表的行数一致，另给两类各自的个数。

    `status['count']` 此前是行数，而 `--sync-cursor-reset` 却拿
    `len(snapshot())` 当总数 —— 两个数在只有共享文件夹游标时是
    「1 与 0」的关系，于是「清了 1 条、还剩 2 条」。
    """
    store = SyncCursorStore(tmp_path / "c.json")
    store.load()
    store.set("lan:a:1", 1700000000.0)
    store.advance_entry("folder:a.llsync", 2, anchor="h2")
    store.advance_entry("folder:b.llsync", 2, anchor="h2")

    status = store.status()
    assert status["count"] == len(status["cursors"]) == 3
    assert status["lan_count"] == 1
    assert status["entry_count"] == 2


def test_forget_clears_entry_and_anchor_too(tmp_path):
    """`forget(key)` 要清掉这个来源的**全部**位置。

    只 pop `cursors` 会留下 `entries` / `anchors`：同一个来源下次出现时
    `entry_for()` 仍返回旧值，`entry_is_valid()` 拿一个**属于上次那个对端**
    的锚点去核对新日志 —— 核对结果只取决于「新日志第 N 条的指纹恰好等于
    旧值吗」。恰好相等时判成「位置仍有效」，于是从第 N 条之后开始读，
    前面的记录全部跳过，而界面写着「同步成功、0 条」。
    """
    path = tmp_path / "c.json"
    store = SyncCursorStore(path)
    store.load()
    store.set("lan:1.2.3.4:5000", 111.0, since_id=7)
    store.advance_entry("folder:peer.llsync", 2, anchor="OLD")

    assert store.forget("folder:peer.llsync") is True
    assert store.entry_for("folder:peer.llsync") == 0
    assert store.anchor_for("folder:peer.llsync") == ""

    reloaded = SyncCursorStore(path)
    reloaded.load()
    assert reloaded.entry_for("folder:peer.llsync") == 0
    assert reloaded.anchor_for("folder:peer.llsync") == ""


def test_forget_then_rename_does_not_inherit_stale_anchor(tmp_path):
    """换名后的对端不该继承旧来源的锚点 —— 这是上一条的后果面。

    旧行为下：`forget` 留下锚点 `h1`；新日志第 2 条恰好也是 `h1` 时
    `entry_is_valid()` 判「位置仍有效」，第 1 条新记录被静默跳过。
    清干净之后判据退化为「位置 0 → 有效，从头读」，两条都在。
    """
    path = tmp_path / "c.json"
    store = SyncCursorStore(path)
    store.load()
    store.advance_entry("folder:old-name.llsync", 2, anchor="same")
    store.forget("folder:old-name.llsync")

    journal = ["same", "extra"]
    assert store.entry_is_valid(
        "folder:old-name.llsync", len(journal), lambda index: journal[index - 1]
    ) is True
    assert store.entry_for("folder:old-name.llsync") == 0


def test_entry_is_valid_treats_position_zero_as_valid(tmp_path):
    store = SyncCursorStore(tmp_path / "c.json")
    store.load()
    assert store.entry_is_valid("k", 0, _anchor_at([])) is True
    assert store.entry_is_valid("k", 10, _anchor_at(["x"] * 10)) is True


def test_anchor_getter_failure_does_not_declare_invalid(tmp_path):
    """取锚点出错时保守地判「有效」。

    判失效的代价是一轮全量重扫；判有效却出错才是丢数据。但取不到锚点
    意味着**无从核对**，把它当成失效会让每轮都重扫一遍 —— 所以退回
    只比长度，与没有锚点时一致。
    """
    store = SyncCursorStore(tmp_path / "c.json")
    store.load()
    store.advance_entry("k", 2, anchor="h2")

    def boom(_index):
        raise RuntimeError("日志读不出来")

    assert store.entry_is_valid("k", 2, boom) is True


def test_status_exposes_anchor(tmp_path):
    store = SyncCursorStore(tmp_path / "c.json")
    store.load()
    store.advance_entry("folder:a.llsync", 1, anchor="h1")
    row = store.status()["cursors"][0]
    assert row["anchor"] == "h1"


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


def test_rewritten_peer_journal_at_same_length_is_rescanned(tmp_path):
    """对端裁到与游标**等长**之后，新记录仍然必须传过来。

    这是本轮修的那个静默丢失：日志被裁到上限附近之后会长期停在
    那个长度上，而导入侧只比长度（`position > len`）就发现不了重写 ——
    「读到第 2 条」在裁剪前指 c1、裁剪后指 n0，长度一样。
    结果是从第 2 条之后开始读，而每条新记录都落在那之前：
    一轮一轮跳过，界面上写着「同步成功、0 条」。
    """
    source, target, exporter, importer = _folder_pair(tmp_path)
    _seed(source, [(f"c{i}", 1000.0 + i) for i in range(6)])
    exporter.export()
    assert importer.import_all() == 6

    # 裁到 2 条：对端长度为 2，而本地游标是 6 → 长度判据能发现
    assert exporter.compact(keep=2) is True
    assert importer.import_all() == 0
    assert importer.cursors.entry_for("folder:laptop.llsync") == 2

    # 现在对端长度**恰好等于**游标 2。再新增一条并裁回 2 条 ——
    # 长度永远在 2 上打转，只看长度的判据永远不会发现序号已经换过一轮。
    for index in range(3):
        source.add(
            ClipboardItem(
                kind="text", content=f"n{index}", preview=f"n{index}",
                created_at=2000.0 + index, hash=f"hn{index}",
            )
        )
        exporter.export()
        exporter.compact(keep=2)
        imported = importer.import_all()
        assert imported == 1, f"第 {index} 条新记录被静默跳过了"
        assert target.find_by_hash(f"hn{index}") is not None


def test_rewrite_is_detected_even_when_journal_only_grows(tmp_path):
    """日志比游标更长时也要核对锚点，不能只信「够长」。

    重写未必让日志变短：换掉全部内容并保留更长的日志，
    「长度 ≥ 游标」成立，但序号已经指向别的记录了。
    """
    source, target, exporter, importer = _folder_pair(tmp_path)
    _seed(source, [("one", 1000.0), ("two", 1001.0)])
    exporter.export()
    assert importer.import_all() == 2
    assert importer.cursors.entry_for("folder:laptop.llsync") == 2

    # 对端日志被整个重写：裁到 1 条再补两条新的，长度 3 > 游标 2
    exporter.compact(keep=1)
    for index in range(2):
        source.add(
            ClipboardItem(
                kind="text", content=f"新{index}", preview=f"新{index}",
                created_at=3000.0 + index, hash=f"hx{index}",
            )
        )
        exporter.export()

    imported = importer.import_all()
    assert imported >= 1, "重写后的日志被当成「仍有效」，新记录全被跳过"
    for index in range(2):
        assert target.find_by_hash(f"hx{index}") is not None


def test_unrewritten_journal_keeps_cursor_incremental(tmp_path):
    """锚点不能把正常路径变成「每轮全量重扫」。

    日志只追加不重写时，游标必须继续增量工作：第二轮导入 0 条，
    而不是靠 hash 去重把整份日志再读一遍。
    """
    source, target, exporter, importer = _folder_pair(tmp_path)
    _seed(source, [("one", 1000.0), ("two", 1001.0)])
    exporter.export()
    assert importer.import_all() == 2
    assert importer.import_all() == 0
    assert importer.cursors.entry_for("folder:laptop.llsync") == 2
    assert importer.cursors.anchor_for("folder:laptop.llsync") == "h0"

    source.add(
        ClipboardItem(kind="text", content="three", preview="three", created_at=1002.0, hash="h2")
    )
    exporter.export()
    assert importer.import_all() == 1
    assert importer.cursors.anchor_for("folder:laptop.llsync") == "h2"


def test_cursor_without_anchor_still_works_after_upgrade(tmp_path):
    """老版本写的游标文件没有锚点：升级后不能因此读错位置。

    缺锚点时退回只比长度 —— 这不完美（等长重写仍然看不出来），
    但第一轮读完就会把锚点补上，之后就是完整的判定。
    """
    shared = tmp_path / "shared"
    source, target = Storage(tmp_path / "a.db"), Storage(tmp_path / "b.db")
    cursor_path = tmp_path / "cursor.json"
    cursor_path.write_text(
        json.dumps(
            {
                "version": 2,
                "cursors": {},
                "entries": {"folder:laptop.llsync": 2},
            }
        ),
        encoding="utf-8",
    )
    assert FolderSync(source, shared, device_name="laptop").export() == 0

    importer = FolderSync(
        target, shared, device_name="desktop",
        cursors=SyncCursorStore(cursor_path),
    )
    assert importer.cursors.anchor_for("folder:laptop.llsync") == ""


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
