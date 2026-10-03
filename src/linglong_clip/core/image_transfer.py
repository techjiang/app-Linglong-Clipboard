"""图片的按需跨设备传输：只传「这张图在哪」，字节等要用时才去拉。

此前图片完全不参与同步（`kind == "image"` 的记录在导出与局域网两个
路径上都被直接跳过）。理由听起来正当 —— 「体积大、隐私风险高」——
但结果是跨设备同步只完成了一半：

```
在 A 上截图 → 记录同步到 B，B 的列表里有一条 [图片] 但打不开
B 上显示的是一条永远取不到图的记录，而列表看不出它和图没关系
```

剪贴板里截图占很大比例，于是「同步」这件事在最常用的一类内容上失效。

为什么不做成「把字节也塞进同步日志」：

- **同步目录会失控**。图片是剪贴板里最占地方的东西，日志里每条都带
  base64 会让同步目录膨胀到几百 MB，而用户选同步目录时想的是「放个笔记本」。
- **大部分图你根本不会在第二台机器上打开**。同步时全传，是拿带宽和
  磁盘换一个多数时候用不上的保证。
- **隐私面更大**。字节一旦落进同步目录/日志，就多了一份不受本机清理
  影响的副本 —— 而本机清理（淘汰、敏感过期、`--images-gc`）正是
  几轮前刚做完的事。

所以这里做**按需拉取**：

1. 同步记录只多带一个 `image_digest`（图片文件名去掉扩展名）。
   接收方因此知道「这条记录原本有图」，也知道**去哪台设备要**。
2. 用户真正要看那张图时，才向对端发一次 `/image/<digest>` 请求。
3. **接收方自己校验**：文件名就是内容的 blake2b 摘要（见
   `ui/clipboard_qt.py` 的 `_from_image`），所以拿到字节后重算一次
   摘要比对即可。校验不过就丢弃 —— 不信任对端，也不信任链路。
   这一点是内容寻址白送的好处：不需要约定额外的校验字段。

不变量：

- **只有对端确实持有该图时才发**。`/image` 接口要按「这张图有没有被
  一条可外发的记录引用」来判断，而不是「images/ 目录里有没有这个文件」——
  否则就开了一个按 hash 读取任意本地文件的入口。
- **敏感记录的图不发**。判定与记录同步走同一份隐私判定，不另起一套。
- **落盘必须原子且先校验**。半个 PNG 写进 `images/` 会被后续的
  `--images` 统计和界面当成一张真图。
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..utils.logger import get_logger

_log = get_logger("image-transfer")

#: 摘要长度（字节）。与 `clipboard_qt._from_image` 的 blake2b 一致。
DIGEST_SIZE = 16

#: 图片文件名形如 `<64 位十六进制>.png`；摘要本身是 32 位十六进制。
_DIGEST_RE = re.compile(r"^[0-9a-f]{32}$")

#: 单张图的上限。与采集侧的 `MAX_IMAGE_BYTES` 保持一致 —— 对端可能被改过，
#: 不能假定它给的一定是合规大小。
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class ImageTransferError(Exception):
    """图片获取失败。调用方按「这张图暂时取不到」处理，不当作致命错误。"""


def digest_of(data: bytes) -> str:
    """按内容算摘要，与采集侧的命名规则一致。"""
    return hashlib.blake2b(data, digest_size=DIGEST_SIZE).hexdigest()


def is_valid_digest(value: str) -> bool:
    """摘要必须是 32 位小写十六进制。

    这个判断同时是一道**路径安全**闸门：`digest` 会参与拼路径，
    放行 `../` 之类的东西就等于给了对端一个读写任意路径的原语。
    """
    return bool(_DIGEST_RE.match(str(value or "").strip().lower()))


def normalize_digest(value: str) -> str:
    """把记录里的 `image_path` / 文件名 / 摘要本身统一成裸摘要，非法则返回空串。"""
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("data:"):
        # data URI 是 JSON 后端内联的图，字节已在记录里，不需要跨设备取
        return ""
    name = raw.rsplit("/", 1)[-1]
    if name.lower().endswith(".png"):
        name = name[:-4]
    name = name.lower()
    return name if is_valid_digest(name) else ""


def digest_of_item(item) -> str:
    """从一条记录里取它引用的图片摘要。

    **两条路都要看**：本机自己采集的图有 `image_path`（文件名就是摘要），
    而从对端同步来的记录**没有路径**、只有 `metadata["image_digest"]` ——
    那时本机确实还没有那份字节。

    只看 `image_path` 会让同步来的记录被当成「本来就没有图」，
    于是「按需补图」永远找不到要补的东西，而且不报错 —— 第一版就是这么错的。
    """
    from .models import IMAGE_DIGEST_KEY

    direct = normalize_digest(getattr(item, "image_path", "") or "")
    if direct:
        return direct
    metadata = getattr(item, "metadata", None)
    if isinstance(metadata, dict):
        return normalize_digest(metadata.get(IMAGE_DIGEST_KEY, ""))
    return ""


def verify(data: bytes, digest: str) -> bool:
    """校验字节与摘要是否相符（定时安全比较不必要：摘要不是秘密）。"""
    expected = str(digest or "").strip().lower()
    if not is_valid_digest(expected):
        return False
    return digest_of(data) == expected


@dataclass
class StoreOutcome:
    """一次落盘的结果。"""

    stored: bool = False
    path: str = ""
    """落盘后的相对路径（`images/<digest>.png`），与采集侧写法一致。"""

    reason: str = ""
    """未落盘的原因，供日志与 CLI 展示。"""


def store(images_dir: Path, digest: str, data: bytes) -> StoreOutcome:
    """把已校验的图片字节落盘。

    调用方必须已经 `verify()` 过 —— 这里再校验一次是有意的：
    落盘是所有路径的收口，而「忘了先校验」的代价是一张来源不明的图
    进到用户的历史里，且**再也无法察觉**（文件名已经是摘要了，
    看上去完全正常）。多算一次摘要（20MB 约 30ms）换掉这个风险是划算的。

    已存在同名文件时直接返回成功：图片按内容命名，同名即同内容。
    """
    if not is_valid_digest(digest):
        return StoreOutcome(reason="摘要格式非法")
    if not data:
        return StoreOutcome(reason="内容为空")
    if len(data) > MAX_IMAGE_BYTES:
        return StoreOutcome(reason=f"超过上限 {MAX_IMAGE_BYTES} 字节")
    if not verify(data, digest):
        return StoreOutcome(reason="内容与摘要不符")

    name = f"{digest}.png"
    target = Path(images_dir) / name
    if target.exists():
        return StoreOutcome(stored=True, path=f"{Path(images_dir).name}/{name}", reason="已存在")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件 + os.replace：直接 write_bytes 被中断会留下半个 PNG，
        # 而它看上去和一张真图没有区别（文件名就是合法摘要），
        # 于是后续 `--images` 统计、`--images-gc` 的引用集都会把它算成正常图。
        fd, temp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".part")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temp_name, target)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        _log.warning("图片落盘失败 %s: %s", name, exc)
        return StoreOutcome(reason=f"写入失败：{exc}")

    return StoreOutcome(stored=True, path=f"{Path(images_dir).name}/{name}", reason="已写入")


def privacy_inspectable(item) -> bool:
    """这张图的内容有没有被**真正看过一眼**。

    隐私判定读的是 `item.content`，而图片记录的 `content` 是文件名 ——
    也就是说判定对图片**天然无效**。唯一能穿透到图片内容的手段是 OCR：
    识别结果写进 `content`，判定才看得到「截图里写着私钥」。

    于是这里要区分两种「OCR 没给出文字」：

    | 情况 | `metadata["ocr"]` | 含义 |
    | --- | --- | --- |
    | 识别跑了，图里本来没字 | 存在，`text` 为空 | 看过，确实没有可判定的内容 |
    | OCR 没开/没装/失败了 | **不存在** | 没看过，这张图对我们是不透明的 |

    后者不能外发，理由不是「它一定敏感」，而是**我们没有任何依据说它不敏感**：
    报出去的是「这台机器上有一张我没读过的图，谁拿到口令都能取走」。
    实测过这条路径：关掉 OCR 时，一张写着 `BEGIN RSA PRIVATE KEY` 的截图
    会以 `sensitive=False` 入库、进同步、字节可被对端按摘要取走 ——
    界面上一切正常。

    装了 tesseract 时这条限制几乎不生效（识别结果会写明「看过」）；
    它兜的是「没装 OCR 却开了同步」这个组合。
    """
    metadata = getattr(item, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    entry = metadata.get("ocr")
    return isinstance(entry, dict)


def pending_digests(items, images_dir: Path) -> list:
    """挑出「记录说有图、但本机没有那份文件」的摘要。

    这是「按需拉取」要解决的问题清单。刻意按文件名是否存在来判断，
    而不是按记录的 `synced` 标记 —— 用户可能手工删过 `images/`、
    或同步盘冲突丢过文件，那时记录的元数据是完整的、只有字节不在，
    而界面上的表现是「这条图片打不开」。这两种情况处理方式相同，不必分开。
    """
    _ = images_dir  # 目录由调用方保证存在；这里只做纯判断，便于测试
    out: list = []
    seen: set = set()
    for item in items:
        digest = digest_of_item(item)
        if not digest or digest in seen:
            continue
        if (Path(images_dir) / f"{digest}.png").exists():
            continue
        seen.add(digest)
        out.append(digest)
    return out
