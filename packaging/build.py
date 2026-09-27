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
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "linglong-clipboard.spec"
DIST = ROOT / "dist"
BUILD = ROOT / "build"


def platform_label() -> str:
    if sys.platform.startswith("win"):
        return "Windows"
    if sys.platform == "darwin":
        return "macOS"
    return "Linux"


def check_dependencies() -> bool:
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
    return True


def clean() -> None:
    for path in (DIST, BUILD):
        if path.exists():
            shutil.rmtree(path)
            print(f"已清理 {path.relative_to(ROOT)}")


def build(onedir: bool = False) -> int:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC),
        "--noconfirm",
        "--clean",
        "--onedir" if onedir else "--onefile",
    ]
    print(f"在 {platform_label()} 上打包：{' '.join(command[2:])}")
    result = subprocess.run(command, cwd=str(ROOT), check=False)
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


def main() -> int:
    parser = argparse.ArgumentParser(description="玲珑剪切板打包脚本")
    parser.add_argument("--onedir", action="store_true", help="目录模式（启动更快）")
    parser.add_argument("--clean", action="store_true", help="打包前清理中间产物")
    args = parser.parse_args()

    print(f"平台：{platform_label()} ({platform.machine()})")
    print(f"Python：{platform.python_version()}")

    if not check_dependencies():
        return 1

    if args.clean:
        clean()

    return build(onedir=args.onedir)


if __name__ == "__main__":
    raise SystemExit(main())
