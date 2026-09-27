"""OCR：语言解析、降级、缓存，以及采集管线里的接入行为。

测试不依赖系统是否装了 tesseract：默认用桩后端，
只在检测到真实可执行文件时才补一组真机用例。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from linglong_clip.core.collector import CAPTURED, SKIPPED_PRIVACY, Collector
from linglong_clip.core.models import ClipboardItem
from linglong_clip.core.ocr import (
    NullBackend,
    OcrEngine,
    OcrResult,
    TesseractBackend,
    build_engine,
    normalize_text,
    resolve_languages,
)


# ---------- 语言解析 ----------


@pytest.mark.parametrize(
    "locale,requested,expected",
    [
        ("zh_CN", "", "chi_sim"),
        ("zh_TW", "", "chi_tra"),
        ("en_US", "", "eng"),
        ("", "", "eng"),
        ("zh-Hans", "", "chi_sim"),
        ("ja_JP", "", "jpn"),
        ("de_DE", "", "deu"),
        ("pt_BR", "", "pt"),
        # 显式配置优先：用户指定语言时不再叠加系统语言
        ("zh_CN", "eng", "eng"),
        ("en_US", "chi_sim+eng", "chi_sim+eng"),
        ("", "zh-TW,eng", "chi_tra+eng"),
        ("zh_CN", "unknown_lang", "unknown"),
    ],
)
def test_resolve_languages(locale, requested, expected):
    assert resolve_languages(locale, requested) == expected


def test_resolve_languages_dedupes():
    assert resolve_languages("zh_CN", "chi_sim") == "chi_sim"


# ---------- 文本规整 ----------


def test_normalize_text_collapses_and_trims():
    raw = "  hello   world \n\n\tfoo  bar  \n\n"
    assert normalize_text(raw) == "hello world\nfoo bar"


def test_normalize_text_empty():
    assert normalize_text("\n\n   \t\n") == ""


# ---------- 桩后端 ----------


class StubBackend:
    """可控后端：按路径后缀决定识别结果，用于测试引擎行为。"""

    name = "stub"

    def __init__(self, mapping=None, available=True, langs=("eng", "chi_sim")):
        self.mapping = mapping or {}
        self._available = available
        self._langs = langs
        self.calls = 0

    def available(self):
        return self._available

    def languages(self):
        return self._langs

    def recognize(self, image_path, languages):
        self.calls += 1
        text, conf = self.mapping.get(Path(image_path).suffix, ("", 0.0))
        return OcrResult(text=text, confidence=conf, language=languages, backend=self.name)


def _image(tmp_path: Path, name: str = "shot.png") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    return path


# ---------- 引擎 ----------


def test_engine_disabled_without_backend():
    engine = OcrEngine(backend=NullBackend())
    assert engine.enabled is False
    assert engine.recognize_full("/nonexistent.png").text == ""
    assert engine.recognize("/nonexistent.png") is None


def test_engine_recognizes_and_caches(tmp_path):
    backend = StubBackend({".png": ("hello world", 88.0)})
    engine = OcrEngine(backend=backend, locale_hint="en_US")
    image = _image(tmp_path)

    first = engine.recognize_full(image)
    assert first.text == "hello world"
    assert first.confidence == 88.0
    assert first.backend == "stub"

    # 同一文件第二次应命中缓存，不再调用后端
    engine.recognize_full(image)
    assert backend.calls == 1
    assert engine.recognize(image) == "hello world"


def test_engine_skips_missing_file(tmp_path):
    backend = StubBackend({".png": ("x", 90.0)})
    engine = OcrEngine(backend=backend)
    assert engine.recognize_full(tmp_path / "ghost.png").text == ""
    assert backend.calls == 0


def test_engine_filters_low_confidence(tmp_path):
    backend = StubBackend({".png": ("noise", 10.0)})
    engine = OcrEngine(backend=backend, min_confidence=50.0)
    assert engine.recognize_full(_image(tmp_path)).text == ""


def test_engine_truncates_long_text(tmp_path):
    backend = StubBackend({".png": ("a" * 500, 90.0)})
    engine = OcrEngine(backend=backend, max_chars=100)
    assert len(engine.recognize_full(_image(tmp_path)).text) == 100


def test_engine_cache_eviction(tmp_path):
    backend = StubBackend({".png": ("same", 90.0)})
    engine = OcrEngine(backend=backend, cache_size=2)
    for name in ("a.png", "b.png", "c.png", "d.png"):
        engine.recognize_full(_image(tmp_path, name))
    engine.clear_cache()
    assert engine._cache == {}


# ---------- 配置构造 ----------


def test_build_engine_disabled_by_config(config):
    config.set("ocr.enabled", False, persist=False)
    engine = build_engine(config)
    assert engine.enabled is False


def test_build_engine_uses_locale(config):
    config.set("ocr.languages", "", persist=False)
    config.set("ui.language", "zh_TW", persist=False)
    engine = build_engine(config)
    assert engine.languages() == "chi_tra"


def test_build_engine_without_config_is_disabled():
    assert build_engine(None).enabled is False


# ---------- 采集管线接入 ----------


def _collector(storage, config, backend):
    return Collector(storage, config, ocr=OcrEngine(backend=backend, locale_hint="en_US"))


def test_image_capture_stores_ocr_text(storage, config, tmp_path):
    backend = StubBackend({".png": ("weekly report draft", 92.0)})
    collector = _collector(storage, config, backend)

    item = ClipboardItem(kind="image", image_path=str(_image(tmp_path)), hash="img-1")
    outcome, saved = collector.process(item)

    assert outcome == CAPTURED
    assert saved.kind == "image"
    assert saved.content == "weekly report draft"
    assert saved.preview == "[图片] weekly report draft"
    assert saved.metadata["ocr"]["confidence"] == 92.0
    # 识别文本进入 FTS，因此能被文字检索命中
    assert [hit.hash for hit in storage.search("weekly")] == ["img-1"]


def test_image_capture_blocked_when_ocr_text_is_sensitive(storage, config, tmp_path):
    backend = StubBackend({".png": ("token: sk-abcdefghijklmnopqrstuvwxyz", 95.0)})
    collector = _collector(storage, config, backend)

    item = ClipboardItem(kind="image", image_path=str(_image(tmp_path)), hash="img-2")
    outcome, saved = collector.process(item)

    # 截图里是密钥时，与文本一样按隐私策略丢弃
    assert outcome == SKIPPED_PRIVACY
    assert saved is None
    assert storage.count() == 0


def test_image_capture_without_ocr_still_works(storage, config, tmp_path):
    collector = Collector(storage, config, ocr=OcrEngine(backend=NullBackend()))

    item = ClipboardItem(kind="image", image_path=str(_image(tmp_path)), hash="img-3")
    outcome, saved = collector.process(item)

    assert outcome == CAPTURED
    assert saved.content == ""
    assert saved.preview == "[图片]"


def test_ocr_failure_does_not_break_capture(storage, config, tmp_path):
    class ExplodingBackend(StubBackend):
        def recognize(self, image_path, languages):
            raise RuntimeError("boom")

    collector = _collector(storage, config, ExplodingBackend())
    item = ClipboardItem(kind="image", image_path=str(_image(tmp_path)), hash="img-4")
    outcome, saved = collector.process(item)

    assert outcome == CAPTURED
    assert saved.preview == "[图片]"


def test_text_items_are_not_ocr_ed(storage, config, tmp_path):
    backend = StubBackend({".png": ("should not run", 90.0)})
    collector = _collector(storage, config, backend)

    collector.process(ClipboardItem(kind="text", content="纯文本", hash="t-1"))
    assert backend.calls == 0


def test_existing_content_short_circuits_ocr(storage, config, tmp_path):
    backend = StubBackend({".png": ("from ocr", 90.0)})
    collector = _collector(storage, config, backend)

    item = ClipboardItem(
        kind="image", image_path=str(_image(tmp_path)), content="已带文本", hash="img-5"
    )
    _, saved = collector.process(item)
    assert saved.content == "已带文本"
    assert backend.calls == 0


# ---------- 真机用例（仅在装了 tesseract 时跑） ----------

_HAS_TESSERACT = shutil.which("tesseract") is not None


@pytest.mark.skipif(not _HAS_TESSERACT, reason="未安装 tesseract")
def test_tesseract_backend_reports_available():
    backend = TesseractBackend()
    assert backend.available() is True
    assert "eng" in backend.languages()


@pytest.mark.skipif(not _HAS_TESSERACT, reason="未安装 tesseract")
def test_tesseract_reads_rendered_text(tmp_path):
    """渲染一张图再识别，验证真实链路（含置信度解析）。"""
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw, ImageFont

    font = None
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        if Path(candidate).exists():
            font = ImageFont.truetype(candidate, 34)
            break
    if font is None:
        pytest.skip("缺少可用的 TrueType 字体")

    image = Image.new("RGB", (720, 90), "white")
    ImageDraw.Draw(image).text((20, 24), "Hello Linglong OCR 2024", fill="black", font=font)
    target = tmp_path / "rendered.png"
    image.save(target)

    engine = OcrEngine(backend=TesseractBackend(), locale_hint="en_US")
    result = engine.recognize_full(target)

    assert "Linglong" in result.text
    assert result.confidence > 50


@pytest.mark.skipif(not _HAS_TESSERACT, reason="未安装 tesseract")
def test_tesseract_missing_executable_degrades():
    backend = TesseractBackend(executable="definitely-not-a-real-binary")
    assert backend.available() is False
    assert backend.recognize(Path("/tmp/whatever.png"), "eng").is_empty
