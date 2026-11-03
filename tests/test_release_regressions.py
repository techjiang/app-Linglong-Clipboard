"""回归测试：钉住 1.0 发布前修掉的一批行为错误。

每条用例对应一个实测复现过的现象，注释里写清「当时是什么表现」，
避免以后有人把修复当成冗余代码删掉。
"""

from __future__ import annotations

import time

from linglong_clip.core import classify
from linglong_clip.core.collector import CAPTURED, Collector
from linglong_clip.core.models import ClipboardItem, content_hash
from linglong_clip.core.privacy import PrivacyGuard
from linglong_clip.core.semantic import SemanticEngine, parse_intent


def _item(content: str, kind: str = "text", **kwargs) -> ClipboardItem:
    return ClipboardItem(
        kind=kind, content=content, hash=content_hash(kind, content or "x"), **kwargs
    )


# ---- 隐私：弱敏感规则不该把内容整条吃掉 ----


def test_email_only_is_not_blocking():
    """现象：复制带邮箱的会议纪要，记录直接从历史里消失。

    邮箱/手机号属于「内容里有个人信息」，不是「这条内容本身是秘密」。
    """
    assert classify.detect_sensitive("联系 alice@example.com 讨论方案") == ["邮箱"]
    assert classify.is_blocking_sensitivity(["邮箱"]) is False


def test_phone_only_is_not_blocking():
    assert classify.is_blocking_sensitivity(classify.detect_sensitive("电话 13800138000")) is False


def test_api_key_is_blocking():
    hits = classify.detect_sensitive("token=ghp_abcdefghijklmnopqrstuvwxyz0123")
    assert hits
    assert classify.is_blocking_sensitivity(hits) is True


def test_card_number_needs_financial_context():
    """现象：复制 16 位订单号被当成银行卡，内容不进历史。"""
    assert "银行卡" not in classify.detect_sensitive("订单号 1234567890123456 已发货")
    assert "银行卡" in classify.detect_sensitive("请把款打到银行卡 6222 0212 3456 7890")


def test_analyze_keeps_kind_for_weak_hits():
    assert classify.analyze("联系 alice@example.com 讨论方案")["kind"] == "text"


def test_guard_marks_but_allows_weak_sensitivity(config):
    guard = PrivacyGuard(config)
    verdict = guard.inspect(_item("联系 alice@example.com"))
    assert verdict.allowed is True
    assert verdict.sensitive is False
    assert "邮箱" in verdict.reasons


def test_collector_keeps_email_text(storage, config):
    collector = Collector(storage, config, PrivacyGuard(config))
    outcome, saved = collector.process(_item("联系 alice@example.com 讨论方案"))
    assert outcome == CAPTURED
    assert saved is not None


# ---- 采集：复制了却没记 ----


def test_json_snippet_is_code():
    """现象：复制一段接口返回的 JSON，历史里显示成「文本」，
    右键「转换为…」里连「格式化 JSON」都不出现。"""
    assert classify.detect_kind('{"a":1,"b":[2,3]}') == "code"
    assert classify.detect_kind("[1, 2, 3]") == "code"


def test_plain_scalars_are_not_code():
    """`"abc"` / `42` / `true` 在 Python 里也是合法 JSON，但不能当代码。"""
    assert classify.detect_kind('"just a string"') != "code"
    assert classify.detect_kind("42") != "code"
    assert classify.looks_like_json("[1,2,") is False


def test_default_types_include_code():
    """现象：复制一段函数不报错、不入库，用户只能干瞪眼。"""
    from linglong_clip.utils.config import DEFAULTS

    assert "code" in DEFAULTS["capture"]["types"]


def test_code_snippet_is_captured(storage, config):
    collector = Collector(storage, config, PrivacyGuard(config))
    outcome, saved = collector.process(_item("def f(x):\n    return x * 2"))
    assert outcome == CAPTURED
    assert saved.kind == "code"


# ---- 检索 ----


def test_exclude_kind_is_parsed():
    """现象：「不要图片」被解析成「只要图片」，结果正好相反。"""
    intent = parse_intent("不要图片")
    assert intent.exclude_kinds == ["image"]
    assert intent.kinds == []


def test_exclude_kind_filters_results(storage):
    storage.add(_item("https://example.com/a", kind="link"))
    storage.add(_item("一段普通文本内容"))
    results = SemanticEngine(storage).search("不要链接")
    assert all(item.kind != "link" for item in results)
    assert results


def test_until_bound_excludes_later_items(storage):
    """现象：搜「昨天」把今天的记录也一起带出来了。

    `intent.until` 被解析出来却没用上，上界形同虚设。
    """
    now = time.time()
    old = storage.add(_item("昨天的记录", created_at=now - 86400 * 1.2))
    fresh = storage.add(_item("今天的记录", created_at=now))

    ids = [item.id for item in SemanticEngine(storage).search("昨天", limit=20)]
    assert old.id in ids
    assert fresh.id not in ids


# ---- 存储排序 ----


def test_order_uses_max_of_used_and_created(storage):
    """现象：一条曾被复制过的旧记录，长期压在新记录前面。

    旧实现用 `COALESCE(NULLIF(used_at,0), created_at)`：只要 used_at 有值，
    就完全忽略 created_at —— 哪怕这条记录比旁边的旧好几天。
    """
    now = time.time()
    stale = storage.add(
        _item("很久以前用过", created_at=now - 86400 * 5, used_at=now - 86400 * 3)
    )
    fresh = storage.add(_item("刚复制", created_at=now))

    order = [item.id for item in storage.list(limit=10)]
    assert order.index(fresh.id) < order.index(stale.id)
