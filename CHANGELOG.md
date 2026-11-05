# 更新日志

版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
