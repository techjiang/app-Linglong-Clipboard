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
