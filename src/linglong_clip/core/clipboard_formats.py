"""剪贴板多形式：系统剪贴板里其实躺着好几份写法，这里把它们收好、用得起来。

**问题**：一次复制往往同时带来好几种表示。在浏览器里复制一段带链接的正文，
剪贴板里同时有 `text/html`（带 `<a>`）和 `text/plain`（链接被拍平成纯文本项，
URL 要么丢掉、要么以 `<https://…>` 这种形式挤在正文里）。文件管理器复制文件时
`text/uri-list` 与 `text/plain` 都在，两者在「粘贴到终端」这个场景下不等价。

而此前 `write_item()` 只会写回「我们存下来的那一份纯文本」：

```
在网页上复制一段正文（含 4 个链接）
  → 采集到剪贴板里的 text/plain，链接已经没了
  → 想粘到另一处，得到的是一段没有链接的文本
  → 原网页还在的话，得回去重新选一次
```

更糟的是 **换行也没了** —— QMimeData 的 `text()` 把行分隔符统一成 `\\n`，
Windows 上再写回去，粘贴进记事本会连成一行。用户在两边各看一次，
只会以为是自己复制姿势不对。

**做法**：采集时把「系统实际提供的那几种形式」逐个取下来存进 `metadata`，
复制出站时再按原来的方式摆回剪贴板，让系统自己决定该用哪一份。
`text/plain` 另存一份原始字节（`text_bytes`），粘贴出去与原文逐字节一致。

设计取舍：

- **只存有用的形式**。`text/uri-list` 与 `text/plain` 能互相推导时只留一份，
  否则每条记录都会翻倍。判定标准是「粘贴到别的应用会不会不一样」——
  纯文本就是路径列表时，`text/uri-list` 是纯冗余，丢掉；反过来说，
  只要两者不等价就都留，因为那时它们各有各的用法。
- **不认识的形式也留名不留内容**。像 `application/x-kde4-urilist` 这类
  平台私货，名字本身也是「这条记录当时是什么」的线索，只是不必存值 ——
  值可能很大，而且我们写回去也不会用它。
- **可以整个关掉**（`history.keep_formats`）。多存一份 HTML 会让单条记录
  变大，介意体积的用户应该能选择不要。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .classify import analyze, detect_kind

#: `metadata` 里存放各种形式的键
FORMATS_KEY = "formats"
#: `metadata` 里存放 `text/plain` 精确字节的键（base64）
TEXT_BYTES_KEY = "text_bytes"

#: 我们真正会取值的 MIME 类型。其余只记名字。
#: `text/uri-list` 单独处理：它已经能从 `files` 还原，不需要额外存值。
KNOWN_FORMATS: Tuple[str, ...] = (
    "text/plain",
    "text/html",
    "text/rtf",
    "application/rtf",
    "text/markdown",
    "text/uri-list",
)

#: 单个形式允许占用的上限（字节）。HTML 动辄几十上百 KB，
#: 超过这个量级多半是把整个页面塞进了剪贴板，存下来只是把历史文件撑大。
MAX_FORMAT_BYTES = 512 * 1024

#: `markdown` 这类「有富文本时应当优先取用」的形式
PREFERRED_MARKDOWN = ("text/markdown", "text/x-markdown")


@dataclass
class ClipboardFormats:
    """一条记录在剪贴板里的几种写法。

    `data` 是 `MIME -> 文本值`（二进制形式按 base64 存）；`names` 是
    「当时剪贴板里有、但我们不存值」的其它形式名。
    """

    data: Dict[str, str] = field(default_factory=dict)
    names: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.data and not self.names

    def get(self, mime: str) -> str:
        return self.data.get(mime, "")

    def has(self, mime: str) -> bool:
        return mime in self.data or mime in self.names

    def kind_names(self) -> List[str]:
        """给人看的形式名列表：`text/html`、`text/rtf`、以及「另外 3 种」里的名字。"""
        return list(self.data) + list(self.names)

    def to_metadata(self) -> Dict[str, object]:
        payload: Dict[str, object] = {}
        if self.data:
            payload["data"] = dict(self.data)
        if self.names:
            payload["names"] = list(self.names)
        return payload

    @classmethod
    def from_metadata(cls, raw: object) -> "ClipboardFormats":
        """从 `metadata["formats"]` 还原。

        形状不符就当作没有 —— 老库、手改过的 JSON、同步过来的旧记录
        都可能出现意外形状，这里不该抛异常把整条记录废掉。
        """
        if not isinstance(raw, dict):
            return cls()
        data: Dict[str, str] = {}
        raw_data = raw.get("data")
        if isinstance(raw_data, dict):
            for key, value in raw_data.items():
                if isinstance(key, str) and isinstance(value, str):
                    data[key] = value
        names: List[str] = []
        raw_names = raw.get("names")
        if isinstance(raw_names, (list, tuple)):
            names = [str(item) for item in raw_names if item]
        return cls(data=data, names=names)

    # ---- 取用 ----

    def markdown(self) -> str:
        for mime in PREFERRED_MARKDOWN:
            if self.data.get(mime):
                return self.data[mime]
        return ""

    def html(self) -> str:
        return self.data.get("text/html", "")

    def rtf(self) -> str:
        return self.data.get("text/rtf") or self.data.get("application/rtf") or ""


def _canonical_lines(text: str) -> List[str]:
    return [line for line in (text or "").replace("\r\n", "\n").split("\n") if line.strip()]


def uri_list_equivalent(text: str, files: Optional[Iterable[str]]) -> bool:
    """`text/plain` 是不是只是 `text/uri-list` 的纯文本翻版。

    是的话就不必两份都留：文件管理器给出的 `text/plain` 通常就是路径列表，
    而 `files` 字段已经完整表达了同一件事（而且更结构化）。
    判定从严 —— 只要有一行不是路径，就说明这份纯文本额外带了信息。
    """
    paths = list(files or [])
    if not paths:
        return False
    lines = _canonical_lines(text)
    if len(lines) != len(paths):
        return False
    return all(line == path for line, path in zip(lines, paths))


def diff_against_text(
    formats: ClipboardFormats, text: str, *, files: Optional[Iterable[str]] = None
) -> List[str]:
    """列出「这份形式和纯文本确实不一样」的那些。

    用来在界面上回答「我复制这段东西，跟别人拿到的不一样吗」。
    与纯文本逐字相同的 HTML（不少应用会这么给）不算额外信息，不列出来；
    `text/uri-list` 在纯文本就是同一批路径时同样不算（见 `uri_list_equivalent`）。
    """
    text = (text or "").strip()
    result: List[str] = []

    def _differs(mime: str) -> bool:
        value = formats.data.get(mime, "")
        return bool(value) and value.strip() != text

    for mime, label in (
        ("text/html", "text/html"),
        ("text/markdown", "text/markdown"),
        ("text/x-markdown", "text/markdown"),
    ):
        if _differs(mime) and label not in result:
            result.append(label)
    if formats.rtf():
        result.append("text/rtf")
    if formats.data.get("text/uri-list") and not uri_list_equivalent(
        formats.data["text/uri-list"], files
    ):
        result.append("text/uri-list")
    return result


def collect_formats(
    *,
    text: str,
    html: str = "",
    rtf: str = "",
    markdown: str = "",
    uri_list: str = "",
    files: Optional[Iterable[str]] = None,
    other_names: Optional[Iterable[str]] = None,
    limit: int = MAX_FORMAT_BYTES,
) -> ClipboardFormats:
    """把「系统提供的形式」收敛成要存进 `metadata` 的形状。

    规则只有一条：**能互相推导的不重复存**。`text/uri-list` 在纯文本就是
    路径列表时被丢掉，其余情况都保留。
    """
    formats = ClipboardFormats()

    def _put(mime: str, value: str) -> None:
        if not value:
            return
        encoded = value.encode("utf-8", "replace")
        if len(encoded) > limit:
            # 超限就只留名字：让用户知道「当时有这一份」，但不必把整页 HTML 存进来
            formats.names.append(mime)
            return
        formats.data[mime] = value

    files = list(files or [])
    text_is_paths = bool(files) and uri_list_equivalent(text, files)

    _put("text/html", html)
    _put("text/rtf", rtf)
    _put("text/markdown", markdown)
    if uri_list and not text_is_paths:
        _put("text/uri-list", uri_list)

    seen = set(formats.data) | set(formats.names)
    for name in other_names or ():
        name = str(name or "").strip()
        if not name or name in seen:
            continue
        if name == "text/plain" or name in seen:
            continue
        formats.names.append(name)
        seen.add(name)
    return formats


def format_summary(
    formats: ClipboardFormats, text: str, *, files: Optional[Iterable[str]] = None
) -> str:
    """一句话说清「这条记录在剪贴板里有哪些形式」，用于预览面板与 CLI。"""
    distinct = diff_against_text(formats, text, files=files)
    if not distinct and not formats.names:
        return ""
    parts = list(distinct)
    if formats.names:
        parts.append("另 " + str(len(formats.names)) + " 种")
    return "、".join(parts)


def preferred_plain_text(
    *, content: str, text_bytes: str = "", markdown: str = ""
) -> Tuple[str, str]:
    """收件方该拿到哪一份纯文本，以及理由。

    三选一，优先级从高到低：

    1. **`markdown`**。这是一处刻意的「先有 bug 后有取舍」：`# 标题` 这类
       文本会被 `classify` 判成「代码」（`#` 正好是代码信号的注释符），
       于是从 GitHub 复制一段 README 会变成代码记录。既然剪贴板里
       本来就有 `text/markdown`，就顺手给它一个正确的类型。
    2. **原始字节**（`text_bytes`）。WM 里那份 `text/plain` 的逐字节副本。
       用它粘贴，末尾有没有换行、是 `CRLF` 还是 `LF` 都与原文一致。
       直接写 `content` 时 Qt 会按平台规矩补齐行尾，看起来「就是多了一行」。
    3. **`content`**。兜底。

    返回 `(文本, 理由)`；理由为空表示就是 `content` 本身。
    """
    if markdown:
        return markdown, "markdown"
    if text_bytes:
        return text_bytes, "bytes"
    return content, ""


def looks_like_markdown(text: str) -> bool:
    """是不是「其实该按 Markdown 记账」的文本。

    只在类型为 `code` 时用来纠偏：`kind` 已经是 `text` 的没必要再判一次，
    而真正的代码（`def foo():`、`{...}`）不能被误伤 —— 判定要求命中
    Markdown 的结构特征，而不是只出现一个 `#`。
    """
    if not text:
        return False
    stripped = text.lstrip()
    if not stripped.startswith(("#", "- ", "* ", "> ", "|", "```", "1. ")):
        return False
    signals = 0
    lines = text.replace("\r\n", "\n").split("\n")
    for line in lines[:40]:
        head = line.lstrip()
        if head.startswith("#"):
            signals += 2
        elif head.startswith(("```", "> ", "|")):
            signals += 2
        elif head.startswith(("- ", "* ", "+ ")):
            signals += 1
        elif _ORDERED_ITEM_RE.match(head):
            signals += 1
    # 链接、粗体、行内代码也是 Markdown 的常见特征
    if "](" in text:
        signals += 1
    if "**" in text or "__" in text:
        signals += 1
    if "`" in text:
        signals += 1
    return signals >= 3


def retag_markdown(item) -> bool:
    """`code` 类型的 Markdown 文本，纠正为 `text` 并按 Markdown 记账。

    返回是否改了。只动「明显是文档」的，真正的代码片段保持原样 ——
    这条规则出错的表现是「代码被当成文档」，比「文档被当成代码」更烦人。
    """
    if item.kind != "code":
        return False
    formats = ClipboardFormats.from_metadata(item.metadata.get(FORMATS_KEY))
    candidate = formats.markdown() or item.content
    if not looks_like_markdown(candidate):
        return False
    item.kind = "text"
    if formats.markdown():
        item.metadata.setdefault("markdown", True)
    return True


#: 有序列表项：`1. ` / `2) ` / `1、`。用正则而不是切片判断 ——
#: 切片写法（`head[1:3] == ". "`）在中文序号与 `10. ` 上都会错。
_ORDERED_ITEM_RE = re.compile(r"^\d{1,3}\s*[.)、]\s+")

#: 「取回一种形式」的可用动作。`key` 用于 CLI 参数与配置，不随文案变。
@dataclass(frozen=True)
class FormatAction:
    key: str
    label: str
    detail: str = ""


#: 顺序就是界面里的展示顺序：越靠上的越常用
FORMAT_ACTIONS: Tuple[FormatAction, ...] = (
    FormatAction("text", "纯文本", "去掉格式，只留文字"),
    FormatAction("html", "HTML", "带格式与链接，粘进富文本编辑器"),
    FormatAction("markdown", "Markdown", "原始 Markdown 源码"),
    FormatAction("rtf", "RTF", "word 之类的富文本格式"),
    FormatAction("urilist", "文件路径列表", "一行一个路径，可直接喂给脚本"),
    FormatAction("shell", "Shell 命令", "每条路径按 shell 规则转义并加引号"),
    FormatAction("json", "JSON 数组", "路径/多行文本转成 JSON 数组"),
    FormatAction("fenced", "Markdown 代码块", "包在 ``` 里，粘贴到文档不会丢格式"),
)

#: `key` -> 该动作需要的 MIME 类型（没有就是在内存里现算，不依赖存下来的形式）
_FORMAT_ACTION_MIME: Dict[str, str] = {
    "html": "text/html",
    "markdown": "text/markdown",
    "rtf": "text/rtf",
    "urilist": "text/uri-list",
}


def available_actions(
    *, kind: str, content: str, formats: ClipboardFormats, files: Optional[Iterable[str]] = None
) -> List[FormatAction]:
    """这条记录当前能取用哪些形式。

    「能取用」的判定标准是**取出来的东西和普通粘贴不一样** ——
    一份和纯文本逐字相同的 HTML 没有取用价值，不该出现在菜单里。
    """
    if kind in ("image",):
        return []
    actions: List[FormatAction] = [FORMAT_ACTIONS[0]]  # 纯文本永远可用

    from . import classify  # 局部导入：避免与 classify 形成循环

    distinct = set(diff_against_text(formats, content, files=files))
    for action in FORMAT_ACTIONS[1:]:
        mime = _FORMAT_ACTION_MIME.get(action.key)
        if mime is not None:
            if mime not in distinct:
                continue
            actions.append(action)
            continue
        if action.key == "shell":
            if kind == "file" or (files if files is not None else classify.is_file_path(content)):
                actions.append(action)
        elif action.key == "json":
            if kind == "file" or files or "\n" in (content or ""):
                actions.append(action)
        elif action.key == "fenced":
            if content:
                actions.append(action)
    return actions


def action_keys(*, kind: str, content: str, formats: ClipboardFormats, files=None) -> List[str]:
    return [a.key for a in available_actions(kind=kind, content=content, formats=formats, files=files)]


def shell_quote(path: str) -> str:
    """按 POSIX shell 规则转义一个路径。

    无脑 `'"' + path + '"'` 是错的：路径里带双引号或 `$` 时，
    引号被提前闭合、变量被展开，粘到终端里执行的是另一个命令。
    这里按 shell 的规矩走：只保留安全字符，其余交给单引号，
    单引号本身用 `'\''` 逃出去。
    """
    if not path:
        return "''"
    safe = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@%+=:,./-_")
    if all(ch in safe for ch in path):
        return path
    return "'" + path.replace("'", "'\\''") + "'"


def resolve_action(
    key: str,
    *,
    kind: str,
    content: str,
    formats: ClipboardFormats,
    files: Optional[Iterable[str]] = None,
) -> Tuple[str, str]:
    """取用某种形式，返回 `(文本, 不适用/失败的原因)`；成功时原因为空串。

    一律返回文本而不是「有的返回文本有的返回二进制」：这个入口面向的是
    「我要把这份东西粘到别处」，而二进制形式（图片、RTF 的完整字节）
    有各自的粘贴通道，不该混进来。
    """
    files = list(files or [])
    if key == "text":
        if kind == "file" and files:
            return "\n".join(files), ""
        return content, ""

    if key == "urilist":
        if files:
            return "\n".join(files), ""
        raw = formats.data.get("text/uri-list", "")
        if raw:
            lines = [line for line in raw.replace("\r\n", "\n").split("\n") if line.strip()]
            return "\n".join(_uri_to_path(line) for line in lines), ""
        if kind == "file":
            return content, ""
        return "", "这条记录里没有文件路径列表"

    if key == "shell":
        targets = files or ([content] if kind == "file" else _nonempty_lines(content))
        if not targets:
            return "", "没有可转义的路径"
        return " ".join(shell_quote(path) for path in targets), ""

    if key == "json":
        import json as _json

        targets = files or _nonempty_lines(content)
        if not targets:
            return "", "没有可转换的内容"
        return _json.dumps(targets, ensure_ascii=False, indent=2), ""

    if key == "fenced":
        body = content or ""
        fence = "```"
        # 内容本身就含 ``` 时把围栏加长，否则粘出去代码块会被提前截断
        while fence in body:
            fence += "`"
        return f"{fence}\n{body}\n{fence}", ""

    mime = _FORMAT_ACTION_MIME.get(key)
    if mime is None:
        return "", f"未知的形式：{key}"
    value = formats.data.get(mime, "")
    if key == "markdown" and not value:
        value = formats.data.get("text/x-markdown", "")
    if not value:
        return "", f"这条记录里没有 {mime}"
    return value, ""


def _nonempty_lines(text: str) -> List[str]:
    return [line.strip() for line in (text or "").replace("\r\n", "\n").split("\n") if line.strip()]


def _uri_to_path(uri: str) -> str:
    from urllib.parse import unquote, urlparse

    uri = uri.strip()
    if not uri.startswith("file:"):
        return uri
    parsed = urlparse(uri)
    return unquote(parsed.path or uri)
