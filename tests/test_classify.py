"""类型识别与敏感信息检测。"""

import pytest

from linglong_clip.core import classify


@pytest.mark.parametrize(
    "text,expected",
    [
        ("https://cnb.cool/foo/bar", "link"),
        ("www.example.com/path", "link"),
        ("#ff8800", "color"),
        ("#abc", "color"),
        ("rgb(12, 34, 56)", "color"),
        ("hsla(120, 50%, 50%, 0.5)", "color"),
        ("/home/user/docs/report.pdf", "file"),
        ("C:\\Users\\me\\Desktop\\a.txt", "file"),
        ("~/Downloads/file.zip", "file"),
        ("这是一句普通的中文文本。", "text"),
        ("hello world", "text"),
    ],
)
def test_detect_kind_basic(text, expected):
    assert classify.detect_kind(text) == expected


def test_detect_kind_image_and_files_take_priority():
    # 图片与文件优先级最高：截图工具通常同时提供文本与图片格式
    assert classify.detect_kind("shot.png", has_image=True) == "image"
    assert classify.detect_kind("/tmp/a", has_files=True) == "file"
    # 文件优先于图片：从文件管理器复制图片时两者并存
    assert classify.detect_kind("x.png", has_image=True, has_files=True) == "file"


@pytest.mark.parametrize(
    "code",
    [
        "def foo(x):\n    return x + 1\n\nclass A:\n    pass",
        "import os\nimport sys\n\ndef main():\n    print(1)\n",
        "public class Main {\n    public static void main(String[] a) {\n    }\n}",
        "func main() {\n    fmt.Println(\"hi\")\n}",
    ],
)
def test_looks_like_code_positive(code):
    assert classify.looks_like_code(code) is True


@pytest.mark.parametrize(
    "text",
    [
        "今天天气不错，适合出门散步。",
        "会议纪要：下周二评审，请提前准备材料。",
        "一二三四五",
        "a",
    ],
)
def test_looks_like_code_negative(text):
    assert classify.looks_like_code(text) is False


@pytest.mark.parametrize(
    "text,reason",
    [
        ("sk-abcdefghijklmnopqrstuvwxyz123456", "API Key"),
        ("AKIAIOSFODNN7EXAMPLE", "AWS 凭证"),
        ("password: hunter2xyz", "密码字段"),
        ("my token = abcdefghijklmno", "密码字段"),
        ("", None),  # 空串不应命中任何规则
    ],
)
def test_detect_sensitive(text, reason):
    hits = classify.detect_sensitive(text)
    if reason is None:
        assert hits == []
    else:
        assert reason in hits


def test_detect_sensitive_private_key():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    assert "私钥" in classify.detect_sensitive(text)


def test_analyze_marks_sensitive_and_overrides_kind():
    result = classify.analyze("password: hunter2xyz")
    assert result["kind"] == "sensitive"
    assert result["sensitive"] is True
    assert "密码字段" in result["sensitive_reasons"]


def test_analyze_extracts_tags():
    result = classify.analyze("#bug 这个很紧急 🔥")
    assert "bug" in result["tags"]
    assert "urgent" in result["tags"]


def test_extract_tags_skips_color_values():
    # 色值形如话题标签，必须排除，否则每条颜色记录都会带一个无意义标签
    assert classify.extract_tags("#ff8800", kind="color") == []
    assert classify.extract_tags("#ff8800") == []


def test_extract_tags_dedupes_case_insensitively():
    tags = classify.extract_tags("#Bug #bug #BUG")
    assert len([tag for tag in tags if tag.lower() == "bug"]) == 1


def test_is_password_manager():
    assert classify.is_password_manager("1Password.exe") is True
    assert classify.is_password_manager("Bitwarden") is True
    assert classify.is_password_manager("firefox") is False


def test_is_private_host():
    assert classify.is_private_host("192.168.1.1") is True
    assert classify.is_private_host("localhost") is True
    assert classify.is_private_host("git.internal") is True
    assert classify.is_private_host("cnb.cool") is False


def test_make_preview_truncates_and_collapses():
    preview = classify.make_preview("text", "a  \n\n  b" * 100)
    assert "\n" not in preview
    assert len(preview) <= 280


def test_summarize_url():
    parts = classify.summarize_url("https://cnb.cool/asoe/repo?x=1")
    assert parts["host"] == "cnb.cool"
    assert parts["scheme"] == "https"
    assert parts["path"] == "/asoe/repo"
