"""图片附件的存储与回收。

`images/` 目录此前**只有入口没有出口**：采集时把 PNG 落盘、记录里存相对路径，
但记录消失之后那份文件没有任何人来收。三条路径都会留下永久垃圾：

1. **淘汰**（`history.max_items` / `max_days`）与**敏感过期**
   （`privacy.sensitive_retention_seconds`）直接 `DELETE FROM items`，
   不像 `delete()` / `clear()` 那样顺手删图 —— 后者是记得删的。
2. **JSON 后端的 `delete()` / `clear()`** 连顺手删也没有，
   于是「换个存储后端，磁盘表现就不一样」。
3. **采集期就被拦下的截图**：图片在读取阶段（`_from_image`）已经落盘，
   而隐私判定发生在之后的 `Collector.process()` 里。判定不通过时记录不存在，
   文件却已经躺在那儿 —— 它的内容可能正是那条被丢弃的密钥截图。

三者的共同点是**用户看不见**：没有报错、界面照常，只有磁盘在涨。
`--stats` 也从来没报过图片占用，所以连「涨了多少」都无从知道。

本模块把这件事收在一处，两条清理路径都用它：

- `collect_referenced()` —— 从记录里收集「还在用的」相对路径。
  这是**唯一**的判据来源，两个后端各自实现，避免各写一份迟早分叉。
- `sweep()` —— 删掉 `images/` 里不再被引用的文件。默认只报告不删
  （`dry_run=True`），因为「哪些文件算垃圾」要靠调用方给出的引用集，
  给错了会把用户的图删掉。
- `usage()` —— 图片目录的体积与文件数，供 `--stats` 与设置页显示。

一条刻意的约定：**只删「看得懂的普通文件」**。子目录、符号链接、非 PNG 的
东西一律不动 —— 用户可能把同步盘指向这里，误删的代价远大于留几 MB。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Set

from ..utils.logger import get_logger

_log = get_logger("images")

#: 只回收我们自己写下的文件。`_from_image()` 用内容哈希命名，扩展名固定。
IMAGE_GLOB = "*.png"

#: 单次回收的默认上限，防止首次运行时一口气删掉几十万文件把界面卡住
DEFAULT_LIMIT = 2000


@dataclass
class ImageUsage:
    """图片目录的占用情况。"""

    files: int = 0
    bytes: int = 0
    missing: int = 0
    """记录引用了、但文件已不在的数量。属于数据损坏，不是垃圾。"""

    def as_dict(self) -> dict:
        return {"files": self.files, "bytes": self.bytes, "missing": self.missing}


@dataclass
class SweepResult:
    """一次回收的结果。"""

    removed: int = 0
    bytes: int = 0
    kept: int = 0
    errors: int = 0
    names: list = field(default_factory=list)
    """被删掉的文件名，供 `--dry-run` 与日志展示（只留文件名，不含目录）。"""

    dry_run: bool = False
    truncated: bool = False
    """是否因为达到上限而提前停下 —— 停下不代表扫完了。"""

    def as_dict(self) -> dict:
        return {
            "removed": self.removed,
            "bytes": self.bytes,
            "kept": self.kept,
            "errors": self.errors,
            "dry_run": self.dry_run,
            "truncated": self.truncated,
        }


def normalize_relative(relative: str) -> str:
    """把记录里的 `image_path` 归一成可比较的相对路径。

    库里存的形式不止一种：`images/abc.png`（常规）、`abc.png`（旧版本）、
    绝对路径（手改过的 JSON）、以及 data URI（JSON 后端内联的图）。
    判断「这个文件还有没有人用」必须先把这几种收敛成同一个写法，
    否则会把还在用的图当成孤儿删掉。
    """
    value = str(relative or "").strip().replace("\\", "/")
    if not value or value.startswith("data:"):
        return ""
    if value.startswith("./"):
        value = value[2:]
    return value


def basename_of(relative: str) -> str:
    """取文件名。引用集按文件名比对，因为库里的路径前缀并不统一。"""
    normalized = normalize_relative(relative)
    return normalized.rsplit("/", 1)[-1] if normalized else ""


def referenced_names(paths: Iterable[str]) -> Set[str]:
    """把一串 `image_path` 收敛成「还在用的文件名」集合。"""
    names: Set[str] = set()
    for path in paths:
        name = basename_of(path)
        if name:
            names.add(name)
    return names


def iter_image_files(images_dir: Path, *, pattern: str = IMAGE_GLOB) -> Iterator[Path]:
    """逐个产出可回收的图片文件。

    只产出**目录下的普通文件**且扩展名匹配。子目录不递归：`images/` 是我们
    自己建的平铺目录，递归进去就有机会碰到用户放在那儿的东西。
    """
    if not images_dir.is_dir():
        return
    try:
        entries = sorted(images_dir.iterdir())
    except OSError as exc:
        _log.debug("图片目录读取失败 %s: %s", images_dir, exc)
        return
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
        except OSError:
            continue
        if fnmatch.fnmatch(entry.name, pattern):
            yield entry


def sweep(
    images_dir: Path,
    referenced: Set[str],
    *,
    dry_run: bool = True,
    limit: int = DEFAULT_LIMIT,
    pattern: str = IMAGE_GLOB,
) -> SweepResult:
    """删掉 `images/` 里没有被 `referenced` 提到的文件。

    `referenced` 是**文件名**集合（见 `referenced_names()`），不是路径 ——
    库里历史遗留了 `images/x.png` 与 `x.png` 两种写法，按文件名比对才不会误删。

    `dry_run=True` 时什么都不删，只把「会删哪些」报出来。默认就是它：
    这个函数的破坏性来自调用方给的引用集，给错一次就是用户丢图。
    """
    result = SweepResult(dry_run=bool(dry_run))
    for path in iter_image_files(images_dir, pattern=pattern):
        if path.name in referenced:
            result.kept += 1
            continue
        if result.removed + result.errors >= limit:
            result.truncated = True
            break
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if not dry_run:
            try:
                path.unlink()
            except OSError as exc:
                # 删不掉不该让整个回收失败：下一轮还会再试一次。
                _log.debug("图片删除失败 %s: %s", path, exc)
                result.errors += 1
                continue
        result.removed += 1
        result.bytes += size
        if len(result.names) < 50:
            result.names.append(path.name)
    return result


def usage(
    images_dir: Path,
    referenced: Set[str],
    *,
    pattern: str = IMAGE_GLOB,
) -> ImageUsage:
    """统计图片目录的占用，并数出「记录引用了但文件不在」的数量。

    后者值得单独报：它是数据损坏（图片被手工删了、同步盘冲突丢了），
    与「垃圾没清」是两回事，处理方式也不同 —— 一个要清、一个要找回来。
    """
    stat = ImageUsage()
    present: Set[str] = set()
    for path in iter_image_files(images_dir, pattern=pattern):
        present.add(path.name)
        try:
            stat.files += 1
            stat.bytes += path.stat().st_size
        except OSError:
            continue
    stat.missing = len(referenced - present)
    return stat
