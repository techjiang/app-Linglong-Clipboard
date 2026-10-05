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


@pytest.fixture(autouse=True)
def isolate_data_dir(tmp_path_factory, monkeypatch):
    """**每一个**用例都跑在隔离的数据目录里。

    为什么是 autouse：游标、墓碑、标记变更这三个存储都默认落在
    `paths.data_dir()`，而它们各自的构造函数**没有**默认取 `tmp_path`。
    忘了显式传 `home` 的用例会静默读写真实用户目录 —— 于是出现
    「单跑绿、一起跑红」「换台机器就红」这类最难查的失败：
    上一个用例写下的游标把下一个用例的记录判成「已同步」。

    这个 fixture 不替代 `home`；`home` 仍然给需要路径的用例用，
    两者的数据目录是同一个（`tmp_path`），只是这里不做显式返回。
    """
    base = tmp_path_factory.mktemp("data-home")
    monkeypatch.setenv("LINGLONG_CLIPBOARD_HOME", str(base))
    return base


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

    app = QApplication.instance() or QApplication([])
    yield app
    # 会话结束时把剪贴板松开再让解释器退出。
    # offscreen 平台下，进程退出时若剪贴板里还压着 `setMimeData` 写进去的
    # 数据，Qt 会在析构阶段段错误 —— 测试全绿之后才崩，容易被误当成
    # 「有测试挂了」。这里主动清一次，退出码就干净了。
    try:
        app.clipboard().clear()
        app.processEvents()
    except Exception:  # noqa: BLE001 - 清理失败不该影响测试结论
        pass
