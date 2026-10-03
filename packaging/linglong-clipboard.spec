# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：三平台共用一份。

产出：
- Windows: dist/玲珑剪切板.exe（单文件，无控制台窗口）
- macOS:   dist/玲珑剪切板.app
- Linux:   dist/linglong-clipboard（单文件）

隐藏导入与排除项都写了注释说明理由，
否则下次改动时会因为「不知道为什么有这行」而误删。
"""

import sys
from pathlib import Path

project_root = Path(SPECPATH).parent
src_dir = project_root / "src"

# ---- 需要显式声明的导入 ----
# 平台后端是按名称动态导入的（见 platform/__init__.py 的 detect_backend），
# 静态分析看不到，必须列进来，否则打包后会报 ModuleNotFoundError。
hidden_imports = [
    "linglong_clip.platform.windows",
    "linglong_clip.platform.linux",
    "linglong_clip.platform.macos",
    "linglong_clip.platform.null",
]
if sys.platform == "darwin":
    hidden_imports.append("Quartz")
if sys.platform.startswith("linux"):
    hidden_imports.append("Xlib")

# ---- 明确排除，压缩体积 ----
# Qt 全家桶很大，只保留用得到的模块。
excludes = [
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "PIL",
    "scipy",
    # Qt 里没被用到的子模块
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSerialPort",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtOpenGL",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtSvgWidgets",
]

a = Analysis(
    [str(src_dir / "linglong_clip" / "__main__.py")],
    pathex=[str(src_dir)],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="linglong-clipboard" if sys.platform != "win32" else "玲珑剪切板",
    debug=False,
    bootloader_ignore_signals=False,
    strip=not sys.platform.startswith("win"),
    upx=False,  # UPX 压缩后杀软误报率高，得不偿失
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # GUI 程序不开控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=sys.platform == "darwin",
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # macOS 需要 .app 包才能出现在 Dock 与「应用程序」里
    icon=None,
    bundle_identifier="cool.cnb.linglong.clipboard" if sys.platform == "darwin" else None,
)
