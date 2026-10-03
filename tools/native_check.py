"""真机原生能力自检：手工用，不进自动测试。

用途：在没有 Windows/macOS 机器的情况下，
先把「真机自检脚本」写好，用户拷过去跑一次即可拿到完整结论。

用法：
    python tools/native_check.py            # 人类可读
    python tools/native_check.py --json     # 机器可读，便于贴进 Issue
    python tools/native_check.py --gui      # 额外验证 Qt 剪贴板读写

退出码：0=全部正常；1=有降级；2=有失败。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---- 输出辅助 ----

_ICON = {"ok": "✓", "degraded": "~", "fail": "✗", "skip": "-"}
_CAPTURED: list = []


def _emit(line: str = "") -> None:
    print(line)
    _CAPTURED.append(line)


def _section(title: str) -> None:
    _emit()
    _emit(f"── {title} " + "─" * max(0, 46 - len(title)))


def _row(name: str, status: str, detail: str = "", hint: str = "") -> None:
    _emit(f"  {_ICON.get(status, '?')} {name}：{detail}" if detail else f"  {_ICON.get(status, '?')} {name}")
    if hint and status in ("fail", "degraded"):
        _emit(f"      提示：{hint}")


def _summarize(report) -> int:
    _emit()
    _emit(f"结论：{report.summary}")
    for note in report.notes:
        _emit(f"  · {note}")
    if report.failures:
        return 2
    if report.degraded:
        return 1
    return 0


# ---- 第一部分：平台原生自检（复用产品内的自检模块）----


def run_platform_checks() -> int:
    from linglong_clip.utils.selfcheck import run_self_check

    _section("平台原生能力")
    report = run_self_check()
    for item in report.results:
        _row(item.name, item.status, item.detail, item.hint)
    code = _summarize(report)
    globals()["_REPORT"] = report
    return code


# ---- 第二部分：Qt 剪贴板端到端（需要图形会话）----


def run_gui_checks() -> int:
    _section("Qt 剪贴板端到端")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        _row("PySide6 可用性", "fail", f"导入失败：{exc}", "pip install PySide6")
        return 2

    app = QApplication.instance() or QApplication([])
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.ui.clipboard_qt import QtClipboardAdapter

    from linglong_clip.utils.paths import data_dir

    adapter = QtClipboardAdapter(app.clipboard(), data_dir() / "images")

    token = f"linglong-selfcheck-{int(time.time())}"
    adapter.set_text(token)
    app.processEvents()
    read = adapter.read_clipboard()
    if read is not None and read.content == token:
        _row("文本写入并读回", "ok", f"往返一致（{len(token)} 字符）")
    else:
        got = "" if read is None else read.content[:40]
        _row("文本写入并读回", "fail", f"读回不一致：{got!r}")
        return 2

    item = ClipboardItem(kind="text", content=token)
    adapter.write_item(item)
    app.processEvents()
    if app.clipboard().text() == token:
        _row("write_item 写入", "ok")
    else:
        _row("write_item 写入", "fail", "QClipboard 未取到内容")

    _emit("  （已把剪贴板内容改为自检令牌，请知悉）")
    return 0


# ---- 第三部分：三平台各自的额外真机项 ----


def run_windows_extras() -> int:
    _section("Windows 专有")
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except Exception as exc:  # noqa: BLE001
        _row("user32 加载", "fail", repr(exc))
        return 2

    # 剪贴板序列号：事件驱动之外最省的变化检测
    try:
        seq = int(user32.GetClipboardSequenceNumber())
        _row("剪贴板序列号", "ok" if seq else "degraded", f"当前序列号 {seq}")
    except Exception as exc:  # noqa: BLE001
        _row("剪贴板序列号", "fail", repr(exc))

    # 系统剪贴板历史：开关（读注册表）+ 真实读取能力
    try:
        from linglong_clip.platform.windows import history_enabled

        enabled = history_enabled()
        if enabled is True:
            _row("系统剪贴板历史开关", "ok", "已开启")
        elif enabled is False:
            _row(
                "系统剪贴板历史开关",
                "degraded",
                "已关闭",
                "设置 → 系统 → 剪贴板 → 打开「剪贴板历史记录」，"
                "否则 --import-native-history 读不到任何东西",
            )
        else:
            _row(
                "系统剪贴板历史开关",
                "degraded",
                "读不到注册表值（可能从未开启过）",
                "同上；读不到只影响提示，不影响其它功能",
            )
    except Exception as exc:  # noqa: BLE001
        _row("系统剪贴板历史开关", "fail", repr(exc))

    # WinRT 投影：历史读取靠它，缺了就只能空手而归
    try:
        from linglong_clip.platform.windows import _load_winrt

        _load_winrt()
        _row("WinRT 投影", "ok", "可导入（历史读取依赖它）")
    except Exception as exc:  # noqa: BLE001
        _row(
            "WinRT 投影",
            "degraded",
            f"不可用：{exc}",
            "pip install winrt 或 pip install winsdk；"
            "缺了只影响系统历史导入，其余功能不受影响",
        )

    # 真去读一次历史：符号在 vs 真能读到东西，是两回事
    try:
        from linglong_clip.platform.windows import WindowsBackend

        backend = WindowsBackend()
        supported = backend.is_history_supported()
        if supported:
            items = backend.read_windows(limit=5)
            _row(
                "系统剪贴板历史读取",
                "ok",
                f"读到 {len(items)} 条文本（最多取 5 条验证）",
            )
        else:
            _row(
                "系统剪贴板历史读取",
                "degraded",
                "能力探测判定不可用",
                "需 Win10 1809+、系统历史已开启、且装了 WinRT 投影",
            )
    except Exception as exc:  # noqa: BLE001
        _row("系统剪贴板历史读取", "fail", repr(exc))

    # 事件驱动监听依赖 AddClipboardFormatListener
    try:
        ok = bool(getattr(user32, "AddClipboardFormatListener", None))
        _row("AddClipboardFormatListener", "ok" if ok else "fail",
             "符号存在" if ok else "当前系统不支持事件驱动监听，将退回轮询")
    except Exception as exc:  # noqa: BLE001
        _row("AddClipboardFormatListener", "fail", repr(exc))

    # RegisterHotKey 真实注册一次
    try:
        hwnd_none, hotkey_id = None, 0xBEEF
        mod_alt, mod_ctrl, mod_shift, mod_norepeat = 0x1, 0x2, 0x4, 0x4000
        vk_f24 = 0x87
        flags = mod_ctrl | mod_alt | mod_shift | mod_norepeat
        if user32.RegisterHotKey(hwnd_none, hotkey_id, flags, vk_f24):
            user32.UnregisterHotKey(hwnd_none, hotkey_id)
            _row("RegisterHotKey 真实注册", "ok", "Ctrl+Alt+Shift+F24 注册并撤销成功")
        else:
            err = ctypes.get_last_error()
            _row("RegisterHotKey 真实注册", "degraded", f"注册失败（错误码 {err}）",
                 "被占用属正常；若所有组合都失败说明权限受限")
    except Exception as exc:  # noqa: BLE001
        _row("RegisterHotKey 真实注册", "fail", repr(exc))

    # 临时剪贴板格式（写入不进系统历史）
    try:
        fmt = user32.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")
        _row("排除系统历史格式", "ok" if fmt else "degraded",
             f"格式 ID {fmt}" if fmt else "系统未注册该格式，敏感内容可能进系统历史")
    except Exception as exc:  # noqa: BLE001
        _row("排除系统历史格式", "fail", repr(exc))
    return 0


def run_macos_extras() -> int:
    _section("macOS 专有")

    # osascript 取前台应用
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first application process '
             "whose frontmost is true"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False,
        )
        name = out.stdout.decode("utf-8", "replace").strip()
        if name:
            _row("osascript 前台应用", "ok", name)
        else:
            _row("osascript 前台应用", "degraded",
                 out.stderr.decode("utf-8", "replace").strip()[:120],
                 "首次调用需要在「系统设置 → 隐私与安全性 → 自动化」里授权")
    except Exception as exc:  # noqa: BLE001
        _row("osascript 前台应用", "fail", repr(exc))

    # pbpaste / pbcopy
    for tool in ("pbpaste", "pbcopy"):
        path = shutil_which(tool)
        _row(f"{tool} 可用", "ok" if path else "degraded", path or "未找到，原生剪贴板读写将不可用")

    # Quartz（全局快捷键）
    try:
        import Quartz  # type: ignore

        _row("pyobjc Quartz 导入", "ok", getattr(Quartz, "__name__", "Quartz"))
        trusted = bool(Quartz.AXIsProcessTrusted())
        _row("辅助功能权限", "ok" if trusted else "degraded",
             "已授予" if trusted else "未授予",
             "未授予时全局快捷键无法生效：系统设置 → 隐私与安全性 → 辅助功能")
    except ImportError:
        _row("pyobjc Quartz 导入", "degraded", "未安装",
             "pip install pyobjc-framework-Quartz；未安装时全局快捷键降级为应用内快捷键")
    except Exception as exc:  # noqa: BLE001
        _row("pyobjc Quartz 导入", "fail", repr(exc))
    return 0


def shutil_which(name: str) -> str:
    import shutil

    return shutil.which(name) or ""


def run_linux_extras() -> int:
    _section("Linux 专有")
    from linglong_clip.platform.linux import session_type

    _row("会话类型", "ok", session_type() or "未知")
    for tool in ("wl-paste", "wl-copy", "xclip", "xsel", "xdotool", "tesseract"):
        path = shutil_which(tool)
        _row(f"{tool} 可用", "ok" if path else "degraded", path or "未安装（相关功能会降级）")
    try:
        import Xlib  # type: ignore  # noqa: F401

        _row("python-xlib 导入", "ok")
    except ImportError:
        _row("python-xlib 导入", "degraded", "未安装", "pip install python-xlib；缺失时退回 xclip/xdotool")
    return 0


# ---- 主流程 ----


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="玲珑剪切板 · 真机原生能力自检")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--gui", action="store_true", help="额外验证 Qt 剪贴板读写")
    args = parser.parse_args(argv)

    _emit("玲珑剪切板 · 真机自检")
    _emit(f"平台：{sys.platform} · Python {sys.version.split()[0]}")
    _emit()

    codes = [run_platform_checks()]

    if args.gui:
        codes.append(run_gui_checks())

    if sys.platform.startswith("win"):
        codes.append(run_windows_extras())
    elif sys.platform == "darwin":
        codes.append(run_macos_extras())
    else:
        codes.append(run_linux_extras())

    code = max(codes)

    if args.json:
        report = globals().get("_REPORT")
        payload = {
            "platform": sys.platform,
            "python": sys.version,
            "exit_code": code,
            "report": report.to_dict() if report is not None else None,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    _emit()
    _emit(f"退出码 {code}（0=正常，1=有降级，2=有失败）")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
