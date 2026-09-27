"""测试夹具：全部用临时目录，互不干扰。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# 让测试无需安装即可导入 src 下的包
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """隔离的数据目录，避免测试写到真实用户目录。"""
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def config(home):
    from linglong_clip.utils.config import Config

    return Config(home / "config.json")


@pytest.fixture()
def storage(home):
    from linglong_clip.core.storage import Storage

    instance = Storage(home / "history.db")
    yield instance
    instance.close()


@pytest.fixture(scope="session")
def qt_app():
    """整个测试会话共用一个 QApplication。

    Qt 不允许存在多个实例，`Application` 内部也会复用这一个。
    """
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])
