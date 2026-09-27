"""命令行入口：查询、维护、JSON 输出。"""

import json

import pytest

from linglong_clip.__main__ import build_parser, main


@pytest.fixture()
def cli_env(home, monkeypatch, capsys):
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home))
    from linglong_clip.core.models import ClipboardItem

    from linglong_clip.core.storage import Storage

    storage = Storage(home / "history.db")
    for index, (kind, content) in enumerate(
        [("text", "hello world"), ("link", "https://cnb.cool/x"), ("code", "def f(): pass")]
    ):
        storage.add(
            ClipboardItem(kind=kind, content=content, preview=content, hash=f"h{index}",
                          source_app="chrome")
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
                     "compact", "json", "plain", "sync_status"):
        assert expected in options
