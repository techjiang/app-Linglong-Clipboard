#!/usr/bin/env python3
"""从矢量母版生成各平台需要的图标文件。

为什么留一份 SVG 母版而不是直接放几张 PNG：图标要在 16px（窗口标题栏）
到 1024px（macOS 应用图标）之间都清楚，手搓多份位图迟早会忘改其中一份。
这里只维护 `assets/icon/linglong-clipboard.svg`，其余全部生成。

产物：
    assets/icon/png/linglong-clipboard-<N>.png   多档 PNG（Linux 桌面图标、文档）
    assets/icon/linglong-clipboard.ico           Windows（PyInstaller / 快捷方式）
    assets/icon/linglong-clipboard.icns          macOS（.app 的图标）

只用 PySide6（已是运行时依赖），不引入 cairosvg / ImageMagick 这类
额外构建期依赖 —— 换台机器 clone 下来就能重新生成。

用法：
    python packaging/icons.py            # 生成
    python packaging/icons.py --check    # 只校验产物是否与母版一致（CI 用）
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "assets" / "icon" / "linglong-clipboard.svg"
#: 小尺寸专用母版。16~32px 下放大镜和底板会糊成一团灰点，
#: 托盘里看到的是一块斑而不是图标 —— 所以小尺寸换一套简化图形。
SVG_SMALL = ROOT / "assets" / "icon" / "linglong-clipboard-small.svg"
OUT = ROOT / "assets" / "icon"

#: 到这个尺寸及以下用简化母版
SMALL_THRESHOLD = 32

#: 需要产出的位图尺寸。16/24/32 给窗口标题栏与托盘，其余给桌面环境与商店图。
PNG_SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)

#: ICO 里装的档位。Windows 会按 DPI 自己挑最合适的一张。
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

#: ICNS 的类型码 -> 边长。这些码是苹果的固定标记，写错系统就不认这个图标。
ICNS_TYPES = {
    b"icp4": 16,
    b"icp5": 32,
    b"icp6": 64,
    b"ic07": 128,
    b"ic08": 256,
    b"ic09": 512,
    b"ic10": 1024,  # 512@2x
    b"ic11": 32,    # 16@2x
    b"ic12": 64,    # 32@2x
    b"ic13": 256,   # 128@2x
    b"ic14": 512,   # 256@2x
}


def render(size: int) -> bytes:
    """把 SVG 渲染成 size×size 的 PNG 字节。

    小尺寸走简化母版：细节在 16px 里不是「更小」，而是「更脏」。
    """
    from PySide6.QtCore import QBuffer, QByteArray, QSize
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    source = SVG_SMALL if (size <= SMALL_THRESHOLD and SVG_SMALL.is_file()) else SVG
    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise SystemExit(f"矢量母版无法解析：{source}")

    image = QImage(QSize(size, size), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter)
    painter.end()

    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise SystemExit(f"PNG 编码失败：{size}px")
    return bytes(payload.data())


def write_ico(frames: dict[int, bytes], target: Path) -> None:
    """写多尺寸 ICO。

    Vista 起 ICO 的每一帧可以直接放 PNG 数据（不必转 BMP+DIB），
    Windows 10/11 与 PyInstaller 都认。手写而非依赖 Pillow：
    打包链路里多一个依赖，就多一处「他的机器上装不上」。
    """
    entries, blobs, offset = [], [], 6 + 16 * len(frames)
    for size in sorted(frames):
        data = frames[size]
        # 256 在单字节字段里写 0，这是 ICO 格式的约定
        entries.append(
            struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        )
        blobs.append(data)
        offset += len(data)
    target.write_bytes(
        struct.pack("<HHH", 0, 1, len(frames)) + b"".join(entries) + b"".join(blobs)
    )


def write_icns(frames: dict[int, bytes], target: Path) -> None:
    """写 ICNS：'icns' + 总长度 + 若干 (类型码, 长度, PNG)。"""
    body = b""
    for code, size in ICNS_TYPES.items():
        data = frames[size]
        body += code + struct.pack(">I", len(data) + 8) + data
    target.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


def main(argv: list[str]) -> int:
    if not SVG.is_file():
        print(f"缺少矢量母版：{SVG}", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    png_dir = OUT / "png"
    png_dir.mkdir(exist_ok=True)

    needed = sorted(set(PNG_SIZES) | set(ICO_SIZES) | set(ICNS_TYPES.values()))
    frames = {size: render(size) for size in needed}

    for size in PNG_SIZES:
        (png_dir / f"linglong-clipboard-{size}.png").write_bytes(frames[size])
    write_ico({s: frames[s] for s in ICO_SIZES}, OUT / "linglong-clipboard.ico")
    write_icns(frames, OUT / "linglong-clipboard.icns")

    total = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"已生成图标：{len(PNG_SIZES)} 档 PNG + ICO + ICNS，共 {total / 1024:.0f} KB")
    for path in sorted(OUT.rglob("*")):
        if path.is_file():
            print(f"  {path.relative_to(ROOT)}  {path.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
