"""主题与样式表。

配色走「深色玻璃质感 + 单一强调色」，避免多色堆叠导致视觉噪音。
样式表集中在 `build_stylesheet` 生成，方便暗/亮两套主题共用结构。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from PySide6.QtWidgets import QProxyStyle


@dataclass(frozen=True)
class Palette:
    """一套主题的色板。"""

    name: str
    bg: str
    bg_alt: str
    surface: str
    surface_hover: str
    surface_active: str
    border: str
    text: str
    text_dim: str
    text_faint: str
    accent: str
    accent_dim: str
    accent_text: str
    danger: str
    warning: str
    success: str
    shadow: str
    #: 列表/下拉里「选中行」的底色。不能用 `surface_active`：它和 `surface`
    #: 只差 1.3 对比度，深色下选中行与未选中行看起来一模一样。
    selection: str
    #: 选中行上的文字色
    selection_text: str
    #: 滚动条滑块与悬停态。同样不能用 `border`（1.35 对比度 = 看不见）
    scrollbar: str
    scrollbar_hover: str

    def to_dict(self) -> Dict[str, str]:
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__  # type: ignore[attr-defined]
        }


DARK = Palette(
    name="dark",
    bg="#14161c",
    bg_alt="#0f1116",
    surface="#1c1f28",
    surface_hover="#252936",
    surface_active="#2e3342",
    border="#363d4f",
    text="#eef1f7",
    text_dim="#a4adc2",
    text_faint="#828cab",
    accent="#4c8dff",
    accent_dim="#2f5fb8",
    accent_text="#ffffff",
    danger="#ff5f6d",
    warning="#f2b447",
    success="#3ddc97",
    shadow="rgba(0, 0, 0, 0.45)",
    selection="#2a4068",
    selection_text="#eef1f7",
    scrollbar="#434c66",
    scrollbar_hover="#5a6480",
)

LIGHT = Palette(
    name="light",
    bg="#f4f6fb",
    bg_alt="#eaeef6",
    surface="#ffffff",
    surface_hover="#f0f3f9",
    surface_active="#e4e9f3",
    border="#ccd4e2",
    text="#1b1f2a",
    text_dim="#5a6376",
    text_faint="#6f7889",
    accent="#2f6fed",
    accent_dim="#1f4fae",
    accent_text="#ffffff",
    danger="#d93a47",
    warning="#b8801f",
    success="#1f9e6b",
    shadow="rgba(15, 24, 46, 0.12)",
    selection="#c9dafa",
    selection_text="#1b1f2a",
    scrollbar="#a2adc3",
    scrollbar_hover="#8894ab",
)

#: 各类型的强调色，列表里作为色条使用
KIND_COLORS: Dict[str, str] = {
    "text": "#4c8dff",
    "rich": "#8b7bff",
    "image": "#3ddc97",
    "file": "#f2b447",
    "link": "#4bc3e0",
    "color": "#ff8ac4",
    "code": "#a3e04c",
    "sensitive": "#ff5f6d",
}


#: 主题配置的合法取值，与设置页下拉一一对应
THEME_VALUES = ("system", "light", "dark")


def _system_prefers_light() -> bool:
    """系统当前是不是浅色外观。

    `ui.theme` 默认是 `system`，而此前 `palette_for()` 把「跟随系统」
    直接当成深色 —— 也就是说选了「跟随系统」的用户，在白色桌面上
    也永远拿到暗色窗口，连浅色主题长什么样都看不到。

    只读 Qt 的 `colorScheme`，不自己去猜桌面环境：Windows / macOS /
    GNOME / KDE 都会把它报给 Qt，自己按平台名判断反而会漏。
    """
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
        if app is None:
            return False
        scheme = app.styleHints().colorScheme()
        return scheme == Qt.ColorScheme.Light
    except Exception:  # noqa: BLE001 - 取不到就按深色，界面不该因此打不开
        return False


def resolve_theme(theme: str) -> str:
    """把配置里的主题值解析成实际渲染用的一档（`light` / `dark`）。

    只有显式写了 `system`（或没写）才去问系统偏好；认不出来的值一律按
    深色处理。认不出来往往是配置被手工改坏，此时「猜一个」比「按默认」
    更容易让人困惑 —— 而且系统的深浅色本来就是会被用户改的东西，
    拿它解释一个拼错的配置值，等于把一个谜面换成另一个谜面。
    """
    text = str(theme or "").strip().lower()
    if text == "light":
        return "light"
    if text in ("system", ""):
        return "light" if _system_prefers_light() else "dark"
    return "dark"


def palette_for(theme: str) -> Palette:
    return LIGHT if resolve_theme(theme) == "light" else DARK


def apply_palette(palette: Palette, target=None) -> None:
    """把色板同步到 Qt 的 `QPalette`。

    QSS 只负责 `build_stylesheet()` 里显式写到的部件。**没写到的部分由
    `QPalette` 决定**，而 `QPalette` 默认来自系统主题：白色桌面上启动，
    深色界面里那些没被 QSS 覆盖的原生绘制（勾选框底板、下拉箭头、
    滚动条轨道、禁用态文字）就仍是浅色 —— 深底上画浅色，勾选框整个
    看不见，用户只能看到一团灰。

    此前只有 QSS、没有这一步，所以「勾选框看不出勾没勾」不是某个控件的
    样式写漏了，而是这条链路缺了一环。`build_stylesheet()` 能盖住的
    控件越多，这一环越不明显，但永远盖不全。
    """
    from PySide6.QtGui import QColor, QGuiApplication, QPalette

    def color(value: str) -> QColor:
        return QColor(value)

    qpal = QPalette()
    qpal.setColor(QPalette.ColorRole.Window, color(palette.bg))
    qpal.setColor(QPalette.ColorRole.WindowText, color(palette.text))
    qpal.setColor(QPalette.ColorRole.Base, color(palette.surface))
    qpal.setColor(QPalette.ColorRole.AlternateBase, color(palette.surface_hover))
    qpal.setColor(QPalette.ColorRole.Text, color(palette.text))
    qpal.setColor(QPalette.ColorRole.Button, color(palette.surface))
    qpal.setColor(QPalette.ColorRole.ButtonText, color(palette.text_dim))
    qpal.setColor(QPalette.ColorRole.BrightText, color("#ffffff"))
    qpal.setColor(QPalette.ColorRole.Highlight, color(palette.accent))
    qpal.setColor(QPalette.ColorRole.HighlightedText, color(palette.accent_text))
    qpal.setColor(QPalette.ColorRole.PlaceholderText, color(palette.text_faint))
    qpal.setColor(QPalette.ColorRole.ToolTipBase, color(palette.surface))
    qpal.setColor(QPalette.ColorRole.ToolTipText, color(palette.text))
    # Light/Mid/Dark 是原生控件 3D 描边的灰度阶梯。给成中性的边框色，
    # 深浅两套主题下都不会出现「白色高光边」。
    qpal.setColor(QPalette.ColorRole.Light, color(palette.border))
    qpal.setColor(QPalette.ColorRole.Midlight, color(palette.border))
    qpal.setColor(QPalette.ColorRole.Mid, color(palette.text_faint))
    qpal.setColor(QPalette.ColorRole.Dark, color(palette.text_dim))
    qpal.setColor(QPalette.ColorRole.Shadow, color(palette.bg_alt))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        qpal.setColor(QPalette.ColorGroup.Disabled, role, color(palette.text_faint))

    application = target if target is not None else QGuiApplication.instance()
    if application is not None:
        application.setPalette(qpal)


def _kind_border_rules(palette: Palette) -> str:
    """按 `kind` 动态属性给卡片左侧色条上色。

    用 QSS 而不是逐卡片 `setStyleSheet`：卡片级样式表会盖掉
    `#Card[selected="true"]`，选中与未选中看起来一模一样 —— 用户点了没反应。
    """
    return "".join(
        f'QFrame#Card[kind="{kind}"] {{ border-left: 3px solid {color}; }}\n'
        for kind, color in KIND_COLORS.items()
    )


def build_stylesheet(palette: Palette) -> str:
    """生成全局 QSS。

    只覆盖必要的控件，其余交给 Qt 默认样式，
    避免「样样都改」导致原生控件失去平台观感。

    关于开头那条 `QWidget`：**故意不写 `background-color`**。
    写过 `QWidget { background-color: <底色> }` 之后，下拉框弹出列表、
    列表控件的选中行填充就全部失效 —— 深色主题的下拉里，当前选中项和
    其它项长得一模一样，用户看不出自己在选哪一个（浅色主题下因为底色
    本来就接近白色，反而看不出来，于是只在深色下报障）。这是
    「全局选择器给所有 QWidget 刷底色」压掉了子控件自带的选区绘制，
    不是某条选中态规则写漏了。改由 `QWidget#Root` / `QMainWindow` /
    `QDialog` 这几个真正的窗口容器各自负责底色，其余容器透明。
    """
    p = palette
    return f"""
QWidget {{
    color: {p.text};
    font-size: 13px;
}}
/* 只给「窗口级」容器刷底色。QScrollArea / QTabWidget 的页、分割器
   这些中间层必须透明：它们各自的底色由下面更具体的规则决定，
   在这里统一刷会把圆角卡片、滚动区视口都盖成一块方底。 */
QWidget#Root, QMainWindow, QDialog, QMessageBox {{
    background-color: {p.bg};
}}

/* ---- 顶部搜索区 ---- */
QFrame#Header {{
    background-color: {p.bg_alt};
    border-bottom: 1px solid {p.border};
}}
QLineEdit#SearchBox {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 10px;
    padding: 9px 14px;
    selection-background-color: {p.accent_dim};
    font-size: 14px;
}}
QLineEdit#SearchBox:focus {{
    border: 1px solid {p.accent};
}}

/* ---- 按钮 ---- */
QPushButton {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 6px 12px;
    color: {p.text};
}}
QPushButton:hover {{
    background-color: {p.surface_hover};
    border-color: {p.accent_dim};
}}
QPushButton:pressed {{
    background-color: {p.surface_active};
}}
QPushButton:checked {{
    background-color: {p.accent};
    border-color: {p.accent};
    color: {p.accent_text};
}}
QPushButton:disabled {{
    color: {p.text_faint};
    background-color: {p.bg_alt};
}}
QPushButton#Primary {{
    background-color: {p.accent};
    border-color: {p.accent};
    color: {p.accent_text};
    font-weight: 600;
}}
QPushButton#Primary:hover {{
    background-color: {p.accent_dim};
}}
/* `#Primary` 比 `:disabled` 更具体，会盖掉上面那条 disabled 规则 ——
   于是禁用的主按钮还是一条亮蓝色，看着能点。这里必须再钉一次。 */
QPushButton#Primary:disabled {{
    background-color: {p.bg_alt};
    border-color: {p.border};
    color: {p.text_faint};
}}
QPushButton#Ghost {{
    background-color: transparent;
    border: none;
    padding: 5px 9px;
}}
QPushButton#Ghost:hover {{
    background-color: {p.surface_hover};
    border-radius: 6px;
}}
QPushButton#Chip {{
    background-color: transparent;
    border: 1px solid {p.border};
    border-radius: 12px;
    padding: 3px 11px;
    color: {p.text_dim};
    font-size: 12px;
}}
QPushButton#Chip:hover {{
    border-color: {p.accent_dim};
    color: {p.text};
}}
QPushButton#Chip:checked {{
    background-color: {p.accent};
    border-color: {p.accent};
    color: {p.accent_text};
}}

/* ---- 列表 ---- */
QListView#ItemList {{
    background-color: {p.bg};
    border: none;
    outline: none;
    padding: 8px;
}}
QListView#ItemList::item {{
    border: none;
    margin: 3px 4px;
    border-radius: 10px;
}}
QListView#ItemList::item:selected {{
    background-color: {p.selection};
    color: {p.selection_text};
}}
QListView#ItemList::item:hover {{
    background-color: {p.surface_hover};
}}

/* ---- 卡片 ---- */
QFrame#Card {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-left: 3px solid {p.accent};
    border-radius: 10px;
}}
{_kind_border_rules(p)}QFrame#Card:hover {{
    background-color: {p.surface_hover};
}}
QFrame#Card[selected="true"] {{
    background-color: {p.selection};
    border: 1px solid {p.accent};
    border-left: 3px solid {p.accent};
}}
/* 卡片内标签一律透明：QLabel 默认会画一层 window 背景，
   在带圆角与色条的卡片上就变成一条突兀的实心长条。 */
QLabel#CardTitle {{
    color: {p.text};
    font-size: 13px;
    background: transparent;
}}
QLabel#CardMeta {{
    color: {p.text_faint};
    font-size: 11px;
    background: transparent;
}}
QLabel#CardKind {{
    color: {p.accent};
    font-size: 11px;
    font-weight: 600;
    background: transparent;
}}
QLabel#CardPin {{
    color: {p.warning};
    font-size: 12px;
    background: transparent;
}}
QLabel#EmptyHint {{
    color: {p.text_faint};
    font-size: 14px;
    background: transparent;
}}

/* ---- 空历史引导页 ---- */
QLabel#EmptyStateTitle {{
    color: {p.text};
    font-size: 20px;
    font-weight: 600;
    background: transparent;
}}
QLabel#EmptyStateHint {{
    color: {p.text_dim};
    font-size: 13px;
    background: transparent;
}}
QFrame#EmptyStateCard {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 10px;
}}
QLabel#EmptyStateCardTitle {{
    color: {p.text};
    font-size: 13px;
    font-weight: 600;
    background: transparent;
}}
QLabel#EmptyStateKey {{
    color: {p.text_faint};
    font-size: 12px;
    background: transparent;
}}
QLabel#EmptyStateValue {{
    color: {p.text_dim};
    font-size: 12px;
    background: transparent;
}}
QLabel#EmptyStateNote {{
    color: {p.text_faint};
    font-size: 11px;
    background: transparent;
}}

/* ---- 侧栏与分组 ---- */
QFrame#Sidebar {{
    background-color: {p.bg_alt};
    border-right: 1px solid {p.border};
}}
QLabel#SectionTitle {{
    color: {p.text_faint};
    font-size: 11px;
    font-weight: 600;
    background: transparent;
}}
QLabel#StatValue {{
    color: {p.text};
    font-size: 18px;
    font-weight: 600;
    background: transparent;
}}
QLabel#StatLabel {{
    color: {p.text_faint};
    font-size: 11px;
    background: transparent;
}}
QListWidget#SideList {{
    background-color: transparent;
    border: none;
    outline: none;
}}
QListWidget#SideList::item {{
    padding: 7px 10px;
    border-radius: 7px;
    color: {p.text_dim};
}}
QListWidget#SideList::item:selected {{
    background-color: {p.selection};
    color: {p.selection_text};
}}
QListWidget#SideList::item:hover {{
    background-color: {p.surface_hover};
}}

/* ---- 预览 ---- */
QFrame#Preview {{
    background-color: {p.bg_alt};
    border-left: 1px solid {p.border};
}}
QTextEdit#PreviewBody, QPlainTextEdit#PreviewBody {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 10px;
    color: {p.text};
    font-family: "JetBrains Mono", "Cascadia Code", "SF Mono",
                 "DejaVu Sans Mono", Consolas, monospace;
    font-size: 12px;
}}

/* ---- 滚动条 ---- */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
/* 滑块的色必须单独给：用 `border` 时它和背景只差 1.35 对比度，
   深色下等于没有滚动条 —— 用户看到「内容被截断了，但没地方能拖」。 */
QScrollBar::handle:vertical {{
    background: {p.scrollbar};
    border-radius: 5px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: {p.scrollbar_hover};
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {p.scrollbar};
    border-radius: 5px;
    min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {p.scrollbar_hover};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

/* ---- 其它 ---- */
QSplitter::handle {{
    background-color: {p.border};
    width: 1px;
}}
QToolTip {{
    background-color: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 6px;
    padding: 5px 8px;
}}
QMenu {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 5px;
}}
QMenu::item {{
    padding: 6px 22px 6px 12px;
    border-radius: 6px;
}}
QMenu::item:selected {{
    background-color: {p.selection};
    color: {p.selection_text};
}}
QMenu::item:disabled {{
    color: {p.text_faint};
}}
/* 菜单项前的勾选框（「暂停采集」「固定」这些可勾选项）。
   这里**只给尺寸，不给底色和边框** —— 一旦 QSS 画了那个方块，
   勾形就得自己补（`QMenu::indicator:checked` 只能刷底色，不能画勾），
   于是「勾上」看起来就是一块纯蓝方块，和「未勾选」的空框同样认不出。
   留白之后绘制交回 `IndicatorStyle.PE_IndicatorMenuCheckMark`，
   它用正文色画一道实心对勾，深浅两套主题下都清楚。
   尺寸必须给：不给的话指示器按原生尺寸走，和这里的行高对不齐。 */
QMenu::indicator {{
    width: 14px;
    height: 14px;
    margin-left: 8px;
    margin-right: 4px;
}}
QMenu::separator {{
    height: 1px;
    background: {p.border};
    margin: 4px 8px;
}}
QStatusBar {{
    background-color: {p.bg_alt};
    border-top: 1px solid {p.border};
    color: {p.text_dim};
    font-size: 11px;
    /* 状态栏高度由内容撑开，11px 字号若不给内边距，
       文字的下半截会被状态栏边界切掉（截图里看得最清楚）。 */
    min-height: 22px;
}}
QStatusBar::item {{
    border: none;
}}
QStatusBar QLabel {{
    background: transparent;
    color: {p.text_dim};
    font-size: 11px;
    padding: 3px 6px;
}}
QCheckBox {{
    spacing: 7px;
    color: {p.text};
}}
/* 输入类控件**故意不给 QComboBox 写 background-color**。
   这是一个实测出来的坑，不是漏写：只要 QSS 里给 QComboBox 设了底色，
   Qt 就会把它判成「被样式表接管」，连带把它的弹出列表也交给 QSS 画 ——
   于是 `selection-background-color` 与 `::item:selected` **全部失效**，
   弹出的下拉里所有行都是同一块底色，用户看不出当前选中的是哪一项。
   浅色主题下底色本来就接近白色，看不出来；深色主题下就成了
   「下拉框没有选中态」。底色交给 `QPalette.Base`（见 `apply_palette`），
   边框、圆角、内边距仍由这里给。 */
QSpinBox, QLineEdit {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 7px;
    padding: 5px 8px;
    color: {p.text};
}}
QComboBox {{
    border: 1px solid {p.border};
    border-radius: 7px;
    padding: 5px 8px;
    color: {p.text};
}}
/* 右侧留出按钮的宽度：不留的话「300 ms」会顶到上下箭头下面，
   数字被箭头压掉一截，看起来像显示不全。 */
QSpinBox, QComboBox {{
    padding-right: 22px;
}}
QSpinBox:focus, QComboBox:focus, QLineEdit:focus {{
    border-color: {p.accent};
}}
/* 刻意不给 `QComboBox::drop-down` / `::down-arrow` 设规则：
   一旦覆盖过子控件，Qt 就不再画默认的箭头，需要自己提供图片。
   曾写过 `QComboBox::drop-down {{ border:none; width:20px; }}`，结果
   下拉框里那个箭头整块消失 —— 用户看到的是一个「不知道能不能点」的输入框。
   同理，QSpinBox 的上下箭头交给 `IndicatorStyle` 用矢量画（见下），
   不依赖任何位图资源。 */

/* 弹出列表（下拉、补全）的选中行。
   这里三处都要写，缺一行就有一处看不出来：
   1. `selection-*` 给「当前高亮行」（键盘上下走的那一行）；
   2. `::item:selected` 给「已经选中的值」（打开下拉时落点那一行）；
   3. `::item:hover` 给鼠标滑过。
   此前只写了 `selection-background-color: surface_active`，而
   `surface_active` 与 `surface` 只差 1.3 对比度 —— 深色下三者
   全是同一块底色，用户看不出自己选的是哪一项。 */
QComboBox QAbstractItemView {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    outline: none;
    selection-background-color: {p.selection};
    selection-color: {p.selection_text};
}}
QComboBox QAbstractItemView::item {{
    padding: 5px 9px;
    min-height: 20px;
    color: {p.text};
    background: transparent;
}}
QComboBox QAbstractItemView::item:hover {{
    background-color: {p.surface_hover};
    color: {p.text};
}}
QComboBox QAbstractItemView::item:selected {{
    background-color: {p.selection};
    color: {p.selection_text};
}}
QComboBox QAbstractItemView::item:disabled {{
    color: {p.text_faint};
}}

/* 通用列表/表格/树的选中行。主窗口的卡片列表走的是自定义 QFrame，
   但设置页、预览、补全这些地方用的是原生 view，同一套配色。 */
QListView, QTreeView, QTableView, QListWidget, QTreeWidget, QTableWidget {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    outline: none;
    selection-background-color: {p.selection};
    selection-color: {p.selection_text};
}}
QListView::item:selected, QTreeView::item:selected,
QTableView::item:selected, QListWidget::item:selected,
QTreeWidget::item:selected, QTableWidget::item:selected {{
    background-color: {p.selection};
    color: {p.selection_text};
}}
QListView::item:hover, QTreeView::item:hover,
QListWidget::item:hover, QTreeWidget::item:hover {{
    background-color: {p.surface_hover};
}}
QHeaderView::section {{
    background-color: {p.bg_alt};
    color: {p.text_dim};
    border: none;
    border-right: 1px solid {p.border};
    border-bottom: 1px solid {p.border};
    padding: 4px 8px;
}}
QTabWidget::pane {{
    border: 1px solid {p.border};
    border-radius: 8px;
    background-color: {p.surface};
}}
/* 设置页每页内容放在滚动区里。QScrollArea 的视口默认吃 QPalette.Base，
   不清掉的话页面上会浮出一块与对话框底色不同的矩形，看着像内容错位。 */
QScrollArea#SettingsPage, QScrollArea#SettingsPage > QWidget > QWidget {{
    background: transparent;
    border: none;
}}
QTabBar::tab {{
    background: transparent;
    color: {p.text_dim};
    padding: 7px 15px;
    border-radius: 7px;
    margin-right: 3px;
}}
QTabBar::tab:selected {{
    background-color: {p.selection};
    color: {p.selection_text};
}}
QGroupBox {{
    border: 1px solid {p.border};
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 10px;
    color: {p.text_dim};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: {p.text_faint};
    font-size: 11px;
    font-weight: 600;
}}
"""


class IndicatorStyle(QProxyStyle):
    """把勾选框、单选框、下拉/微调箭头改成矢量绘制。

    为什么需要它：`build_stylesheet()` 给这些控件设了底板（深色背景 +
    边框），而**图标本身仍由样式画**。在深色主题下这些图标跟着
    `QPalette` 走，白色桌面启动时会拿到浅色图标 —— 深底浅边，勾选框
    只剩一个几乎看不见的方块，用户根本看不出自己勾没勾。这是实测
    截图上「设置页每个选项都像没勾」的直接原因。

    用矢量而不是几张 PNG：图标要在 100%~200% 缩放下都清楚，也要跟着
    主题换色。位图方案要按 DPI × 主题各备一套，迟早漏一个。

    接在原生样式外面（`QProxyStyle(base)`）而不是自己从零画：按钮、
    滚动条、输入框的其余部分继续用平台实现，只接管那几个图标。
    """

    #: 箭头宽度/高度，单位像素（逻辑像素，高分屏由 Qt 自己缩放）
    ARROW_HALF_WIDTH = 3.5
    ARROW_HALF_HEIGHT = 2.6

    def __init__(self, base: str = "Fusion") -> None:
        super().__init__(base)
        #: 当前主题的色板，由 `apply_theme()` 设置
        self._palette: Optional[Palette] = None

    def set_palette_spec(self, palette: Palette) -> None:
        self._palette = palette

    # ---- 取色 ----

    def _text_color(self, widget):
        from PySide6.QtGui import QColor, QPalette

        if widget is not None:
            return widget.palette().color(QPalette.ColorRole.WindowText)
        if self._palette is not None:
            return QColor(self._palette.text)
        return QColor("#eef1f7")

    def _border_color(self, widget):
        from PySide6.QtGui import QColor

        if self._palette is not None:
            return QColor(self._palette.text_faint)
        return self._text_color(widget)

    def _accent_color(self, widget):
        from PySide6.QtGui import QColor

        if self._palette is not None:
            return QColor(self._palette.accent)
        return self._text_color(widget)

    # ---- 绘制 ----

    def drawPrimitive(self, element, option, painter, widget=None) -> None:  # noqa: N802 - Qt 命名
        from PySide6.QtWidgets import QStyle

        arrows = {
            QStyle.PrimitiveElement.PE_IndicatorArrowDown: 1,
            QStyle.PrimitiveElement.PE_IndicatorSpinDown: 1,
            QStyle.PrimitiveElement.PE_IndicatorArrowUp: -1,
            QStyle.PrimitiveElement.PE_IndicatorSpinUp: -1,
        }
        if element in arrows:
            self._draw_arrow(element, arrows[element], option, painter, widget)
            return

        if element in (
            QStyle.PrimitiveElement.PE_IndicatorCheckBox,
            QStyle.PrimitiveElement.PE_IndicatorRadioButton,
        ):
            self._draw_tick(
                element == QStyle.PrimitiveElement.PE_IndicatorRadioButton,
                option,
                painter,
                widget,
            )
            return

        if element == QStyle.PrimitiveElement.PE_IndicatorMenuCheckMark:
            # 菜单项被勾上时的那道勾。交给原生会按系统配色画浅灰对勾，
            # 深色菜单底上等于看不见 —— 用户不知道自己是否已经暂停采集。
            self._draw_menu_tick(option, painter, widget)
            return

        super().drawPrimitive(element, option, painter, widget)

    def _draw_arrow(self, element, sign: int, option, painter, widget) -> None:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPainter, QPen, QPolygonF

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = option.rect
        cx = rect.center().x() + 0.5
        cy = rect.center().y() + 0.5
        half_w = self.ARROW_HALF_WIDTH
        half_h = self.ARROW_HALF_HEIGHT
        if sign > 0:
            points = [
                QPointF(cx - half_w, cy - half_h),
                QPointF(cx + half_w, cy - half_h),
                QPointF(cx, cy + half_h),
            ]
        else:
            points = [
                QPointF(cx - half_w, cy + half_h),
                QPointF(cx + half_w, cy + half_h),
                QPointF(cx, cy - half_h),
            ]
        color = self._text_color(widget)
        painter.setPen(QPen(color, 1.2))
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF(points))
        painter.restore()

    def _draw_menu_tick(self, option, painter, widget) -> None:
        """画菜单项被勾上时的那道对勾。

        QSS 那边只给了指示器的尺寸、不画方块（见 `QMenu::indicator`），
        于是这个 primitive 会落到这里，由我们自己画勾 —— 深浅两套主题
        都能控制颜色，不会跟着系统原生的浅色指示器走。
        """
        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import QPainter, QPen
        from PySide6.QtWidgets import QStyle

        if not option.state & QStyle.StateFlag.State_On:
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(option.rect).adjusted(1, 1, -1, -1)
        # 用正文色而不是强调色：浅色主题的强调蓝偏亮，白勾压上去几乎看不出，
        # 而正文色在两套主题下都和菜单底色有足够对比。
        pen = QPen(self._text_color(widget), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolyline(
            [
                QPointF(rect.left() + rect.width() * 0.26, rect.center().y()),
                QPointF(rect.center().x() - 0.5, rect.bottom() - rect.height() * 0.28),
                QPointF(rect.right() - rect.width() * 0.24, rect.top() + rect.height() * 0.30),
            ]
        )
        painter.restore()

    def _draw_tick(self, radio: bool, option, painter, widget) -> None:
        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import QColor, QPainter, QPen
        from PySide6.QtWidgets import QStyle

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        state = option.state
        checked = bool(state & QStyle.StateFlag.State_On)
        enabled = bool(state & QStyle.StateFlag.State_Enabled)
        partially = bool(state & QStyle.StateFlag.State_NoChange)
        rect = QRectF(option.rect).adjusted(0.5, 0.5, -0.5, -0.5)

        accent = self._accent_color(widget)
        outline = self._border_color(widget)
        if not enabled:
            outline = QColor(outline)
            outline.setAlpha(110)

        if checked or partially:
            painter.setBrush(accent)
            painter.setPen(QPen(accent, 1))
            if radio:
                painter.drawEllipse(rect)
            else:
                painter.drawRoundedRect(rect, 3, 3)
            # 对勾用底色画，深浅两套主题下都和强调色有足够对比
            mark = QColor("#ffffff" if accent.lightness() < 170 else "#14161c")
            pen = QPen(mark, 1.8)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if partially:
                painter.drawLine(
                    QPointF(rect.left() + 3, rect.center().y()),
                    QPointF(rect.right() - 3, rect.center().y()),
                )
            else:
                painter.drawPolyline(
                    [
                        QPointF(rect.left() + rect.width() * 0.24, rect.center().y()),
                        QPointF(rect.center().x() - 0.5, rect.bottom() - rect.height() * 0.26),
                        QPointF(rect.right() - rect.width() * 0.22, rect.top() + rect.height() * 0.28),
                    ]
                )
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(outline, 1.2))
            if radio:
                painter.drawEllipse(rect)
            else:
                painter.drawRoundedRect(rect, 3, 3)
        painter.restore()


def install_style(palette: Palette, application=None) -> Optional["IndicatorStyle"]:
    """把 `IndicatorStyle` 装到应用上，并同步配色。

    必须走 `QApplication.setStyle()` 而不是给单个控件设：勾选框、下拉框、
    微调框散落在主窗口、设置页、转换结果窗口、快速粘贴浮层四处，
    逐个装等于把「漏一个」留成常态。

    返回装上的样式对象；拿不到 `QApplication` 时返回 None（测试里用桩
    应用时会走到这条路，不该因此报错）。
    """
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    target = application if application is not None else QGuiApplication.instance()
    if not isinstance(target, QApplication):
        return None

    current = target.style()
    if isinstance(current, IndicatorStyle):
        style = current
    else:
        style = IndicatorStyle(current.objectName() or "Fusion")
        target.setStyle(style)
    style.set_palette_spec(palette)
    return style


def toast_style(palette: Palette) -> str:
    """浮层提示（Quick Paste 窗口）的样式。"""
    p = palette
    return f"""
QFrame#QuickPanel {{
    background-color: {p.surface};
    border: 1px solid {p.accent_dim};
    border-radius: 14px;
}}
QFrame#QuickRow {{
    background-color: transparent;
    border-radius: 9px;
}}
/* 浮层里键盘指向的那一行。用 `surface_active` 时它和面板底色只差
   1.3 对比度，深色下等于没有高亮 —— 用户按上下键看不出焦点在哪。 */
QFrame#QuickRow[active="true"] {{
    background-color: {p.selection};
    color: {p.selection_text};
}}
QLabel#QuickIndex {{
    color: {p.accent};
    font-weight: 700;
    font-size: 12px;
}}
QLabel#QuickText {{
    color: {p.text};
    font-size: 13px;
}}
QLabel#QuickHint {{
    color: {p.text_faint};
    font-size: 11px;
}}
QLineEdit#QuickSearch {{
    background-color: {p.bg};
    border: 1px solid {p.border};
    border-radius: 9px;
    padding: 8px 12px;
    font-size: 14px;
}}
QLineEdit#QuickSearch:focus {{
    border: 1px solid {p.accent};
}}
/* 占位提示必须显式给色：浮层用的是独立局部样式表（toast_style），
   不指定时占位文字会沿用主窗口的暗色，在深色面板上几乎看不见 ——
   表现为「输入框里好像什么都没有」。 */
QLineEdit {{
    placeholder-text-color: {p.text_faint};
    min-height: 20px;
}}
/* 转换结果窗口独立成窗、不继承主窗口样式表，这几个控件名由它使用 */
QTextEdit#TransformBody {{
    background-color: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 10px;
}}
QPushButton#Primary {{
    background-color: {p.accent};
    border: 1px solid {p.accent};
    color: {p.accent_text};
    font-weight: 600;
}}
QPushButton#Primary:hover {{
    background-color: {p.accent_dim};
}}
QPushButton#Ghost {{
    background-color: transparent;
    border: 1px solid {p.border};
    border-radius: 6px;
    padding: 3px 9px;
    color: {p.text_dim};
    font-size: 12px;
}}
QPushButton#Ghost:hover {{
    border-color: {p.accent_dim};
    color: {p.text};
}}
"""
