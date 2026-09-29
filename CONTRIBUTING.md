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

**三条硬性要求**，都是踩过坑才写下来的：

1. **能力矩阵必须与实现一致**。`capabilities()` 里写的 `True` 要有对应的
   代码。macOS 后端曾经把 `transient_clipboard` 写死为 `True`，
   而实际只实现了来源识别 —— 声明了安全粘贴却做不到，比不声明更糟。
2. **实现 `sanity_check()`**。它返回 `(是否可用, 人类可读的原因)`，
   会被 `--selfcheck` 调用。没有它，自检只能验「接口在不在」，
   验不出「功能空不空」。
3. **补仿真原生层测试，而不是打桩断言**。假的 `user32` / 假的 `Quartz`
   要跑真实业务链路（`register()` → 事件分发 → 回调），
   而不是断言「调用了某个方法」。打桩测不出「注册成功但消息没人分发」
   这类缺陷 —— 这正是本轮修掉的两个问题之一。

真机验证清单在 `docs/native-verification.md`，修改平台代码时请一并更新。

### 新增存储后端

`core/storage.py`（SQLite）与 `core/repository.py`（JSON 单文件）是**并列**关系，
不是继承关系。要加第三种后端，实现同一组方法即可：

```
add / touch / update_fields / delete / delete_many / clear
get / find_by_hash / latest / list / search
count / count_by_kind / top_sources / dedupe / recanonicalize
（删除相关的墓碑由 `core/tombstones.py` 独立维护，后端不必实现删除传播）
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

### 新增规则触发时机

`Rule.trigger` 目前是 `auto` / `outbound` / `manual`。
加第四种之前先确认它真的不是前三种的组合 —— 每种时机都意味着
**一个新的调用点**，而调用点就是「配了不生效」最常见的产地。

要加的话，四件事必须一起做：

1. `TRIGGERS` 里加值，并补 `_TRIGGER_WHEN` 的人话说明（用户在界面上要看到
   「什么时候跑」，光有个英文常量等于没说）
2. 复用 `actions_for_trigger()`，不要另写一遍执行逻辑
3. 在 `describe_rules()` 里补这个时机特有的「写了也不会生效」检查
4. 补一条测试，断言**其余的时机不会被这条规则误触发** ——
   规则引擎最容易错的地方不是「该跑没跑」，而是「不该跑的时候跑了」

触发方式写错（自造名字）时 `normalize_trigger()` 会落到 `manual` 并记警告，
同时把原值留在 `raw_trigger` 里。别把这一步省成「直接当 manual」：
单看 `trigger = manual` 是分不清「用户就要手动」和「用户写错了」的。

### 改动「复制/粘贴」路径

历史里的内容出站（复制、直接粘贴、快速粘贴第 N 条）统一经过
`Application.apply_outbound_rules()`。界面上有**五条**入口：右键菜单「复制」、
右键菜单「直接粘贴」、双击、回车 / `Ctrl+C`、`Ctrl+1..9`。

新增入口时不要自己拼 `copy_item()`，走 `MainWindow._emit_copy()` /
`_emit_paste()` —— 它们同时发 `copy_requested` 与 `outbound_requested`。
少发后者，这条路径就会绕开出站规则，用户看到的是
「同样的操作，有时加工了有时没加工」，且无从判断哪个才对。
`test_window_copy_paths_all_route_through_outbound` 守着这件事。

另外，改动写入剪贴板的内容时留意**自我写入指纹**：它由
`_mark_own_write()` 与 `QtClipboardAdapter` 两侧共同决定，
口径必须完全一致（载荷取 `fingerprint_payload()`，类型一律 `text`）。
不一致的表现不是报错，而是历史里多出一条你自己刚复制的内容 ——
很容易被当成「采集正常」而放过。

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

### 新增配置项

**加了配置项就一定要有读取方。** 这一点踩过 7 次坑：设置页有勾选框、
`DEFAULTS` 有默认值、代码里没人读 —— 用户勾了没反应，还以为是自己用错了。

流程：

1. 在 `utils/config.py` 的 `DEFAULTS` 里加键（**只改配置结构时才动
   `SCHEMA_VERSION`**，新增键不需要，`_deep_merge` 会自动补默认值）
2. 在 `ui/settings_dialog.py` 里加控件 —— **注意这只算「写回」，不算读取方**
3. 在真正的业务代码里读它，并在 `Application._on_config_change` 里接上
   热生效（如果不需要重启的话）
4. 跑一遍 `python -m linglong_clip --config-audit`，应当无输出且退出码 0

如果配置结构真的变了（键位改名、语义变更），才需要加一档迁移：

```python
SCHEMA_VERSION = 2

_MIGRATIONS = {
    2: lambda raw: _rename_key(raw, "ui.old_name", "ui.new_name"),
}
```

迁移函数要求：**幂等**、**失败时不破坏数据**、**只读不改传入对象**
（返回新的 dict）。文件的版本比程序新（用户装回旧版）时不会被改动。

### 修改配置淘汰/截断逻辑

- `history.pin_immune` 要同时改 `core/storage.py`（SQLite）和
  `core/repository.py`（JSON），两个后端语义必须逐条对齐
- `capture.max_text_length` 的截断位置**必须在隐私判定之后**：
  判定读的是完整内容，先截断再判定会让「落在截断线之后的密钥」被放行
- 淘汰排序一律把固定项排前，这样关闭豁免时先被淘汰的仍是久用的普通记录

### 修改同步的删除语义

「删除能否跨设备生效」由 `core/tombstones.py` 与两条同步路径共同决定。
改动前请先确认这几条约束都还成立，每条都对应一个真实会发生的错误：

1. **只记指纹，不记内容**。墓碑里出现被删内容，就等于「删除后仍留一份副本」。
2. **只有显式删除才记墓碑**。压缩淘汰、敏感过期是本机整理，不扩散到别的设备。
   「显式删除」包括界面上的清理按钮 —— 它以 `storage.clear()` 的名义绕过
   `Application`，最容易漏记墓碑，表现是清掉的记录在别的设备上复活。
   新增任何「批量删除」入口时，都要照 `Application.clear_unpinned` 走一遍。
3. **对端离线超过保留期就不追删**。删掉一台离线很久的设备的副本，
   是在丢那台设备自己的数据。
4. **固定项不被对端删除清掉** —— 但只在对它的意图仍然有效时。
   判据是固定的来源设备（`metadata.pinned_by`）：来源等于发起删除的设备，
   说明那个固定正是它设的、而它已经改主意了，属于过期残留，删除要照做。
   （原因见「修改同步的用户标记语义」第 3、7 条：标记取并集，会让
   「谁设的固定」丢失，于是删除保护会认错人。）
5. **本机已记墓碑的指纹，不再从任何日志里导入**。包括设备自己那份日志 ——
   否则它会把自己删掉的记录从自己的日志里复活。
   `deleted_in_batch` 只管「同一批里先有删除」，管不到跨批的情况，两者都要有。
6. **同一批读取中，删除必须先于内容应用**。游标有 1 秒安全窗口，
   两类记录可能落在同一批里；顺序反了，删除会被内容覆盖。
7. **发送侧不能用「已导出内容」的过滤去拦墓碑**。
   「内容已发过」恰恰是删除要传达的前提，拦掉就等于删除永不传播。
8. **失败即不推进/不确认**。对端不可达时保留待发状态，下轮重试。

两条同步路径（`core/sync.py` 的共享文件夹、`core/lan_sync.py` 的局域网）
各有一份 `_apply_tombstone()`，语义必须逐条对齐 —— 历史教训是
「主流程过滤了、同步漏了」这类不一致最难发现。改一处就要改另一处，
并同步更新 `tests/test_tombstones.py` 里两组同名用例。

### 修改同步的用户标记语义

「固定 / 收藏能不能跨设备」由 `core/sync.py` 的 `FlagSnapshot` 与
`apply_flags()` 共同决定。几条约束，每条都对应一个真实会发生的错误：

1. **导出侧要补发标记的变化**。内容发过不等于标记发过 ——
   若沿用「内容已发过就跳过」，标记的后续变化永远传不出去。
   判断依据是「本地标记 vs 日志里已发出的标记」取并集后是否更大。
2. **导入侧对已存在的记录也要合并标记**。命中 `seen_local` 时直接
   `continue` 就等于把对端的标记丢掉。
3. **合并默认取并集，不取最后写入者**。两个标记都是加法式的用户意图；
   单纯并集下「取消过」与「从未设过」表示相同（都是本地 False），
   无法区分。取并集是唯一不会悄悄弄丢显式标记的选择。
   取消的传播由下面第 8 条的独立通道负责，不要在 `merge()` 里动它。
4. **两条同步路径共用同一个 `apply_flags()`**。`FolderSync` 与
   `LanSyncClient` 各写一套迟早分叉 —— `_apply_tombstone()` 当年就踩过。
5. **不能只改一处判断**。`SyncRecord` 的字段、`to_json` / `from_json`、
   `to_item`、导出、导入，五处都要带上标记，漏一处就是静默丢失。
6. **老数据要能读**。`from_json` 对缺失字段必须默认 `False`，
   而不是报错 —— 旧版本写出的记录没有这两个键。
7. **固定标记要记来源设备**（`SyncRecord.pinned_by` / `metadata.pinned_by`）。
   删除保护会读它来判断「这个固定是否已失效」（见「修改同步的删除语义」第 4 条）。
   两个约定不能错：
   - **本机设的记空串**，不写自己的设备名 —— 空串在 `pin_is_stale()` 里
     表示「本机意图」，永远不判为过期。
   - **导出时把空来源补成发送方的设备名**（两条同步路径都要）——
     不补的话对端收到来源为空的固定，会理解成「它自己设的」，
     于是拒绝本该生效的删除。这正是记录被复活的成因。

8. **取消走独立的事件通道**（`core/flag_changes.py`），不要改并集本身。
   一条「取消」是一个带时间戳的显式动作，解算规则是
   **设备内取最新、设备间取并集**（时间戳只用于同一台设备内排序，
   因此不要求跨设备时钟同步）。改动时这几条约束都要保住：

   - **变更不受增量游标约束，也不参与游标推进**。它的时间戳与同批内容的
     `created_at` 不同源；按游标裁掉，一条晚到的取消就永远传不过来。
   - **先落变更、再处理内容**。顺序反了，取消会被同批内容的并集盖回去
     （与删除的批内顺序是同一类问题）。
   - **只靠内容记录碰不到取消**。内容只发一次，标记变化是之后的动作，
     所以要有 `_apply_flag_change()` 按指纹直接落地，不能只依赖内容循环里的
     `apply_flags()`。
   - **导入侧在记录上没有标记时也要看变更存储**。取消恰恰是以
     「记录上什么都没有」的形式表达的，早退会把取消吞掉。
   - **取消后清掉 `pinned_by`**。否则一个过期的来源会跟着未固定的记录
     漂到后续同步里，删除保护会读到过期的设备名。
   - **记录幂等**。同一个值重复登记不新增，否则界面刷新会不断追加事件。
   - **不传 `flag_changes` 时退化为纯并集**，既有调用点不受影响。
   - **两条路径各有一份 `_apply_flag_change()`**，语义必须逐条对齐，
     并同步更新 `tests/test_sync_flags.py` 与 `tests/test_lan_sync.py`
     里两组同名用例（只测一条等于没测）。

9. **局域网的事件确认在写出响应之后**。墓碑与标记变更都不受 `since`
   约束，所以必须有「已送达」的确认，否则每轮都在重传全部历史事件。
   确认必须放在 `_send_json()` 成功之后：`wfile.write` 失败会抛异常，
   走不到确认那步，事件保持待发、下轮重传 —— 宁可重复告知也不能漏。
8. **取消固定要连来源一起清掉**。留着旧来源，后续重设或判过期时会串味。
9. **比对「标记有没有变化」时只看布尔位，不看来源**。来源变化（先本机
   固定、后来对端也固定）不该单独触发重发，否则每轮同步都反复补发。

### 修改「复制出站」的防护语义

「敏感内容复制出去之后有什么保护」由 `core/copy_guard.py` 的
`plan_outbound()` 与 `ui/clipboard_qt.py` 的写回路径共同决定。
几条约束，每条都对应一个真实会发生的错误：

1. **排除标记只能在写入时挂**。内容一旦进了系统剪贴板历史就没有接口能取出来，
   所以 `history_exclusion_formats()` 的格式名必须在 `setMimeData` 之前
   挂到同一个 `QMimeData` 上。此前 `write_item()` 的纯文本分支走了
   `set_text()`，把刚挂好的 mime 丢掉 —— 标记只对带 HTML 的内容生效，
   而敏感内容恰好绝大多数都是纯文本。
2. **格式的值必须为空**。系统只检查格式在不在；塞入真实数据反而会变成
   一块内容被历史当成条目存下来，与目的相反。
3. **敏感项一律挂标记，不给配置机会**。加了「默认关闭」的开关等于没做，
   而这一步没有副作用。脱敏后仍要挂：正则漏一处就漏一处。
4. **清理必须带条件**。只在剪贴板内容仍是我们写的那份时才清。
   无条件清空会删掉用户后来的复制，而且他多半不会立刻发现。
5. **低于 5 秒按「不清空」处理**，不是「立刻清空」。
   后者会让粘贴必然失败。
6. **`capabilities()['transient_clipboard']` 必须跟着实现走**。
   Windows 与 macOS 都曾把它写死为 `True`，而 `set_transient_text`
   一来没有实现、二来没有调用方 —— 声明了安全粘贴却做不到。
7. **没有机制的平台要如实返回空列表**，并在设置页与 `--outbound` 里说明。
   声明做不到的事比不声明更糟。

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
- **平台相关代码必须能用仿真原生层测**：假 `user32` / 假 `Quartz` 跑真实链路，
  别用 `monkeypatch` 断言「调用过某方法」。参考 `tests/test_hotkeys.py`。
- **测不到的行为要换一种验法**：配置项如果没有读取方，就没有可断言的行为。
  这类守卫用静态审计 + 「在假源码树上验证判据」的方式补，
  参考 `tests/test_config_audit.py`。
- **新加 CLI 标志必须有用例守着它不会落到 GUI 分支** ——
  曾经有个标志没登记到分发表，表现是静默挂起（卡到 10 分钟超时才发现），
  而不是报错。参考 `tests/test_cli.py::test_selfcheck_skips_gui_dispatch`。

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
- **`linglong-clipboard --selfcheck --json` 的输出**（原生能力相关问题时）
