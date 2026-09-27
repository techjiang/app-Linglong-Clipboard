# 架构说明

本文面向二次开发者，说明分层边界、关键数据流与几个刻意的设计取舍。
面向使用者的内容见 [README](../README.md)。

## 分层

```
        ┌─────────────────────────────────────────┐
        │  ui/          界面与交互，唯一依赖 Qt   │
        └────────────────┬────────────────────────┘
                         │ 只向下调用
        ┌────────────────▼────────────────────────┐
        │  core/        业务逻辑，零 Qt 依赖      │
        │               可脱离图形环境测试        │
        └────────────────┬────────────────────────┘
                         │
        ┌────────────────▼────────────────────────┐
        │  platform/    平台差异，接口 + 多实现   │
        └─────────────────────────────────────────┘
```

三条硬约束：

1. **`core/` 不导入 Qt。** 这是整个设计的地基。核心逻辑能在无显示器环境下跑测试，
   也让「换掉界面框架」变成局部改动。
2. **平台判断不出现在 `ui/` 和 `core/`。** 需要原生能力时调 `platform/` 的接口，
   由 `detect_backend()` 挑选实现。
3. **`ui/` 不直接读写数据库。** 界面通过信号把意图发给 `Application`，
   由控制器统一处理。这样「暂停采集」「退出清理」这类全局状态只有一处。

## 关键数据流

### 采集

```
系统剪贴板
   │
   │ ① ClipboardMonitor（守护线程，默认 300ms 轮询）
   │    · 用内容指纹比对，指纹未变直接返回
   ▼
   │ ② gui_invoke → 切回 GUI 线程读取
   │    QClipboard 只能在所属线程访问，跨线程读会拿到空数据
   ▼
ClipboardItem（原始载荷）
   │
   │ ③ Collector.process()（仍在采集线程，不碰 Qt）
   │    · 补齐来源（FocusTracker，按秒缓存）
   │    · 类型识别 / 标签抽取（classify）
   │    · 隐私风控（PrivacyGuard）
   │    · 按内容指纹去重（命中则只更新使用次数）
   │    · 落库 + 按条数/天数淘汰
   ▼
SQLite
   │
   │ ④ item_captured 信号（跨线程）
   ▼
GUI 线程：刷新列表 + 状态提示
```

③ 之所以留在后台线程：识别、正则扫描、SQLite 写入都不涉及 Qt 对象，
放主线程会在连续复制时造成可感知的卡顿。

### 检索

```
用户输入「上周的截图」
   │
   ├─ parse_intent()  拆成 {类型: 图片, 时间: 上周, 关键词: ''}
   │
   ├─ 无关键词 + 有过滤条件 → storage.list(kind, since, ...)
   │  有本分支的原因：图片/文件类记录没有可检索文本，
   │  退回全文检索必然查空
   │
   ├─ 内存过滤（来源别名、多类型并集、时间上界）
   │
   └─ rank() 四信号加权排序
        相关度 + 时间衰减(半衰期 7 天) + log(使用频次) + 固定/收藏加成
```

排序权重集中在 `RankingWeights`，便于按用户习惯调整。

## 平台适配

`ClipboardBackend` 定义了能力接口，每个方法都有「不支持」的默认实现。
子类只覆盖自己能做到的：

| 方法 | 用途 | 缺失时的表现 |
| --- | --- | --- |
| `read_primary_text()` | X11/Wayland 主选择区 | 返回空串，该功能不出现 |
| `read_windows()` | 系统剪贴板历史 | 返回空列表 |
| `current_focus()` | 来源应用识别 | 来源显示「未知来源」 |
| `set_transient_text()` | 安全粘贴（限时清除） | 返回 False，改由定时器清空 |

`capabilities()` 返回能力矩阵，供设置页展示与功能降级。

**Windows** 用 `ctypes` 直接调 Win32，零第三方依赖：

- `GetClipboardSequenceNumber` 做低成本变化检测
- `AddClipboardFormatListener` 事件驱动，避免轮询空转
- `OpenClipboard` 带重试，因为其它进程常短暂占用剪贴板
- 写入时附加 `ExcludeClipboardContentFromMonitorProcessing`

**Linux** 区分 X11 与 Wayland：

- Wayland 走 `wl-paste` / `wl-copy`，全局快捷键交给桌面环境
- X11 优先用 Xlib 读 PRIMARY 与 `_NET_ACTIVE_WINDOW`，无 Xlib 时退到命令行工具
- `XGrabKey` 注册时同时抓 NumLock/CapsLock 组合，
  否则用户开着这些锁键时快捷键会失效

**macOS** 用 `osascript` 取前台应用，避免为一个小功能引入 pyobjc 强制依赖。

## 存储

单表 `items` + FTS5 虚拟表，靠触发器保持同步。

```sql
items(id, kind, content, preview, html, rtf, image_path, image_width,
      image_height, files, metadata, source_app, source_title, hash,
      size, pinned, favorite, sensitive, tags, device,
      created_at, used_at, use_count)

items_fts(content, preview, source_app, tags)   -- external content 表
```

几个细节：

- **`trigram` 分词器**：中文和代码片段没有空格，`unicode61` 会整段当一个词。
  `trigram` 按 3 字符切分，对这类内容友好。不可用时退回 `unicode61`。
- **FTS 查询串要转义**：用户输入里的 `AND`、`"`、`*`、`:` 都是 FTS5 语法字符，
  未转义会抛语法错误。实现里按 token 拆分并加引号。
- **短查询走 LIKE**：`trigram` 查不到 3 字符以下的词，
  「剪贴」这种两字中文必须由 LIKE 兜底，所以 FTS 未命中时会再试一次。
- **图片落盘策略**：PNG 存 `images/`，数据库只存相对路径；
  按内容哈希命名，同一张图重复复制只落一份文件。

## 隐私

`PrivacyGuard` 是所有判定的唯一入口 —— 采集、同步、复制出站都调它，
避免「主流程过滤了、同步漏了」这类不一致。

判定分两级：

1. **拒绝记录**：PEM 私钥；来源命中密码管理器黑名单。
2. **标记敏感**：API Key、JWT、身份证、银行卡、密码字段等。
   标记后**默认不落盘**（`sensitive_retention_seconds = 0`），
   用户可在设置里改成限时保留，由定时任务按 TTL 清除。

脱敏规则支持用户自定义正则，非法正则在加载时跳过并告警，不影响其它规则。

## 同步

不设中心服务器，两种模式共用一套「每设备一个日志文件」格式。

```
shared-folder/
├── laptop.llsync     ← 每台设备只追加自己的
├── desktop.llsync
└── server.llsync
```

好处是**天然无写冲突**：多台机器同时同步也不会损坏文件。
文件内部按「4 字节大端长度 + 载荷」分块，读取时逐块解析，
单块损坏只丢那一条，不会让整个日志不可用。

加密用 Fernet（`cryptography`），密钥由口令 + 固定盐经 PBKDF2 派生。
未安装 `cryptography` 时自动退化为明文并在日志告警一次 ——
可选依赖缺失不应该让整个功能不可用。

导出侧的过滤规则与采集侧一致：敏感内容、图片默认都不出本机。

## 线程模型

| 线程 | 职责 | 约束 |
| --- | --- | --- |
| GUI 线程 | 所有 Qt 对象、界面刷新、剪贴板读写 | 不做耗时 I/O |
| 采集线程 | 轮询、识别、风控、落库 | 不碰任何 Qt 对象 |
| 同步线程 | 按需启动，文件读写与加解密 | 不碰 UI |

跨线程通信只有两条通道：

- **GUI 线程 → 采集线程**：无。采集线程是只读消费者。
- **采集线程 → GUI 线程**：
  1. `GuiThreadInvoker.call()` —— 需要返回值时用（读剪贴板）
  2. `item_captured` 信号 —— 单向通知（刷新界面）

`GuiThreadInvoker` 用信号 + `threading.Event` 实现同步投递：
发起方阻塞等待，接收方在主线程执行完置位。超时 2 秒，
应用退出时 `cancel()` 唤醒所有等待者，避免采集线程死等。

## 自动化引擎

转换器是纯函数 `(text, **params) -> str | None`：

- 返回 `None` 表示「这条内容不适用该转换」，不算错误
- 抛 `TransformError` 表示「确实是错的」，错误信息直接给用户看
- 其它异常被捕获并记录，单个动作失败不影响界面

规则是数据结构而非代码，因此能从配置文件热加载，
用户可以在设置界面里改而不用重新编译。

## 测试策略

| 层次 | 做法 |
| --- | --- |
| 核心逻辑 | 直接单元测试，用临时目录隔离数据 |
| 存储 | 内存/临时 SQLite，覆盖 FTS 降级与索引重建 |
| 平台层 | 测接口契约与降级路径；原生实现用桩对象 |
| 界面 | Qt `offscreen` 平台，无需显示器，CI 可跑 |
| 端到端 | 真实 Qt 剪贴板 → 采集 → 落库 → 检索 → 变换 → 写回 |

平台原生代码（Windows/macOS）在 Linux CI 上无法执行，
因此把「能力探测」与「能力使用」分开：探测逻辑可测，
使用逻辑通过桩对象验证契约。
