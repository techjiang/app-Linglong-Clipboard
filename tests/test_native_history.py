"""系统剪贴板历史导入。

重点不在「能导入」，而在**导入不能绕过隐私判定** ——
这个入口能一次性把系统里攒了很久的内容全捞进来，
一旦它自己另开一条落库路径，就成了一扇后门。
"""

from __future__ import annotations

import pytest

from linglong_clip.core.collector import Collector
from linglong_clip.core.native_history import import_native_history
from linglong_clip.core.privacy import PrivacyGuard
from linglong_clip.platform.base import CaptureResult, FocusInfo


class _FakeHistoryBackend:
    """可控的系统历史后端。"""

    name = "windows"

    def __init__(self, texts=None, *, supported=True, raises=None):
        self.texts = list(texts or [])
        self._supported = supported
        self.raises = raises
        self.calls = 0

    def is_history_supported(self) -> bool:
        return self._supported

    def read_windows(self, limit: int = 30):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return [CaptureResult(kind="text", text=text) for text in self.texts[:limit]]


def _collector(storage, config):
    return Collector(storage, config, PrivacyGuard(config))


# --------------------------------------------------------------- 基本导入


def test_imports_history_items(storage, config):
    backend = _FakeHistoryBackend(["第一条", "第二条"])
    report = import_native_history(backend, _collector(storage, config))

    assert report.supported is True
    assert report.scanned == 2
    assert report.captured == 2
    assert storage.count() == 2


def test_import_marks_provenance(storage, config):
    """导入的记录要能看出「不是这次会话抄来的」，否则无从排查。"""
    backend = _FakeHistoryBackend(["来自系统历史"])
    import_native_history(backend, _collector(storage, config))

    item = storage.list(limit=1)[0]
    assert item.metadata.get("imported_from") == "system_history"


def test_import_classifies_kind(storage, config):
    """历史里的 URL 也该被识别成链接，而不是一律当纯文本。"""
    backend = _FakeHistoryBackend(["https://cnb.cool/x"])
    import_native_history(backend, _collector(storage, config))

    item = storage.list(limit=1)[0]
    assert item.kind == "link"


def test_import_is_idempotent(storage, config):
    """同一批历史导入两次，第二次应全部计为「已存在」。"""
    backend = _FakeHistoryBackend(["重复内容"])
    first = import_native_history(backend, _collector(storage, config))
    second = import_native_history(backend, _collector(storage, config))

    assert first.captured == 1
    assert second.captured == 0
    assert second.merged == 1
    assert storage.count() == 1


def test_import_respects_limit(storage, config):
    backend = _FakeHistoryBackend([f"内容{i}" for i in range(10)])
    report = import_native_history(backend, _collector(storage, config), limit=3)
    assert report.scanned == 3
    assert storage.count() == 3


def test_import_skips_empty_results(storage, config):
    class _Sparse(_FakeHistoryBackend):
        def read_windows(self, limit: int = 30):
            return [CaptureResult(text="有内容"), CaptureResult(), None]

    report = import_native_history(_Sparse(), _collector(storage, config))
    assert report.scanned == 1
    assert report.captured == 1


def test_import_carries_source_when_present(storage, config):
    class _WithSource(_FakeHistoryBackend):
        def read_windows(self, limit: int = 30):
            result = CaptureResult(text="带来源的内容")
            result.source = FocusInfo(app_id="chrome.exe", window_title="某页面")
            return [result]

    import_native_history(_WithSource(), _collector(storage, config))
    item = storage.list(limit=1)[0]
    assert item.source_app == "chrome.exe"
    assert item.source_title == "某页面"


# --------------------------------------------------------------- 隐私不可绕过


def test_import_blocks_private_keys(storage, config):
    """系统历史里的私钥必须和实时复制的私钥一样被拦下。

    这是本模块存在的意义所在：导入走的是同一条 Collector 管线。
    若哪天有人为了「快一点」直接往 storage.add() 里塞，
    这个用例就该红。
    """
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
    backend = _FakeHistoryBackend([pem, "普通内容"])
    report = import_native_history(backend, _collector(storage, config))

    assert report.captured == 1
    assert report.skipped_privacy == 1
    contents = [item.content for item in storage.list(limit=10)]
    assert pem not in contents


def test_import_blocks_api_keys(storage, config):
    """敏感内容默认不落盘：连系统历史捞回来的也不进。"""
    backend = _FakeHistoryBackend(["password=SuperSecret123"])
    report = import_native_history(backend, _collector(storage, config))
    assert report.skipped_privacy == 1
    assert report.captured == 0


def test_import_reports_blocked_sample_preview(storage, config):
    """给出可读预览，方便用户判断「是不是误拦了」。"""
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
    backend = _FakeHistoryBackend([pem])
    report = import_native_history(backend, _collector(storage, config))
    assert report.blocked_samples
    assert "PRIVATE KEY" in report.blocked_samples[0]


def test_import_blocked_preview_is_truncated(storage, config):
    """预览要截断：别把整段密钥又打印到终端里 —— 拦下来又打出来等于没拦。"""
    pem = "-----BEGIN RSA PRIVATE KEY-----\n" + ("A" * 500) + "\n-----END RSA PRIVATE KEY-----"
    backend = _FakeHistoryBackend([pem])
    report = import_native_history(backend, _collector(storage, config))
    assert len(report.blocked_samples[0]) <= 48
    # 但整段密钥绝不能被完整回显
    assert "AAAA" * 10 not in report.blocked_samples[0]


# --------------------------------------------------------------- 不支持与失败


def test_unsupported_backend_reports_reason(storage, config):
    backend = _FakeHistoryBackend([], supported=False)
    report = import_native_history(backend, _collector(storage, config))

    assert report.supported is False
    assert "Windows" in report.unsupported_reason
    assert backend.calls == 0


def test_backend_without_reader_reports_reason(storage, config):
    class _Bare:
        name = "null"

    report = import_native_history(_Bare(), _collector(storage, config))
    assert report.supported is False
    assert "不提供" in report.unsupported_reason


def test_reader_exception_is_reported_not_raised(storage, config):
    """导入是增强能力：读历史炸了不该把命令带崩。"""
    backend = _FakeHistoryBackend([], raises=OSError("历史服务没起来"))
    report = import_native_history(backend, _collector(storage, config))
    assert report.supported is False
    assert "失败" in report.unsupported_reason


def test_support_check_exception_is_contained(storage, config):
    class _Exploding(_FakeHistoryBackend):
        def is_history_supported(self):
            raise RuntimeError("探测炸了")

    report = import_native_history(_Exploding(), _collector(storage, config))
    assert report.supported is False


def test_report_summary_is_human_readable(storage, config):
    backend = _FakeHistoryBackend(["a", "b"])
    report = import_native_history(backend, _collector(storage, config))
    summary = report.summary()
    assert "扫描 2" in summary
    assert "导入 2" in summary


def test_report_summary_when_unsupported(storage, config):
    report = import_native_history(
        _FakeHistoryBackend([], supported=False), _collector(storage, config)
    )
    assert report.summary() == report.unsupported_reason


# --------------------------------------------------------------- 边角


def test_report_summary_lists_all_categories(storage, config):
    """各分类都要出现在摘要里，用户才知道少掉的去哪了。"""
    from linglong_clip.core.native_history import ImportReport

    report = ImportReport(scanned=10, captured=4, merged=3, skipped_privacy=2, skipped_other=1)
    summary = report.summary()
    assert "已存在 3" in summary
    assert "隐私拦截 2" in summary
    assert "跳过 1" in summary


def test_report_imported_excludes_merged(storage, config):
    """合并的算「已存在」，不计入本次导入。"""
    from linglong_clip.core.native_history import ImportReport

    report = ImportReport(captured=3, merged=5)
    assert report.imported == 3


def test_import_counts_other_skips(storage, config):
    """被类型开关挡下的（非隐私原因）计入 skipped_other。"""
    config.set("capture.types", ["link"])
    backend = _FakeHistoryBackend(["纯文本内容", "https://cnb.cool/x"])
    report = import_native_history(backend, _collector(storage, config))

    assert report.captured == 1
    assert report.skipped_other == 1
    assert report.skipped_privacy == 0


def test_support_check_defaults_true_for_legacy_backend(storage, config):
    """老式后端只有 read_windows()、没有能力探测，交给读取本身收敛。"""

    class _Legacy:
        name = "windows"

        def __init__(self):
            self.calls = 0

        def read_windows(self, limit=30):
            self.calls += 1
            return [CaptureResult(kind="text", text="读到内容")]

    backend = _Legacy()
    report = import_native_history(backend, _collector(storage, config))
    assert report.supported is True
    assert report.captured == 1
    assert backend.calls == 1


def test_unsupported_hint_for_non_windows():
    from linglong_clip.core.native_history import _unsupported_hint

    class _Linux:
        name = "linux"

    hint = _unsupported_hint(_Linux())
    assert "linux" in hint
    assert "Windows" not in hint
