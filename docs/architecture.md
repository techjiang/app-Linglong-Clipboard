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
   │    · 截图 OCR（OcrEngine）—— 识别结果写回 content
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

历史有两种落盘方式，接口相同、可互换，由 `storage.backend` 决定：

| | `core/storage.py` | `core/repository.py` |
| --- | --- | --- |
| 介质 | SQLite 单文件 | 单个 JSON 文本文件 |
| 检索 | FTS5（`trigram` / `unicode61`） | 进程内扫描 + 字段权重打分 |
| 图片 | `images/` 下的独立 PNG | 默认内联为 data URI |
| 默认 | ✅ | 需显式切换 |

两者暴露同一组方法（`add`/`get`/`list`/`search`/`find_by_hash`/`touch`/
`update_fields`/`delete`/`clear`/`enforce_limits`/`stats`/`close`），
所以 `Application`、CLI、同步模块都不必判分支 —— `open_repository()` 是唯一入口。
这不是过度抽象：同步模块写日志时也要读「本地有哪些 hash」，
两套后端各写一遍迟早会不一致。

### SQLite 后端

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

### JSON 后端

格式刻意做成一眼能读、能手改：空字段不落盘，中文不转义，
每条记录带 `hash` 便于去重与跨设备对账。

几个必须处理的点：

- **原子写**：先写 `.tmp` 再 `os.replace`。直接 `write_text` 在进程被杀时
  会留下半截 JSON，整份历史就废了。
- **损坏不致命**：解析失败时把原文件改名成 `.json.broken` 再重建，
  用户还能自己抢救，而不是启动即崩。
- **图片内联**：`set_image_data()` 登记「相对路径 -> data URI」，
  落盘时写进记录的 `image` 字段。读取端按
  「参数是 data URI -> 登记表 -> 记录内联 -> 同名文件」依次定位，
  最后一级是为了兼容从别处导入、图片未内联的 JSON。
- **写入是整文件重写**：这是刻意的取舍。剪贴板是秒级低频操作，
  几千条记录重写只有几毫秒；换成追加式日志就得处理 `compact` 与半行损坏，
  复杂度不值得。记录量到十万级时应改用 SQLite，而不是优化这里。
- **并发**：单把 `RLock` 保住「读-改-写」，因为每次写都基于内存态整体重写，
  分片锁换不来收益。

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

局域网直连是另一种模式（不依赖网盘），见下面「局域网同步」一节。

## OCR

`core/ocr.py` 把「图片里的文字」变成可检索的 `content`，从而复用既有的
FTS5 索引与排序，检索路径零改动。

```
图片 ClipboardItem（kind=image, content=''）
   │
   │ Collector._enrich() 里最先执行
   ▼
OcrEngine.recognize_full()
   │  · 语言解析：ui.language → tesseract 语言串（chi_sim+eng）
   │  · 缓存：按「路径 + 大小 + mtime」命中，同一张图不重复识别
   │  · 降级：后端不可用 / 超时 / 识别为空 → 返回空结果，不抛异常
   ▼
item.content = 识别文本；item.metadata['ocr'] = {置信度, 语言, 耗时}
```

**顺序很关键：OCR 必须排在 `PrivacyGuard.inspect()` 之前。**
因为 `inspect()` 读的就是 `item.content`，先识别再判定，
「截图里是 API Key」这种情况才会被和文本一样拦下；
反过来则会把密钥当成一张普通图片放行。

设计要点：

- **后端可插拔**（`OcrBackend` 协议）。默认给 tesseract 实现与 `NullBackend`，
  测试注入桩后端，因此**不依赖系统是否装了 tesseract**。
- **失败一律降级**。OCR 是增强能力，缺依赖或识别失败时截图照常入库，
  只是搜不到文字；`_ocr_enrich()` 里用宽泛的 `except` 兜底，不让它影响采集。
- **tesseract 调用要串行化**。它内部有共享状态、非线程安全，
  引擎里用锁保护。
- **置信度过滤**（`ocr.min_confidence`）。低置信结果往往是噪音，
  写进历史反而会污染检索，默认 0 不过滤，用户可按需调高。

## 局域网同步

`core/lan_sync.py` 与 `sync.py` 的共享文件夹模式**互补而非替代**，
两者复用同一个 `SyncRecord` 传输单元、同一个 `SyncCipher` 加密、
同一份 `PrivacyGuard` 判定，避免两条路径的过滤强度不一致。

```
A 设备                                        B 设备
┌────────────────────┐                  ┌────────────────────┐
│ LanSyncServer      │  GET /records    │ LanSyncClient      │
│  · 只提供读取       │ ◄────────────────│  · 主动拉取        │
│  · X-Linglong-Token│   since / limit  │  · 解密 + 导入     │
│  · 过隐私后编码     │ ────────────────►│  · 跳过重复与敏感  │
└────────────────────┘                  └────────────────────┘
        ▲                                        │
        └──────── DiscoveryService（可选 UDP）────┘
```

几个刻意的选择：

- **拉取而非推送**。发送方不接收写入（`POST` 直接返回 405），
  攻击面只剩「读取」一条路径；接收方自己决定何时拉，不必处理对端离线队列。
- **标准库 `http.server`**。同步是几十毫秒级的小请求，
  不值得为一个功能引入 aiohttp/requests。
- **鉴权用共享口令 + 定时安全比较**。同网段不等于可信，
  请求头带上口令，服务端用 `hmac.compare_digest` 比较，避免时序侧信道；
  `/ping` 不校验口令，且只回设备名与版本，不含任何内容。
- **发现默认手动配置**。mDNS/zeroconf 要额外依赖，且不少企业 Wi-Fi 拦组播；
  UDP 广播发现作为可选项，默认关闭。
- **图片与敏感项不外发**。与共享文件夹模式一致。

`LanSync` 对外的形状（`start` / `stop` / `sync_all` / `status`）刻意与
`FolderSync` 对齐，因此 `Application.run_sync` 不需要为模式写分支。

## 线程模型

| 线程 | 职责 | 约束 |
| --- | --- | --- |
| GUI 线程 | 所有 Qt 对象、界面刷新、剪贴板读写 | 不做耗时 I/O |
| 采集线程 | 轮询、识别、风控、落库 | 不碰任何 Qt 对象 |
| 同步线程 | 按需启动，文件读写与加解密 | 不碰 UI |
| 局域网服务线程 | `ThreadingHTTPServer`，每请求一线程 | 只读，不碰 UI |

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
| JSON 存储 | 临时目录里的 JSON 文件，覆盖损坏恢复、原子写、内联图片与搬迁 |
| 平台层 | 测接口契约与降级路径；原生实现用桩对象 |
| 界面 | Qt `offscreen` 平台，无需显示器，CI 可跑 |
| 端到端 | 真实 Qt 剪贴板 → 采集 → 落库 → 检索 → 变换 → 写回 |

平台原生代码（Windows/macOS）在 Linux CI 上无法执行，
因此把「能力探测」与「能力使用」分开：探测逻辑可测，
使用逻辑通过桩对象验证契约。
