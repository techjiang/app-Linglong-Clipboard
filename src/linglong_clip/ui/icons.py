"""应用图标。

图标来源有两层，按可用性回落：

1. `assets/icon/` 下的多档位图（打包时随资源一起带上）——各平台任务栏、
   标题栏、托盘认的都是它；
2. 代码画的矢量图（`tray.build_icon`）——资源缺失时兜底，界面不会因为
   找不到图标而变成一堆方框。

之所以不只用代码画的：从 PyInstaller 产物或安装包启动时，任务栏图标来自
**可执行文件自身嵌入的图标**，代码里画得再好看也补不上那一格空白。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from PySide6.QtGui import QIcon

from ..utils.resources import icon_png


@lru_cache(maxsize=1)
def app_icon() -> QIcon:
    """多尺寸应用图标；资源缺失时返回空 QIcon，由调用方决定怎么兜底。"""
    icon = QIcon()
    added = 0
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        path = icon_png(size)
        if path is not None:
            icon.addFile(str(path))
            added += 1
    if not added:
        return QIcon()
    return icon


def window_icon(palette=None) -> QIcon:
    """窗口 / 托盘用的图标，带代码绘制兜底。"""
    icon = app_icon()
    if not icon.isNull():
        return icon
    from .tray import build_icon  # 延迟导入：避免 tray <-> icons 循环

    if palette is None:
        from .theme import DARK

        palette = DARK
    return build_icon(palette)
