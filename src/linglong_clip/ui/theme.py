"""主题与样式表。

配色走「深色玻璃质感 + 单一强调色」，避免多色堆叠导致视觉噪音。
样式表集中在 `build_stylesheet` 生成，方便暗/亮两套主题共用结构。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


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
    border="#2a2f3d",
    text="#eef1f7",
    text_dim="#a4adc2",
    text_faint="#6c7590",
    accent="#4c8dff",
    accent_dim="#2f5fb8",
    accent_text="#ffffff",
    danger="#ff5f6d",
    warning="#f2b447",
    success="#3ddc97",
    shadow="rgba(0, 0, 0, 0.45)",
)

LIGHT = Palette(
    name="light",
    bg="#f4f6fb",
    bg_alt="#eaeef6",
    surface="#ffffff",
    surface_hover="#f0f3f9",
    surface_active="#e4e9f3",
    border="#d7dde9",
    text="#1b1f2a",
    text_dim="#5a6376",
    text_faint="#8d96a8",
    accent="#2f6fed",
    accent_dim="#1f4fae",
    accent_text="#ffffff",
    danger="#d93a47",
    warning="#b8801f",
    success="#1f9e6b",
    shadow="rgba(15, 24, 46, 0.12)",
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


def palette_for(theme: str) -> Palette:
    return LIGHT if theme == "light" else DARK


def build_stylesheet(palette: Palette) -> str:
    """生成全局 QSS。

    只覆盖必要的控件，其余交给 Qt 默认样式，
    避免「样样都改」导致原生控件失去平台观感。
    """
    p = palette
    return f"""
QWidget {{
    background-color: {p.bg};
    color: {p.text};
    font-size: 13px;
}}
QWidget#Root, QMainWindow, QDialog {{
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
    background-color: {p.surface_active};
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
QFrame#Card[selected="true"] {{
    background-color: {p.surface_active};
    border: 1px solid {p.accent};
    border-left: 3px solid {p.accent};
}}
QLabel#CardTitle {{
    color: {p.text};
    font-size: 13px;
}}
QLabel#CardMeta {{
    color: {p.text_faint};
    font-size: 11px;
}}
QLabel#CardKind {{
    color: {p.accent};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#CardPin {{
    color: {p.warning};
    font-size: 12px;
}}
QLabel#EmptyHint {{
    color: {p.text_faint};
    font-size: 14px;
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
}}
QLabel#StatValue {{
    color: {p.text};
    font-size: 18px;
    font-weight: 600;
}}
QLabel#StatLabel {{
    color: {p.text_faint};
    font-size: 11px;
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
    background-color: {p.surface_active};
    color: {p.text};
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
QScrollBar::handle:vertical {{
    background: {p.border};
    border-radius: 5px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: {p.text_faint};
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {p.border};
    border-radius: 5px;
    min-width: 28px;
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
    background-color: {p.surface_active};
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
}}
QCheckBox {{
    spacing: 7px;
    color: {p.text};
}}
QSpinBox, QComboBox, QLineEdit {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 7px;
    padding: 5px 8px;
    color: {p.text};
}}
QSpinBox:focus, QComboBox:focus, QLineEdit:focus {{
    border-color: {p.accent};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    selection-background-color: {p.surface_active};
}}
QTabWidget::pane {{
    border: 1px solid {p.border};
    border-radius: 8px;
    background-color: {p.surface};
}}
QTabBar::tab {{
    background: transparent;
    color: {p.text_dim};
    padding: 7px 15px;
    border-radius: 7px;
    margin-right: 3px;
}}
QTabBar::tab:selected {{
    background-color: {p.surface_active};
    color: {p.text};
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
QFrame#QuickRow[active="true"] {{
    background-color: {p.surface_active};
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
"""
