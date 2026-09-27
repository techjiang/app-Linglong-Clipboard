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
    maintenance.add_argument("--import-sync", action="store_true", help="从同步目录导入")
    maintenance.add_argument("--export-sync", action="store_true", help="导出到同步目录")

    output = parser.add_argument_group("输出")
    output.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本处理")
    output.add_argument("--plain", action="store_true", help="只输出内容，不带格式")
    return parser


def _load_storage(config=None):
    from .core.storage import Storage

    return Storage()


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
        print(f"{str(item.id):>5}  [{markers}] {stamp}  {item.kind_label:<4} {preview}")


def _run_cli(args: argparse.Namespace) -> int:
    from .core.semantic import SemanticEngine
    from .core.storage import Storage

    storage = Storage()

    if args.stats:
        stats = storage.stats()
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

    if args.import_sync or args.export_sync:
        from .core.sync import FolderSync, SyncCipher
        from .utils.config import Config

        config = Config()
        folder = str(config.get("sync.folder", "") or "").strip()
        if not folder:
            print("未配置同步目录（请在设置中填写，或设置 sync.folder）", file=sys.stderr)
            storage.close()
            return 1
        from pathlib import Path

        import socket

        device = str(config.get("sync.device_name", "") or "") or socket.gethostname()
        sync = FolderSync(
            storage,
            Path(folder).expanduser(),
            device_name=device,
            cipher=SyncCipher(bool(config.get("sync.encrypt", True)), password=device),
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
            args.list,
            args.search,
            args.get is not None,
            args.import_sync,
            args.export_sync,
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
