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
    main(["--search", "链接"])
    assert "cnb.cool" in capsys.readouterr().out


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
    assert len(lines) == 1
    assert "x2" in lines[0]


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
                     "compact", "json", "plain", "sync_status", "selfcheck"):
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
