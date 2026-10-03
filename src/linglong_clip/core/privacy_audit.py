"""隐私体检：说清「这台机器上的脱敏规则，到底哪些真的会生效」。

存在的理由：这个项目反复出现同一类缺陷 —— 声明了但没人接。
脱敏这一块尤其危险，因为它的失败是**静默**的：

- 正则写错一个字，``PrivacyGuard.reload_rules()`` 只是打一条 warning 然后
  跳过这条规则。用户以为「我加了条规则保护工单号」，实际复制出去原样带走了。
- 用户自定义规则的入口只在配置文件里（``privacy.custom_redact_rules``），
  设置页此前根本没有编辑器 —— 文档却写着「设置 → 隐私 里可填」。
- ``PrivacyGuard.scan()`` 从一开始就写着「用于『隐私体检』面板」，
  但那个面板从未被建出来，这个方法没有任何读取方
  （同期的 ``describe_hits()`` 是 ``PrivacyVerdict.summary`` 的重复实现，
  也已一并删掉）。

所以这里补上那件事：拿一份样本内容过一遍真实的脱敏规则，
把「哪条命中了几次、哪条一次都没命中、哪条因为正则错被跳过了」摊开。
判据与 ``--rules``（体检自动化规则）一致 —— 都盯着「配了但不会生效」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .privacy import BUILTIN_REDACT_RULES, PrivacyGuard

#: 各类敏感信息的样本，用于演示某条规则到底会不会命中。
#:
#: 刻意用假数据：体检报告会连同样本一起打印出来，用真密钥当样本
#: 等于把体检工具本身变成一处泄露点。
_PROBES: Dict[str, str] = {
    "私钥": "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
    "API Key": "sk-abcdefghijklmnopqrstuvwx",
    "AWS 凭证": "AKIAIOSFODNN7EXAMPLE",
    "JWT": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop",
    "密码字段": "password: hunter2xyz",
    "身份证": "11010119900307123X",
    "银行卡": "4111 1111 1111 1111",
}

#: 自定义规则的默认探针：用户规则千变万化，没法定样本。
#: 用一句说明代替，让报告能写清「这条没样本可验，请自己试」。
_NO_PROBE_HINT = "（内置样本覆盖不到自定义规则，请用 --redact 拿真实内容试）"


@dataclass
class RuleCheck:
    """单条脱敏规则的体检结论。"""

    name: str
    pattern: str
    #: 内置规则还是用户自定义规则 —— 后者写错了才是用户能自己修的
    custom: bool
    #: 正则是否编译通过
    valid: bool = True
    #: 编译失败的原因
    error: str = ""
    #: 用样本试跑时命中的次数
    hits: int = 0
    #: 试跑用的样本（空表示没样本）
    probe: str = ""
    #: 不会生效的原因，空表示正常
    problems: List[str] = field(default_factory=list)
    #: 非问题的提示（如「没有样本可验」），不影响 will_run
    note: str = ""

    @property
    def will_run(self) -> bool:
        return not self.problems

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "pattern": self.pattern,
            "custom": self.custom,
            "valid": self.valid,
            "error": self.error,
            "hits": self.hits,
            "probe": self.probe,
            "will_run": self.will_run,
            "problems": list(self.problems),
            "note": self.note,
        }


@dataclass
class PrivacyAuditReport:
    """一次完整隐私体检的结果。"""

    checks: List[RuleCheck] = field(default_factory=list)
    #: 脱敏开关是否打开。关闭时所有规则都不会被执行 —— 这是最常见的
    #: 「规则配了但完全没生效」的原因，必须单独报出来。
    redact_enabled: bool = False
    #: 敏感内容是否落盘（retention > 0）
    sensitive_retained: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def invalid(self) -> List[RuleCheck]:
        return [item for item in self.checks if not item.valid]

    @property
    def useless(self) -> List[RuleCheck]:
        """有样本可验、但一次都没命中的自定义规则。

        只对自定义规则报「没命中」：内置规则各有各的用途，
        某个样本不命中很正常（比如样本里没有身份证）。而用户自己加的规则
        写了却从不命中，几乎总是写错了。
        """
        return [item for item in self.checks if item.custom and item.valid and item.probe and item.hits == 0]

    @property
    def healthy(self) -> bool:
        return not self.invalid and not self.useless

    @property
    def custom_count(self) -> int:
        return sum(1 for item in self.checks if item.custom)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "redact_enabled": self.redact_enabled,
            "sensitive_retained": self.sensitive_retained,
            "healthy": self.healthy,
            "custom_rules": self.custom_count,
            "checks": [item.to_dict() for item in self.checks],
            "notes": list(self.notes),
        }


def _probe_for(name: str, pattern: str) -> str:
    """给一条规则找个能验它的样本。

    先按规则名找内置样本；找不到时退而求其次 —— 把规则自己的正则里
    能看出来的字面量拼一份最小样本（``EMP\\d{6}`` → ``EMP123456``）。
    这么做是为了让「自定义规则有没有写对」也能被自动化验证，
    而不是把判断全推给用户。
    """
    if name in _PROBES:
        return _PROBES[name]
    literal = _literal_sample(pattern)
    return literal


def _literal_sample(pattern: str) -> str:
    """从正则里拼一份「最可能命中」的最小样本。

    只处理最常见的结构：字面量、字符类、转义类，以及紧跟其后的量词。
    拼不出来就返回空串，报告里会写「没有样本可验」，不会硬凑一个
    必然失败的样本去误报 —— 误报比不报更坏，用户会去改一条本来没错的规则。
    """
    sample: List[str] = []
    i = 0
    length = len(pattern)

    def _repeat(atom: str, quant: str) -> str:
        """按量词把单个字符重复到能通过的最小次数。"""
        if not atom:
            return ""
        if quant.startswith("{"):
            body = quant[1:-1]
            low = int(body.split(",")[0] or 1)
            return atom * max(low, 0)
        if quant in ("+", "*"):
            return atom  # `*` 取 1 次也满足；`+` 至少要一次
        return atom

    def _char_class(body: str) -> Optional[str]:
        if body.startswith("^"):
            # 否定字符类：挑一个不在排除集里的常见字符即可。
            # 排除集通常只有几个符号（如 `[^-]`），字母总能通过。
            for candidate in "abcdefghijklmnopqrstuvwxyz0123456789":
                if candidate not in body:
                    return candidate
            return None
        # 优先取一个大写字母、再小写、再数字，最后才是符号 ——
        # 这样 `[A-Z]{2}` 出来的是 "AA" 而不是 "  "，可读得多
        for group in (body, body, body):
            for candidate in group:
                if candidate.isupper():
                    return candidate
            for candidate in group:
                if candidate.islower():
                    return candidate
            for candidate in group:
                if candidate.isdigit():
                    return candidate
        for candidate in body:
            if candidate.isalnum():
                return candidate
        return None

    try:
        while i < length:
            char = pattern[i]
            atom = ""
            escaped = False

            if char == "\\" and i + 1 < length:
                nxt = pattern[i + 1]
                atom = {"d": "1", "w": "a", "s": " "}.get(nxt, nxt)
                i += 2
                escaped = True
            elif char == "[":
                end = pattern.find("]", i + 1)
                if end == -1:
                    return ""
                picked = _char_class(pattern[i + 1 : end])
                if picked is None:
                    return ""
                atom = picked
                i = end + 1
            elif char == "(" and i + 1 < length and pattern[i + 1] == "?":
                # 内联旗标组 `(?i)` / `(?im)`：消费掉整个 `(?...)`，
                # 否则 `?` 会被当成量词，样本里凭空多出一个字符
                end = pattern.find(")", i)
                if end == -1:
                    return ""
                i = end + 1
                continue
            elif char in "(){}|^$.*+?":
                # 分组、锚点、量词本身不产生字面量：跳到下一轮
                i += 1
                continue
            else:
                atom = char
                i += 1

            # 读紧跟的量词
            quant = ""
            if i < length and pattern[i] in "*+?":
                quant = pattern[i]
                i += 1
            elif i < length and pattern[i] == "{":
                end = pattern.find("}", i)
                if end == -1:
                    return ""
                quant = pattern[i : end + 1]
                i = end + 1

            sample.append(_repeat(atom, quant))

        text = "".join(sample)
        # 自己验一遍：拼出来的样本要是连自己都不命中，就是没拼对。
        if text and re.search(pattern, text):
            return text
        return ""
    except (re.error, ValueError, IndexError):
        return ""


def audit_privacy(config=None, guard: Optional[PrivacyGuard] = None) -> PrivacyAuditReport:
    """对当前生效的脱敏规则做一次体检。

    读取的是 ``PrivacyGuard`` 真正加载的那份规则（内置 + 用户自定义），
    而不是 ``config`` 里的原始文本 —— 用户最需要知道的是
    「实际装上的规则」是什么样，不是「我写了什么」。
    """
    guard = guard or PrivacyGuard(config)
    report = PrivacyAuditReport()

    if config is not None:
        report.redact_enabled = bool(config.get("privacy.redact_on_copy_out", False))
        report.sensitive_retained = (
            int(config.get("privacy.sensitive_retention_seconds", 0) or 0) > 0
        )

    custom_names = set()
    if config is not None:
        raw = config.get("privacy.custom_redact_rules", {}) or {}
        if isinstance(raw, dict):
            custom_names = {str(key) for key in raw}

    # 内置规则 + 配置里声明的自定义规则（含编译失败被 Guard 跳过的那些）。
    # 后者是关键：编译失败的规则不会出现在 guard._rules 里，
    # 只从 Guard 取就会漏报「我加的规则被静默跳过了」。
    declared: Dict[str, str] = dict(BUILTIN_REDACT_RULES)
    if config is not None:
        raw = config.get("privacy.custom_redact_rules", {}) or {}
        if isinstance(raw, dict):
            declared.update({str(key): str(value) for key, value in raw.items()})

    loaded = dict(getattr(guard, "_rules", {}) or {})

    for name, pattern in declared.items():
        is_custom = name in custom_names
        check = RuleCheck(name=name, pattern=pattern, custom=is_custom)
        compiled = loaded.get(name)
        try:
            compiled = compiled or re.compile(pattern)
        except re.error as exc:
            check.valid = False
            check.error = str(exc)
            check.problems.append(f"正则无法编译，这条规则已被静默跳过：{exc}")
            report.checks.append(check)
            continue

        probe = _probe_for(name, pattern)
        check.probe = probe
        if probe:
            try:
                check.hits = len(compiled.findall(probe))
            except re.error as exc:  # 编译过了但 findall 仍可能因分组报错
                check.valid = False
                check.error = str(exc)
                check.problems.append(f"匹配时出错：{exc}")
                report.checks.append(check)
                continue
            if check.hits == 0 and is_custom:
                check.problems.append(
                    f"用样本 {probe!r} 试跑一次都没命中 —— 规则可能写错了，"
                    "也可能只是样本不合，请用 --redact 拿真实内容确认"
                )
        else:
            # 没有样本可验不是「规则有问题」—— 只是自动体检帮不上忙，
            # 报成问题会让 healthy 与界面上的 ✗ 互相矛盾，用户白跑一趟。
            check.note = _NO_PROBE_HINT
        report.checks.append(check)

    # 脱敏总开关关着时，规则再对也不会执行。这是最容易被忽略的一条。
    if not report.redact_enabled and report.checks:
        report.notes.append(
            "「复制到外部时脱敏」当前是关闭的 —— 上面的规则一条都不会执行。"
            "在设置 → 隐私 里打开后才会生效。"
        )
    if report.sensitive_retained:
        report.notes.append(
            "敏感内容当前会记录到历史（sensitive_retention_seconds > 0），"
            "只是到期后清除。默认的 0 是「识别到就直接丢弃」。"
        )
    return report


def format_audit(report: PrivacyAuditReport) -> str:
    """人类可读输出。"""
    lines = ["玲珑剪切板 · 隐私体检", ""]
    lines.append(f"出站脱敏：{'已开启' if report.redact_enabled else '未开启'}")
    lines.append(
        "敏感内容："
        + ("标记后限时保留" if report.sensitive_retained else "识别到直接丢弃（默认）")
    )
    lines.append(f"生效规则：{len(report.checks)} 条（其中自定义 {report.custom_count} 条）")
    lines.append("")

    if not report.checks:
        lines.append("没有任何脱敏规则。")
        return "\n".join(lines)

    for check in report.checks:
        mark = "✓" if check.will_run else "✗"
        kind = "自定义" if check.custom else "内置"
        lines.append(f"{mark} {check.name}（{kind}）")
        lines.append(f"    正则：{check.pattern}")
        if check.probe and check.hits:
            lines.append(f"    样本命中 {check.hits} 次")
        elif check.probe:
            lines.append("    样本一次都没命中")
        else:
            lines.append("    没有可用于验证的样本")
        if check.note:
            lines.append(f"    · {check.note}")
        for problem in check.problems:
            lines.append(f"    ！{problem}")
        lines.append("")

    bad = [check for check in report.checks if not check.will_run]
    lines.append(f"共 {len(report.checks)} 条规则，其中 {len(bad)} 条需要处理")
    for note in report.notes:
        lines.append(f"· {note}")
    if bad:
        lines.append("写错的规则不会报错，只会静默地不生效 —— 按上面的原因修。")
    return "\n".join(lines)
