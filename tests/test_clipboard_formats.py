"""剪贴板多形式：把系统给出的那几份写法收下来、用得起来。

这一组守的是一类**「功能看着有、用起来不对」**的缺陷：

- 网页上复制带链接的正文，粘出来链接没了（`text/html` 从没存过）
- 复制一段块级代码，末尾凭空多一行（`text/plain` 被 Qt 按平台规矩重写）
- 从 GitHub 复制 README 变成「代码」类型（`#` 撞上代码注释符）
- 路径贴进终端，带空格的路径被拆成两个参数

所以用例重点全在**边界与判定**，而不是「HTML 存下来了」这种显然结论。
"""

import base64

import pytest

from linglong_clip.core.clipboard_formats import (
    FORMATS_KEY,
    TEXT_BYTES_KEY,
    ClipboardFormats,
    action_keys,
    available_actions,
    collect_formats,
    diff_against_text,
    format_summary,
    looks_like_markdown,
    preferred_plain_text,
    resolve_action,
    retag_markdown,
    shell_quote,
    uri_list_equivalent,
)
from linglong_clip.core.models import ClipboardItem


# ---- 收下形式 ----


def test_collect_keeps_html_and_marks_rtf():
    formats = collect_formats(text="hello", html="<b>hello</b>", rtf="{\\rtf1}")
    assert formats.data["text/html"] == "<b>hello</b>"
    assert formats.data["text/rtf"] == "{\\rtf1}"


def test_collect_drops_uri_list_when_text_is_the_paths():
    """纯文本就是路径列表时，`text/uri-list` 不构成额外信息，不重复存。"""
    formats = collect_formats(
        text="/tmp/a\n/tmp/b",
        uri_list="file:///tmp/a\nfile:///tmp/b",
        files=["/tmp/a", "/tmp/b"],
    )
    assert "text/uri-list" not in formats.data
    assert formats.is_empty


def test_collect_keeps_uri_list_when_text_differs():
    """反过来，两者不等价时都得留：它们各有各的用法（粘贴到终端 vs 粘贴到文件管理器）。"""
    formats = collect_formats(
        text="拖过来的两个文件",
        uri_list="file:///tmp/a\nfile:///tmp/b",
        files=["/tmp/a", "/tmp/b"],
    )
    assert formats.data["text/uri-list"] == "file:///tmp/a\nfile:///tmp/b"


def test_collect_keeps_only_names_for_unknown_formats():
    formats = collect_formats(text="x", other_names=["application/x-kde4-urilist", "TARGETS"])
    assert formats.data == {}
    assert "application/x-kde4-urilist" in formats.names


def test_collect_records_oversized_form_without_its_value():
    """超限的形式只留名字：让用户知道「当时有这一份」，但不把整页 HTML 存进历史。"""
    formats = collect_formats(text="x", html="<p>" + "a" * 100, limit=10)
    assert "text/html" not in formats.data
    assert "text/html" in formats.names


def test_metadata_roundtrip():
    formats = collect_formats(text="x", html="<b>x</b>", other_names=["TARGETS"])
    restored = ClipboardFormats.from_metadata(formats.to_metadata())
    assert restored.data == formats.data
    assert restored.names == formats.names


@pytest.mark.parametrize("raw", [None, "nope", [], {"data": "bad"}, {"names": "bad"}])
def test_metadata_shape_tolerance(raw):
    """老库、手改过的 JSON、同步来的旧记录都可能形状不对，绝不能抛异常。"""
    formats = ClipboardFormats.from_metadata(raw)
    assert formats.data == {}
    assert formats.names == []


# ---- 「和纯文本不一样」的判定 ----


def test_identical_html_is_not_a_distinct_form():
    formats = collect_formats(text="hello", html="hello")
    assert diff_against_text(formats, "hello") == []
    assert format_summary(formats, "hello") == ""


def test_distinct_html_is_reported():
    formats = collect_formats(text="hello", html="<b>hello</b>")
    assert diff_against_text(formats, "hello") == ["text/html"]
    assert "text/html" in format_summary(formats, "hello")


def test_uri_list_not_distinct_when_it_matches_files():
    formats = collect_formats(text="/a\n/b", uri_list="file:///a\nfile:///b", files=["/a", "/b"])
    assert diff_against_text(formats, "/a\n/b", files=["/a", "/b"]) == []


# ---- 该给哪一份纯文本 ----


def test_preferred_prefers_markdown():
    text, source = preferred_plain_text(content="plain", text_bytes="raw", markdown="# md")
    assert (text, source) == ("# md", "markdown")


def test_preferred_prefers_exact_bytes_over_content():
    """"原始字节优先级高于 `content`：Qt 会按平台规矩补行尾，字节不会。"""
    text, source = preferred_plain_text(content="abc", text_bytes="abc\r\n")
    assert (text, source) == ("abc\r\n", "bytes")


def test_preferred_falls_back_to_content():
    assert preferred_plain_text(content="abc") == ("abc", "")


# ---- Markdown 纠偏 ----


@pytest.mark.parametrize(
    "text",
    [
        "# 标题\n\n- 一\n- 二\n\n**粗体** 和 [链接](http://a)\n",
        "## 说明\n\n| 列 | 值 |\n| --- | --- |\n| a | 1 |\n",
        "> 引用\n\n1. 第一步\n2. 第二步\n",
    ],
)
def test_looks_like_markdown_accepts_documents(text):
    assert looks_like_markdown(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "def foo():\n    return 1",
        "#include <stdio.h>\nint main() { return 0; }",
        "SELECT * FROM items WHERE id = 1",
        "for (i = 0; i < 10; i++) { sum += i; }",
        "user = {'name': 'a'}\nprint(user)",
    ],
)
def test_looks_like_markdown_rejects_code(text):
    """真正的代码不能被误判成文档 —— 那个方向的错误更烦人。"""
    assert looks_like_markdown(text) is False


def test_retag_markdown_moves_code_to_text():
    item = ClipboardItem(kind="code", content="# 标题\n\n- 一\n- 二\n\n**粗体** 和 [链接](http://a)\n")
    assert retag_markdown(item) is True
    assert item.kind == "text"


def test_retag_markdown_leaves_real_code_alone():
    item = ClipboardItem(kind="code", content="def foo():\n    return 1")
    assert retag_markdown(item) is False
    assert item.kind == "code"


def test_retag_markdown_uses_stored_markdown_form():
    """正文被判成别的类型时，以剪贴板里那份 `text/markdown` 为准。"""
    item = ClipboardItem(
        kind="code",
        content="随便一段",
        metadata={FORMATS_KEY: {"data": {"text/markdown": "# 标题\n\n- 一\n- 二\n\n**粗** [链](u)\n"}}},
    )
    assert retag_markdown(item) is True
    assert item.kind == "text"


# ---- 可用动作与取用 ----


def test_available_actions_baseline_is_plain_text():
    formats = ClipboardFormats()
    keys = action_keys(kind="text", content="hello", formats=formats)
    assert keys[0] == "text"
    assert "html" not in keys


def test_available_actions_includes_html_only_when_distinct():
    same = collect_formats(text="hello", html="hello")
    assert "html" not in action_keys(kind="text", content="hello", formats=same)
    distinct = collect_formats(text="hello", html="<b>hello</b>")
    assert "html" in action_keys(kind="text", content="hello", formats=distinct)


def test_available_actions_offers_shell_for_paths():
    keys = action_keys(kind="file", content="/a b/c", formats=ClipboardFormats(), files=["/a b/c"])
    assert "shell" in keys and "json" in keys


def test_image_has_no_form_actions():
    assert available_actions(kind="image", content="", formats=ClipboardFormats()) == []


def test_resolve_action_urilist_converts_file_uris():
    formats = collect_formats(
        text="两个文件", uri_list="file:///tmp/a%20b.txt", files=[]
    )
    text, error = resolve_action("urilist", kind="text", content="两个文件", formats=formats)
    assert error == ""
    assert text == "/tmp/a b.txt"


def test_resolve_action_urilist_reports_absence():
    text, error = resolve_action("urilist", kind="text", content="hi", formats=ClipboardFormats())
    assert text == "" and "文件路径" in error


def test_resolve_action_json_indents():
    text, error = resolve_action(
        "json", kind="file", content="", formats=ClipboardFormats(), files=["/a", "/b"]
    )
    assert error == ""
    assert '"\\/a"' not in text and "/a" in text and text.startswith("[")


def test_resolve_action_unknown_key():
    text, error = resolve_action("nope", kind="text", content="x", formats=ClipboardFormats())
    assert text == "" and "未知" in error


def test_resolve_action_fence_grows_when_body_contains_fence():
    """正文里本来就有 ``` 时围栏要加长，否则粘出去代码块会被提前截断。"""
    text, _ = resolve_action("fenced", kind="text", content="a\n```\nb", formats=ClipboardFormats())
    assert text.startswith("````\n")


# ---- shell 转义 ----


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/tmp/plain.txt", "/tmp/plain.txt"),
        ("/tmp/a b/c.txt", "'/tmp/a b/c.txt'"),
        ("it's", "'it'\\''s'"),
        ("$HOME/x", "'$HOME/x'"),
        ('say "hi"', "'say \"hi\"'"),
        ("", "''"),
    ],
)
def test_shell_quote(path, expected):
    assert shell_quote(path) == expected


def test_shell_quote_does_not_expand_variables():
    """无脑加双引号是错的：`$HOME` 会被 shell 展开成别的东西。"""
    assert "$HOME" not in shell_quote("a$HOMEb").replace("'", "") or "'" in shell_quote("a$HOMEb")


# ---- 文件形态 ----


def test_uri_list_equivalent_needs_matching_lines():
    assert uri_list_equivalent("/a", ["/a"]) is True
    assert uri_list_equivalent("/a\n/b", ["/a"]) is False
    assert uri_list_equivalent("/a", []) is False
    assert uri_list_equivalent("", ["/a"]) is False


def test_text_bytes_key_is_what_the_adapter_writes():
    """采集侧与取用侧用的是同一个键名，改名时两边必须一起动。"""
    assert TEXT_BYTES_KEY == "text_bytes"
    assert base64.b64encode("x".encode()).decode() == "eA=="
