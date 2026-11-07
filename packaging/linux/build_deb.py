#!/usr/bin/env python3
"""把目录模式产物打成 Debian 安装包（.deb）。

为什么是 .deb 而不是 AppImage / tar.gz：
- tar.gz 解压出来是「裸文件」，没有桌面图标、没有菜单项、命令行里也敲不到，
  用户装完只能在文件夹里找到它；
- AppImage 不往系统里装任何东西，图标要靠运行时挂载，同样找不到。

.deb 把这些一次做全：程序进 `/opt`，图标进 `/usr/share/icons`，
菜单项进 `/usr/share/applications`，命令行入口进 `/usr/bin`，
卸载时 `apt remove` 会一次清干净。

依赖 `dpkg-deb`（Debian/Ubuntu 自带，Linux 构建机不必额外装什么）。
不用 `fakeroot`：整棵树在构建时就已经按目标 uid/gid 写好，
实际以 root 构建时直接 dpkg-deb 即可。

用法：
    python3 packaging/linux/build_deb.py            # 用 dist/linglong-clipboard
    python3 packaging/linux/build_deb.py --arch arm64
"""

from __future__ import annotations

import argparse
import gzip
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "dist"
ASSETS = ROOT / "assets"

PACKAGE = "linglong-clipboard"
APP_DIR_NAME = "linglong-clipboard"
#: 应用显示名。写成 ASCII 的 .desktop 文件名，中文名放在 Name 字段里 ——
#: 文件名带中文在部分桌面环境里会导致菜单项不出现。
DESKTOP_FILE = "linglong-clipboard.desktop"


def version() -> str:
    text = (ROOT / "src" / "linglong_clip" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"\'')
    raise SystemExit("读不到版本号")


def deb_arch() -> str:
    # 用 platform.machine() 而不是 os.uname()：后者在 Windows 上不存在，
    # 而这份脚本会被 build.py 在三个平台上都导入（只有 Linux 真正走到）。
    import platform as _platform

    machine = _platform.machine()
    return {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "armhf"}.get(machine, machine)


DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=Linglong Clipboard
Name[zh_CN]=玲珑剪切板
GenericName=Clipboard Manager
GenericName[zh_CN]=剪切板管理器
Comment=Local-first smart clipboard with semantic search
Comment[zh_CN]=本地优先的智能剪切板：语义检索、就地转换、跨设备同步
Exec={exec_path} %U
TryExec={exec_path}
Icon={icon_name}
Terminal=false
Categories=Utility;Accessibility;
Keywords=clipboard;paste;history;clipboard-manager;
Keywords[zh_CN]=剪贴板;粘贴;历史;
StartupWMClass=玲珑剪切板
StartupNotify=false
X-GNOME-UsesNotifications=true
"""


def _write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


#: 目录条目。tar 里只有文件也能解开，但 dpkg 依赖目录条目来建目录树 ——
#: 缺了它 `dpkg -i` 会报
#: `unable to create '.../libQt6Core.so.6.dpkg-new': No such file or directory`。
#: 这是「能打出包」和「包能装上」之间的差别，只有真装一次才会发现。
def _dir_entries(stage: Path, base: Path) -> list[tuple[str, None, int]]:
    entries = []
    for path in sorted(stage.rglob("*")):
        if not path.is_dir() or path == base:
            continue
        if base != stage and base not in path.parents:
            continue
        entries.append((f"./{path.relative_to(stage).as_posix()}", None, 0o755))
    return entries


def _tar_gz(entries: list[tuple[str, bytes, int]]) -> bytes:
    """手工拼 tar.gz。

    dpkg-deb 要的 control.tar.gz / data.tar.gz 只有一种布局：路径必须相对，
    带 ./ 前缀，且顺序不影响。用 tarfile 而非系统 tar，免得打包机上的
    tar 版本行为差异（GNU tar 会给每个条目加 mtime 与 owner）影响结果——
    这里把 owner 固定成 root:root，安装后才不会出现「文件属于构建用户」。
    """
    buffer = io.BytesIO()
    # mtime 固定：同样的输入产出同样的包，便于核对哈希
    # 必须用 GNU 格式：tarfile 默认在遇到超长路径或非 ASCII 名时会写 PAX 扩展头，
    # 而 dpkg 不认 PAX（`unsupported PAX tar header type 'x'`），包直接装不上。
    # Qt 的深层路径轻易超过 100 字节，这不是罕见情况。
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, data, mode in entries:
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return gzip.compress(buffer.getvalue(), mtime=0)


def build(arch: str, output_dir: Path) -> Path:
    ver = version()
    source = DIST / APP_DIR_NAME
    if not source.is_dir():
        raise SystemExit(
            f"找不到目录模式产物：{source}\n"
            "先跑：python3 packaging/build.py --no-installer"
        )

    stage = output_dir / f"deb-stage-{arch}"
    if stage.exists():
        shutil.rmtree(stage)

    # ---- 文件树 ----
    # /opt/linglong-clipboard：程序本体（目录模式产物整体搬进来）
    opt = stage / "opt" / APP_DIR_NAME
    shutil.copytree(source, opt)
    # 主可执行文件必须有可执行位（copytree 会保留，但压缩解压链路里可能丢）
    exe = opt / "linglong-clipboard"
    if not exe.is_file():
        raise SystemExit(f"产物里没有主程序：{exe}")
    exe.chmod(0o755)

    # 图标：多档位放进 hicolor 主题目录，桌面环境按尺寸自动挑
    icon_name = "linglong-clipboard"
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        src = ASSETS / "icon" / "png" / f"{icon_name}-{size}.png"
        if src.is_file():
            _write(
                stage / "usr" / "share" / "icons" / "hicolor"
                / f"{size}x{size}" / "apps" / f"{icon_name}.png",
                src.read_bytes(),
            )
    # 高分屏与部分旧桌面读 scalable 目录；这里放 256 版兜底
    big = ASSETS / "icon" / "png" / f"{icon_name}-256.png"
    if big.is_file():
        _write(
            stage / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps"
            / f"{icon_name}.svg",
            (ASSETS / "icon" / f"{icon_name}.svg").read_bytes(),
        )

    # 桌面菜单项
    exec_path = f"/opt/{APP_DIR_NAME}/{APP_DIR_NAME}"
    _write(
        stage / "usr" / "share" / "applications" / DESKTOP_FILE,
        DESKTOP_ENTRY.format(exec_path=exec_path, icon_name=icon_name).encode("utf-8"),
    )

    # 命令行入口：`linglong-clipboard --list` 之类在装完之后就能用
    _write(
        stage / "usr" / "bin" / APP_DIR_NAME,
        f"#!/bin/sh\nexec {exec_path} \"$@\"\n".encode("utf-8"),
        0o755,
    )

    # 文档
    for name in ("LICENSE", "README.md", "CHANGELOG.md"):
        src = ROOT / name
        if src.is_file():
            _write(stage / "usr" / "share" / "doc" / PACKAGE / name, src.read_bytes())

    # ---- control ----
    installed_size = sum(
        p.stat().st_size for p in stage.rglob("*") if p.is_file()
    ) // 1024

    control = f"""Package: {PACKAGE}
Version: {ver}
Section: utils
Priority: optional
Architecture: {arch}
Maintainer: TechSauce <official@tech.asoe.cn>
Installed-Size: {installed_size}
Depends: libc6, libegl1 | libegl-mesa0, libglib2.0-0, libfontconfig1, libdbus-1-3, libxkbcommon0 | libxkbcommon-x11-0
Recommends: libxcb-cursor0, libxcb-icccm4, libxcb-keysyms1, libxcb-shape0, tesseract-ocr
Homepage: https://cnb.cool/asoe/TechSauce/app-Linglong-Clipboard
Description: Local-first smart clipboard with semantic search
 Linglong Clipboard keeps your copy history searchable: natural-language
 queries, 33 in-place transforms, privacy filtering, and peer-to-peer sync.
 .
 Data stays in your own home directory (~/.local/share/linglong-clipboard).
 Uninstalling does not remove your history.
"""
    _write(stage / "DEBIAN" / "control", control.encode("utf-8"))

    # postinst：刷新桌面数据库与图标缓存，否则装完要注销重登才出现菜单项
    _write(
        stage / "DEBIAN" / "postinst",
        b"""#!/bin/sh
set -e
if [ -x /usr/bin/update-desktop-database ]; then
  update-desktop-database -q /usr/share/applications || true
fi
if [ -x /usr/bin/gtk-update-icon-cache ]; then
  gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
""",
        0o755,
    )
    _write(
        stage / "DEBIAN" / "postrm",
        b"""#!/bin/sh
set -e
if [ -x /usr/bin/update-desktop-database ]; then
  update-desktop-database -q /usr/share/applications || true
fi
exit 0
""",
        0o755,
    )

    # ---- 组装 ----
    control_entries, data_entries = [], []
    for path in sorted((stage / "DEBIAN").rglob("*")):
        if path.is_file():
            control_entries.append(
                (f"./{path.relative_to(stage / 'DEBIAN').as_posix()}",
                 path.read_bytes(), path.stat().st_mode & 0o777)
            )
    # 目录条目必须排在文件前面：dpkg 按 tar 顺序解包，先遇到文件再建目录就晚了
    data_entries = []
    seen_dirs = set()
    for path in sorted(stage.rglob("*")):
        if (stage / "DEBIAN") in path.parents or path.name == "DEBIAN":
            continue
        if path.is_dir():
            for parent in list(path.parents)[::-1]:
                if parent == stage or parent in seen_dirs or stage not in parent.parents and parent != stage:
                    continue
                seen_dirs.add(parent)
                data_entries.append(
                    (f"./{parent.relative_to(stage).as_posix()}", None, 0o755)
                )
            data_entries.append((f"./{path.relative_to(stage).as_posix()}", None, 0o755))
        elif path.is_file():
            data_entries.append(
                (f"./{path.relative_to(stage).as_posix()}",
                 path.read_bytes(), path.stat().st_mode & 0o777)
            )
    # 去重并保持首次出现顺序
    _seen, _unique = set(), []
    for entry in data_entries:
        if entry[0] in _seen:
            continue
        _seen.add(entry[0])
        _unique.append(entry)
    data_entries = _unique

    deb_path = output_dir / f"{PACKAGE}_{ver}_{arch}.deb"
    _assemble_deb(deb_path, _tar_gz(control_entries), _tar_gz(data_entries))
    shutil.rmtree(stage, ignore_errors=True)
    return deb_path


def _assemble_deb(target: Path, control_tar: bytes, data_tar: bytes) -> None:
    """`!<arch>\\n` + debian-binary + control.tar.gz + data.tar.gz。

    dpkg-deb 能直接读，`ar t` 也能列出来 —— 手写而不调 dpkg-deb 的 --build，
    是因为后者对临时目录的属主与权限比较挑，在容器里以 root 构建时
    会把整棵树的 owner 写成数值 uid，装到用户机器上就成了「文件属于 1000」。
    """
    def entry(name: str, data: bytes) -> bytes:
        header = (
            f"{name:<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n"
        ).encode("ascii")
        return header + data + (b"\n" if len(data) % 2 else b"")

    blob = b"!<arch>\n"
    blob += entry("debian-binary", b"2.0\n")
    blob += entry("control.tar.gz", control_tar)
    blob += entry("data.tar.gz", data_tar)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)
    target.chmod(0o644)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="构建 .deb 安装包")
    parser.add_argument("--arch", default=deb_arch(), help=f"目标架构（默认 {deb_arch()}）")
    parser.add_argument("--out", default=str(DIST), help="输出目录（默认 dist/）")
    args = parser.parse_args(argv[1:])

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    deb = build(args.arch, out)
    print(f"已生成：{deb.relative_to(ROOT)}  {deb.stat().st_size / 1024 / 1024:.1f} MB")

    # 自检：包结构不合法的话，用户 apt install 才报错，那时已经发出去了
    if shutil.which("dpkg-deb"):
        result = subprocess.run(
            ["dpkg-deb", "--info", str(deb)], capture_output=True, check=False
        )
        if result.returncode != 0:
            print(result.stderr.decode("utf-8", "replace"), file=sys.stderr)
            print("dpkg-deb 校验失败", file=sys.stderr)
            return 1
        print("dpkg-deb 校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
