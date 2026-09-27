"""采集管线：识别 → 风控 → 去重 → 落库 → 淘汰。

单独成层的好处是每一步都可独立测试，且同步模块能复用同一套
识别/风控逻辑，避免两条路径判定不一致。
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from ..platform.base import FocusInfo
from ..utils.logger import get_logger
from . import classify
from .models import ClipboardItem, content_hash
from .ocr import OcrEngine
from .privacy import PrivacyGuard
from .storage import Storage

_log = get_logger("collector")

#: 采集结果，供 UI 展示「刚刚记下了什么」
CaptureOutcome = str
CAPTURED = "captured"
MERGED = "merged"
SKIPPED_DUPLICATE = "skipped_duplicate"
SKIPPED_EMPTY = "skipped_empty"
SKIPPED_PRIVACY = "skipped_privacy"
SKIPPED_TYPE = "skipped_type"


class Collector:
    """把一次剪贴板读取变成一条历史记录。"""

    def __init__(
        self,
        storage: Storage,
        config,
        guard: Optional[PrivacyGuard] = None,
        *,
        device_name: str = "",
        on_captured: Optional[Callable[[ClipboardItem, str], None]] = None,
        ocr: Optional[OcrEngine] = None,
    ) -> None:
        self._storage = storage
        self._config = config
        self._guard = guard or PrivacyGuard(config)
        self._device_name = device_name
        self._on_captured = on_captured
        self._ocr = ocr
        self._lock = threading.RLock()

    def set_ocr(self, engine: Optional[OcrEngine]) -> None:
        """热替换 OCR 引擎，供设置变更后即时生效。"""
        with self._lock:
            self._ocr = engine

    # ---- 主入口 ----

    def process(self, item: ClipboardItem, focus: Optional[FocusInfo] = None) -> tuple:
        """处理一条剪贴板内容，返回 `(结果常量, 记录或 None)`。"""
        if item is None or item.is_empty:
            return SKIPPED_EMPTY, None

        if not self._config.get("capture.enabled", True):
            return SKIPPED_TYPE, None

        self._apply_source(item, focus)
        self._enrich(item)

        if not self._is_type_enabled(item.kind) and item.kind != "sensitive":
            return SKIPPED_TYPE, None

        verdict = self._guard.inspect(item)
        if not verdict.allowed:
            _log.info("内容被隐私策略拦截：%s", verdict.summary)
            return SKIPPED_PRIVACY, None
        if verdict.sensitive:
            item.sensitive = True
            if not self._guard.should_keep_sensitive():
                # 敏感内容不落盘：既不进历史，也不参与同步
                _log.info("敏感内容不入库：%s", verdict.summary)
                return SKIPPED_PRIVACY, None

        item.hash = item.hash or content_hash(item.kind, self._hash_payload(item))
        item.tags = list(
            dict.fromkeys(item.tags + classify.extract_tags(item.content, kind=item.kind))
        )
        item.device = item.device or self._device_name
        if not item.preview:
            item.preview = classify.make_preview(item.kind, item.content, files=item.files)
        if not item.size:
            item.size = self._estimate_size(item)

        with self._lock:
            return self._persist(item)

    def _persist(self, item: ClipboardItem) -> tuple:
        existing = self._storage.find_by_hash(item.hash) if item.hash else None
        if existing is not None:
            if self._config.get("history.merge_duplicates", True):
                self._storage.touch(existing.id)
                # 重复内容也要更新来源：同一段文本可能被不同应用复制过
                self._storage.update_fields(
                    existing.id,
                    metadata={
                        **existing.metadata,
                        "last_source": item.source_app,
                        "copies": int(existing.metadata.get("copies", 1)) + 1,
                    },
                )
                merged = self._storage.get(existing.id)
                self._emit(merged, MERGED)
                return MERGED, merged
            return SKIPPED_DUPLICATE, existing

        saved = self._storage.add(item)
        self._enforce_limits()
        self._emit(saved, CAPTURED)
        return CAPTURED, saved

    # ---- 辅助 ----

    def _apply_source(self, item: ClipboardItem, focus: Optional[FocusInfo]) -> None:
        if focus is None or focus.is_empty:
            return
        item.source_app = item.source_app or focus.app_id or focus.app_name
        item.source_title = item.source_title or focus.window_title

    def _enrich(self, item: ClipboardItem) -> None:
        """补齐类型、摘要、标签等派生字段。"""
        # OCR 必须在 analyze 与隐私判定之前跑：识别出的文字要进入 content，
        # 才能被 FTS 检索到，也才能让「截图里是 API Key」这种情况被拦下。
        self._ocr_enrich(item)

        analysis = classify.analyze(
            item.content,
            has_html=bool(item.html),
            has_image=bool(item.image_path),
            has_files=bool(item.files),
        )
        # 采集层已经明确知道类型时，不让启发式结论覆盖
        if item.kind in ("", "text") or item.kind is None:
            item.kind = str(analysis["kind"])
        if not item.preview:
            item.preview = classify.make_preview(item.kind, item.content, files=item.files)
        if analysis["sensitive"]:
            item.sensitive = True
            item.metadata.setdefault(
                "sensitive_reasons", list(analysis["sensitive_reasons"])
            )
        tags = list(analysis["tags"])
        if item.kind == "link":
            parts = classify.summarize_url(item.content)
            if parts.get("host"):
                item.metadata.setdefault("host", parts["host"])
                if classify.is_private_host(parts["host"]):
                    item.metadata.setdefault("private_host", True)
        item.tags = list(dict.fromkeys(item.tags + tags))

    def _ocr_enrich(self, item: ClipboardItem) -> None:
        """对截图做文字识别，把结果写进 `content` 供检索使用。

        没有 OCR 引擎、图片不存在或识别为空时静默跳过，
        图片记录本身照常入库，只是搜不到文字。
        """
        if item.kind != "image" or item.content or not item.image_path:
            return
        engine = self._ocr
        if engine is None or not engine.enabled:
            return

        try:
            result = engine.recognize_full(item.image_path)
        except Exception as exc:  # noqa: BLE001 - OCR 是增强能力，不能影响采集
            _log.debug("OCR 失败，跳过图片文字识别：%s", exc)
            return

        if result.is_empty:
            item.metadata.setdefault("ocr", {"text": "", "backend": result.backend})
            return

        item.content = result.text
        item.metadata["ocr"] = {
            "text": result.text,
            "backend": result.backend,
            "language": result.language,
            "confidence": result.confidence,
            "duration_ms": result.duration_ms,
        }

    @staticmethod
    def _hash_payload(item: ClipboardItem) -> str:
        if item.kind == "image" and item.hash:
            return item.hash
        if item.kind == "file" and item.files:
            return "\n".join(item.files)
        return item.content

    @staticmethod
    def _estimate_size(item: ClipboardItem) -> int:
        if item.kind == "image" and item.image_path:
            try:
                from pathlib import Path

                return Path(item.image_path).stat().st_size
            except OSError:
                return 0
        return len((item.content or "").encode("utf-8"))

    def _is_type_enabled(self, kind: str) -> bool:
        enabled = self._config.get("capture.types", None)
        if not enabled:
            return True
        return kind in enabled

    def _enforce_limits(self) -> None:
        max_items = int(self._config.get("history.max_items", 0) or 0)
        max_days = int(self._config.get("history.max_days", 0) or 0)
        if max_items <= 0 and max_days <= 0:
            return
        try:
            removed = self._storage.enforce_limits(max_items, max_days)
            if removed:
                _log.debug("淘汰 %d 条历史", removed)
        except Exception:  # noqa: BLE001
            _log.exception("历史淘汰失败")

    def purge_sensitive(self) -> int:
        """清除超期敏感记录。由定时器周期调用。"""
        ttl = int(self._config.get("privacy.sensitive_retention_seconds", 0) or 0)
        if ttl <= 0:
            return 0
        removed = self._storage.purge_expired_sensitive(ttl)
        if removed:
            _log.info("清除 %d 条超期敏感记录", removed)
        return removed

    def _emit(self, item: Optional[ClipboardItem], outcome: str) -> None:
        if item is None or self._on_captured is None:
            return
        try:
            self._on_captured(item, outcome)
        except Exception:  # noqa: BLE001
            _log.exception("采集通知回调失败")
