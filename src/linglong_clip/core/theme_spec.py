"""展示规格：列表密度这类「只影响呈现」的配置，它的合法取值从哪来。

存在理由是一次范围很小、但形状很典型的缺陷：

    ui.card_density 里的值会被一个会兜底的函数收敛掉
    （`normalize_density()`：非 compact 即 comfortable），
    而**写回配置**的那几条路径从不校验。于是配置文件里可以长期
    留着一个既不合法、界面上也选不出来的值：

        "card_density": "超级紧凑"     ← 界面里根本没有这个选项

    实际表现是「显示成舒适的密度」—— 也就是说，用户看不出任何异常，
    只有把配置文件打开才会发现里面躺着一个不存在的档位。
    主界面列表与快速粘贴浮层各自收敛一次，两处口径还很容易分叉。

这和前几轮修过的「声明了但没接上」是同一类：**界面、配置、消费方
三者的口径没有收在一处**。所以这里把「有哪些档位、哪个是合法值、
每个档位究竟是什么样」定义成一份带说明的清单，让配置校验、设置页
下拉、界面消费方、命令行报告都读同一份。

刻意放在 `core/` 而不是 `ui/`：分层约束要求 core 不导入 Qt，反过来
ui 依赖 core 是允许的；而且密度最终会出现在 `--ui-status` 这类
无图形环境的输出里。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

#: 默认密度。写成常量而不是在几处各写一遍字符串字面量 ——
#: 默认值散落时，改一处漏一处的表现是「设置页显示的和实际用的不是一档」。
DEFAULT_DENSITY = "comfortable"


@dataclass(frozen=True)
class DensityOption:
    """一个列表密度档位。"""

    value: str
    label: str
    #: 设置页下拉里的说明，同时也是这一档「到底改了什么」的定语
    detail: str
    #: 卡片里显示几行信息（类型徽标 + 摘要 + 元信息）
    lines: int


#: 档位清单。顺序即设置页下拉里的展示顺序，第一项是默认值。
DENSITY_OPTIONS: Tuple[DensityOption, ...] = (
    DensityOption(
        value=DEFAULT_DENSITY,
        label="舒适（三行信息）",
        detail="类型徽标 + 摘要 + 来源/时间/标签",
        lines=3,
    ),
    DensityOption(
        value="compact",
        label="紧凑（两行，一屏更多）",
        detail="类型徽标 + 摘要；来源/时间仍在悬停提示里",
        lines=2,
    ),
)

#: 合法档位集合，用于校验
DENSITY_VALUES = tuple(option.value for option in DENSITY_OPTIONS)

_BY_VALUE: Dict[str, DensityOption] = {option.value: option for option in DENSITY_OPTIONS}


def normalize_density(value: object) -> str:
    """把任意输入收敛到一个合法档位。

    大小写与空白不算错：`" COMPACT "` 与 `"compact"` 是同一个档位。
    其余一律落到默认档位 —— 但**这不是校验**，只保证消费方拿到的
    一定是合法值。配置里存的那个字符串该不该被改写，由
    `density_issue()` 报出来、由配置迁移去收敛，不要靠这里悄悄吃掉：
    静默收敛正是这次缺陷能长期存在的原因。
    """
    text = str(value or "").strip().lower()
    return text if text in _BY_VALUE else DEFAULT_DENSITY


def density_option(value: object) -> DensityOption:
    """取某个档位的完整说明（含展示文案），拿不到时给默认档位。"""
    return _BY_VALUE[normalize_density(value)]


def is_valid_density(value: object) -> bool:
    """这个值是不是一个存在的档位（不做兜底）。"""
    return str(value or "").strip().lower() in _BY_VALUE


def describe_density(value: object) -> str:
    """一句话说清这一档长什么样，用于设置页与命令行。"""
    return f"{density_option(value).label} · {density_option(value).detail}"


def density_issue(value: object) -> str:
    """配置值的体检结论；合法时返回空串。

    分成「空」与「不认识」两种说法：前者是没配过（用户从没动过或
    配置文件被手工删了这一行），后者是配了一个不存在的档位。
    两者的处理方式一样（都收敛到默认档位），但排查时想知道的
    完全不是同一件事。
    """
    if value is None or str(value).strip() == "":
        return f"未设置，将按默认档位「{density_option(None).label}」显示"
    if not is_valid_density(value):
        options = "、".join(DENSITY_VALUES)
        return f"'{value}' 不是有效的列表密度（可选：{options}），将按默认档位显示"
    return ""


def density_report(value: object) -> Dict[str, object]:
    """当前配置下「密度」这件事的完整来路，供 `--ui-status` 使用。"""
    option = density_option(value)
    issue = density_issue(value)
    return {
        "configured": "" if value is None else str(value),
        "effective": option.value,
        "label": option.label,
        "detail": option.detail,
        "lines": option.lines,
        "valid": is_valid_density(value),
        "issue": issue,
        "options": [
            {
                "value": item.value,
                "label": item.label,
                "detail": item.detail,
                "lines": item.lines,
                "default": item.value == DEFAULT_DENSITY,
            }
            for item in DENSITY_OPTIONS
        ],
    }


def format_density(report: Dict[str, object]) -> str:
    """把 `density_report()` 写成几行人话。"""
    lines = [
        "列表密度",
        f"  配置值：{report['configured'] or '（未设置）'}",
        f"  实际使用：{report['effective']}（{report['detail']}）",
    ]
    if report["issue"]:
        lines.append(f"  ！{report['issue']}")
    lines.append("  可选档位：")
    for item in report["options"]:
        mark = "*" if item["default"] else " "
        current = "（当前）" if item["value"] == report["effective"] else ""
        lines.append(
            f"   {mark} {item['value']:<12} {item['label']} · {item['detail']}{current}"
        )
    return "\n".join(lines)


__all__ = [
    "DEFAULT_DENSITY",
    "DENSITY_OPTIONS",
    "DENSITY_VALUES",
    "DensityOption",
    "describe_density",
    "density_issue",
    "density_option",
    "density_report",
    "format_density",
    "is_valid_density",
    "normalize_density",
]
