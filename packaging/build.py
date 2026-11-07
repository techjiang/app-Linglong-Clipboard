#!/usr/bin/env python3
"""一键打包脚本：在各自平台上运行，产出可直接交付的发行物。

用法：
    python packaging/build.py                # 当前平台，目录模式 + 安装包
    python packaging/build.py --onefile      # 单文件版（便携，不装）
    python packaging/build.py --no-installer # 只出便携版压缩包
    python packaging/build.py --clean        # 先清理再打包

产出：
    Windows  dist/linglong-clipboard-<ver>-windows-setup.exe   安装版（推荐）
             dist/linglong-clipboard-windows.zip               便携版（解压即用）
    Linux    dist/linglong-clipboard_<ver>_amd64.deb           安装版（推荐）
             dist/linglong-clipboard-linux.tar.gz              便携版
    macOS    dist/linglong-clipboard-macos.zip                 .app

**为什么默认目录模式**：单文件版每次启动要把 60 MB 的 Qt 解压到临时目录，
冷启动要几秒，托盘图标在解压期间还是空白。安装包本来就会把文件摊到安装目录，
单文件的「一个文件」在这里没有收益，代价全落在用户身上。

注意：PyInstaller 不支持交叉编译。Windows 的 .exe 由 Wine + Windows 版
Python 打（见 packaging/build_windows_wine.sh），macOS 的 .app 只能在 macOS 上打。
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "linglong-clipboard.spec"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
ASSETS = ROOT / "assets"

#: 产物基名，与 spec 里的 name 保持一致。
#: 一律 ASCII：中文 exe 名会在跨平台打包链路上丢掉（见 spec 里的注释），
#: 用户看到的中文名来自快捷方式与窗口标题。
APP_NAME = "linglong-clipboard"

#: 发行包统一用 ASCII 名，下载后不会变成一堆乱码
RELEASE_BASENAME = "linglong-clipboard"


def version() -> str:
    """从源码读版本号，避免 pyproject / __init__ / 安装包三处各写一遍。"""
    text = (ROOT / "src" / "linglong_clip" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"\'')
    raise SystemExit("读不到版本号")


def platform_label() -> str:
    if sys.platform.startswith("win"):
        return "Windows"
    if sys.platform == "darwin":
        return "macOS"
    return "Linux"


#: 各平台上「装了更好、没装则相应能力降级」的可选依赖。
#: 缺了这些 PyInstaller 照样能打包成功，但打出来的包里会**少一整块功能**，
#: 而使用者在界面上只会看到「不可用」，不知道是包的问题还是自己环境的问题。
OPTIONAL_DEPS: dict[str, tuple[str, str, str]] = {
    "linux": (
        "Xlib",
        "python-xlib",
        "X11 上的全局快捷键与来源应用识别会不可用（--stats 里「主选择区」也会显示未启用）",
    ),
    "darwin": (
        "Quartz",
        "pyobjc-framework-Quartz",
        "macOS 上的全局快捷键会不可用",
    ),
}


def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def check_dependencies(strict: bool = False) -> bool:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("缺少 PyInstaller。请先安装：pip install -r requirements-dev.txt")
        return False
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("缺少 PySide6。请先安装：pip install -r requirements.txt")
        return False

    key = _platform_key()
    if key in OPTIONAL_DEPS:
        module, package, consequence = OPTIONAL_DEPS[key]
        try:
            __import__(module)
        except ImportError:
            message = (
                f"缺少可选依赖 {package}（import {module}）。\n"
                f"  打出来的包会少一块功能：{consequence}\n"
                f"  补装：pip install {package}"
            )
            if strict:
                print(f"错误：{message}", file=sys.stderr)
                return False
            print(f"警告：{message}")
    return True


def ensure_icons() -> bool:
    """图标是打包的硬前置：缺了它产物在任务栏里就是个空白方块。

    Windows / macOS 的图标直接嵌进可执行文件，缺了 PyInstaller 不报错、
    只是打出一个没脸的 exe —— 用户在资源管理器里就会看到默认白纸图标。
    所以这里主动生成/校验一次，而不是等用户来投诉。
    """
    needed = [
        ASSETS / "icon" / "linglong-clipboard.ico",
        ASSETS / "icon" / "linglong-clipboard.icns",
        ASSETS / "icon" / "png" / "linglong-clipboard-256.png",
    ]
    if all(path.is_file() for path in needed):
        return True
    print("图标缺档，先生成一次…")
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    result = subprocess.run(
        [sys.executable, str(ROOT / "packaging" / "icons.py")],
        cwd=str(ROOT), env=env, check=False,
    )
    return result.returncode == 0


def clean() -> None:
    for path in (DIST, BUILD):
        if path.exists():
            shutil.rmtree(path)
            print(f"已清理 {path.relative_to(ROOT)}")


def _prepare_dist() -> None:
    """清掉 dist 里同名的旧产物。

    `--clean` 只清 PyInstaller 的构建缓存，不动 dist/。
    于是「先打单文件、再打目录模式」时，dist 里已有一个同名**文件**，
    COLLECT 要建同名**目录**，直接报
    `ValueError: Resource '...' is not a valid file!`。
    """
    if not DIST.exists():
        return
    for entry in DIST.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def build(onedir: bool = True, dist: "Path | None" = None) -> int:
    global DIST
    # 不能把 `--onefile/--onedir` 和 .spec 一起传：PyInstaller 会拒绝
    # （「makespec options not valid when a .spec file is given」），
    # 发版第一步就会卡在这里。模式改由环境变量传给 spec。
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC),
        "--noconfirm",
        "--clean",
    ]
    env = dict(os.environ)
    env["LINGLONG_ONEDIR"] = "1" if onedir else "0"
    if dist is not None:
        # 换输出目录：CI 里 /workspace 是任务间共享的，各平台产物挤在同一个
        # dist/ 里会互相踩（_prepare_dist 会清空整个目录）。
        # PyInstaller 也要一起改，否则它照样写回默认的 dist/。
        DIST = dist
        command += ["--distpath", str(dist)]
    _prepare_dist()
    print(f"在 {platform_label()} 上打包：{' '.join(command[2:])}"
          f"（{'目录' if onedir else '单文件'}模式）")
    result = subprocess.run(command, cwd=str(ROOT), check=False, env=env)
    if result.returncode != 0:
        print("打包失败", file=sys.stderr)
        return result.returncode

    print(f"\n完成，产物位于 {DIST.relative_to(ROOT)}/")
    for entry in sorted(DIST.iterdir()):
        if entry.is_file():
            print(f"  {entry.name}  {entry.stat().st_size / 1024 / 1024:.1f} MB")
        else:
            print(f"  {entry.name}/")
    return 0


# ---- 发行包 ----

def staged_dir() -> Path:
    """目录模式产物所在的目录。"""
    return DIST / APP_NAME


def package_archive(onedir: bool):
    """便携版压缩包：解压即用，不需要安装。

    安装包之外仍然保留它，是因为总有人要在受限环境里跑：
    U 盘、别人的机器、没有管理员权限的电脑。安装包解决不了那些场景。
    """
    ver = version()
    if sys.platform.startswith("win"):
        source = DIST / f"{APP_NAME}.exe"
        if not source.is_file():
            return None
        target = DIST / f"{RELEASE_BASENAME}-windows.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(source, source.name)
        return target

    if sys.platform == "darwin":
        app = next(DIST.glob("*.app"), None)
        if app is None:
            return None
        target = DIST / f"{RELEASE_BASENAME}-macos.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in app.rglob("*"):
                bundle.write(path, path.relative_to(DIST))
        return target

    target = DIST / f"{RELEASE_BASENAME}-linux.tar.gz"
    folder = DIST / RELEASE_BASENAME
    source = folder if (onedir and folder.is_dir()) else DIST / APP_NAME
    if not source.exists():
        return None
    with tarfile.open(target, "w:gz") as bundle:
        bundle.add(source, arcname=RELEASE_BASENAME)
    return target


def build_installer() -> "Path | None":
    """按平台产出安装包。缺工具链时明确说清楚，不静默跳过。"""
    if sys.platform == "darwin":
        print("macOS 的 .dmg / .pkg 需要 macOS 才能做，跳过安装包")
        return None

    if sys.platform.startswith("win"):
        return _windows_installer()
    return _linux_installer()


def _windows_installer():
    """NSIS 安装向导：写开始菜单、桌面快捷方式、添加/删除程序、卸载器。

    为什么要打安装包而不是给个 zip：
    zip 解压出来的 exe 是「裸奔」的 —— 没有开始菜单入口、没有卸载记录、
    没有图标缓存，用户下次想找它只能翻文件夹。

    实现放在 `packaging/windows/make_installer.py`：Wine 交叉构建那条路径
    也要用同一份逻辑。两处各写一遍迟早起偏。
    """
    if shutil.which("makensis") is None:
        print("未找到 makensis（NSIS），Windows 安装包未生成。")
        print("  补装：apt-get install nsis   /   choco install nsis")
        return None

    source_dir = DIST / APP_NAME
    if not source_dir.is_dir():
        print(f"找不到目录模式产物：{source_dir}，无法打安装包")
        return None

    result = subprocess.run(
        [sys.executable, str(ROOT / "packaging" / "windows" / "make_installer.py"),
         str(source_dir), str(DIST)],
        cwd=str(ROOT), check=False,
    )
    if result.returncode != 0:
        return None
    candidates = sorted(DIST.glob("*-setup.exe"))
    return candidates[0] if candidates else None


def _linux_installer():
    """打 .deb：装到 /opt、写 .desktop 与图标、注册命令行入口。

    不用 AppImage：AppImage 不装进系统，图标和菜单项都得靠运行时挂载，
    用户「装完了但找不到」的体验和 zip 一样。
    """
    script = ROOT / "packaging" / "linux" / "build_deb.py"
    if not script.is_file():
        print(f"缺少 .deb 构建脚本：{script}")
        return None
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(ROOT), check=False,
    )
    if result.returncode != 0:
        print(".deb 打包失败", file=sys.stderr)
        return None
    candidates = sorted(DIST.glob("*.deb"))
    return candidates[0] if candidates else None


def main() -> int:
    parser = argparse.ArgumentParser(description="玲珑剪切板打包脚本")
    parser.add_argument(
        "--onefile", action="store_true",
        help="打单文件版（便携，启动慢；默认是目录模式）",
    )
    parser.add_argument(
        "--clean", action="store_true", help="打包前清理中间产物",
    )
    parser.add_argument(
        "--no-installer", action="store_true", help="只出便携版压缩包，不做安装包",
    )
    parser.add_argument(
        "--require-native",
        action="store_true",
        help="原生依赖缺失时直接失败（发布流水线用，避免产出功能缺失的包）",
    )
    parser.add_argument(
        "--dist", metavar="目录",
        help="产物输出目录（默认 dist/）。多平台产物要并存时用它隔开",
    )
    args = parser.parse_args()

    print(f"平台：{platform_label()} ({platform.machine()})")
    print(f"Python：{platform.python_version()}")
    print(f"版本：{version()}")

    if not check_dependencies(strict=args.require_native):
        return 1
    if not ensure_icons():
        print("图标生成失败，打包中止", file=sys.stderr)
        return 1

    if args.clean:
        clean()

    dist = Path(args.dist).resolve() if args.dist else None
    onedir = not args.onefile
    code = build(onedir=onedir, dist=dist)
    if code != 0:
        return code

    archive = package_archive(onedir)
    if archive is not None:
        print(f"\n便携版：{archive.relative_to(ROOT)}"
              f"（{archive.stat().st_size / 1024 / 1024:.1f} MB）")

    if not args.no_installer:
        installer = build_installer()
        if installer is not None:
            print(f"安装包：{installer.relative_to(ROOT)}"
                  f"（{installer.stat().st_size / 1024 / 1024:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
