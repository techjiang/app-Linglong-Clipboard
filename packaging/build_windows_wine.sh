#!/usr/bin/env bash
# 在 Linux 上用 Wine 交叉打包 Windows 版 .exe。
#
# 为什么不是「在 GitHub Actions 的 windows runner 上打」：
# 那样 CNB 的版本页就只能挂 Linux 包，Windows 用户要跳去另一个站点下载。
# PyInstaller 确实不支持交叉编译，但它只需要「一个能跑的 Windows Python」——
# Wine 提供了这个环境，于是同一台 Linux 构建机上就能产出原生的 .exe。
#
# 三条容易踩空的坑，都在下面显式处理了：
#   1. PySide6 6.11+ 的 Qt6Core.dll 依赖 icuuc.dll，而 wheel 里没带，导入必崩。
#      固定在 6.9.3——那是最后一版不引 ICU 的。
#   2. Wine 的 ANSI 代码页决定「非 ASCII 文件名能不能落盘」。不设 UTF-8 时
#      PyInstaller 写 `玲珑剪切板.pkg` 直接 FileNotFoundError。
#   3. 控制台默认 cp1252，任何中文输出都会 UnicodeEncodeError。产品侧已修
#      （见 __main__._make_streams_printable），这里再兜一层环境变量。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${LINGLONG_WINPY_PREFIX:-/opt/winpy}"
PY_VERSION="${LINGLONG_WINPY_VERSION:-3.11.9}"
PYSIDE_VERSION="${LINGLONG_WINPYSIDE_VERSION:-6.9.3}"
# cryptography 的 Rust 扩展在 Wine 下加载不了（_rust 模块缺依赖），
# 只有纯 C 实现的 3.4.x 能跑。它只用于可选的同步加密，功能等价。
CRYPTO_VERSION="${LINGLONG_WINCRYPTO_VERSION:-3.4.8}"
EMBED_URL="https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-embed-amd64.zip"

export WINEDEBUG="${WINEDEBUG:--all}"
export LANG="${LANG:-C.UTF-8}"
export LC_ALL="${LC_ALL:-C.UTF-8}"
export PYTHONIOENCODING=utf-8

log() { echo "[wine-build] $*"; }

# 统一入口。Wine 8 有个坑：只要标准流不是终端（CI 把输出写进日志就中招），
# Windows 版 Python 会在 init_sys_streams 阶段直接崩：
#     OSError: [WinError 6] Invalid handle
# tools/wine_run.py 借一个 pty 把它骗过去，退出码和输出都干净地转回来。
wine_py() { python3 "$ROOT/tools/wine_run.py" -- wine "$@"; }

if ! command -v wine >/dev/null 2>&1; then
  log "缺少 wine，无法交叉打包 Windows 产物"
  exit 1
fi

# ---- 准备 Windows 侧的 Python 环境（有缓存就复用） ----
if [ ! -x "$PREFIX/python.exe" ]; then
  log "准备 Windows Python $PY_VERSION 到 $PREFIX"
  mkdir -p "$PREFIX"
  curl -fsSL -o "$PREFIX/py.zip" "$EMBED_URL"
  python3 - "$PREFIX/py.zip" "$PREFIX" <<'PY'
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PY
  rm -f "$PREFIX/py.zip"
  # embed 版默认不启用 site，pip 装到 Lib/site-packages 的包会看不见。
  # 文件名形如 python311._pth（主次版本号，无点）。
  _PTH="$(ls "$PREFIX"/python*._pth 2>/dev/null | head -1)"
  [ -n "$_PTH" ] || { log "找不到 ._pth 文件"; exit 1; }
  sed -i 's/^#import site/import site/' "$_PTH"
  curl -fsSL -o "$PREFIX/get-pip.py" https://bootstrap.pypa.io/get-pip.py
  wine_py "$PREFIX/python.exe" "$PREFIX/get-pip.py" --no-warn-script-location
fi

PY="$PREFIX/python.exe"
wine_py "$PY" -c "import sys; print('windows python', sys.version.split()[0], sys.platform)"

# ---- 依赖：版本必须是 6.9.3，理由见文件头 ----
# 探测写成 -c 而不是 heredoc：pty 里子进程的 stdin 是终端，
# 再往里塞 heredoc 会让读 stdin 的进程一直等下去。
if ! wine_py "$PY" -c "import sys; from PySide6 import __version__; sys.exit(0 if __version__ == sys.argv[1] else 1)" "$PYSIDE_VERSION" >/dev/null 2>&1
then
  log "安装 PySide6==$PYSIDE_VERSION / PyInstaller / 可选项"
  wine_py "$PY" -m pip install --no-warn-script-location \
    "PySide6==$PYSIDE_VERSION" "pyinstaller>=6" "cryptography==$CRYPTO_VERSION" pyyaml
  # 6.12 的 pdf/webengine 子包会与 6.9.3 冲突，且产品完全用不到
  rm -rf "$PREFIX/Lib/site-packages/pyside6_pdf"* \
         "$PREFIX/Lib/site-packages/pyside6_webengine"* 2>/dev/null || true
fi

# ---- 打包 ----
cd "$ROOT"
# 产物先落在独立目录，最后再挑出 Windows 那两个搬进 dist/。
# 共用 dist/ 会被 build.py 的 _prepare_dist 清空——CI 的 /workspace 是任务间
# 共享的，上一个 stage 刚打好的 Linux 包就躺在那儿。
WIN_DIST="$ROOT/dist-windows"
rm -rf "$WIN_DIST"
log "开始打包 -> ${WIN_DIST#$ROOT/}"
wine_py "$PY" packaging/build.py --require-native --dist "$WIN_DIST"
mkdir -p dist

EXE="$WIN_DIST/玲珑剪切板.exe"
ZIP="$WIN_DIST/玲珑剪切板-windows.zip"
[ -f "$EXE" ] || { log "没找到 $EXE"; exit 1; }
[ -f "$ZIP" ] || { log "没找到 $ZIP"; exit 1; }

# ---- 冒烟：产物必须真能跑 ----
# 打包成功不代表能启动：入口写错、控制台编码不兼容，都是「构建成功、双击闪退」。
log "冒烟测试 .exe"
cat > "$ROOT/packaging/_wine_smoke.py" <<'PY'
"""在 Wine 里跑一遍刚打出来的 .exe，确认它不只是个几十 MB 的砖。"""
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
exe = os.environ["LINGLONG_SMOKE_EXE"]
failed = False
for arg in ("--version", "--stats"):
    result = subprocess.run([exe, arg], capture_output=True)
    text = (result.stdout + result.stderr).decode("utf-8", "replace")
    print(f"  {arg} -> rc={result.returncode}")
    if result.returncode == 0:
        print("   ", text.strip().splitlines()[0] if text.strip() else "(no output)")
    else:
        failed = True
        print(text[:800])
raise SystemExit(1 if failed else 0)
PY
# Wine 只认 Windows 路径写法：把 POSIX 路径换成 Z: 盘的 Windows 路径
to_win() { printf 'Z:%s' "$(printf '%s' "$1" | tr '/' '\\')"; }
LINGLONG_SMOKE_EXE="$(to_win "$EXE")" \
LINGLONG_CLIPBOARD_HOME='C:\smoke' \
  wine_py "$PY" "$(to_win "$ROOT/packaging/_wine_smoke.py")"
rm -f "$ROOT/packaging/_wine_smoke.py"

# 挑出 Windows 产物，统一成 ASCII 名称，和 Linux 包 / GitHub Release 对齐
cp -f "$ZIP" dist/linglong-clipboard-windows.zip
cp -f "$EXE" dist/linglong-clipboard.exe
ls -la dist/
log "完成：dist/linglong-clipboard-windows.zip"
