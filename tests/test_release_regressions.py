"""回归测试：钉住 1.0 发布前修掉的一批行为错误。

每条用例对应一个实测复现过的现象，注释里写清「当时是什么表现」，
避免以后有人把修复当成冗余代码删掉。
"""

from __future__ import annotations

import time

from linglong_clip.core import classify
from linglong_clip.core.collector import CAPTURED, Collector
from linglong_clip.core.models import ClipboardItem, content_hash
from linglong_clip.core.privacy import PrivacyGuard
from linglong_clip.core.semantic import SemanticEngine, parse_intent


def _item(content: str, kind: str = "text", **kwargs) -> ClipboardItem:
    return ClipboardItem(
        kind=kind, content=content, hash=content_hash(kind, content or "x"), **kwargs
    )


# ---- 隐私：弱敏感规则不该把内容整条吃掉 ----


def test_email_only_is_not_blocking():
    """现象：复制带邮箱的会议纪要，记录直接从历史里消失。

    邮箱/手机号属于「内容里有个人信息」，不是「这条内容本身是秘密」。
    """
    assert classify.detect_sensitive("联系 alice@example.com 讨论方案") == ["邮箱"]
    assert classify.is_blocking_sensitivity(["邮箱"]) is False


def test_phone_only_is_not_blocking():
    assert classify.is_blocking_sensitivity(classify.detect_sensitive("电话 13800138000")) is False


def test_api_key_is_blocking():
    hits = classify.detect_sensitive("token=ghp_abcdefghijklmnopqrstuvwxyz0123")
    assert hits
    assert classify.is_blocking_sensitivity(hits) is True


def test_card_number_needs_financial_context():
    """现象：复制 16 位订单号被当成银行卡，内容不进历史。"""
    assert "银行卡" not in classify.detect_sensitive("订单号 1234567890123456 已发货")
    assert "银行卡" in classify.detect_sensitive("请把款打到银行卡 6222 0212 3456 7890")


def test_analyze_keeps_kind_for_weak_hits():
    assert classify.analyze("联系 alice@example.com 讨论方案")["kind"] == "text"


def test_guard_marks_but_allows_weak_sensitivity(config):
    guard = PrivacyGuard(config)
    verdict = guard.inspect(_item("联系 alice@example.com"))
    assert verdict.allowed is True
    assert verdict.sensitive is False
    assert "邮箱" in verdict.reasons


def test_collector_keeps_email_text(storage, config):
    collector = Collector(storage, config, PrivacyGuard(config))
    outcome, saved = collector.process(_item("联系 alice@example.com 讨论方案"))
    assert outcome == CAPTURED
    assert saved is not None


# ---- 采集：复制了却没记 ----


def test_json_snippet_is_code():
    """现象：复制一段接口返回的 JSON，历史里显示成「文本」，
    右键「转换为…」里连「格式化 JSON」都不出现。"""
    assert classify.detect_kind('{"a":1,"b":[2,3]}') == "code"
    assert classify.detect_kind("[1, 2, 3]") == "code"


def test_plain_scalars_are_not_code():
    """`"abc"` / `42` / `true` 在 Python 里也是合法 JSON，但不能当代码。"""
    assert classify.detect_kind('"just a string"') != "code"
    assert classify.detect_kind("42") != "code"
    assert classify.looks_like_json("[1,2,") is False


def test_default_types_include_code():
    """现象：复制一段函数不报错、不入库，用户只能干瞪眼。"""
    from linglong_clip.utils.config import DEFAULTS

    assert "code" in DEFAULTS["capture"]["types"]


def test_code_snippet_is_captured(storage, config):
    collector = Collector(storage, config, PrivacyGuard(config))
    outcome, saved = collector.process(_item("def f(x):\n    return x * 2"))
    assert outcome == CAPTURED
    assert saved.kind == "code"


# ---- 检索 ----


def test_exclude_kind_is_parsed():
    """现象：「不要图片」被解析成「只要图片」，结果正好相反。"""
    intent = parse_intent("不要图片")
    assert intent.exclude_kinds == ["image"]
    assert intent.kinds == []


def test_exclude_kind_filters_results(storage):
    storage.add(_item("https://example.com/a", kind="link"))
    storage.add(_item("一段普通文本内容"))
    results = SemanticEngine(storage).search("不要链接")
    assert all(item.kind != "link" for item in results)
    assert results


def _last_week_midday() -> float:
    """上周三中午 —— 无论今天星期几，这个时刻都落在「上周」区间内。

    注意 `_day_start(n)` 的参数是「往前推 n 天」，不是星期序号。
    """
    return parse_intent("上周").since + 2 * 86400


def _midday_days_ago(days: int) -> float:
    """`days` 天前的**正午**。

    不用 `now - 86400 * days`：那个偏移量对应的日期取决于现在几点。
    `now - 86400 * 1.2` 在下午算「昨天」，在凌晨 00:05 就是「前天」——
    这条用例因此在每天 0:00~4:48 之间必红。
    取正午则无论何时跑都稳稳落在目标那一整天里。
    """
    import datetime as _dt

    target = _dt.datetime.fromtimestamp(time.time()) - _dt.timedelta(days=days)
    return target.replace(hour=12, minute=0, second=0, microsecond=0).timestamp()


def test_until_bound_excludes_later_items(storage):
    """现象：搜「昨天」把今天的记录也一起带出来了。

    `intent.until` 被解析出来却没用上，上界形同虚设。
    """
    now = time.time()
    old = storage.add(_item("昨天的记录", created_at=_midday_days_ago(1)))
    fresh = storage.add(_item("今天的记录", created_at=now))

    ids = [item.id for item in SemanticEngine(storage).search("昨天", limit=20)]
    assert old.id in ids
    assert fresh.id not in ids


# ---- 存储排序 ----


def test_order_uses_max_of_used_and_created(storage):
    """现象：一条曾被复制过的旧记录，长期压在新记录前面。

    旧实现用 `COALESCE(NULLIF(used_at,0), created_at)`：只要 used_at 有值，
    就完全忽略 created_at —— 哪怕这条记录比旁边的旧好几天。
    """
    now = time.time()
    stale = storage.add(
        _item("很久以前用过", created_at=now - 86400 * 5, used_at=now - 86400 * 3)
    )
    fresh = storage.add(_item("刚复制", created_at=now))

    order = [item.id for item in storage.list(limit=10)]
    assert order.index(fresh.id) < order.index(stale.id)


# ---- 检索：类型条件与时间区间 ----
#
# 这三条都是「只在某一类输入下现形」的缺陷：单看代码每一步都对，
# 只有把「调用方传了什么」和「解析出了什么」并排放才看得出来。


def test_explicit_kind_and_query_kind_must_both_hold(storage):
    """现象：侧栏选「链接」，再搜「上周的截图」，返回的却是一批链接。

    `kind or intent.kinds[0]` 把解析出的 `image` 直接丢弃了 ——
    用户明确筛了类型，结果里却全是另一个类型。
    两个条件互相矛盾时无交集，正确答案是空，不是「随便挑一个」。
    """
    # 钉进确定的上周时刻，别用「8 天前」：那个偏移对应哪一天取决于今天星期几，
    # 周一时 8 天前是上周日（能过），周四时是上周三（也能过），
    # 但这条用例要验的是「类型冲突」，不该顺带验日期。
    stamp = _last_week_midday()
    storage.add(_item("https://cnb.cool/x", kind="link", created_at=stamp))
    storage.add(_item("shot.png", kind="image", created_at=stamp))

    engine = SemanticEngine(storage)
    assert engine.search("上周的截图") != []
    # 显式指定 kind=link 与查询里的 image 矛盾 → 空
    assert engine.search("上周的截图", kind="link") == []
    # 两者一致时照常返回
    assert engine.search("上周的截图", kind="image") != []


def test_last_week_upper_bound_is_today_not_monday(storage):
    """现象：「上周」的命中范围随「今天星期几」漂移。

    `last_week` 的上界取本周一 00:00，而过滤判的是 `created_at > until`，
    于是**周一那天自己也被排除**：同一个库、同一条查询，周一跑是空，
    周二跑是对的。测试也因此只在周一周二通过。
    """
    since = parse_intent("上周").since
    # 区间必须真的覆盖上周末，且不吞掉今天
    assert since > 0
    inside = storage.add(_item("上周末的记录", created_at=since + 1))
    outside = storage.add(_item("未来一周的记录", created_at=time.time() + 8 * 86400))

    ids = [item.id for item in SemanticEngine(storage).search("上周", limit=20)]
    assert inside.id in ids
    assert outside.id not in ids


def test_time_window_uses_one_clock_for_both_bounds(storage):
    """现象：一条旧记录被重新复制后，落在「since 之前、until 之后」的夹缝里。

    下界判 `max(created_at, used_at)`，上界只判 `created_at`，
    两个口径不一致时，中间的记录两边都不收 —— 用户看到的是「明明有却搜不到」。
    """
    now = time.time()
    # 创建很早、但刚刚用过一次 → 应算「最近碰过」，落在下界之内
    reused = storage.add(
        _item("很久以前写的但刚复制过", created_at=now - 30 * 86400, used_at=now - 60)
    )
    ids = [item.id for item in SemanticEngine(storage).search("最近", limit=20)]
    assert reused.id in ids

    # 反方向：创建在很久以后（时钟漂移 / 导入的未来时间戳），不该被「最近」收进来
    future = storage.add(_item("未来时间戳", created_at=now + 10 * 86400))
    ids = [item.id for item in SemanticEngine(storage).search("最近", limit=20)]
    assert future.id not in ids


def test_last_week_window_is_stable_across_weekdays(storage):
    """现象：`上周的截图` 能不能搜到，取决于今天是星期几。

    上界取的是「本周一 00:00」这个**时刻**，而过滤判 `created_at > until`，
    周一 00:00 之后的事件全部落在区间外 —— 于是周一到周三的表现各不相同。
    判据必须只看「上周那一整周」，与今天星期几无关。

    这里用同一份数据、按不同 `tm_wday` 反复验，把日期依赖钉死。
    """
    import datetime as _dt

    from linglong_clip.core import semantic as sem

    real_localtime = time.localtime

    def _range_for(weekday: int):
        """伪造「今天是周 `weekday`」，取 `上周` 的区间。"""

        def fake(sec=None):
            value = real_localtime(sec)
            return time.struct_time(
                (value.tm_year, value.tm_mon, value.tm_mday,
                 value.tm_hour, value.tm_min, value.tm_sec,
                 weekday, value.tm_yday, value.tm_isdst)
            )

        original = sem.time.localtime
        sem.time.localtime = fake
        try:
            return sem._resolve_range("last_week")
        finally:
            sem.time.localtime = original

    for weekday in range(7):
        since, until = _range_for(weekday)
        assert since > 0 and until > since, f"周{weekday + 1} 区间退化"
        span_days = round((until - since) / 86400)
        assert span_days == 7, f"周{weekday + 1} 的上周不是 7 天：{span_days}"

        # 上周三中午永远落在这个区间内
        middle = _dt.datetime.fromtimestamp(since).replace(hour=12) + _dt.timedelta(days=2)
        stamp = middle.timestamp()
        assert since <= stamp < until, f"周{weekday + 1} 把上周三漏掉了"


def test_week_window_has_an_upper_bound(storage):
    """现象：搜「本周」会把下周的记录也带出来。

    `_TIME_RANGES` 里「本周」的第二个槽位就是 `"week"`，它同时充当
    「上周」的上界。早先这里返回 `0.0`（表示不限），两个词的上界一起消失。
    """
    since, until = __import__(
        "linglong_clip.core.semantic", fromlist=["_resolve_range"]
    )._resolve_range("week")
    assert since > 0
    assert until > since
    # 本周的终点是「本周日结束」= 本周一 + 6 天，所以跨度是 6 天而非 7 天
    assert round((until - since) / 86400) == 6


# ---- 界面：设置页与窗口在真实屏幕上放得下 ----
#
# 这一组用例守的是同一件事的两个表现：控件「存在」但用户看不到 / 点不到。
# 纯逻辑测试测不出来（控件对象都在，`isChecked()` 也读得到），
# 所以断言的是几何关系：窗口在屏幕内、按钮在窗口内。

def test_settings_dialog_fits_a_short_screen(qt_app, monkeypatch):
    """现象：768px 高的屏幕上，设置页从 y=0 长到 1282，保存按钮在屏幕外。

    Qt 会把 QDialog 撑到内容的最小高度，「采集」那一页内容就有 1200 多像素。
    此前是固定 `resize(760, 640)`，于是窗口被内容撑开，按钮点不到 ——
    用户只能强杀进程，并合理地认为「设置页坏了」。
    """
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QGuiApplication

    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    class _Screen:
        """把可用区域伪装成 1024×600 的小屏。"""

        def availableGeometry(self):
            return QRect(0, 0, 1024, 600)

        def geometry(self):
            return QRect(0, 0, 1024, 600)

    fake_screen = _Screen()
    monkeypatch.setattr(
        QGuiApplication, "primaryScreen", staticmethod(lambda: fake_screen)
    )
    monkeypatch.setattr(
        QGuiApplication, "screenAt", staticmethod(lambda _point: fake_screen)
    )
    dialog = SettingsDialog(Config())
    dialog.show()
    qt_app.processEvents()

    assert dialog.height() <= 600, f"设置页比屏幕还高：{dialog.height()}"
    # 保存按钮必须落在窗口内，否则用户点不到它
    button_bottom = dialog._save_button.mapTo(dialog, dialog._save_button.rect().bottomLeft()).y()
    assert 0 <= button_bottom <= dialog.height()


def test_settings_pages_are_scrollable(qt_app):
    """内容超出一屏时必须能滚，而不是把整个窗口撑高。"""
    from PySide6.QtWidgets import QScrollArea

    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    dialog = SettingsDialog(Config())
    pages = dialog.findChildren(QScrollArea, "SettingsPage")
    assert len(pages) == dialog._tabs.count()
    for page in pages:
        assert page.widgetResizable() is True
        assert page.widget() is not None


def test_main_window_fits_a_short_screen(qt_app, storage, config, monkeypatch):
    """现象：1366×768 的笔记本上主窗口比可用区域还高，标题栏被顶出屏幕。"""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QGuiApplication

    from linglong_clip.core.semantic import SemanticEngine
    from linglong_clip.ui.main_window import MainWindow

    class _Screen:
        def availableGeometry(self):
            return QRect(0, 0, 1024, 600)

        def geometry(self):
            return QRect(0, 0, 1024, 600)

    monkeypatch.setattr(
        QGuiApplication, "primaryScreen", staticmethod(lambda: _Screen())
    )
    window = MainWindow(storage, config, SemanticEngine(storage))
    try:
        assert window.width() <= 1024
        assert window.height() <= 600
        # 最小尺寸也不能超过屏幕 —— 否则窗口永远有一条边在屏幕外
        assert window.minimumWidth() <= 1024
        assert window.minimumHeight() <= 600
    finally:
        window.shutdown()


def test_quick_paste_stays_inside_the_screen(qt_app, storage, config, monkeypatch):
    """现象：小屏上浮层按 22% 定位，9 行时底边跑到屏幕外，提示行被切掉。"""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QGuiApplication

    from linglong_clip.core.models import ClipboardItem, content_hash
    from linglong_clip.core.semantic import SemanticEngine
    from linglong_clip.ui.quick_paste import QuickPasteOverlay

    for index in range(12):
        content = f"第 {index} 条"
        storage.add(
            ClipboardItem(kind="text", content=content, hash=content_hash("text", content))
        )

    class _Screen:
        def availableGeometry(self):
            return QRect(0, 0, 1024, 600)

    monkeypatch.setattr(
        QGuiApplication, "screenAt", staticmethod(lambda _point: _Screen())
    )
    overlay = QuickPasteOverlay(storage, SemanticEngine(storage), config)
    overlay.show_centered()
    qt_app.processEvents()
    assert overlay.geometry().bottom() <= 600
    assert overlay.geometry().top() >= 0
    overlay.close_overlay()


# ---- 界面：主题这件事的三处漏接 ----

def test_theme_palette_reaches_the_application(qt_app):
    """现象：深色界面里勾选框是深底浅边，看不出勾没勾。

    QSS 只负责它显式写到的部件，其余原生绘制跟着 `QPalette` 走；
    而此前从未设置过 `QPalette` —— 白色桌面上启动就拿到浅色图标。
    """
    from PySide6.QtGui import QPalette

    from linglong_clip.ui.theme import DARK, apply_palette

    apply_palette(DARK, qt_app)
    assert qt_app.palette().color(QPalette.ColorRole.Window).name() == DARK.bg
    assert qt_app.palette().color(QPalette.ColorRole.Base).name() == DARK.surface
    assert qt_app.palette().color(QPalette.ColorRole.Highlight).name() == DARK.accent


def test_indicator_style_is_vendor_neutral(qt_app):
    """勾选/箭头图标由自己画，不依赖位图资源。"""
    from linglong_clip.ui.theme import IndicatorStyle, install_style, DARK

    style = install_style(DARK, qt_app)
    assert isinstance(style, IndicatorStyle)
    assert isinstance(qt_app.style(), IndicatorStyle)
    assert style._palette is not None


def test_overlay_and_transform_follow_theme_switch(qt_app, storage, config):
    """现象：切到浅色主题后，按快捷键弹出的浮层还是深色。

    浮层与转换窗口是独立顶层窗口，不继承主窗口样式表。
    此前换主题只重建了主窗口，这两个窗口一直带着启动那一刻的色板。
    """
    from linglong_clip.core.automation import AutomationEngine
    from linglong_clip.core.semantic import SemanticEngine
    from linglong_clip.ui.quick_paste import QuickPasteOverlay
    from linglong_clip.ui.transform_window import TransformWindow
    from linglong_clip.ui.theme import LIGHT, toast_style, build_stylesheet

    overlay = QuickPasteOverlay(storage, SemanticEngine(storage), config)
    transformer = TransformWindow(config, AutomationEngine())

    overlay.apply_theme("light")
    transformer.apply_theme("light")

    assert overlay._palette_theme.name == "light"
    assert overlay.styleSheet() == toast_style(LIGHT)
    assert transformer.styleSheet() == build_stylesheet(LIGHT)


def test_system_theme_consults_the_platform(qt_app, monkeypatch):
    """现象：选了「跟随系统」，白色桌面上也永远是深色。

    `palette_for()` 此前把 `system` 直接当 dark，`system` 是默认值 ——
    于是默认配置下浅色主题根本不可能出现。
    """
    from linglong_clip.ui import theme as theme_module

    monkeypatch.setattr(theme_module, "_system_prefers_light", lambda: True)
    assert theme_module.resolve_theme("system") == "light"
    assert theme_module.palette_for("system").name == "light"

    monkeypatch.setattr(theme_module, "_system_prefers_light", lambda: False)
    assert theme_module.resolve_theme("system") == "dark"
    # 显式指定永远优先于系统偏好
    assert theme_module.resolve_theme("light") == "light"
    assert theme_module.resolve_theme("dark") == "dark"


# ---- 平台提示：别在 Windows/macOS 上指错方向 ----


def test_primary_selection_hint_is_platform_aware(qt_app, config, monkeypatch):
    """现象：Windows / macOS 用户在设置页看到「装 python-xlib 或 xclip/xsel」。

    主选择区是 X11 / Wayland 的概念，那两个平台上压根不存在。报「缺功能」
    还教用户去装一个不存在的包，比不说更糟 —— 用户会去搜、去装、发现装不上，
    然后认为软件坏了。
    """
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils import paths

    dialog = SettingsDialog(config)

    monkeypatch.setattr(paths, "is_linux", lambda: False)
    monkeypatch.setattr(paths, "is_macos", lambda: False)
    windows_text = dialog._primary_unavailable_text()
    assert "python-xlib" not in windows_text
    assert "没有主选择区" in windows_text

    monkeypatch.setattr(paths, "is_macos", lambda: True)
    mac_text = dialog._primary_unavailable_text()
    assert "python-xlib" not in mac_text
    assert "macOS" in mac_text

    # Linux 上仍然要给可操作的安装建议 —— 那里确实缺库
    monkeypatch.setattr(paths, "is_linux", lambda: True)
    monkeypatch.setattr(paths, "is_macos", lambda: False)
    assert "xclip" in dialog._primary_unavailable_text()


def test_native_history_hint_is_platform_aware(qt_app, config, monkeypatch):
    """现象：Linux 与 macOS 上也写「需要 Windows 10 1809+」。

    用户既不知道自己该做什么，也无从判断这功能跟自己有没有关系。
    """
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils import paths

    dialog = SettingsDialog(config)

    monkeypatch.setattr(paths, "is_windows", lambda: False)
    monkeypatch.setattr(paths, "is_macos", lambda: False)
    linux_text = dialog._native_history_unavailable_text()
    assert "Windows 10 1809" not in linux_text
    assert "Linux" in linux_text

    monkeypatch.setattr(paths, "is_linux", lambda: False)
    monkeypatch.setattr(paths, "is_macos", lambda: True)
    mac_text = dialog._native_history_unavailable_text()
    assert "Windows 10 1809" not in mac_text
    assert "macOS" in mac_text

    # Windows 上才是那句话该出现的地方
    monkeypatch.setattr(paths, "is_macos", lambda: False)
    monkeypatch.setattr(paths, "is_windows", lambda: True)
    assert "1809" in dialog._native_history_unavailable_text()


# ---- 「关于」页：装成安装版的用户手里没有 README ----


def test_about_tab_lists_author_and_channels(qt_app, config):
    """现象：「关于」页只有版本号和运行环境。

    用户想问「这是谁做的、去哪反馈、有没有新版本」，界面上一个入口都没有。
    """
    from PySide6.QtWidgets import QLabel

    from linglong_clip.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(config)
    page = dialog._build_about_tab()
    joined = "\n".join(label.text() for label in page.findChildren(QLabel))

    assert "科技酱" in joined
    assert "同一个世界" in joined
    assert "asoe.cn" in joined
    assert "291974598" in joined


def test_author_channels_defined_once():
    """作者信息只存一处，界面与文档都从这里取 —— 两处各写一份迟早分叉。"""
    from linglong_clip import AUTHOR, AUTHOR_CHANNELS, AUTHOR_QQ_GROUPS

    assert AUTHOR == "科技酱"
    urls = dict(AUTHOR_CHANNELS)
    assert urls["主站"] == "https://www.asoe.cn"
    assert urls["技术文档"] == "https://docs.asoe.cn"
    assert urls["GitHub"] == "https://github.com/techjiang/"
    assert urls["哔哩哔哩"] == "https://space.bilibili.com/1768832152"
    assert urls["玲珑论坛"] == "https://forums.asoe.cn/"
    assert urls["邮箱"] == "official@tech.asoe.cn"
    assert AUTHOR_QQ_GROUPS == ("291974598", "474819022", "631167288")


# ---- 空状态引导页：矮窗口里别把最该读的那句切掉 ----


def test_empty_state_panel_is_scrollable(qt_app, storage, config):
    """现象：540px 高的主窗口里，「卸载程序不会删除这个目录」被切掉半截。

    面板内部有 addStretch，窗口一变矮布局就把弹性空间压成负值，
    卡片被压到比内容还小。而那句话正是这个面板存在的理由 ——
    「装完软件我的数据会不会被删」。
    """
    from PySide6.QtWidgets import QLabel

    from linglong_clip.core.automation import AutomationEngine
    from linglong_clip.core.semantic import SemanticEngine
    from linglong_clip.ui.main_window import MainWindow

    window = MainWindow(storage, config, SemanticEngine(storage), AutomationEngine())
    window.resize(900, 540)
    window.reload()
    qt_app.processEvents()

    assert window._empty_scroll.widgetResizable()
    panel = window._empty_panel
    notes = [
        label for label in panel.findChildren(QLabel)
        if label.objectName() == "EmptyStateNote"
    ]
    assert notes, "引导页缺少「卸载程序不会删除这个目录」那句说明"
    # 面板要按内容报高度，否则外层滚动区无从滚起
    assert panel.sizeHint().height() >= panel.layout().minimumSize().height()


# ---- 深色主题：那些「看不见」的控件 ----


def _contrast(fore: str, back: str) -> float:
    """WCAG 相对对比度，1.0 表示完全一样、21.0 表示黑白。"""

    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    def luminance(color: str) -> float:
        text = color.lstrip("#")
        r, g, b = (int(text[i : i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)

    hi, lo = sorted((luminance(fore), luminance(back)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_selection_is_visible_against_surface():
    """现象：切到深色主题后，列表与下拉里的「选中行」和普通行一样。

    选中态此前借用 `surface_active`，它与 `surface` 只差 1.3 对比度；
    而 `surface_hover` 更接近。三者挤在一起，深色下谁也看不出选中的是哪一行。
    """
    from linglong_clip.ui.theme import DARK, LIGHT

    for palette in (DARK, LIGHT):
        assert _contrast(palette.selection, palette.surface) >= 1.4, palette.name
        assert _contrast(palette.selection_text, palette.selection) >= 4.5, palette.name


def test_scrollbar_handle_is_visible():
    """现象：内容超出时看不到滚动条，只有一条和背景同色的细缝。"""
    from linglong_clip.ui.theme import DARK, LIGHT

    for palette in (DARK, LIGHT):
        assert _contrast(palette.scrollbar, palette.bg) >= 2.0, palette.name
        assert _contrast(palette.scrollbar_hover, palette.scrollbar) > 1.0, palette.name


def test_combo_has_no_background_color_in_qss():
    """现象：深色主题里下拉框弹出后所有选项一个色，看不出当前选了哪个。

    根因不在「选中态规则写漏了」，而是 QSS 里给 `QComboBox` 设了
    `background-color` —— 一旦设了，Qt 就把它判成被样式表接管，
    连带把弹出列表也交给 QSS 画，`selection-background-color`
    与 `::item:selected` 全部失效。底色改由 `QPalette.Base` 提供。

    这里按选择器精确匹配：`QSpinBox, QComboBox, QLineEdit` 那种群组里
    混进 `QComboBox` 也一样会触发，所以不能只找 `QComboBox {`。
    """
    import re

    from linglong_clip.ui.theme import DARK, build_stylesheet

    sheet = build_stylesheet(DARK)
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", sheet):
        selectors, body = match.group(1), match.group(2)
        names = [part.strip() for part in selectors.split(",")]
        if "QComboBox" not in names:
            continue
        assert "background-color" not in body, f"QComboBox 不能设 background-color：{selectors.strip()}"


def test_combo_popup_selection_is_styled():
    """`selection-*` 与 `::item:selected` 必须真的写进样式表。"""
    from linglong_clip.ui.theme import DARK, build_stylesheet

    sheet = build_stylesheet(DARK)
    assert "QComboBox QAbstractItemView" in sheet
    assert f"selection-background-color: {DARK.selection};" in sheet
    assert "QComboBox QAbstractItemView::item:selected" in sheet


def test_no_global_widget_background():
    """现象：给 `QWidget` 刷底色后，子控件的选区填充被一起压掉。

    写过 `QWidget { background-color: ... }` 之后，下拉弹出列表与
    原生列表的选中行都不再绘制。窗口级容器各自负责底色就够了。
    """
    import re

    from linglong_clip.ui.theme import DARK, build_stylesheet

    sheet = build_stylesheet(DARK)
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", sheet):
        selectors, body = match.group(1), match.group(2)
        names = [part.strip() for part in selectors.split(",")]
        if "QWidget" not in names:
            continue
        assert "background-color" not in body, "裸 QWidget 选择器不能再刷底色"


def test_disabled_primary_button_looks_disabled():
    """现象：禁用的主按钮仍是一条亮蓝色，看着能点。

    `#Primary` 的优先级高于 `:disabled`，必须再钉一条 `#Primary:disabled`。
    """
    from linglong_clip.ui.theme import DARK, build_stylesheet

    sheet = build_stylesheet(DARK)
    assert "QPushButton#Primary:disabled" in sheet
    assert f"color: {DARK.text_faint};" in sheet


def test_menu_indicator_leaves_the_tick_to_the_style():
    """现象：托盘的「恢复采集」勾上之后只是一块纯蓝方块，和没勾一样认不出。

    菜单是独立顶层窗口，不吃主窗口样式表。这里踩过两次坑：
    完全不管 —— 原生指示器按系统浅色配色画，深色底上是浅灰空框；
    而用 QSS 接管 `::indicator` 之后，`::indicator:checked` 只能刷一块
    底色，勾形不会画。所以只能给尺寸，把绘制交回 `IndicatorStyle`
    的 `PE_IndicatorMenuCheckMark`。
    """
    import re

    from linglong_clip.ui.theme import DARK, LIGHT, build_stylesheet

    for palette in (DARK, LIGHT):
        sheet = build_stylesheet(palette)
        block = re.search(r"QMenu::indicator\s*\{([^}]*)\}", sheet)
        assert block, "菜单勾选框需要定尺寸"
        body = block.group(1)
        # 一旦画了方块，对勾就没机会画，勾选和不勾选长得一样
        assert "background" not in body
        assert "border" not in body
        assert _contrast(palette.text_faint, palette.bg) >= 3.0, palette.name


def test_tray_menu_follows_theme(qt_app):
    """现象：切到浅色主题后，点托盘弹出的还是深色菜单。"""
    from linglong_clip.ui.theme import DARK, LIGHT, build_stylesheet
    from linglong_clip.ui.tray import TrayIcon

    tray = TrayIcon(DARK)
    assert tray._menu.styleSheet() == build_stylesheet(DARK)

    tray.apply_theme(LIGHT)
    assert tray._menu.styleSheet() == build_stylesheet(LIGHT)


def test_settings_dialog_uses_palette_not_hardcoded_colors(qt_app, config):
    """现象：设置页里红色的校验失败、橙色的「不会生效」在浅色主题下淡到看不清。

    这些颜色此前写死成 `#e5484d` / `#e5a00d`，两套主题共用一份 ——
    橙色在浅色底上的对比度只有 2.07。现在统一从色板取。
    """
    from linglong_clip.ui.settings_dialog import SettingsDialog

    from linglong_clip.ui.theme import palette_for

    dialog = SettingsDialog(config, None, {})
    # 对话框持有的色板必须与当前主题一致，后面几处着色都从它取
    assert dialog._palette is palette_for(config.get("ui.theme", "system"))
    # 没有告警时状态标签不着色（空串），说明这些颜色是按需从色板取的
    assert dialog._rules_state.styleSheet() == ""


def test_secondary_text_contrast_passes_both_themes():
    """次要文字（时间、来源、说明）在两套主题下都要读得出来。"""
    from linglong_clip.ui.theme import DARK, LIGHT

    for palette in (DARK, LIGHT):
        assert _contrast(palette.text_faint, palette.bg) >= 3.5, palette.name
        assert _contrast(palette.text_faint, palette.surface) >= 3.0, palette.name
        assert _contrast(palette.text_dim, palette.surface) >= 4.5, palette.name
