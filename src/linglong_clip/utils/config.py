"""配置管理：JSON 落盘 + 点号路径读写 + 变更通知。"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .logger import get_logger
from .paths import config_path

#: 当前配置结构版本。**改配置结构时递增它，并在 `_MIGRATIONS` 里补一档转换。**
#:
#: 这个键此前只是个摆设：写在 DEFAULTS 里，但没有任何读取方，
#: 于是「旧版配置需要转换」这件事没有任何地方在做。用户升级后
#: 老配置文件里的旧键位会被原样保留、新键位拿默认值，两边都对不上。
SCHEMA_VERSION = 1

#: 版本号 -> 把「上一版的配置」转成「该版的配置」的函数。
#:
#: 只在格式真的不兼容时才需要加一档。新增配置项不需要 ——
#: `_deep_merge` 会用 DEFAULTS 自动补上缺失的键。
_MIGRATIONS: Dict[int, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}


DEFAULTS: Dict[str, Any] = {
    "capture": {
        "enabled": True,
        "types": ["text", "rich", "image", "file", "link", "color"],
        "max_text_length": 200_000,
        "poll_interval_ms": 300,
        # 主选择区（X11/Wayland PRIMARY，选中即生效）是否也采集。
        # 只监听剪贴板时，不应该因为「我选了一段文字」就被记下来。
        "monitor_primary": False,
        # 主选择区的独立节流，比剪贴板慢：拖选过程中每一帧都在变，
        # 按剪贴板那个间隔读会把中间态全存下来。
        "primary_interval_ms": 1_000,
    },
    "history": {
        "max_items": 2_000,
        "max_days": 0,  # 0 表示不按时间淘汰
        "merge_duplicates": True,
        # 归一化去重：判断「两条内容算不算同一条」时先做规范化处理
        "canonical_dedupe": {
            # 链接去掉 utm_* / fbclid 等跟踪参数：同一页面被不同渠道分享出的
            # 多条链接应当算同一条，而不是各占一行
            "url": True,
            # 路径收敛写法（折叠 //、消解 . 与 ..）：指向同一文件的不同写法算同一条
            "path": True,
        },
        "pin_immune": True,
        # 收下剪贴板里的各种写法（HTML、原始字节、Markdown、uri-list…），
        # 让「复制出去」能拿回原来那份，而不是只剩我们存下来的纯文本。
        # 关掉后记录更小，代价是粘贴结果只能是我们保存的那一份。
        "keep_formats": True,
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
    "storage": {
        # sqlite = 单文件数据库（默认，记录量大更合适）
        # json   = <数据目录>/.clipboard/clipboard.json（可直接阅读与搬运）
        "backend": "sqlite",
        "json_inline_images": True,
        "json_max_inline_image_kb": 2048,
    },
    "automation": {
        "workflows": [],
    },
    # 注意：schema_version 的实际用途见 `_migrate()`，不要手工改这里的值 ——
    # 它由 `SCHEMA_VERSION` 单一来源生成，避免两处各写一个数字然后分叉。
    "meta": {
        "schema_version": SCHEMA_VERSION,
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


def _migrate(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把旧版配置逐档升级到当前 `SCHEMA_VERSION`。

    两个刻意的行为：

    - **只升不降**。文件的版本比当前高（用户装回了旧版程序）时不动它，
      也不报错 —— 降级转换无从写起，硬改只会丢数据。此时保留原版本号，
      下次升回新版程序仍能继续转换。
    - **转换函数抛异常时保留原文**。迁移失败不该让用户打不开程序，
      退回原配置 + 默认值至少还能用，且日志里能看到原因。
    """
    meta = raw.get("meta")
    version = 0
    if isinstance(meta, dict):
        try:
            version = int(meta.get("schema_version") or 0)
        except (TypeError, ValueError):
            version = 0

    if version >= SCHEMA_VERSION:
        return raw

    data = copy.deepcopy(raw)
    for target in range(version + 1, SCHEMA_VERSION + 1):
        migrate = _MIGRATIONS.get(target)
        if migrate is None:
            continue
        try:
            data = migrate(data)
        except Exception as exc:  # noqa: BLE001 - 迁移失败不能让程序起不来
            get_logger("config").warning("配置迁移到 v%d 失败，保留原配置: %s", target, exc)
            return raw

    data.setdefault("meta", {})
    if not isinstance(data["meta"], dict):
        data["meta"] = {}
    data["meta"]["schema_version"] = SCHEMA_VERSION
    if version:
        get_logger("config").info("配置已从 v%d 迁移到 v%d", version, SCHEMA_VERSION)
    return data


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
            # 先展开平铺键、再迁移版本、最后与默认值合并：
            # 顺序反了迁移函数看到的就不是最终形状的配置。
            expanded = _expand_flat_keys(raw)
            migrated = _migrate(expanded)
            self._data = _deep_merge(DEFAULTS, migrated)
            # 迁移过或补过默认值都要落盘，否则下次启动重复做同一件事
            if migrated is not raw:
                self.save()

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
