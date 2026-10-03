"""路径解析：环境变量覆盖与目录创建。"""

import os

import pytest

from linglong_clip.utils import paths


def test_override_env_is_respected(home, monkeypatch):
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home))
    assert paths.data_dir() == home


def test_directories_are_created(home, monkeypatch):
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home / "nested" / "deep"))
    assert paths.data_dir().is_dir()
    assert paths.config_dir().is_dir()


def test_platform_detection_flags():
    flags = [paths.is_windows(), paths.is_macos(), paths.is_linux()]
    assert sum(flags) <= 1


def test_database_and_config_paths_are_under_home(home, monkeypatch):
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home))
    assert str(paths.database_path()).startswith(str(home))
    assert str(paths.config_path()).startswith(str(home))


def test_log_dir_created(home, monkeypatch):
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home))
    assert paths.log_dir().is_dir()


def test_json_paths_follow_data_dir(home, monkeypatch):
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home))
    assert paths.json_dir() == home / ".clipboard"
    assert paths.json_path() == home / ".clipboard" / "clipboard.json"


def test_json_dir_can_be_overridden(home, monkeypatch):
    """JSON 目录可单独指定，便于把历史放到同步盘或 U 盘。"""
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home))
    monkeypatch.setenv("LINGLONG_CLIPBOARD_JSON_HOME", str(home / "usb"))
    assert paths.json_dir() == home / "usb"
    assert paths.json_path() == home / "usb" / "clipboard.json"


def test_json_dir_is_not_created_eagerly(home, monkeypatch):
    """与 data_dir 不同：目录留给写文件时创建，避免只读环境里启动就报错。"""
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(home / "fresh"))
    assert not paths.json_dir().exists()
