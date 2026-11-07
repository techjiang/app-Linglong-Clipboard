"""打包资源的定位。

源码运行时资源在仓库的 `assets/` 下；PyInstaller 打包后它们被解到
`sys._MEIPASS`（单文件模式）或可执行文件旁边的目录（目录模式）。
两种形态都从这里统一出口，调用方不必自己判断「我是不是被打包了」。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

#: 图标基名，与 packaging/icons.py 的产物保持一致
ICON_BASENAME = "linglong-clipboard"


def _resource_roots() -> list:
    """按优先级列出可能的资源根目录。"""
    roots = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        # 单文件模式解压目录 / 目录模式下 PyInstaller 也走这里
        roots.append(Path(bundle))
    if getattr(sys, "frozen", False):
        # 目录模式：可执行文件同级（onedir 布局），便于用户替换图标
        roots.append(Path(sys.executable).resolve().parent)
    # 源码运行：src/linglong_clip/utils/resources.py -> 仓库根
    roots.append(Path(__file__).resolve().parents[3])
    return roots


def asset_path(*parts: str) -> Optional[Path]:
    """找 `assets/<parts...>`，找不到返回 None（不抛异常）。

    找不到不是错误：图标只是外观，缺了应该退回到代码绘制的版本，
    而不是让整个界面起不来。
    """
    for root in _resource_roots():
        candidate = root.joinpath("assets", *parts)
        if candidate.exists():
            return candidate
    return None


def icon_path() -> Optional[Path]:
    """平台对应的图标文件路径。"""
    if sys.platform.startswith("win"):
        return asset_path("icon", f"{ICON_BASENAME}.ico")
    if sys.platform == "darwin":
        return asset_path("icon", f"{ICON_BASENAME}.icns")
    return asset_path("icon", "png", f"{ICON_BASENAME}-256.png")


def icon_png(size: int) -> Optional[Path]:
    """指定尺寸的 PNG 图标（任意平台都可能有，用于窗口图标）。"""
    return asset_path("icon", "png", f"{ICON_BASENAME}-{size}.png")
