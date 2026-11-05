#!/usr/bin/env python3
"""一键打包脚本：在各自平台上运行，产出原生可执行文件。

用法：
    python packaging/build.py            # 当前平台，单文件
    python packaging/build.py --onedir   # 目录模式，启动更快
    python packaging/build.py --clean    # 先清理再打包

注意：PyInstaller 不支持交叉编译。要出 Windows 的 .exe 就得在 Windows 上跑，
macOS 的 .app 只能在 macOS 上打。CI 里用矩阵构建（见 .github/workflows/build.yml）。
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

#: 产物基名，与 spec 里的 name 保持一致
APP_NAME = "玲珑剪切板" if sys.platform.startswith("win") else "linglong-clipboard"


def platform_label() -> str:
    if sys.platform.startswith("win"):
        return "Windows"
    if sys.platform == "darwin":
        return "macOS"
    return "Linux"


#: 各平台上「装了更好、没装则相应能力降级」的可选依赖。
#: 缺了这些 PyInstaller 照样能打包成功，但打出来的包里会**少一整块功能**，
#: 而使用者在界面上只会看到「不可用」，不知道是包的问题还是自己环境的问题。
OPTIONAL_DEPS: dict[str, tuple[str, str]] = {
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

#: 缺了这些但**必须**在打包时报告出来的可选依赖
#: （`cryptography` / `pyyaml` 走的是运行时优雅降级，界面上会明确说明，不必拦）


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


def build(onedir: bool = False, dist: "Path | None" = None) -> int:
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

    archive = package_archive(onedir)
    print(f"\n完成，产物位于 {DIST.relative_to(ROOT)}/")
    for entry in sorted(DIST.iterdir()):
        if entry.is_file():
            print(f"  {entry.name}  {entry.stat().st_size / 1024 / 1024:.1f} MB")
        else:
            print(f"  {entry.name}/")
    if archive is not None:
        print(f"\n发行包：{archive.relative_to(ROOT)}")
    return 0


def package_archive(onedir: bool):
    """把产物打成可直接分发的归档，没有产物时返回 None。

    裸可执行文件有两个麻烦：浏览器/网盘会拦，Linux 下经邮件或 HTTP
    传输后容易丢可执行位。放进归档一并解决。
    """
    if sys.platform.startswith("win"):
        source = DIST / f"{APP_NAME}.exe"
        if not source.is_file():
            return None
        target = DIST / f"{APP_NAME}-windows.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(source, source.name)
        return target

    if sys.platform == "darwin":
        app = next(DIST.glob("*.app"), None)
        if app is None:
            return None
        target = DIST / f"{APP_NAME}-macos.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in app.rglob("*"):
                bundle.write(path, path.relative_to(DIST))
        return target

    target = DIST / f"{APP_NAME}-linux.tar.gz"
    folder = DIST / APP_NAME
    source = folder if (onedir and folder.is_dir()) else DIST / APP_NAME
    if not source.exists():
        return None
    with tarfile.open(target, "w:gz") as bundle:
        bundle.add(source, arcname=APP_NAME)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="玲珑剪切板打包脚本")
    parser.add_argument("--onedir", action="store_true", help="目录模式（启动更快）")
    parser.add_argument("--clean", action="store_true", help="打包前清理中间产物")
    parser.add_argument(
        "--require-native",
        action="store_true",
        help="原生依赖缺失时直接失败（发布流水线用，避免产出功能缺失的包）",
    )
    parser.add_argument(
        "--dist",
        metavar="目录",
        help="产物输出目录（默认 dist/）。多平台产物要并存时用它隔开",
    )
    args = parser.parse_args()

    print(f"平台：{platform_label()} ({platform.machine()})")
    print(f"Python：{platform.python_version()}")

    if not check_dependencies(strict=args.require_native):
        return 1

    if args.clean:
        clean()

    dist = Path(args.dist).resolve() if args.dist else None
    return build(onedir=args.onedir, dist=dist)


if __name__ == "__main__":
    raise SystemExit(main())
