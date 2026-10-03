"""内容归一：判断「这两段内容算不算同一条」时用的规范化。

去重、跨设备对账、重复项合并都建立在内容指纹（`hash`）之上，而指纹直接
取自原始内容。于是「同一条链接从不同 App 复制」会生成不同的指纹：

```
https://shop.example.com/item/42?utm_source=weibo&spm=a1z.123    ← 微博分享
https://shop.example.com/item/42?utm_source=wechat&ref=abc       ← 微信分享
https://shop.example.com/item/42                                 ← 地址栏
```

三条，各自独立占一行、各自记一次复制次数。但用户眼里这就是「同一个页面」，
只是被不同渠道的分享链接各加了一串尾巴。文件路径同理：`/tmp/a/../a/x.txt`
与 `/tmp/a/x.txt` 指的是同一个文件。

归一的原则是**保守**：只剥掉明确无意义的装饰，任何拿不准的地方一律原样保留。
误合并比不合并更糟——不合并只是列表多几行，误合并会让用户找不到东西，
而且原始内容已经被删了，救不回来。所以：

- 只处理**确定是链接**（`classify.is_url`）与**确定是路径**（`classify.is_file_path`）的输入
- 查询参数只删白名单里的「跟踪参数」，其余参数**一个都不动**（可能带语义）
- 短链接不做展开（那要发网络请求），`?v=` / `?id=` 这类可能改变页面内容的参数保留
- 片段（`#anchor`）**不删**：单页应用靠它区分页面，删掉会把两个页面并成一条

归一后的字符串**只用于计算指纹**，不写回 `item.content`。用户看到的、
复制的、搜索的仍然是原始内容——规范化一旦写进记录，就是在篡改用户数据。
"""

from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: 明确只服务渠道归因、对页面内容无影响的查询参数。
#: 判断标准是「删掉它，用户打开的页面完全一样」——不满足这条的一律不进来。
#: 前缀匹配的放 _TRACKING_PARAM_PREFIXES，精确匹配的放 _TRACKING_PARAMS。
_TRACKING_PARAMS = frozenset(
    {
        # 通用归因
        "ref",
        "ref_src",
        "referrer",
        "source",
        "src",
        "from",
        "share_source",
        "share_medium",
        "share_plat",
        "share_tag",
        "share_token",
        "channel",
        "cid",
        # Google / Meta / 微软广告
        "gclid",
        "gclsrc",
        "dclid",
        "fbclid",
        "msclkid",
        "twclid",
        "igshid",
        "yclid",
        "s_kwcid",
        # 淘宝 / 天猫
        "spm",
        "scm",
        "ali_refid",
        "ali_trackid",
        # 微博 / 微信 / QQ
        "weibo_id",
        "sudaref",
        "scene",
        "subscene",
        # 邮件营销
        "mc_cid",
        "mc_eid",
        "mkt_tok",
        "_hsenc",
        "_hsmi",
        "vero_id",
    }
)

#: 前缀匹配的参数：这些家族的参数名带后缀变体，逐个列举必然漏
_TRACKING_PARAM_PREFIXES = ("utm_", "pk_", "mtm_", "piwik_", "hsa_", "at_", "ito_")

#: 路径归一里要折叠掉的重复分隔符
_MULTI_SLASH_RE = re.compile(r"/{2,}")


def is_tracking_param(name: str) -> bool:
    """该查询参数是否属于「删掉也不影响页面」的跟踪参数。"""
    lowered = (name or "").lower()
    if not lowered:
        return False
    if lowered in _TRACKING_PARAMS:
        return True
    return lowered.startswith(_TRACKING_PARAM_PREFIXES)


def normalize_url(url: str, *, strip_tracking: bool = True) -> str:
    """链接归一：去跟踪参数 + 查询参数排序 + 主机小写 + 去末尾斜杠。

    返回归一后的字符串；输入不是链接时**原样返回**，由调用方决定怎么用。
    刻意不做的事：不补全 scheme（`example.com/x` 与 `https://example.com/x`
    可能真是两个不同的东西，尤其在内网），不展开短链，不动 `#` 片段。
    """
    from .classify import is_url

    text = (url or "").strip()
    if not is_url(text):
        return url

    candidate = text if "://" in text else f"http://{text}"
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return url

    # 主机名统一小写：DNS 不区分大小写，`Example.COM` 与 `example.com` 是同一台机器
    netloc = parts.netloc.lower()

    if strip_tracking and parts.query:
        # keep_blank_values 保留 `?a=` 这种空值参数——空值也可能有语义
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        kept = [(key, value) for key, value in pairs if not is_tracking_param(key)]
        # 排序让 `?a=1&b=2` 与 `?b=2&a=1` 归一成同一个串。
        # 代价是改了参数顺序，但查询参数本就无序，服务端不该依赖顺序。
        kept.sort()
        query = urlencode(kept, doseq=True)
    else:
        query = parts.query

    path = parts.path
    # 末尾斜杠：`/a/b/` 与 `/a/b` 绝大多数站点指向同一页面。
    # 只在路径不止一个字符时去掉，避免把根路径 `/` 削成空串。
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    path = _MULTI_SLASH_RE.sub("/", path)

    # 无 scheme 的输入返回无 scheme 的归一结果，保持形状一致。
    # 不能直接把空 scheme 交给 urlunsplit——它会拼出 `//example.com/x`，
    # 多出的两个斜杠会让「归一后还等于原值」这个判断失效。
    if "://" not in text:
        rebuilt = netloc + path
        if query:
            rebuilt += "?" + query
        if parts.fragment:
            rebuilt += "#" + parts.fragment
        return rebuilt
    return urlunsplit((parts.scheme, netloc, path, query, parts.fragment))


def normalize_path(path: str) -> str:
    """路径归一：折叠重复分隔符、消解 `.` 与 `..`、去末尾斜杠。

    纯字符串运算，**不碰文件系统**：不 `resolve()`、不解析符号链接。
    剪贴板里的路径常常是「还不存在的目标文件」或「另一台机器上的路径」，
    真去 `resolve()` 会得到错误结果甚至卡在网络盘上。

    相对路径的 `..` 不越界消解：`../a` 不能变成 `a`，那是两个不同的位置。
    """
    text = (path or "").strip()
    if not text:
        return path

    # 同时处理 Windows 与 POSIX 分隔符：剪贴板内容可能来自另一平台
    looks_windows = bool(re.match(r"^[A-Za-z]:", text)) or "\\" in text
    sep = "\\" if looks_windows else "/"
    unified = text.replace("\\", "/")

    # UNC 前缀 `//server/share` 要保住，否则会被当成相对路径处理
    unc = unified.startswith("//")
    root = unified.startswith("/")
    drive = ""
    if looks_windows and re.match(r"^[A-Za-z]:", unified):
        drive, unified = unified[:2], unified[2:]
        root = unified.startswith("/")
        unified = unified.lstrip("/")

    parts: list = []
    for segment in unified.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            # 只有「上一个段还是普通目录名」时才允许回退；
            # 段列表为空或是另一个 `..` 时保留，避免相对路径越界
            if parts and parts[-1] != "..":
                parts.pop()
            elif not root:
                parts.append("..")
            continue
        parts.append(segment)

    body = "/".join(parts)
    if root:
        body = "/" + body
    if unc:
        body = "/" + body

    result = drive + body
    if not result:
        return path
    if sep == "\\":
        result = result.replace("/", "\\")
    return result


def canonical_content(kind: str, content: str, *, options: Optional[dict] = None) -> str:
    """按类型把内容归一成用于计算指纹的形式。

    只对**链接**与**路径**做归一；其余类型原样返回——文本里一个空格都可能
    是有意义的，不该由我们替用户决定它「等价于」别的什么。
    """
    options = options or {}
    text = content or ""
    if not text:
        return text

    if kind == "link" and options.get("url_strip_tracking", True):
        return normalize_url(text, strip_tracking=True)
    if kind == "file" and options.get("path_normalize", True):
        # 多行路径（一次复制多个文件）逐行归一，顺序保持不动：
        # 用户选文件的顺序可能是刻意的，排序反而会让指纹不稳定
        lines = text.splitlines()
        if len(lines) > 1:
            return "\n".join(normalize_path(line) if line.strip() else line for line in lines)
        return normalize_path(text)
    return text


def canonical_key(
    kind: str,
    content: str,
    *,
    files: Optional[Iterable[str]] = None,
    options: Optional[dict] = None,
) -> str:
    """生成归一后的指纹载荷。与 `collector._hash_payload()` 的形状保持一致。

    文件类型有 `files` 列表时以列表为准（与采集路径一致），否则退回 content。
    """
    options = options or {}
    if kind == "file" and files:
        joined = "\n".join(files)
        return canonical_content("file", joined, options=options)
    return canonical_content(kind, content, options=options)
