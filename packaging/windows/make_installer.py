#!/usr/bin/env python3
"""调用 NSIS 生成 Windows 安装包。

在 Linux 上跑：NSIS 有原生 Linux 版本（apt-get install nsis），
编出来的 setup.exe 与在 Windows 上编的没有区别。

用法：
    make_installer.py <目录模式产物> <输出目录>
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def version() -> str:
    text = (ROOT / "src" / "linglong_clip" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"\'')
    raise SystemExit("读不到版本号")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    source = Path(argv[1]).resolve()
    out_dir = Path(argv[2]).resolve()

    makensis = shutil.which("makensis")
    if makensis is None:
        print("未找到 makensis（NSIS）。apt-get install nsis", file=sys.stderr)
        return 1
    if not source.is_dir():
        print(f"找不到目录模式产物：{source}", file=sys.stderr)
        return 1

    ver = version()
    target = out_dir / f"linglong-clipboard-{ver}-windows-setup.exe"
    icon = ROOT / "assets" / "icon" / "linglong-clipboard.ico"
    if not icon.is_file():
        print(f"缺少图标：{icon}（先跑 python packaging/icons.py）", file=sys.stderr)
        return 1

    result = subprocess.run(
        [
            makensis,
            # 选项顺序有讲究：脚本路径必须排在最后，`/D` 必须紧跟脚本路径
            # **之前**但不能排在第一个参数位——`makensis /DX script.nsi` 会被
            # 当成「脚本叫 /DX」。这是 NSIS 命令行的固定格式，写错时报
            # `Can't open script "/DAPP_VERSION=1.0.1"`，看起来像脚本丢了。
            "-NOCD",
            f"-DAPP_VERSION={ver}",
            f"-DSOURCE_DIR={source}",
            f"-DOUTPUT_FILE={target}",
            f"-DICON_FILE={icon}",
            str(ROOT / "packaging" / "windows" / "installer.nsi"),
        ],
        cwd=str(ROOT),
        check=False,
    )
    if result.returncode != 0 or not target.is_file():
        print("NSIS 编译失败", file=sys.stderr)
        return result.returncode or 1
    print(f"  {target.name}：{target.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
