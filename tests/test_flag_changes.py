"""标记变更通道：让「取消固定 / 取消收藏」也能跨设备传播。

这一组守的是一类**用户动作被静默忽略**的缺陷：用户确实点了一次
「取消收藏」，界面变了，但另一台设备上看不出发生过，仍然是已收藏。

用例重点在**并集与取消如何共存**这个边界，而不是「能记下来」：
设备内取最新、设备间取并集、没有变更时不覆盖本地值、
同一动作别重复追加、已送达的不复述。
"""

import json
import time

import pytest

from linglong_clip.core.flag_changes import (
    FLAG_FAVORITE,
    FLAG_PINNED,
    FlagChange,
    FlagChangeStore,
    build_flag_change_store,
    resolve_flag,
)


def _store(tmp_path, name="flag_changes.json"):
    return build_flag_change_store(tmp_path / name)


# ---- FlagChange 本身 ----


def test_change_json_roundtrip():
    change = FlagChange(hash="abc", flag=FLAG_PINNED, value=True, at=1234.5, device="laptop")
    restored = FlagChange.from_json(change.to_json())
    assert restored == change


def test_change_from_json_rejects_bad_shapes():
    assert FlagChange.from_json("不是字典") is None
    assert FlagChange.from_json({"hash": "", "flag": "pinned", "at": 1.0}) is None
    assert FlagChange.from_json({"hash": "a", "flag": "不认识的标记", "at": 1.0}) is None
    assert FlagChange.from_json({"hash": "a", "flag": "pinned", "at": "坏值"}) is None
    assert FlagChange.from_json({"hash": "a", "flag": "pinned", "at": 0}) is None


# ---- resolve_flag：本轮语义的核心 ----


def test_resolve_returns_none_when_nothing_recorded():
    """没人表态时返回 None，调用方据此**保留本地值**而不是当成 False。"""
    assert resolve_flag([], FLAG_PINNED) is None


def test_resolve_latest_per_device_wins():
    """同一台设备内，时间戳最新的那次生效（先设后取消 → 不要）。"""
    changes = [
        FlagChange(hash="a", flag=FLAG_PINNED, value=True, at=1.0, device="laptop"),
        FlagChange(hash="a", flag=FLAG_PINNED, value=False, at=2.0, device="laptop"),
    ]
    assert resolve_flag(changes, FLAG_PINNED) is False


def test_resolve_latest_per_device_wins_for_set():
    """反过来（先取消后设置）也一样，靠时间戳而不是记录顺序。"""
    changes = [
        FlagChange(hash="a", flag=FLAG_PINNED, value=False, at=2.0, device="laptop"),
        FlagChange(hash="a", flag=FLAG_PINNED, value=True, at=3.0, device="laptop"),
    ]
    assert resolve_flag(changes, FLAG_PINNED) is True


def test_resolve_union_across_devices():
    """跨设备取并集：一台取消不抹掉另一台的显式意图。"""
    changes = [
        FlagChange(hash="a", flag=FLAG_PINNED, value=False, at=5.0, device="laptop"),
        FlagChange(hash="a", flag=FLAG_PINNED, value=True, at=1.0, device="desktop"),
    ]
    assert resolve_flag(changes, FLAG_PINNED) is True


def test_resolve_cancel_wins_when_only_that_device_acted():
    """**本轮要修的那条**：只有本机设过又取消，取消必须生效。"""
    changes = [
        FlagChange(hash="a", flag=FLAG_PINNED, value=True, at=1.0, device="laptop"),
        FlagChange(hash="a", flag=FLAG_PINNED, value=False, at=2.0, device="laptop"),
    ]
    assert resolve_flag(changes, FLAG_PINNED) is False


def test_resolve_ignores_other_flags():
    """收藏的变更不该影响固定。"""
    changes = [
        FlagChange(hash="a", flag=FLAG_FAVORITE, value=True, at=1.0, device="laptop"),
    ]
    assert resolve_flag(changes, FLAG_PINNED) is None
    assert resolve_flag(changes, FLAG_FAVORITE) is True


def test_resolve_empty_device_is_its_own_actor():
    """空设备名（本机动作）本身是一个独立的「设备」，不被别的设备顶掉。"""
    changes = [
        FlagChange(hash="a", flag=FLAG_PINNED, value=True, at=1.0, device=""),
        FlagChange(hash="a", flag=FLAG_PINNED, value=False, at=2.0, device="laptop"),
    ]
    assert resolve_flag(changes, FLAG_PINNED) is True


# ---- 存储：记录与幂等 ----


def test_record_and_effective(tmp_path):
    store = _store(tmp_path)
    assert store.record("a", FLAG_PINNED, True, at=1.0, device="laptop") is True
    assert store.effective("a", FLAG_PINNED) is True


def test_record_is_idempotent_for_same_value(tmp_path):
    """同一个值重复登记不新增 —— 否则界面刷新会不断往文件里追加事件。"""
    store = _store(tmp_path)
    assert store.record("a", FLAG_PINNED, True, at=1.0, device="laptop") is True
    assert store.record("a", FLAG_PINNED, True, at=2.0, device="laptop") is False
    assert len(store.all_changes()) == 1


def test_record_cancel_after_set(tmp_path):
    store = _store(tmp_path)
    store.record("a", FLAG_PINNED, True, at=1.0, device="laptop")
    assert store.record("a", FLAG_PINNED, False, at=2.0, device="laptop") is True
    assert store.effective("a", FLAG_PINNED) is False


def test_record_rejects_empty_hash_and_unknown_flag(tmp_path):
    store = _store(tmp_path)
    assert store.record("", FLAG_PINNED, True) is False
    assert store.record("a", "不想认识你", True) is False
    assert store.all_changes() == []


def test_record_many_counts_only_new(tmp_path):
    store = _store(tmp_path)
    added = store.record_many(
        [("a", FLAG_PINNED, True), ("b", FLAG_FAVORITE, True), ("", FLAG_PINNED, True)],
        at=1.0,
        device="laptop",
    )
    assert added == 2
    assert store.record_many([("a", FLAG_PINNED, True)], at=2.0, device="laptop") == 0


def test_record_many_tolerates_bad_entries(tmp_path):
    store = _store(tmp_path)
    added = store.record_many(
        [("a",), ("b", FLAG_PINNED, True), None], at=1.0, device="laptop"
    )
    assert added == 1


def test_effective_none_for_unknown_hash(tmp_path):
    store = _store(tmp_path)
    assert store.effective("没听过", FLAG_PINNED) is None


# ---- 送达确认 ----


def test_pending_excludes_acked(tmp_path):
    store = _store(tmp_path)
    store.record("a", FLAG_PINNED, True, at=1.0, device="laptop")
    store.record("b", FLAG_PINNED, True, at=2.0, device="laptop")
    pending = store.pending()
    assert len(pending) == 2
    keys = [store._key(change) for change in pending]
    store.ack(keys[:1])
    remaining = store.pending()
    assert len(remaining) == 1
    assert store._key(remaining[0]) == keys[1]


def test_ack_is_per_change_not_per_hash(tmp_path):
    """同一指纹的两条变更要能分别确认，不能按指纹一锅端。"""
    store = _store(tmp_path)
    store.record("a", FLAG_PINNED, True, at=1.0, device="laptop")
    store.record("a", FLAG_PINNED, False, at=2.0, device="laptop")
    first = store.all_changes()[0]
    store.ack([store._key(first)])
    assert len(store.pending()) == 1


def test_ack_ignores_unknown_keys(tmp_path):
    store = _store(tmp_path)
    store.record("a", FLAG_PINNED, True, at=1.0, device="laptop")
    assert store.ack(["不存在|pinned|0.000|"]) == 0
    assert store.ack([""]) == 0


def test_key_is_stable_across_reload(tmp_path):
    """key 不能用列表下标 —— 重排/裁剪后必须仍是同一个。"""
    path = tmp_path / "flag_changes.json"
    store = build_flag_change_store(path)
    store.record("a", FLAG_PINNED, True, at=1.0, device="laptop")
    key = store._key(store.all_changes()[0])
    reloaded = build_flag_change_store(path)
    assert reloaded._key(reloaded.all_changes()[0]) == key


# ---- 持久化与容错 ----


def test_persists_across_reload(tmp_path):
    path = tmp_path / "flag_changes.json"
    store = build_flag_change_store(path)
    store.record("a", FLAG_PINNED, False, at=2.0, device="laptop")
    reloaded = build_flag_change_store(path)
    assert reloaded.effective("a", FLAG_PINNED) is False


def test_corrupt_file_is_reset_not_fatal(tmp_path):
    path = tmp_path / "flag_changes.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    store = build_flag_change_store(path)
    assert store.all_changes() == []
    assert store.record("a", FLAG_PINNED, True, at=1.0) is True


def test_non_dict_payload_is_tolerated(tmp_path):
    path = tmp_path / "flag_changes.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    store = build_flag_change_store(path)
    assert store.all_changes() == []


def test_bad_entries_are_skipped_individually(tmp_path):
    path = tmp_path / "flag_changes.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "changes": [
                    {"hash": "a", "flag": "pinned", "value": True, "at": 1.0},
                    {"hash": "", "flag": "pinned", "at": 2.0},
                    {"hash": "b", "flag": "pinned", "at": "坏"},
                    {"hash": "c", "flag": "favorite", "value": True, "at": 3.0},
                ],
                "acked": {"坏的键": "也不是数字", "x|pinned|1.000|": 5.0},
            }
        ),
        encoding="utf-8",
    )
    store = build_flag_change_store(path)
    assert {change.hash for change in store.all_changes()} == {"a", "c"}
    assert store.status()["pending"] == 2


def test_atomic_write_leaves_no_tmp(tmp_path):
    path = tmp_path / "flag_changes.json"
    store = build_flag_change_store(path)
    store.record("a", FLAG_PINNED, True, at=1.0)
    assert path.exists()
    assert not path.with_suffix(path.suffix + ".tmp").exists()


# ---- 裁剪 ----


def test_trim_per_hash_keeps_newest(tmp_path):
    from linglong_clip.core.flag_changes import MAX_CHANGES_PER_HASH

    store = _store(tmp_path)
    for index in range(MAX_CHANGES_PER_HASH + 10):
        store.record("a", FLAG_PINNED, index % 2 == 0, at=float(index + 1), device="laptop")
    changes = store.changes_for("a")
    assert len(changes) <= MAX_CHANGES_PER_HASH
    # 保留的是最近的（时间戳最大那批）
    assert max(change.at for change in changes) == MAX_CHANGES_PER_HASH + 10


def test_prune_noop_when_no_ttl(tmp_path):
    store = _store(tmp_path)
    store.record("a", FLAG_PINNED, True, at=1.0)
    assert store.prune(older_than=0) == 0
    assert len(store.all_changes()) == 1


def test_prune_only_removes_acked_and_old(tmp_path):
    store = _store(tmp_path)
    store.record("a", FLAG_PINNED, True, at=1.0, device="laptop")
    store.record("b", FLAG_PINNED, True, at=1.0, device="laptop")
    changed = store.all_changes()
    store.ack([store._key(changed[0])])
    now = 1.0 + 10_000
    removed = store.prune(older_than=100, now=now)
    assert removed == 1
    remaining = store.changes_for("b")
    assert len(remaining) == 1


def test_reset_clears_everything(tmp_path):
    store = _store(tmp_path)
    store.record("a", FLAG_PINNED, True, at=1.0)
    assert store.reset() == 1
    assert store.all_changes() == []
    assert store.reset() == 0


# ---- status ----


def test_status_reports_pending(tmp_path):
    store = _store(tmp_path)
    store.record("a", FLAG_PINNED, False, at=1.0, device="laptop")
    status = store.status()
    assert status["count"] == 1
    assert status["pending"] == 1
    row = status["changes"][0]
    assert row["flag"] == FLAG_PINNED
    assert row["value"] is False
    assert row["pending"] is True
    assert row["at_text"]


# ---- 写入失败不抛 ----


def test_write_failure_does_not_raise(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(
        "pathlib.Path.write_text",
        lambda *a, **k: (_ for _ in ()).throw(OSError("磁盘满")),
    )
    # 不该抛异常：取消传播失败不该影响本地取消
    assert store.record("a", FLAG_PINNED, True, at=1.0) is True


# ---- 补齐边界 ----

def test_latest_for_returns_none_when_no_match(tmp_path):
    """没见过该指纹/该设备时返回 None（幂等判断的起点）。"""
    store = _store(tmp_path)
    store.record("a", FLAG_PINNED, True, at=1.0, device="laptop")
    assert store._latest_for("a", FLAG_PINNED, "desktop") is None
    assert store._latest_for("b", FLAG_PINNED, "laptop") is None


def test_global_trim_keeps_newest(tmp_path):
    """整体条数上限也要生效：每个指纹各自很少，但指纹很多时总量仍有界。"""
    from linglong_clip.core import flag_changes as module

    store = _store(tmp_path)
    monkeypatch_cap = module.MAX_CHANGES
    try:
        module.MAX_CHANGES = 10
        for index in range(25):
            store.record(f"hash{index}", FLAG_PINNED, True, at=float(index + 1))
    finally:
        module.MAX_CHANGES = monkeypatch_cap
    # 裁剪后条数不超过当时的上限，且保留的是较新的那些
    assert len(store.all_changes()) <= 10


def test_format_ts_handles_never():
    from linglong_clip.core.flag_changes import _format_ts

    assert _format_ts(0) == "从未"
    assert _format_ts(-1) == "从未"
    assert _format_ts(1_700_000_000).startswith("20")


def test_status_includes_path_even_when_missing(tmp_path):
    store = _store(tmp_path)
    status = store.status()
    assert status["exists"] is False
    assert status["path"].endswith("flag_changes.json")
    assert status["count"] == 0


def test_trim_keeps_under_cap_hashes_untouched(tmp_path):
    """某个指纹没超上限时原样保留，不该被别的指纹的裁剪牵连。"""
    from linglong_clip.core.flag_changes import MAX_CHANGES_PER_HASH

    store = _store(tmp_path)
    # 一个指纹只记两条（远低于上限）
    store.record("small", FLAG_PINNED, True, at=1.0, device="laptop")
    store.record("small", FLAG_PINNED, False, at=2.0, device="laptop")
    # 另一个指纹反复切换，触发该指纹的裁剪
    for index in range(MAX_CHANGES_PER_HASH + 5):
        store.record(
            "big", FLAG_PINNED, index % 2 == 0, at=float(index + 10), device="laptop"
        )
    assert len(store.changes_for("small")) == 2
    assert len(store.changes_for("big")) <= MAX_CHANGES_PER_HASH
