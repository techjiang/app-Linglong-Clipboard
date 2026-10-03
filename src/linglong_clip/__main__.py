"""命令行入口。

GUI 之外保留纯 CLI 能力（`--list`、`--search`、`--get`）是有意为之：
在服务器、SSH 会话或排查问题时，不启动图形界面也能检查历史。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linglong-clipboard",
        description="玲珑剪切板 —— 跨平台智能剪贴板",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  linglong-clipboard                  启动图形界面\n"
            "  linglong-clipboard --quick          唤起快速粘贴浮层\n"
            "  linglong-clipboard --monitor-primary  选中文字即记录（Linux）\n"
            "  linglong-clipboard --list -n 20     列出最近 20 条\n"
            "  linglong-clipboard --search '上周的截图'   # 扫全库，末尾会报扫描量\n"
            "  linglong-clipboard --search '部署' --json  # 带 scanned/backend，便于排查搜不到\n"
            "  linglong-clipboard --get 12         输出第 12 条内容\n"
            "  linglong-clipboard --stats          查看统计与检索能力\n"
            "  linglong-clipboard --import-native-history  导入系统剪贴板历史\n"
            "  linglong-clipboard --selfcheck      自检原生能力是否真的可用\n"
            "  linglong-clipboard --sync-status    查看同步模式与对端状态\n"
            "  linglong-clipboard --sync-cursor    查看各同步来源的增量游标\n"
            "  linglong-clipboard --storage-status 查看历史文件的存放位置\n"
            "  linglong-clipboard --merge-duplicates 合并重复项（含归一后新出现的）\n"
            "  linglong-clipboard --outbound       复制敏感内容时实际生效的防护\n"
            "  linglong-clipboard --privacy-audit  隐私体检：脱敏规则哪些真的会生效\n"
            "  linglong-clipboard --redact 'token=abc12345'  试跑脱敏，看会被替换成什么\n"
            "  linglong-clipboard --remove 12      删除第 12 条（删除会同步到其它设备）\n"
            "  linglong-clipboard --tombstones     查看删除标记的同步进度\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"玲珑剪切板 {__version__}")

    mode = parser.add_argument_group("运行模式")
    mode.add_argument("--quick", action="store_true", help="启动后直接唤起快速粘贴浮层")
    mode.add_argument("--minimized", action="store_true", help="启动后最小化到系统托盘")
    mode.add_argument(
        "--monitor-primary",
        action="store_true",
        help="启动时开启主选择区采集（选中即记录，Linux X11/Wayland）",
    )

    query = parser.add_argument_group("查询")
    query.add_argument("--list", action="store_true", help="列出历史记录")
    query.add_argument(
        "--search",
        metavar="QUERY",
        help="检索历史，支持自然语言（如「上周来自 Chrome 的链接」）",
    )
    query.add_argument("--get", type=int, metavar="ID", help="输出指定 ID 的完整内容")
    query.add_argument(
        "--form-key",
        metavar="KEY",
        help=(
            "配合 --get：取用这条记录的某种形式而不是默认那份。"
            "可选 text / html / markdown / rtf / urilist / shell / json / fenced"
        ),
    )
    query.add_argument(
        "--kind",
        choices=["text", "rich", "image", "file", "link", "color", "code", "sensitive"],
        help="按类型过滤",
    )
    query.add_argument(
        "-n", "--limit", type=int, default=20,
        help="查询返回条数上限（默认 20）；--export-sync 时作为每页扫描批量，不影响导出总量",
    )

    maintenance = parser.add_argument_group("维护")
    maintenance.add_argument("--stats", action="store_true", help="显示统计信息")
    maintenance.add_argument("--clear", action="store_true", help="清空未固定记录")
    maintenance.add_argument("--compact", action="store_true", help="压缩数据库")
    maintenance.add_argument(
        "--merge-duplicates",
        action="store_true",
        help="合并重复内容（保留复制次数，不会丢使用频次）",
    )
    maintenance.add_argument(
        "--recanonicalize",
        action="store_true",
        help="按归一规则重算历史指纹（不动数据，只让老记录能被正确判重）",
    )
    maintenance.add_argument(
        "--formats",
        nargs="?",
        const="",
        metavar="ID",
        help=(
            "看某条记录在剪贴板里有哪些形式、各自能不能取用；"
            "不带 ID 时统计整个历史"
        ),
    )
    maintenance.add_argument(
        "--normalize",
        metavar="TEXT",
        help="预览归一结果：这段内容会被当成哪一条（用于排查「为什么没合并」）",
    )
    maintenance.add_argument(
        "--outbound",
        action="store_true",
        help="查看「复制敏感内容出去」时实际生效的防护（写入级系统排除、限时清空）",
    )
    maintenance.add_argument("--import-sync", action="store_true", help="从同步来源导入")
    maintenance.add_argument("--export-sync", action="store_true", help="导出到同步来源")
    maintenance.add_argument("--sync-status", action="store_true", help="查看同步模式与对端状态")
    maintenance.add_argument(
        "--import-native-history",
        action="store_true",
        help="导入系统剪贴板历史（Windows 10 1809+，补齐本工具启动前的记录）",
    )
    maintenance.add_argument(
        "--sync-cursor",
        action="store_true",
        help="查看各同步来源的增量游标（已同步到哪个时间点）",
    )
    maintenance.add_argument(
        "--sync-cursor-reset",
        action="store_true",
        help="清空同步增量游标（下次同步回到全量重扫）",
    )
    maintenance.add_argument(
        "--tombstones",
        action="store_true",
        help="查看删除标记（哪些删除还没同步到对端）",
    )
    maintenance.add_argument(
        "--tombstones-reset",
        action="store_true",
        help="清空删除标记（之后的历史删除不再向对端传播）",
    )
    maintenance.add_argument(
        "--purge-tombstones",
        action="store_true",
        help="清除超过保留期的删除标记",
    )
    maintenance.add_argument(
        "--flag-changes",
        action="store_true",
        help="查看标记变更（取消固定/收藏）的同步进度",
    )
    maintenance.add_argument(
        "--flag-changes-reset",
        action="store_true",
        help="清空标记变更（之后的历史取消不再向对端传播）",
    )
    maintenance.add_argument(
        "--remove",
        type=int,
        metavar="ID",
        help="删除一条记录，并让这次删除同步到其它设备",
    )

    images_group = parser.add_argument_group("图片附件")
    images_group.add_argument(
        "--images",
        action="store_true",
        help="历史里的图片占用多少磁盘、有多少张没被任何记录引用",
    )
    images_group.add_argument(
        "--images-gc",
        action="store_true",
        help="回收 images/ 里不再被记录引用的图片文件（配 --dry-run 只报告不删）",
    )
    images_group.add_argument(
        "--dry-run",
        action="store_true",
        help="配 --images-gc 使用：只列出会删哪些文件，不真删",
    )
    images_group.add_argument(
        "--images-pending",
        action="store_true",
        help="列出「同步过来的记录说有图、但本机还没有那份文件」的图片（从对端按需取）",
    )
    images_group.add_argument(
        "--sync-images",
        action="store_true",
        help="向局域网对端按需拉取缺失的图片（图片不进同步日志，要用时才取）",
    )
    images_group.add_argument(
        "--images-limit",
        type=int,
        metavar="N",
        default=20,
        help="配 --sync-images：单次最多补几张（默认 20，避免一次拉几千张卡住界面）",
    )

    storage_group = parser.add_argument_group("历史存储")
    storage_group.add_argument(
        "--storage-status",
        action="store_true",
        help="显示当前历史存到哪里（SQLite 或 JSON 单文件）",
    )
    storage_group.add_argument(
        "--storage",
        choices=["sqlite", "json"],
        help="切换历史存储方式：sqlite（默认）或 json（<数据目录>/.clipboard/clipboard.json）",
    )
    storage_group.add_argument(
        "--storage-migrate",
        action="store_true",
        help="把当前存储里的记录复制到另一种存储方式（不删除原数据）",
    )
    storage_group.add_argument(
        "--where",
        action="store_true",
        help="只输出数据目录路径，便于脚本取用",
    )

    diagnose = parser.add_argument_group("诊断")
    diagnose.add_argument(
        "--selfcheck",
        action="store_true",
        help="自检原生能力（剪贴板读写、前台窗口、全局快捷键），换机器排查时先跑这个",
    )
    diagnose.add_argument(
        "--config-audit",
        action="store_true",
        help="审计配置项：找出「声明了但没有读取方」的空转配置（改了不生效的那些）",
    )
    diagnose.add_argument(
        "--ui-status",
        action="store_true",
        help="看界面配置的实际口径（如列表密度：配置值、实际使用、可选档位）",
    )
    diagnose.add_argument(
        "--rules",
        action="store_true",
        help="列出自动化规则及其触发时机，并标出不会生效的那些",
    )
    diagnose.add_argument(
        "--privacy-audit",
        action="store_true",
        help="隐私体检：列出脱敏规则并标出写错/不命中的那些（含自定义规则）",
    )
    diagnose.add_argument(
        "--redact",
        metavar="TEXT",
        help="拿一段真实内容试跑脱敏规则，看会被替换成什么（不写历史、不碰剪贴板）",
    )

    output = parser.add_argument_group("输出")
    output.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本处理")
    output.add_argument("--plain", action="store_true", help="只输出内容，不带格式")
    return parser


def format_import_summary(outcome, *, source: str = "") -> str:
    """把一次同步导入的结果写成一句话。

    刻意把「只更新了标记」单独说出来：此前两种情况的输出是一模一样的
    「导入 0 条」，用户无从判断到底是「没有新东西」还是「对端带来的
    固定/收藏已经合并进来了」。这两件事对用户意味着完全不同的结果。

    `source` 用来说明记录从哪来（「局域网对端」/「同步目录」）—— 这句话
    出现在用户排查「同步怎么没生效」时最先看到的地方，省掉猜测。
    """
    where = source or "同步对端"
    parts = [f"已从{where}导入 {outcome.imported} 条记录"]
    if outcome.flags_updated:
        parts.append(f"另有 {outcome.flags_updated} 条只更新了固定/收藏标记")
    if outcome.removed:
        parts.append(f"按对端删除标记清掉 {outcome.removed} 条")
    return "，".join(parts)


def _load_storage(config=None):
    """按配置打开历史仓库（SQLite 或 JSON 单文件）。"""
    from .core.repository import open_repository
    from .utils.config import Config

    return open_repository(config or Config())


def _apply_storage_switch(args, config) -> int:
    """处理 `--storage` / `--storage-migrate` / `--where` / `--storage-status`。"""
    from .utils.paths import data_dir, database_path, json_path

    if args.where:
        print(data_dir())
        return 0

    # 搬运的源是「配置里原本的后端」，目标是切换后的那个，
    # 所以这里必须在 `--storage` 生效前先记下来。
    previous_backend = str(config.get("storage.backend", "sqlite") or "sqlite").lower()
    if args.storage:
        if args.storage == previous_backend:
            print(f"当前已经是 {args.storage} 存储，无需切换")
        else:
            config.set("storage.backend", args.storage)

    if args.storage_migrate:
        return _migrate_storage(previous_backend=previous_backend, config=config)

    if args.storage_status:
        from .core import storage_files

        backend = str(config.get("storage.backend", "sqlite") or "sqlite")
        target = json_path() if backend == "json" else database_path()
        # SQLite 的历史**不是单个文件**：WAL 模式下 `-wal` 里装着尚未并入主库
        # 的全部改动，`-shm` 在进程没退出时存在。只报主文件会让同一个数字
        # 在 `--stats` 与 `du` 之间差一倍，用户无从判断该不该清理历史。
        footprint = storage_files.measure(target)
        payload = {
            "backend": backend,
            "path": str(target),
            "exists": target.exists(),
            "size": footprint.total_bytes,
            "main_size": footprint.main_bytes,
            "companion_size": footprint.companion_bytes,
            "files": [entry.as_dict() for entry in footprint.files],
            "sqlite_path": str(database_path()),
            "json_path": str(json_path()),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        label = "JSON 单文件" if backend == "json" else "SQLite 数据库"
        print(f"存储方式：{label}")
        print(f"历史文件：{target}{'' if target.exists() else '（尚未创建）'}")
        if footprint.exists:
            print(f"文件大小：{storage_files.format_bytes(footprint.total_bytes)}")
            if footprint.companion_bytes:
                # 逐个数出来，用户才能把「这个 -wal 是谁建的」对上号
                names = "、".join(
                    entry.name for entry in footprint.files if entry.companion
                )
                print(
                    f"          其中 {storage_files.format_bytes(footprint.companion_bytes)}"
                    f" 在 {names}（尚未并入主库的改动，启动时会自动合并）"
                )
        other = database_path() if backend == "json" else json_path()
        if other.exists():
            print(f"另一种存储也有数据：{other}（可用 --storage-migrate 合并）")
        return 0

    if args.storage:
        backend = str(config.get("storage.backend", "sqlite") or "sqlite")
        print(f"已切换为 {backend} 存储（下次启动生效）")
        return 0

    return -1


def _migrate_storage(*, previous_backend: str, config) -> int:
    """把记录从「原本的后端」复制到「配置里现在的后端」，原数据保留以便回退。

    这样 `--storage json --storage-migrate` 一条命令就能「切过去 + 搬过去」。
    """
    from .core.repository import JsonRepository
    from .core.storage import Storage
    from .utils.paths import database_path, json_path

    # 重新读一次配置：调用方可能刚用 `--storage` 改过，必须拿落盘后的新值
    config.load()
    to_backend = str(config.get("storage.backend", "sqlite") or "sqlite").lower()
    if previous_backend == to_backend:
        print(f"源与目标都是 {previous_backend} storage，无需搬运（用 --storage 指定要切到哪种）")
        return 0

    source_is_json = previous_backend == "json"
    source = JsonRepository(json_path()) if source_is_json else Storage(database_path())
    target = Storage(database_path()) if source_is_json else JsonRepository(json_path())

    items = source.list(limit=100_000)
    known = {item.hash for item in target.list(limit=100_000) if item.hash}
    copied = 0
    for item in items:
        if item.hash and item.hash in known:
            continue
        item.id = None
        target.add(item)
        copied += 1
    source.close()
    target.close()

    direction = "JSON -> SQLite" if source_is_json else "SQLite -> JSON"
    print(f"已从 {direction} 复制 {copied} 条记录（共 {len(items)} 条，原数据保留）")
    return 0


def _canonical_options(config) -> dict:
    """把 `history.canonical_dedupe` 配置翻译成归一模块认识的参数名。

    单独抽出来是为了让 CLI 与采集路径读的是同一份配置，
    避免「CLI 合并时按新规则、采集时按旧规则」这种两边不一致的状态。
    """
    enabled = config.get("history.canonical_dedupe", True)
    if not enabled:
        # 传 False 给两个开关，等价于完全不做归一
        return {"url_strip_tracking": False, "path_normalize": False}
    return {
        "url_strip_tracking": bool(config.get("history.canonical_dedupe.url", True)),
        "path_normalize": bool(config.get("history.canonical_dedupe.path", True)),
    }


def _print_items(items, *, as_json: bool, plain: bool, limit: int) -> None:
    if as_json:
        payload = [
            {
                "id": item.id,
                "kind": item.kind,
                "preview": item.preview,
                "content": item.content,
                "source_app": item.source_app,
                "created_at": item.created_at,
                "pinned": item.pinned,
                "favorite": item.favorite,
                "tags": item.tags,
                "size": item.size,
                "use_count": item.use_count,
            }
            for item in items[:limit]
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if plain:
        for item in items[:limit]:
            print(item.content)
        return

    if not items:
        print("没有匹配的记录")
        return

    import time

    for item in items[:limit]:
        stamp = time.strftime("%m-%d %H:%M", time.localtime(item.created_at))
        markers = "".join(
            [
                "P" if item.pinned else " ",
                "F" if item.favorite else " ",
                "!" if item.sensitive else " ",
            ]
        )
        preview = item.preview.replace("\n", " ")[:70]
        # 合并重复项后「复制过几次」是判断片段重要性的唯一线索，值得露出来
        count = f"x{item.use_count:<3}" if item.use_count > 1 else "    "
        print(
            f"{str(item.id):>5}  [{markers}] {stamp}  {item.kind_label:<4} "
            f"{count} {preview}"
        )


def _footprint_line(stats: dict) -> str:
    """一行说清「这份历史真实占了磁盘多少」。

    主角是**总占用**，不是主文件大小 —— 只报主文件会少报 WAL 那一大截，
    而用户正是拿这个数字决定要不要清理历史。
    """
    from .core import storage_files

    info = stats.get("storage_files") or {}
    total = int(info.get("bytes", stats.get("db_size") or 0))
    companion = int(info.get("companion_bytes") or 0)
    if not info.get("exists"):
        return f"{storage_files.format_bytes(total)}（尚未创建）"
    if companion:
        return (
            f"{storage_files.format_bytes(total)}"
            f"（主库 {storage_files.format_bytes(int(info.get('main_bytes') or 0))}"
            f" + 待合并 {storage_files.format_bytes(companion)}）"
        )
    return storage_files.format_bytes(total)


def _footprint_details(stats: dict) -> list:
    """占用里「哪些是能收回的」那几行。没有可说的就返回空列表。

    两种「能收回」要分开讲，因为处理方式不同：

    - WAL 里有改动没并进主库 → 跑一次自动合并即可，不需要 `VACUUM`
    - 主库本身有空洞（删过大量记录）→ 需要 `VACUUM` 重建
    """
    from .core import storage_files

    info = stats.get("storage_files") or {}
    if not info.get("exists"):
        return []
    lines = []
    companion = int(info.get("companion_bytes") or 0)
    main_bytes = int(info.get("main_bytes") or 0)
    # 几百字节的 shm 是恒有的，小到可以忽略时报出来只会让人疑神疑鬼
    if companion > max(64 * 1024, main_bytes * 0.05):
        lines.append(
            f"          其中 {storage_files.format_bytes(companion)} 是尚未并入主库的改动"
            "（下次启动或 --compact 会自动合并）"
        )
    holes = int(info.get("reclaimable_bytes") or 0)
    if holes > 0:
        lines.append(
            f"          另有约 {storage_files.format_bytes(holes)} 是删过记录留下的空洞"
            "，可用 --compact 收回"
        )
    return lines


def _image_status(storage) -> dict:
    """图片附录的占用与垃圾量，供 `--stats` / `--images` 输出。

    `unreferenced` 是「目录里有、但没有任何记录提到」的文件 —— 它就是
    磁盘只涨不缩的来源。`missing` 相反，是记录提到了、文件却不在，
    属于数据损坏，两者必须分开说：一个要清，一个要找回来。
    """
    empty = {
        "dir": "",
        "files": 0,
        "bytes": 0,
        "unreferenced": 0,
        "unreferenced_bytes": 0,
        "missing": 0,
    }
    usage_fn = getattr(storage, "image_usage", None)
    if usage_fn is None:
        return empty
    try:
        usage = usage_fn()
    except Exception:  # noqa: BLE001 - 统计失败不该拖垮整个命令
        return empty

    import os

    dir_path = getattr(storage, "images_dir", None)
    referenced = storage.referenced_images()
    unreferenced = 0
    unreferenced_bytes = 0
    if dir_path is not None:
        from .core import image_store

        for path in image_store.iter_image_files(dir_path):
            if path.name in referenced:
                continue
            unreferenced += 1
            try:
                unreferenced_bytes += path.stat().st_size
            except OSError:
                pass

    return {
        "dir": str(dir_path) if dir_path is not None else "",
        "files": usage.files,
        "bytes": usage.bytes,
        "unreferenced": unreferenced,
        "unreferenced_bytes": unreferenced_bytes,
        "missing": usage.missing,
    }


def _run_image_sync(args, storage, config) -> int:
    """`--images-pending` 与 `--sync-images` 的实现。

    两件事分开是有意的：**看清单**和**去拉取**是不同性质的动作用户要能分开。
    前者只读本地记录，任何模式下都能看；后者要连对端，只有局域网模式能做到。
    """
    from .core import image_transfer
    from .core.lan_sync import build_lan_sync
    from .core.privacy import PrivacyGuard

    images_dir = storage.images_dir
    wanted = image_transfer.pending_digests(storage.iter_all(batch=512), images_dir)

    if args.images_pending and not args.sync_images:
        if args.json:
            print(json.dumps({"pending": wanted, "count": len(wanted)}, ensure_ascii=False, indent=2))
            return 0
        if not wanted:
            print("没有缺失的图片：同步过来的记录里每一张图本机都有")
            return 0
        print(f"缺失 {len(wanted)} 张图片（记录说有图，但本机没有那份文件）：")
        # 只列前 20 个：这是给人看的清单，不是导出的数据。
        for digest in wanted[:20]:
            print(f"  {digest}")
        if len(wanted) > 20:
            print(f"  …… 还有 {len(wanted) - 20} 张")
        print("\n  linglong-clipboard --sync-images   # 向局域网对端按需取回")
        return 0

    # --sync-images
    mode = str(config.get("sync.mode", "folder") or "folder").lower()
    if mode != "lan":
        # 共享文件夹模式**没有请求通道**：对端只是另一个目录，
        # 字节不在里面就无从索取。这不是没做，是模式本身不具备这个能力，
        # 所以要明确说出来，而不是给一个「已尝试」的空结果。
        print(
            f"当前同步模式是 {mode}，没有可用的取图通道。\n"
            "  图片字节不进同步日志，所以共享文件夹模式取不回图。\n"
            "  要按需补图请把 sync.mode 设为 lan，并配置至少一个对端。",
            file=sys.stderr,
        )
        return 2

    # 带上隐私判定：构造 `LanSync` 时会一并起本机的服务端，
    # 而服务端的 `/image` 要靠它决定「这张图该不该外发」。
    # 不传的话，本机服务端在这条命令的进程里会对任何引用它的记录放行。
    lan = build_lan_sync(storage, config, guard=PrivacyGuard(config))
    if lan is None:
        print("局域网同步未启用或配置不完整，无法取图", file=sys.stderr)
        return 2

    limit = max(1, int(getattr(args, "images_limit", 20) or 20))
    try:
        outcome = lan.fetch_missing_images(limit=limit)
    finally:
        lan.stop()

    payload = outcome.as_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if not outcome.missing:
        print("没有缺失的图片：同步过来的记录里每一张图本机都有")
        return 0
    print(f"缺失 {outcome.missing} 张，本次补回 {outcome.fetched} 张（影响 {outcome.adopted} 条记录）")
    if outcome.orphaned:
        # 字节拿到了但没人引用 —— 多半是那条记录已经被删/淘汰了。
        # 单独报出来，否则「补了 3 张」而界面上一条都没变，看着像坏掉。
        print(f"  其中 {outcome.orphaned} 张取回时已无记录引用（记录可能已删除）")
    if outcome.failed:
        print(f"  {outcome.failed} 张没取到：对端都没有这张图，或口令不匹配")
    if outcome.truncated:
        print(f"  达到单次上限（{limit} 张）提前停下，再跑一次继续")
    return 0


def _run_images(args, storage) -> int:
    """`--images` 与 `--images-gc` 的实现。"""
    from .core import image_store

    if args.images_gc:
        dry_run = bool(args.dry_run)
        # 上限调大一些：`--images-gc` 是用户显式要求回收，不是后台顺手清一下，
        # 卡在默认 2000 个文件会让「清干净」变成「清了两次」。
        result = storage.sweep_images(dry_run=dry_run, limit=200000)
        payload = result.as_dict()
        payload["names"] = result.names
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        verb = "将回收" if dry_run else "已回收"
        if not result.removed and not result.errors:
            print("没有可回收的图片：images/ 里每个文件都有记录在引用")
            return 0
        print(
            f"{verb} {result.removed} 张图片"
            f"（{result.bytes / 1024 / 1024:.1f} MB），"
            f"保留 {result.kept} 张仍被引用的"
        )
        if dry_run and result.names:
            print("将被删除的文件（最多列 50 个）：")
            for name in result.names:
                print(f"  {name}")
            print("这是预演，什么都没有删。确认后去掉 --dry-run 再跑一次。")
        if result.errors:
            print(f"有 {result.errors} 个文件删除失败（已跳过，下次会重试）")
        if result.truncated:
            print("达到单次上限提前停下，再跑一次可以继续。")
        return 0

    info = _image_status(storage)
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    if not info["files"] and not info["missing"]:
        print(f"没有图片附件（目录：{info['dir']}）")
        return 0
    print(f"图片目录：{info['dir']}")
    print(f"共 {info['files']} 张，占用 {info['bytes'] / 1024 / 1024:.1f} MB")
    if info["unreferenced"]:
        print(
            f"其中 {info['unreferenced']} 张未被任何记录引用"
            f"（{info['unreferenced_bytes'] / 1024 / 1024:.1f} MB）"
        )
        print("  linglong-clipboard --images-gc --dry-run   # 先看会删哪些")
        print("  linglong-clipboard --images-gc             # 真的回收")
    else:
        print("每张图都有记录在引用，没有可回收的")
    if info["missing"]:
        print(
            f"另有 {info['missing']} 条记录引用的图片已不在磁盘上"
            "（图片被手工删过或同步盘冲突），这些记录打不开原图"
        )
    return 0


def _primary_status(config) -> dict:
    """主选择区（Linux PRIMARY）当前状态，供 `--stats` 与 JSON 输出使用。"""
    enabled = bool(config.get("capture.monitor_primary", False))
    from .platform import detect_backend

    try:
        backend = detect_backend()
        caps = backend.capabilities()
        supported = bool(caps.get("primary_selection", False))
        backend_name = getattr(backend, "name", "unknown")
    except Exception as exc:  # noqa: BLE001
        return {
            "enabled": enabled,
            "supported": False,
            "backend": "unknown",
            "detail": f"探测失败：{exc!r}",
            "warning": "无法确认本机是否支持主选择区" if enabled else "",
        }

    if supported:
        detail = f"可用 · 后端 {backend_name}"
    else:
        detail = "本机无可用通道（X11 需 python-xlib 或 xclip/xsel；Wayland 需 wl-clipboard）"

    warning = ""
    if enabled and not supported:
        warning = "已开启但本机读不到，选中文字不会进历史"
    return {
        "enabled": enabled,
        "supported": supported,
        "backend": backend_name,
        "detail": detail,
        "warning": warning,
    }


def _run_form_key(args: argparse.Namespace, storage, item) -> int:
    """`--get N --form-key KEY`：按指定形式取出来，直接打到 stdout。

    存在的理由和 `--normalize` 一样：这条路径上的问题几乎都是「取出来的东西
    和预期不一样」，而它没法靠读代码确认 —— 打开某个终端的粘贴行为、
    看 CRLF 有没有被改、看 HTML 里有没有链接，都得真跑一次。
    往 stdout 打是为了能直接管道给别的命令：

        linglong-clipboard --get 12 --form-key html > page.html
    """
    from .core.clipboard_formats import (
        ClipboardFormats,
        FORMATS_KEY,
        FORMAT_ACTIONS,
        resolve_action,
    )

    formats = ClipboardFormats.from_metadata(item.metadata.get(FORMATS_KEY))
    text, error = resolve_action(
        args.form_key,
        kind=item.kind,
        content=item.content or "",
        formats=formats,
        files=item.files,
    )
    storage.close()
    if error:
        known = "、".join(action.key for action in FORMAT_ACTIONS)
        print(error, file=sys.stderr)
        print(f"可选形式：{known}", file=sys.stderr)
        return 1
    print(text)
    return 0


def _run_formats(args: argparse.Namespace, storage) -> int:
    """`--formats [ID]`：这条记录（或整个历史）有哪些形式、哪些能取用。

    回答的是「我为什么取不到 HTML」这类问题 —— 判定依据是**存下来的形式**，
    而它是否存在取决于采集那一刻系统给了什么，事后无从推断。
    """
    from .core.clipboard_formats import (
        FORMATS_KEY,
        ClipboardFormats,
        format_summary,
        resolve_action,
    )

    def _describe(item) -> dict:
        formats = ClipboardFormats.from_metadata(item.metadata.get(FORMATS_KEY))
        stored = sorted(formats.data)
        extra = sorted(formats.names)
        usable = []
        for key in ("text", "html", "markdown", "rtf", "urilist", "shell", "json", "fenced"):
            text, error = resolve_action(
                key,
                kind=item.kind,
                content=item.content or "",
                formats=formats,
                files=item.files,
            )
            if not error:
                usable.append({"key": key, "chars": len(text)})
        return {
            "id": item.id,
            "kind": item.kind,
            "stored": stored,
            "names_only": extra,
            "summary": format_summary(formats, item.content or "", files=item.files),
            "usable": usable,
        }

    if args.formats:
        try:
            item_id = int(args.formats)
        except ValueError:
            print(f"ID 必须是数字：{args.formats}", file=sys.stderr)
            storage.close()
            return 1
        item = storage.get(item_id)
        if item is None:
            print(f"找不到 ID 为 {item_id} 的记录", file=sys.stderr)
            storage.close()
            return 1
        rows = [_describe(item)]
    else:
        rows = [_describe(item) for item in storage.list(limit=args.limit)]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        storage.close()
        return 0

    with_formats = [row for row in rows if row["stored"] or row["names_only"]]
    print(f"扫描 {len(rows)} 条记录，其中 {len(with_formats)} 条带额外形式")
    for row in with_formats:
        print(f"#{row['id']}  {row['kind']}  {row['summary'] or '（无差异）'}")
        if row["stored"]:
            print(f"    已存：{'、'.join(row['stored'])}")
        if row["names_only"]:
            print(f"    仅留名：{'、'.join(row['names_only'])}")
        print(f"    可取用：{'、'.join(entry['key'] for entry in row['usable'])}")
    if not with_formats:
        print(
            "（没有记录带额外形式。历史里的记录若是在升级前采集的，"
            "metadata 中没有 formats 字段，复制出站时只能给出纯文本）"
        )
    storage.close()
    return 0


def _run_redact(args: argparse.Namespace, config) -> int:
    """拿一段真实内容试跑脱敏规则。

    存在的理由和 `--outbound` 一样：这件事没法靠读代码确认。用户加的
    自定义正则写错一个字符，`PrivacyGuard` 只会静默跳过它 —— 复制出去的
    密钥原样带走，界面上看不出任何异常。所以给一条能当场验的路径，
    输入什么、出来什么一目了然，且**不写历史、不碰系统剪贴板**。
    """
    from .core.privacy import PrivacyGuard

    guard = PrivacyGuard(config)
    text = args.redact
    hits = guard.scan(text)
    redacted = guard.redact(text)

    if args.json:
        print(
            json.dumps(
                {
                    "input": text,
                    "output": redacted,
                    "changed": redacted != text,
                    "hits": hits,
                    "redact_on_copy_out": bool(
                        config.get("privacy.redact_on_copy_out", False)
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.plain:
        print(redacted)
        return 0

    print(f"输入：{text}")
    print(f"输出：{redacted}")
    print()
    if hits:
        print("命中规则：" + "、".join(f"{name}×{count}" for name, count in hits.items()))
    else:
        print("命中规则：无 —— 这段内容不会被脱敏")
    if not config.get("privacy.redact_on_copy_out", False):
        print()
        print("注意：「复制到外部时脱敏」当前是关闭的，上面的输出只是「如果打开会怎样」。")
        print("      在设置 → 隐私 里打开后才会真的生效。")
    return 0


def _run_outbound(args: argparse.Namespace, config) -> int:
    """报出「复制敏感内容出去」时实际生效的防护。

    之所以要有这条命令：这件事没法靠读代码确认。README 曾写着
    「写入时附加 ExcludeClipboardContentFromMonitorProcessing」，而
    那个方法从来没有调用方，设置页里负责时长的勾选也没人读 ——
    用户看到的是「已设置」，实际什么都没发生。让程序自己报最直接。
    """
    from .platform import detect_backend

    backend = detect_backend()
    formats: List[str] = []
    probe_error = ""
    getter = getattr(backend, "history_exclusion_formats", None)
    if getter is not None:
        try:
            formats = [str(item) for item in (getter() or []) if item]
        except Exception as exc:  # noqa: BLE001 - 探测失败也要给出其余结论
            probe_error = repr(exc)

    seconds = int(config.get("privacy.sensitive_auto_clear_seconds", 0) or 0)
    redact = bool(config.get("privacy.redact_on_copy_out", False))
    name = getattr(backend, "name", "unknown")

    if args.json:
        print(
            json.dumps(
                {
                    "platform": backend.platform_label(),
                    "backend": name,
                    "history_exclusion_formats": formats,
                    "auto_clear_seconds": seconds,
                    "redact_on_copy_out": redact,
                    "probe_error": probe_error,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(f"平台：{backend.platform_label()} · 后端：{name}")
    if probe_error:
        print(f"  写入级系统排除：探测失败（{probe_error}）")
    elif formats:
        print(f"  写入级系统排除：{'、'.join(formats)}")
        print("      复制敏感内容时会声明「不进系统剪贴板历史」，粘贴不受影响")
    else:
        print("  写入级系统排除：本平台无此机制")
        print("      敏感内容仍会被系统剪贴板历史（如 Win+V）记一份")
    if seconds > 0:
        print(f"  限时清空：{seconds} 秒后清空剪贴板（内容未被替换时）")
    else:
        print("  限时清空：关闭")
        print("      可在设置 → 隐私 里设定；低于 5 秒按关闭处理")
    print(f"  出站脱敏：{'已开启（敏感片段替换为占位符）' if redact else '关闭'}")
    print()
    print("注意：这些手段只是缩小暴露窗口，不是擦除痕迹 ——")
    print("      剪贴板管理器、输入法、云同步可能已经读过一次。")
    print("      真正的保障是「敏感内容压根不落盘」，见设置 → 隐私。")
    return 0


def _run_cli(args: argparse.Namespace) -> int:
    from .core.normalize import canonical_content
    from .core.semantic import SemanticEngine
    from .utils.config import Config

    if args.selfcheck:
        return _run_selfcheck(args)

    config = Config()

    if args.outbound:
        # 与 --selfcheck 一样**不打开历史库**：它回答的是「原生能力能不能用」，
        # 不该被历史文件损坏挡住 —— 那恰恰是最需要它的时候。
        return _run_outbound(args, config)

    handled = _apply_storage_switch(args, config)
    if handled >= 0:
        return handled

    storage = _load_storage(config)

    if args.images_pending or args.sync_images:
        code = _run_image_sync(args, storage, config)
        storage.close()
        return code

    if args.images or args.images_gc:
        code = _run_images(args, storage)
        storage.close()
        return code

    if args.stats:
        from .core.ocr import build_engine

        stats = storage.stats()
        ocr_status = build_engine(config).status()
        stats["ocr"] = ocr_status
        primary_status = _primary_status(config)
        stats["primary_selection"] = primary_status
        image_info = _image_status(storage)
        stats["images"] = image_info
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        else:
            print(f"记录总数：{stats['items']}")
            print(f"历史占用：{_footprint_line(stats)}")
            for extra in _footprint_details(stats):
                print(extra)
            print(f"全文检索：{'FTS5（' + stats['tokenizer'] + '）' if stats['fts'] else '未启用（退化为模糊匹配）'}")
            if stats["kinds"]:
                print("类型分布：")
                for kind, count in sorted(stats["kinds"].items(), key=lambda pair: -pair[1]):
                    print(f"  {kind:<10} {count}")
            if stats["sources"]:
                print("常用来源：")
                for source, count in stats["sources"]:
                    print(f"  {source:<20} {count}")
            if ocr_status["usable"]:
                langs = "、".join(ocr_status["available"]) or "未知"
                line = (
                    f"截图 OCR：可用（{ocr_status['backend']} · "
                    f"语言 {ocr_status['languages']} · 已装 {langs}）"
                )
                if ocr_status["degraded"]:
                    # 「能用，但用的不是你要的语言」必须说出来：
                    # 否则日文用户拿中文识别串跑，结果是能跑但认不准
                    line += f"\n         注意：{ocr_status['detail']}"
                print(line)
            elif ocr_status["enabled"]:
                # 装了 tesseract 但没有可用语言数据 —— 以前这里会报「可用」
                print(f"截图 OCR：不可用（{ocr_status['detail']}，截图将无法被文字搜索）")
            else:
                print("截图 OCR：不可用（未找到 tesseract，截图将无法被文字搜索）")
            # 主选择区：开关开着但本机读不到时必须说出来，
            # 否则用户只会看到「我勾了，但选中文字没进历史」
            line = f"主选择区：{'已启用' if primary_status['enabled'] else '未启用'}"
            line += f"（{primary_status['detail']}）"
            if primary_status["warning"]:
                line += f"\n          注意：{primary_status['warning']}"
            print(line)
            # 图片占用：此前统计里完全没有这一项，于是「磁盘为什么一直涨」
            # 在界面上是个无解的问题 —— 记录数明明被上限压住了。
            if image_info["files"] or image_info["missing"]:
                line = (
                    f"图片附件：{image_info['files']} 张 · "
                    f"{image_info['bytes'] / 1024 / 1024:.1f} MB"
                )
                if image_info["unreferenced"]:
                    line += (
                        f"\n          其中 {image_info['unreferenced']} 张"
                        f"（{image_info['unreferenced_bytes'] / 1024 / 1024:.1f} MB）"
                        "没有记录引用，可用 --images-gc 回收"
                    )
                if image_info["missing"]:
                    line += f"\n          有 {image_info['missing']} 条记录引用的图片已不在磁盘上"
                print(line)
        storage.close()
        return 0

    if args.remove is not None:
        from .core.tombstones import build_tombstone_store, fingerprint

        item = storage.get(args.remove)
        if item is None:
            print(f"找不到 ID 为 {args.remove} 的记录", file=sys.stderr)
            storage.close()
            return 1
        tombstones = build_tombstone_store()
        digest = fingerprint(item)
        storage.delete(item.id)
        recorded = tombstones.record(digest) if digest else False
        print(f"已删除记录 {args.remove}")
        if recorded:
            print("已记下删除标记：下次同步时其它设备上的同一内容会一起删除")
        else:
            print("未留下删除标记（这条没有指纹，无法跨设备识别），删除只在本机生效")
        storage.close()
        return 0

    if args.tombstones or args.tombstones_reset or args.purge_tombstones:
        from .core.tombstones import build_tombstone_store

        tombstones = build_tombstone_store()
        if args.tombstones_reset:
            count = tombstones.reset()
            print(f"已清空 {count} 条删除标记，之后的历史删除不再向对端传播")
            if not args.tombstones:
                storage.close()
                return 0
        if args.purge_tombstones:
            removed = tombstones.prune()
            print(f"已清除 {removed} 条超期删除标记")
            if not args.tombstones:
                storage.close()
                return 0
        status = tombstones.status()
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
        else:
            print(f"删除标记文件：{status['path']}")
            print(f"共 {status['count']} 条，其中待同步 {status['pending']} 条")
            print(f"保留期：{status['retention_seconds']} 秒（超期后不再传播）")
            if not status["count"]:
                print("（本机还没有被删除的内容）")
            for row in status["tombstones"]:
                flag = "待同步" if row["pending"] else ("已送达" if row["acknowledged"] else "已超期")
                print(f"  {row['hash'][:12]}  {row['deleted_text']}  {flag}")
        storage.close()
        return 0

    if args.flag_changes or args.flag_changes_reset:
        from .core.flag_changes import build_flag_change_store

        changes = build_flag_change_store()
        if args.flag_changes_reset:
            count = changes.reset()
            print(f"已清空 {count} 条标记变更，之后的历史取消不再向对端传播")
            print("（已在其它设备上生效的取消不受影响，那些设备上的标记已经是取消后的状态）")
            if not args.flag_changes:
                storage.close()
                return 0
        status = changes.status()
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
        else:
            print(f"标记变更文件：{status['path']}")
            print(f"共 {status['count']} 条，其中待同步 {status['pending']} 条")
            if not status["count"]:
                print("（本机还没有对固定/收藏做过显式调整）")
            for row in status["changes"]:
                name = "固定" if row["flag"] == "pinned" else "收藏"
                action = "设置" if row["value"] else "取消"
                flag = "待同步" if row["pending"] else "已送达"
                who = row["device"] or "本机"
                print(f"  {row['hash'][:12]}  {name}{action}   {row['at_text']}  {who}  {flag}")
        storage.close()
        return 0

    if args.clear:
        from .core.tombstones import build_tombstone_store, local_fingerprints

        tombstones = build_tombstone_store()
        before = set(local_fingerprints(storage))
        removed = storage.clear(keep_pinned=True)
        survivors = set(local_fingerprints(storage))
        # 只给「真的消失了」的指纹记墓碑：固定项还在，不能被算成删除
        doomed = [digest for digest in before if digest not in survivors]
        recorded = tombstones.record_many(doomed)
        print(f"已清理 {removed} 条未固定记录")
        if recorded:
            print(f"已记下 {recorded} 条删除标记，下次同步时其它设备上的同一内容会一起清理")
        storage.close()
        return 0

    if args.compact:
        storage.optimize()
        print("数据库已压缩")
        storage.close()
        return 0

    if args.normalize is not None:
        # 排查用：告诉用户「这段内容按归一规则被当成什么」。
        # 「为什么我复制的链接没有被合并」这类问题，看这一行就有答案。
        from .core.classify import analyze

        raw = args.normalize
        kind = str(analyze(raw)["kind"])
        canonical = canonical_content(kind, raw, options=_canonical_options(config))
        print(f"类型：{kind}")
        print(f"原文：{raw}")
        print(f"归一：{canonical}")
        print("相同" if canonical == raw else "已归一（不同写法会被判为同一条）")
        storage.close()
        return 0

    if args.recanonicalize:
        rehashed = storage.recanonicalize(_canonical_options(config))
        if rehashed:
            print(f"已重算 {rehashed} 条记录的指纹（内容未改动）")
        else:
            print("所有记录的指纹都已是最新，无需重算")
        storage.close()
        return 0

    if args.merge_duplicates:
        # 先按当前归一规则重算指纹，再分组合并。
        # 顺序不能反：老记录的指纹是拿原始内容算的，「同一页面被不同渠道分享」
        # 会是几串不同的 hash，直接 dedupe() 分组根本看不出它们重复，
        # 于是这次升级对历史数据等于没生效。
        options = _canonical_options(config)
        rehashed = storage.recanonicalize(options)
        removed = storage.dedupe()
        if rehashed:
            print(f"已按归一规则重算 {rehashed} 条记录的指纹")
        if removed:
            print(f"已合并 {removed} 条重复记录（复制次数已折算到保留项上）")
        elif not rehashed:
            print("没有重复记录需要合并")
        else:
            print("重算后未发现新的重复项")
        storage.close()
        return 0

    if args.config_audit:
        from .utils.config_audit import audit_config_usage, format_audit

        report = audit_config_usage()
        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(format_audit(report))
        storage.close()
        return 0 if report.healthy else 1

    if args.ui_status:
        return _run_ui_status(args, config)

    if args.rules:
        from .core.automation import AutomationEngine, describe_rules, format_rules

        engine = AutomationEngine()
        engine.load_from_config(config.get("automation.workflows", []) or [])
        report = describe_rules(engine.rules())
        if args.json:
            print(json.dumps([item.to_dict() for item in report], ensure_ascii=False, indent=2))
        else:
            print(format_rules(report))
        storage.close()
        return 0 if all(item.will_run for item in report) else 1

    if args.privacy_audit:
        from .core.privacy_audit import audit_privacy, format_audit

        report = audit_privacy(config)
        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(format_audit(report))
        storage.close()
        return 0 if report.healthy else 1

    if args.redact is not None:
        return _run_redact(args, config)

    if args.import_native_history:
        return _run_native_history_import(args, storage, config)

    if args.list or args.search:
        if args.search:
            # 全库快照只取一次：`--search` 只查一遍，但 `search_outcome`
            # 内部会同时问后端与扫全库，两次遍历同一份历史纯属浪费。
            from .core.search_index import SearchIndex

            engine = SemanticEngine(storage, index=SearchIndex(storage))
            outcome = engine.search_outcome(args.search, kind=args.kind, limit=args.limit)
            if args.json:
                # 排查用：结果集本身看不出「为什么搜不到」，
                # `scanned` / `backend` 才是回答那个问题的两个数。
                print(
                    json.dumps(
                        {
                            "query": args.search,
                            "kind": args.kind,
                            "backend": outcome.backend,
                            "terms": outcome.terms,
                            "scanned": outcome.scanned,
                            "candidates": outcome.candidates,
                            "count": len(outcome.items),
                            "items": [
                                {
                                    "id": item.id,
                                    "kind": item.kind,
                                    "preview": item.preview,
                                    "content": item.content,
                                    "source_app": item.source_app,
                                    "created_at": item.created_at,
                                    "pinned": item.pinned,
                                    "favorite": item.favorite,
                                    "tags": item.tags,
                                    "size": item.size,
                                    "use_count": item.use_count,
                                }
                                for item in outcome.items
                            ],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                storage.close()
                return 0
            _print_items(
                outcome.items, as_json=False, plain=args.plain, limit=args.limit
            )
            if not args.plain:
                # 这行是给「搜不到」准备的：用户能立刻看出是候选集太小
                # 还是真的没有匹配，不必去猜自己是不是记错了关键词。
                print(
                    f"（{outcome.backend} · 扫描 {outcome.scanned} 条 · "
                    f"候选 {outcome.candidates} 条 · 命中 {len(outcome.items)} 条）"
                )
            storage.close()
            return 0
        items = storage.list(kind=args.kind, limit=args.limit)
        _print_items(items, as_json=args.json, plain=args.plain, limit=args.limit)
        storage.close()
        return 0

    if args.get is not None:
        item = storage.get(args.get)
        if item is None:
            print(f"找不到 ID 为 {args.get} 的记录", file=sys.stderr)
            storage.close()
            return 1
        if args.form_key:
            return _run_form_key(args, storage, item)
        if args.json:
            print(
                json.dumps(
                    {
                        "id": item.id,
                        "kind": item.kind,
                        "content": item.content,
                        "source_app": item.source_app,
                        "created_at": item.created_at,
                        "files": item.files,
                        "tags": item.tags,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(item.content)
        storage.close()
        return 0

    if args.formats is not None:
        return _run_formats(args, storage)

    if args.sync_cursor or args.sync_cursor_reset:
        from .core.sync_cursor import build_cursor_store

        cursors = build_cursor_store()
        if args.sync_cursor_reset:
            count = len(cursors.snapshot())
            cursors.reset()
            print(f"已清空 {count} 条同步游标，下次同步将重新全量扫描")
            if not args.sync_cursor:
                storage.close()
                return 0
        status = cursors.status()
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
        else:
            print(f"游标文件：{status['path']}")
            if not status["count"]:
                print("尚无同步记录（或从未同步过）")
            for row in status["cursors"]:
                print(f"  {row['key']:<32} {row['since_text']}")
        storage.close()
        return 0

    if args.sync_status:
        from .core.lan_sync import build_lan_sync
        from .core.sync_cursor import build_cursor_store

        cursors = build_cursor_store()
        mode = str(config.get("sync.mode", "folder") or "folder").lower()
        if mode == "lan":
            node = build_lan_sync(storage, config, cursors=cursors)
            if node is None:
                print("局域网同步未启用（需要 sync.enabled = true 且 sync.mode = lan）")
                storage.close()
                return 1
            if node.start():
                status = node.status()
                if args.json:
                    print(json.dumps(status, ensure_ascii=False, indent=2))
                else:
                    print(f"模式：局域网直连（{'运行中' if status['running'] else '已停止'}）")
                    print(f"监听：{status['device']}:{status['port']}")
                    print(f"鉴权：{'已启用共享口令' if status['auth_required'] else '未启用（同网段均可读取，慎用）'}")
                    print(f"加密：{'已启用' if status['encrypted'] else '未启用'}")
                    peers = status.get("peers") or []
                    print(f"对端：{len(peers)} 个")
                    for peer in peers:
                        label = f"{peer['host']}:{peer['port']}"
                        if peer.get("device"):
                            label += f" ({peer['device']})"
                        key = f"lan:{peer['host']}:{peer['port']}"
                        since = cursors.snapshot().get(key, 0.0)
                        if since > 0:
                            import time as _time

                            label += f" · 已同步至 {_time.strftime('%m-%d %H:%M', _time.localtime(since))}"
                        print(f"  {label}")
                    _print_pending_event_summary()
                node.stop()
            else:
                print("局域网同步服务启动失败（端口可能被占用）", file=sys.stderr)
                storage.close()
                return 1
        else:
            folder = str(config.get("sync.folder", "") or "").strip()
            payload = {
                "mode": "folder",
                "folder": folder,
                "configured": bool(folder),
                "cursors": cursors.status(),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print("模式：共享文件夹")
                print(f"同步目录：{folder or '未配置'}")
                print(f"游标文件：{cursors.path}")
                _print_pending_event_summary()
        storage.close()
        return 0

    if args.import_sync or args.export_sync:
        from .core.sync import FolderSync, SyncCipher
        from .core.sync_cursor import build_cursor_store

        cursors = build_cursor_store()

        mode = str(config.get("sync.mode", "folder") or "folder").lower()
        import socket

        device = str(config.get("sync.device_name", "") or "") or socket.gethostname()

        if mode == "lan":
            from .core.lan_sync import build_lan_sync

            node = build_lan_sync(storage, config, cursors=cursors)
            if node is None:
                print("局域网同步未启用（需要 sync.enabled = true 且 sync.mode = lan）", file=sys.stderr)
                storage.close()
                return 1
            if not node.start():
                print("局域网同步服务启动失败（端口可能被占用）", file=sys.stderr)
                storage.close()
                return 1
            try:
                outcome = node.sync_all_detailed()
                print(format_import_summary(outcome, source="局域网对端"))
            finally:
                node.stop()
            storage.close()
            return 0

        folder = str(config.get("sync.folder", "") or "").strip()
        if not folder:
            print("未配置同步目录（请在设置中填写，或设置 sync.folder）", file=sys.stderr)
            storage.close()
            return 1
        from pathlib import Path

        sync = FolderSync(
            storage,
            Path(folder).expanduser(),
            device_name=device,
            cipher=SyncCipher(bool(config.get("sync.encrypt", True)), password=device),
            cursors=cursors,
        )
        if args.export_sync:
            print(f"已导出 {sync.export(limit=args.limit)} 条记录")
        else:
            print(format_import_summary(sync.import_all_detailed(), source="同步目录"))
        storage.close()
        return 0

    storage.close()
    return -1  # 未命中任何 CLI 模式，交给 GUI


def _run_ui_status(args: argparse.Namespace, config) -> int:
    """界面配置的实际口径。

    这一项回答的是「配置里写的、界面上显示的、实际生效的」是不是同一件事。
    列表密度这种枚举型配置此前三处口径都不一致：配置里可以是任意字符串、
    下拉里只有那个选项、消费点自己兜底 —— 而兜底是静默的，于是配置里
    躺着一个不存在的档位时，谁也发现不了。
    """
    from .core.theme_spec import density_report, format_density

    report = density_report(config.get("ui.card_density"))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("玲珑剪切板 · 界面配置")
        print()
        print(format_density(report))
    # 退出码：0 配置合法 / 1 配置需要修正（与 --config-audit 一致，便于脚本判断）
    return 0 if report["valid"] else 1


def _run_selfcheck(args: argparse.Namespace) -> int:
    """原生能力自检。

    刻意不打开数据库：自检回答的是「这台机器上的原生能力能不能用」，
    不该被历史文件损坏之类的问题挡住 —— 否则最需要它的时候就恰好用不了。
    """
    from .utils.config import Config
    from .utils.selfcheck import format_report, run_self_check

    # 带上配置：OCR 一项需要读「识别语言」与开关，用默认配置探测会得到
    # 与实际运行不一致的结论
    report = run_self_check(config=Config())
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    # 退出码：0 全部正常 / 1 有降级 / 2 有失败，便于脚本判断
    if report.failures:
        return 2
    if report.degraded:
        return 1
    return 0


def _run_native_history_import(args: argparse.Namespace, storage, config) -> int:
    """把系统剪贴板历史导入本工具。

    走 `Collector`，与实时采集同一条管线 —— 隐私判定、去重、类型识别、
    OCR 一个都不跳过。绝不直接往 storage 里塞：那样系统历史就成了
    「绕过风控的后门」，从里面捞回的密码会直接落库。
    """
    from .core.collector import Collector
    from .core.native_history import import_native_history
    from .core.ocr import build_engine as build_ocr_engine
    from .core.privacy import PrivacyGuard
    from .platform import detect_backend

    backend = detect_backend()
    guard = PrivacyGuard(config)
    collector = Collector(
        storage,
        config,
        guard,
        ocr=build_ocr_engine(config),
    )
    report = import_native_history(backend, collector, limit=args.limit)

    if args.json:
        print(
            json.dumps(
                {
                    "backend": getattr(backend, "name", "unknown"),
                    "supported": report.supported,
                    "scanned": report.scanned,
                    "captured": report.captured,
                    "merged": report.merged,
                    "skipped_privacy": report.skipped_privacy,
                    "skipped_other": report.skipped_other,
                    "reason": report.unsupported_reason,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        storage.close()
        # 不支持不算失败：这是一项增强能力，缺它不影响本工具工作
        return 0

    if not report.supported:
        print(report.unsupported_reason)
        storage.close()
        return 0

    print(report.summary())
    if report.blocked_samples:
        preview = "、".join(report.blocked_samples)
        print(f"（被隐私策略拦下的内容已丢弃，示例预览：{preview}）")
    storage.close()
    return 0


def _print_pending_event_summary() -> None:
    """打印「还有多少删除/取消等事件等待同步」。

    这两类事件不受增量游标约束、每次同步都会重发直到被确认，所以
    「有几条待发」是判断同步是否真的走通的直接线索 —— 不说的话，
    用户看到对端没反应只能猜。
    """
    try:
        from .core.flag_changes import build_flag_change_store
        from .core.tombstones import build_tombstone_store

        tombstones = build_tombstone_store().status()
        changes = build_flag_change_store().status()
    except Exception:  # noqa: BLE001 - 状态摘要读不到不该让命令失败
        return
    print(
        f"待同步事件：删除 {tombstones.get('pending', 0)} 条"
        f"、标记变更 {changes.get('pending', 0)} 条"
    )


#: 交给 CLI 处理的参数名（`argparse` 的 `dest`）。
#:
#: 为什么要显式维护一张表，而不是像以前那样在 `main()` 里手写一长串
#: `args.xxx` 判断：**漏登记一个标志不会报错，只会静默地掉进 GUI 分支**。
#: 那个分支会去连显示器、起事件循环，于是「本来该立刻返回的命令」变成
#: 一直挂着不返回 —— 这不是崩溃，是卡住，得等超时才发现。
#: 这个坑在 `--merge-duplicates` 上踩过一次，在 `--formats` 上又踩了一次，
#: 所以第三次不再靠记性：改成一张表，并补一条用例守着
#: （`tests/test_cli.py::test_every_cli_flag_is_dispatched`）。
CLI_DEST_NAMES = (
    "stats",
    "clear",
    "compact",
    "merge_duplicates",
    "recanonicalize",
    "normalize",
    "formats",
    "form_key",
    "outbound",
    "list",
    "search",
    "get",
    "import_sync",
    "export_sync",
    "sync_status",
    "import_native_history",
    "sync_cursor",
    "sync_cursor_reset",
    "tombstones",
    "tombstones_reset",
    "purge_tombstones",
    "flag_changes",
    "flag_changes_reset",
    "remove",
    "storage_status",
    "storage",
    "storage_migrate",
    "where",
    "selfcheck",
    "config_audit",
    "ui_status",
    "rules",
    "privacy_audit",
    "redact",
    "images",
    "images_gc",
    "images_pending",
    "sync_images",
)


def wants_cli(args: argparse.Namespace) -> bool:
    """这些参数里有没有哪一个是「要跑命令行模式」的意思。

    判定用「不是默认值」而不是「非空」：`--formats` 这类可选带值的标志
    默认是 `None`，带值时可能是空串（合法值），不能靠真假判断。
    """
    for name in CLI_DEST_NAMES:
        if not hasattr(args, name):
            continue
        value = getattr(args, name)
        if name == "redact":
            # `--redact` 可以接空串（试跑脱敏时给空输入），所以看 `is not None`
            if value is not None:
                return True
            continue
        if isinstance(value, bool):
            if value:
                return True
        elif value is not None:
            return True
    return False


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if wants_cli(args):
        return _run_cli(args)

    try:
        from .ui.application import Application
    except ImportError as exc:
        print(
            f"无法加载图形界面（缺少 PySide6？）：{exc}\n"
            "可改用命令行模式，例如：linglong-clipboard --list",
            file=sys.stderr,
        )
        return 2

    app = Application(
        [sys.argv[0]],
        start_minimized=args.minimized,
        monitor_primary=args.monitor_primary,
    )
    if args.quick:
        app.show_quick_paste()
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
