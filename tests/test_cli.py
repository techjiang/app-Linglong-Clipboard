"""命令行入口：查询、维护、JSON 输出。"""

import json

import pytest

from linglong_clip.__main__ import build_parser, main
from linglong_clip.core.models import content_hash
from linglong_clip.core.normalize import canonical_content


@pytest.fixture()
def cli_env(home, monkeypatch, capsys):
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home))
    from linglong_clip.core.models import ClipboardItem

    from linglong_clip.core.storage import Storage

    storage = Storage(home / "history.db")
    for index, (kind, content) in enumerate(
        [("text", "hello world"), ("link", "https://cnb.cool/x"), ("code", "def f(): pass")]
    ):
        # 用真实指纹而不是编造的 `h0/h1`：`--merge-duplicates` 会先按归一规则
        # 重算指纹，假的 hash 会被认成「需要重算」，让「无重复可合并」的用例失真
        storage.add(
            ClipboardItem(
                kind=kind,
                content=content,
                preview=content,
                hash=content_hash(kind, canonical_content(kind, content)),
                source_app="chrome",
            )
        )
    storage.close()
    return home


def test_list_output(cli_env, capsys):
    assert main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "hello world" in out
    assert "https://cnb.cool/x" in out


def test_list_respects_limit(cli_env, capsys):
    main(["--list", "-n", "1"])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1


def test_list_json(cli_env, capsys):
    assert main(["--list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload[0]["kind"] in {"text", "link", "code"}


def test_list_plain(cli_env, capsys):
    main(["--list", "--plain"])
    out = capsys.readouterr().out
    assert "hello world" in out


def test_filter_by_kind(cli_env, capsys):
    main(["--list", "--kind", "link"])
    out = capsys.readouterr().out
    assert "cnb.cool" in out
    assert "hello world" not in out


def test_search(cli_env, capsys):
    main(["--search", "hello"])
    assert "hello world" in capsys.readouterr().out


def test_search_natural_language(cli_env, capsys):
    """带结构的名词才解析成类型过滤（「最近的链接」），带「的」是信号。"""
    main(["--search", "最近的链接"])
    assert "cnb.cool" in capsys.readouterr().out


def test_search_bare_noun_matches_text(cli_env, capsys):
    """光输「链接」按文字搜，而不是筛出所有链接类记录。

    种子里那条 `https://cnb.cool/x` 不含「链接」二字，所以不该被搜出来 ——
    这是个真实故障的回归用例：此前「链接」被无条件当成类型过滤，
    于是用户搜自己刚复制的那两个字时永远查空，而记录就在库里。
    """
    main(["--search", "链接"])
    out = capsys.readouterr().out
    assert "cnb.cool" not in out
    assert "没有匹配的记录" in out


def test_search_reports_scan_diagnostics(cli_env, capsys):
    """结果末尾报出扫描量：这是「为什么搜不到」唯一的答案来源。

    「没有匹配」与「没进候选集」在结果集里长得一样，用户只能怀疑自己
    记错了关键词 —— 把这两个数打出来，问题当场可分。
    """
    main(["--search", "hello"])
    out = capsys.readouterr().out
    assert "扫描" in out and "候选" in out


def test_search_json_includes_diagnostics(cli_env, capsys):
    assert main(["--search", "hello", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["query"] == "hello"
    assert payload["scanned"] >= 1
    assert payload["backend"]
    assert payload["count"] == len(payload["items"])


def test_search_json_is_not_a_bare_list(cli_env, capsys):
    """`--search --json` 带诊断字段后不再是裸数组，脚本要能据此判断。

    这是刻意的破坏性变更：以前它和 `--list --json` 形状相同，
    于是「为什么没搜到」这件事在脚本里无从下手。形状变了要写进文档。
    """
    main(["--search", "hello", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    assert "items" in payload


def test_search_no_match(cli_env, capsys):
    main(["--search", "zzz-not-there"])
    assert "没有匹配的记录" in capsys.readouterr().out


def test_get_by_id(cli_env, capsys):
    main(["--get", "1"])
    assert "hello world" in capsys.readouterr().out


def test_get_missing_returns_error(cli_env, capsys):
    assert main(["--get", "9999"]) == 1
    assert "找不到" in capsys.readouterr().err


def test_get_json(cli_env, capsys):
    main(["--get", "1", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["content"] == "hello world"


def test_stats(cli_env, capsys):
    assert main(["--stats"]) == 0
    out = capsys.readouterr().out
    assert "记录总数：3" in out
    assert "全文检索" in out


def test_stats_json(cli_env, capsys):
    main(["--stats", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"] == 3


def test_clear(cli_env, capsys):
    assert main(["--clear"]) == 0
    assert "已清理 3 条" in capsys.readouterr().out


def test_compact(cli_env, capsys):
    assert main(["--compact"]) == 0
    assert "压缩" in capsys.readouterr().out


def test_sync_requires_folder(cli_env, capsys):
    assert main(["--import-sync"]) == 1
    assert "未配置同步目录" in capsys.readouterr().err


# ---- 重复项合并 ----


def _add_duplicates(home, times=3, content="重复内容", digest="dup"):
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.storage import Storage

    storage = Storage(home / "history.db")
    for index in range(times):
        storage.add(
            ClipboardItem(
                kind="text",
                content=content,
                preview=content,
                hash=digest,
                created_at=100 + index,
            )
        )
    storage.close()


def test_merge_duplicates_cli(cli_env, capsys):
    _add_duplicates(cli_env)
    assert main(["--merge-duplicates"]) == 0
    assert "已合并 2 条重复记录" in capsys.readouterr().out


def test_merge_duplicates_cli_noop(cli_env, capsys):
    # cli_env 里三条记录内容与指纹都不同，也没有跟踪参数差异，没有可合并的
    assert main(["--merge-duplicates"]) == 0
    out = capsys.readouterr().out
    assert "没有重复记录" in out
    assert "重算" not in out  # 指纹本已是最新，不该报告重算


def test_merge_duplicates_preserves_usage_in_list(cli_env, capsys):
    """合并后列表里只剩一条，且带上复制次数，不再堆成多行。"""
    _add_duplicates(cli_env)
    main(["--merge-duplicates"])
    capsys.readouterr()

    assert main(["--search", "重复内容"]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    # 命令行会额外打一行检索摘要（扫描量/候选数），它不是记录行
    records = [line for line in lines if not line.startswith("（")]
    assert len(records) == 1
    assert "x2" in records[0]


def test_json_output_includes_use_count(cli_env, capsys):
    main(["--list", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert all("use_count" in entry for entry in payload)


# ---- OCR 与局域网同步在 CLI 里的可见性 ----


def test_stats_reports_ocr_capability(cli_env, capsys):
    assert main(["--stats"]) == 0
    out = capsys.readouterr().out
    assert "截图 OCR" in out
    # 已装 tesseract 报可用，未装报不可用，两者都不能缺失
    assert ("可用" in out) or ("不可用" in out)


def test_stats_json_includes_ocr(cli_env, capsys):
    main(["--stats", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "ocr" in payload
    assert set(payload["ocr"]) >= {"enabled", "backend", "languages", "available"}


def test_sync_status_folder_mode(cli_env, capsys):
    assert main(["--sync-status"]) == 0
    out = capsys.readouterr().out
    assert "共享文件夹" in out


def test_sync_status_folder_json(cli_env, capsys):
    main(["--sync-status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "folder"
    # 顺带报出游标文件位置，方便排查"为什么没拉新记录"
    assert "cursors" in payload
    assert payload["cursors"]["path"].endswith("sync_cursors.json")


def test_sync_cursor_empty(cli_env, capsys):
    assert main(["--sync-cursor"]) == 0
    out = capsys.readouterr().out
    assert "游标文件" in out
    assert "尚无同步记录" in out


def test_sync_cursor_json(cli_env, capsys, home):
    from linglong_clip.core.sync_cursor import build_cursor_store

    build_cursor_store().set("lan:127.0.0.1:48620", 1700000000.0)

    assert main(["--sync-cursor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["cursors"][0]["key"] == "lan:127.0.0.1:48620"


def test_sync_cursor_reset(cli_env, capsys):
    from linglong_clip.core.sync_cursor import build_cursor_store

    build_cursor_store().set("lan:a:1", 5.0)
    assert main(["--sync-cursor-reset"]) == 0
    assert "已清空 1 条同步游标" in capsys.readouterr().out
    assert build_cursor_store().snapshot() == {}


def test_sync_cursor_reset_without_existing(cli_env, capsys):
    assert main(["--sync-cursor-reset"]) == 0
    assert "已清空 0 条同步游标" in capsys.readouterr().out


def test_sync_status_lan_mode(cli_env, capsys, config):
    """局域网模式应能报出监听端口与鉴权状态。"""
    config.update(
        {
            "sync.enabled": True,
            "sync.mode": "lan",
            "sync.lan_port": 0,
            "sync.lan_token": "pw",
            "sync.device_name": "cli-node",
        },
        persist=True,
    )
    assert main(["--sync-status"]) == 0
    out = capsys.readouterr().out
    assert "局域网直连" in out
    assert "cli-node" in out
    assert "已启用共享口令" in out


def test_sync_status_lan_json(cli_env, capsys, config):
    config.update(
        {"sync.enabled": True, "sync.mode": "lan", "sync.lan_port": 0},
        persist=True,
    )
    main(["--sync-status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "lan"
    assert payload["running"] is True
    assert isinstance(payload["port"], int)


def test_sync_status_lan_disabled(cli_env, capsys, config):
    config.update({"sync.enabled": False, "sync.mode": "lan"}, persist=True)
    assert main(["--sync-status"]) == 1
    assert "未启用" in capsys.readouterr().out


def test_import_sync_lan_mode(cli_env, capsys, config):
    """局域网模式下 --import-sync 应走拉取逻辑而不是找文件夹。"""
    config.update(
        {"sync.enabled": True, "sync.mode": "lan", "sync.lan_port": 0},
        persist=True,
    )
    assert main(["--import-sync"]) == 0
    assert "局域网对端" in capsys.readouterr().out
    assert "未配置同步目录" not in capsys.readouterr().err


def test_version_flag():
    from linglong_clip import __version__

    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_parser_has_all_modes():
    parser = build_parser()
    options = {action.dest for action in parser._actions}
    for expected in ("quick", "minimized", "list", "search", "get", "stats", "clear",
                     "compact", "json", "plain", "sync_status", "selfcheck",
                     "config_audit", "rules", "privacy_audit", "redact"):
        assert expected in options


# ---- 历史存储切换 ----


def test_storage_status_defaults_to_sqlite(home, capsys):
    assert main(["--storage-status"]) == 0
    out = capsys.readouterr().out
    assert "SQLite" in out
    assert "history.db" in out


def test_storage_status_json_backend(home, capsys):
    main(["--storage", "json"])
    capsys.readouterr()
    assert main(["--storage-status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["backend"] == "json"
    assert payload["path"].endswith(".clipboard/clipboard.json")


def test_storage_switch_is_idempotent(home, capsys):
    main(["--storage", "json"])
    capsys.readouterr()
    assert main(["--storage", "json"]) == 0
    assert "无需切换" in capsys.readouterr().out


def test_where_prints_data_dir(home, capsys):
    assert main(["--where"]) == 0
    assert capsys.readouterr().out.strip() == str(home)


def test_list_uses_json_backend(home, capsys):
    """切到 JSON 后，CLI 读写的就是那个文件，而不是 history.db。"""
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.repository import JsonRepository
    from linglong_clip.utils import paths

    main(["--storage", "json"])
    repo = JsonRepository(paths.json_path())
    repo.add(ClipboardItem(kind="text", content="json 里的记录", preview="json 里的记录", hash="h"))
    repo.close()
    capsys.readouterr()

    assert main(["--list"]) == 0
    assert "json 里的记录" in capsys.readouterr().out
    assert not (home / "history.db").exists()


def test_migrate_sqlite_to_json(home, capsys):
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.repository import JsonRepository
    from linglong_clip.core.storage import Storage
    from linglong_clip.utils import paths

    storage = Storage(home / "history.db")
    storage.add(ClipboardItem(kind="text", content="要搬过去的记录", preview="p", hash="h1"))
    storage.close()

    assert main(["--storage", "json", "--storage-migrate"]) == 0
    assert "复制 1 条" in capsys.readouterr().out

    repo = JsonRepository(paths.json_path())
    assert [item.content for item in repo.list()] == ["要搬过去的记录"]
    repo.close()
    # 原数据保留，用户后悔时还能切回去
    assert (home / "history.db").exists()


def test_migrate_skips_existing_hashes(home, capsys):
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.repository import JsonRepository
    from linglong_clip.core.storage import Storage
    from linglong_clip.utils import paths

    repo = JsonRepository(paths.json_path())
    repo.add(ClipboardItem(kind="text", content="已存在", preview="p", hash="dup"))
    repo.close()

    storage = Storage(home / "history.db")
    storage.add(ClipboardItem(kind="text", content="已存在", preview="p", hash="dup"))
    storage.close()

    capsys.readouterr()
    assert main(["--storage", "json", "--storage-migrate"]) == 0
    assert "复制 0 条" in capsys.readouterr().out


def test_migrate_json_to_sqlite(home, capsys):
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.repository import JsonRepository
    from linglong_clip.core.storage import Storage
    from linglong_clip.utils import paths

    repo = JsonRepository(paths.json_path())
    repo.add(ClipboardItem(kind="link", content="https://cnb.cool", preview="p", hash="h2"))
    repo.close()

    main(["--storage", "json"])
    capsys.readouterr()
    assert main(["--storage-migrate"]) == 0  # 当前是 sqlite 源，目标是 json
    assert main(["--storage", "sqlite", "--storage-migrate"]) == 0  # 再搬回来
    assert "复制 1 条" in capsys.readouterr().out

    storage = Storage(home / "history.db")
    assert [item.content for item in storage.list()] == ["https://cnb.cool"]
    storage.close()


def test_storage_status_mentions_other_backend(home, capsys):
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.storage import Storage

    storage = Storage(home / "history.db")
    storage.add(ClipboardItem(kind="text", content="x", preview="p", hash="h"))
    storage.close()

    main(["--storage", "json"])
    capsys.readouterr()
    assert main(["--storage-status"]) == 0
    assert "另一种存储也有数据" in capsys.readouterr().out


# ---- 归一化去重：--normalize / --recanonicalize ----


def test_normalize_preview_for_link(cli_env, capsys):
    assert main(["--normalize", "https://a.com/p?utm_source=x&q=1"]) == 0
    out = capsys.readouterr().out
    assert "https://a.com/p?q=1" in out
    assert "已归一" in out


def test_normalize_preview_for_clean_input(cli_env, capsys):
    assert main(["--normalize", "普通文本"]) == 0
    out = capsys.readouterr().out
    assert "相同" in out


def test_normalize_preview_reports_kind(cli_env, capsys):
    assert main(["--normalize", "/tmp/a//b.txt"]) == 0
    out = capsys.readouterr().out
    assert "类型：file" in out
    assert "/tmp/a/b.txt" in out


def test_recanonicalize_reports_count(cli_env, capsys):
    assert main(["--recanonicalize"]) == 0
    out = capsys.readouterr().out
    # cli_env 的记录指纹已是归一后的，应当报告无需重算
    assert "无需重算" in out


def test_recanonicalize_then_merge_legacy(cli_env, capsys):
    """老数据经重算后能被合并，且合并结果保住复制次数。"""
    from linglong_clip.core.models import ClipboardItem, content_hash
    from linglong_clip.core.storage import Storage

    storage = Storage(cli_env / "history.db")
    storage.clear()
    for index, url in enumerate(
        ["https://a.com/p?utm_source=weibo", "https://a.com/p?utm_source=wechat", "https://a.com/p"]
    ):
        storage.add(ClipboardItem(kind="link", content=url, preview=url,
                                  hash=content_hash("link", url), created_at=100 + index))
    storage.close()

    assert main(["--merge-duplicates"]) == 0
    out = capsys.readouterr().out
    assert "已按归一规则重算 2 条记录的指纹" in out
    assert "已合并 2 条重复记录" in out

    assert main(["--list"]) == 0
    listing = capsys.readouterr().out
    assert "x2" in listing


# ---- 原生能力自检 ----


def _fake_report(failures=0, degraded=0):
    """构造一份指定状态的假自检报告。"""
    from linglong_clip.utils.selfcheck import (
        DEGRADED,
        FAIL,
        OK,
        CheckResult,
        SelfCheckReport,
    )

    report = SelfCheckReport(platform_label="TestOS", python_version="3.x", backend="fake")
    report.results.append(CheckResult("后端", OK, "fake"))
    for index in range(degraded):
        report.results.append(CheckResult(f"降级项{index}", DEGRADED, "原因", "提示"))
    for index in range(failures):
        report.results.append(CheckResult(f"失败项{index}", FAIL, "原因", "提示"))
    report.notes = ["结论说明"]
    return report


def test_selfcheck_prints_report(cli_env, capsys, monkeypatch):
    from linglong_clip.utils import selfcheck as sc_module

    monkeypatch.setattr(sc_module, "run_self_check", lambda **kw: _fake_report())
    assert main(["--selfcheck"]) == 0
    out = capsys.readouterr().out
    assert "原生能力自检" in out
    assert "结论：" in out


def test_selfcheck_json_output(cli_env, capsys, monkeypatch):
    from linglong_clip.utils import selfcheck as sc_module

    monkeypatch.setattr(sc_module, "run_self_check", lambda **kw: _fake_report())
    assert main(["--selfcheck", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["backend"] == "fake"
    assert payload["healthy"] is True


def test_selfcheck_exit_code_1_when_degraded(cli_env, monkeypatch):
    """降级给退出码 1，脚本能区分「能用但不完善」与「真的坏了」。"""
    from linglong_clip.utils import selfcheck as sc_module

    monkeypatch.setattr(
        sc_module, "run_self_check", lambda **kw: _fake_report(degraded=1)
    )
    assert main(["--selfcheck"]) == 1


def test_selfcheck_exit_code_2_when_failed(cli_env, monkeypatch):
    from linglong_clip.utils import selfcheck as sc_module

    monkeypatch.setattr(sc_module, "run_self_check", lambda **kw: _fake_report(failures=1))
    assert main(["--selfcheck"]) == 2


def test_selfcheck_does_not_need_database(cli_env, monkeypatch, capsys):
    """自检不该被历史文件问题挡住 —— 最需要它的时候恰好用不了最糟。"""
    from linglong_clip.core.storage import Storage
    from linglong_clip.utils import selfcheck as sc_module

    # 放一个损坏的历史文件
    (cli_env / "history.db").write_bytes(b"this is not a sqlite database at all")
    monkeypatch.setattr(sc_module, "run_self_check", lambda **kw: _fake_report())

    assert main(["--selfcheck"]) == 0
    assert "原生能力自检" in capsys.readouterr().out
    # 没被打开过，所以也不会被改坏
    Storage  # 保持导入，确保模块路径没错


def test_selfcheck_skips_gui_dispatch(cli_env, monkeypatch):
    """必须命中 CLI 分支，不能落到 GUI 分支把进程挂住。"""
    from linglong_clip.ui import application as app_module

    def boom(*args, **kwargs):
        raise AssertionError("不该启动 GUI")

    monkeypatch.setattr(app_module, "Application", boom)
    from linglong_clip.utils import selfcheck as sc_module

    monkeypatch.setattr(sc_module, "run_self_check", lambda **kw: _fake_report())
    assert main(["--selfcheck"]) == 0


# ------------------------------------------- 系统剪贴板历史导入（CLI）


def test_import_native_history_registered_in_dispatch(cli_env, monkeypatch, capsys):
    """新标志必须登记进 `main()` 的分发表。

    回归用例：`--merge-duplicates` 当初漏了这一步，于是标志静默落到
    GUI 分支 —— 不报错，直接挂住，靠测试超时才发现。
    """
    from linglong_clip.ui import application as app_module

    def boom(*args, **kwargs):
        raise AssertionError("不该启动 GUI")

    monkeypatch.setattr(app_module, "Application", boom)
    assert main(["--import-native-history"]) == 0


def test_import_native_history_unsupported_message(cli_env, capsys, monkeypatch):
    """平台不支持时给一句可读解释，退出码仍是 0（不影响其它功能）。"""
    import linglong_clip.platform as platform_module

    class _Unsupported:
        name = "linux"

        def is_history_supported(self):
            return False

        def read_windows(self, limit=30):
            raise AssertionError("能力探测已判定不支持，不该再去读")

    monkeypatch.setattr(platform_module, "detect_backend", lambda: _Unsupported())

    assert main(["--import-native-history"]) == 0
    out = capsys.readouterr().out
    assert "不支持" in out or "不提供" in out


def test_import_native_history_json_shape(cli_env, capsys, monkeypatch):
    import linglong_clip.platform as platform_module

    class _Unsupported:
        name = "linux"

        def is_history_supported(self):
            return False

        def read_windows(self, limit=30):
            raise AssertionError("能力探测已判定不支持，不该再去读")

    monkeypatch.setattr(platform_module, "detect_backend", lambda: _Unsupported())

    assert main(["--import-native-history", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["supported"] is False
    assert payload["backend"] == "linux"
    assert set(payload) >= {
        "backend",
        "supported",
        "scanned",
        "captured",
        "merged",
        "skipped_privacy",
        "skipped_other",
        "reason",
    }


def test_import_native_history_imports_and_reports(cli_env, capsys, monkeypatch):
    from linglong_clip.platform.base import CaptureResult

    class _Backend:
        name = "windows"

        def is_history_supported(self):
            return True

        def read_windows(self, limit=30):
            return [
                CaptureResult(kind="text", text="系统历史里的内容"),
                CaptureResult(
                    kind="text",
                    text="-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
                ),
            ]

    import linglong_clip.platform as platform_module

    monkeypatch.setattr(platform_module, "detect_backend", lambda: _Backend())

    assert main(["--import-native-history"]) == 0
    out = capsys.readouterr().out
    assert "导入 1" in out
    assert "隐私拦截 1" in out


def test_import_native_history_blocked_content_not_stored(
    cli_env, capsys, monkeypatch
):
    """被拦下的敏感内容绝不能进库 —— 这是本功能最关键的一条。"""
    from linglong_clip.core.normalize import canonical_content
    from linglong_clip.core.models import content_hash
    from linglong_clip.platform.base import CaptureResult

    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"

    class _Backend:
        name = "windows"

        def is_history_supported(self):
            return True

        def read_windows(self, limit=30):
            return [CaptureResult(kind="text", text=pem)]

    import linglong_clip.platform as platform_module

    monkeypatch.setattr(platform_module, "detect_backend", lambda: _Backend())

    assert main(["--import-native-history"]) == 0
    capsys.readouterr()

    assert main(["--list", "--json"]) == 0
    items = json.loads(capsys.readouterr().out)
    contents = [item.get("content", "") for item in items]
    assert pem not in contents
    assert content_hash("text", canonical_content("text", pem)) != ""


def test_stats_json_reports_language_degradation(cli_env, capsys):
    """`--stats --json` 要能区分「想要的」与「实际用的」语言。

    只报一个 `languages` 字段时，脚本无法判断这个串是否可用 ——
    而不可用的语言串会让 tesseract 整批失败。
    """
    main(["--stats", "--json"])
    payload = json.loads(capsys.readouterr().out)
    ocr = payload["ocr"]
    assert set(ocr) >= {"usable", "requested", "missing", "degraded", "reason", "detail"}


def _write_config(home, **values):
    """把配置写进隔离的数据目录，`main()` 里的 `Config()` 会读到它。"""
    path = home / "config.json"
    payload = json.loads(path.read_text()) if path.is_file() else {}
    payload.update(values)
    path.write_text(json.dumps(payload, ensure_ascii=False))


class _FakeTesseract:
    """假装装了 tesseract，但只带指定语言。"""

    def __init__(self, langs):
        self._langs = langs

    def install(self, monkeypatch):
        import linglong_clip.core.ocr as ocr

        langs = self._langs

        class Backend(ocr.TesseractBackend):
            def path(self):
                return "/usr/bin/tesseract"

            def languages(self):
                return langs

        monkeypatch.setattr(ocr, "TesseractBackend", Backend)
        return self


def test_stats_warns_when_language_degraded(cli_env, capsys, monkeypatch):
    """降级必须在 `--stats` 里说出来，而不是显示成「可用」。"""
    _FakeTesseract(("chi_sim", "eng")).install(monkeypatch)
    _write_config(cli_env, ui={"language": "ja_JP"})

    assert main(["--stats"]) == 0
    out = capsys.readouterr().out
    assert "注意" in out
    assert "jpn" in out
    assert "eng" in out


def test_stats_reports_unusable_when_no_language_data(cli_env, capsys, monkeypatch):
    """装了 tesseract 但没有语言数据时不能报「可用」。"""
    _FakeTesseract(("osd",)).install(monkeypatch)

    assert main(["--stats"]) == 0
    out = capsys.readouterr().out
    assert "截图 OCR：不可用" in out
    assert "语言数据" in out


def test_stats_json_usable_flag_reflects_language_data(cli_env, capsys, monkeypatch):
    _FakeTesseract(("osd",)).install(monkeypatch)
    main(["--stats", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ocr"]["usable"] is False


# ---- 配置项审计（--config-audit）----


def test_config_audit_reports_healthy(cli_env, capsys):
    """当前代码库没有空转配置，退出码 0。"""
    assert main(["--config-audit"]) == 0
    out = capsys.readouterr().out
    assert "配置项审计" in out
    assert "✓" in out


def test_config_audit_json_output(cli_env, capsys):
    assert main(["--config-audit", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["unread"] == []
    assert payload["total"] == payload["read_count"]


def test_config_audit_exit_code_1_when_dead_keys(cli_env, capsys, monkeypatch):
    from linglong_clip.utils import config_audit as module

    monkeypatch.setattr(
        module, "audit_config_usage",
        lambda *a, **kw: module.AuditReport(unread=["ui.fake"], read_count=3),
    )
    assert main(["--config-audit"]) == 1
    out = capsys.readouterr().out
    assert "ui.fake" in out


def test_config_audit_skips_gui_dispatch(cli_env, monkeypatch):
    """必须命中 CLI 分支，不能落到 GUI 分支把进程挂住。

    上一轮就有过一个标志没登记到分发表、于是静默挂起（卡 10 分钟超时）
    的例子，所以新标志都要有这条用例守着。
    """
    from linglong_clip.ui import application as app_module

    def boom(*args, **kwargs):
        raise AssertionError("不该启动 GUI")

    monkeypatch.setattr(app_module, "Application", boom)
    assert main(["--config-audit"]) == 0


# ------------------------------------------- 出站防护（复制敏感内容）


def test_outbound_skips_gui_dispatch(cli_env, monkeypatch):
    """必须命中 CLI 分支，不能落到 GUI 分支把进程挂住。

    这一条的代价曾经真实发生过：`--merge-duplicates` 漏登记分发表，
    表现是静默挂住（卡到超时才发现），而不是报错。
    """
    from linglong_clip.ui import application as app_module

    def boom(*args, **kwargs):
        raise AssertionError("不该启动 GUI")

    monkeypatch.setattr(app_module, "Application", boom)
    assert main(["--outbound"]) == 0


def test_outbound_works_with_corrupted_history(cli_env, capsys):
    """历史文件损坏时仍要能跑。

    这条命令回答的是「原生能力能不能用」，不该被历史文件挡住 ——
    那恰恰是最需要它的时候。与 `--selfcheck` 同样的理由。
    """
    (cli_env / "history.db").write_bytes(b"this is not a sqlite database at all")
    assert main(["--outbound"]) == 0
    assert "写入级系统排除" in capsys.readouterr().out


def test_outbound_reports_platform_and_means(cli_env, capsys):
    assert main(["--outbound"]) == 0
    out = capsys.readouterr().out
    assert "写入级系统排除" in out
    assert "限时清空" in out
    assert "出站脱敏" in out


def test_outbound_states_limits_honestly(cli_env, capsys):
    """必须写明「不是擦除痕迹」—— 用户不能把密钥托付给这条路。"""
    main(["--outbound"])
    out = capsys.readouterr().out
    assert "缩小暴露窗口" in out
    assert "不落盘" in out


def test_outbound_json_shape(cli_env, capsys):
    assert main(["--outbound", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) >= {
        "platform",
        "backend",
        "history_exclusion_formats",
        "auto_clear_seconds",
        "redact_on_copy_out",
    }
    assert isinstance(payload["history_exclusion_formats"], list)


def test_outbound_reports_auto_clear_off(cli_env, capsys):
    """关掉自动清空后必须说出来，不能让用户以为还在防。"""
    from linglong_clip.utils.config import Config

    Config().set("privacy.sensitive_auto_clear_seconds", 0)
    main(["--outbound"])
    out = capsys.readouterr().out
    assert "限时清空：关闭" in out


def test_outbound_reports_redact_enabled(cli_env, capsys):
    from linglong_clip.utils.config import Config

    Config().set("privacy.redact_on_copy_out", True)
    main(["--outbound"])
    assert "出站脱敏：已开启" in capsys.readouterr().out


def test_outbound_survives_exclusion_probe_failure(cli_env, monkeypatch, capsys):
    """探测排除格式失败也要给出其余结论，不能整体报错。"""
    import linglong_clip.platform as platform_module

    class _Exploding:
        name = "exploding"

        def platform_label(self):
            return "测试平台"

        def history_exclusion_formats(self):
            raise RuntimeError("探测炸了")

    monkeypatch.setattr(platform_module, "detect_backend", lambda: _Exploding())
    assert main(["--outbound"]) == 0
    out = capsys.readouterr().out
    assert "探测炸了" in out
    assert "限时清空" in out


def test_outbound_reports_system_mechanism_when_present(cli_env, monkeypatch, capsys):
    """有系统机制时必须报出来 —— 这是 README 承诺的那一条。"""
    import linglong_clip.platform as platform_module

    class _WithExclusion:
        name = "win-like"

        def platform_label(self):
            return "Windows"

        def history_exclusion_formats(self):
            return ["ExcludeClipboardContentFromMonitorProcessing"]

    monkeypatch.setattr(platform_module, "detect_backend", lambda: _WithExclusion())
    main(["--outbound"])
    out = capsys.readouterr().out
    assert "ExcludeClipboardContentFromMonitorProcessing" in out
    assert "不进系统剪贴板历史" in out


# ------------------------------------------------- 主选择区（--stats / 自检）

def test_stats_reports_primary_selection_unsupported(cli_env, capsys, monkeypatch):
    """本机读不到主选择区时要说清楚，而不是只报「未启用」。"""
    import linglong_clip.platform as platform_module

    class _NoPrimary:
        name = "linux"

        def capabilities(self):
            return {"primary_selection": False}

        def read_primary_text(self):
            return ""

    monkeypatch.setattr(platform_module, "detect_backend", lambda: _NoPrimary())

    assert main(["--stats"]) == 0
    out = capsys.readouterr().out
    assert "主选择区" in out
    assert "未启用" in out
    assert "无可用通道" in out


def test_stats_warns_when_primary_enabled_but_unreadable(cli_env, capsys, monkeypatch):
    """开关开着但本机读不到 —— 必须直说，否则用户只会以为功能坏了。"""
    import linglong_clip.platform as platform_module

    class _NoPrimary:
        name = "linux"

        def capabilities(self):
            return {"primary_selection": False}

        def read_primary_text(self):
            return ""

    monkeypatch.setattr(platform_module, "detect_backend", lambda: _NoPrimary())

    from linglong_clip.utils.config import Config
    from linglong_clip.utils.paths import data_dir

    config = Config(data_dir() / "config.json")
    config.set("capture.monitor_primary", True)

    assert main(["--stats"]) == 0
    out = capsys.readouterr().out
    assert "已启用" in out
    assert "注意" in out
    assert "不会进历史" in out


def test_stats_json_includes_primary_selection(cli_env, capsys, monkeypatch):
    import linglong_clip.platform as platform_module

    class _WithPrimary:
        name = "linux"

        def capabilities(self):
            return {"primary_selection": True}

        def read_primary_text(self):
            return "选中内容"

    monkeypatch.setattr(platform_module, "detect_backend", lambda: _WithPrimary())

    assert main(["--stats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["primary_selection"]["supported"] is True
    assert payload["primary_selection"]["backend"] == "linux"


def test_stats_survives_backend_probe_failure(cli_env, capsys, monkeypatch):
    """平台探测抛异常时统计仍要出得来 —— 这正是最需要它的时候。"""
    import linglong_clip.platform as platform_module

    class _Exploding:
        name = "linux"

        def capabilities(self):
            raise RuntimeError("X 服务器断了")

    monkeypatch.setattr(platform_module, "detect_backend", lambda: _Exploding())

    assert main(["--stats"]) == 0
    out = capsys.readouterr().out
    assert "记录总数" in out
    assert "探测失败" in out or "无可用通道" in out


def test_monitor_primary_flag_reaches_cli_branch():
    """新标志必须命中 CLI/启动分支。

    `--merge-duplicates` 当初就是漏了这步，静默落到 GUI 分支挂住，
    表现不是报错而是卡死 —— 加标志时最容易漏的一步。
    """
    from linglong_clip.__main__ import build_parser

    args = build_parser().parse_args(["--monitor-primary"])
    assert args.monitor_primary is True


# ------------------------------------------- 标记变更（取消固定/收藏）


def test_flag_changes_skips_gui_dispatch(cli_env, monkeypatch, capsys):
    """必须命中 CLI 分支，不能落到 GUI 分支把进程挂住。

    与 `--merge-duplicates` 那次的回归同类：漏登记分发表不是报错，
    是静默挂起，靠测试超时才发现。
    """
    from linglong_clip.ui import application as app_module

    def boom(*args, **kwargs):
        raise AssertionError("不该启动 GUI")

    monkeypatch.setattr(app_module, "Application", boom)
    assert main(["--flag-changes"]) == 0


def test_flag_changes_reset_skips_gui_dispatch(cli_env, monkeypatch, capsys):
    from linglong_clip.ui import application as app_module

    def boom(*args, **kwargs):
        raise AssertionError("不该启动 GUI")

    monkeypatch.setattr(app_module, "Application", boom)
    assert main(["--flag-changes-reset"]) == 0


def test_flag_changes_empty_state(cli_env, capsys):
    """还没有任何显式调整时给一句可读解释，而不是空白。"""
    assert main(["--flag-changes"]) == 0
    out = capsys.readouterr().out
    assert "标记变更文件" in out
    assert "本机还没有对固定/收藏做过显式调整" in out


def test_flag_changes_lists_records(cli_env, capsys):
    """有变更时列出正负两向，且显示是谁做的。"""
    from linglong_clip.core.flag_changes import (
        FLAG_FAVORITE,
        FLAG_PINNED,
        build_flag_change_store,
    )

    store = build_flag_change_store()
    store.record("abc123", FLAG_PINNED, True, at=1_700_000_000, device="")
    store.record("abc123", FLAG_PINNED, False, at=1_700_000_100, device="")
    store.record("def456", FLAG_FAVORITE, True, at=1_700_000_200, device="laptop")

    assert main(["--flag-changes"]) == 0
    out = capsys.readouterr().out
    assert "固定设置" in out
    assert "固定取消" in out
    assert "收藏设置" in out
    assert "laptop" in out
    assert "本机" in out


def test_flag_changes_json_output(cli_env, capsys):
    import json as _json

    from linglong_clip.core.flag_changes import FLAG_PINNED, build_flag_change_store

    build_flag_change_store().record("abc", FLAG_PINNED, False, at=1_700_000_000)
    assert main(["--flag-changes", "--json"]) == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["changes"][0]["value"] is False


def test_flag_changes_reset_also_prints_status(cli_env, capsys):
    """两个标志同时给出时，重置之后接着展示现状（与 --tombstones 一致）。"""
    from linglong_clip.core.flag_changes import FLAG_PINNED, build_flag_change_store

    build_flag_change_store().record("abc", FLAG_PINNED, True, at=1_700_000_000)
    assert main(["--flag-changes-reset", "--flag-changes"]) == 0
    out = capsys.readouterr().out
    assert "已清空 1 条标记变更" in out
    assert "共 0 条" in out


def test_flag_changes_reset_alone_exits(cli_env, capsys):
    from linglong_clip.core.flag_changes import FLAG_PINNED, build_flag_change_store

    build_flag_change_store().record("abc", FLAG_PINNED, True, at=1_700_000_000)
    assert main(["--flag-changes-reset"]) == 0
    out = capsys.readouterr().out
    assert "已清空 1 条标记变更" in out


def test_flag_changes_flags_registered_in_dispatch_table():
    """两个标志都要在 `main()` 的分发表里 —— 这是最容易漏的一步。"""
    from linglong_clip.__main__ import build_parser

    parser = build_parser()
    assert parser.parse_args(["--flag-changes"]).flag_changes is True
    assert parser.parse_args(["--flag-changes-reset"]).flag_changes_reset is True


# ---- 自动化规则体检（--rules）----


def _write_rules(home, rules):
    import json

    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    config.set("automation.workflows", rules, persist=True)


def test_rules_reports_healthy(home, capsys):
    from linglong_clip.__main__ import main

    _write_rules(
        home,
        [
            {"name": "压缩 JSON", "kinds": ["text"], "actions": ["json.minify"],
             "trigger": "auto"},
            {"name": "去跟踪参数", "kinds": ["link"], "actions": ["url.strip_tracking"],
             "trigger": "outbound"},
        ],
    )
    assert main(["--rules"]) == 0
    out = capsys.readouterr().out
    assert "去跟踪参数" in out
    assert "复制/粘贴出去时自动执行" in out
    assert "其中 0 条不会生效" in out


def test_rules_flags_bad_trigger_with_exit_code(home, capsys):
    """写错的触发方式必须报出来，而不是静默按手动处理。

    这类错误在界面上看不出任何异常，规则只是「什么都不做」——
    与「规则本来就要手动跑」在界面上完全一样。
    """
    from linglong_clip.__main__ import main

    _write_rules(
        home,
        [{"name": "写错了", "kinds": ["link"], "actions": ["url.strip_tracking"],
          "trigger": "paste"}],
    )
    assert main(["--rules"]) == 1
    out = capsys.readouterr().out
    assert "'paste' 不是有效值" in out


def test_rules_json_output(home, capsys):
    import json as jsonlib

    from linglong_clip.__main__ import main

    _write_rules(
        home, [{"name": "r", "kinds": ["text"], "actions": ["json.minify"],
                "trigger": "auto"}]
    )
    assert main(["--rules", "--json"]) == 0
    payload = jsonlib.loads(capsys.readouterr().out)
    assert payload[0]["name"] == "r"
    assert payload[0]["will_run"] is True
    assert "when" in payload[0]


def test_rules_empty_is_fine(home, capsys):
    from linglong_clip.__main__ import main

    assert main(["--rules"]) == 0
    assert "没有配置任何自动化规则" in capsys.readouterr().out


# ---- 隐私体检（--privacy-audit / --redact）----


def _write_redact_rules(home, rules):
    from linglong_clip.utils.config import Config

    config = Config(home / "config.json")
    config.set("privacy.custom_redact_rules", rules, persist=True)


def test_privacy_audit_reports_healthy(home, capsys):
    from linglong_clip.__main__ import main

    _write_redact_rules(home, {"员工号": r"EMP\d{6}"})
    assert main(["--privacy-audit"]) == 0
    out = capsys.readouterr().out
    assert "隐私体检" in out
    assert "员工号" in out
    assert "0 条需要处理" in out


def test_privacy_audit_flags_uncompilable_rule(home, capsys):
    """写错的脱敏正则必须报出来 —— 它会被静默跳过，界面上看不出异常。"""
    from linglong_clip.__main__ import main

    _write_redact_rules(home, {"写错的": "[unclosed"})
    assert main(["--privacy-audit"]) == 1
    out = capsys.readouterr().out
    assert "写错的" in out
    assert "无法编译" in out


def test_privacy_audit_notes_disabled_switch(home, capsys):
    from linglong_clip.__main__ import main
    from linglong_clip.utils.config import Config

    Config(home / "config.json").set("privacy.redact_on_copy_out", False, persist=True)
    assert main(["--privacy-audit"]) == 0
    out = capsys.readouterr().out
    assert "未开启" in out
    assert "一条都不会执行" in out


def test_privacy_audit_json(home, capsys):
    import json as jsonlib

    from linglong_clip.__main__ import main

    _write_redact_rules(home, {"写错的": "[unclosed"})
    assert main(["--privacy-audit", "--json"]) == 1
    payload = jsonlib.loads(capsys.readouterr().out)
    assert payload["healthy"] is False
    assert payload["custom_rules"] == 1


def test_redact_replaces_sensitive_fragment(home, capsys):
    from linglong_clip.__main__ import main

    assert main(["--redact", "password: hunter2xyz"]) == 0
    out = capsys.readouterr().out
    assert "[已脱敏:密码字段]" in out
    assert "hunter2xyz" in out  # 输入那一行仍会显示，方便对照


def test_redact_plain_outputs_only_result(home, capsys):
    from linglong_clip.__main__ import main

    assert main(["--redact", "token=abcdefgh", "--plain"]) == 0
    assert capsys.readouterr().out.strip() == "token=[已脱敏:密码字段]"


def test_redact_reports_no_hit(home, capsys):
    from linglong_clip.__main__ import main

    assert main(["--redact", "just an ordinary sentence"]) == 0
    out = capsys.readouterr().out
    assert "不会被脱敏" in out


def test_redact_json(home, capsys):
    import json as jsonlib

    from linglong_clip.__main__ import main

    assert main(["--redact", "sk-abcdefghijklmnopqrstuvwx", "--json"]) == 0
    payload = jsonlib.loads(capsys.readouterr().out)
    assert payload["changed"] is True
    assert payload["hits"] == {"API Key": 1}


def test_redact_applies_custom_rule(home, capsys):
    """自定义规则要真的参与脱敏 —— 这是这轮补上的那条路径。"""
    from linglong_clip.__main__ import main

    _write_redact_rules(home, {"员工号": r"EMP\d{6}"})
    assert main(["--redact", "负责人 EMP123456", "--plain"]) == 0
    assert capsys.readouterr().out.strip() == "负责人 [已脱敏:员工号]"


def test_redact_does_not_touch_storage(home, capsys):
    """试跑脱敏不该写历史、不该改剪贴板 —— 它只是个探针。"""
    from linglong_clip.__main__ import main

    main(["--redact", "token=abcdefgh"])
    capsys.readouterr()  # 丢掉试跑本身的输出，只看历史里有没有
    assert main(["--list"]) == 0
    assert "abcdefgh" not in capsys.readouterr().out


def test_privacy_audit_skips_gui_dispatch(cli_env, monkeypatch):
    """新标志都要有这条用例守着它不会落到 GUI 分支把进程挂住。"""
    from linglong_clip.ui import application as app_module

    def boom(*args, **kwargs):
        raise AssertionError("不该启动 GUI")

    monkeypatch.setattr(app_module, "Application", boom)
    assert main(["--privacy-audit"]) == 0


def test_redact_skips_gui_dispatch(cli_env, monkeypatch):
    from linglong_clip.ui import application as app_module

    def boom(*args, **kwargs):
        raise AssertionError("不该启动 GUI")

    monkeypatch.setattr(app_module, "Application", boom)
    assert main(["--redact", "hello"]) == 0


# ---- 命令行分发表：所有标志都得有归宿 ----

#: 这些标志**故意**没有 CLI 分支：它们是给图形界面用的运行模式。
#: 出现新名字时必须在这里显式登记 —— 让「漏登记」变成一个需要改两处的动作，
#: 而不是一个不会报错的疏忽。
#: 修饰性标志。它们本身不表示「要跑命令行」，只跟着别的标志走，
#: 所以不该出现在 `CLI_DEST_NAMES` 里（放进去会让 `--dry-run` 单独执行时
#: 也走 CLI 分支，而它没有任何含义）。
MODIFIER_DESTS = {
    "dry_run",
    "images_limit",
}

#: GUI 专用（或仅影响输出的）标志：它们不决定走哪条分支。
GUI_ONLY_DESTS = {
    "version",
    "quick",
    "minimized",
    "monitor_primary",
    "json",
    "plain",
    "limit",
    "kind",
    "help",
}


def test_every_cli_flag_is_dispatched():
    """每个参数要么在 `CLI_DEST_NAMES` 里，要么是显式登记的 GUI 专用项。

    守的是一类**不会报错、只会卡住**的缺陷：新加的标志忘记登记到分发表，
    于是它静默地掉进 GUI 分支 —— 那里会去连显示器、起事件循环，
    「本该立刻返回的命令」变成一直挂着。这个坑在 `--merge-duplicates`
    和 `--formats` 上各踩过一次，第三次改成用表驱动 + 这条用例兜底。
    """
    import argparse

    from linglong_clip.__main__ import CLI_DEST_NAMES, build_parser

    parser = build_parser()
    dests = set()
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if action.dest:
            dests.add(action.dest)

    missing = dests - set(CLI_DEST_NAMES) - GUI_ONLY_DESTS - MODIFIER_DESTS
    assert not missing, (
        f"这些参数没有归宿：{sorted(missing)}。"
        f"要么在 CLI_DEST_NAMES 里加上，要么在 GUI_ONLY_DESTS / MODIFIER_DESTS 里登记。"
    )
    unknown = set(CLI_DEST_NAMES) - dests
    assert not unknown, f"CLI_DEST_NAMES 里有不存在的参数：{sorted(unknown)}"


# ---- 界面配置的实际口径（--ui-status）----


def test_ui_status_reports_effective_density(cli_env, capsys):
    assert main(["--ui-status"]) == 0
    out = capsys.readouterr().out
    assert "列表密度" in out
    assert "实际使用" in out
    # 可选档位要逐个列出来，否则用户不知道该填什么
    assert "comfortable" in out and "compact" in out


def test_ui_status_json_is_machine_readable(cli_env, capsys):
    assert main(["--ui-status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["effective"] in ("comfortable", "compact")
    assert payload["valid"] is True
    assert payload["issue"] == ""
    assert {item["value"] for item in payload["options"]} == {"comfortable", "compact"}


def test_ui_status_flags_dirty_config_value(cli_env, capsys, home):
    """配置里躺着一个不存在的档位时，命令要报出来并把文件改回合法值。

    这是本轮缺陷的**回归闸门**：修复前这个值会被原样读出来、
    `--ui-status` 也不存在，于是「界面显示成默认档位、配置里写着别的」
    这件事没有任何地方能查。
    """
    from linglong_clip.utils.paths import config_path

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ui": {"card_density": "超级紧凑"}, "meta": {"schema_version": 1}}),
        encoding="utf-8",
    )

    code = main(["--ui-status"])
    out = capsys.readouterr().out
    # 清洗发生在打开配置时，所以这里报出来的是「实际使用」，
    # 而文件里那个不存在的档位已经被改写 —— 不是任由它躺着。
    assert code == 0
    assert "实际使用" in out
    assert json.loads(path.read_text(encoding="utf-8"))["ui"]["card_density"] == "comfortable"


def test_ui_status_heals_value_written_after_load(cli_env, capsys, home):
    """运行期间被 `set()` 写脏的值，下一次打开配置时同样会被改写。

    清洗挂在**打开**这一步，而不是挂在消费点上：消费点每处都要兜底
    （必须的，界面不能崩），但兜底救不了文件里的值。
    一条命令就能验证这条链路 —— 写完脏值、再跑一次命令、看文件。
    """
    from linglong_clip.utils.config import Config
    from linglong_clip.utils.paths import config_path

    config = Config(config_path())
    config.set("ui.card_density", "超级紧凑")
    assert json.loads(config_path().read_text(encoding="utf-8"))["ui"]["card_density"] == "超级紧凑"

    assert main(["--ui-status"]) == 0
    assert "实际使用" in capsys.readouterr().out
    assert json.loads(config_path().read_text(encoding="utf-8"))["ui"]["card_density"] == "comfortable"


def test_ui_status_exit_code_is_one_when_issue_is_reported(cli_env, capsys, monkeypatch):
    """体检结论不受清洗影响：报告说有问题就退 1。

    清洗与报告是两件事 —— 清洗让文件变正确，但**这一次**读到的值确实
    曾经是错的。把两者混成一件，就会出现「改了文件所以退出码永远是 0」，
    脚本再也拿不到信号。
    """
    from linglong_clip.utils.config import Config

    # 模拟「值仍不合法」的那一刻：清洗函数被跳过（比如它自己不认这个键）
    monkeypatch.setattr(
        Config, "get", lambda self, key, default=None: "乱填" if key == "ui.card_density" else default
    )
    assert main(["--ui-status"]) == 1
    assert "不是有效" in capsys.readouterr().out


def test_ui_status_never_crashes_on_garbage(cli_env, capsys, home):
    """`config.json` 被手工改成非字符串也要能给出结论。

    体检类是「最需要它的时候最不能挂」的命令：配置被写坏时用户第一个
    会去跑的就是它。
    """
    from linglong_clip.utils.paths import config_path

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ui": {"card_density": [1, 2, 3]}, "meta": {"schema_version": 1}}),
        encoding="utf-8",
    )
    assert main(["--ui-status"]) == 0
    assert "列表密度" in capsys.readouterr().out


def test_ui_status_flag_skips_gui_dispatch(cli_env, monkeypatch):
    from linglong_clip.ui import application as app_module

    monkeypatch.setattr(
        app_module, "Application", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该启动 GUI"))
    )
    assert main(["--ui-status"]) == 0


def test_formats_flag_skips_gui_dispatch(cli_env, monkeypatch):
    from linglong_clip.ui import application as app_module

    monkeypatch.setattr(
        app_module, "Application", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该启动 GUI"))
    )
    assert main(["--formats"]) == 0


# ---- 取用剪贴板里的其它形式（--formats / --get --form-key）----


def _add_item_with_formats(home, *, body="看这个 链接\n", html='<p>看这个 <a href="https://a/b">链接</a></p>'):
    from linglong_clip.core.models import ClipboardItem

    from linglong_clip.core.storage import Storage

    storage = Storage(home / "history.db")
    metadata = {"formats": {"data": {"text/html": html}}}
    if body.endswith("\n"):
        import base64

        metadata["text_bytes"] = base64.b64encode(body.encode()).decode()
    item = storage.add(
        ClipboardItem(
            kind="text",
            content=body,
            preview="看这个 链接",
            hash=content_hash("text", "formats-seed"),
            metadata=metadata,
        )
    )
    storage.close()
    return item.id


def test_form_formats_lists_stored_forms(cli_env, capsys):
    _add_item_with_formats(cli_env)
    assert main(["--formats"]) == 0
    out = capsys.readouterr().out
    assert "已存：text/html" in out
    assert "可取用：" in out


def test_form_formats_json(cli_env, capsys):
    _add_item_with_formats(cli_env)
    assert main(["--formats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    row = next(item for item in payload if item["stored"])
    assert "text/html" in row["stored"]
    assert any(entry["key"] == "html" for entry in row["usable"])


def test_form_formats_reports_empty_history(cli_env, capsys):
    assert main(["--formats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert all(not item["stored"] for item in payload)


def test_form_key_returns_html(cli_env, capsys):
    item_id = _add_item_with_formats(cli_env)
    assert main(["--get", str(item_id), "--form-key", "html"]) == 0
    assert "https://a/b" in capsys.readouterr().out


def test_form_key_returns_exact_bytes(cli_env, capsys):
    """`--form-key text` 给出的是存下来的精确字节，末尾换行不丢。"""
    item_id = _add_item_with_formats(cli_env, body="abc\n")
    assert main(["--get", str(item_id), "--form-key", "text"]) == 0
    assert capsys.readouterr().out == "abc\n\n"


def test_form_key_missing_form_reports_available(cli_env, capsys):
    item_id = _add_item_with_formats(cli_env)
    assert main(["--get", str(item_id), "--form-key", "markdown"]) == 1
    err = capsys.readouterr().err
    assert "可选用" in err or "可选形式" in err


def test_form_key_shell_quotes_paths(cli_env, capsys):
    from linglong_clip.core.models import ClipboardItem

    from linglong_clip.core.storage import Storage

    storage = Storage(cli_env / "history.db")
    item = storage.add(
        ClipboardItem(
            kind="file",
            content="/tmp/a b/c.txt",
            preview="x",
            hash=content_hash("file", "p1"),
            files=["/tmp/a b/c.txt", "/tmp/d.txt"],
        )
    )
    storage.close()
    assert main(["--get", str(item.id), "--form-key", "shell"]) == 0
    assert capsys.readouterr().out.strip() == "'/tmp/a b/c.txt' /tmp/d.txt"


# ---- 图片附件（--images / --images-gc）----


def _seed_images(home, orphans=0, *, referenced=1):
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.storage import Storage

    storage = Storage(home / "history.db")
    images = storage.images_dir
    images.mkdir(parents=True, exist_ok=True)
    for index in range(referenced):
        name = f"used{index}.png"
        (images / name).write_bytes(b"\x89PNG" + bytes([index]))
        storage.add(
            ClipboardItem(
                kind="image",
                content=name,
                image_path=f"images/{name}",
                hash=f"h{index}",
                preview=f"[图片] {name}",
            )
        )
    for index in range(orphans):
        (images / f"orphan{index}.png").write_bytes(b"\x89PNG" + b"junk" * 50)
    storage.close()
    return images


def test_images_reports_usage(cli_env, capsys):
    _seed_images(cli_env, orphans=2)

    assert main(["--images"]) == 0
    out = capsys.readouterr().out
    assert "3 张" in out
    assert "2 张未被任何记录引用" in out
    assert "--images-gc" in out


def test_images_json_shape(cli_env, capsys):
    _seed_images(cli_env, orphans=1)

    assert main(["--images", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["files"] == 2
    assert payload["unreferenced"] == 1
    assert payload["unreferenced_bytes"] > 0
    assert payload["missing"] == 0


def test_images_without_any_files(cli_env, capsys):
    from linglong_clip.core.storage import Storage

    Storage(cli_env / "history.db").close()
    assert main(["--images"]) == 0
    assert "没有图片附件" in capsys.readouterr().out


def test_images_reports_missing(cli_env, capsys):
    images = _seed_images(cli_env, referenced=1)
    (images / "used0.png").unlink()

    assert main(["--images"]) == 0
    out = capsys.readouterr().out
    assert "已不在磁盘上" in out


def test_images_gc_dry_run_keeps_files(cli_env, capsys):
    images = _seed_images(cli_env, orphans=3)

    assert main(["--images-gc", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "将回收 3 张" in out
    assert "预演" in out
    assert sorted(path.name for path in images.iterdir()) == [
        "orphan0.png",
        "orphan1.png",
        "orphan2.png",
        "used0.png",
    ]


def test_images_gc_removes_only_orphans(cli_env, capsys):
    images = _seed_images(cli_env, orphans=3)

    assert main(["--images-gc"]) == 0
    assert "已回收 3 张" in capsys.readouterr().out
    assert [path.name for path in images.iterdir()] == ["used0.png"]


def test_images_gc_nothing_to_do(cli_env, capsys):
    _seed_images(cli_env, orphans=0)
    assert main(["--images-gc"]) == 0
    assert "没有可回收" in capsys.readouterr().out


def test_images_gc_json(cli_env, capsys):
    _seed_images(cli_env, orphans=2)
    assert main(["--images-gc", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["removed"] == 2
    assert sorted(payload["names"]) == ["orphan0.png", "orphan1.png"]


def test_stats_mentions_image_usage(cli_env, capsys):
    _seed_images(cli_env, orphans=2)
    assert main(["--stats"]) == 0
    out = capsys.readouterr().out
    assert "图片附件" in out
    assert "--images-gc" in out


def test_stats_json_includes_images(cli_env, capsys):
    _seed_images(cli_env, orphans=1)
    assert main(["--stats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["images"]["files"] == 2
    assert payload["images"]["unreferenced"] == 1


def test_images_flag_skips_gui_dispatch(cli_env, monkeypatch):
    """新标志忘登记到分发表时会静默掉进 GUI 分支并挂住 —— 这条守着它。"""
    from linglong_clip.ui import application as app_module

    monkeypatch.setattr(
        app_module,
        "Application",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不该启动 GUI")),
    )
    assert main(["--images"]) == 0


# ---- 图片按需补图（--images-pending / --sync-images）----


def _seed_pending(home, count=2):
    """造「记录说有图、但本机没有那份文件」的状态。"""
    from linglong_clip.core.models import IMAGE_DIGEST_KEY, ClipboardItem
    from linglong_clip.core.storage import Storage

    storage = Storage(home / "history.db")
    digests = [f"{index:032x}" for index in range(1, count + 1)]
    for digest in digests:
        storage.add(
            ClipboardItem(
                kind="image",
                content=f"{digest}.png",
                preview="[图片]",
                hash=f"img-{digest[:6]}",
                metadata={IMAGE_DIGEST_KEY: digest},
            )
        )
    storage.close()
    return digests


def test_images_pending_lists_missing(cli_env, capsys):
    digests = _seed_pending(cli_env, 2)

    assert main(["--images-pending"]) == 0
    out = capsys.readouterr().out
    assert "缺失 2 张图片" in out
    for digest in digests:
        assert digest in out
    assert "--sync-images" in out


def test_images_pending_json_shape(cli_env, capsys):
    digests = _seed_pending(cli_env, 1)

    assert main(["--images-pending", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["pending"] == digests


def test_images_pending_empty(cli_env, capsys):
    assert main(["--images-pending"]) == 0
    assert "没有缺失的图片" in capsys.readouterr().out


def test_sync_images_without_lan_mode_explains(cli_env, capsys):
    """共享文件夹模式没有请求通道，要明确说出来而不是给个空结果。"""
    _seed_pending(cli_env, 1)

    assert main(["--sync-images"]) == 2
    err = capsys.readouterr().err
    assert "没有可用的取图通道" in err
    assert "lan" in err


def test_sync_images_flag_is_dispatched_not_gui(cli_env, monkeypatch, capsys):
    """`--sync-images` 必须走 CLI 分支。

    漏登记到分发表时它不报错，而是掉进 GUI 分支去连显示器、起事件循环 ——
    本该立刻返回的命令变成一直挂着，等超时才发现。
    """
    _seed_pending(cli_env, 1)
    called = {}

    def fake_gui(*args, **kwargs):
        called["gui"] = True
        raise AssertionError("--sync-images 不该走 GUI 分支")

    monkeypatch.setattr("linglong_clip.__main__._run_gui", fake_gui, raising=False)
    main(["--sync-images"])
    assert "gui" not in called


# ---- 真实磁盘占用 ----


def test_stats_reports_real_footprint(home, capsys):
    """`--stats` 报的必须是磁盘真实占用，不是主文件大小。

    用户拿这个数字决定要不要清理历史，而 SQLite 在 WAL 模式下
    `-wal` 里装着尚未并入主库的全部改动 —— 只 stat 主文件会少报一大截，
    与 `du` 对不上。这条用例把「CLI 说的话」和「磁盘上的事实」钉在一起。
    """
    import os
    from pathlib import Path

    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.storage import Storage

    store = Storage(home / "history.db")
    for index in range(60):
        store.add(
            ClipboardItem(kind="text", content=f"内容 {index} " + "x" * 2000, preview=f"{index}")
        )
    # 先关掉连接：CLI 是另一个进程，它看到的只会有「已关闭的连接」那一份。
    # 拿带 WAL 的现场去比会自欺欺人 —— 两边根本不在同一个时刻。
    store.close()
    disk = sum(
        os.stat(p).st_size for p in Path(home).glob("history.db*") if p.is_file()
    )

    assert main(["--stats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["db_size"] == payload["storage_files"]["bytes"]
    # 量级要对得上。不写等号是因为 `--stats` 自己会打开一次库，
    # 那次打开会往 WAL 里补一点东西 —— 差几十字节是正常的，
    # 而旧实现（只 stat 主文件）会差出几倍。
    assert abs(payload["db_size"] - disk) < 64 * 1024


def test_stats_text_says_where_the_bytes_are(home, capsys):
    """文本输出要能看出「主库之外还有多少」。"""
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.storage import Storage

    store = Storage(home / "history.db")
    for index in range(60):
        store.add(
            ClipboardItem(kind="text", content=f"内容 {index} " + "x" * 2000, preview=f"{index}")
        )
    assert main(["--stats"]) == 0
    out = capsys.readouterr().out
    assert "历史占用：" in out
    store.close()


def test_storage_status_reports_companion_files(home, capsys):
    """`--storage-status` 要报出 WAL / shm，以及它们各占多少。"""
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.storage import Storage

    store = Storage(home / "history.db")
    for index in range(60):
        store.add(
            ClipboardItem(kind="text", content=f"内容 {index} " + "x" * 2000, preview=f"{index}")
        )

    assert main(["--storage-status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["size"] == payload["main_size"] + payload["companion_size"]
    names = {entry["name"] for entry in payload["files"]}
    assert "history.db" in names
    assert "history.db-wal" in names

    assert main(["--storage-status"]) == 0
    out = capsys.readouterr().out
    assert "history.db-wal" in out
    store.close()


def test_storage_status_without_a_database(home, capsys):
    """库还没建时不谎报 0 字节，而是说「尚未创建」。"""
    assert main(["--storage-status"]) == 0
    assert "尚未创建" in capsys.readouterr().out


def test_compact_actually_shrinks(home, capsys):
    """`--compact` 之后占用必须真的变小。

    此前 `VACUUM` 排在 checkpoint **之后**，它自己产生的改动会立刻写一份
    新 WAL —— 终端打印「数据库已压缩」，占用却几乎没变。
    断言结果而不是「调用了 VACUUM」：那种写法同样会执行那些语句。
    """
    from pathlib import Path

    from linglong_clip.core import storage_files
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.storage import Storage

    store = Storage(home / "history.db")
    for index in range(200):
        store.add(
            ClipboardItem(kind="text", content=f"内容 {index} " + "x" * 3000, preview=f"{index}")
        )
    store.delete_many([item.id for item in store.list(limit=1000)][:150])
    store.close()

    before = storage_files.measure(Path(home) / "history.db").total_bytes
    assert main(["--compact"]) == 0
    capsys.readouterr()
    after = storage_files.measure(Path(home) / "history.db").total_bytes
    assert after < before


def test_stats_flags_reclaimable_holes(home, capsys):
    """删过大量记录之后，`--stats` 要指出「空洞可用 --compact 收回」。"""
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.storage import Storage

    store = Storage(home / "history.db")
    for index in range(200):
        store.add(
            ClipboardItem(kind="text", content=f"内容 {index} " + "x" * 3000, preview=f"{index}")
        )
    store.delete_many([item.id for item in store.list(limit=1000)][:180])
    store.close()

    assert main(["--stats"]) == 0
    out = capsys.readouterr().out
    assert "--compact" in out

    assert main(["--stats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["storage_files"]["reclaimable_bytes"] > 0


def test_stats_counts_wal_of_a_live_database(home, capsys):
    """库正在被写入（WAL 里还有东西）时，WAL 必须被算进占用。

    这是本轮修掉的那个偏小的数字：只 stat 主文件时，用户看到的
    是「2.6 MB」，而磁盘上真实躺着 6.7 MB。
    """
    from pathlib import Path

    from linglong_clip.core import storage_files
    from linglong_clip.core.models import ClipboardItem
    from linglong_clip.core.storage import Storage

    store = Storage(home / "history.db")
    for index in range(60):
        store.add(
            ClipboardItem(kind="text", content=f"内容 {index} " + "x" * 2000, preview=f"{index}")
        )
    # 此刻 WAL 里还有未合并的改动
    footprint = storage_files.measure(Path(home) / "history.db")
    assert footprint.companion_bytes > 0
    assert footprint.total_bytes > footprint.main_bytes

    # 同一个进程里直接问 stats()，拿到的就是这份（含 WAL）
    stats = store.stats()
    assert stats["db_size"] == footprint.total_bytes
    assert stats["storage_files"]["companion_bytes"] == footprint.companion_bytes
    store.close()
