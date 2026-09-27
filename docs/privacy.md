# 隐私与安全

玲珑剪切板处理的是用户最敏感的数据之一 —— 凡是经过剪贴板的东西。
本文说明数据流向与防护措施，便于自查与审计。

## 数据流

```
系统剪贴板
    │
    │  截图先做 OCR（仅本地 tesseract 进程，不出网）
    ▼
┌─────────────────────┐
│  PrivacyGuard       │  ← 唯一的判定入口（判定对象是 OCR 后的文本）
│  · 来源黑名单        │
│  · 敏感信息正则      │
└──────┬──────────────┘
       │
       ├── 拒绝 ──→ 直接丢弃，不写任何地方（连日志都不含原文）
       │
       └── 放行 ──→ 本机 SQLite
                       │
                       │  同步前再过一次同样的策略
                       ▼
                   加密（可选）→ 用户自己的同步目录 / 局域网对端
```

**OCR 的顺序是刻意的**：识别在 `PrivacyGuard` 之前执行，判定读的是识别出的文本。
如果反过来先判定再识别，一张「上面写着 API Key」的截图就会被当成普通图片放行。
现在这种情况会与纯文本一样被拦下，密钥既不进历史库，也不进检索索引。

三处关键点：

1. **判定集中在 `PrivacyGuard`**。采集、同步、复制出站都调它，
   不存在「某条路径忘了过滤」的可能。
2. **敏感内容默认不落盘**。`sensitive_retention_seconds = 0` 意味着
   识别出密码类内容后直接丢弃，不进历史、不进索引、不进同步。
3. **不经过任何服务器**。没有账号体系，没有遥测，没有云 API 调用。
   OCR 走本地 `tesseract` 进程，识别结果不离开本机；
   局域网同步是设备间直连，接收方主动拉取，也没有中间人。

## 硬拦截（不可配置）

以下情况**一定会被丢弃**，设置里没有开关：

- **PEM 私钥**：匹配 `-----BEGIN ... PRIVATE KEY-----` 的完整块。
  这类内容一旦泄露后果不可逆，不给用户「要不要记」的选择。

## 来源过滤

### 内置密码管理器识别

默认跳过以下应用（按名称片段匹配，不区分大小写）：

`1password`、`bitwarden`、`keepass`、`lastpass`、`dashlane`、`enpass`、
`keeper`、`nordpass`、`protonpass`、`roboform`、`buttercup`、`pass`、
`gnome-keyring`、`seahorse`、`keychain`、`credential`

此外，窗口标题含「密码 / 密钥 / password / passwd / keychain / vault」
的也会被拦下 —— 覆盖尚未被识别的管理器。

可在设置 → 隐私 里关闭，或添加额外的屏蔽应用（逗号分隔）。

## 敏感信息识别

以下模式被识别后会标记为敏感：

| 类型 | 匹配对象 |
| --- | --- |
| 私钥 | PEM 格式的私钥块 |
| API Key | `sk-`、`ghp_`、`glpat-`、`xoxb-` 等前缀的长串 |
| AWS 凭证 | `AKIA` 开头的 20 位标识 |
| JWT | 三段式 `eyJ...` token |
| 银行卡 | 15–19 位数字 |
| 身份证 | 18 位中国身份证号 |
| 密码字段 | `password:`、`token=`、`secret=` 等后跟值 |
| 邮箱 | 标准邮箱格式 |
| 手机号 | 中国大陆手机号 |

### 保留策略

| 设置 | 行为 |
| --- | --- |
| `sensitive_retention_seconds = 0`（默认） | 直接丢弃，不进历史 |
| `sensitive_retention_seconds > 0` | 记录但标记，超期由定时任务清除 |

标记为敏感的记录在列表里显示 🛡 图标，不会参与跨设备同步。

### 脱敏

开启「复制到外部时脱敏」后，敏感片段被替换为占位符，**字段名保留**：

```
输入：password: hunter2xyz
输出：password: [已脱敏:密码字段]
```

### 自定义规则

设置 → 隐私 里可填自定义正则，格式为 JSON：

```json
{
  "员工号": "EMP\\d{6}",
  "内部工单": "TICKET-[A-Z]{2}-\\d{4}"
}
```

非法正则在加载时跳过并记录告警，不影响其它规则生效。

## 同步加密

开启 `sync.encrypt` 后：

- 算法：Fernet（AES-128-CBC + HMAC-SHA256）
- 密钥派生：PBKDF2-HMAC-SHA256，200,000 次迭代
- 密钥来源：设备名 + 固定盐

> **注意**：这是「防同步盘读取」级别的保护，不是端到端加密。
> 密钥由设备名派生，同一同步组内的设备需要一致的设备名配置。
> 若要更高强度，建议把同步目录放在已加密的容器里（如 VeraCrypt）。

未安装 `cryptography` 时会自动退化为明文并在日志告警一次。

## 不参与同步的内容

即使本地保留，以下内容也**不会导出到同步目录**：

- 标记为敏感的记录
- 图片附件（避免同步目录膨胀，也避免隐私图片外流）
- 来自被屏蔽应用的记录

## 数据落地位置

| 平台 | 数据 | 配置 |
| --- | --- | --- |
| Windows | `%APPDATA%\LinglongClipboard\` | 同左 |
| macOS | `~/Library/Application Support/LinglongClipboard/` | 同左 |
| Linux | `~/.local/share/linglong-clipboard/` | `~/.config/linglong-clipboard/` |

日志（含操作记录，**不含剪贴板原文**）在 `logs/` 子目录，轮转保留 3 份。

## 彻底清除数据

```bash
# Linux
rm -rf ~/.local/share/linglong-clipboard ~/.config/linglong-clipboard

# macOS
rm -rf ~/Library/Application\ Support/LinglongClipboard

# Windows (PowerShell)
Remove-Item -Recurse "$env:APPDATA\LinglongClipboard"
```

或只清记录：`linglong-clipboard --clear`（保留固定项）。

## 自查清单

验证隐私策略是否生效：

1. 在密码管理器里复制一个密码 → 历史里应**无记录**
2. 复制 `password: test123456` → 历史里应**无记录**
3. 复制一段 PEM 私钥 → 历史里应**无记录**
4. 开启脱敏后复制 `token=abcdefghijkl` 并粘贴到别处 → 应显示 `[已脱敏:密码字段]`
5. 复制一张截图 → 图片只在本地 `images/` 目录，同步目录里不应出现

自动化验证见 `tests/test_privacy.py` 与 `tests/test_integration.py`。
