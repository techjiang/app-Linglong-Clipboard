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

重新生成：

```bash
QT_QPA_PLATFORM=offscreen python packaging/screenshots.py
```

脚本走的是与界面测试相同的 offscreen 渲染路径，无需显示器，CI 里也能跑。
