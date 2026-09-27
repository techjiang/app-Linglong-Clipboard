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
            "  linglong-clipboard --list -n 20     列出最近 20 条\n"
            "  linglong-clipboard --search '上周的截图'\n"
            "  linglong-clipboard --get 12         输出第 12 条内容\n"
            "  linglong-clipboard --stats          查看统计与检索能力\n"
            "  linglong-clipboard --sync-status    查看同步模式与对端状态\n"
            "  linglong-clipboard --sync-cursor    查看各同步来源的增量游标\n"
            "  linglong-clipboard --storage-status 查看历史文件的存放位置\n"
            "  linglong-clipboard --merge-duplicates 合并重复项（含归一后新出现的）\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"玲珑剪切板 {__version__}")

    mode = parser.add_argument_group("运行模式")
    mode.add_argument("--quick", action="store_true", help="启动后直接唤起快速粘贴浮层")
    mode.add_argument("--minimized", action="store_true", help="启动后最小化到系统托盘")

    query = parser.add_argument_group("查询")
    query.add_argument("--list", action="store_true", help="列出历史记录")
    query.add_argument(
        "--search",
        metavar="QUERY",
        help="检索历史，支持自然语言（如「上周来自 Chrome 的链接」）",
    )
    query.add_argument("--get", type=int, metavar="ID", help="输出指定 ID 的完整内容")
    query.add_argument(
        "--kind",
        choices=["text", "rich", "image", "file", "link", "color", "code", "sensitive"],
        help="按类型过滤",
    )
    query.add_argument("-n", "--limit", type=int, default=20, help="返回条数上限（默认 20）")

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
        "--normalize",
        metavar="TEXT",
        help="预览归一结果：这段内容会被当成哪一条（用于排查「为什么没合并」）",
    )
    maintenance.add_argument("--import-sync", action="store_true", help="从同步来源导入")
    maintenance.add_argument("--export-sync", action="store_true", help="导出到同步来源")
    maintenance.add_argument("--sync-status", action="store_true", help="查看同步模式与对端状态")
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

    output = parser.add_argument_group("输出")
    output.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本处理")
    output.add_argument("--plain", action="store_true", help="只输出内容，不带格式")
    return parser


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
        backend = str(config.get("storage.backend", "sqlite") or "sqlite")
        target = json_path() if backend == "json" else database_path()
        payload = {
            "backend": backend,
            "path": str(target),
            "exists": target.exists(),
            "size": target.stat().st_size if target.exists() else 0,
            "sqlite_path": str(database_path()),
            "json_path": str(json_path()),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        label = "JSON 单文件" if backend == "json" else "SQLite 数据库"
        print(f"存储方式：{label}")
        print(f"历史文件：{target}{'' if target.exists() else '（尚未创建）'}")
        if target.exists():
            print(f"文件大小：{target.stat().st_size / 1024:.1f} KB")
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


def _run_cli(args: argparse.Namespace) -> int:
    from .core.normalize import canonical_content
    from .core.semantic import SemanticEngine
    from .utils.config import Config

    config = Config()

    handled = _apply_storage_switch(args, config)
    if handled >= 0:
        return handled

    storage = _load_storage(config)

    if args.stats:
        from .core.ocr import build_engine

        stats = storage.stats()
        ocr_status = build_engine(config).status()
        stats["ocr"] = ocr_status
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        else:
            print(f"记录总数：{stats['items']}")
            print(f"数据库大小：{stats['db_size'] / 1024:.1f} KB")
            print(f"全文检索：{'FTS5（' + stats['tokenizer'] + '）' if stats['fts'] else '未启用（退化为模糊匹配）'}")
            if stats["kinds"]:
                print("类型分布：")
                for kind, count in sorted(stats["kinds"].items(), key=lambda pair: -pair[1]):
                    print(f"  {kind:<10} {count}")
            if stats["sources"]:
                print("常用来源：")
                for source, count in stats["sources"]:
                    print(f"  {source:<20} {count}")
            if ocr_status["enabled"]:
                langs = "、".join(ocr_status["available"]) or "未知"
                print(f"截图 OCR：可用（{ocr_status['backend']} · 语言 {ocr_status['languages']} · 已装 {langs}）")
            else:
                print("截图 OCR：不可用（未找到 tesseract，截图将无法被文字搜索）")
        storage.close()
        return 0

    if args.clear:
        removed = storage.clear(keep_pinned=True)
        print(f"已清理 {removed} 条未固定记录")
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

    if args.list or args.search:
        if args.search:
            engine = SemanticEngine(storage)
            items = engine.search(args.search, kind=args.kind, limit=args.limit)
        else:
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
                print(f"已从局域网对端导入 {node.sync_all()} 条记录")
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
            print(f"已导入 {sync.import_all()} 条记录")
        storage.close()
        return 0

    storage.close()
    return -1  # 未命中任何 CLI 模式，交给 GUI


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if any(
        [
            args.stats,
            args.clear,
            args.compact,
            args.merge_duplicates,
            args.recanonicalize,
            args.normalize is not None,
            args.list,
            args.search,
            args.get is not None,
            args.import_sync,
            args.export_sync,
            args.sync_status,
            args.sync_cursor,
            args.sync_cursor_reset,
            args.storage_status,
            args.storage is not None,
            args.storage_migrate,
            args.where,
        ]
    ):
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

    app = Application([sys.argv[0]], start_minimized=args.minimized)
    if args.quick:
        app.show_quick_paste()
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
