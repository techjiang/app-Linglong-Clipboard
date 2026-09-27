"""自动化：把剪切板内容按规则加工后回写。

这是「玲珑」相对传统剪切板的差异点之一 —— 复制一段 JSON 顺手格式化、
复制一堆路径自动去重、复制日志自动提取时间戳，都不需要离开当前应用。

规则用纯数据描述（JSON），因此可以从配置文件热加载，
用户能在设置界面里增删改而不用改代码。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..utils.logger import get_logger
from .models import CODE, FILE, LINK, TEXT, ClipboardItem

_log = get_logger("automation")

#: 触发方式
TRIGGER_MANUAL = "manual"
TRIGGER_AUTO = "auto"

#: 加工器的返回约定：返回 None 表示「这条不适合该转换」，跳过
Transformer = Callable[..., Optional[str]]


class TransformError(Exception):
    """加工失败，附带给用户看的原因。"""


# ---- 内置转换器 ----


def _json_format(text: str, indent: int = 2, sort_keys: bool = False) -> Optional[str]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransformError(f"不是合法 JSON：{exc.msg}") from exc
    return json.dumps(parsed, ensure_ascii=False, indent=int(indent), sort_keys=bool(sort_keys))


def _json_minify(text: str) -> Optional[str]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransformError(f"不是合法 JSON：{exc.msg}") from exc
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def _url_decode(text: str) -> Optional[str]:
    if "%" not in text:
        return None
    decoded = urllib.parse.unquote_plus(text.strip())
    return decoded if decoded != text else None


def _url_encode(text: str) -> Optional[str]:
    return urllib.parse.quote(text.strip(), safe="")


def _url_strip_tracking(text: str) -> Optional[str]:
    """去掉链接里的 utm_*、fbclid 等跟踪参数，保留下来的才是有效信息。"""
    stripped = text.strip()
    if not stripped.startswith(("http://", "https://")):
        return None
    parsed = urllib.parse.urlsplit(stripped)
    if not parsed.query:
        return None
    tracking_prefixes = ("utm_", "fbclid", "gclid", "mc_", "ref_", "igshid", "spm")
    kept = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith(tracking_prefixes)
    ]
    if len(kept) == len(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)):
        return None
    query = urllib.parse.urlencode(kept)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment)
    )


def _base64_encode(text: str) -> Optional[str]:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _base64_decode(text: str) -> Optional[str]:
    candidate = text.strip()
    if not re.fullmatch(r"[A-Za-z0-9+/=\s]{8,}", candidate):
        return None
    try:
        return base64.b64decode(candidate, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise TransformError("不是合法的 Base64 文本") from None


def _lines_sort(text: str, reverse: bool = False) -> Optional[str]:
    lines = text.splitlines()
    if len(lines) < 2:
        return None
    return "\n".join(sorted(lines, reverse=bool(reverse)))


def _lines_unique(text: str) -> Optional[str]:
    lines = text.splitlines()
    if len(lines) < 2:
        return None
    seen: List[str] = []
    for line in lines:
        if line not in seen:
            seen.append(line)
    return "\n".join(seen) if len(seen) != len(lines) else None


def _lines_trim(text: str) -> Optional[str]:
    lines = [line.strip() for line in text.splitlines()]
    result = "\n".join(line for line in lines if line)
    return result if result != text else None


def _lines_number(text: str) -> Optional[str]:
    lines = text.splitlines()
    if len(lines) < 2:
        return None
    width = len(str(len(lines)))
    numbered = "\n".join(
        f"{str(index).rjust(width)} | {line}" for index, line in enumerate(lines, 1)
    )
    return numbered if numbered != text else None


def _csv_to_json(text: str, delimiter: str = ",") -> Optional[str]:
    rows = [line for line in text.splitlines() if line.strip()]
    if len(rows) < 2:
        return None
    header = [cell.strip() for cell in rows[0].split(delimiter)]
    if len(header) < 2:
        return None
    records: List[Dict[str, str]] = []
    for line in rows[1:]:
        cells = [cell.strip() for cell in line.split(delimiter)]
        records.append(dict(zip(header, cells)))
    return json.dumps(records, ensure_ascii=False, indent=2)


def _csv_to_markdown(text: str, delimiter: str = ",") -> Optional[str]:
    rows = [[cell.strip() for cell in line.split(delimiter)] for line in text.splitlines()]
    rows = [row for row in rows if any(row)]
    if len(rows) < 2 or len(rows[0]) < 2:
        return None
    width = len(rows[0])
    normalized = [row + [""] * (width - len(row)) for row in rows if len(row) <= width]
    header = normalized[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in normalized[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _case_snake(text: str) -> Optional[str]:
    words = _split_words(text)
    return "_".join(word.lower() for word in words) if words else None


def _case_camel(text: str) -> Optional[str]:
    words = _split_words(text)
    if not words:
        return None
    return words[0].lower() + "".join(word.capitalize() for word in words[1:])


def _case_pascal(text: str) -> Optional[str]:
    words = _split_words(text)
    return "".join(word.capitalize() for word in words) if words else None


def _case_kebab(text: str) -> Optional[str]:
    words = _split_words(text)
    return "-".join(word.lower() for word in words) if words else None


def _split_words(text: str) -> List[str]:
    if "\n" in text or len(text) > 200:
        return []
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text.strip())
    return [word for word in re.split(r"[\s_\-./]+", spaced) if word]


def _path_basenames(text: str) -> Optional[str]:
    """从一堆路径里只留下文件名，适合往聊天窗口里贴。"""
    import os

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 1:
        return None
    names = [os.path.basename(line.rstrip("/\\")) or line for line in lines]
    result = "\n".join(names)
    return result if result != text else None


def _path_quoted(text: str, quote: str = '"') -> Optional[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    result = "\n".join(f"{quote}{line}{quote}" for line in lines)
    return result if result != text else None


def _escape_shell(text: str) -> Optional[str]:
    return "'" + text.replace("'", "'\\''") + "'"


def _extract_emails(text: str) -> Optional[str]:
    found = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    if not found:
        return None
    return "\n".join(dict.fromkeys(found))


def _extract_urls(text: str) -> Optional[str]:
    found = re.findall(r"https?://[^\s<>\"')]+", text)
    if not found:
        return None
    return "\n".join(dict.fromkeys(found))


def _extract_numbers(text: str) -> Optional[str]:
    found = re.findall(r"-?\d+(?:\.\d+)?", text)
    if not found:
        return None
    return "\n".join(found)


def _extract_ips(text: str) -> Optional[str]:
    found = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    if not found:
        return None
    return "\n".join(dict.fromkeys(found))


def _hash_sha256(text: str) -> Optional[str]:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_md5(text: str) -> Optional[str]:
    return hashlib.md5(text.encode("utf-8")).hexdigest()  # noqa: S324 - 仅作校验和用途


def _text_stats(text: str) -> Optional[str]:
    words = len(re.findall(r"[A-Za-z0-9_]+", text))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    lines = text.count("\n") + 1
    return (
        f"字符数: {len(text)}\n"
        f"行数: {lines}\n"
        f"英文词数: {words}\n"
        f"中文字数: {cjk}\n"
        f"字节数: {len(text.encode('utf-8'))}"
    )


def _swap_case(text: str) -> Optional[str]:
    return text.swapcase()


def _reverse(text: str) -> Optional[str]:
    return text[::-1]


def _strip_whitespace(text: str) -> Optional[str]:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    result = "\n".join(line for line in lines if line)
    return result if result != text else None


def _markdown_quote(text: str) -> Optional[str]:
    lines = text.splitlines()
    if not lines:
        return None
    return "\n".join(f"> {line}" for line in lines)


def _markdown_table_from_tsv(text: str) -> Optional[str]:
    return _csv_to_markdown(text, delimiter="\t")


def _yaml_from_json(text: str) -> Optional[str]:
    """JSON → YAML。没装 pyyaml 时用内置的极简序列化。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransformError(f"不是合法 JSON：{exc.msg}") from exc
    try:
        import yaml  # type: ignore

        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    except ImportError:
        return _tiny_yaml_dump(data)


def _tiny_yaml_dump(value: Any, indent: int = 0) -> str:
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return f"{pad}{{}}"
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:")
                lines.append(_tiny_yaml_dump(item, indent + 2))
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return f"{pad}[]"
        lines = []
        for item in value:
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}-")
                lines.append(_tiny_yaml_dump(item, indent + 2))
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
        return "\n".join(lines)
    return f"{pad}{_yaml_scalar(value)}"


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or re.search(r"[:#\[\]{}&*!|>'\"%@`]", text) or text.strip() != text:
        return json.dumps(text, ensure_ascii=False)
    return text


# ---- 注册表 ----

@dataclass
class Action:
    """一个可执行的动作。"""

    key: str
    name: str
    transform: Transformer
    #: 适用的载荷类型，空列表表示全部
    kinds: List[str] = field(default_factory=list)
    category: str = "通用"
    description: str = ""
    params: Dict[str, Any] = field(default_factory=dict)

    def applies_to(self, kind: str) -> bool:
        return not self.kinds or kind in self.kinds


def _build_registry() -> Dict[str, Action]:
    actions = [
        Action("json.format", "格式化 JSON", _json_format, [CODE, TEXT], "数据",
               "缩进重排，便于阅读"),
        Action("json.minify", "压缩 JSON", _json_minify, [CODE, TEXT], "数据",
               "去掉所有多余空白"),
        Action("json.to_yaml", "JSON 转 YAML", _yaml_from_json, [CODE, TEXT], "数据", ""),
        Action("csv.to_json", "CSV 转 JSON", _csv_to_json, [TEXT], "数据", ""),
        Action("csv.to_markdown", "CSV 转表格", _csv_to_markdown, [TEXT], "数据", ""),
        Action("tsv.to_markdown", "TSV 转表格", _markdown_table_from_tsv, [TEXT], "数据", ""),
        Action("url.decode", "URL 解码", _url_decode, [LINK, TEXT], "链接", ""),
        Action("url.encode", "URL 编码", _url_encode, [LINK, TEXT], "链接", ""),
        Action("url.strip_tracking", "去掉跟踪参数", _url_strip_tracking, [LINK], "链接",
               "清理 utm_* / fbclid 等"),
        Action("encode.base64", "Base64 编码", _base64_encode, [TEXT, CODE], "编码", ""),
        Action("encode.base64_decode", "Base64 解码", _base64_decode, [TEXT, CODE], "编码", ""),
        Action("encode.sha256", "计算 SHA-256", _hash_sha256, [TEXT, CODE], "编码", ""),
        Action("encode.md5", "计算 MD5", _hash_md5, [TEXT, CODE], "编码", ""),
        Action("lines.sort", "按行排序", _lines_sort, [TEXT, CODE, FILE], "文本", ""),
        Action("lines.unique", "按行去重", _lines_unique, [TEXT, CODE, FILE], "文本", ""),
        Action("lines.trim", "清理空行", _lines_trim, [TEXT, CODE, FILE], "文本", ""),
        Action("lines.number", "加行号", _lines_number, [TEXT, CODE, FILE], "文本", ""),
        Action("text.trim_space", "压缩空白", _strip_whitespace, [TEXT, CODE], "文本", ""),
        Action("text.swap_case", "大小写互换", _swap_case, [TEXT], "文本", ""),
        Action("text.reverse", "反转文本", _reverse, [TEXT], "文本", ""),
        Action("text.stats", "统计字数", _text_stats, [TEXT, CODE], "文本",
               "字符/行/词数统计"),
        Action("text.quote", "转为引用块", _markdown_quote, [TEXT, CODE], "文本", ""),
        Action("case.snake", "转 snake_case", _case_snake, [TEXT], "命名", ""),
        Action("case.camel", "转 camelCase", _case_camel, [TEXT], "命名", ""),
        Action("case.pascal", "转 PascalCase", _case_pascal, [TEXT], "命名", ""),
        Action("case.kebab", "转 kebab-case", _case_kebab, [TEXT], "命名", ""),
        Action("path.basename", "只留文件名", _path_basenames, [FILE, TEXT], "路径", ""),
        Action("path.quote", "路径加引号", _path_quoted, [FILE, TEXT], "路径", ""),
        Action("shell.escape", "Shell 转义", _escape_shell, [TEXT, FILE, CODE], "路径", ""),
        Action("extract.email", "提取邮箱", _extract_emails, [TEXT], "提取", ""),
        Action("extract.url", "提取链接", _extract_urls, [TEXT], "提取", ""),
        Action("extract.number", "提取数字", _extract_numbers, [TEXT], "提取", ""),
        Action("extract.ip", "提取 IP", _extract_ips, [TEXT], "提取", ""),
    ]
    return {action.key: action for action in actions}


REGISTRY: Dict[str, Action] = _build_registry()


def list_actions(kind: Optional[str] = None) -> List[Action]:
    actions = list(REGISTRY.values())
    if kind:
        actions = [action for action in actions if action.applies_to(kind)]
    return actions


# ---- 规则引擎 ----


@dataclass
class Rule:
    """自动加工规则。

    匹配条件全部满足才触发；多个动作按顺序链式执行。
    """

    name: str = ""
    enabled: bool = True
    #: 匹配的载荷类型
    kinds: List[str] = field(default_factory=list)
    #: 内容必须匹配的正则
    pattern: str = ""
    #: 内容必须包含的子串
    contains: str = ""
    #: 来源应用需包含的子串
    source_contains: str = ""
    #: 内容长度下限
    min_length: int = 0
    actions: List[str] = field(default_factory=list)
    action_params: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: 触发方式：auto 表示采集时自动执行
    trigger: str = TRIGGER_MANUAL

    def matches(self, item: ClipboardItem) -> bool:
        if not self.enabled:
            return False
        if self.kinds and item.kind not in self.kinds:
            return False
        if self.min_length and len(item.content) < self.min_length:
            return False
        if self.contains and self.contains not in item.content:
            return False
        if self.source_contains and self.source_contains.lower() not in (
            item.source_app or ""
        ).lower():
            return False
        if self.pattern:
            try:
                if not re.search(self.pattern, item.content, re.MULTILINE):
                    return False
            except re.error:
                return False
        return True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Rule":
        return cls(
            name=str(data.get("name", "")),
            enabled=bool(data.get("enabled", True)),
            kinds=list(data.get("kinds") or []),
            pattern=str(data.get("pattern", "")),
            contains=str(data.get("contains", "")),
            source_contains=str(data.get("source_contains", "")),
            min_length=int(data.get("min_length", 0) or 0),
            actions=list(data.get("actions") or []),
            action_params=dict(data.get("action_params") or {}),
            trigger=str(data.get("trigger", TRIGGER_MANUAL)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "kinds": self.kinds,
            "pattern": self.pattern,
            "contains": self.contains,
            "source_contains": self.source_contains,
            "min_length": self.min_length,
            "actions": self.actions,
            "action_params": self.action_params,
            "trigger": self.trigger,
        }


@dataclass
class AppliedResult:
    """一次加工的产物。"""

    action_key: str
    output: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class AutomationEngine:
    """动作执行 + 规则匹配。

    线程安全：规则可能在设置界面被改，而采集线程正在读。
    """

    def __init__(self, rules: Optional[List[Rule]] = None) -> None:
        self._lock = threading.RLock()
        self._rules: List[Rule] = list(rules or [])

    # ---- 规则管理 ----

    def load_from_config(self, raw: List[Dict[str, Any]]) -> int:
        rules = [Rule.from_dict(entry) for entry in raw if isinstance(entry, dict)]
        with self._lock:
            self._rules = rules
        return len(rules)

    def rules(self) -> List[Rule]:
        with self._lock:
            return list(self._rules)

    def add_rule(self, rule: Rule) -> None:
        with self._lock:
            self._rules.append(rule)

    def remove_rule(self, name: str) -> bool:
        with self._lock:
            before = len(self._rules)
            self._rules = [rule for rule in self._rules if rule.name != name]
            return len(self._rules) != before

    def to_config(self) -> List[Dict[str, Any]]:
        return [rule.to_dict() for rule in self.rules()]

    # ---- 执行 ----

    def apply(self, action_key: str, item: ClipboardItem, **params: Any) -> AppliedResult:
        """对单条内容执行一个动作。"""
        action = REGISTRY.get(action_key)
        if action is None:
            return AppliedResult(action_key, "", f"未知动作：{action_key}")
        if not item.content:
            return AppliedResult(action_key, "", "内容为空，无可加工")
        if not action.applies_to(item.kind):
            return AppliedResult(
                action_key, "", f"{action.name} 不适用于「{item.kind_label}」"
            )

        merged = dict(action.params)
        merged.update(params)
        try:
            output = action.transform(item.content, **merged)
        except TransformError as exc:
            return AppliedResult(action_key, "", str(exc))
        except TypeError as exc:
            return AppliedResult(action_key, "", f"参数不匹配：{exc}")
        except Exception as exc:  # noqa: BLE001 - 单个动作失败不该影响界面
            _log.exception("动作 %s 执行失败", action_key)
            return AppliedResult(action_key, "", f"执行失败：{exc}")

        if output is None:
            return AppliedResult(action_key, "", "内容不适用于该转换")
        return AppliedResult(action_key, output)

    def apply_chain(self, action_keys: List[str], item: ClipboardItem) -> AppliedResult:
        """链式执行：上一个的输出作为下一个的输入。"""
        current = item.content
        chain = [item.kind]
        for index, key in enumerate(action_keys):
            probe = ClipboardItem(
                kind=item.kind, content=current, preview=current[:200], hash=item.hash
            )
            result = self.apply(key, probe)
            if not result.ok:
                return AppliedResult(
                    key, "", f"链路第 {index + 1} 步（{key}）失败：{result.error}"
                )
            current = result.output
        return AppliedResult("+".join(action_keys), current)

    def auto_actions_for(self, item: ClipboardItem) -> List[AppliedResult]:
        """找出所有 `auto` 规则并执行，用于采集后自动加工。"""
        results: List[AppliedResult] = []
        for rule in self.rules():
            if rule.trigger != TRIGGER_AUTO or not rule.matches(item):
                continue
            if len(rule.actions) == 1:
                params = rule.action_params.get(rule.actions[0], {})
                results.append(self.apply(rule.actions[0], item, **params))
            else:
                results.append(self.apply_chain(rule.actions, item))
        return results

    def suggest_for(self, item: ClipboardItem) -> List[Action]:
        """给一条内容推荐可用动作：先看类型适配，再看规则偏好。"""
        candidates = list_actions(item.kind)
        # 规则里出现过的动作排在前面，符合用户习惯
        preferred: List[str] = []
        for rule in self.rules():
            if not rule.enabled or not rule.matches(item):
                continue
            for key in rule.actions:
                if key not in preferred:
                    preferred.append(key)

        def sort_key(action: Action) -> tuple:
            return (0 if action.key in preferred else 1, action.category, action.name)

        return sorted(candidates, key=sort_key)
