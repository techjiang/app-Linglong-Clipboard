"""原生能力自检：把「这台机器上到底哪些原生能力真的可用」问清楚。

存在的理由：平台相关的代码在 CI 上跑不到（Windows 的 `user32`、macOS 的
`Quartz` 在 Linux 机器上无法真实调用），此前只验过「接口形状」。
于是出现一种最坏情况——接口对得上，功能是空的（例如 macOS 的全局快捷键
只探测了 pyobjc 是否可导入，`register()` 从未覆写，永远返回 False）。

这个模块按「与真实运行完全相同的顺序」调用一遍后端，
把每一步的结果、耗时、失败原因摊开，供真机排查使用：

    linglong-clipboard --selfcheck

判定分三级，不合并成一句「不支持」：

- `ok`   —— 调通了
- `degraded` —— 调不通但已按设计降级，功能由别的路径提供
- `fail` —— 预期应能用却没有，需要排查
"""

from __future__ import annotations

import platform
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List

OK = "ok"
DEGRADED = "degraded"
FAIL = "fail"
SKIP = "skip"

#: 自检用的探测组合：极不常用，避免抢占用户正在使用的快捷键
PROBE_SEQUENCE = "Ctrl+Alt+Shift+F24"


@dataclass
class CheckResult:
    """单项自检结果。"""

    name: str
    status: str
    detail: str = ""
    hint: str = ""
    elapsed_ms: float = 0.0

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


@dataclass
class SelfCheckReport:
    platform_label: str = ""
    python_version: str = ""
    backend: str = ""
    results: List[CheckResult] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def failures(self) -> List[CheckResult]:
        return [r for r in self.results if r.status == FAIL]

    @property
    def degraded(self) -> List[CheckResult]:
        return [r for r in self.results if r.status == DEGRADED]

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.status == OK)

    @property
    def healthy(self) -> bool:
        return not self.failures

    @property
    def summary(self) -> str:
        parts = [f"{self.ok_count} 项正常"]
        if self.degraded:
            parts.append(f"{len(self.degraded)} 项降级")
        if self.failures:
            parts.append(f"{len(self.failures)} 项失败")
        return "、".join(parts)

    def to_dict(self) -> Dict[str, object]:
        return {
            "platform": self.platform_label,
            "python": self.python_version,
            "backend": self.backend,
            "summary": self.summary,
            "healthy": self.healthy,
            "checks": [r.to_dict() for r in self.results],
            "notes": self.notes,
        }


class NativeSelfCheck:
    """对平台后端与全局快捷键做一次真实调用链的自检。

    只做只读探测：除注册/撤销一次全局快捷键外，不产生任何副作用，
    也不会改写用户的剪贴板。
    """

    def __init__(self, backend=None, hotkeys_factory=None, config=None) -> None:
        self._backend = backend
        self._hotkeys_factory = hotkeys_factory
        #: OCR 检查需要读配置；为 None 时按默认配置探测
        self._config = config

    # ---- 入口 ----

    def run(self) -> SelfCheckReport:
        report = SelfCheckReport(
            platform_label=platform.platform(),
            python_version=sys.version.split()[0],
        )
        backend = self._backend
        if backend is None:
            from ..platform import detect_backend

            backend = detect_backend()
        report.backend = getattr(backend, "name", "unknown")

        checks: List[Callable[[], CheckResult]] = [
            lambda: self._check_backend(backend),
            lambda: self._check_capabilities(backend),
            lambda: self._check_clipboard_probe(backend),
            lambda: self._check_focus(backend),
            lambda: self._check_outbound_guard(backend),
            lambda: self._check_primary_selection(backend),
            self._check_hotkey_backend,
            self._check_hotkey_probe,
            self._check_ocr,
        ]
        for check in checks:
            report.results.append(self._timed(check))
        report.notes = self._build_notes(report)
        return report

    @staticmethod
    def _timed(fn: Callable[[], CheckResult]) -> CheckResult:
        started = time.perf_counter()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - 自检自身绝不能抛异常
            result = CheckResult("未命名检查", FAIL, f"检查过程抛出异常：{exc!r}")
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        return result

    def _make_hotkeys(self):
        if self._hotkeys_factory is not None:
            return self._hotkeys_factory()
        from ..ui.hotkeys import HotkeyManager

        return HotkeyManager()

    # ---- 各项检查 ----

    @staticmethod
    def _check_backend(backend) -> CheckResult:
        from ..platform.base import NullBackend

        name = getattr(backend, "name", "unknown")
        if isinstance(backend, NullBackend):
            return CheckResult(
                "被选中的后端",
                DEGRADED,
                f"{name}（兜底后端）",
                "未匹配到平台后端，剪贴板读写全部落在 Qt 层，功能可用但没有原生增强",
            )
        return CheckResult("被选中的后端", OK, name)

    @staticmethod
    def _check_capabilities(backend) -> CheckResult:
        try:
            caps = backend.capabilities()
        except Exception as exc:  # noqa: BLE001
            return CheckResult("后端能力矩阵", FAIL, f"capabilities() 失败：{exc!r}")
        text = "、".join(f"{key}={'是' if value else '否'}" for key, value in caps.items())
        return CheckResult("后端能力矩阵", OK, text)

    @staticmethod
    def _check_clipboard_probe(backend) -> CheckResult:
        """验证后端真实读取能力（只读，不改写剪贴板）。"""
        from ..platform.base import NullBackend

        if isinstance(backend, NullBackend):
            return CheckResult(
                "剪贴板原生读取",
                DEGRADED,
                "兜底后端不提供原生读取",
                "该项由 Qt 层提供，不影响使用",
            )
        probe = getattr(backend, "sanity_check", None)
        if probe is None:
            return CheckResult(
                "剪贴板原生读取",
                SKIP,
                "该后端未实现 sanity_check()",
                "只影响自检的深度，不代表功能缺失",
            )
        ok, detail = probe()
        if ok:
            return CheckResult("剪贴板原生读取", OK, detail)
        return CheckResult(
            "剪贴板原生读取",
            DEGRADED,
            detail,
            "读取失败会回落到 Qt 剪贴板，功能仍可用",
        )

    @staticmethod
    def _check_focus(backend) -> CheckResult:
        try:
            info = backend.current_focus()
        except Exception as exc:  # noqa: BLE001
            return CheckResult("前台窗口识别", FAIL, f"current_focus() 失败：{exc!r}")
        if info.is_empty:
            return CheckResult(
                "前台窗口识别",
                DEGRADED,
                "未取到前台窗口",
                "无图形会话（SSH / 无桌面）时属正常；有桌面却为空则需排查",
            )
        return CheckResult(
            "前台窗口识别", OK, f"{info.display}（{info.window_title or '无标题'}）"
        )

    def _check_outbound_guard(self, backend) -> CheckResult:
        """敏感内容复制出去时，这台机器能做什么防护。

        这项此前根本不存在 —— 于是「安全粘贴标记 ✅ 系统级」写在 README 里，
        而实现从未接上连接用方都没有，谁也发现不了。自检回答的是
        **实际会生效的手段**，不是「代码里有没有这个函数」。
        """
        formats: List[str] = []
        getter = getattr(backend, "history_exclusion_formats", None)
        if getter is not None:
            try:
                formats = [str(item) for item in (getter() or []) if item]
            except Exception as exc:  # noqa: BLE001
                return CheckResult(
                    "出站防护（复制敏感内容）",
                    FAIL,
                    f"history_exclusion_formats() 失败：{exc!r}",
                )

        seconds = 0
        if self._config is not None:
            try:
                seconds = int(
                    self._config.get("privacy.sensitive_auto_clear_seconds", 30) or 0
                )
            except Exception:  # noqa: BLE001
                seconds = 0

        parts: List[str] = []
        if formats:
            parts.append(f"系统级排除剪贴板历史（{'、'.join(formats)}）")
        if seconds > 0:
            parts.append(f"{seconds} 秒后清空剪贴板")
        if not parts:
            return CheckResult(
                "出站防护（复制敏感内容）",
                DEGRADED,
                "仅依赖「敏感内容不落盘」，复制出站无额外手段",
                "在设置 → 隐私 里设定「剪贴板自动清空」可减少暴露窗口",
            )
        return CheckResult("出站防护（复制敏感内容）", OK, "；".join(parts))

    @staticmethod
    def _check_primary_selection(backend) -> CheckResult:
        """主选择区（Linux PRIMARY）是否真的读得到。

        与能力矩阵的区别：能力矩阵回答「按声明算能不能用」，
        这里回答「现在这一秒读一次能不能读到」。两者会不一致 ——
        X11 下声明可用、但 X 服务器刚断掉时读就是空。
        读取本身是只读的，不改写任何选择区内容。
        """
        try:
            caps = backend.capabilities()
        except Exception as exc:  # noqa: BLE001
            return CheckResult("主选择区读取", FAIL, f"能力探测失败：{exc!r}")

        if not caps.get("primary_selection", False):
            return CheckResult(
                "主选择区读取",
                DEGRADED,
                "本机无可用通道（X11 需 python-xlib 或 xclip/xsel；Wayland 需 wl-clipboard）",
                "「选中即记录」不可用；仅影响该可选功能，采集与检索不受影响",
            )

        reader = getattr(backend, "read_primary_text", None)
        if reader is None:
            return CheckResult("主选择区读取", FAIL, "后端未提供 read_primary_text()")
        try:
            text = reader()
        except Exception as exc:  # noqa: BLE001
            return CheckResult("主选择区读取", FAIL, f"读取失败：{exc!r}")
        if text:
            return CheckResult("主选择区读取", OK, f"读到 {len(text)} 字符")
        return CheckResult(
            "主选择区读取",
            OK,
            "通道可用，当前选择区为空（没有未复制的选中内容）",
        )

    def _check_hotkey_backend(self) -> CheckResult:
        manager = self._make_hotkeys()
        name = manager.backend_name
        if name == "none":
            return CheckResult(
                "全局快捷键后端",
                DEGRADED,
                "无可用后端",
                manager.reason or "当前平台未提供全局快捷键实现",
            )
        if not manager.is_available:
            return CheckResult(
                "全局快捷键后端",
                DEGRADED,
                f"{name} 当前不可用",
                manager.reason or "后端存在但环境条件不满足",
            )
        return CheckResult("全局快捷键后端", OK, name)

    def _check_hotkey_probe(self) -> CheckResult:
        """真实注册一次快捷键再撤销，确认能力不是「只在代码里存在」。"""
        probe = self._make_hotkeys()
        if probe.backend_name == "none":
            return CheckResult("全局快捷键注册", SKIP, "无后端，跳过注册探测")
        if not probe.is_available:
            return CheckResult(
                "全局快捷键注册",
                SKIP,
                f"后端不可用（{probe.reason or '环境不满足'}）",
                "先解决上方「全局快捷键后端」一项",
            )

        registered = probe.register(PROBE_SEQUENCE, lambda: None)
        try:
            if registered:
                return CheckResult(
                    "全局快捷键注册", OK, f"已成功注册并撤销 {PROBE_SEQUENCE}"
                )
            return CheckResult(
                "全局快捷键注册",
                DEGRADED,
                f"注册失败：{probe.reason or '未知原因'}",
                "被其它程序占用属正常；若是权限问题（macOS 辅助功能 / X11 被其它抓键者独占）需另行处理",
            )
        finally:
            probe.unregister_all()

    def _check_ocr(self) -> CheckResult:
        """检查 OCR 是否**真的**能跑，而不只是「tesseract 在 PATH 里」。

        这里的价值在于把「语言数据缺失」揪出来 —— 它单独看每一项都正常：
        tesseract 装了、开关开了、设置页显示「已安装语言：chi_sim、eng」，
        但实际请求的语言没装时 tesseract 会整批失败，一张截图都识别不了。
        """
        try:
            from ..core.ocr import build_engine

            config = self._config
            if config is None:
                # 不传配置时用默认配置探测，而不是 build_engine(None) ——
                # 后者会返回「禁用」的引擎，把 OCR 报成未启用，与实际情况不符
                from .config import Config

                config = Config()
            engine = build_engine(config)
        except Exception as exc:  # noqa: BLE001 - 自检不能因探测失败而崩
            return CheckResult("截图 OCR", FAIL, f"构建引擎失败：{exc!r}")

        if not engine.enabled:
            return CheckResult(
                "截图 OCR",
                DEGRADED,
                "未启用或未找到 tesseract",
                "截图照常记录，只是搜不到图里的文字；"
                "需要时安装 tesseract 与对应语言包（如 tesseract-ocr-chi-sim）",
            )

        choice = engine.language_choice()
        installed = "、".join(engine.status().get("available", [])) or "无"

        if not choice.usable:
            return CheckResult(
                "截图 OCR",
                FAIL,
                f"tesseract 已安装，但没有任何语言数据（已安装：{installed}）",
                "装语言包后即可用，例如 Debian/Ubuntu 的 tesseract-ocr-chi-sim / "
                "tesseract-ocr-eng；macOS 用 brew install tesseract-lang",
            )

        if choice.degraded:
            return CheckResult(
                "截图 OCR",
                DEGRADED,
                f"实际使用 {choice.languages}（期望 {choice.wants}）· 已安装：{installed}",
                f"{choice.describe()}。装齐语言包，或在设置页「识别语言」里手动指定已装的",
            )

        return CheckResult(
            "截图 OCR", OK, f"{choice.languages} · 已安装：{installed}"
        )

    # ---- 结论 ----

    @staticmethod
    def _build_notes(report: SelfCheckReport) -> List[str]:
        notes: List[str] = []
        for failure in report.failures:
            notes.append(
                f"{failure.name}：{failure.detail}（{failure.hint or '需要排查'}）"
            )
        for item in report.degraded:
            notes.append(f"{item.name} 已降级：{item.detail}")
        if not notes:
            notes.append("全部原生能力均按预期可用")
        return notes


def format_report(report: SelfCheckReport) -> str:
    """人类可读输出。"""
    icon = {OK: "✓", DEGRADED: "~", FAIL: "✗", SKIP: "-"}
    lines = [
        "玲珑剪切板 · 原生能力自检",
        f"平台：{report.platform_label}",
        f"Python：{report.python_version}",
        f"后端：{report.backend or '未知'}",
        "",
    ]
    for item in report.results:
        mark = icon.get(item.status, "?")
        lines.append(f"  {mark} {item.name}：{item.detail}（{item.elapsed_ms:.1f}ms）")
        if item.hint and item.status in (FAIL, DEGRADED):
            lines.append(f"      提示：{item.hint}")
    lines.append("")
    lines.append(f"结论：{report.summary}")
    for note in report.notes:
        lines.append(f"  · {note}")
    return "\n".join(lines)


def run_self_check(backend=None, hotkeys_factory=None, config=None) -> SelfCheckReport:
    """便捷入口。"""
    return NativeSelfCheck(
        backend=backend, hotkeys_factory=hotkeys_factory, config=config
    ).run()


__all__ = [
    "CheckResult",
    "NativeSelfCheck",
    "SelfCheckReport",
    "FAIL",
    "DEGRADED",
    "OK",
    "SKIP",
    "PROBE_SEQUENCE",
    "format_report",
    "run_self_check",
]
