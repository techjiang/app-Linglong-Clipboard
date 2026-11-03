# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：三平台共用一份。

产出：
- Windows: dist/玲珑剪切板.exe（单文件，无控制台窗口）
- macOS:   dist/玲珑剪切板.app
- Linux:   dist/linglong-clipboard（单文件）

隐藏导入与排除项都写了注释说明理由，
否则下次改动时会因为「不知道为什么有这行」而误删。
"""

import os
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
    "PySide6.QtNetwork",
    "PySide6.QtPrintSupport",
    "PySide6.QtDBus",
    "PySide6.QtXml",
    "PySide6.QtConcurrent",
    "PySide6.QtUiTools",
    "PySide6.QtSerialBus",
    "PySide6.QtStateMachine",
    "PySide6.QtWebSockets",
    "PySide6.QtSpatialAudio",
    "PySide6.QtTextToSpeech",
    "PySide6.QtHttpServer",
    # 开发/测试期才用的包，最终用户用不到
    "pytest",
    "setuptools",
    "pip",
    "wheel",
    "PyInstaller",
]

# `excludes` 只作用于 Python 层：PySide6 的 hook 会把整套 Qt 库直接塞进
# binaries，管不到。这里按文件名做最后一道过滤 —— 剪贴板只用
# Core / Gui / Widgets，其余 Qt 库对最终用户毫无意义，留着体积翻倍还拖慢启动。
_DROP_QT_LIBS = (
    "libQt6Qml", "libQt6Quick", "libQt6VirtualKeyboard", "libQt6Pdf",
    "libQt6Sql", "libQt6Test", "libQt6Designer", "libQt6Help",
    "libQt6Multimedia", "libQt6Charts", "libQt6DataVisualization",
    "libQt6Bluetooth", "libQt6Nfc", "libQt6Positioning", "libQt6SerialPort",
    "libQt6SerialBus", "libQt6SpatialAudio", "libQt6Graphs", "libQt6TextToSpeech",
    "libQt6HttpServer", "libQt6WebEngine", "libQt6WebSockets", "libQt6StateMachine",
    "libQt6Concurrent", "libQt6PrintSupport", "libQt6SvgWidgets", "libQt6Svg",
)

#: Qt 插件同理：只留当前平台真正会用到的
_DROP_QT_PLUGINS = (
    "qmltooling", "qml", "virtualkeyboard", "designer", "sqldrivers",
    "multimedia", "printsupport", "webview", "scenegraph", "assetimporters",
    "position", "sensors", "canbus", "nfc", "bluetooth",
)


def _keep(entry) -> bool:
    name = str(entry[0]).replace("\\", "/")
    lowered = name.lower()
    if any(token.lower() in lowered for token in _DROP_QT_LIBS):
        return False
    if "/plugins/" in lowered:
        return not any(token in lowered for token in _DROP_QT_PLUGINS)
    return True

# 入口必须是「绝对导入」的启动脚本：直接把 `linglong_clip/__main__.py`
# 交给 PyInstaller 会变成顶层脚本，里面的相对导入失去包上下文，
# 产物一运行就 ImportError（开发时都是 `-m` 跑，察觉不到）。
_ENTRY = project_root / "packaging" / "_entry.py"

a = Analysis(
    [str(_ENTRY)],
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
a.binaries = TOC([entry for entry in a.binaries if _keep(entry)])
a.datas = TOC([entry for entry in a.datas if _keep(entry)])

pyz = PYZ(a.pure)

# 单文件还是目录模式由 build.py 通过环境变量传递。
# 不能给 PyInstaller 同时传 .spec 和 --onefile/--onedir（它会直接报错拒绝），
# 所以这个选择只能写在 spec 的结构里：onefile = 内联 binaries 且没有 COLLECT。
ONEDIR = os.environ.get("LINGLONG_ONEDIR", "0") == "1"

exe = EXE(
    pyz,
    a.scripts,
    [] if ONEDIR else a.binaries,
    [] if ONEDIR else a.datas,
    [],
    name="linglong-clipboard" if sys.platform != "win32" else "玲珑剪切板",
    # 目录模式：EXE 只是 COLLECT 的输入，不该落到 dist/ 里和目录同名
    exclude_binaries=ONEDIR,
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

if ONEDIR:
    # EXE 的产物目录要挪进 build/：目录模式下 COLLECT 会建一个与 EXE 同名的
    # 目录，EXE 也留在 dist/ 的话两者同名，PyInstaller 会报
    # `Resource '...' is not a valid file!`（它在 dist 里看到的是同名文件）。
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=not sys.platform.startswith("win"),
        upx=False,
        name="linglong-clipboard" if sys.platform != "win32" else "玲珑剪切板",
    )
