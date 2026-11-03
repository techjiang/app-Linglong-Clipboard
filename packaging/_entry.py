"""打包入口。

单独一个文件的原因：PyInstaller 把入口当顶层脚本执行，
包内的相对导入（`from . import __version__`）会失去包上下文，
打包出的可执行文件一启动就报
`attempted relative import with no known parent package`。
这里用绝对导入把包当库调起来，开发和打包的入口就一致了。
"""

from __future__ import annotations

import sys

from linglong_clip.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
