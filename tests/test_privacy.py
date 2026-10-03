"""隐私策略：来源过滤、脱敏、敏感判定。"""

import pytest

from linglong_clip.core.models import ClipboardItem
from linglong_clip.core.privacy import PrivacyGuard


@pytest.fixture()
def guard(config):
    return PrivacyGuard(config)


def test_private_key_is_refused(guard):
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
    verdict = guard.inspect(ClipboardItem(kind="text", content=text))
    assert verdict.allowed is False
    assert "私钥" in verdict.summary


def test_password_manager_source_is_blocked(guard):
    item = ClipboardItem(kind="text", content="hunter2", source_app="1Password.exe")
    assert guard.inspect(item).allowed is False


def test_blocked_apps_from_config(config):
    config.set("privacy.blocked_apps", ["banking-app", "vault"])
    guard = PrivacyGuard(config)
    assert guard.is_blocked_source("my-banking-app") is True
    assert guard.is_blocked_source("firefox") is False


def test_sensitive_content_is_flagged_but_allowed(guard):
    item = ClipboardItem(kind="text", content="token = abcdefghijklmnop")
    verdict = guard.inspect(item)
    assert verdict.allowed is True
    assert verdict.sensitive is True


def test_ordinary_content_passes_clean(guard):
    verdict = guard.inspect(ClipboardItem(kind="text", content="今天天气不错"))
    assert verdict.allowed is True
    assert verdict.sensitive is False
    assert verdict.summary == "无风险"


def test_redact_replaces_value_keeps_field_name(guard):
    result = guard.redact("password: hunter2xyz")
    assert "hunter2xyz" not in result
    assert "password" in result
    assert "已脱敏" in result


def test_redact_multiple_kinds(guard):
    text = "sk-abcdefghijklmnopqrstuvwxyz12 and card 6222021234567890123"
    result = guard.redact(text)
    assert "sk-abcdefghijklmnopqrstuvwxyz12" not in result
    assert "6222021234567890123" not in result


def test_redact_leaves_clean_text_untouched(guard):
    assert guard.redact("普通文本") == "普通文本"


def test_custom_redact_rule(config):
    config.set("privacy.custom_redact_rules", {"工号": r"EMP\d{6}"})
    guard = PrivacyGuard(config)
    assert "EMP123456" not in guard.redact("工号 EMP123456 的记录")


def test_invalid_custom_rule_is_skipped(config):
    config.set("privacy.custom_redact_rules", {"坏规则": "([unclosed"})
    guard = PrivacyGuard(config)  # 不应抛出
    assert guard.redact("文本") == "文本"


def test_scan_counts_hits(guard):
    found = guard.scan("token=abcdefghij and sk-abcdefghijklmnopqrstuvwxyz1")
    assert found.get("API Key") == 1
    assert found.get("密码字段") == 1


def test_should_keep_sensitive_follows_ttl(config):
    guard = PrivacyGuard(config)
    assert guard.should_keep_sensitive() is False  # 默认不保留
    config.set("privacy.sensitive_retention_seconds", 300)
    assert guard.should_keep_sensitive() is True


def test_reload_rules_picks_up_config_changes(config):
    guard = PrivacyGuard(config)
    assert guard.redact("EMP999999") == "EMP999999"
    config.set("privacy.custom_redact_rules", {"工号": r"EMP\d{6}"})
    guard.reload_rules()
    assert "EMP999999" not in guard.redact("EMP999999")
