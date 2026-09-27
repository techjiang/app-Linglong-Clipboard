"""自动化：内置转换器与规则引擎。"""

import pytest

from linglong_clip.core.automation import (
    REGISTRY,
    TRIGGER_AUTO,
    AutomationEngine,
    Rule,
    TransformError,
    list_actions,
)
from linglong_clip.core.models import ClipboardItem


@pytest.fixture()
def engine():
    return AutomationEngine()


def _item(kind="text", content=""):
    return ClipboardItem(kind=kind, content=content, preview=content[:50])


@pytest.mark.parametrize(
    "key,source,expected_substring",
    [
        ("json.format", '{"b":1,"a":2}', '"b": 1'),
        ("json.minify", '{\n  "a": 1\n}', '{"a":1}'),
        ("url.decode", "a%20b%2Fc", "a b/c"),
        ("url.encode", "a b/c", "a%20b%2Fc"),
        ("lines.unique", "a\nb\na", "a\nb"),
        ("lines.sort", "c\na\nb", "a\nb\nc"),
        ("lines.number", "x\ny", "1 | x"),
        ("lines.trim", "a\n\n\nb", "a\nb"),
        ("case.snake", "helloWorld", "hello_world"),
        ("case.camel", "hello_world", "helloWorld"),
        ("case.pascal", "hello_world", "HelloWorld"),
        ("case.kebab", "hello_world", "hello-world"),
        ("extract.email", "联系 a@b.com 或 c@d.org", "a@b.com"),
        ("extract.url", "见 https://a.com 与 https://b.com", "https://a.com"),
        ("extract.number", "共 12 个，重 3.5kg", "12"),
        ("extract.ip", "网关 192.168.1.1", "192.168.1.1"),
        ("text.stats", "hello 世界", "字符数"),
        ("markdown.quote" if "markdown.quote" in REGISTRY else "text.quote", "引言", "> 引言"),
        ("json.to_yaml", '{"a":1}', "a: 1"),
        ("csv.to_json", "n,v\na,1", '"n": "a"'),
        ("csv.to_markdown", "a,b\n1,2", "| a | b |"),
        ("encode.base64", "hello", "aGVsbG8="),
        ("encode.base64_decode", "aGVsbG8=", "hello"),
        ("path.normalize", "/tmp//a/../b.txt", "/tmp/b.txt"),
    ],
)
def test_builtin_transforms(engine, key, source, expected_substring):
    result = engine.apply(key, _item(content=source))
    assert result.ok, result.error
    assert expected_substring in result.output


def test_json_format_rejects_invalid(engine):
    result = engine.apply("json.format", _item(content="not json"))
    assert result.ok is False
    assert "JSON" in result.error


def test_json_format_indent_param(engine):
    result = engine.apply("json.format", _item(content='{"a":1}'), indent=4)
    assert result.ok
    assert '    "a"' in result.output


def test_base64_decode_reports_bad_input(engine):
    result = engine.apply("encode.base64_decode", _item(content="hello world"))
    assert result.ok is False


def test_hash_transforms(engine):
    sha = engine.apply("encode.sha256", _item(content="abc"))
    assert sha.ok and len(sha.output) == 64
    md5 = engine.apply("encode.md5", _item(content="abc"))
    assert md5.ok and len(md5.output) == 32


def test_url_strip_tracking(engine):
    result = engine.apply(
        "url.strip_tracking", _item(kind="link", content="https://x.com/a?utm_source=t&id=5")
    )
    assert result.ok
    assert "utm_source" not in result.output
    assert "id=5" in result.output


def test_url_strip_tracking_noop_without_tracking_params(engine):
    result = engine.apply("url.strip_tracking", _item(kind="link", content="https://x.com/a?id=5"))
    assert result.ok is False
    assert "不适用" in result.error


def test_unknown_action(engine):
    result = engine.apply("does.not.exist", _item(content="x"))
    assert result.ok is False
    assert "未知动作" in result.error


def test_action_rejects_wrong_kind(engine):
    result = engine.apply("json.format", _item(kind="image", content="x"))
    assert result.ok is False
    assert "不适用" in result.error


def test_empty_content(engine):
    result = engine.apply("json.format", _item(content=""))
    assert result.ok is False
    assert "空" in result.error


def test_apply_chain(engine):
    result = engine.apply_chain(["json.format", "json.minify"], _item(content='{"a": 1}'))
    assert result.ok
    assert result.output == '{"a":1}'


def test_apply_chain_reports_failing_step(engine):
    result = engine.apply_chain(["json.format", "json.minify"], _item(content="bad"))
    assert result.ok is False
    assert "第 1 步" in result.error


def test_path_basename(engine):
    result = engine.apply("path.basename", _item(kind="file", content="/a/b/c.txt\n/d/e/f.log"))
    assert result.ok
    assert result.output == "c.txt\nf.log"


def test_shell_escape(engine):
    result = engine.apply("shell.escape", _item(content="it's here"))
    assert result.ok
    assert result.output.startswith("'") and result.output.endswith("'")


def test_rule_matching():
    rule = Rule(name="r", kinds=["code"], contains="def ", min_length=5, actions=["json.format"])
    assert rule.matches(_item(kind="code", content="def foo(): pass")) is True
    assert rule.matches(_item(kind="text", content="def foo(): pass")) is False
    assert rule.matches(_item(kind="code", content="x = 1")) is False
    # 长度下限是「大于等于」，所以 5 字符通过、4 字符不通过
    assert rule.matches(_item(kind="code", content="def a")) is True
    assert rule.matches(_item(kind="code", content="def ")) is False


def test_rule_pattern_matching():
    rule = Rule(name="r", pattern=r"^\s*[\{\[]", actions=["json.format"])
    assert rule.matches(_item(content='{"a":1}')) is True
    assert rule.matches(_item(content="plain")) is False


def test_rule_source_matching():
    rule = Rule(name="r", source_contains="chrome", actions=["url.strip_tracking"])
    item = ClipboardItem(kind="link", content="x", source_app="google-chrome")
    assert rule.matches(item) is True


def test_invalid_rule_pattern_does_not_match():
    rule = Rule(name="r", pattern="([unclosed", actions=["json.format"])
    assert rule.matches(_item(content="x")) is False


def test_rule_disabled():
    rule = Rule(name="r", enabled=False, actions=["json.format"])
    assert rule.matches(_item(content="x")) is False


def test_rule_roundtrip_dict():
    rule = Rule(name="r", kinds=["text"], actions=["json.format"], trigger=TRIGGER_AUTO)
    restored = Rule.from_dict(rule.to_dict())
    assert restored.name == rule.name
    assert restored.kinds == rule.kinds
    assert restored.trigger == TRIGGER_AUTO


def test_auto_actions_run_for_matching_rule(engine):
    engine.add_rule(
        Rule(name="fmt", kinds=["text"], pattern=r"^\s*[\{\[]", actions=["json.minify"],
             trigger=TRIGGER_AUTO)
    )
    results = engine.auto_actions_for(_item(content='{ "a" : 1 }'))
    assert len(results) == 1 and results[0].ok
    assert results[0].output == '{"a":1}'


def test_auto_actions_skip_non_matching(engine):
    engine.add_rule(
        Rule(name="fmt", kinds=["code"], actions=["json.minify"], trigger=TRIGGER_AUTO)
    )
    assert engine.auto_actions_for(_item(kind="text", content="x")) == []


def test_manual_rules_do_not_auto_run(engine):
    engine.add_rule(Rule(name="m", kinds=["text"], actions=["json.minify"]))
    assert engine.auto_actions_for(_item(content='{"a":1}')) == []


def test_load_rules_from_config(engine):
    count = engine.load_from_config(
        [{"name": "a", "actions": ["json.format"], "kinds": ["text"]}]
    )
    assert count == 1
    assert engine.rules()[0].name == "a"


def test_remove_rule(engine):
    engine.add_rule(Rule(name="x"))
    assert engine.remove_rule("x") is True
    assert engine.remove_rule("x") is False


def test_suggest_for_prefers_rule_actions(engine):
    engine.add_rule(Rule(name="r", kinds=["text"], actions=["extract.email"]))
    suggestions = engine.suggest_for(_item(content="a@b.com"))
    assert suggestions[0].key == "extract.email"


def test_list_actions_by_kind():
    assert len(list_actions("image")) == 0
    assert len(list_actions("text")) > 10
    assert len(list_actions()) >= 30


def test_all_registry_actions_have_metadata():
    for key, action in REGISTRY.items():
        assert action.key == key
        assert action.name
        assert action.category
        assert callable(action.transform)


# ---- 跟踪参数清理与路径归一 ----


@pytest.mark.parametrize(
    "key",
    ["ref", "fbclid", "gclid", "spm", "mc_cid", "igshid", "pk_campaign"],
)
def test_strip_tracking_covers_common_params(engine, key):
    """`ref` / `fbclid` 这类无前缀的参数曾因只做前缀匹配而漏掉。"""
    result = engine.apply("url.strip_tracking", _item(content=f"https://a.com/p?{key}=x&q=1", kind="link"))
    assert result.ok, result.error
    assert result.output == "https://a.com/p?q=1"


def test_strip_tracking_returns_none_when_nothing_to_strip(engine):
    """没有跟踪参数时返回 None，让上层提示「不适用于该转换」。"""
    result = engine.apply("url.strip_tracking", _item(content="https://a.com/p?q=1", kind="link"))
    assert not result.ok


def test_strip_tracking_shares_definition_with_dedupe(engine):
    """转换器与去重必须用同一份跟踪参数名单。

    两处各维护一份迟早分叉，用户会看到「转换说没有跟踪参数，
    去重却认为这是两条不同的链接」这种自相矛盾的结果。
    """
    from linglong_clip.core.normalize import is_tracking_param, normalize_url

    url = "https://a.com/p?ref=abc&utm_source=x&q=1"
    result = engine.apply("url.strip_tracking", _item(content=url, kind="link"))
    assert result.output == normalize_url(url)
    assert is_tracking_param("ref")


def test_path_normalize_multiline(engine):
    result = engine.apply("path.normalize", _item(content="/a//b.txt\n/c/./d.txt", kind="file"))
    assert result.ok, result.error
    assert result.output == "/a/b.txt\n/c/d.txt"


def test_path_normalize_none_when_already_clean(engine):
    result = engine.apply("path.normalize", _item(content="/a/b.txt", kind="file"))
    assert not result.ok


def test_path_normalize_rejects_empty(engine):
    result = engine.apply("path.normalize", _item(content="   ", kind="file"))
    assert not result.ok
