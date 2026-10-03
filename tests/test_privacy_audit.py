"""隐私体检：脱敏规则的「会不会生效」判定与样本生成。"""

import pytest

from linglong_clip.core.privacy import PrivacyGuard
from linglong_clip.core.privacy_audit import (
    _literal_sample,
    audit_privacy,
    format_audit,
)


@pytest.fixture()
def guard(config):
    return PrivacyGuard(config)


# ---- 样本生成 ----


@pytest.mark.parametrize(
    "pattern,expected_hit",
    [
        (r"EMP\d{6}", True),
        (r"TICKET-[A-Z]{2}-\d{4}", True),
        (r"[0-9]{3}-[0-9]{4}", True),
        (r"(?i)secret", True),
        (r"(?i)(password)(\S{6,})", True),
        (r"-----BEGIN [^-]*PRIVATE KEY-----", True),
    ],
)
def test_literal_sample_actually_matches(pattern, expected_hit):
    """拼出来的样本必须真的能命中自己的正则。

    这条是整个体检功能的地基：样本错了，报告就会把对的说成错的。
    所以对每个样本再验一遍 —— 生成与验证都走同一份正则。
    """
    import re

    sample = _literal_sample(pattern)
    assert sample, f"{pattern!r} 应能拼出样本"
    assert bool(re.search(pattern, sample)) is expected_hit


def test_literal_sample_gives_up_on_lookahead():
    """拼不出来的正则应返回空串，而不是硬凑一个必然失败的样本。"""
    assert _literal_sample(r"(?=abc)xyz") == ""


def test_literal_sample_survives_garbage():
    """非法正则不该抛异常 —— 体检本身不能因为一条坏规则崩掉。"""
    assert _literal_sample("[unclosed") == ""


# ---- 体检结论 ----


def test_builtin_rules_all_hit_their_samples(config):
    report = audit_privacy(config)
    builtin = [check for check in report.checks if not check.custom]
    assert builtin
    assert all(check.valid for check in builtin)
    assert all(check.hits > 0 for check in builtin)


def test_custom_rule_that_matches_is_healthy(config):
    config.set("privacy.custom_redact_rules", {"员工号": r"EMP\d{6}"})
    report = audit_privacy(config)
    custom = [check for check in report.checks if check.custom]
    assert len(custom) == 1
    assert custom[0].will_run
    assert custom[0].hits == 1


def test_invalid_custom_rule_is_reported_not_dropped(config):
    """正则写错的规则必须出现在报告里。

    `PrivacyGuard` 加载时会把编译失败的规则静默跳过，只从 Guard 取规则
    就会漏报 —— 而这正是用户最需要知道的一条。
    """
    config.set("privacy.custom_redact_rules", {"写错的": "[unclosed"})
    report = audit_privacy(config)
    custom = [check for check in report.checks if check.custom]
    assert len(custom) == 1
    assert custom[0].valid is False
    assert custom[0].will_run is False
    assert "无法编译" in custom[0].problems[0]
    assert report.healthy is False


def test_invalid_rule_does_not_break_other_rules(config):
    config.set("privacy.custom_redact_rules", {"坏": "[unclosed", "好": r"EMP\d{6}"})
    report = audit_privacy(config)
    by_name = {check.name: check for check in report.checks}
    assert by_name["好"].will_run
    assert not by_name["坏"].will_run


def test_rule_without_probe_is_not_a_problem(config):
    """没有样本可验只是「自动体检帮不上忙」，不能报成规则有问题。"""
    config.set("privacy.custom_redact_rules", {"前瞻": "(?=abc)xyz"})
    report = audit_privacy(config)
    custom = [check for check in report.checks if check.custom][0]
    assert custom.will_run is True
    assert custom.note
    assert report.healthy is True


def test_redact_disabled_is_noted(config):
    config.set("privacy.redact_on_copy_out", False)
    report = audit_privacy(config)
    assert report.redact_enabled is False
    assert any("不会执行" in note for note in report.notes)


def test_sensitive_retention_is_noted(config):
    config.set("privacy.sensitive_retention_seconds", 300)
    report = audit_privacy(config)
    assert report.sensitive_retained is True
    assert any("限时保留" in note or "记录到历史" in note for note in report.notes)


def test_report_serializes(config):
    config.set("privacy.custom_redact_rules", {"坏": "[unclosed"})
    payload = audit_privacy(config).to_dict()
    assert payload["healthy"] is False
    assert payload["custom_rules"] == 1
    assert any(item["valid"] is False for item in payload["checks"])


def test_format_audit_marks_broken_rules(config):
    config.set("privacy.custom_redact_rules", {"坏": "[unclosed"})
    text = format_audit(audit_privacy(config))
    assert "✗ 坏" in text
    assert "需要处理" in text


def test_format_audit_without_rules(config, monkeypatch):
    """内置规则清空时（理论上不该发生）也不该崩，给出明确一句。"""
    import linglong_clip.core.privacy_audit as module

    monkeypatch.setattr(module, "BUILTIN_REDACT_RULES", {})
    report = audit_privacy(config)
    assert report.checks == []
    assert "没有任何脱敏规则" in format_audit(report)


def test_audit_uses_guard_rules(config):
    """体检对象是「实际装上的规则」，与 Guard 加载结果一致。"""
    config.set("privacy.custom_redact_rules", {"员工号": r"EMP\d{6}"})
    guard = PrivacyGuard(config)
    report = audit_privacy(config, guard=guard)
    names = {check.name for check in report.checks}
    assert "员工号" in names
    assert names == set(guard._rules) | {"员工号"}
