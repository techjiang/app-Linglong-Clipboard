# 截图

界面截图由 `tests` 里的界面测试同样的 offscreen 渲染方式生成，
可随代码更新重新产出，避免文档截图与实现脱节。

| 文件 | 内容 |
| --- | --- |
| `main-dark.png` | 主界面（深色主题） |
| `main-light.png` | 主界面（浅色主题） |
| `quick-paste.png` | Ctrl+Shift+V 快速粘贴浮层 |
| `search-dark.png` | 自然语言检索结果 |
| `settings-ocr.png` | 设置页（含 OCR 与局域网同步选项） |
| `settings-capture-primary.png` | 设置页「采集」（含主选择区开关与系统历史导入） |
| `settings-native.png` | 设置页「关于」的原生能力自检面板 |
| `settings-sync-flags.png` | 设置页「同步」的固定/收藏同步规则与删除同步面板 |
| `settings-sync-flag-changes.png` | 同上，但有 1 条取消等待同步（体现同步进度而非空状态） |
| `settings-privacy-outbound.png` | 设置页「隐私」的出站防护实时说明 |
| `settings-automation.png` | 设置页「自动化」：每条规则的执行时机，以及不会生效的当场标出 |
| `settings-interface.png` | 设置页「外观」（列表密度、快速粘贴序号、关闭行为） |
| `density-comfortable.png` / `density-compact.png` | 列表两档密度对比 |
| `cli-image-fetch.png` | 「图片按需补图」命令行的真实输出（两台设备、真实 socket） |
| `cli-numbered-hotkey.png` | 浮层显示期间又复制了东西时，序号实际指向哪一条（真实 id 与内容） |

重新生成：

```bash
QT_QPA_PLATFORM=offscreen python packaging/screenshots.py
```

脚本走的是与界面测试相同的 offscreen 渲染路径，无需显示器，CI 里也能跑。
