"""打包资产与安装包的回归测试。

这些用例盯着的是「能构建」和「能交付」之间的差别：
- 图标文件必须存在且格式正确，否则 Windows 的 exe 会是一张白纸；
- 目录模式产物里必须真的有 Qt 运行库，否则装完双击没反应；
- 数据目录的说明必须能被界面读到，否则「数据存在哪」又变成靠猜。

它们不跑构建（太慢），只校验产物与约定本身。
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "assets" / "icon"


def test_icon_master_svg_exists():
    assert (ICON / "linglong-clipboard.svg").is_file()


def test_small_icon_master_exists():
    """16~32px 走简化母版：细节在那么小的尺寸里只会糊成灰点。"""
    assert (ICON / "linglong-clipboard-small.svg").is_file()


@pytest.mark.parametrize("size", [16, 24, 32, 48, 64, 128, 256, 512])
def test_png_sizes_exist(size):
    path = ICON / "png" / f"linglong-clipboard-{size}.png"
    assert path.is_file(), f"缺少 {size}px 图标"
    # PNG magic：确认不是空文件或占位文本
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_ico_contains_multiple_sizes():
    """ICO 必须装多档位：Windows 会按 DPI 自己挑，只给一张会糊。"""
    data = (ICON / "linglong-clipboard.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    assert count >= 5, f"ICO 只装了 {count} 档，分辨率切换会糊"

    sizes = set()
    for index in range(count):
        offset = 6 + index * 16
        width = data[offset] or 256
        sizes.add(width)
    assert {16, 32, 48, 256} <= sizes


def test_icns_header_is_valid():
    data = (ICON / "linglong-clipboard.icns").read_bytes()
    assert data[:4] == b"icns"
    declared = struct.unpack(">I", data[4:8])[0]
    assert declared == len(data), "ICNS 长度字段与实际文件不符，macOS 会拒绝这个图标"


def test_icns_has_retina_and_small_entries():
    """icp4/icp5 顾小尺寸，ic10 是 512@2x —— 缺了 Dock 里会模糊。"""
    data = (ICON / "linglong-clipboard.icns").read_bytes()
    for code in (b"icp4", b"icp5", b"ic07", b"ic08", b"ic10"):
        assert code in data, f"ICNS 缺少 {code!r}"


def test_spec_embeds_platform_icon():
    """spec 的 icon= 不能是 None：那是 exe 在资源管理器里的那张脸。"""
    text = (ROOT / "packaging" / "linglong-clipboard.spec").read_text(encoding="utf-8")
    assert "icon=icon_file" in text
    assert "icon=None," not in text


def test_spec_ships_icon_assets():
    """运行时 setWindowIcon 读的是 datas 里那份，不能只嵌进二进制。"""
    text = (ROOT / "packaging" / "linglong-clipboard.spec").read_text(encoding="utf-8")
    assert 'assets_dir / "icon"' in text
    assert '"assets/icon"' in text


def test_spec_defaults_to_onedir():
    """默认必须是目录模式。

    单文件版每次启动要把几十 MB 的 Qt 解压到临时目录，冷启动慢几秒，
    托盘图标在解压期间还是空白 —— 安装包里这么做的收益是零。
    """
    text = (ROOT / "packaging" / "linglong-clipboard.spec").read_text(encoding="utf-8")
    assert 'os.environ.get("LINGLONG_ONEDIR", "1")' in text


def test_exe_name_is_ascii():
    """可执行文件名必须是 ASCII。

    中文 exe 名在 NSIS + Wine 链路上会被弄丢：编译能过，写盘时主程序消失，
    装出来只剩 _internal 和卸载器 —— 只有「装一次再列目录」才发现。
    用户看到的中文名由快捷方式与窗口标题负责。
    """
    text = (ROOT / "packaging" / "linglong-clipboard.spec").read_text(encoding="utf-8")
    assert 'name="linglong-clipboard"' in text
    assert 'name="玲珑剪切板"' not in text


def test_windows_installer_defines_expected_metadata():
    text = (ROOT / "packaging" / "windows" / "installer.nsi").read_text(encoding="utf-8")
    # 装完能在开始菜单找到、能卸载、能在添加/删除程序里看到版本
    assert "$SMPROGRAMS" in text
    assert "$DESKTOP" in text
    assert "uninstall.exe" in text
    assert "Uninstall\\${APP_ID}" in text
    assert "DisplayVersion" in text


def test_windows_installer_keeps_user_data():
    """卸载不能删用户历史。

    剪贴板历史是用户数据，卸载程序时清掉它不可接受 —— 重装后应该还在。
    """
    text = (ROOT / "packaging" / "windows" / "installer.nsi").read_text(encoding="utf-8")
    section = text.split('Section "Uninstall"', 1)[1].split("SectionEnd")[0]
    # 卸载只删安装目录；数据目录在 $APPDATA\LinglongClipboard，不能碰
    assert 'RMDir /r "$APPDATA' not in section
    assert 'Delete "$APPDATA' not in section


def test_linux_desktop_entry_declares_icon_and_exec():
    text = (ROOT / "packaging" / "linux" / "build_deb.py").read_text(encoding="utf-8")
    assert "Icon={icon_name}" in text
    assert "Exec={exec_path}" in text
    assert "StartupWMClass" in text


def test_deb_builder_writes_directory_entries():
    """dpkg 依赖 tar 里的目录条目来建目录树。

    只有文件没有目录时，`dpkg -i` 会报
    `unable to create '.../libQt6Core.so.6.dpkg-new': No such file or directory`。
    """
    text = (ROOT / "packaging" / "linux" / "build_deb.py").read_text(encoding="utf-8")
    assert "tarfile.DIRTYPE" in text


def test_deb_builder_uses_gnu_tar_format():
    """dpkg 不认 PAX 扩展头（`unsupported PAX tar header type 'x'`）。

    tarfile 默认在长路径或非 ASCII 名时写 PAX 头，而 Qt 的深层路径轻易超 100 字节。
    """
    text = (ROOT / "packaging" / "linux" / "build_deb.py").read_text(encoding="utf-8")
    assert "tarfile.GNU_FORMAT" in text


def test_build_script_generates_icons_before_packaging():
    """图标是硬前置：缺了它 PyInstaller 不报错，只是打个没脸的 exe。"""
    text = (ROOT / "packaging" / "build.py").read_text(encoding="utf-8")
    assert "def ensure_icons" in text
    assert "ensure_icons()" in text


def test_build_script_defaults_to_onedir():
    text = (ROOT / "packaging" / "build.py").read_text(encoding="utf-8")
    assert "onedir = not args.onefile" in text


# ---- 数据持久化的可见性 ----


def test_empty_state_panel_names_storage_location(qt_app, home):
    """空历史页必须写明数据目录与历史文件。

    此前只有一句「复制任意内容后会自动出现在这里」，用户完全不知道
    历史落在磁盘哪里，「数据持久化」这件事只能靠猜 —— 被当成缺陷报上来。
    """
    from linglong_clip.ui.empty_state import EmptyStatePanel
    from linglong_clip.utils.paths import data_dir, database_path

    panel = EmptyStatePanel()
    values = [label.text() for label in panel._path_labels.values()]
    assert str(data_dir()) in values
    assert str(database_path()) in values


def test_empty_state_panel_offers_open_dir(qt_app, home):
    from linglong_clip.ui.empty_state import EmptyStatePanel

    panel = EmptyStatePanel()
    seen = []
    panel.open_data_dir_requested.connect(lambda: seen.append(1))
    panel._open_dir.click()
    assert seen, "「打开数据目录」按钮没有连上任何信号"


def test_first_launch_announce_is_one_shot(tmp_path, monkeypatch):
    """首次启动提示只说一次，标记要落盘。

    每次启动都弹一遍等于骚扰；一次都不说则等于没做。
    """
    from linglong_clip.utils.config import Config

    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(tmp_path))
    config = Config()
    assert config.get("ui.storage_intro_shown", False) is False
    config.set("ui.storage_intro_shown", True)
    assert Config().get("ui.storage_intro_shown") is True


def test_settings_has_open_data_dir_button(qt_app, home):
    from linglong_clip.ui.settings_dialog import SettingsDialog
    from linglong_clip.utils.config import Config

    dialog = SettingsDialog(Config())
    assert dialog._open_data_dir.isEnabled()
    assert "数据目录" in dialog._open_data_dir.text() or "打开" in dialog._open_data_dir.text()


def test_resources_locates_icon_outside_bundle():
    """源码运行时也要找得到图标，否则开发时看不到真实效果。"""
    from linglong_clip.utils import resources

    assert resources.icon_png(64) is not None
    assert resources.icon_path() is not None


def test_window_icon_is_not_null(qt_app):
    """窗口图标不能是空的：空 QIcon 会让标题栏与任务栏都显示默认方块。"""
    from linglong_clip.ui.icons import app_icon, window_icon

    assert not app_icon().isNull()
    icon = window_icon()
    assert not icon.isNull()
    sizes = {size.width() for size in icon.availableSizes()}
    assert {16, 32, 64} <= sizes, f"图标档位不全：{sorted(sizes)}"


def test_tray_prefers_resource_icon(qt_app):
    """托盘图标走资源文件而不是单张绘制位图：多档位在高 DPI 下才不糊。"""
    from linglong_clip.ui.theme import DARK
    from linglong_clip.ui.tray import tray_icon

    icon = tray_icon(DARK)
    sizes = {size.width() for size in icon.availableSizes()}
    assert len(sizes) > 1, "托盘图标只有一张位图，切换 DPI 时会发灰"
