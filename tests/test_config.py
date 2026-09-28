"""配置：默认值、点号路径、持久化、变更通知。"""

import json

from linglong_clip.utils.config import DEFAULTS, Config


def test_defaults_available(tmp_path):
    config = Config(tmp_path / "c.json")
    assert config.get("capture.enabled") is True
    assert config.get("ui.theme") == "system"


def test_set_and_get(tmp_path):
    config = Config(tmp_path / "c.json")
    config.set("ui.theme", "dark")
    assert config.get("ui.theme") == "dark"


def test_get_missing_returns_default(tmp_path):
    config = Config(tmp_path / "c.json")
    assert config.get("nope.nothing", "fallback") == "fallback"


def test_creates_nested_path(tmp_path):
    config = Config(tmp_path / "c.json")
    config.set("custom.deep.key", 42)
    assert config.get("custom.deep.key") == 42


def test_persists_to_disk(tmp_path):
    path = tmp_path / "c.json"
    Config(path).set("ui.theme", "light")
    assert Config(path).get("ui.theme") == "light"


def test_file_written_on_first_load(tmp_path):
    path = tmp_path / "c.json"
    Config(path)
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["ui"]["theme"] == "system"


def test_user_file_merges_over_defaults(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"ui": {"theme": "light"}}), encoding="utf-8")
    config = Config(path)
    assert config.get("ui.theme") == "light"
    # 未提及的键仍取默认值
    assert config.get("capture.enabled") is True


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{ not json", encoding="utf-8")
    config = Config(path)
    assert config.get("ui.theme") == "system"


def test_non_dict_root_falls_back(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert Config(path).get("ui.theme") == "system"


def test_update_multiple(tmp_path):
    config = Config(tmp_path / "c.json")
    config.update({"ui.theme": "dark", "history.max_items": 50})
    assert config.get("ui.theme") == "dark"
    assert config.get("history.max_items") == 50


def test_change_notification(tmp_path):
    config = Config(tmp_path / "c.json")
    seen = []
    config.on_change(lambda key, value: seen.append((key, value)))
    config.set("ui.theme", "dark")
    assert seen == [("ui.theme", "dark")]


def test_listener_exception_does_not_break_others(tmp_path):
    config = Config(tmp_path / "c.json")
    seen = []
    config.on_change(lambda key, value: 1 / 0)
    config.on_change(lambda key, value: seen.append(key))
    config.set("ui.theme", "dark")
    assert seen == ["ui.theme"]


def test_get_returns_copy_for_mutable_values(tmp_path):
    config = Config(tmp_path / "c.json")
    first = config.get("capture.types")
    first.append("mutated")
    assert "mutated" not in config.get("capture.types")


def test_as_dict_is_a_copy(tmp_path):
    config = Config(tmp_path / "c.json")
    snapshot = config.as_dict()
    snapshot["ui"]["theme"] = "mutated"
    assert config.get("ui.theme") == "system"


def test_no_temp_file_left_behind(tmp_path):
    path = tmp_path / "c.json"
    config = Config(path)
    config.save()
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_defaults_cover_documented_groups():
    for group in ("capture", "history", "privacy", "ui", "sync", "automation", "meta"):
        assert group in DEFAULTS


# ---- meta.schema_version：配置结构版本与迁移 ----


def test_defaults_carry_current_schema_version(tmp_path):
    from linglong_clip.utils.config import SCHEMA_VERSION

    config = Config(tmp_path / "c.json")
    assert config.get("meta.schema_version") == SCHEMA_VERSION


def test_old_config_without_version_is_migrated_and_saved(tmp_path):
    """老配置文件（没有 meta.schema_version）打开后应补上版本号并落盘。

    这个键此前只是摆设：写了但没人读，于是「旧配置需要转换」
    这件事没有任何地方在做。
    """
    from linglong_clip.utils.config import SCHEMA_VERSION

    path = tmp_path / "c.json"
    path.write_text(json.dumps({"ui": {"theme": "dark"}}), encoding="utf-8")
    config = Config(path)
    assert config.get("ui.theme") == "dark"
    assert config.get("meta.schema_version") == SCHEMA_VERSION
    assert json.loads(path.read_text(encoding="utf-8"))["meta"]["schema_version"] == SCHEMA_VERSION


def test_migration_runs_registered_step(tmp_path, monkeypatch):
    """注册一档迁移，确认它真的被执行。"""
    from linglong_clip.utils import config as config_module

    calls = []

    def _step(raw):
        calls.append(raw.get("meta", {}).get("schema_version"))
        raw = dict(raw)
        raw["ui"] = {"theme": "light"}
        return raw

    monkeypatch.setattr(config_module, "SCHEMA_VERSION", 2)
    monkeypatch.setitem(config_module._MIGRATIONS, 2, _step)

    path = tmp_path / "c.json"
    path.write_text(json.dumps({"meta": {"schema_version": 1}, "ui": {"theme": "dark"}}),
                    encoding="utf-8")
    config = Config(path)
    assert calls == [1]
    assert config.get("ui.theme") == "light"
    assert config.get("meta.schema_version") == 2


def test_failing_migration_keeps_original_config(tmp_path, monkeypatch):
    """迁移抛异常时不能让程序起不来：退回原配置 + 默认值。"""
    from linglong_clip.utils import config as config_module

    def _boom(raw):
        raise ValueError("迁移逻辑有 bug")

    monkeypatch.setattr(config_module, "SCHEMA_VERSION", 2)
    monkeypatch.setitem(config_module._MIGRATIONS, 2, _boom)

    path = tmp_path / "c.json"
    path.write_text(json.dumps({"meta": {"schema_version": 1}, "ui": {"theme": "dark"}}),
                    encoding="utf-8")
    config = Config(path)
    # 原值保留，程序照常可用
    assert config.get("ui.theme") == "dark"


def test_future_schema_version_is_left_alone(tmp_path, monkeypatch):
    """文件版本比程序新（装回旧版）时不改动，也不报错。"""
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"meta": {"schema_version": 99}, "ui": {"theme": "light"}}),
                    encoding="utf-8")
    config = Config(path)
    assert config.get("ui.theme") == "light"
    assert config.get("meta.schema_version") == 99


def test_flat_keys_are_expanded_before_migration(tmp_path):
    """先展开平铺键再迁移，否则迁移函数看到的是错形状的配置。"""
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"ui.theme": "dark", "capture.enabled": False}), encoding="utf-8")
    config = Config(path)
    assert config.get("ui.theme") == "dark"
    assert config.get("capture.enabled") is False


def test_schema_version_not_hardcoded_in_defaults():
    """DEFAULTS 里的版本号必须来自 SCHEMA_VERSION，不能是另一个独立数字。"""
    from linglong_clip.utils.config import SCHEMA_VERSION

    assert DEFAULTS["meta"]["schema_version"] == SCHEMA_VERSION
