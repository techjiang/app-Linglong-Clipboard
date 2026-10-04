"""局域网同步的协议版本协商。

此前 `PROTOCOL_VERSION = "1"` 这个常量出现在四个地方 —— HTTP 的
`Server` 头、`/ping` 的响应、`/records` 的响应、UDP 广播包 —— 而
**没有任何一处会读它**。它是一句印在产品上的版本号，不是一句会参与
判断的话。后果不是「不兼容」，而是**不兼容被伪装成正常**：

```
未来版本的对端（字段名从 `hash`/`content` 换成了别的）
    → 本机解密成功、每行都是合法 JSON、字段读不到
    → 逐条 continue 跳过
    → 导入 0 条，HTTP 200，peer.ok = True，error = ""
界面上写：在线 · future-desktop
本机历史：0 条

用户看到「同步成功了，对端没有东西可同步」，
而不是「这两台机器说的不是同一种话」。
```

反过来也一样：任何一个能连上来的客户端，报 `protocol=99` 或者
压根不报版本，本机都会照样按协议 1 的语义把历史交出去。

这个模块只回答一个问题：**「对端说的版本，我能不能按我的理解读它」**
—— `peer_speaks_our_version()`。答案分三种：说得来、说不来、没报
（老版本）。第三种刻意单独成一档，因为它不该和「说得来」混为一谈，
也不该被当成「说不来」直接拒掉：协议 1 是历史上唯一存在过的版本，
没报版本的对端就是那个版本。

判定**保守**：版本串只认「主.次」形式的纯数字，整数部分相同即兼容
（次版本只增不改语义）。认不出来的一律当「说不来」—— 与其按自己的
心意去读一份不知道是什么的东西，不如明确说分不清。
"""

from __future__ import annotations

from typing import Optional

#: 当前协议版本。改动字段语义（删字段、换字段名、改编码）时必须升主版本号。
#:
#: 为什么是模块级常量而不是 `lan_sync` 里的一个字符串：它要被
#: 服务端、客户端、发现广播、CLI 四处读到，而「四处各持一份」
#: 正是前几轮反复出事的结构（见 `theme_spec.DENSITY_OPTIONS`）。
PROTOCOL_VERSION = "1"


class ProtocolVerdict(str):
    """兼容性判定结果（继承 str 便于直接比较与参与格式化）。"""


#: 说得来
COMPATIBLE = "compatible"
#: 版本号认得出，但主版本不同
INCOMPATIBLE = "incompatible"
#: 对端没报版本 —— 协议 1 时期的老版本，按协议 1 处理
UNREPORTED = "unreported"
#: 报了个认不出来的东西（空串、垃圾、非字符串）
MALFORMED = "malformed"


def parse_version(raw: object) -> Optional[tuple]:
    """把版本串解析成 `(主, 次)` 整数对；认不出来返回 `None`。

    只认 ``"1"`` / ``"1.0"` / ``"2.3"`` 这类纯数字形式，且段数不超过 2。
    `"1.0.0"`、`"v1"`、`""`、`None`、数字 `1` 都算认不出来 ——
    这一层**故意窄**：宽进会让「说不来」的东西被读成「说得来」，
    而这正是要防的那件事。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    parts = text.split(".")
    if len(parts) > 2:
        return None
    numbers = []
    for part in parts:
        if not part.isdigit():  # isdigit 认全角与上标数字，再挡一层
            return None
        if not part.isascii():
            return None
        numbers.append(int(part))
    if not numbers:
        return None
    if len(numbers) == 1:
        numbers.append(0)
    return numbers[0], numbers[1]


def _major_of(raw: object) -> Optional[int]:
    parsed = parse_version(raw)
    return None if parsed is None else parsed[0]


def peer_speaks_our_version(raw: object) -> ProtocolVerdict:
    """对本机自报的协议版本，做出兼容性判定。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return UNREPORTED
    mine = _major_of(PROTOCOL_VERSION)
    theirs = _major_of(raw)
    if theirs is None:
        return MALFORMED
    return COMPATIBLE if theirs == mine else INCOMPATIBLE


def is_usable(verdict: str) -> bool:
    """这个判定下，能不能按本机的语义去读对端的数据。

    `UNREPORTED` 算能用：协议 1 是历史上唯一存在过的版本，不报版本的
    就是它。`MALFORMED` 不算 —— 我们不知道那是什么，而按自己的心意
    去读一份不知道是什么的数据，正是这个模块要消灭的事。
    """
    return verdict in (COMPATIBLE, UNREPORTED)


def describe(raw: object, verdict: Optional[str] = None) -> str:
    """一句能直接印给人看的话。

    用户看到的必须是「哪台机器、什么版本、差在哪、该干什么」，
    而不是 `HTTP 400` 或「同步了，但一条都没有」。
    """
    verdict = verdict or peer_speaks_our_version(raw)
    shown = f"{raw!r}" if isinstance(raw, str) else str(raw)
    if verdict == COMPATIBLE:
        return f"协议 {raw}（本机 {PROTOCOL_VERSION}）"
    if verdict == UNREPORTED:
        return f"对端未报协议版本，按协议 {PROTOCOL_VERSION} 处理"
    if verdict == INCOMPATIBLE:
        return (
            f"对端说的是协议 {raw}，本机是 {PROTOCOL_VERSION} —— 版本不一致，"
            "无法按本机的语义解读对端数据。请把两台机器升到同一版本"
        )
    return (
        f"对端自报的协议版本无法识别（{shown}）—— 无法确认双方说的是同一种话，"
        f"本机是 {PROTOCOL_VERSION}"
    )


def advertised_payload() -> dict:
    """塞进 `/ping` 与 UDP 广播包的那几个字段。

    两处**同一份**：广播与 `/ping` 报的版本必须是同一个数，
    否则「广播说协议 2、握手说协议 1」这种自相矛盾无从排查。
    """
    return {"protocol": PROTOCOL_VERSION}
