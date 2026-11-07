# 更新日志

版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.2] - 2026-11-06

这一版只做一件事：把「能构建出来的东西」变成「装上就能用的东西」。

### 安装版

此前只有 zip / tar.gz，解压出来是一个裸可执行文件：没有菜单项、没有卸载入口、
图标缓存里也没有它。现在两个平台都有真正的安装包：

| 平台 | 安装包 | 装完得到什么 |
| --- | --- | --- |
| Windows | `linglong-clipboard-1.0.2-windows-setup.exe`（33 MB） | 开始菜单项、桌面快捷方式、添加/删除程序条目、卸载器 |
| Linux | `linglong-clipboard_1.0.2_amd64.deb`（89 MB） | 应用菜单图标、`linglong-clipboard` 命令、`apt remove` 可卸载 |

Windows 装到当前用户目录，不需要管理员权限，不弹 UAC。
便携版压缩包仍然提供，给 U 盘和没有安装权限的场景。

### 不再是单文件

打包默认改为**目录模式**。单文件版每次启动要把几十 MB 的 Qt 解压到临时目录，
冷启动慢几秒，托盘图标在解压期间还是空白。安装包里单文件没有任何收益，
代价全落在使用者身上。`LINGLONG_ONEDIR=0` 仍可显式打单文件版。

### 图标

仓库里此前**一个图标文件都没有**，spec 里写的是 `icon=None`，窗口也没设
`setWindowIcon` —— 所以任务栏、标题栏、托盘、exe 全都没有图标。

现在有一份矢量母版（`assets/icon/linglong-clipboard.svg`）+ 一套由它生成的
PNG（9 档）/ ICO（7 档）/ ICNS（11 档，含 Retina）：

- 16~32px 走**另一份简化母版**：放大镜和底板在小尺寸下只会糊成灰点；
- Windows 的 `.exe` 嵌入图标（已在构建产物里校验字节）；
- 窗口设图标，实测 `_NET_WM_ICON` 有 6 档；
- 托盘改读多档位资源，不再用单张绘制位图（高 DPI 下会发灰）；
- `.deb` 把图标装进 `hicolor` 主题的 8 个尺寸目录。

生成脚本 `packaging/icons.py` 只依赖 PySide6，换台机器 clone 下来就能重新生成。

### 数据存在哪，界面直接说

数据一直是持久化的（`%APPDATA%` / `~/.local/share`），但软件里一个字都没提，
用户只能靠猜。现在三处都能看到：

- 首次启动时状态栏提示一次目录路径（标记落盘，之后不再打扰）；
- 空历史时主界面中间那张卡片写明「数据目录 / 存储方式 / 历史文件」，
  带「打开数据目录」按钮；
- 设置 → 采集 → 「历史存储」里同样的信息与按钮。

### 真实运行截图

新增 `packaging/screenshots_live.py`：在真实 X11 里跑**装好的可执行文件**，
用真实剪贴板操作产出截图，并检查 `_NET_WM_ICON` 是否真的设上。

与原有的 offscreen 渲染脚本分工明确：那个证明「界面代码能跑」，
这个证明「装上能用」。图标不显示、托盘空白这类问题只有后者看得出来。

### 修掉的问题

- **Windows 安装包里主程序消失**：可执行文件用的中文名，在 NSIS + Wine 链路上
  编译能过，但写盘时主 exe 直接不见，装出来只剩 `_internal` 和卸载器。
  可执行文件名改为 ASCII，界面上的中文名交给快捷方式与窗口标题。
- **`.deb` 装不上**：手工拼的 tar 缺目录条目（`dpkg -i` 报
  `unable to create '...dpkg-new': No such file or directory`），
  且 tarfile 默认写 PAX 扩展头而 dpkg 不认（`unsupported PAX tar header type 'x'`）。
  改用 GNU 格式并补全目录条目。
- **中文快捷方式写不进去**：Wine 的代码页问题。构建脚本显式设 `LANG=C.UTF-8`。

### 验证

```
2084 passed（原 2052）
```

真机验证（不只看构建是否成功）：

```
Linux   dpkg -i 装上 → /usr/bin 入口可用 → GUI 启动 → xclip 真实复制 5 条全部入库
        分类正确（文本/链接/代码/颜色）→ 重启后记录仍在 → JSON 存储可 grep 中文
        卸载后 /opt 与 /usr/bin 清干净，数据目录保留
        _NET_WM_ICON 6 档，WM_CLASS = linglong-clipboard / 玲珑剪切板
Windows setup.exe 静默安装 → 主 exe 落盘 → 桌面快捷方式、开始菜单、卸载注册项齐全
        exe 内嵌图标字节校验通过 → --version / --where 正常
```

## [1.0.1] - 2026-11-05

### 修复

- **Windows 版 `--version` / `--stats` 闪退**：控制台编码（cp1252 / cp936）装不下中文，
  打印版本号时就抛 `UnicodeEncodeError`，打包成 .exe 后连报错都看不见。
  入口现在会把标准流切到 UTF-8 并允许降级输出。

### 发行

- **CNB 版本页终于有 Windows 包了**：`linglong-clipboard-windows.zip`。
  PyInstaller 不能交叉编译，但 Wine 里跑一个真正的 Windows Python 就能产出原生 .exe，
  于是同一台 Linux 构建机一次出齐 Linux + Windows 两个包。

## [1.0.0] - 2025-11-03

第一个正式版本。三平台共用一份代码，跨平台差异收敛在 `platform/` 适配层。

### 核心能力

- **本地优先的存储**：SQLite + FTS5（`trigram` 分词），中文与代码片段都能检索；
  历史同时落一份可读的 `.clipboard/clipboard.json`，随时能打开看、能手改。
- **语义检索**：搜索框吃自然语言（`上周的截图`、`来自 Chrome 的链接`、`收藏的代码`），
  识别出的条件回显在搜索框下方；排序按全文相关度、时间衰减、使用频次、固定/收藏四项加权。
- **33 种内置转换**：JSON 格式化、路径提文件名、URL 去跟踪参数等，分 5 大类，
  按内容类型自动列出可用动作。
- **隐私保护**：密码、密钥、身份证号等敏感内容自动识别，可选择不落库、不进系统剪贴板历史。
- **跨设备同步**：共享文件夹与局域网直连两种模式，都不经过第三方服务器；
  每台设备只追加自己的日志文件，天然避免并发写冲突；每个同步来源带增量游标，只拉新记录。
- **三平台适配**：Windows / Linux / macOS 剪贴板与全局快捷键，缺失原生能力时优雅降级，
  不让应用启动失败。

### 界面

- 主窗口：左侧筛选、中间卡片列表、右侧预览，深色 / 浅色两套主题。
- 快速粘贴浮层（`Ctrl+Shift+V`）：按序号直选，回车即粘贴。
- 设置对话框、系统托盘、开机自启。

### 命令行

GUI 之外保留纯 CLI：`--list`、`--search`、`--get`、`--stats`、`--clear`、`--compact`、
`--import-sync` / `--export-sync`。服务器、SSH 会话里不启动图形界面也能查历史。

### 发行包

| 平台 | 文件 |
| --- | --- |
| Windows | `linglong-clipboard-windows.zip` |
| macOS | `linglong-clipboard-macos.zip` |
| Linux | `linglong-clipboard-linux.tar.gz` |

### 已知限制

- PyInstaller 不支持交叉编译，三个平台各自在对应系统上构建。
- macOS 包未签名、未公证，首次打开需要在「系统设置 → 隐私与安全性」里放行。
- macOS 全局快捷键需要 `pyobjc-framework-Quartz`，未安装时该功能不可用。
