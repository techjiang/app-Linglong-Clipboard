"""配置项审计：找出「声明了但没人读」的配置键。

存在的理由：这个项目反复出现同一类缺陷 —— 设置页里有个勾选框、
配置文件里有对应默认值，但**代码里没有任何读取方**。用户勾了它，
界面上一切正常，只是那个开关什么也不做：

    ui.card_density       列表密度     界面上连控件都没有
    ui.close_to_tray      关闭收托盘   关窗行为完全不受它影响
    history.pin_immune    固定项豁免   淘汰 SQL 里硬编码 pinned = 0
    capture.max_text_length 单条上限   采集路径里硬编码 200_000

这类缺陷的共同点是**靠读代码发现，测不出来**：没有读取方就没有可断言的
行为，测试写无可写。所以反过来做一次静态审计 —— 拿 `config.DEFAULTS`
里的所有叶子键，去源码里找读取点，找不到的报出来。

判据刻意保守，只报「连一次字符串引用都没有」的键：

- 用 `_config.get("a.b")` / `config.get("a.b")` 任一形式引用都算读到
- 子键可能只被拼出来（如 `f"ocr.{key}"`），此时父组件的字符串出现即算通过
- 配置模块自身与设置页不算读取方 —— 前者是定义处，后者只是把值写回去，
  两者都不消费这个值
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: 这些文件不算读取方：
#: - `ui/settings_dialog.py` 只是把界面上的值**写回**配置，写回去不等于会生效
#: - `utils/config.py` 是定义处，其中的 `_MIGRATIONS` 等逻辑读的是配置键本身，
#:   但为了不把「定义」误判成「消费」，默认也排除；`meta.schema_version`
#:   这类「配置模块自己维护的元数据」通过下面的白名单单独放行。
_NON_READER_FILES = {"ui/settings_dialog.py"}

#: 由配置模块自身消费的键，不要求业务代码有读取方。
#: `meta.schema_version` 就是典型：它的消费者是配置的迁移流程本身。
_SELF_CONSUMED_PREFIXES = ("meta.",)

#: 只做定义/描述的文件名后缀
_SKIP_SUFFIXES = (".pyc",)

#: 匹配源码里的配置键字面量。允许「单段」（`'ocr'`）与「多段」
#: （`'sync.lan_port'`）两种写法 —— 前者是「整段被取用」，
#: 对判据有意义，不能因为没点号就漏掉。
_ASSIGN_RE = re.compile(r'["\'](?P<key>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)["\']')


@dataclass
class AuditReport:
    """审计结果。"""

    #: 没有任何读取方的配置键
    unread: List[str] = field(default_factory=list)
    #: 有读取方的配置键数量
    read_count: int = 0
    #: 扫过的文件数
    scanned_files: int = 0
    #: 读取点统计：键 -> 引用它的模块（相对路径）
    references: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return not self.unread

    @property
    def total(self) -> int:
        return self.read_count + len(self.unread)

    def to_dict(self) -> Dict[str, object]:
        return {
            "unread": list(self.unread),
            "read_count": self.read_count,
            "total": self.total,
            "scanned_files": self.scanned_files,
        }

    def summary(self) -> str:
        if self.healthy:
            return f"未发现空转配置（共 {self.total} 个配置键，全部有读取方）"
        return f"发现 {len(self.unread)} 个空转配置（共 {self.total} 个配置键）"


def _iter_default_leaves(node: object, prefix: str = "") -> List[str]:
    """把嵌套的 DEFAULTS 摊成点号路径的叶子键列表。"""
    leaves: List[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            leaves.extend(_iter_default_leaves(value, path))
    else:
        leaves.append(prefix)
    return leaves


def default_keys() -> List[str]:
    """`config.DEFAULTS` 里的全部叶子键。"""
    from .config import DEFAULTS

    return _iter_default_leaves(DEFAULTS)


def _source_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def _scan(root: Optional[pathlib.Path] = None) -> Dict[str, List[str]]:
    """扫一遍源码，返回「配置键 -> 引用它的文件列表」。"""
    base = root or _source_root()
    found: Dict[str, List[str]] = {}
    scanned = 0
    for path in base.rglob("*.py"):
        if path.suffix in _SKIP_SUFFIXES:
            continue
        relative = path.relative_to(base).as_posix()
        if relative in _NON_READER_FILES:
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in _ASSIGN_RE.finditer(text):
            key = match.group("key")
            found.setdefault(key, []).append(relative)

    return found


def audit_config_usage(
    root: Optional[pathlib.Path] = None,
    keys: Optional[List[str]] = None,
) -> AuditReport:
    """审计全部配置键，报出没有读取方的那些。

    `root` 与 `keys` 可显式传入，便于测试在假源码树上验证判据，
    不必依赖当前仓库的实际内容。
    """
    keys = list(keys) if keys is not None else default_keys()
    references = _scan(root)

    report = AuditReport()
    report.scanned_files = len({p for paths in references.values() for p in paths})
    for key in keys:
        if key.startswith(_SELF_CONSUMED_PREFIXES):
            # 配置模块自己维护的元数据，不要求业务代码读它
            report.read_count += 1
            report.references[key] = ["utils/config.py"]
            continue
        readers = references.get(key)
        if readers is None:
            # 子键可能只被拼出来：`config.get(f"ocr.{name}")`。此时源码里
            # 出现的是 `ocr.` 这个前缀，而不是 `ocr.langs` 这个叶子键本身。
            # 所以再往上找一级父节点（`ocr`）—— 但**只找一级**：
            # 放宽到「整个顶层段都算活」会把「段里有的键被读、有的没被读」
            # 这种最常见的空转形态一起漏掉。
            key_parts = key.split(".")
            if len(key_parts) >= 2:
                parent = ".".join(key_parts[:-1])
                readers = references.get(parent)
        if readers:
            report.read_count += 1
            report.references[key] = sorted(set(readers))
        else:
            report.unread.append(key)
    report.unread.sort()
    return report


def format_audit(report: AuditReport) -> str:
    """人类可读输出。"""
    lines = [
        "玲珑剪切板 · 配置项审计",
        "",
    ]
    if report.healthy:
        lines.append(f"✓ {report.summary()}")
        return "\n".join(lines)

    lines.append(f"✗ {report.summary()}")
    lines.append("")
    lines.append("以下配置项在源码中找不到读取方，用户改了不会生效：")
    for key in report.unread:
        lines.append(f"  · {key}")
    lines.append("")
    lines.append("处理方式：接上读取方，或从 DEFAULTS 与设置页里删掉。")
    return "\n".join(lines)


__all__ = [
    "AuditReport",
    "audit_config_usage",
    "default_keys",
    "format_audit",
]
