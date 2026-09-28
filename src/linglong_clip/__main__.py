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
            "  linglong-clipboard --import-native-history  导入系统剪贴板历史\n"
            "  linglong-clipboard --selfcheck      自检原生能力是否真的可用\n"
            "  linglong-clipboard --sync-status    查看同步模式与对端状态\n"
            "  linglong-clipboard --sync-cursor    查看各同步来源的增量游标\n"
            "  linglong-clipboard --storage-status 查看历史文件的存放位置\n"
            "  linglong-clipboard --merge-duplicates 合并重复项（含归一后新出现的）\n"
            "  linglong-clipboard --outbound       复制敏感内容时实际生效的防护\n"
            "  linglong-clipboard --remove 12      删除第 12 条（删除会同步到其它设备）\n"
            "  linglong-clipboard --tombstones     查看删除标记的同步进度\n"
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
        "--remove",
        type=int,
        metavar="ID",
        help="删除一条记录，并让这次删除同步到其它设备",
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

    if args.import_native_history:
        return _run_native_history_import(args, storage, config)

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
            args.outbound,
            args.list,
            args.search,
            args.get is not None,
            args.import_sync,
            args.export_sync,
            args.sync_status,
            args.import_native_history,
            args.sync_cursor,
            args.sync_cursor_reset,
            args.tombstones,
            args.tombstones_reset,
            args.purge_tombstones,
            args.remove is not None,
            args.storage_status,
            args.storage is not None,
            args.storage_migrate,
            args.where,
            args.selfcheck,
            args.config_audit,
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
