# 检索语法

搜索框既接受直接关键词，也接受自然语言。两者可以混用。

## 关键词检索

直接输入内容片段即可，走 SQLite FTS5 全文索引（`trigram` 分词器）。

```
会议纪要          # 任意位置包含这三个字
https://cnb.cool  # 链接片段
def handler       # 代码片段
```

**2 字符以下的中文**（如「剪贴」）在 `trigram` 下查不到，
实现会自动退回 `LIKE` 模糊匹配，结果一致但数据量大时略慢。

**截图里的文字同样可搜**：开启 OCR 后，识别结果会写进记录的检索内容，
所以「那张写着会议时间的截图」也能用文字命中，无需记住图片本身长什么样。
未安装 tesseract 时该功能自动跳过，其它检索不受影响。

多个词之间是 **OR** 关系：`会议 附件` 会返回包含任一词的记录。

## 自然语言检索

系统会从句子中抽取出过滤条件，剩下的才是关键词。识别出的条件显示在搜索框下方。

### 按类型

| 说法 | 类型 |
| --- | --- |
| 图片、图像、截图、照片、图、image、screenshot、photo | `image` |
| 链接、网址、url、link、网页、website | `link` |
| 文件、路径、目录、文件夹、file、path、document | `file` |
| 颜色、色值、color、hex、rgb | `color` |
| 代码、函数、脚本、片段、code、snippet、function | `code` |
| 富文本、格式、样式、rich、html | `rich` |
| 密码、敏感、密钥、password、secret | `sensitive` |

```
截图              → 只看图片
代码片段          → 只看代码
```

### 按时间

| 说法 | 范围 |
| --- | --- |
| 今天、today | 今天 00:00 起 |
| 昨天、yesterday | 昨天全天 |
| 本周、这周、this week | 本周一 00:00 起 |
| 上周、last week | 上周一 至 上周日 |
| 本月、这个月、this month | 本月 1 日起 |
| 上个月、last month | 上月 1 日至本月 1 日 |
| 最近、recent、recently | 近 3 天 |

```
上周的截图         → 类型：图片 + 时间：上周
今天复制的内容      → 时间：今天
```

### 按来源应用

用「来自 / 从 / 在 / from / in」引出应用名：

```
来自 Chrome 的链接
从 vscode 复制的代码
in firefox
```

常见应用有别名映射，写 `chrome` 就能匹配 `google-chrome`、`chromium`：

| 别名 | 实际匹配 |
| --- | --- |
| chrome | chrome, google-chrome, chromium |
| vscode | code, vscode, visual studio code |
| terminal | terminal, konsole, gnome-terminal, alacritty, kitty, iterm |
| 微信 | wechat, weixin |

### 按状态

```
固定项 / 置顶 / pinned    → 只看已固定
收藏的代码 / favorite     → 只看已收藏（可叠加类型）
```

## 组合使用

```
上周来自 chrome 的链接      → 类型：链接 + 时间：上周 + 来源：chrome
收藏的代码                 → 类型：代码 + 仅收藏
最近图片                   → 类型：图片 + 时间：近 3 天
```

## 排序规则

结果按四个信号加权排序，权重在 `core/semantic.py` 的 `RankingWeights` 里可调：

| 信号 | 默认权重 | 说明 |
| --- | --- | --- |
| 全文相关度 | 1.0 | 命中越靠前得分越高 |
| 时间衰减 | 0.45 | 半衰期 7 天 |
| 使用频次 | 0.3 | `log(1+次数)`，收敛不会无限增长 |
| 固定 / 收藏 | 0.6 / 0.35 | 直接加成 |
| 精确匹配 | 0.8 | 完全相等时额外加成 |

固定项永远排在最前，其次是收藏项。

## 命令行里使用

同一套解析逻辑，CLI 里也能用：

```bash
linglong-clipboard --search '上周的截图'
linglong-clipboard --search '来自 chrome 的链接' --json
```
