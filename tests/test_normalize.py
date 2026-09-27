"""内容归一：链接去跟踪参数、路径写法收敛。

归一直接决定「两条内容算不算同一条」，所以用例的重点不在「归一成功」，
而在**边界**：哪些该归一、哪些绝对不能碰。误合并比不合并更糟 ——
不合并只是列表多一行，误合并会让用户的内容凭空消失且不可恢复。
"""

import pytest

from linglong_clip.core.normalize import (
    canonical_content,
    canonical_key,
    is_tracking_param,
    normalize_path,
    normalize_url,
)

# ---- 查询参数判定 ----


@pytest.mark.parametrize(
    "name",
    ["utm_source", "utm_medium", "UTM_CAMPAIGN", "fbclid", "gclid", "spm", "ref",
     "mc_cid", "igshid", "msclkid", "pk_campaign", "mtm_source", "_hsenc"],
)
def test_tracking_params_recognized(name):
    assert is_tracking_param(name) is True


@pytest.mark.parametrize(
    "name",
    ["id", "v", "q", "page", "t", "lang", "hl", "file", "token", ""],
)
def test_meaningful_params_preserved(name):
    # 判断标准是「删掉它，用户打开的页面完全一样」。
    # `?v=` 决定视频版本、`?id=` 决定看哪条记录，这些必须是有效参数。
    assert is_tracking_param(name) is False


# ---- 链接归一 ----


def test_same_page_different_channels_merges():
    """同一个页面被不同渠道分享出的链接，归一后应当完全一致。

    这是本轮要解决的核心问题：不加归一，这三条在历史里各占一行。
    """
    weibo = "https://shop.example.com/item/42?utm_source=weibo&spm=a1z.123"
    wechat = "https://shop.example.com/item/42?utm_source=wechat&ref=abc"
    plain = "https://shop.example.com/item/42"
    assert normalize_url(weibo) == normalize_url(wechat) == normalize_url(plain)


def test_query_order_does_not_matter():
    assert normalize_url("https://a.com/p?b=2&a=1") == normalize_url("https://a.com/p?a=1&b=2")


def test_host_is_case_insensitive():
    # DNS 不区分大小写，`Example.COM` 与 `example.com` 是同一台机器
    assert normalize_url("https://EXAMPLE.COM/x") == normalize_url("https://example.com/x")


def test_trailing_slash_removed():
    assert normalize_url("https://a.com/b/") == normalize_url("https://a.com/b")


def test_root_slash_kept():
    # 根路径 `/` 不能削成空串，否则 `https://a.com` 与 `https://a.com/x` 的
    # 前缀关系会被破坏，归一结果也不稳定
    assert normalize_url("https://a.com/") == "https://a.com/"


def test_duplicate_slashes_collapsed():
    assert normalize_url("https://a.com//b//c") == "https://a.com/b/c"


@pytest.mark.parametrize(
    "url",
    [
        "https://a.com/p?v=2",
        "https://a.com/p?id=7",
        "https://a.com/p?page=3",
        "https://a.com/p?q=hello",
    ],
)
def test_semantic_params_survive(url):
    # `?v=2` 与 `?v=3` 可能是两个不同的视频页面，删掉就是误合并
    assert "?" in normalize_url(url)


def test_fragment_preserved():
    # 单页应用靠 `#` 区分页面，`/doc#install` 与 `/doc#config` 是两页
    assert normalize_url("https://a.com/doc#install") != normalize_url("https://a.com/doc#config")


def test_scheme_not_added():
    # 不补 scheme：`example.com/x` 与 `https://example.com/x` 可能真是两个东西
    assert normalize_url("example.com/x") == "example.com/x"


def test_no_scheme_result_has_no_leading_slashes():
    # urlunsplit 拿到空 scheme 会拼出 `//example.com/x`，
    # 多出的斜杠会让「归一后仍等于原值」的判断失效
    assert not normalize_url("example.com/x").startswith("//")


def test_port_preserved():
    assert normalize_url("http://localhost:8080/a/") == "http://localhost:8080/a"


def test_non_url_returned_unchanged():
    for text in ["hello world", "普通文本 utm_source=x", "", "https://a.com/p?a=1#f"]:
        if text.startswith("https"):
            continue
        assert normalize_url(text) == text


def test_strip_tracking_can_be_disabled():
    url = "https://a.com/p?utm_source=x&q=1"
    assert normalize_url(url, strip_tracking=False) != normalize_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://shop.example.com/item/42?utm_source=weibo&spm=x",
        "https://a.com//b/?b=2&a=1#frag",
        "example.com/x?utm_source=a",
        "https://a.com/",
        "http://localhost:8080/a/",
    ],
)
def test_url_normalization_is_idempotent(url):
    once = normalize_url(url)
    assert normalize_url(once) == once


# ---- 路径归一 ----


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/a//b", "/a/b"),
        ("/a/b/", "/a/b"),
        ("/a/./b", "/a/b"),
        ("/a/b/../c.txt", "/a/c.txt"),
        ("/tmp//a/../a/x.txt", "/tmp/a/x.txt"),
        ("/", "/"),
        ("~/docs/", "~/docs"),
        ("//srv/share/x", "//srv/share/x"),
        ("C:\\a\\..\\b", "C:\\b"),
        ("C:\\Users\\\\a\\f.txt", "C:\\Users\\a\\f.txt"),
    ],
)
def test_normalize_path(raw, expected):
    assert normalize_path(raw) == expected


@pytest.mark.parametrize("raw", ["../a", "..", "a/../..", "./x", "~/a", "//srv/share/x"])
def test_path_normalization_is_idempotent(raw):
    once = normalize_path(raw)
    assert normalize_path(once) == once


def test_relative_parent_not_escaping():
    # `../a` 不能变成 `a`：相对路径的 `..` 指向另一个位置，
    # 只有前面确实还有普通目录段时才允许回退
    assert normalize_path("../a") == "../a"
    assert normalize_path("a/../b") == "b"


def test_posix_root_clamp():
    assert normalize_path("/a/../..") == "/"


def test_unc_prefix_preserved():
    # UNC 路径的 `//` 是协议的一部分，折叠掉就不再是网络路径
    assert normalize_path("//server/share/file").startswith("//")


def test_path_with_spaces_untouched():
    assert normalize_path("/a/b c/d") == "/a/b c/d"


# ---- 按类型的统一入口 ----


def test_canonical_content_link():
    assert canonical_content("link", "https://a.com/p?utm_source=x") == "https://a.com/p"


def test_canonical_content_file():
    assert canonical_content("file", "/a//b/../c") == "/a/c"


def test_canonical_content_text_untouched():
    # 文本里一个空格都可能有意义，不该由我们替用户判定等价
    raw = "hello   world\n\n  tab\there"
    assert canonical_content("text", raw) == raw


def test_canonical_content_image_untouched():
    assert canonical_content("image", "某段 OCR 文本") == "某段 OCR 文本"


def test_canonical_content_multiline_paths_keep_order():
    # 一次复制多个文件时，点选顺序可能是刻意的，排序会让指纹不稳定
    raw = "/b//x\n/tmp/a/../c"
    assert canonical_content("file", raw) == "/b/x\n/tmp/c"


def test_canonical_key_prefers_files_list():
    from linglong_clip.core.normalize import canonical_key

    assert canonical_key("file", "ignored", files=["/a//x", "/b/./y"]) == "/a/x\n/b/y"


def test_options_can_disable_each_rule():
    url = "https://a.com/p?utm_source=x"
    assert canonical_content("link", url, options={"url_strip_tracking": False}) == url
    path = "/a//b"
    assert canonical_content("file", path, options={"path_normalize": False}) == path


# ---- 边界与容错 ----


def test_normalize_url_empty_and_blank():
    assert normalize_url("") == ""
    assert normalize_url("   ") == "   "


def test_normalize_path_empty_and_blank():
    assert normalize_path("") == ""
    assert normalize_path("   ") == "   "


def test_normalize_url_handles_bad_port():
    """`http://a.com:99999/x` 这种越界端口 urlparse 会抛错，不能让它冒出去。"""
    weird = "http://a.com:99999999/x"
    assert normalize_url(weird) == weird


def test_normalize_path_keeps_backslash_flavor():
    """Windows 路径归一后仍应当是 Windows 写法，不能混进 POSIX 斜杠。"""
    assert normalize_path("C:\\a\\b\\..\\c") == "C:\\a\\c"
    assert "\\" in normalize_path("C:\\a\\\\b")


def test_normalize_path_drive_only_falls_back():
    # 纯盘符没有可归一的段，返回原值而不是空串
    assert normalize_path("C:") == "C:"


def test_canonical_content_empty():
    assert canonical_content("link", "") == ""
    assert canonical_content("file", "") == ""


def test_canonical_content_unknown_kind_untouched():
    assert canonical_content("color", "#ff0000") == "#ff0000"


def test_multiline_paths_preserve_blank_lines():
    raw = "/a//x\n\n/b//y"
    assert canonical_content("file", raw) == "/a/x\n\n/b/y"
