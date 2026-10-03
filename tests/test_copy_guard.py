"""出站防护：敏感内容复制到系统剪贴板时的处理。

存在的理由：工具此前的防护全部在**入库方向**（采集拦截、不同步、
限时从历史清除），而真正把密钥带出机器的动作是**复制出站**。系统剪贴板
有两个我们控制不了的下游 —— 系统剪贴板历史与剪贴板长期驻留 —— 两者都
只能在写入时声明或安排。

所以这里验的是：判定对不对、写的时候有没有带上标记、清空的时机与条件
对不对，以及**不该动的时候有没有乱动**。
"""

from __future__ import annotations

import time

from linglong_clip.core.copy_guard import (
    MIN_AUTO_CLEAR_SECONDS,
    ClipboardReleaser,
    OutboundPlan,
    plan_outbound,
)


# ------------------------------------------------------------- 出站判定


def test_normal_content_is_not_touched():
    """普通内容不该被特殊对待 —— 系统历史正是用户找回「刚复制的链接」的地方。"""
    plan = plan_outbound(sensitive=False, auto_clear_seconds=30)
    assert plan.is_protected is False
    assert plan.exclude_from_history is False
    assert plan.auto_clear_seconds == 0


def test_sensitive_content_always_excluded_from_history():
    """排除标记不给配置机会：加了默认关掉的开关等于没做。"""
    plan = plan_outbound(sensitive=True, auto_clear_seconds=0)
    assert plan.exclude_from_history is True
    assert plan.auto_clear_seconds == 0
    assert plan.reasons


def test_sensitive_content_schedules_clear():
    plan = plan_outbound(sensitive=True, auto_clear_seconds=45)
    assert plan.exclude_from_history is True
    assert plan.auto_clear_seconds == 45


def test_redacted_content_still_excluded():
    """脱敏是正则匹配，漏一处就漏一处；排除标记是系统级保证，兜底更可靠。"""
    plan = plan_outbound(sensitive=True, auto_clear_seconds=30, redacted=True)
    assert plan.exclude_from_history is True
    assert any("脱敏" in reason for reason in plan.reasons)


def test_too_short_clear_is_treated_as_disabled():
    """低于下限时不清空 —— 复制完还没粘贴内容就没了，没人想要这个行为。"""
    plan = plan_outbound(sensitive=True, auto_clear_seconds=MIN_AUTO_CLEAR_SECONDS - 1)
    assert plan.auto_clear_seconds == 0
    # 但排除标记仍要挂上，两件事互不绑定
    assert plan.exclude_from_history is True


def test_clear_at_the_lower_bound_is_kept():
    plan = plan_outbound(sensitive=True, auto_clear_seconds=MIN_AUTO_CLEAR_SECONDS)
    assert plan.auto_clear_seconds == MIN_AUTO_CLEAR_SECONDS


def test_negative_seconds_do_not_become_negative_timer():
    plan = plan_outbound(sensitive=True, auto_clear_seconds=-5)
    assert plan.auto_clear_seconds == 0


def test_zero_means_do_not_clear_not_clear_now():
    """`0` 是「不清空」，不是「立刻清空」—— 后者会让粘贴必然失败。"""
    plan = plan_outbound(sensitive=True, auto_clear_seconds=0)
    assert plan.auto_clear_seconds == 0


def test_none_seconds_handled():
    plan = plan_outbound(sensitive=True, auto_clear_seconds=None)
    assert plan.auto_clear_seconds == 0


def test_plan_is_protected_when_only_exclusion():
    plan = OutboundPlan(exclude_from_history=True)
    assert plan.is_protected is True


# ------------------------------------------------------------- 限时清空


class _FakeClipboard:
    """假剪贴板：内容可读可写，记录清空次数。"""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.cleared = 0
        self.read_failures = 0

    def read(self) -> str:
        if self.read_failures > 0:
            self.read_failures -= 1
            raise OSError("剪贴板读不了")
        return self.text

    def clear(self) -> None:
        self.cleared += 1
        self.text = ""


def _releaser(clip: _FakeClipboard) -> ClipboardReleaser:
    return ClipboardReleaser(read_text=clip.read, clear=clip.clear)


def test_clear_happens_when_content_unchanged():
    clip = _FakeClipboard("secret")
    releaser = _releaser(clip)
    assert releaser.schedule("secret", 1) is True
    time.sleep(1.3)
    assert clip.cleared == 1


def test_clear_does_not_delete_user_copy():
    """用户复制完密钥马上又复制了别的东西，清理不能把那条抹掉。

    这是「无条件清空」与「带条件清空」的分界，也是最该守住的一条。
    """
    clip = _FakeClipboard("secret")
    releaser = _releaser(clip)
    releaser.schedule("secret", 1)
    clip.text = "用户后来的复制"
    time.sleep(1.3)
    assert clip.cleared == 0
    assert clip.text == "用户后来的复制"


def test_schedule_returns_false_for_zero():
    clip = _FakeClipboard("x")
    releaser = _releaser(clip)
    assert releaser.schedule("x", 0) is False
    assert releaser.pending() == 0


def test_read_failure_skips_clear_instead_of_clearing():
    """读不到就別清 —— 宁可留着，也不能误删用户的东西。"""
    clip = _FakeClipboard("secret")
    clip.read_failures = 1
    releaser = _releaser(clip)
    releaser.schedule("secret", 1)
    time.sleep(1.3)
    assert clip.cleared == 0


def test_clear_failure_is_contained():
    clip = _FakeClipboard("secret")

    def _boom():
        raise OSError("清不了")

    releaser = ClipboardReleaser(read_text=clip.read, clear=_boom)
    releaser.schedule("secret", 1)
    time.sleep(1.3)  # 不应抛到线程外


def test_pending_list_prunes_finished_timers():
    """长期运行不能因为定时器列表无限增长而漏内存。"""
    clip = _FakeClipboard("x")
    releaser = _releaser(clip)
    for _ in range(5):
        releaser.schedule("x", 0.05)
    time.sleep(0.2)  # 全部结束
    # 再排一次会顺手清掉已结束的，只剩这一个
    releaser.schedule("x", 5)
    assert releaser.pending() == 1


def test_pending_counts_still_running():
    clip = _FakeClipboard("x")
    releaser = _releaser(clip)
    releaser.schedule("x", 5)
    releaser.schedule("x", 5)
    assert releaser.pending() == 2


def test_clear_seconds_accepts_float():
    """`Timer` 接受小数秒；判定侧不能用 `int` 把它截成 0 之后当成「不清空」。"""
    clip = _FakeClipboard("secret")
    releaser = _releaser(clip)
    assert releaser.schedule("secret", 0.05) is True
    time.sleep(0.25)
    assert clip.cleared == 1


def test_cancel_all_stops_pending_clears():
    clip = _FakeClipboard("secret")
    releaser = _releaser(clip)
    releaser.schedule("secret", 1)
    assert releaser.pending() == 1
    releaser.cancel_all()
    assert releaser.pending() == 0
    time.sleep(1.3)
    assert clip.cleared == 0


def test_schedule_with_empty_expected_text():
    """空指纹也要能走通：比对的就是「还是不是我写的那份」。"""
    clip = _FakeClipboard("")
    releaser = _releaser(clip)
    assert releaser.schedule("", 1) is True
    time.sleep(1.3)
    assert clip.cleared == 1


# ------------------------------------------------- 与平台/配置的接线


def test_base_backend_has_no_exclusion_formats():
    from linglong_clip.platform.base import ClipboardBackend

    assert ClipboardBackend().history_exclusion_formats() == []


def test_null_backend_has_no_exclusion_formats():
    from linglong_clip.platform import NullBackend

    assert NullBackend().history_exclusion_formats() == []


def test_windows_declares_exclusion_format():
    """Windows 必须声明排除格式 —— README 里「让敏感内容不进系统历史」
    靠的就是它。此前这个方法根本不存在，声明无处可查。
    """
    from linglong_clip.platform import windows as win_module

    backend = win_module.WindowsBackend.__new__(win_module.WindowsBackend)
    backend._timers = []
    assert win_module.CLIPBOARD_FORMAT_EXCLUDE_MONITOR in backend.history_exclusion_formats()


def test_macos_reports_no_exclusion_format():
    """macOS 没有等价机制，必须如实返回空 —— 不能声明做不到的事。"""
    from linglong_clip.platform.macos import MacOSBackend

    assert MacOSBackend().history_exclusion_formats() == []


def test_linux_reports_no_exclusion_format():
    from linglong_clip.platform.linux import LinuxBackend

    assert LinuxBackend().history_exclusion_formats() == []


# ------------------------------------------------- Qt 写回路径（真实 QClipboard）


class _BackendWithExclusion:
    name = "fake-win"

    def history_exclusion_formats(self):
        return ["ExcludeClipboardContentFromMonitorProcessing"]


class _BackendWithoutExclusion:
    name = "fake-plain"


class _BackendExploding:
    name = "fake-broken"

    def history_exclusion_formats(self):
        raise RuntimeError("探测炸了")


def _adapter(qt_app, tmp_path, backend):
    from linglong_clip.ui.clipboard_qt import QtClipboardAdapter

    return QtClipboardAdapter(qt_app.clipboard(), tmp_path / "images", backend)


def test_write_item_attaches_exclusion_format(qt_app, tmp_path):
    """排除标记必须落在剪贴板数据上 —— 只调后端不算生效。

    这件事只能在写入时做：内容进了系统历史就取不回来。
    """
    from linglong_clip.core.models import ClipboardItem

    adapter = _adapter(qt_app, tmp_path, _BackendWithExclusion())
    ok = adapter.write_item(
        ClipboardItem(kind="text", content="secret"),
        exclude_from_history=True,
    )
    assert ok is True
    formats = qt_app.clipboard().mimeData().formats()
    assert "ExcludeClipboardContentFromMonitorProcessing" in formats
    assert qt_app.clipboard().text() == "secret"


def test_write_item_without_flag_leaves_no_format(qt_app, tmp_path):
    from linglong_clip.core.models import ClipboardItem

    adapter = _adapter(qt_app, tmp_path, _BackendWithExclusion())
    adapter.write_item(ClipboardItem(kind="text", content="plain"))
    formats = qt_app.clipboard().mimeData().formats()
    assert "ExcludeClipboardContentFromMonitorProcessing" not in formats


def test_write_item_degrades_on_platform_without_mechanism(qt_app, tmp_path):
    """没有机制的平台仍要正常写入：标记缺位不能变成「写不进去」。"""
    from linglong_clip.core.models import ClipboardItem

    adapter = _adapter(qt_app, tmp_path, _BackendWithoutExclusion())
    assert adapter.write_item(
        ClipboardItem(kind="text", content="secret"), exclude_from_history=True
    )
    assert qt_app.clipboard().text() == "secret"
    assert qt_app.clipboard().mimeData().formats() == ["text/plain"]


def test_write_item_survives_exclusion_probe_failure(qt_app, tmp_path):
    from linglong_clip.core.models import ClipboardItem

    adapter = _adapter(qt_app, tmp_path, _BackendExploding())
    assert adapter.write_item(
        ClipboardItem(kind="text", content="secret"), exclude_from_history=True
    )
    assert qt_app.clipboard().text() == "secret"


def test_exclusion_format_carries_no_data(qt_app, tmp_path):
    """格式的值必须为空。

    系统只检查格式在不在；塞入真实数据反而会变成一块内容，
    被历史当成条目存下来 —— 与目的相反。
    """
    adapter = _adapter(qt_app, tmp_path, _BackendWithExclusion())
    adapter.set_text("secret", exclude_from_history=True)
    data = qt_app.clipboard().mimeData().data("ExcludeClipboardContentFromMonitorProcessing")
    assert bytes(data) == b""


def test_set_text_with_exclusion_keeps_text(qt_app, tmp_path):
    adapter = _adapter(qt_app, tmp_path, _BackendWithExclusion())
    adapter.set_text("hello", exclude_from_history=True)
    mime = qt_app.clipboard().mimeData()
    assert mime.text() == "hello"
    assert "ExcludeClipboardContentFromMonitorProcessing" in mime.formats()


def test_set_text_without_flag_uses_plain_path(qt_app, tmp_path):
    adapter = _adapter(qt_app, tmp_path, _BackendWithExclusion())
    adapter.set_text("hello")
    assert qt_app.clipboard().mimeData().formats() == ["text/plain"]


def test_adapter_without_backend_degrades(qt_app, tmp_path):
    """后端为 None（Qt 层降级）时也不能崩、不能写不进去。"""
    adapter = _adapter(qt_app, tmp_path, None)
    adapter.set_text("hello", exclude_from_history=True)
    assert qt_app.clipboard().text() == "hello"


def test_list_containing_empty_format_names_is_skipped(qt_app, tmp_path):
    class _BackendWithBlanks:
        name = "blanks"

        def history_exclusion_formats(self):
            return ["", None, "RealFormat"]

    adapter = _adapter(qt_app, tmp_path, _BackendWithBlanks())
    adapter.set_text("x", exclude_from_history=True)
    formats = qt_app.clipboard().mimeData().formats()
    assert "RealFormat" in formats
    assert "" not in formats
