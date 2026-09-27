# 原生能力真机验证

平台相关的代码（Windows 的 `user32`、macOS 的 `Quartz`）在 Linux CI 上
无法真实调用，此前的做法是用桩对象验「接口形状是否对得上」。
问题在于：**接口对得上，功能可以是空的**。这不是假设，本轮就查出两处：

- macOS 的全局快捷键只探测了 pyobjc 能否 `import`，`register()` 从未覆写，
  继承基类的假实现永远返回 `False`
- Windows 的事件通道：Qt 侧事件过滤器装了，但 `AddClipboardFormatListener`
  从未被调用，`WM_HOTKEY` 也没有分发函数 —— 注册能成功，回调永不触发

两者在 Linux 上都看不出问题：前者 `backend_name` 显示 `macos`、
`is_available` 返回真；后者 `register()` 返回真、日志写着「已注册」。
只有真机操作才会暴露。

本文档给出**不依赖 Windows/macOS 机器也能推进的验证方式**，
以及最终必须落到真机上的那几项。

---

## 一、先跑自检（任何平台）

```bash
linglong-clipboard --selfcheck          # 人类可读
linglong-clipboard --selfcheck --json   # 机器可读，便于贴进 Issue
```

退出码：`0` 全部正常 / `1` 有降级 / `2` 有失败。

Linux 示例（无图形会话的服务器上）：

```
玲珑剪切板 · 原生能力自检
平台：Linux-5.4.241-tlinux4-x86_64-with-glibc2.36
Python：3.11.2
后端：linux

  ✓ 被选中的后端：linux（0.0ms）
  ✓ 后端能力矩阵：primary_selection=否、native_history=否、focus_detection=否（0.1ms）
  ~ 剪贴板原生读取：X11 会话下未找到 Xlib / xclip / xsel（0.0ms）
      提示：读取失败会回落到 Qt 剪贴板，功能仍可用
  ~ 前台窗口识别：未取到前台窗口（0.0ms）
      提示：无图形会话（SSH / 无桌面）时属正常；有桌面却为空则需排查
  ~ 全局快捷键后端：linux-x11 当前不可用（3.7ms）
      提示：无法连接 X 服务器：No module named 'Xlib'
  - 全局快捷键注册：后端不可用，跳过注册探测

结论：2 项正常、3 项降级
```

三级判定是有意区分的，不能合并成一句「不支持」：

| 判定 | 含义 | 需要动作 |
| --- | --- | --- |
| `ok` | 调通了 | 无 |
| `degraded` | 调不通，但已按设计降级，功能由别的路径提供 | 通常无；若影响体验再看提示 |
| `fail` | 预期应能用却没有 | 需要排查 |

自检**只读**：不改写剪贴板，唯一副作用是注册并立即撤销一个
`Ctrl+Alt+Shift+F24`（刻意选一个不会有程序占用的组合）。

---

## 二、没有目标机器时：仿真原生层测试

`tests/test_hotkeys.py`（50 个用例）用**仿真**而不是打桩：

- **Windows**：喂一个记录调用的假 `user32`。`RegisterHotKey` 的入参
  （修饰键位、虚拟键码、`MOD_NOREPEAT`）逐个断言；然后构造一条
  `WM_HOTKEY` 消息走 `handle_native_event()` —— 这条路径正是本轮补上的，
  打桩式测试永远发现不了「注册成功但没人分发消息」
- **macOS**：喂一个假 `Quartz`，覆盖权限未授予、tap 创建失败、
  事件标志位精确匹配（多按一个修饰键不该触发、CapsLock 开着不该失效）、
  系统超时停掉 tap 后必须自己重开、回调抛异常不能把 tap 拖死

关键差异：**仿真原生层跑的是真实业务代码**（`register()` →
`_handle_event()` 的完整链路），而打桩只断言「调用了某个方法」。

```bash
pytest tests/test_hotkeys.py -v
```

覆盖不到的部分仍然存在：真实的键码冲突、真实的权限弹窗、
真实的系统消息投递时序。所以下面这一步不能省。

---

## 三、真机验证（在 Windows / macOS 上执行）

仓库里备了脚本，把散落在各处的探测串成一次运行：

```bash
python tools/native_check.py         # 人类可读
python tools/native_check.py --json  # 附 JSON，便于回帖
python tools/native_check.py --gui   # 额外验证 Qt 剪贴板读写
```

`--gui` 会把剪贴板内容改成自检令牌，跑完记得重新复制你要的东西。
脚本**不修改任何配置与历史**。

### Windows 需要确认的

`tools/native_check.py` 里的 **Windows 专有** 一节会逐项检查：

- `GetClipboardSequenceNumber` 可用（事件驱动去重依赖它）
- `AddClipboardFormatListener` 符号存在 —— 缺了会退回轮询
- `RegisterHotKey` 真实注册一次 `Ctrl+Alt+Shift+F24` 再撤销
- `ExcludeClipboardContentFromMonitorProcessing` 格式可注册
  （敏感内容不进系统剪贴板历史）

脚本覆盖不到的，需要手工操作一遍：

1. **全局快捷键**。启动应用，在**记事本**里按 `Ctrl+Shift+V`。
   预期：快速粘贴浮层弹出，且记事本里没有插入任何字符
   （热键事件被吞掉，这是正确行为）。
2. **事件驱动采集**。确认日志里有
   `已启用 Windows 剪贴板原生事件监听（窗口句柄 NNN）`。
   若看到的是 `AddClipboardFormatListener 注册失败`，说明退回了轮询 ——
   功能可用但间隔是 1s，需要说明。
3. **来源应用识别**。从 Chrome 复制一段文字，列表里的来源应显示
   `chrome.exe` 而不是「未知来源」。
4. **敏感内容的系统历史**。在密码管理器里复制一个密码，
   按 `Win+V` 看系统剪贴板历史 —— 不应出现该密码。
5. **退出干净**。托盘菜单退出，确认任务管理器里没有残留进程。
   残留通常意味着 `RemoveClipboardFormatListener` 没调到。
6. **打包产物**。`python packaging/build.py` 产出的 `.exe` 双击能起，
   不是只能从源码跑。

### macOS 需要确认的

脚本里的 **macOS 专有** 一节检查 `osascript` 前台应用、`pbpaste`/`pbcopy`、
pyobjc Quartz 能否导入、以及辅助功能权限是否已授予。

手工操作：

1. **辅助功能权限**。首次注册全局快捷键时会弹系统授权引导。
   授权后**需要重启应用**（事件 tap 在进程启动时创建）。
   授权路径：系统设置 → 隐私与安全性 → 辅助功能。
2. **全局快捷键**。在 TextEdit 里按 `Cmd+Shift+V`，
   预期浮层弹出且 TextEdit 无输入。
3. **临时剪贴板**。复制一条敏感记录（开启「复制出站打码」），
   确认内容在预期时间后被清空。**注意**：清理条件是
   「当前剪贴板内容仍是我们写的那份」，你若在这期间复制了别的东西，
   它不会去动它 —— 这是刻意设计，避免误删用户的复制。
4. **来源应用识别**。首次调用 `osascript` 会在
   「隐私与安全性 → 自动化」里请求授权，允许后来源才能正确显示。
5. **打包产物**。`python packaging/build.py` 产出 `.app`，双击能起。
   注意 macOS 会因未签名而拦截，需右键 → 打开。

### 回帖时请附上

```bash
linglong-clipboard --selfcheck --json > selfcheck.json
```

以及第 1、2 项（快捷键 + 事件驱动）的实际结果。有这两项就能定位
绝大多数平台相关问题。

---

## 四、当前状态

| 平台 | 自动化验证 | 真机验证 |
| --- | --- | --- |
| Linux | ✅ 完整（含真实 socket、Qt offscreen） | ✅ 本机 |
| Windows | ✅ 仿真原生层（50 个用例） | ⏳ **待实测** |
| macOS | ✅ 仿真原生层（含假的 Quartz tap） | ⏳ **待实测** |

真机验证未完成的项，README 的「已知限制」里逐条写明，不在别处含糊表述。
