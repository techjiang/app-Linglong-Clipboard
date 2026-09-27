# 贡献指南

感谢参与玲珑剪切板。本文说明开发约定，动手前请先读一遍。

## 环境准备

```bash
pip install -r requirements-dev.txt

# Linux 还需要 Qt 的图形运行库
sudo apt-get install -y libegl1 libglib2.0-0 libxkbcommon-x11-0 libdbus-1-3
```

## 开发约定

### 分层约束（硬性）

| 规则 | 原因 |
| --- | --- |
| `core/` 不导入 Qt | 核心逻辑要能在无图形环境下测试；也让换界面框架的代价可控 |
| `ui/`、`core/` 里不出现 `sys.platform` 判断 | 平台差异必须收敛在 `platform/` |
| `ui/` 不直接读写数据库 | 界面通过信号发意图，由 `Application` 统一处理 |
| 历史仓库通过 `core/repository.py` 的 `open_repository()` 获取 | 存储有 SQLite 与 JSON 两种后端，调用方不该到处判分支 |

### 新增原生能力

必须提供降级路径。参考 `platform/base.py` 的接口设计：
每个方法都有「不支持」的默认实现，子类只覆盖自己能做到的。

并在 README 的「三平台原生适配」表格里补上对应格子。

### 新增存储后端

`core/storage.py`（SQLite）与 `core/repository.py`（JSON 单文件）是**并列**关系，
不是继承关系。要加第三种后端，实现同一组方法即可：

```
add / touch / update_fields / delete / delete_many / clear
get / find_by_hash / latest / list / search
count / count_by_kind / top_sources / dedupe / recanonicalize
enforce_limits / purge_expired_sensitive
get_meta / set_meta / optimize / stats / close
```

并在 `open_repository()` 里加分支、在 `config.DEFAULTS["storage"]` 里加可选项。
注意 `images_dir` 属性必须存在 —— 采集器要用它决定图片放哪。
`tests/test_repository.py` 的用例可以照着抄一份给新后端。

### 新增转换器

在 `core/automation.py` 里写一个纯函数，然后注册进 `_build_registry()`：

```python
def _my_transform(text: str, option: str = "default") -> Optional[str]:
    """返回 None 表示「不适用」，抛 TransformError 表示「确实是错的」。"""
    if not text.startswith("..."):
        return None
    return text.upper()
```

约定：

- 返回 `None` → 内容不适用该转换，提示「内容不适用于该转换」
- 抛 `TransformError` → 用户输入有误，错误信息直接展示给用户
- 其它异常 → 被捕获并记日志，不影响界面

同时补 `tests/test_automation.py` 里 `test_builtin_transforms` 的参数化用例。

### 修改判重规则

「两条内容算不算同一条」由 `core/collector.py` 的 `_canonical_payload()`
决定，它把内容交给 `core/normalize.py` 归一后再算指纹。

改这里要记住三件事：

1. **归一结果不写回 `content`**。它只用于算 `hash`，用户看到的必须仍是原文。
2. **判据要保守**。误合并比不合并更糟 —— 不合并只是列表多几行，
   误合并会让用户找不到东西且无法恢复。拿不准的一律不归一。
3. **存量数据要能重算**。新增归一规则后，老记录的指纹是旧规则算的，
   必须让 `recanonicalize()` 能识别并改写它们（两个后端各实现一次），
   否则新规则只对新数据生效，历史里的重复永远合不了。

跟踪参数名单只在 `core/normalize.py` 里定义一份，
`automation.py` 的 `url.strip_tracking` 转换器要复用它而不是另抄一份。

## 测试

```bash
pytest tests/ -q                                     # 全部
pytest tests/ --cov=linglong_clip --cov-report=html  # 带覆盖率
pytest tests/test_storage.py -k fts -v               # 单个模块
```

要求：

- **核心模块（`core/`）覆盖率维持在 85% 以上**，新增逻辑同步补用例
- 界面测试走 Qt `offscreen` 平台，不要引入对真实显示器的依赖
- 数据目录用 `tmp_path` / `LINGLONG_CLIPBOARD_HOME` 隔离，不要写到真实用户目录
- 涉及平台的用例用 `pytest.importorskip`

## 提交

提交信息与 PR 标题统一用 [Conventional Commits](https://www.conventionalcommits.org/) 风格：

```
feat(core): 支持按来源应用分组
fix(linux): 修正 Wayland 下主选择区读取
docs(readme): 补充 Wayland 快捷键说明
refactor(semantic): 简化意图解析的停用词处理
test(storage): 补充 FTS 索引重建用例
```

PR 描述里请说明：

1. 解决了什么问题（不是「改了什么」）
2. 验证方式（跑了哪些测试、手工验证步骤）
3. 有取舍时说明考虑过但放弃的方案

## 代码风格

- 类型标注：公开函数都要标
- 注释写「为什么」，不写「是什么」—— 代码能表达的不重复一遍
- 不引入不必要的依赖：几行能写清的逻辑不要拉第三方包
  （新增依赖请在 PR 里说明理由并做成可选）
- 删除调试输出与被注释掉的旧实现

## 报告问题

Issue 里请包含：

- 平台与版本（Windows 11 / Ubuntu 24.04 Wayland / macOS 14）
- 复现步骤
- 期望行为与实际行为
- 相关日志（`logs/linglong.log`，**注意先检查是否含敏感内容**）
