#!/usr/bin/env python3
"""源码目录直接运行时的入口（免安装）。

打包后的可执行文件走 `packaging/` 里的配置，
两者都指向 `linglong_clip.__main__:main`。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from linglong_clip.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
