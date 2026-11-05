#!/usr/bin/env python3
"""在伪终端里跑一条命令，并把输出原样转回来。

为什么需要它：Wine 8 下只要标准流不是终端（CI 把输出写进日志就是），
Windows 版 Python 会在 `init_sys_streams` 阶段直接崩，报
`OSError: [WinError 6] Invalid handle`，连一行自己的日志都留不下。
用 `script` 包裹也能骗过去，但它会带出交互式回显、阻塞在管道上；
这里只借一个 pty，退出码和输出都干净地带回来。

用法：
    wine_run.py [--] 命令 参数...
"""

from __future__ import annotations

import os
import pty
import re
import select
import sys

# 伪终端会带回一堆光标控制序列（隐藏/显示光标、清行），日志里全是噪音
_ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def run(command: list[str], timeout: float | None = None) -> tuple[int, bytes]:
    pid, fd = pty.fork()
    if pid == 0:
        # 子进程：直接 exec，pty.fork 已经把标准流接过去了
        try:
            os.execvp(command[0], command)
        except OSError as exc:
            os.write(2, f"{command[0]}: {exc}\n".encode())
            os._exit(127)

    chunks: list[bytes] = []
    while True:
        try:
            readable, _, _ = select.select([fd], [], [], timeout)
        except OSError:
            break
        if not readable:
            break
        try:
            data = os.read(fd, 65536)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)

    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), b"".join(chunks)


def main(argv: list[str]) -> int:
    command = argv[1:] if argv[:1] == ["--"] else argv
    if not command:
        print(__doc__, file=sys.stderr)
        return 2
    code, output = run(command)
    # 原样吐回（清掉控制序列），调用方重定向到哪里都行
    sys.stdout.buffer.write(_ANSI.sub(b"", output).replace(b"\r\n", b"\n"))
    sys.stdout.buffer.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
