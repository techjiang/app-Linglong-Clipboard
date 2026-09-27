"""隐私与安全：来源过滤、内容脱敏、敏感项自动过期。

三条策略：
1. **来源黑名单**：密码管理器等应用的内容不入库；
2. **内容脱敏**：对外复制/同步时可把已识别的敏感片段替换为占位符；
3. **自动过期**：敏感记录按 TTL 从历史里清除，且默认不写图片附件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Pattern

from ..utils.logger import get_logger
from .classify import SENSITIVE_PATTERNS, is_password_manager
from .models import ClipboardItem

_log = get_logger("privacy")

#: 脱敏占位符模板
REDACTED = "[已脱敏:{name}]"

#: 全局兜底脱敏规则，用户自定义规则会叠加在其上
BUILTIN_REDACT_RULES: Dict[str, str] = {
    "私钥": "-----BEGIN [^-]*PRIVATE KEY-----[\\s\\S]*?-----END [^-]*PRIVATE KEY-----",
    "API Key": r"\b(?:sk|pk|ghp|gho|ghs|glpat|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b",
    "AWS 凭证": r"\bAKIA[0-9A-Z]{16}\b",
    "JWT": r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
    # 分组 1 = 字段名（保留可读性），分组 2 = 待脱敏的值
    "密码字段": (
        r"(?i)((?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key)"
        r"(?:\s*[:=]\s*))(\S{6,})"
    ),
    "身份证": r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b",
    "银行卡": r"\b(?:\d[ -]?){16,19}\b",
}

#: 整段命中这些模式即判定为「不该记录」，与 SENSITIVE_PATTERNS 的「标记」语义不同
_NEVER_RECORD: List[Pattern[str]] = [
    re.compile(r"-----BEGIN[^-]*PRIVATE KEY-----"),
]


@dataclass
class PrivacyVerdict:
    """对一条待入库内容的风控结论。"""

    allowed: bool = True
    sensitive: bool = False
    reasons: List[str] = field(default_factory=list)
    redacted_content: str = ""

    @property
    def summary(self) -> str:
        return "、".join(self.reasons) if self.reasons else "无风险"


class PrivacyGuard:
    """隐私策略执行器。

    所有判定集中在这里，采集管线与同步模块都调用同一份逻辑，
    避免「主流程过滤了、同步漏了」这类不一致。
    """

    def __init__(self, config=None) -> None:
        self._config = config
        self._rules: Dict[str, Pattern[str]] = {}
        self.reload_rules()

    # ---- 配置 ----

    def _cfg(self, key: str, default):
        if self._config is None:
            return default
        return self._config.get(key, default)

    def reload_rules(self) -> None:
        """重建脱敏正则，含用户自定义规则。"""
        rules: Dict[str, str] = dict(BUILTIN_REDACT_RULES)
        custom = self._cfg("privacy.custom_redact_rules", {}) or {}
        if isinstance(custom, dict):
            rules.update({str(k): str(v) for k, v in custom.items()})
        compiled: Dict[str, Pattern[str]] = {}
        for name, pattern in rules.items():
            try:
                compiled[name] = re.compile(pattern)
            except re.error as exc:
                _log.warning("脱敏规则 %s 无效，已跳过: %s", name, exc)
        self._rules = compiled

    # ---- 来源过滤 ----

    def is_blocked_source(self, app_id: str, window_title: str = "") -> bool:
        """来源是否在黑名单里（含内置的密码管理器判定）。"""
        blocked = self._cfg("privacy.blocked_apps", []) or []
        haystack = f"{app_id} {window_title}".lower()
        for entry in blocked:
            if entry and str(entry).lower() in haystack:
                return True
        if self._cfg("privacy.exclude_password_managers", True):
            if is_password_manager(app_id) or is_password_manager(window_title):
                return True
            # 标题里出现「密码/密钥」等词时也拦，覆盖未识别的管理器
            if re.search(r"(密码|密钥|password|passwd|keychain|vault)", window_title, re.I):
                return True
        return False

    # ---- 内容风控 ----

    def inspect(self, item: ClipboardItem) -> PrivacyVerdict:
        """判定一条内容能否入库，以及是否需要标记为敏感。"""
        text = item.content or ""
        reasons: List[str] = []

        for pattern in _NEVER_RECORD:
            if pattern.search(text):
                return PrivacyVerdict(
                    allowed=False,
                    sensitive=True,
                    reasons=["包含私钥，拒绝记录"],
                )

        if item.source_app and self.is_blocked_source(item.source_app, item.source_title):
            return PrivacyVerdict(
                allowed=False,
                sensitive=True,
                reasons=[f"来源在黑名单：{item.source_app}"],
            )

        for name, pattern in SENSITIVE_PATTERNS:
            try:
                if pattern.search(text):
                    reasons.append(name)
            except re.error:
                continue

        sensitive = bool(reasons)
        verdict = PrivacyVerdict(allowed=True, sensitive=sensitive, reasons=reasons)
        if sensitive and self._cfg("privacy.redact_on_copy_out", False):
            verdict.redacted_content = self.redact(text)
        return verdict

    def redact(self, text: str) -> str:
        """把所有命中的敏感片段替换成占位符。"""
        if not text:
            return text
        result = text
        for name, pattern in self._rules.items():
            try:
                if pattern.groups >= 2:
                    # 「字段: 值」形式只替换值，保留字段名可读性
                    result = pattern.sub(
                        lambda match, _n=name: (
                            f"{match.group(1)}{REDACTED.format(name=_n)}"
                        ),
                        result,
                    )
                else:
                    result = pattern.sub(REDACTED.format(name=name), result)
            except re.error:
                continue
        return result

    def scan(self, text: str) -> Dict[str, int]:
        """统计各类敏感信息的命中次数，用于「隐私体检」面板。"""
        found: Dict[str, int] = {}
        for name, pattern in self._rules.items():
            try:
                matches = pattern.findall(text or "")
            except re.error:
                continue
            if matches:
                found[name] = len(matches)
        return found

    def auto_clear_seconds(self) -> int:
        return int(self._cfg("privacy.sensitive_auto_clear_seconds", 30) or 0)

    def should_keep_sensitive(self) -> bool:
        """敏感内容是否允许入库（入库后由 TTL 清除）。"""
        ttl = self._cfg("privacy.sensitive_retention_seconds", 0) or 0
        return int(ttl) > 0

    @staticmethod
    def describe_hits(reasons: List[str]) -> str:
        return "、".join(reasons) if reasons else "无"
