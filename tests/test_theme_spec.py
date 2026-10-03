"""列表密度这类「只影响呈现」的配置：三处口径必须收在同一份定义里。

这一组用例守的是一条边界 —— **配置里写的、界面上能选的、实际生效的
必须是同一件事**。此前的形态是：配置项可以存任意字符串，设置页下拉里
只有两个选项，而消费点各自兜底。兜底本身是对的（界面不能崩），
但它是**静默**的：`"card_density": "超级紧凑"` 会让界面显示成默认档位，
却没有任何地方说得出「这个值不存在」。用户只能靠翻配置文件发现。
"""

from __future__ import annotations

import json

import pytest

from linglong_clip.core.theme_spec import (
    DEFAULT_DENSITY,
    DENSITY_OPTIONS,
    DENSITY_VALUES,
    density_issue,
    density_option,
    density_report,
    describe_density,
    format_density,
    is_valid_density,
    normalize_density,
)


# ---- 清单本身 ----

def test_default_density_is_in_the_catalog():
    """默认值必须是清单里的第一项。

    设置页下拉的第一项就是默认值（索引回退到 0），清单与默认值分叉时，
    「配置没配过」和「配成了第一项」会指向两个不同的档位。
    """
    assert DENSITY_VALUES[0] == DEFAULT_DENSITY
    assert is_valid_density(DEFAULT_DENSITY)


def test_catalog_has_unique_values_and_labels():
    values = [option.value for option in DENSITY_OPTIONS]
    labels = [option.label for option in DENSITY_OPTIONS]
    assert len(values) == len(set(values))
    assert len(labels) == len(set(labels))


def test_every_option_explains_what_it_changes():
    """每一项都要说明「改了什么」。

    下拉里只有「舒适 / 紧凑」两个词时，用户没有依据判断该选哪个 ——
    而这一项恰好是「改了但看不出改了哪里」最容易发生的地方。
    """
    for option in DENSITY_OPTIONS:
        assert option.detail, f"{option.value} 缺少说明"
        assert option.lines >= 2


# ---- 收敛（消费方拿到的值一定合法） ----

@pytest.mark.parametrize(
    "value",
    ["compact", " COMPACT ", "Compact", "\tcompact\n"],
)
def test_normalize_accepts_case_and_whitespace(value):
    assert normalize_density(value) == "compact"


@pytest.mark.parametrize("value", ["超级紧凑", "dense", "", None, 123, "comfortable "])
def test_normalize_falls_back_to_default(value):
    """非 compact 一律落到默认档位，且绝不返回清单外的值。"""
    result = normalize_density(value)
    assert result in DENSITY_VALUES


def test_normalize_is_idempotent():
    for value in ["compact", "COMPACT", "乱填", "", None]:
        once = normalize_density(value)
        assert normalize_density(once) == once


# ---- 体检（合法性单独判定，不与收敛混为一谈） ----

def test_valid_values_have_no_issue():
    for value in DENSITY_VALUES:
        assert is_valid_density(value)
        assert density_issue(value) == ""


def test_empty_value_is_reported_differently_from_unknown():
    """「没配过」与「配了个不存在的档位」必须分开说。

    处理方式一样（都按默认档位显示），但排查时想知道的完全不是同一件事：
    前者是没动过，后者是有人写进了一个不存在的值。
    """
    empty = density_issue("")
    assert "未设置" in empty
    assert "不是有效" not in empty

    unknown = density_issue("超级紧凑")
    assert "不是有效" in unknown
    # 报错信息里要给出可选值，否则用户不知道改成什么才算对
    for value in DENSITY_VALUES:
        assert value in unknown

    assert density_issue(None) == empty


def test_issue_reports_the_original_value_verbatim():
    """原值要原样带出来。

    写成「配置无效」的话，用户拿着这句话既不知道哪个键、
    也不知道原来写的是什么，只能自己去翻文件对比。
    """
    assert "超级紧凑" in density_issue("超级紧凑")


# ---- 报告（`--ui-status` 的数据源） ----

def test_report_separates_configured_from_effective():
    report = density_report("乱填的档位")
    assert report["configured"] == "乱填的档位"
    assert report["effective"] == DEFAULT_DENSITY
    assert report["valid"] is False
    assert report["issue"]


def test_report_lists_every_option_with_default_marked():
    report = density_report("compact")
    assert [item["value"] for item in report["options"]] == list(DENSITY_VALUES)
    defaults = [item["value"] for item in report["options"] if item["default"]]
    assert defaults == [DEFAULT_DENSITY]
    assert {item["value"] for item in report["options"]} == set(DENSITY_VALUES)


def test_report_is_json_serialisable():
    """`--ui-status --json` 要能直接 `json.dumps` 出去。"""
    payload = json.dumps(density_report("乱填"), ensure_ascii=False)
    assert "effective" in payload


def test_format_report_marks_current_and_invalid():
    text = format_density(density_report("乱填"))
    assert "实际使用" in text
    assert DEFAULT_DENSITY in text
    # 非法值必须当场标出来，不能只写「可选档位」让人自己对照
    assert "不是有效" in text
    # 当前生效的那一档要有标记
    assert "（当前）" in text


def test_describe_and_option_agree():
    for value in DENSITY_VALUES:
        option = density_option(value)
        assert describe_density(value) == f"{option.label} · {option.detail}"


# ---- 与配置的联动：脏值在打开时被清洗并落盘 ----

def test_config_normalizes_dirty_density_on_load(tmp_path):
    """配置里躺着一个不存在的档位时，打开就被改写并落盘。

    只靠消费点兜底的话，配置文件里那个值会一直留着 —— 用户看不出异常，
    只有打开文件才会发现。所以清洗必须在**打开时**做一次。
    """
    from linglong_clip.utils.config import Config

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"ui": {"card_density": "超级紧凑"}, "meta": {"schema_version": 1}}),
        encoding="utf-8",
    )
    config = Config(path)
    assert config.get("ui.card_density") == DEFAULT_DENSITY
    assert json.loads(path.read_text(encoding="utf-8"))["ui"]["card_density"] == DEFAULT_DENSITY


def test_config_keeps_valid_density_untouched(tmp_path):
    from linglong_clip.utils.config import Config

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"ui": {"card_density": "compact"}, "meta": {"schema_version": 1}}),
        encoding="utf-8",
    )
    config = Config(path)
    assert config.get("ui.card_density") == "compact"


def test_config_writes_back_uppercase_as_lowercase(tmp_path):
    """`COMPACT` 是合法档位（大小写不敏感），落盘时统一成小写。

    留着大写也不算错，但那样「同一档位有两种写法」，比较与去重都得多带
    一层归一，不如在入口统一。
    """
    from linglong_clip.utils.config import Config

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"ui": {"card_density": "COMPACT"}, "meta": {"schema_version": 1}}),
        encoding="utf-8",
    )
    config = Config(path)
    assert config.get("ui.card_density") == "compact"


def test_config_without_density_key_is_left_alone(tmp_path):
    """没配过的键不该被「清洗」凭空写出来。

    `_deep_merge(DEFAULTS, ...)` 本来就会补上默认值，但那是补默认值，
    不是「修正非法值」—— 两者都写文件的话，「为什么我的配置文件里
    多了一行我没写过的键」会变成一个没人能回答的问题。
    """
    from linglong_clip.utils.config import Config

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"meta": {"schema_version": 1}}), encoding="utf-8")
    config = Config(path)
    assert config.get("ui.card_density") == DEFAULT_DENSITY


# ---- 界面消费方：卡片按同一份清单解释密度 ----

@pytest.mark.parametrize("value", ["compact", "COMPACT", "乱填", "", None])
def test_item_card_accepts_arbitrary_input(storage, qt_app, value):
    """卡片构造时给任何值都不能抛异常，且状态一定落在清单内。"""
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.ui.item_card import ItemCard
    from linglong_clip.ui.theme import palette_for

    item = ClipboardItem(id=1, kind="text", content="一条记录", source_app="chrome")
    card = ItemCard(item, palette_for("light"), density=value)
    assert card.is_compact == (normalize_density(value) == "compact")
