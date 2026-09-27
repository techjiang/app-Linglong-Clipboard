"""数据模型。"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 载荷类型
TEXT = "text"
RICH = "rich"
IMAGE = "image"
FILE = "file"
LINK = "link"
COLOR = "color"
CODE = "code"
SENSITIVE = "sensitive"

KIND_LABELS: Dict[str, str] = {
    TEXT: "文本",
    RICH: "富文本",
    IMAGE: "图片",
    FILE: "文件",
    LINK: "链接",
    COLOR: "颜色",
    CODE: "代码",
    SENSITIVE: "敏感",
}


def content_hash(kind: str, payload: str) -> str:
    """内容指纹：用于去重与跨设备同步时的冲突判定。"""
    digest = hashlib.blake2b(digest_size=16)
    digest.update(kind.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(payload.encode("utf-8", "replace"))
    return digest.hexdigest()


@dataclass
class ClipboardItem:
    """一条剪贴板记录。"""

    id: Optional[int] = None
    kind: str = TEXT
    content: str = ""
    preview: str = ""
    html: str = ""
    rtf: str = ""
    image_path: str = ""
    image_width: int = 0
    image_height: int = 0
    files: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    source_app: str = ""
    source_title: str = ""
    hash: str = ""
    size: int = 0
    pinned: bool = False
    favorite: bool = False
    sensitive: bool = False
    tags: List[str] = field(default_factory=list)
    device: str = ""
    created_at: float = field(default_factory=time.time)
    used_at: float = 0.0
    use_count: int = 0

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def is_empty(self) -> bool:
        """没有任何实质载荷，采集时应直接丢弃。

        纯空白文本（含只按了回车/空格的情况）同样视为空，
        否则历史里会塞满看不见的记录。
        """
        if self.image_path or self.files:
            return False
        if self.html or self.rtf:
            return False
        return not (self.content or "").strip()

    @property
    def display_source(self) -> str:
        return self.source_app or "未知来源"

    @property
    def size_label(self) -> str:
        if self.size < 1024:
            return f"{self.size} B"
        if self.size < 1024 * 1024:
            return f"{self.size / 1024:.1f} KB"
        return f"{self.size / 1024 / 1024:.2f} MB"

    def to_row(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "content": self.content,
            "preview": self.preview,
            "html": self.html,
            "rtf": self.rtf,
            "image_path": self.image_path,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "files": json.dumps(self.files, ensure_ascii=False),
            "metadata": json.dumps(self.metadata, ensure_ascii=False),
            "source_app": self.source_app,
            "source_title": self.source_title,
            "hash": self.hash,
            "size": self.size,
            "pinned": int(self.pinned),
            "favorite": int(self.favorite),
            "sensitive": int(self.sensitive),
            "tags": json.dumps(self.tags, ensure_ascii=False),
            "device": self.device,
            "created_at": self.created_at,
            "used_at": self.used_at,
            "use_count": self.use_count,
        }

    @classmethod
    def from_row(cls, row: Any) -> "ClipboardItem":
        def _json(value: Any, fallback: Any) -> Any:
            """解析 JSON 字段；内容损坏或类型不符都退回默认值。

            类型要核对，因为库里可能残留旧版本写下的 `[]` / `{}`，
            直接返回会让调用方在 `.get()` 或 `.append()` 上炸掉。
            """
            if not value:
                return fallback
            try:
                parsed = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return fallback
            if isinstance(fallback, dict) and not isinstance(parsed, dict):
                return fallback
            if isinstance(fallback, list) and not isinstance(parsed, list):
                return fallback
            return parsed

        return cls(
            id=row["id"],
            kind=row["kind"],
            content=row["content"] or "",
            preview=row["preview"] or "",
            html=row["html"] or "",
            rtf=row["rtf"] or "",
            image_path=row["image_path"] or "",
            image_width=row["image_width"] or 0,
            image_height=row["image_height"] or 0,
            files=_json(row["files"], []),
            metadata=_json(row["metadata"], {}),
            source_app=row["source_app"] or "",
            source_title=row["source_title"] or "",
            hash=row["hash"] or "",
            size=row["size"] or 0,
            pinned=bool(row["pinned"]),
            favorite=bool(row["favorite"]),
            sensitive=bool(row["sensitive"]),
            tags=_json(row["tags"], []),
            device=row["device"] or "",
            created_at=row["created_at"] or 0.0,
            used_at=row["used_at"] or 0.0,
            use_count=row["use_count"] or 0,
        )
