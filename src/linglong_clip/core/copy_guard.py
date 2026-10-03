"""出站防护：敏感内容复制到系统剪贴板时的收尾处理。

为什么单独成一个模块：

工具对敏感内容的防护此前全部集中在**入库方向** —— 采集时拦截、不同步、
限时从历史里清除。但真正把密钥带出机器的动作是**复制出站**：
用户点一下「复制」，内容就进了系统剪贴板，之后会流向哪就不由我们了。

系统剪贴板有两个我们控制不了的下游：

1. **系统剪贴板历史**。Windows 10 1809+ 默认开启，复制过的内容会一直留在
   `Win+V` 里，本工具删掉历史也没用 —— 那是系统的另一份副本。
2. **剪贴板本身长期驻留**。复制完密钥去泡杯咖啡再粘贴，中间任何进程
   都能读一次剪贴板。

两件事都只能在**写入时**声明或安排，事后补不回来。所以这里把
「怎么写、写完之后多久收尾」收在一处，UI 与 CLI 走同一条路径。

设计取舍：

- **敏感项一律挂排除标记，不给配置机会**。加了开关，默认关掉的那种配置
  等于没做；而这一步没有副作用，不需要用户权衡。
- **自动清空是「带条件的」**。只在剪贴板内容仍是我们写的那份时才清。
  否则用户复制完密钥马上又复制了别的东西，我们一清反而删掉了用户的内容。
- **`0` 表示不清空，而不是「立刻清空」**。立刻清空会让粘贴必然失败，
  是个没人想要的行为。
- **清空只是「缩小暴露窗口」**，不是擦除痕迹。剪贴板管理器、输入法、
  云同步都可能已经读过一次。这一点写进文档，免得用户把密钥托付给这条路径。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, List

from ..utils.logger import get_logger

_log = get_logger("copy-guard")

#: 自动清空的下限。低于这个值等于「复制完还没粘贴就被清掉」，没有意义。
MIN_AUTO_CLEAR_SECONDS = 5


@dataclass
class OutboundPlan:
    """一次复制出站该怎么做。"""

    #: 是否挂「不进系统剪贴板历史」标记
    exclude_from_history: bool = False
    #: 内容写出去之后，多久清空剪贴板（0 = 不清）
    auto_clear_seconds: int = 0
    #: 采取这些措施的原因，用于界面提示与排查
    reasons: List[str] = field(default_factory=list)

    @property
    def is_protected(self) -> bool:
        return self.exclude_from_history or self.auto_clear_seconds > 0


def plan_outbound(
    *,
    sensitive: bool,
    auto_clear_seconds: int = 0,
    redacted: bool = False,
) -> OutboundPlan:
    """决定一条内容复制出去时要做什么。

    只有敏感项才需要防护：普通内容进了系统历史本来就是用户想要的
    （「刚才复制的那个链接」正是靠系统历史找回来的）。
    """
    if not sensitive:
        return OutboundPlan()

    reasons: List[str] = []
    # 已经脱敏的内容本身不含密钥，仍然挂排除标记 ——
    # 脱敏是正则匹配，漏一处就漏一处，而排除标记是系统级保证，兜底更可靠。
    if redacted:
        reasons.append("已脱敏后复制")
    else:
        reasons.append("敏感内容")

    seconds = max(0, int(auto_clear_seconds or 0))
    if 0 < seconds < MIN_AUTO_CLEAR_SECONDS:
        # 太短会让粘贴必然失败，宁可不做也不做错
        _log.debug("自动清空 %s 秒低于下限，按不清空处理", seconds)
        seconds = 0

    return OutboundPlan(
        exclude_from_history=True,
        auto_clear_seconds=seconds,
        reasons=reasons,
    )


class ClipboardReleaser:
    """安排「复制出去的内容过一会儿自己消失」。

    有原生实现（`set_transient_text`）就用原生的，没有就退回到本地定时器。
    两条路的收尾判据一致：**只有剪贴板内容仍是我们写的那份时才清**。

    为什么不用「无条件清空」：用户复制密钥后马上又复制了一段普通文本，
    无条件清空删掉的是用户的内容，而且他多半不会立刻发现。
    """

    def __init__(self, read_text: Callable[[], str], clear: Callable[[], None]) -> None:
        self._read_text = read_text
        self._clear = clear
        self._timers: List[threading.Timer] = []
        self._lock = threading.Lock()

    def schedule(self, expected_text: str, seconds: int) -> bool:
        """`seconds` 秒后清空剪贴板 —— 前提是它还是 `expected_text`。

        返回是否真的安排了。
        """
        if seconds <= 0:
            return False
        with self._lock:
            # 清掉已结束的定时器，长期运行不能让列表无限增长
            self._timers = [item for item in self._timers if item.is_alive()]
            timer = threading.Timer(seconds, self._expire, args=(expected_text,))
            timer.daemon = True
            self._timers.append(timer)
            timer.start()
        return True

    def _expire(self, expected_text: str) -> None:
        try:
            current = self._read_text()
        except Exception:  # noqa: BLE001 - 读失败就不清，宁可留着
            _log.debug("清空前读取剪贴板失败，跳过本次清理", exc_info=True)
            return
        if current != expected_text:
            _log.debug("剪贴板内容已变化，不做清理")
            return
        try:
            self._clear()
        except Exception:  # noqa: BLE001
            _log.debug("清空剪贴板失败", exc_info=True)

    def pending(self) -> int:
        with self._lock:
            self._timers = [item for item in self._timers if item.is_alive()]
            return len(self._timers)

    def cancel_all(self) -> None:
        """退出时取消全部待清理任务，避免进程退出后定时器还在跑。"""
        with self._lock:
            for timer in self._timers:
                timer.cancel()
            self._timers = []
