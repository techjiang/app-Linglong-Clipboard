"""系统剪贴板历史导入。

存在的理由：本工具只能看到**自己启动之后**复制的内容。装好之前、
或者被退出期间复制的东西，历史里就是缺的。Windows 10 1809+ 的系统
剪贴板历史正好补上这段空缺。

一条硬约束：导入的每一条都必须走 `Collector.process()`，与实时采集
完全同一条管线。不这么做就会绕开隐私判定 —— 从系统历史里捞回来的
密码、私钥会直接落库，而实时复制的同一段内容却会被拦掉。
「同一个东西换个入口就放行了」是最不该出现的漏洞。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from ..platform.base import CaptureResult
from ..utils.logger import get_logger
from . import classify
from .collector import (
    CAPTURED,
    MERGED,
    SKIPPED_PRIVACY,
    Collector,
)
from .models import ClipboardItem

_log = get_logger("native_history")


@dataclass
class ImportReport:
    """一次历史导入的结果，供 CLI / 界面展示。"""

    scanned: int = 0
    captured: int = 0
    merged: int = 0
    skipped_privacy: int = 0
    skipped_other: int = 0
    unsupported_reason: str = ""
    blocked_samples: List[str] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return not self.unsupported_reason

    @property
    def imported(self) -> int:
        """新入库的条数（合并的算「已存在」，不计入）。"""
        return self.captured

    def summary(self) -> str:
        if not self.supported:
            return self.unsupported_reason
        parts = [f"新导入 {self.captured} 条"]
        if self.merged:
            parts.append(f"已存在 {self.merged} 条")
        if self.skipped_privacy:
            parts.append(f"隐私拦截 {self.skipped_privacy} 条")
        if self.skipped_other:
            parts.append(f"跳过 {self.skipped_other} 条")
        return f"共扫描 {self.scanned} 条，" + "，".join(parts)


def _to_item(result: CaptureResult) -> ClipboardItem:
    """把平台层的读取结果转成记录。

    类型判定交给 `classify.analyze()`，与实时采集同一套规则 ——
    历史里的一条 URL 也该被识别成链接，而不是一律当纯文本。
    """
    analysis = classify.analyze(result.text)
    item = ClipboardItem(
        kind=str(analysis.get("kind") or "text"),
        content=result.text,
        html=result.html,
        rtf=result.rtf,
    )
    item.metadata["imported_from"] = "system_history"
    if not result.source.is_empty:
        item.source_app = result.source.app_id or result.source.app_name
        item.source_title = result.source.window_title
    return item


def import_native_history(
    backend,
    collector: Collector,
    *,
    limit: int = 30,
) -> ImportReport:
    """从系统剪贴板历史导入记录。

    `backend` 需实现 `read_windows()`；不支持的后端会得到一份
    带 `unsupported_reason` 的报告，而不是抛异常 —— 调用方（CLI/界面）
    据此给用户一句可读的解释即可。
    """
    report = ImportReport()

    reader = getattr(backend, "read_windows", None)
    if reader is None:
        report.unsupported_reason = "当前平台后端不提供系统剪贴板历史读取"
        return report

    if not _supports(backend):
        report.unsupported_reason = _unsupported_hint(backend)
        return report

    try:
        results = reader(limit=limit) or []
    except Exception as exc:  # noqa: BLE001 - 导入是增强能力，不能拖垮调用方
        _log.warning("读取系统剪贴板历史失败：%r", exc)
        report.unsupported_reason = f"读取系统剪贴板历史失败：{exc}"
        return report

    for result in results:
        if result is None or result.is_empty:
            continue
        report.scanned += 1
        outcome, item = collector.process(_to_item(result))
        if outcome == CAPTURED:
            report.captured += 1
        elif outcome == MERGED:
            report.merged += 1
        elif outcome == SKIPPED_PRIVACY:
            report.skipped_privacy += 1
            if len(report.blocked_samples) < 5:
                # 截到 48 字符：够看清是哪类内容，又不至于把整段密钥
                # 又打印回终端 —— 拦下来又打出来等于没拦。
                preview = (result.text or "")[:48].replace("\n", " ")
                report.blocked_samples.append(preview)
        else:
            report.skipped_other += 1
    return report


def _supports(backend) -> bool:
    checker = getattr(backend, "is_history_supported", None)
    if checker is None:
        # 老式后端只实现了 read_windows()、没实现能力探测：交给读取本身去收敛
        return True
    try:
        return bool(checker())
    except Exception:  # noqa: BLE001
        return False


def _unsupported_hint(backend) -> str:
    name = getattr(backend, "name", "unknown")
    if name == "windows":
        return (
            "系统剪贴板历史不可用：需要 Windows 10 1809+、"
            "在「设置 → 系统 → 剪贴板」里打开历史，并安装 winrt/winsdk 投影"
        )
    return f"{name} 后端不提供系统剪贴板历史"
