"""配置项审计：找出「声明了但没有读取方」的空转配置。

这类缺陷此前反复出现：设置页有勾选框、DEFAULTS 有默认值，
但代码里没有任何读取方。测不出行为（没有读取方就没有可断言的行为），
所以用静态审计兜住。
"""

from __future__ import annotations

import contextlib

import pytest

from linglong_clip.utils import config_audit
from linglong_clip.utils.config_audit import (
    audit_config_usage,
    default_keys,
    format_audit,
)


def audit_fake(pkg, defaults):
    """在假源码树上跑审计：显式传入根目录与键列表。"""
    return audit_config_usage(root=pkg, keys=defaults)


def test_current_config_has_no_dead_keys():
    """当前代码库里不应有空转配置。

    这是本轮修掉的 5 个开关的回归守卫：谁能再往 DEFAULTS 里加一个
    没人读的键，这条用例会直接失败。
    """
    report = audit_config_usage()
    assert report.unread == [], f"发现空转配置：{report.unread}"


def test_default_keys_are_flat_dotted_paths():
    keys = default_keys()
    assert "capture.enabled" in keys
    assert "ui.card_density" in keys
    assert "meta.schema_version" in keys
    # 不应出现把整个 dict 当叶子键的情况
    assert "capture" not in keys
    assert "sync" not in keys


def test_detects_declared_but_unread_key(tmp_path):
    """造一个只有定义、没有读取方的假包，审计必须报出来。"""
    pkg = tmp_path / "fakeclip"
    pkg.mkdir()
    (pkg / "consumer.py").write_text(
        "def go(config):\n    return config.get('a.used')\n", encoding="utf-8"
    )
    report = audit_fake(pkg, ["a.used", "a.unused"])
    assert "a.unused" in report.unread
    assert "a.used" not in report.unread


def test_settings_dialog_is_not_a_reader(tmp_path):
    """设置页只是把值写回配置，写回去不等于会生效，不能算读取方。"""
    pkg = tmp_path / "fakeclip"
    (pkg / "ui").mkdir(parents=True)
    (pkg / "ui" / "settings_dialog.py").write_text(
        "def save(config):\n    config.set('x.only_written', True)\n", encoding="utf-8"
    )
    report = audit_fake(pkg, ["x.only_written"])
    assert "x.only_written" in report.unread


def test_explicit_prefix_on_parent_section_counts_as_read(tmp_path):
    """子键被拼出来时（`"ocr." + name`），父段的字面量出现即算读到。

    判据刻意放宽一级：严格按叶子键匹配会把 `config.get("ocr." + name)`
    这类动态读取误报成空转。
    """
    pkg = tmp_path / "fakeclip"
    pkg.mkdir()
    (pkg / "consumer.py").write_text(
        "def go(config, name):\n"
        "    section = config.get('ocr')\n"
        "    return (section or {}).get(name, {})\n",
        encoding="utf-8",
    )
    report = audit_fake(pkg, ["ocr.enabled", "ocr.langs"])
    assert report.unread == []


def test_sibling_key_does_not_excuse_missing_reader(tmp_path):
    """同一段里别的键被读了，不代表这个键有人读。

    这是最常见的空转形态（一段配置里只接了其中一个），
    判据放宽到这里就停，不能把整段都放过。
    """
    pkg = tmp_path / "fakeclip"
    pkg.mkdir()
    (pkg / "consumer.py").write_text(
        "def go(config):\n    return config.get('ocr.enabled')\n", encoding="utf-8"
    )
    report = audit_fake(pkg, ["ocr.enabled", "ocr.min_confidence"])
    assert report.unread == ["ocr.min_confidence"]


def test_self_consumed_meta_keys_are_exempt(tmp_path):
    """`meta.*` 由配置模块自己消费（迁移流程），不要求业务代码读它。"""
    pkg = tmp_path / "fakeclip"
    pkg.mkdir()
    report = audit_fake(pkg, ["meta.schema_version"])
    assert report.unread == []


def test_report_serializable_and_summary():
    report = audit_config_usage()
    payload = report.to_dict()
    assert isinstance(payload["unread"], list)
    assert payload["total"] == payload["read_count"] + len(payload["unread"])
    assert "空转配置" in report.summary() or "全部有读取方" in report.summary()
    assert "配置项审计" in format_audit(report)


def test_unhealthy_report_lists_keys():
    report = config_audit.AuditReport(unread=["ui.fake"], read_count=5)
    assert report.healthy is False
    text = config_audit.format_audit(report)
    assert "ui.fake" in text
    assert "✗" in text

