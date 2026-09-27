"""内容识别：类型判定、敏感信息标记、标签抽取。

全部基于规则而非模型，理由是剪贴板内容高频且必须零延迟，
正则与启发式的成本可忽略，且判定逻辑可解释、可单测。
"""

from __future__ import annotations

import ipaddress
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .models import CODE, COLOR, FILE, IMAGE, LINK, RICH, SENSITIVE, TEXT

# ---- 基础正则 ----

_URL_RE = re.compile(
    r"^(?:https?|ftp|file|ssh|git|ws|wss|mailto|tel)://[^\s]+$"
    r"|^www\.[^\s]+$"
    r"|^[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+/[^\s]*$",
    re.IGNORECASE,
)
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_RGB_COLOR_RE = re.compile(
    r"^rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(?:,\s*[\d.]+\s*)?\)$",
    re.IGNORECASE,
)
_HSL_COLOR_RE = re.compile(
    r"^hsla?\(\s*\d{1,3}\s*,\s*\d{1,3}%\s*,\s*\d{1,3}%\s*(?:,\s*[\d.]+\s*)?\)$",
    re.IGNORECASE,
)

#: 路径判定：Windows 盘符、UNC、POSIX 绝对路径、`~/` 开头
_PATH_RE = re.compile(
    r"^(?:[a-zA-Z]:[\\/][^\r\n\t\"<>|?*]*"
    r"|\\\\[^\r\n\t\"<>|?*]+"
    r"|/(?:[^\s/]+/)*[^\s/]*"
    r"|~[/\\][^\r\n\t\"<>|?*]*)$"
)

# ---- 代码特征 ----
#: 用「语法特征计数」判定，命中 2 项以上才认作代码，避免把普通文本误判
_CODE_SIGNALS: List[Tuple[re.Pattern[str], int]] = [
    (re.compile(r"^\s*(?:def|class|func|fn|impl|struct|interface|type)\s+\w+", re.M), 2),
    (re.compile(r"^\s*(?:import|from|use|require|#include|using)\s+\S+", re.M), 2),
    (re.compile(r"^\s*(?:public|private|protected|static|async|final)\s+\S+", re.M), 2),
    (re.compile(r"\b(?:if|for|while|switch|match)\s*\(?.*[):{]\s*$", re.M), 1),
    (re.compile(r"[{}();]\s*$", re.M), 1),
    (re.compile(r"=>|->|::|:=|\?\?"), 1),
    (re.compile(r"^\s*(?://|#|/\*|\*|--)\s", re.M), 1),
    (re.compile(r"\b(?:return|yield|await|await|throw|raise|except)\b"), 1),
    (re.compile(r"(?:^|\s)[A-Za-z_]\w*\s*=\s*[^=]"), 1),
]
_CODE_THRESHOLD = 3
#: 明显是编程语言源代码扩展名
_CODE_EXT_RE = re.compile(
    r"\.(?:py|js|ts|tsx|jsx|java|kt|kts|go|rs|c|cc|cpp|cxx|h|hpp|cs|rb|php|swift|m|mm|sh|bash|zsh|fish|ps1|lua|pl|r|sql|vue|svelte|dart|scala|clj|ex|exs|erl|hs|ml|jl|nim|zig|v|groovy|gradle|tf|proto|graphql|gql)$",
    re.IGNORECASE,
)

# ---- 敏感信息 ----
SENSITIVE_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    # PEM 私钥的固定开头就是 5 个连字符，用 {5,} 兼容手写时多打的情况
    ("私钥", re.compile(r"-{5,}BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-{5,}")),
    ("API Key", re.compile(r"\b(?:sk|pk|ghp|gho|ghs|glpat|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b")),
    ("AWS 凭证", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("银行卡", re.compile(r"\b(?:\d[ -]?){15,19}\b")),
    ("身份证", re.compile(r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b")),
    (
        "密码字段",
        re.compile(
            r"(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key)"
            r"\s*[:=]\s*\S{6,}",
            re.IGNORECASE,
        ),
    ),
    ("邮箱", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
]

#: 这些应用被视作密码管理器，默认不记录其内容
PASSWORD_MANAGER_HINTS = (
    "1password",
    "bitwarden",
    "keepass",
    "lastpass",
    "dashlane",
    "enpass",
    "keeper",
    "nordpass",
    "protonpass",
    "roboform",
    "buttercup",
    "pass",
    "gnome-keyring",
    "seahorse",
    "keychain",
    "credential",
)

# ---- 标签抽取 ----
_HASHTAG_RE = re.compile(r"#([\w\u4e00-\u9fff-]{1,30})")
_EMOJI_TAG = {
    "🐛": "bug",
    "✨": "feature",
    "🔥": "urgent",
    "📌": "pin",
    "✅": "done",
    "📝": "note",
    "💡": "idea",
    "⚠️": "warning",
}


def detect_kind(
    text: str,
    *,
    has_html: bool = False,
    has_image: bool = False,
    has_files: bool = False,
) -> str:
    """判定载荷类型。

    优先级：文件 > 图片 > 颜色 > 链接 > 文件路径 > 代码 > 富文本 > 文本。
    文件排在图片之前是有意的：从文件管理器复制图片时两种格式同时存在，
    用户的本意是「这几个文件」，记成图片会丢失路径信息。
    """
    if has_files:
        return FILE
    if has_image:
        return IMAGE

    stripped = (text or "").strip()
    if not stripped:
        return TEXT

    if _is_single_line(stripped):
        if is_color(stripped):
            return COLOR
        if is_url(stripped):
            return LINK
        if is_file_path(stripped):
            return FILE

    if looks_like_code(stripped):
        return CODE

    if has_html and _has_rich_markup(text):
        return RICH

    return TEXT


def _is_single_line(text: str) -> bool:
    return "\n" not in text and len(text) < 2048


def is_url(text: str) -> bool:
    """判断是否为 URL。

    注意 `www.example.com/x` 这种没有 scheme 的写法：
    `urlparse` 会把它整体当成 path（netloc 为空），
    所以必须先补上 scheme 再解析，否则合法链接会被判成普通文本。
    """
    if len(text) > 2048 or any(ch.isspace() for ch in text):
        return False
    if not _URL_RE.match(text):
        return False

    candidate = text if "://" in text else f"http://{text}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return False

    host = parsed.hostname
    if not host:
        return False
    # 允许 localhost 这类无点主机名，其余必须带点
    return "." in host or host == "localhost"


def is_color(text: str) -> bool:
    return bool(
        _HEX_COLOR_RE.match(text)
        or _RGB_COLOR_RE.match(text)
        or _HSL_COLOR_RE.match(text)
    )


def is_file_path(text: str) -> bool:
    if "\n" in text or len(text) > 4096:
        return False
    if not _PATH_RE.match(text):
        return False
    # 单个 `/` 或 `/ 短词` 不当作路径
    return len(text.strip()) > 2


def looks_like_code(text: str) -> bool:
    if len(text) < 12:
        return False
    if len(text) > 200_000:
        return False

    score = 0
    for pattern, weight in _CODE_SIGNALS:
        if pattern.search(text):
            score += weight
            if score >= _CODE_THRESHOLD + 2:
                break
    if score < _CODE_THRESHOLD:
        # 缩进层级明显（4 空格或 tab 连续多行）也算弱信号
        indented = sum(1 for line in text.splitlines() if line.startswith(("    ", "\t")))
        if indented >= 3 and score >= 1:
            score += 1
    return score >= _CODE_THRESHOLD


def _has_rich_markup(html: str) -> bool:
    lowered = html.lower()
    return any(
        tag in lowered
        for tag in ("<b>", "<i>", "<u>", "<strong>", "<em>", "<a ", "<span", "<p>", "<ul")
    )


def detect_sensitive(text: str) -> List[str]:
    """返回命中的敏感规则名，空列表表示未命中。"""
    if not text or len(text) > 100_000:
        # 超长内容不做全量扫描，避免卡顿；只扫前 100KB
        text = text[:100_000]
    hits: List[str] = []
    for name, pattern in SENSITIVE_PATTERNS:
        try:
            if pattern.search(text):
                hits.append(name)
        except re.error:
            continue
    return hits


def is_password_manager(app_id: str) -> bool:
    lowered = (app_id or "").lower()
    return any(hint in lowered for hint in PASSWORD_MANAGER_HINTS)


def extract_tags(text: str, *, kind: str = "") -> List[str]:
    """从内容里抽取标签：`#话题` 与语义化 emoji。

    颜色值（`#ff00aa`）与 CSS 颜色关键字会被 `_HASHTAG_RE` 误抓成标签，
    因此色值类内容直接跳过话题抽取。
    """
    tags: List[str] = []
    if not text:
        return tags
    if kind == COLOR or is_color(text.strip()):
        return tags
    for match in _HASHTAG_RE.finditer(text[:20_000]):
        tag = match.group(1)
        if tag.lower() not in (existing.lower() for existing in tags):
            tags.append(tag)
        if len(tags) >= 8:
            break
    for emoji, tag in _EMOJI_TAG.items():
        if emoji in text and tag not in tags:
            tags.append(tag)
    return tags[:10]


def make_preview(kind: str, text: str, *, files: Optional[List[str]] = None) -> str:
    """生成列表里展示的一行摘要。"""
    if kind == IMAGE:
        return "[图片]"
    if kind == FILE:
        items = files or ([text] if text else [])
        if len(items) == 1:
            return items[0]
        return f"[{len(items)} 个文件] {items[0] if items else ''}"
    collapsed = " ".join((text or "").split())
    return collapsed[:280]


def analyze(
    text: str,
    *,
    has_html: bool = False,
    has_image: bool = False,
    has_files: bool = False,
) -> Dict[str, object]:
    """一次性得出分类结论，供采集管线调用。

    敏感内容强制归类为 `sensitive`，优先于其它类型 —— 这类内容默认不落盘。
    """
    kind = detect_kind(
        text, has_html=has_html, has_image=has_image, has_files=has_files
    )
    sensitive_hits = detect_sensitive(text) if kind != IMAGE else []
    tags = extract_tags(text, kind=kind)
    if sensitive_hits:
        kind = SENSITIVE
    return {
        "kind": kind,
        "sensitive": bool(sensitive_hits),
        "sensitive_reasons": sensitive_hits,
        "tags": tags,
        "preview": make_preview(kind, text, files=None),
    }


def summarize_url(text: str) -> Dict[str, str]:
    """拆出链接的域名与路径，用于来源分组与展示。"""
    if not is_url(text):
        return {}
    candidate = text if "://" in text else f"https://{text}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return {}
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname or "",
        "path": parsed.path or "",
    }


def is_private_host(host: str) -> bool:
    """判断是否内网地址，用于「内网链接不外传」这类策略。"""
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return host.endswith((".local", ".internal", ".lan"))
