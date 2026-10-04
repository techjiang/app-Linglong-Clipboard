"""历史文件的真实占用：一个 SQLite 库从来不只是「那个 .db 文件」。

这个模块存在的理由是**用户看不到的那部分磁盘**：

    --stats           → 数据库大小：2652.0 KB    （只 stat 了主文件）
    磁盘上真实的       → 6999.9 KB                （主库 + WAL + shm）

同一个数字在两条路径上差一倍，而用户正是拿它决定要不要清理历史。
更麻烦的是 `--compact`：它跑完 `VACUUM` 之后，随后打开的连接会把一份新的
WAL 写回去 —— 终端说「已压缩」，占用却几乎没变，用户只能得出「这功能是假的」。

所以这里分三层：

1. **枚举与归因**（纯函数）：哪些文件算这个库的一部分、字节怎么归。
2. **与真实磁盘对账**：测出来的数必须等于 `du` 得到的数 —— 这是唯一
   不会自说自话的验法。
3. **`optimize()` 真的收回了空间**：断言的是「测出来的占用变小」，
   而不是「调用过 VACUUM」。
"""

import os
from pathlib import Path

import pytest

from linglong_clip.core import storage_files
from linglong_clip.core.models import ClipboardItem


def _disk_total(main: Path) -> int:
    """模拟 `du`：把这个库在磁盘上真实占用的所有文件加起来。"""
    return sum(
        os.stat(p).st_size for p in Path(str(main)).parent.glob(Path(str(main)).name + "*")
        if p.is_file()
    )


# ---- 枚举 ----


def test_main_file_absent_means_not_exists(tmp_path):
    footprint = storage_files.measure(tmp_path / "history.db")
    assert footprint.exists is False
    assert footprint.total_bytes == 0
    assert footprint.files == []


def test_companion_files_are_counted(tmp_path):
    main = tmp_path / "history.db"
    main.write_bytes(b"x" * 1000)
    (tmp_path / "history.db-wal").write_bytes(b"y" * 200)
    (tmp_path / "history.db-shm").write_bytes(b"z" * 50)

    footprint = storage_files.measure(main)
    assert footprint.main_bytes == 1000
    assert footprint.companion_bytes == 250
    assert footprint.total_bytes == 1250
    assert [entry.name for entry in footprint.files] == [
        "history.db",
        "history.db-wal",
        "history.db-shm",
    ]


def test_shm_is_not_counted_as_pending_content(tmp_path):
    """`-shm` 是索引，不是「还没并进主库的改动」。

    这个缺陷的形状：`-shm` 在 Linux 上**恒为 32 KB**（SQLite 的
    `SQLITE_SHM_*` 常量定死），与库里有多少数据无关。此前它被并进
    `companion_bytes`，而三处调用方都拿那个数当「待合并」报出来 ——
    于是 WAL 明明是 0 字节，屏幕上却写着「待合并 32.0 KB ·
    尚未并入主库的改动（下次启动会自动合并）」。

    没有东西会合并，也没有东西需要合并。用户只能得出
    「我的历史是不是一直没落盘」。
    """
    main = tmp_path / "history.db"
    main.write_bytes(b"x" * 4096)
    # WAL 空着（全部改动已并入主库），shm 是索引
    (tmp_path / "history.db-wal").write_bytes(b"")
    (tmp_path / "history.db-shm").write_bytes(b"z" * 32768)

    footprint = storage_files.measure(main)
    assert footprint.content_bytes == 0, "WAL 空着，不该有任何待合并的内容"
    assert footprint.index_bytes == 32768
    # 但总占用仍要如实算上索引 —— `du` 会看到这些字节
    assert footprint.companion_bytes == 32768
    assert footprint.total_bytes == 4096 + 32768


def test_wal_content_is_counted_as_pending(tmp_path):
    """WAL 里有真东西时才是「待合并」，且只算 WAL 那部分。"""
    main = tmp_path / "history.db"
    main.write_bytes(b"x" * 4096)
    (tmp_path / "history.db-wal").write_bytes(b"y" * 5000)
    (tmp_path / "history.db-shm").write_bytes(b"z" * 32768)

    footprint = storage_files.measure(main)
    assert footprint.content_bytes == 5000
    assert footprint.index_bytes == 32768


def test_journal_counts_as_pending(tmp_path):
    """非 WAL 模式下的回滚日志同样是「装内容的」伴随文件。"""
    main = tmp_path / "history.db"
    main.write_bytes(b"x" * 100)
    (tmp_path / "history.db-journal").write_bytes(b"j" * 777)

    footprint = storage_files.measure(main)
    assert footprint.content_bytes == 777
    assert footprint.index_bytes == 0


def test_companion_kind_matches_exact_suffixes():
    """归类按后缀精确匹配，用户自己的同名备份文件不算任何一种。"""
    assert storage_files.companion_kind("history.db-wal") == "content"
    assert storage_files.companion_kind("history.db-journal") == "content"
    assert storage_files.companion_kind("history.db-shm") == "index"
    assert storage_files.companion_kind("history.db-wal-old") == "other"
    assert storage_files.companion_kind("history.db-bak") == "other"


def test_file_entry_reports_its_kind(tmp_path):
    """每个文件条目自己说得清是「主 / 内容 / 索引」。"""
    main = tmp_path / "history.db"
    main.write_bytes(b"x" * 10)
    (tmp_path / "history.db-wal").write_bytes(b"y" * 10)
    (tmp_path / "history.db-shm").write_bytes(b"z" * 10)

    kinds = {entry.name: entry.kind for entry in storage_files.measure(main).files}
    assert kinds == {
        "history.db": "main",
        "history.db-wal": "content",
        "history.db-shm": "index",
    }


def test_only_owned_companions_are_included(tmp_path):
    """主库名字只是前缀，不能把用户自己的同类文件算进来。

    `history.db-wal-old`、`history.db-bak` 这类名字很自然会出现在
    用户目录里（备份、排查时 `cp` 出来的）。它们不属于这个库，
    算进来会让占用虚高，而虚高的数字会让人做错决定。
    """
    main = tmp_path / "history.db"
    main.write_bytes(b"x" * 100)
    (tmp_path / "history.db-wal-old").write_bytes(b"y" * 10000)
    (tmp_path / "history.db-bak").write_bytes(b"z" * 10000)
    (tmp_path / "history.db-wal").write_bytes(b"w" * 7)

    footprint = storage_files.measure(main)
    assert footprint.companion_bytes == 7
    assert footprint.total_bytes == 107


def test_missing_wal_is_not_invented(tmp_path):
    """WAL 不在时说它不在，而不是补一个 0 字节的条目。

    「WAL 已经合并完了」和「WAL 存在但是空的」在界面上要能分开 ——
    前者不该出现在文件清单里。
    """
    main = tmp_path / "history.db"
    main.write_bytes(b"x" * 10)
    names = [entry.name for entry in storage_files.measure(main).files]
    assert names == ["history.db"]


def test_companion_paths_are_derived_by_suffix(tmp_path):
    main = tmp_path / "history.db"
    assert [p.name for p in storage_files.companion_paths(main)] == [
        "history.db-wal",
        "history.db-journal",
        "history.db-shm",
    ]


def test_unreadable_file_is_counted_as_zero(tmp_path, monkeypatch):
    """取不到大小按 0 算，不抛异常。

    用户跑 `--stats` 往往正是因为磁盘出状况 —— 那正是最需要它还能出话的时候。
    """
    main = tmp_path / "history.db"
    main.write_bytes(b"x" * 10)

    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        if str(path).endswith("-wal"):
            raise OSError("权限不足")
        return real_stat(path, *args, **kwargs)

    (tmp_path / "history.db-wal").write_bytes(b"y" * 10)
    monkeypatch.setattr(storage_files.os, "stat", fake_stat)
    footprint = storage_files.measure(main)
    assert footprint.total_bytes == 10
    assert footprint.exists is True


# ---- 字节格式化 ----


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KB"),
        (6996192, "6.7 MB"),
        (5 * 1024 * 1024, "5.0 MB"),
    ],
)
def test_format_bytes(value, expected):
    assert storage_files.format_bytes(value) == expected


def test_format_bytes_never_goes_negative():
    assert storage_files.format_bytes(-100) == "0 B"


# ---- 与真实磁盘对账 ----


def test_measured_size_matches_disk(home):
    """测出来的占用必须等于磁盘上真实占用的总和。

    这是本文件最重要的一条：其余断言都可以自说自话，
    只有它把「我们报的数」和「用户 `du` 看到的数」钉在一起。
    """
    from linglong_clip.core.storage import Storage

    store = Storage(home / "history.db")
    for index in range(50):
        store.add(
            ClipboardItem(kind="text", content=f"内容 {index} " + "x" * 2000, preview=f"{index}")
        )

    footprint = storage_files.measure(home / "history.db")
    assert footprint.total_bytes == _disk_total(home / "history.db")
    # WAL 模式下一个还在写入的库必然有 WAL —— 只 stat 主文件会少报
    assert footprint.companion_bytes > 0
    assert footprint.total_bytes > footprint.main_bytes
    store.close()


def test_stats_reports_total_not_main_file(home):
    """`stats()['db_size']` 报的是总占用，不是主文件大小。"""
    from linglong_clip.core.storage import Storage

    store = Storage(home / "history.db")
    for index in range(50):
        store.add(
            ClipboardItem(kind="text", content=f"内容 {index} " + "x" * 2000, preview=f"{index}")
        )
    stats = store.stats()
    main_size = (home / "history.db").stat().st_size
    assert stats["db_size"] == _disk_total(home / "history.db")
    assert stats["db_size"] > main_size
    assert stats["storage_files"]["bytes"] == stats["db_size"]
    assert stats["storage_files"]["main_bytes"] == main_size
    store.close()


def test_stats_survives_a_missing_database(tmp_path):
    """库还没建时也要出得来，并标成「尚未创建」。"""
    from linglong_clip.core.storage import Storage

    store = Storage(tmp_path / "history.db")
    store.close()
    (tmp_path / "history.db").unlink()
    stats = store.stats()
    assert stats["storage_files"]["exists"] is False
    assert stats["db_size"] == 0


# ---- 可回收量 ----


def test_reclaimable_bytes_reflects_deleted_rows(home):
    """删掉大量记录之后，可回收量应随之变大。

    这个数就是「跑一次 `--compact` 大致能收回多少」——
    `--compact` 的输出此前只有「数据库已压缩」一句，用户无从判断值不值得跑。
    """
    from linglong_clip.core.storage import Storage

    store = Storage(home / "history.db")
    for index in range(200):
        store.add(
            ClipboardItem(kind="text", content=f"内容 {index} " + "x" * 3000, preview=f"{index}")
        )
    before = storage_files.reclaimable_bytes(store._conn)
    ids = [item.id for item in store.list(limit=1000)]
    store.delete_many(ids[:150])
    after = storage_files.reclaimable_bytes(store._conn)
    assert after > before
    assert store.stats()["storage_files"]["reclaimable_bytes"] == after
    store.close()


def test_reclaimable_bytes_on_a_broken_connection_is_zero():
    """量不出来返回 0，不抛异常。"""

    class Broken:
        def execute(self, _sql):
            raise RuntimeError("连接已关闭")

    assert storage_files.reclaimable_bytes(Broken()) == 0


def test_json_backend_reports_same_shape(home, monkeypatch):
    """两个后端的 `storage_files` 形状要一致。

    调用方（CLI / 设置页）不该区分「这个后端有没有伴随文件」——
    形状分叉的症状正是这个项目一直在防的那类：换个存储方式，报的数就不一样。
    """
    from linglong_clip.core.repository import JsonRepository

    monkeypatch.setenv("LINGLONG_CLIPBOARD_JSON_HOME", str(home / "json"))
    store = JsonRepository(home / "json" / "clipboard.json")
    store.add(ClipboardItem(kind="text", content="hello", preview="hello"))
    sqlite_stats = {"storage_files"}
    from linglong_clip.core.storage import Storage

    sq = Storage(home / "sq.db")
    assert set(store.stats()["storage_files"]) == set(sq.stats()["storage_files"])
    assert set(sqlite_stats) <= set(store.stats())
    store.close()
    sq.close()


# ---- optimize 真的收回了空间 ----


def test_optimize_shrinks_measured_footprint(home):
    """压缩之后，**测出来的占用**必须变小。

    断言的是结果而不是「调用过 VACUUM」：先 checkpoint 再 VACUUM 的写法
    同样会执行这两条语句，但 `VACUUM` 自己产生的改动会立刻写出一份新 WAL，
    占用几乎没变 —— 用户看到的是「说压缩了，磁盘一点没动」。
    """
    from linglong_clip.core.storage import Storage

    store = Storage(home / "history.db")
    for index in range(200):
        store.add(
            ClipboardItem(kind="text", content=f"内容 {index} " + "x" * 3000, preview=f"{index}")
        )
    ids = [item.id for item in store.list(limit=1000)]
    store.delete_many(ids[:150])

    before = storage_files.measure(home / "history.db").total_bytes
    store.optimize()
    after = storage_files.measure(home / "history.db").total_bytes
    assert after < before
    # WAL 必须被清干净 —— 留下未合并的改动就是「压缩看不出来」的成因
    assert storage_files.measure(home / "history.db").companion_bytes < 64 * 1024
    store.close()


def test_optimize_keeps_the_connection_usable(home):
    """压缩之后连接**仍然能用**。

    顺手 `close()` 一下看起来更彻底（WAL 会被删掉），但 `_closed` 一旦置上，
    后面找上来的调用方拿到的是「空历史」—— 收尾、界面定时器都会。
    所以这里断言「压缩后还能正常查询」。
    """
    from linglong_clip.core.storage import Storage

    store = Storage(home / "history.db")
    store.add(ClipboardItem(kind="text", content="保留我", preview="保留我"))
    store.optimize()
    assert store.count() == 1
    assert store.latest().content == "保留我"
    store.close()


def test_optimize_honours_the_closed_flag(home):
    """关掉之后再调 `optimize()` 不抛异常。"""
    from linglong_clip.core.storage import Storage

    store = Storage(home / "history.db")
    store.close()
    store.optimize()  # 不抛即通过
