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
