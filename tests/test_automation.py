"""自动化：内置转换器与规则引擎。"""

import pytest

from linglong_clip.core.automation import (
    REGISTRY,
    TRIGGER_AUTO,
    TRIGGER_OUTBOUND,
    AutomationEngine,
    Rule,
    TransformError,
    describe_rules,
    format_rules,
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


# ---- 出站规则（trigger = outbound）----


def test_outbound_rule_transforms_copy_payload(engine):
    """复制前清理跟踪参数：这是出站规则存在的唯一理由。"""
    engine.load_from_config(
        [
            {
                "name": "去跟踪参数",
                "kinds": ["link"],
                "contains": "utm_",
                "actions": ["url.strip_tracking"],
                "trigger": "outbound",
            }
        ]
    )
    data = engine.outbound_data(_item(kind="link", content="https://a.com/p?utm_source=x&id=1"))

    assert data.modified is True
    assert data.text == "https://a.com/p?id=1"
    assert data.applied == ["去跟踪参数"]


def test_outbound_rule_does_not_run_at_capture(engine):
    """出站规则不该在采集时执行 —— 否则产物会被写进历史，用户找不到原文。"""
    engine.load_from_config(
        [{"name": "r", "kinds": ["link"], "actions": ["url.strip_tracking"],
          "trigger": "outbound"}]
    )
    assert engine.auto_actions_for(_item(kind="link", content="https://a.com/?utm_source=x")) == []


def test_auto_rule_does_not_run_on_outbound(engine):
    """反过来也一样：采集规则不该在复制时突然改掉要粘贴的内容。"""
    engine.load_from_config(
        [{"name": "r", "kinds": ["text"], "actions": ["json.minify"],
          "trigger": "auto"}]
    )
    data = engine.outbound_data(_item(content='{ "a" : 1 }'))
    assert data.modified is False
    assert data.text == '{ "a" : 1 }'


def test_outbound_untouched_when_rule_does_not_match(engine):
    engine.load_from_config(
        [{"name": "r", "kinds": ["link"], "contains": "utm_",
          "actions": ["url.strip_tracking"], "trigger": "outbound"}]
    )
    data = engine.outbound_data(_item(kind="link", content="https://a.com/clean"))
    assert data.modified is False
    assert data.text == "https://a.com/clean"
    assert data.describe() == ""


def test_outbound_failure_falls_back_to_original(engine):
    """规则没跑成时必须照原样复制，而不是把内容变成空。"""
    engine.load_from_config(
        [{"name": "不合法的转换", "kinds": ["text"], "actions": ["json.minify"],
          "trigger": "outbound"}]
    )
    data = engine.outbound_data(_item(content="这不是 JSON"))

    assert data.text == "这不是 JSON"
    assert data.modified is False
    assert data.errors and "不合法的转换" in data.errors[0]
    assert "未生效" in data.describe()


def test_outbound_chain_applies_in_order(engine):
    engine.load_from_config(
        [{"name": "格式化再压缩", "kinds": ["text"],
          "actions": ["json.format", "json.minify"], "trigger": "outbound"}]
    )
    data = engine.outbound_data(_item(content='{"a":1,"b":2}'))
    assert data.text == '{"a":1,"b":2}'
    assert data.modified is True


def test_outbound_skip_partial_chain_keeps_original(engine):
    """链式里任一步失败都退回原文，不留半成品。"""
    engine.load_from_config(
        [{"name": "两步", "kinds": ["text"],
          "actions": ["json.format", "json.minify"], "trigger": "outbound"}]
    )
    data = engine.outbound_data(_item(content="not json at all"))
    assert data.text == "not json at all"
    assert data.modified is False


# ---- 触发方式校验与规则体检 ----


def test_unknown_trigger_is_treated_as_manual_and_kept_for_audit():
    """写错的触发方式要留下痕迹：单看 trigger=manual 分不清「就要手动」还是「写错了」。"""
    rule = Rule.from_dict({"name": "r", "actions": ["json.minify"], "trigger": "paste"})
    assert rule.trigger == "manual"
    assert rule.raw_trigger == "paste"


def test_valid_trigger_is_preserved_in_raw():
    rule = Rule.from_dict({"name": "r", "actions": ["json.minify"], "trigger": "outbound"})
    assert rule.trigger == "outbound"
    assert rule.raw_trigger == "outbound"


def test_describe_rules_flags_unknown_action(engine):
    engine.load_from_config([{"name": "坏", "actions": ["nope"], "trigger": "auto"}])
    report = describe_rules(engine.rules())
    assert report[0].will_run is False
    assert any("未知动作" in problem for problem in report[0].problems)


def test_describe_rules_flags_empty_actions(engine):
    engine.load_from_config([{"name": "空", "actions": [], "trigger": "auto"}])
    assert describe_rules(engine.rules())[0].will_run is False


def test_describe_rules_flags_bad_trigger(engine):
    engine.load_from_config(
        [{"name": "拼错", "actions": ["json.minify"], "trigger": "on_copy"}]
    )
    problems = describe_rules(engine.rules())[0].problems
    assert any("on_copy" in problem for problem in problems)


def test_describe_rules_flags_bad_kind(engine):
    engine.load_from_config(
        [{"name": "类型拼错", "kinds": ["txt"], "actions": ["json.minify"], "trigger": "auto"}]
    )
    problems = describe_rules(engine.rules())[0].problems
    assert any("txt" in problem for problem in problems)


def test_describe_rules_flags_bad_regex(engine):
    engine.load_from_config(
        [{"name": "正则坏", "pattern": "([", "actions": ["json.minify"], "trigger": "auto"}]
    )
    problems = describe_rules(engine.rules())[0].problems
    assert any("正则" in problem for problem in problems)


def test_describe_rules_flags_disabled(engine):
    engine.load_from_config(
        [{"name": "禁用了", "enabled": False, "actions": ["json.minify"], "trigger": "auto"}]
    )
    problems = describe_rules(engine.rules())[0].problems
    assert any("禁用" in problem for problem in problems)


def test_healthy_rules_report_no_problems(engine):
    engine.load_from_config(
        [
            {"name": "a", "kinds": ["text"], "actions": ["json.minify"], "trigger": "auto"},
            {"name": "b", "kinds": ["link"], "actions": ["url.strip_tracking"],
             "trigger": "outbound"},
            {"name": "c", "kinds": ["text"], "actions": ["lines.unique"]},
        ]
    )
    reports = describe_rules(engine.rules())
    assert all(report.will_run for report in reports)
    assert "采集入库时自动执行" in reports[0].when
    assert "复制/粘贴出去时" in reports[1].when
    assert "手动" in reports[2].when


def test_format_rules_reports_summary():
    text = format_rules([])
    assert "没有配置" in text


def test_rule_built_in_code_defaults_raw_trigger_to_manual():
    """直接构造 Rule（不走 from_dict）时 raw_trigger 也要有值。

    它是体检的输入，缺了就会在 `describe_rules` 里抛 AttributeError ——
    体检脚本自己崩掉比漏报更糟，因为用户会以为「没问题」。
    """
    rule = Rule(name="x", actions=["json.minify"], trigger=TRIGGER_OUTBOUND)
    assert rule.raw_trigger == "manual"
    report = describe_rules([rule])[0]
    # 手工构造时不带原始写法，不该被误判成「写错了触发方式」
    assert all("不是有效值" not in problem for problem in report.problems)


def test_describe_rules_flags_outbound_image_rule(engine):
    """图片在剪贴板里是位图，没有可用的文本转换 —— 配了也不会有产物。"""
    engine.load_from_config(
        [{"name": "图片出站", "kinds": ["image"], "actions": ["lines.unique"],
          "trigger": "outbound"}]
    )
    problems = describe_rules(engine.rules())[0].problems
    assert any("图片" in problem for problem in problems)


def test_outbound_image_rule_produces_nothing(engine):
    """与上面那条体检结论对齐：实际执行确实什么都做不了。"""
    engine.load_from_config(
        [{"name": "图片出站", "kinds": ["image"], "actions": ["lines.unique"],
          "trigger": "outbound"}]
    )
    data = engine.outbound_data(_item(kind="image", content=""))
    assert data.modified is False
    assert data.errors
