#!/usr/bin/env python3
"""在**真实 X11 显示**里跑一遍装好的程序并截图。

与 `packaging/screenshots.py` 的分工：
- 那个用 offscreen 渲染，不需要显示器，CI 里能跑，产出文档配图；
- 这个要真的显示服务器，跑的是**装好的可执行文件**，截的是真实窗口。

为什么两个都要：offscreen 渲染证明「界面代码能跑」，但它不经过窗口管理器、
不经过系统托盘、不经过 WM_CLASS 与图标协商。安装版出问题时往往正落在
那三者里（图标不显示、菜单项不出现、托盘空白），只有真窗口才看得出来。

用法（需要 Xvfb 或真实桌面）：
    Xvfb :99 -screen 0 1440x900x24 &
    DISPLAY=:99 python packaging/screenshots_live.py

依赖：`import`（ImageMagick）、`xclip`（或 xsel）。缺了就明确跳过，不假装成功。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
BINARY = os.environ.get("LINGLONG_BINARY", "/opt/linglong-clipboard/linglong-clipboard")

#: 真实复制的内容。刻意覆盖几种会被分类成不同 kind 的类型 ——
#: 截图里能同时看到「文本 / 链接 / 代码 / 颜色」，才说明分类没坏。
SAMPLES = [
    "会议纪要：周四评审设计稿，记得带上交互说明",
    "https://cnb.cool/asoe/TechSauce?utm_source=chat",
    'def handler(event):\n    return event["key"]',
    "#4c8dff",
    "docker run -p 8080:80 nginx:alpine",
]


def need(*tools: str) -> bool:
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        print(f"缺少工具：{', '.join(missing)}，跳过真实截图", file=sys.stderr)
        return False
    return True


def wait_for_window(timeout: float = 30.0) -> str:
    """等应用真的把窗口映射出来，返回窗口 id。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            ["xwininfo", "-root", "-children"], capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            # 窗口标题是中文，xwininfo 在某些 locale 下转不成可读字符，
            # 所以按 WM_CLASS 里的可执行名认，不按标题。
            if "linglong-clipboard" in line and "1x1" not in line:
                return line.split()[0]
        time.sleep(0.5)
    raise SystemExit("等不到应用窗口，可能没启动成功（看运行日志）")


def main() -> int:
    if not need("xwininfo", "import", "xclip"):
        return 1
    if not Path(BINARY).is_file():
        print(f"找不到可执行文件：{BINARY}", file=sys.stderr)
        print("先安装：dpkg -i dist/*.deb  或 set LINGLONG_BINARY=<路径>", file=sys.stderr)
        return 1

    home = tempfile.mkdtemp(prefix="linglong-live-")
    env = dict(os.environ, LINGLONG_CLIPBOARD_HOME=home)
    OUT.mkdir(parents=True, exist_ok=True)

    print(f"启动 {BINARY}（数据目录 {home}）")
    # stdin 必须断开：GUI 进程继承终端 stdin 后，某些桌面环境下会一直阻塞在
    # 读标准输入上（表现为「脚本挂住、没有任何输出」）。
    process = subprocess.Popen(
        [BINARY], env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        window = wait_for_window()
        print(f"窗口已出现：{window}")

        # 空状态先截一张：这是新装用户看到的第一屏
        _capture(OUT / "empty-first-run.png")

        for sample in SAMPLES:
            # xclip 会把自己变成常驻进程去「持有」选区（否则选区内容会随进程
            # 退出消失）。它继承 stdout 的话，调用方那边的管道就一直不会 EOF ——
            # 表现是脚本看着卡住，其实早就跑完了。所以这里把它的输出断开。
            subprocess.run(
                ["xclip", "-selection", "clipboard"], input=sample.encode(),
                check=False, timeout=15,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(1.2)  # 采集是 300ms 轮询，留出余量
        time.sleep(1.5)
        _capture(OUT / "real-linux-running.png")

        # 图标是安装版最容易掉的一环，单独把 WM_HINTS 里的档位记下来
        icons = subprocess.run(
            ["xprop", "-id", window, "_NET_WM_ICON"], capture_output=True, text=True
        ).stdout
        sizes = [
            line.split("(")[1].split(")")[0]
            for line in icons.splitlines() if "Icon (" in line
        ]
        print(f"_NET_WM_ICON 档位：{sizes or '空（图标没设上）'}")
        if not sizes:
            print("警告：窗口没有设置图标，任务栏会显示默认方块", file=sys.stderr)
            return 1
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    print("完成。")
    return 0


def _capture(target: Path) -> None:
    subprocess.run(["import", "-window", "root", str(target)], check=False)
    print(f"  ✓ {target.relative_to(ROOT)}")


if __name__ == "__main__":
    raise SystemExit(main())
