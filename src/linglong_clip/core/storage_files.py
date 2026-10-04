"""历史文件到底占了磁盘多少 —— 一个 SQLite 库从来不只是「那个 .db 文件」。

为什么需要它：

`storage.stats()['db_size']` 一直只 `stat()` 主文件，而 SQLite 在 WAL 模式
（本项目的默认）下有**三个**文件才是完整的一份历史：

```
history.db        主库
history.db-wal    自上次 checkpoint 以来的全部改动 —— 内容就在这里
history.db-shm    共享内存索引，进程没退出时存在
```

漏掉 WAL 的后果不是少报几个百分点，而是**同一个数字在两条路径上差一倍**：

```
--stats            → 数据库大小：2652.0 KB     （只 stat 了主文件）
--storage-status   → 文件大小：2652.0 KB
磁盘上真实的        → 6999.9 KB（主库 + WAL + shm）
```

用户据此判断「该不该清理历史」，而两个命令都给了同一个偏小的答案。
更麻烦的是 `--compact`：它跑完 `VACUUM` 之后，随后打开的连接（任何一种
查询都会打开）会把一份新的 WAL 写回去 —— 于是终端打印「数据库已压缩」，
占用却几乎没变。用户只能得出「这功能是假的」。

但「把它们加起来」只解决了一半。另一半是**别把索引当成内容**：

```
history.db-wal    0 字节        ← 没有任何改动没进主库
history.db-shm   32768 字节     ← 索引，一个字节的内容都不装
```

WAL 空着、`-shm` 也不小的时候，此前会输出「待合并 32.0 KB · 尚未并入主库
的改动（下次启动会自动合并）」—— 每一个字都是错的：没有待合并的东西，
也没有任何事情会发生。而 `-shm` 在 Linux 上**恒为 32 KB**（SQLite 的
`SQLITE_SHM_*` 常量定死，与库里有多少数据无关），所以这句话会**永远**挂着，
用户会一直以为自己的历史没写完盘。

`-shm` 之所以不能当内容：它是 WAL 的共享内存索引，回答的是「哪些页在 WAL 里、
在什么位置」，本身不存数据。它与内容量的关系是零 —— 空库开着连接也是 32 KB。

这个模块只做两件事，都是纯计算，不碰任何数据库连接：

- `database_files(path)` —— 枚举 SQLite 库的全部伴随文件（存在即算）。
- `measure(...)` —— 按「主库 / 装内容的伴随 / 只有索引的伴随」三层归因字节数。

三层对应三种不同的用户动作，所以不能合并成一个数：

| 归因 | 是什么 | 用户该做什么 |
| --- | --- | --- |
| `main_bytes` | 主库 | 里面有空洞（`reclaimable`）才需要 `VACUUM` |
| `content_bytes` | `-wal` / `-journal` 里没说进主库的内容 | 等自动合并，或跑 `--compact` |
| `index_bytes` | `-shm`（纯索引） | **什么都不用做** |

总占用仍是三层之和（`total_bytes`），因为用户 `du` 看到的正是这个数。

伴随文件的命名规则由 SQLite 定死：主库后面直接拼 `-wal` / `-journal` / `-shm`。
与同目录下的 `history.db-bak`、`history.db-wal-old` 这类**用户自己的文件**
不会有歧义 —— 一个文件要么正是 `history.db-wal`，要么不是。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

#: SQLite 会自己创建、并算作「这个库的一部分」的后缀。
#:
#: - `-wal`：预写日志（`journal_mode=WAL`，本项目默认）。**它装着内容**，
#:   不是可选缓存 —— 少算它等于少报一大部分历史。
#: - `-journal`：回滚日志（非 WAL 模式的默认，以及个别 DDL 会临时用到）。
#: - `-shm`：WAL 的共享内存索引，最后一个连接关闭时消失。
COMPANION_SUFFIXES: Tuple[str, ...] = ("-wal", "-journal", "-shm")

#: 伴随文件里**装着内容**的那几个：它们的大小就是「还没并进主库的改动量」。
#:
#: `-wal` 是预写日志，自上次 checkpoint 以来的全部改动都在里面；
#: `-journal` 是回滚日志，只在非 WAL 模式或个别 DDL 期间存在。
#: 二者清零（或消失）就意味着「已经全部并进主库」。
CONTENT_SUFFIXES: Tuple[str, ...] = ("-wal", "-journal")

#: 伴随文件里**只有索引、没有内容**的那个。
#:
#: `-shm` 是 WAL 的共享内存索引：它记录「哪些页在 WAL 里、在什么位置」，
#: 本身**一个字节的内容都不装**。把它算成「待合并的改动」是本模块此前的
#: 一处误报 —— 见下面 `StorageFootprint.content_bytes` 的说明。
#:
#: 它还有一个容易骗人的性质：Linux 上它**恒为 32 KB**（一页 4 KB 的哈希
#: 表 + 索引区，由 SQLite 的 `SQLITE_SHM_*` 常量定死），与库里有多少数据
#: 无关。空库开着连接也是 32 KB。所以它一旦被当成「待合并」，
#: 用户会**永远**看到一句「有 32.0 KB 尚未并入主库」，而实际上一步都不用做。
INDEX_SUFFIXES: Tuple[str, ...] = ("-shm",)

#: 新格式（3.7.0+）WAL 的头部大小。用不到、只是别处可能想判类型。
WAL_HEADER_SIZE = 32


def companion_kind(name: str) -> str:
    """这个伴随文件名是「装内容的」还是「只有索引的」。

    返回 ``"content"`` / ``"index"`` / ``"other"``。判定按**后缀精确匹配**：
    ``history.db-wal-old`` 是用户自己的文件，不能算进任何一个。
    """
    for suffix in CONTENT_SUFFIXES:
        if name.endswith(suffix):
            return "content"
    for suffix in INDEX_SUFFIXES:
        if name.endswith(suffix):
            return "index"
    return "other"


@dataclass
class FileEntry:
    """一个文件在磁盘上的真实占用。"""

    path: Path
    size: int
    #: 是主文件还是伴随文件
    companion: bool

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def kind(self) -> str:
        """`main` / `content` / `index` / `other`。

        主文件之外的判定交给 `companion_kind()`。别处要用它的地方
        都不要自己 `endswith` —— 「哪些后缀装内容」只有一处定义。
        """
        if not self.companion:
            return "main"
        return companion_kind(self.name)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "size": self.size,
            "companion": self.companion,
            "kind": self.kind,
        }


@dataclass
class StorageFootprint:
    """一整份历史在磁盘上的占用，按文件归因。

    占用分三层，因为它们对应三种**不同的用户动作**（或无需动作）：

    | 归因 | 是什么 | 用户该做什么 |
    | --- | --- | --- |
    | `main_bytes` | 主库 | 里面有空洞才需要 `VACUUM` |
    | `content_bytes` | WAL / journal 里**没说进主库的内容** | 等它自动合并，或跑 `--compact` |
    | `index_bytes` | `-shm`（WAL 的共享内存索引，不装内容） | **什么都不用做** |

    此前只有「主库 + 伴随」两层，`-shm` 被并进「待合并」，于是
    「有没有待合并的改动」这个判断从根上就错了 —— 详见
    `StorageFootprint.content_bytes`。
    """

    main_bytes: int = 0
    #: 装内容的伴随文件（`-wal` / `-journal`）。**它非零才代表真有改动没进主库。**
    content_bytes: int = 0
    #: 只有索引的伴随文件（`-shm`）。恒有、且与数据量无关，**不代表任何待办**。
    index_bytes: int = 0
    files: List[FileEntry] = field(default_factory=list)
    #: 库不存在时为空集 —— 调用方据此说「尚未创建」，而不是说「占用 0 字节」
    exists: bool = False

    @property
    def companion_bytes(self) -> int:
        """全部伴随文件的总大小（`-wal` + `-journal` + `-shm`）。

        只用来算**总占用**（用户 `du` 会看到这些字节，少算就不对账）。
        **不要**拿它回答「有多少改动没并进主库」—— 那个问题的答案是
        `content_bytes`，因为它不含恒为 32 KB 的 `-shm`。
        """
        return self.content_bytes + self.index_bytes

    @property
    def total_bytes(self) -> int:
        """真实占用。这才是用户 `du` 得到的数字。"""
        return self.main_bytes + self.companion_bytes

    #: `as_dict()` 的字段集合。两个后端必须给出**同一组键** ——
    #: 形状分叉的症状正是这个项目一直在防的那类：换个存储方式，
    #: 调用方（CLI / 设置页）就得多判一次分支，迟早有一边漏掉。
    #: `tests/test_storage_files.py` 里对两种后端各断言一次。
    DICT_KEYS = (
        "bytes",
        "main_bytes",
        "companion_bytes",
        "content_bytes",
        "index_bytes",
        "reclaimable_bytes",
        "exists",
        "files",
    )

    def as_dict(self, *, reclaimable: int = 0) -> dict:
        """整理成给 CLI / 设置页的形状。

        `reclaimable` 只有 SQLite 量得出来（`freelist_count × page_size`），
        JSON 单文件没有「空洞」的概念。但**键必须存在且为 0**：
        「这个后端没有这个概念」与「有这个概念、现在是 0」对调用方来说
        不该是两件事 —— 前者会让它不得不写一个后端分支。
        """
        return {
            "bytes": self.total_bytes,
            "main_bytes": self.main_bytes,
            "companion_bytes": self.companion_bytes,
            "content_bytes": self.content_bytes,
            "index_bytes": self.index_bytes,
            "reclaimable_bytes": max(0, int(reclaimable)),
            "exists": self.exists,
            "files": [entry.as_dict() for entry in self.files],
        }


def companion_paths(main: Path) -> List[Path]:
    """主库可能带有的伴随文件路径（不判断是否存在）。"""
    return [Path(str(main) + suffix) for suffix in COMPANION_SUFFIXES]


def database_files(main: Path) -> List[FileEntry]:
    """枚举这个库当前真实占用的全部文件，主文件在前。

    不存在的文件不出现 —— 「WAL 不见了吗」这个问题由调用方对比
    `companion_paths()` 来判断，这里只报事实。
    """
    main = Path(main)
    entries: List[FileEntry] = []
    size = _size_or_none(main)
    if size is not None:
        entries.append(FileEntry(path=main, size=size, companion=False))
    for path in companion_paths(main):
        size = _size_or_none(path)
        if size is None:
            continue
        entries.append(FileEntry(path=path, size=size, companion=True))
    return entries


def measure(main: Optional[Path]) -> StorageFootprint:
    """测量一份历史的真实占用。``main`` 为 None 时返回空结果。"""
    footprint = StorageFootprint()
    if main is None:
        return footprint
    entries = database_files(Path(main))
    footprint.files = entries
    footprint.exists = any(not entry.companion for entry in entries)
    for entry in entries:
        if not entry.companion:
            footprint.main_bytes += entry.size
        elif entry.kind == "content":
            footprint.content_bytes += entry.size
        else:
            # `-shm` 以及任何我们没归类的伴随文件都算索引。
            # 宁可少报「待合并」也不能多报 —— 多报会让用户去做一件
            # 根本不存在的事（「我的改动怎么还没合并」）。
            footprint.index_bytes += entry.size
    return footprint


def format_bytes(value: int) -> str:
    """人类可读的字节数。

    刻意与别处的写法保持一致（`--images` 用的是 MB 一位小数），
    但这里要同时面对「几百字节的 WAL」和「几十 MB 的库」，
    所以按量级换单位，而不是一律折成 MB 后出现 `0.0 MB`。
    """
    size = float(max(0, int(value)))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _size_or_none(path: Path) -> Optional[int]:
    """文件大小；不存在或取不到时返回 None。

    用 `os.stat` 而不是 `Path.exists()` + `Path.stat()`：后者是两个系统调用，
    中间那个文件可能正好被删掉（WAL 在连接关闭时会被 SQLite 自己删掉），
    于是 `exists()` 为真、`stat()` 却抛 `FileNotFoundError`。

    遇到取不到的文件一律**当成不存在**，而不是记一笔 0 字节：
    一个 0 字节的 `-wal` 会被下游当成「WAL 存在且已清空」，
    而事实是「我们不知道它在不在」。

    统计失败不该让 `--stats` 整个命令挂掉 —— 用户跑它往往正是因为
    磁盘出状况，那正是最需要它还能出话的时候。
    """
    try:
        return int(os.stat(path).st_size)
    except OSError:
        return None


def reclaimable_bytes(connection) -> int:
    """主库里「删过记录但空间还没还给系统」的那部分。

    算法是 SQLite 官方文档给出的那套：`page_size × freelist_count`。
    `freelist_count` 是已经被清理、但页面仍被库文件占着的页数 ——
    它们不会自动还回去，只有 `VACUUM` 会重建整个文件。

    为什么要单独量它：`--compact` 的输出此前只有「数据库已压缩」这一句，
    用户没有任何办法判断「值不值得跑」。而这个数正是答案 ——
    它是一次 `VACUUM` 大致能收回的量。

    量不出来（连接不可用、库损坏、非 SQLite）时返回 0，
    而不是抛异常：拿不到数不该让 `--stats` 整个命令挂掉。
    """
    try:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        free_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
    except Exception:  # noqa: BLE001 - 统计不该拖垮调用方
        return 0
    if page_size <= 0 or free_pages <= 0:
        return 0
    return page_size * free_pages
