"""配置管理：JSON 落盘 + 点号路径读写 + 变更通知。"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .logger import get_logger
from .paths import config_path

DEFAULTS: Dict[str, Any] = {
    "capture": {
        "enabled": True,
        "types": ["text", "rich", "image", "file", "link", "color"],
        "max_text_length": 200_000,
        "poll_interval_ms": 300,
    },
    "history": {
        "max_items": 2_000,
        "max_days": 0,  # 0 表示不按时间淘汰
        "merge_duplicates": True,
        "pin_immune": True,
    },
    "privacy": {
        "exclude_password_managers": True,
        "sensitive_retention_seconds": 0,  # 0 表示敏感内容完全不落盘
        "sensitive_auto_clear_seconds": 30,
        "blocked_apps": [],
        "redact_on_copy_out": False,
        "custom_redact_rules": {},
    },
    "ui": {
        "theme": "system",  # system / light / dark
        "language": "zh_CN",
        "fuzzy_search": True,
        "global_hotkey": "Ctrl+Shift+V",
        "quick_paste_hotkey": "Ctrl+Shift+1..9",
        "start_minimized": True,
        "close_to_tray": True,
        "card_density": "comfortable",  # comfortable / compact
    },
    "ocr": {
        # 截图文字识别：让图片能被文字搜到。默认开启，缺 tesseract 时静默降级。
        "enabled": True,
        "executable": "tesseract",
        # 留空表示按 ui.language 自动推断；多语言用 + 连接，如 chi_sim+eng
        "languages": "",
        "min_confidence": 0.0,
        "max_chars": 10_000,
        "timeout": 8.0,
    },
    "sync": {
        "enabled": False,
        "mode": "folder",  # folder / lan
        "folder": "",
        "device_name": "",
        "encrypt": True,
        # ---- 局域网直连模式（mode = lan）----
        "lan_port": 48620,
        # 共享口令：同网段不等于可信，留空表示不做鉴权（仅建议在可信内网使用）
        "lan_token": "",
        # 对端地址列表，支持 "host" / "host:port"，一行一个
        "lan_peers": [],
        # 自动发现依赖 UDP 广播，部分 Wi-Fi 会拦截，故默认关闭
        "lan_discovery": False,
    },
    "automation": {
        "workflows": [],
    },
    "meta": {
        "schema_version": 1,
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _expand_flat_keys(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把平铺的点号键展开成嵌套结构。

    早期版本把配置写成 `{"capture.enabled": true}`，直接把这种文件喂给
    `_deep_merge` 会造出名为 `capture.enabled` 的顶层键，
    之后 `get("capture.enabled")` 就查不到了。这里统一转换一次。
    """
    nested: Dict[str, Any] = {}
    for key, value in raw.items():
        if "." not in key:
            nested[key] = value
            continue
        node = nested
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                # 同名键既有平铺又有嵌套时，以嵌套写法为准
                break
        else:
            node[parts[-1]] = value
    return nested


class Config:
    """线程安全的配置对象，支持 `config.get("ui.theme")` 形式的点号路径。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else config_path()
        self._lock = threading.RLock()
        self._listeners: List[Callable[[str, Any], None]] = []
        self._data = copy.deepcopy(DEFAULTS)
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        with self._lock:
            if not self._path.exists():
                self._data = copy.deepcopy(DEFAULTS)
                self.save()
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                get_logger("config").warning("配置文件读取失败，使用默认配置: %s", exc)
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            self._data = _deep_merge(DEFAULTS, _expand_flat_keys(raw))

    def save(self) -> None:
        with self._lock:
            payload = json.dumps(self._data, ensure_ascii=False, indent=2)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._path)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._data
            for part in key.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return copy.deepcopy(node) if isinstance(node, (dict, list)) else node

    def set(self, key: str, value: Any, persist: bool = True) -> None:
        with self._lock:
            parts = key.split(".")
            node = self._data
            for part in parts[:-1]:
                nxt = node.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[part] = nxt
                node = nxt
            node[parts[-1]] = value
            if persist:
                self.save()
        self._notify(key, value)

    def update(self, values: Dict[str, Any], persist: bool = True) -> None:
        for key, value in values.items():
            self.set(key, value, persist=False)
        if persist:
            self.save()
        for key, value in values.items():
            self._notify(key, value)

    def on_change(self, callback: Callable[[str, Any], None]) -> None:
        self._listeners.append(callback)

    def _notify(self, key: str, value: Any) -> None:
        for callback in list(self._listeners):
            try:
                callback(key, value)
            except Exception:  # noqa: BLE001 - 单个监听器出错不应影响其它监听器
                get_logger("config").exception("配置监听器执行失败: %s", key)

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)
