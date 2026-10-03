"""原生能力自检：三级判定、异常隔离、输出格式。

自检工具自身如果不稳，排查时就成了新的噪声源，
所以这里重点测「探测抛异常时还能不能给出结论」。
"""

from __future__ import annotations

import json

import pytest

from linglong_clip.platform.base import ClipboardBackend, FocusInfo, NullBackend
from linglong_clip.utils.selfcheck import (
    DEGRADED,
    FAIL,
    OK,
    SKIP,
    CheckResult,
    NativeSelfCheck,
    SelfCheckReport,
    format_report,
    run_self_check,
)


class HealthBackend(ClipboardBackend):
    """一个「什么都能干」的后端，用来验证全绿路径。"""

    name = "healthy"

    def capabilities(self):
        return {
            "primary_selection": True,
            "native_history": True,
            "focus_detection": True,
            "transient_clipboard": True,
            "wayland": False,
        }

    def current_focus(self):
        return FocusInfo(app_id="chrome", app_name="Chrome", window_title="知乎")

    def sanity_check(self):
        return True, "读写链路正常"

    def history_exclusion_formats(self):
        # 「什么都能干」的后端也要能声明出站排除，否则全绿路径走不到 ——
        # 自检新增的出站防护一项会如实报「降级」。
        return ["ExcludeClipboardContentFromMonitorProcessing"]


class BrokeBackend(HealthBackend):
    name = "broke"

    def capabilities(self):
        raise RuntimeError("capabilities 炸了")

    def current_focus(self):
        raise RuntimeError("current_focus 炸了")

    def sanity_check(self):
        return False, "调不通"

    def is_available(self):
        return True


class FakeHotkeys:
    """假 HotkeyManager：按需模拟各种可用性。"""

    def __init__(self, name="fake", available=True, register_ok=True, reason=""):
        self._name = name
        self._available = available
        self._register_ok = register_ok
        self._reason = reason
        self.registered = []
        self.unregistered = 0

    @property
    def backend_name(self):
        return self._name

    @property
    def is_available(self):
        return self._available

    @property
    def reason(self):
        return self._reason

    def register(self, sequence, callback):
        if not self._register_ok:
            return False
        self.registered.append(sequence)
        return True

    def unregister_all(self):
        self.unregistered += 1


def _run(backend=None, hotkeys=None, monitor=None):
    return NativeSelfCheck(
        backend=backend,
        hotkeys_factory=(lambda: hotkeys) if hotkeys else None,
        monitor=monitor,
    ).run()


def _find(report, name):
    """按名字取一项检查结果 —— 比按下标可靠，插入新检查不会让用例错位。"""
    for item in report.results:
        if item.name == name:
            return item
    raise AssertionError(f"报告里没有「{name}」这一项：{[i.name for i in report.results]}")


class FakeMonitor:
    """只带 `is_event_driven` 的假监视器 —— 自检只需要这一个事实。"""

    def __init__(self, event_driven):
        self.is_event_driven = event_driven


# ---------------------------------------------------------------- 全绿


def test_healthy_backend_reports_no_failure():
    report = _run(HealthBackend(), FakeHotkeys())
    assert report.failures == []
    assert report.healthy is True
    assert report.backend == "healthy"


def test_healthy_backend_marks_clipboard_ok():
    report = _run(HealthBackend(), FakeHotkeys())
    entry = next(r for r in report.results if r.name == "剪贴板原生读取")
    assert entry.status == OK


def test_hotkey_probe_registers_and_cleans_up():
    fake = FakeHotkeys()
    _run(HealthBackend(), fake)
    assert fake.registered == ["Ctrl+Alt+Shift+F24"]
    assert fake.unregistered == 1


def test_hotkey_probe_uses_obscure_combination():
    """探测用的组合不能是用户常用的，否则会抢占真实快捷键。"""
    from linglong_clip.utils.selfcheck import PROBE_SEQUENCE

    assert "F24" in PROBE_SEQUENCE
    assert PROBE_SEQUENCE != "Ctrl+Shift+V"


# ---------------------------------------------------------------- 后端异常


def test_backend_exception_becomes_failure_not_crash():
    report = _run(BrokeBackend(), FakeHotkeys())
    names = {r.name: r.status for r in report.results}
    assert names["后端能力矩阵"] == FAIL
    assert names["前台窗口识别"] == FAIL
    assert report.healthy is False


def test_degraded_clipboard_is_not_failure():
    report = _run(BrokeBackend(), FakeHotkeys())
    entry = next(r for r in report.results if r.name == "剪贴板原生读取")
    assert entry.status == DEGRADED


def test_null_backend_is_degraded():
    report = _run(NullBackend(), FakeHotkeys())
    entry = next(r for r in report.results if r.name == "被选中的后端")
    assert entry.status == DEGRADED
    assert "兜底" in entry.detail


def test_backend_without_sanity_check_is_skipped():
    class NoProbe(ClipboardBackend):
        name = "noprobe"

        def capabilities(self):
            return {}

        def current_focus(self):
            return FocusInfo(app_id="x")

    report = _run(NoProbe(), FakeHotkeys())
    entry = next(r for r in report.results if r.name == "剪贴板原生读取")
    assert entry.status == SKIP


def test_empty_focus_is_degraded_with_hint():
    class NoFocus(ClipboardBackend):
        name = "nofocus"

        def capabilities(self):
            return {}

    report = _run(NoFocus(), FakeHotkeys())
    entry = next(r for r in report.results if r.name == "前台窗口识别")
    assert entry.status == DEGRADED
    assert "SSH" in entry.hint


# ---------------------------------------------------------------- 快捷键异常


def test_hotkey_backend_none_is_degraded():
    report = _run(HealthBackend(), FakeHotkeys(name="none", available=False))
    entry = next(r for r in report.results if r.name == "全局快捷键后端")
    assert entry.status == DEGRADED


def test_hotkey_backend_unavailable_is_degraded():
    fake = FakeHotkeys(available=False, reason="没有 X 服务器")
    report = _run(HealthBackend(), fake)
    entry = next(r for r in report.results if r.name == "全局快捷键后端")
    assert entry.status == DEGRADED
    assert "X 服务器" in entry.hint


def test_hotkey_probe_failure_is_degraded_not_failure():
    """注册失败可能是被别的程序占用，不该报成 fail。"""
    report = _run(HealthBackend(), FakeHotkeys(register_ok=False, reason="被占用"))
    entry = next(r for r in report.results if r.name == "全局快捷键注册")
    assert entry.status == DEGRADED
    assert "被占用" in entry.detail


def test_hotkey_probe_skipped_when_backend_unavailable():
    report = _run(HealthBackend(), FakeHotkeys(available=False))
    entry = next(r for r in report.results if r.name == "全局快捷键注册")
    assert entry.status == SKIP


# ------------------------------------------------------ 出站防护


class _ConfigStub:
    def __init__(self, values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def _run_with_config(backend, seconds):
    return NativeSelfCheck(
        backend=backend,
        hotkeys_factory=lambda: FakeHotkeys(),
        config=_ConfigStub({"privacy.sensitive_auto_clear_seconds": seconds}),
    ).run()


def test_outbound_reports_system_level_exclusion():
    """声明了系统级排除就必须报出来 —— 这是 README 承诺的那一条。"""
    report = _run(HealthBackend(), FakeHotkeys())
    entry = next(r for r in report.results if r.name == "出站防护（复制敏感内容）")
    assert entry.status == OK
    assert "ExcludeClipboardContentFromMonitorProcessing" in entry.detail


def test_outbound_reports_auto_clear_seconds():
    report = _run_with_config(HealthBackend(), 45)
    entry = next(r for r in report.results if r.name == "出站防护（复制敏感内容）")
    assert "45 秒后清空剪贴板" in entry.detail


def test_outbound_degraded_without_any_means():
    """没有任何手段时必须如实报降级，而不是含糊地报「正常」。

    此前设置页有「剪贴板自动清空」而没有人读它，界面看起来一切正常。
    """
    class Bare(ClipboardBackend):
        name = "bare"

    report = _run_with_config(Bare(), 0)
    entry = next(r for r in report.results if r.name == "出站防护（复制敏感内容）")
    assert entry.status == DEGRADED
    assert "无额外手段" in entry.detail
    assert "自动清空" in entry.hint


def test_outbound_ok_when_only_auto_clear_configured():
    """没有系统机制但配了限时清空，算「有手段」而非「什么都没有」。"""
    class Bare(ClipboardBackend):
        name = "bare"

    report = _run_with_config(Bare(), 30)
    entry = next(r for r in report.results if r.name == "出站防护（复制敏感内容）")
    assert entry.status == OK
    assert "30 秒后清空剪贴板" in entry.detail


def test_outbound_probe_failure_is_reported_not_raised():
    class Exploding(ClipboardBackend):
        name = "exploding"

        def history_exclusion_formats(self):
            raise RuntimeError("探测炸了")

    report = _run_with_config(Exploding(), 0)
    entry = next(r for r in report.results if r.name == "出站防护（复制敏感内容）")
    assert entry.status == FAIL
    assert "探测炸了" in entry.detail


def test_outbound_empty_format_names_ignored():
    class Blank(ClipboardBackend):
        name = "blank"

        def history_exclusion_formats(self):
            return ["", None]

    report = _run_with_config(Blank(), 0)
    entry = next(r for r in report.results if r.name == "出站防护（复制敏感内容）")
    # 只有空白格式名不算「有手段」
    assert entry.status == DEGRADED


def test_outbound_check_survives_config_get_failure():
    class BadConfig:
        def get(self, key, default=None):
            raise RuntimeError("配置读不了")

    report = NativeSelfCheck(
        backend=HealthBackend(),
        hotkeys_factory=lambda: FakeHotkeys(),
        config=BadConfig(),
    ).run()
    # 读配置失败不该让自检崩，排除格式仍应报出来
    entry = next(r for r in report.results if r.name == "出站防护（复制敏感内容）")
    assert entry.status == OK


# ---------------------------------------------------------------- 报告


def test_report_summary_counts():
    report = _run(HealthBackend(), FakeHotkeys())
    assert "项正常" in report.summary


def test_report_notes_default_when_all_good():
    report = _run(HealthBackend(), FakeHotkeys())
    assert report.notes == ["全部原生能力均按预期可用"]


def test_report_to_dict_is_json_serializable():
    report = _run(HealthBackend(), FakeHotkeys())
    payload = json.loads(json.dumps(report.to_dict(), ensure_ascii=False))
    assert payload["backend"] == "healthy"
    assert payload["healthy"] is True
    assert isinstance(payload["checks"], list)


def test_format_report_contains_all_checks():
    report = _run(HealthBackend(), FakeHotkeys())
    text = format_report(report)
    assert "玲珑剪切板 · 原生能力自检" in text
    assert "结论：" in text
    for item in report.results:
        assert item.name in text


def test_format_report_marks_failures():
    report = _run(BrokeBackend(), FakeHotkeys())
    text = format_report(report)
    assert "✗" in text


def test_elapsed_ms_recorded():
    report = _run(HealthBackend(), FakeHotkeys())
    assert all(r.elapsed_ms >= 0 for r in report.results)


def test_check_result_to_dict_rounds_elapsed():
    entry = CheckResult("x", OK, "detail", "hint", elapsed_ms=1.23456)
    assert entry.to_dict()["elapsed_ms"] == 1.235


def test_report_explicit_backend_is_used():
    report = _run(HealthBackend(), FakeHotkeys())
    assert report.backend == "healthy"


def test_run_self_check_uses_real_backend_when_none_given():
    """不传后端时应自己探测，且绝不能抛异常。"""
    report = run_self_check()
    assert report.results
    assert report.backend


def test_selfcheck_never_raises_on_broken_hotkeys():
    class Exploding:
        @property
        def backend_name(self):
            raise RuntimeError("炸了")

    # 只要不抛到调用方就算通过
    report = _run(HealthBackend(), Exploding())
    assert report.results


# ---------------------------------------------------------------- OCR


class _Cfg:
    """最小配置替身：自检只用到 get。"""

    def __init__(self, values=None):
        self._values = values or {}

    def get(self, key, default=None):
        return self._values.get(key, default)


def _ocr_result(report):
    return next(r for r in report.results if r.name == "截图 OCR")


def test_ocr_check_present_in_report():
    """OCR 是文档里承诺的能力，自检必须覆盖它。"""
    report = _run(HealthBackend(), FakeHotkeys())
    assert _ocr_result(report) is not None


def test_ocr_check_degraded_when_disabled():
    report = NativeSelfCheck(
        backend=HealthBackend(),
        hotkeys_factory=lambda: FakeHotkeys(),
        config=_Cfg({"ocr.enabled": False}),
    ).run()
    entry = _ocr_result(report)
    assert entry.status == DEGRADED
    assert "未启用" in entry.detail


def test_ocr_check_ok_with_installed_language(monkeypatch):
    """用桩后端模拟「tesseract 在、语言也对得上」。"""
    import linglong_clip.core.ocr as ocr

    class Backend(ocr.TesseractBackend):
        def path(self):
            return "/usr/bin/tesseract"

        def languages(self):
            return ("chi_sim", "eng")

    monkeypatch.setattr(ocr, "TesseractBackend", Backend)
    report = NativeSelfCheck(
        backend=HealthBackend(),
        hotkeys_factory=lambda: FakeHotkeys(),
        config=_Cfg({"ocr.enabled": True, "ui.language": "zh_CN"}),
    ).run()
    entry = _ocr_result(report)
    assert entry.status == OK
    assert "chi_sim" in entry.detail


def test_ocr_check_degraded_when_language_missing(monkeypatch):
    """核心回归：期望的语言没装时不能报「正常」。

    此前设置页会显示「可用 · 已安装语言：chi_sim、eng」，而实际请求 jpn，
    tesseract 整批失败 —— 每一项单看都正常，合起来一张图也识别不了。
    """
    import linglong_clip.core.ocr as ocr

    class Backend(ocr.TesseractBackend):
        def path(self):
            return "/usr/bin/tesseract"

        def languages(self):
            return ("chi_sim", "eng")

    monkeypatch.setattr(ocr, "TesseractBackend", Backend)
    report = NativeSelfCheck(
        backend=HealthBackend(),
        hotkeys_factory=lambda: FakeHotkeys(),
        config=_Cfg({"ocr.enabled": True, "ui.language": "ja_JP"}),
    ).run()
    entry = _ocr_result(report)
    assert entry.status == DEGRADED
    assert "jpn" in entry.detail
    assert "eng" in entry.detail
    assert entry.hint


def test_ocr_check_fails_when_no_language_data(monkeypatch):
    """装了 tesseract 但一个语言都没有 —— 这是明确要用户动手的，判为失败。"""
    import linglong_clip.core.ocr as ocr

    class Backend(ocr.TesseractBackend):
        def path(self):
            return "/usr/bin/tesseract"

        def languages(self):
            return ("osd",)

    monkeypatch.setattr(ocr, "TesseractBackend", Backend)
    report = NativeSelfCheck(
        backend=HealthBackend(),
        hotkeys_factory=lambda: FakeHotkeys(),
        config=_Cfg({"ocr.enabled": True}),
    ).run()
    entry = _ocr_result(report)
    assert entry.status == FAIL
    assert report.healthy is False
    assert "语言" in entry.hint


def test_ocr_check_survives_engine_build_exception(monkeypatch):
    """自检不能因为 OCR 探测抛异常就整体崩掉。"""
    import linglong_clip.core.ocr as ocr

    def boom(_config=None):
        raise RuntimeError("构建炸了")

    monkeypatch.setattr(ocr, "build_engine", boom)
    report = NativeSelfCheck(
        backend=HealthBackend(), hotkeys_factory=lambda: FakeHotkeys()
    ).run()
    entry = _ocr_result(report)
    assert entry.status == FAIL
    assert "构建" in entry.detail


def test_ocr_check_defaults_to_real_config_when_none():
    """不传配置时也要真的探测，而不是当成「未启用」。"""
    report = _run(HealthBackend(), FakeHotkeys())
    assert _ocr_result(report).status in (OK, DEGRADED, FAIL)


def test_run_self_check_accepts_config():
    report = run_self_check(
        backend=HealthBackend(), hotkeys_factory=lambda: FakeHotkeys(), config=_Cfg()
    )
    assert _ocr_result(report) is not None


# ------------------------------------------------- 主选择区自检

class _PrimaryBackend:
    name = "linux"

    def __init__(self, caps=True, text="选中内容", explode=False):
        self._caps = caps
        self._text = text
        self._explode = explode
        self.reads = 0

    def capabilities(self):
        return {"primary_selection": self._caps}

    def read_primary_text(self):
        self.reads += 1
        if self._explode:
            raise RuntimeError("X 连接断了")
        return self._text


def _primary_result(backend):
    report = NativeSelfCheck(backend=backend).run()
    return next(r for r in report.results if r.name == "主选择区读取")


def test_primary_check_ok_when_readable():
    backend = _PrimaryBackend(text="选中的一段文字")
    result = _primary_result(backend)
    assert result.status == OK
    assert "字符" in result.detail


def test_primary_check_ok_when_selection_empty():
    """通道可用但当前没有选中内容 —— 这是正常状态，不是故障。"""
    result = _primary_result(_PrimaryBackend(text=""))
    assert result.status == OK
    assert "为空" in result.detail


def test_primary_check_degraded_when_unsupported():
    result = _primary_result(_PrimaryBackend(caps=False))
    assert result.status == DEGRADED
    assert "wl-clipboard" in result.detail
    # 必须说清「这只是一个可选功能」，否则用户会以为采集坏了
    assert "不受影响" in result.hint


def test_primary_check_fail_when_read_raises():
    """能力声明为可用、真读却失败 —— 这正是能力矩阵看不出来的那类问题。"""
    result = _primary_result(_PrimaryBackend(explode=True))
    assert result.status == FAIL
    assert "读取失败" in result.detail


def test_primary_check_fail_when_capabilities_raises():
    class _Broken:
        name = "linux"

        def capabilities(self):
            raise RuntimeError("没救了")

    result = _primary_result(_Broken())
    assert result.status == FAIL
    assert "能力探测失败" in result.detail


# ---------------------------------------------------------------- 采集通道


class EventBackend(HealthBackend):
    """支持事件通知的后端（`requires_polling = False`）。"""

    name = "event-capable"
    requires_polling = False


def test_monitor_mode_reports_event_driven():
    report = _run(EventBackend(), FakeHotkeys(), monitor=FakeMonitor(True))
    item = _find(report, "采集通道")
    assert item.status == OK
    assert "事件驱动" in item.detail


def test_monitor_mode_reports_degraded_when_events_failed_silently():
    """本平台支持事件通知、实际却在轮询 —— 必须说出来。

    这是它存在的理由：Windows 上 `AddClipboardFormatListener` 注册失败会
    静默退回轮询，界面上毫无痕迹，而采集延迟已经变了。
    """
    report = _run(EventBackend(), FakeHotkeys(), monitor=FakeMonitor(False))
    item = _find(report, "采集通道")
    assert item.status == DEGRADED
    assert "而非事件驱动" in item.detail
    assert "窗口句柄" in item.hint


def test_monitor_mode_ok_on_poll_only_platform():
    """只能在轮询的平台上报 OK 而不是降级 —— 那不是缺陷。"""
    report = _run(HealthBackend(), FakeHotkeys(), monitor=FakeMonitor(False))
    item = _find(report, "采集通道")
    assert item.status == OK
    assert "轮询" in item.detail


def test_monitor_mode_without_running_monitor_does_not_claim_events():
    """没有运行中的监视器时不许编出「当前是事件驱动」这个结论。"""
    report = _run(EventBackend(), FakeHotkeys())
    item = _find(report, "采集通道")
    assert item.status == OK
    assert "当前" not in item.detail
    assert "支持事件驱动采集" in item.detail
