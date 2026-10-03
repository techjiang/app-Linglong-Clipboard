"""各平台的数据目录约定。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "LinglongClipboard"
APP_DIR_NAME = "linglong-clipboard"

#: JSON 单文件后端的数据目录名。与 SQLite 后端共用同一层目录，
#: 用户在一处就能看到「历史数据库」和「历史 JSON」，不必猜第二个位置。
JSON_DIR_NAME = ".clipboard"
JSON_FILE_NAME = "clipboard.json"


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def data_dir() -> Path:
    """按平台约定返回可写数据目录，目录不存在时创建。"""
    override = os.environ.get("LINGLONG_CLIPBOARD_HOME")
    if override:
        path = Path(override).expanduser()
    elif is_windows():
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
        path = Path(base) / APP_NAME
    elif is_macos():
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
        path = Path(base) / APP_DIR_NAME

    path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir() -> Path:
    """配置文件目录，Linux 下遵循 XDG 规范。"""
    override = os.environ.get("LINGLONG_CLIPBOARD_HOME")
    if override:
        path = Path(override).expanduser()
    elif is_windows():
        path = data_dir()
    elif is_macos():
        path = data_dir()
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
        path = Path(base) / APP_DIR_NAME

    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    override = os.environ.get("LINGLONG_CLIPBOARD_HOME")
    if override:
        path = Path(override).expanduser() / "logs"
    elif is_macos():
        path = Path.home() / "Library" / "Logs" / APP_NAME
    elif is_linux():
        base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
        path = Path(base) / APP_DIR_NAME
    else:
        path = data_dir() / "logs"

    path.mkdir(parents=True, exist_ok=True)
    return path


def json_dir() -> Path:
    """JSON 单文件后端的数据目录（不自动创建，交给写文件时创建）。"""
    override = os.environ.get("LINGLONG_CLIPBOARD_JSON_HOME")
    if override:
        return Path(override).expanduser()
    return data_dir() / JSON_DIR_NAME


def json_path() -> Path:
    """JSON 历史文件路径。"""
    return json_dir() / JSON_FILE_NAME


def database_path() -> Path:
    return data_dir() / "history.db"


def config_path() -> Path:
    return config_dir() / "config.json"
