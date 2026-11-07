#!/usr/bin/env python3
"""把目录模式产物打成 Windows 便携版 zip。

解压后是 `linglong-clipboard/` 一个目录，不把上百个 dll 撒到用户当前目录里。
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("用法：zip_portable.py <产物目录> <输出 zip>", file=sys.stderr)
        return 2
    source, target = Path(argv[1]), Path(argv[2])
    if not source.is_dir():
        print(f"找不到产物目录：{source}", file=sys.stderr)
        return 1

    prefix = "linglong-clipboard"
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                bundle.write(path, f"{prefix}/{path.relative_to(source).as_posix()}")
                count += 1
    print(f"  {target.name}：{count} 个文件，{target.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
