"""界面层测试。

用 Qt 的 offscreen 平台，无需真实显示器即可跑。
覆盖卡片渲染、主窗口筛选/搜索、快速粘贴选择逻辑、
剪贴板适配器的读写与图片落盘。
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from linglong_clip.core.models import ClipboardItem, content_hash  # noqa: E402
from linglong_clip.core.semantic import SemanticEngine  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def seeded(storage, qt_app):
    now = time.time()
    rows = [
        ("text", "会议纪要：下周二评审", "chrome", False),
        ("link", "https://cnb.cool/asoe/TechSauce?utm_source=x", "google-chrome", False),
        ("code", "def handler(event):\n    return event['key']", "code", True),
        ("color", "#4c8dff", "figma", False),
        ("image", "shot.png", "flameshot", False),
    ]
    for index, (kind, content, source, pinned) in enumerate(rows):
        storage.add(
            ClipboardItem(
                kind=kind, content=content, preview=content,
                hash=content_hash(kind, content), source_app=source,
                created_at=now - index * 60, pinned=pinned,
            )
        )
    return storage


# ---- 卡片 ----

def test_humanize_time():
    from linglong_clip.ui.item_card import humanize_time

    now = time.time()
    assert humanize_time(now) == "刚刚"
    assert "分钟前" in humanize_time(now - 120)
    assert "小时前" in humanize_time(now - 7200)
    assert "昨天" in humanize_time(now - 90000)
    assert "天前" in humanize_time(now - 3 * 86400)
    assert humanize_time(0) == ""


def test_humanize_size():
    from linglong_clip.ui.item_card import humanize_size

    assert humanize_size(0) == ""
    assert humanize_size(512) == "512 B"
    assert "KB" in humanize_size(4096)
    assert "MB" in humanize_size(5 * 1024 * 1024)


def test_card_preview_text_for_each_kind(qt_app):
    from linglong_clip.ui.item_card import ItemCard
    from linglong_clip.ui.theme import DARK

    image = ClipboardItem(kind="image", image_path="a.png", image_width=100, image_height=50)
    card = ItemCard(image, DARK)
    assert "100" in card._preview_text() and "50" in card._preview_text()

    single = ClipboardItem(kind="file", files=["/a/one.txt"])
    assert ItemCard(single, DARK)._preview_text() == "/a/one.txt"

    many = ClipboardItem(kind="file", files=[f"/a/{i}.txt" for i in range(4)])
    assert "等 4 个文件" in ItemCard(many, DARK)._preview_text()

    empty = ClipboardItem(kind="text", content="   ")
    assert ItemCard(empty, DARK)._preview_text() == "(空)"


def test_card_tooltip_mentions_actions(qt_app):
    from linglong_clip.ui.item_card import ItemCard
    from linglong_clip.ui.theme import DARK

    card = ItemCard(ClipboardItem(kind="text", content="x", source_app="chrome"), DARK)
    tooltip = card._tooltip_text()
    assert "chrome" in tooltip
    assert "双击复制" in tooltip


def test_card_emits_signals(qt_app):
    from linglong_clip.ui.item_card import ItemCard
    from linglong_clip.ui.theme import DARK

    card = ItemCard(ClipboardItem(id=7, kind="text", content="x"), DARK)
    seen = []
    card.double_clicked.connect(seen.append)
    card.double_clicked.emit(7)
    assert seen == [7]


# ---- 主窗口 ----

@pytest.fixture()
def window(storage, config, qt_app):
    from linglong_clip.ui.main_window import MainWindow

    return MainWindow(storage, config, SemanticEngine(storage))


def test_window_renders_all_cards(window, seeded):
    window.reload()
    assert len(window._cards) == 5
    assert window._items


def test_window_search_filters(window, seeded):
    window._search.setText("会议纪要")
    window.reload()
    assert len(window._items) == 1
    assert "会议纪要" in window._items[0].content


def test_window_natural_language_shows_explanation(window, seeded):
    window._search.setText("上周的截图")
    window.reload()
    assert "已理解" in window._intent_label.text()


def test_window_filter_by_kind_via_chip(window, seeded):
    window._on_chip("link")
    assert len(window._items) == 1
    assert window._items[0].kind == "link"


def test_window_side_filter_shows_all(window, seeded):
    # 侧栏第一项是「全部」，切到别的项再切回来才会触发信号
    window._on_chip("link")
    window._side_list.setCurrentRow(0)
    assert len(window._items) == 5


def test_window_pinned_filter(window, seeded):
    from linglong_clip.ui.main_window import SIDE_FILTERS

    row = [i for i, entry in enumerate(SIDE_FILTERS) if entry[2] == "pinned"][0]
    window._side_list.setCurrentRow(row)
    assert all(item.pinned for item in window._items)


def test_window_toggle_pin_emits_intent(window, seeded):
    """切换固定只发信号，不自己写库。

    界面拿不到「这条固定是谁设的」这类跨设备信息，写入交给 Application ——
    所以这里断言的是**发出去的意图**，落库由 Application 的用例守着。
    """
    window.reload()
    target = next(item for item in window._items if not item.pinned)
    window._select(target.id)
    received = []
    window.flag_toggled.connect(lambda *args: received.append(args))
    window._toggle_pin_selected()
    assert received == [(target.id, "pinned", True)]


def test_window_toggle_favorite_emits_intent(window, seeded):
    window.reload()
    target = window._items[0]
    window._select(target.id)
    received = []
    window.flag_toggled.connect(lambda *args: received.append(args))
    window._toggle_favorite_selected()
    assert received == [(target.id, "favorite", True)]


def test_window_toggle_does_not_write_storage_directly(window, seeded):
    """界面不再直接写库：没接 Application 时，存储内容必须原样不动。

    这条守着 CONTRIBUTING 里的分层约束（ui/ 不直接读写数据库）。
    写库要顺带做「记录固定来源」这种跨设备的事，界面做不到也不该替它做。
    """
    window.reload()
    target = next(item for item in window._items if not item.pinned)
    window._select(target.id)
    before = window._storage.get(target.id)
    window._toggle_pin_selected()
    after = window._storage.get(target.id)
    assert after.pinned == before.pinned is False


def test_window_empty_state(window, storage):
    window._search.setText("绝不存在的关键词xyz")
    window.reload()
    assert window._items == []
    assert window._empty_label.isHidden() is False


def test_window_copy_signal(window, seeded):
    window.reload()
    received = []
    window.copy_requested.connect(lambda item_id, plain: received.append((item_id, plain)))
    window._copy_selected_plain()
    assert received and received[0][1] is True


def test_window_paste_nth_emits(window, seeded):
    window.reload()
    received = []
    window.paste_requested.connect(received.append)
    window._paste_nth(2)
    assert received == [window._items[1].id]


def test_window_paste_nth_out_of_range_is_noop(window, seeded):
    window.reload()
    received = []
    window.paste_requested.connect(received.append)
    window._paste_nth(99)
    assert received == []


def test_window_delete_signal(window, seeded):
    window.reload()
    received = []
    window.delete_requested.connect(received.append)
    window._delete_selected()
    assert received == [window._selected_id]


def test_window_apply_theme_rebuilds(window, seeded):
    window.apply_theme("light")
    assert window._palette_theme.name == "light"
    window.apply_theme("dark")
    assert window._palette_theme.name == "dark"


def test_window_stats_refresh(window, seeded):
    window.reload()
    assert window._stat_items.text() == "5"


def test_window_intent_label_cleared_without_query(window, seeded):
    window._search.setText("会议")
    window.reload()
    window._search.clear()
    window.reload()
    assert window._intent_label.text() == ""


def test_row_for_kind_mapping(window):
    assert window._row_for_kind("link") > 0
    assert window._row_for_kind("unknown") == 0


# ---- 快速粘贴 ----

@pytest.fixture()
def overlay(storage, config, qt_app):
    from linglong_clip.ui.quick_paste import QuickPasteOverlay

    return QuickPasteOverlay(storage, SemanticEngine(storage), config)


def test_overlay_lists_recent(overlay, seeded):
    overlay._reload()
    assert len(overlay._rows) == 5


def test_overlay_respects_max_rows(overlay, storage, config):
    for index in range(20):
        storage.add(ClipboardItem(kind="text", content=f"c{index}", preview=f"c{index}",
                                  hash=f"h{index}"))
    overlay._reload()
    from linglong_clip.ui.quick_paste import MAX_ROWS

    assert len(overlay._rows) <= MAX_ROWS


def test_overlay_search(overlay, seeded):
    overlay._search.setText("会议纪要")
    overlay._reload()
    assert len(overlay._items) == 1


def test_overlay_move_wraps(overlay, seeded):
    overlay._reload()
    overlay._active = 0
    overlay._move(-1)
    assert overlay._active == len(overlay._rows) - 1
    overlay._move(1)
    assert overlay._active == 0


def test_overlay_choose_emits(overlay, seeded):
    overlay._reload()
    chosen = []
    overlay.item_chosen.connect(lambda item_id, plain: chosen.append((item_id, plain)))
    overlay._choose(2)
    assert chosen == [(overlay._items[1].id, False)]


def test_overlay_choose_shift_is_plain_text(overlay, seeded):
    overlay._reload()
    chosen = []
    overlay.item_chosen.connect(lambda item_id, plain: chosen.append((item_id, plain)))
    overlay._choose(1, plain_text_only=True)
    assert chosen[0][1] is True


def test_overlay_choose_out_of_range_is_noop(overlay, seeded):
    overlay._reload()
    chosen = []
    overlay.item_chosen.connect(chosen.append)
    overlay._choose(99)
    assert chosen == []


def test_overlay_digit_input_selects_position(overlay, seeded):
    overlay._reload()
    overlay._on_search_changed("3")
    assert overlay._active == 2


def test_overlay_digit_beyond_range_is_ignored(overlay, seeded):
    overlay._reload()
    overlay._on_search_changed("9")
    assert overlay._active == 0


def test_overlay_empty_state(overlay, storage):
    overlay._search.setText("no-such-content-xyz")
    overlay._reload()
    assert overlay._rows == []


def test_overlay_row_text_truncates(qt_app):
    from linglong_clip.ui.quick_paste import QuickRow
    from linglong_clip.ui.theme import DARK

    item = ClipboardItem(kind="text", content="x" * 500)
    assert len(QuickRow._row_text(item)) <= 160


# ---- 剪贴板适配器 ----

@pytest.fixture()
def adapter(qt_app, home, monkeypatch):
    from linglong_clip.ui.clipboard_qt import QtClipboardAdapter

    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home))
    return QtClipboardAdapter(qt_app.clipboard(), home / "images")


def test_adapter_reads_text(adapter, qt_app):
    qt_app.clipboard().setText("hello 剪贴板")
    item = adapter.read_clipboard()
    assert item.kind == "text"
    assert item.content == "hello 剪贴板"
    assert item.hash


def test_adapter_returns_none_when_empty(adapter, qt_app):
    qt_app.clipboard().clear()
    assert adapter.read_clipboard() is None


def test_adapter_saves_image_to_disk(adapter, qt_app, home):
    image = QImage(24, 12, QImage.Format.Format_RGB32)
    image.fill(QColor("#4c8dff"))
    qt_app.clipboard().setImage(image)

    item = adapter.read_clipboard()
    assert item.kind == "image"
    assert (item.image_width, item.image_height) == (24, 12)
    assert (home / item.image_path).is_file()


def test_adapter_dedupes_identical_images(adapter, qt_app, home):
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("#000000"))
    qt_app.clipboard().setImage(image)
    first = adapter.read_clipboard()
    qt_app.clipboard().setImage(image)
    second = adapter.read_clipboard()
    assert first.hash == second.hash
    assert first.image_path == second.image_path
    assert len(list((home / "images").glob("*.png"))) == 1


def test_adapter_writes_text_back(adapter, qt_app):
    adapter.set_text("写回的内容")
    assert qt_app.clipboard().text() == "写回的内容"


def test_adapter_plain_text_only_strips_html(adapter, qt_app):
    item = ClipboardItem(kind="text", content="纯文本", html="<b>纯文本</b>")
    adapter.write_item(item, plain_text_only=True)
    assert qt_app.clipboard().text() == "纯文本"
    assert qt_app.clipboard().mimeData().hasHtml() is False


def test_adapter_keeps_html_by_default(adapter, qt_app):
    item = ClipboardItem(kind="text", content="富文本", html="<b>富文本</b>")
    adapter.write_item(item)
    assert qt_app.clipboard().mimeData().hasHtml() is True


def test_adapter_image_roundtrip(adapter, qt_app):
    image = QImage(16, 16, QImage.Format.Format_RGB32)
    image.fill(QColor("#ff8800"))
    qt_app.clipboard().setImage(image)
    item = adapter.read_clipboard()

    qt_app.clipboard().clear()
    assert adapter.write_item(item) is True
    back = qt_app.clipboard().image()
    assert (back.width(), back.height()) == (16, 16)
    assert back.pixelColor(0, 0).name() == "#ff8800"


def test_adapter_loads_image_from_absolute_path(adapter, home):
    image = QImage(4, 4, QImage.Format.Format_RGB32)
    image.fill(QColor("#00ff00"))
    target = home / "abs.png"
    image.save(str(target))
    assert adapter._load_image(str(target)) is not None


def test_adapter_missing_image_returns_none(adapter):
    assert adapter._load_image("nope/not-there.png") is None


def test_adapter_file_urls(adapter, qt_app, home):
    from PySide6.QtCore import QMimeData, QUrl

    target = home / "a.txt"
    target.write_text("x", encoding="utf-8")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(target))])
    qt_app.clipboard().setMimeData(mime)

    item = adapter.read_clipboard()
    assert item.kind == "file"
    assert item.files == [str(target)]


def test_adapter_overlong_text_is_skipped(adapter, qt_app):
    qt_app.clipboard().setText("x" * 6_000_000)
    assert adapter.read_clipboard() is None


# ---- 主题 ----

def test_theme_palettes_are_complete():
    from linglong_clip.ui.theme import DARK, LIGHT

    for palette in (DARK, LIGHT):
        assert palette.name
        for field in ("bg", "surface", "text", "accent", "border"):
            assert getattr(palette, field).startswith("#")


def test_stylesheet_built_for_both_themes():
    from linglong_clip.ui.theme import DARK, LIGHT, build_stylesheet

    for palette in (DARK, LIGHT):
        sheet = build_stylesheet(palette)
        assert palette.accent in sheet
        assert sheet.count("{") == sheet.count("}")


def test_palette_for_unknown_theme_defaults_to_dark():
    from linglong_clip.ui.theme import palette_for

    assert palette_for("nonsense").name == "dark"
    assert palette_for("light").name == "light"


def test_kind_colors_cover_all_kinds():
    from linglong_clip.core.models import KIND_LABELS
    from linglong_clip.ui.theme import KIND_COLORS

    for kind in KIND_LABELS:
        assert kind in KIND_COLORS


def test_window_shutdown_stops_search_debounce(window, storage):
    """销毁窗口前停掉搜索防抖定时器。

    这条守的是一个真实缺陷：`setText` 会启动 120ms 单发定时器，若窗口
    在这之前被回收（退出、测试夹具结束），定时器仍会在事件循环里到期并
    回调 `reload()` —— 而那时 storage 多半已经关了。异常从事件循环里抛出，
    Qt 只会打一行 `Exceptions caught in Qt event loop`，追不到源头。
    """
    window._search.setText("会议纪要")
    assert window._search_timer.isActive()

    window.shutdown()
    assert not window._search_timer.isActive()


def test_window_reload_after_storage_close_is_noop(window, storage, seeded):
    """存储关闭后窗口的刷新不该抛异常，只显示空列表。"""
    window.reload()
    assert len(window._items) == 5

    storage.close()
    window.reload()

    assert window._items == []
    assert window._cards == {}
    assert window._status_label.text() == "无匹配记录"


def test_window_reload_after_shutdown_does_not_touch_storage(storage, config, qt_app):
    """「窗口已回收、定时器才到期」这条路径必须安静收场。

    这是上面两条合起来的效果：定时器不再回调，即使回调也被存储层挡住。
    """
    from linglong_clip.ui.main_window import MainWindow

    window = MainWindow(storage, config, SemanticEngine(storage))
    window._search.setText("上周的截图")
    window.shutdown()
    storage.close()

    # 定时器即使被强行触发（模拟事件循环晚一步），也不该炸
    window._search_timer.timeout.emit()
    assert window._items == []
