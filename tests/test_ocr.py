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
    _classify_tesseract_failure,
    build_engine,
    normalize_text,
    resolve_languages,
    select_languages,
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
    """没指定语言时按界面语言挑 `chi_tra`。

    断言的是「想要什么」，不是「最终用了什么」：
    后者要先过滤成**已装**的语言，而这取决于跑测试的机器装没装
    `tesseract-ocr-chi-tra` —— CI 上多数没装，于是 `languages()` 返回空串
    （`tesseract` 一行都没有时可用的语言就是「无」）。
    两件事混在一个断言里，结果就是这条用例只在装了繁体语言包的机器上绿。
    """
    config.set("ocr.languages", "", persist=False)
    config.set("ui.language", "zh_TW", persist=False)
    engine = build_engine(config)
    assert engine.language_choice().wants == "chi_tra"


def test_build_engine_filters_to_installed_languages(config):
    """已装语言里没有繁体时，不能硬把一个装不上的语言串丢给 tesseract。

    与上一条是同一处的两面：`wants` 是用户意图，`languages` 是实际可用。
    """
    config.set("ocr.languages", "", persist=False)
    config.set("ui.language", "zh_TW", persist=False)
    engine = build_engine(config)
    usable = engine.languages()
    if engine.enabled and engine.backend.languages():
        installed = set(engine.backend.languages())
        assert all(code in installed for code in usable.split("+"))
    else:
        # 没装 tesseract：一个都用不了，返回空串而不是「假装能用」
        assert usable == ""


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


# ---------- 语言挑选：想要的 vs 能用的 ----------


def test_resolve_languages_does_not_check_installation():
    """`resolve_languages` 只做名字映射，不关心是否已安装。

    这条是**故意的**：它返回「想要的」，交由 `select_languages` 与已装语言求交。
    两件事混在一起，会让「用户配置了什么」无法单独观察到。
    """
    assert resolve_languages("ja_JP", "") == "jpn"
    assert select_languages(["chi_sim", "eng"], "ja_JP", "").wants == "jpn"


def test_select_languages_keeps_available_ones_in_order():
    """中英混排的 `chi_sim+eng` 不该被拆散，顺序也要保住。"""
    choice = select_languages(["eng", "chi_sim"], "zh_CN", "chi_sim+eng")
    assert choice.languages == "chi_sim+eng"
    assert choice.degraded is False
    assert choice.reason == ""


def test_select_languages_falls_back_to_eng_when_wanted_missing():
    """这是本轮修的核心场景：请求未安装的语言会让 tesseract 整批失败。"""
    choice = select_languages(["chi_sim", "eng"], "ja_JP", "")
    assert choice.wants == "jpn"
    assert choice.languages == "eng"
    assert choice.degraded is True
    assert choice.reason == "fallback"
    assert choice.missing == ("jpn",)


def test_select_languages_drops_only_missing_from_multi_language():
    """部分缺失时保留可用部分，而不是整个回退到 eng。"""
    choice = select_languages(["chi_sim", "eng"], "zh_CN", "chi_sim+jpn")
    assert choice.languages == "chi_sim"
    assert choice.missing == ("jpn",)
    assert choice.degraded is True
    assert choice.reason == "partial"


def test_select_languages_falls_back_to_any_installed_without_eng():
    """没有 eng 时退到某个已装语言，总比请求一个不存在的语言要好。"""
    choice = select_languages(["chi_tra"], "en_US", "")
    assert choice.languages == "chi_tra"
    assert choice.degraded is True


def test_select_languages_ignores_osd_as_content_language():
    """`osd` 只做方向检测，不该被当成正文语言挑出来。"""
    choice = select_languages(["osd"], "en_US", "")
    assert choice.usable is False
    assert choice.reason == "no_language_data"


def test_select_languages_no_data_at_all():
    choice = select_languages([], "zh_CN", "")
    assert choice.usable is False
    assert choice.languages == ""
    assert choice.degraded is False
    assert "语言数据" in choice.describe()


def test_select_languages_describe_mentions_missing():
    choice = select_languages(["eng"], "ja_JP", "")
    text = choice.describe()
    assert "jpn" in text
    assert "eng" in text


def test_select_languages_is_idempotent_on_available_input():
    """已装语言满足期望时不该被改动 —— 现状是「刚好都对」的机器行为不变。"""
    choice = select_languages(["chi_sim", "chi_tra", "eng", "osd"], "zh_TW", "")
    assert choice.languages == "chi_tra"
    assert choice.degraded is False


# ---------- 失败分类 ----------


@pytest.mark.parametrize(
    "stderr,expected",
    [
        (
            "Error opening data file /usr/share/tesseract-ocr/5/tessdata/jpn.traineddata\n"
            "Failed loading language 'jpn'\n"
            "Tesseract couldn't load any languages!",
            "language_unavailable",
        ),
        ("Could not initialize tesseract.", "language_unavailable"),
        ("Failed loading language 'kor'", "language_unavailable"),
        ("some unexpected failure", "unknown"),
        ("", "unknown"),
    ],
)
def test_classify_tesseract_failure(stderr, expected):
    error, detail = _classify_tesseract_failure(1, stderr, "jpn")
    assert error == expected
    assert detail


def test_classify_tesseract_failure_detail_includes_language():
    _error, detail = _classify_tesseract_failure(
        1, "Failed loading language 'jpn'", "jpn"
    )
    assert "jpn" in detail


def test_classify_tesseract_failure_keeps_first_line_only():
    """stderr 可能很长，只留第一行，避免把整段日志塞进元数据。"""
    _error, detail = _classify_tesseract_failure(
        1, "some unexpected failure\nsecond line\nthird", "eng"
    )
    assert "second line" not in detail


# ---------- 引擎：不再请求未安装的语言 ----------


class RecordingBackend:
    """记录每次请求的语言串，用来断言「请求的是什么」。"""

    name = "tesseract"  # 冒充真实后端，才会走 select_languages 分支

    def __init__(self, langs=("eng", "chi_sim"), text="", conf=0.0, error=""):
        self._langs = langs
        self._text = text
        self._conf = conf
        self._error = error
        self.requested = []

    def available(self):
        return True

    def languages(self):
        return self._langs

    def recognize(self, image_path, languages):
        self.requested.append(languages)
        return OcrResult(
            text=self._text,
            confidence=self._conf,
            language=languages,
            backend=self.name,
            error=self._error,
        )


def test_engine_never_requests_uninstalled_language(tmp_path):
    """回归本轮的 bug：只装了 eng 却按 locale 请求 jpn，tesseract 整批失败。"""
    backend = RecordingBackend(langs=("eng",), text="read this")
    engine = OcrEngine(backend=backend, locale_hint="ja_JP")

    result = engine.recognize_full(_image(tmp_path))
    assert backend.requested == ["eng"]
    assert result.text == "read this"
    assert result.error == ""


def test_engine_reports_degradation_in_choice(tmp_path):
    backend = RecordingBackend(langs=("eng",))
    engine = OcrEngine(backend=backend, locale_hint="ja_JP")
    choice = engine.language_choice()
    assert choice.languages == "eng"
    assert choice.degraded is True
    assert engine.languages() == "eng"


def test_engine_skips_call_when_no_language_data(tmp_path):
    """一个语言都没装时不去调 tesseract —— 必然失败，还要白起子进程。"""
    backend = RecordingBackend(langs=("osd",))
    engine = OcrEngine(backend=backend, locale_hint="en_US")

    result = engine.recognize_full(_image(tmp_path))
    assert backend.requested == []
    assert result.error == "no_language_data"
    assert result.failed is True


def test_engine_preserves_error_when_result_is_empty(tmp_path):
    """失败原因不能在「结果为空」这条清理路径上被洗掉。"""
    backend = RecordingBackend(
        langs=("eng",), error="language_unavailable"
    )
    engine = OcrEngine(backend=backend, locale_hint="en_US")

    result = engine.recognize_full(_image(tmp_path))
    assert result.text == ""
    assert result.error == "language_unavailable"
    assert result.failed is True


def test_engine_preserves_error_through_cache(tmp_path):
    backend = RecordingBackend(langs=("eng",), error="timeout")
    engine = OcrEngine(backend=backend, locale_hint="en_US")
    image = _image(tmp_path)

    engine.recognize_full(image)
    cached = engine.recognize_full(image)
    assert backend.requested == ["eng"]  # 命中缓存
    assert cached.error == "timeout"


def test_engine_preserves_error_when_truncating(tmp_path):
    backend = RecordingBackend(langs=("eng",), text="a" * 500, error="")
    engine = OcrEngine(backend=backend, locale_hint="en_US", max_chars=100)
    result = engine.recognize_full(_image(tmp_path))
    assert len(result.text) == 100
    assert result.error == ""


def test_engine_low_confidence_keeps_error(tmp_path):
    backend = RecordingBackend(langs=("eng",), text="noise", conf=5.0)
    engine = OcrEngine(backend=backend, locale_hint="en_US", min_confidence=50.0)
    result = engine.recognize_full(_image(tmp_path))
    assert result.text == ""
    assert result.error == ""


def test_stub_backend_bypasses_language_selection(tmp_path):
    """非 tesseract 后端不提供已装语言，按期望串直接调用即可。"""
    backend = StubBackend({".png": ("ok", 90.0)})
    engine = OcrEngine(backend=backend, locale_hint="zh_TW")
    assert engine.language_choice().languages == "chi_tra"


# ---------- 状态报告 ----------


def test_status_marks_usable_only_when_language_available():
    backend = RecordingBackend(langs=("eng",))
    engine = OcrEngine(backend=backend, locale_hint="ja_JP")
    status = engine.status()
    assert status["enabled"] is True
    assert status["usable"] is True  # 回退到了 eng，能跑
    assert status["languages"] == "eng"
    assert status["requested"] == "jpn"
    assert status["missing"] == ["jpn"]
    assert status["degraded"] is True


def test_status_not_usable_without_language_data():
    backend = RecordingBackend(langs=())
    engine = OcrEngine(backend=backend, locale_hint="en_US")
    status = engine.status()
    assert status["enabled"] is True
    assert status["usable"] is False
    assert status["reason"] == "no_language_data"


def test_status_usable_and_not_degraded_when_all_present():
    backend = RecordingBackend(langs=("chi_sim", "eng"))
    engine = OcrEngine(backend=backend, locale_hint="zh_CN")
    status = engine.status()
    assert status["usable"] is True
    assert status["degraded"] is False
    assert status["languages"] == "chi_sim"


# ---------- 采集管线：失败可诊断 ----------


def test_capture_records_ocr_failure_reason(storage, config, tmp_path):
    """失败原因必须落库，否则「图里没字」与「识别没跑起来」无法区分。"""
    backend = StubBackend({".png": ("", 0.0)})
    engine = OcrEngine(backend=backend, locale_hint="en_US")
    engine.language_choice = lambda: _choice_with_error()  # type: ignore[assignment]
    collector = Collector(storage, config, ocr=engine)

    item = ClipboardItem(kind="image", image_path=str(_image(tmp_path)), hash="img-fail")
    outcome, saved = collector.process(item)

    assert outcome == CAPTURED
    assert saved.content == ""
    assert saved.metadata["ocr"]["error"] == "no_language_data"


def _choice_with_error():
    from linglong_clip.core.ocr import LanguageChoice

    return LanguageChoice(reason="no_language_data")


def test_capture_does_not_record_error_for_blank_image(storage, config, tmp_path):
    """图里本来就没文字时不该留下 error —— 那是正常结果，不是故障。"""
    backend = StubBackend({".png": ("", 0.0)})
    collector = _collector(storage, config, backend)

    item = ClipboardItem(kind="image", image_path=str(_image(tmp_path)), hash="img-blank")
    _outcome, saved = collector.process(item)

    assert "error" not in saved.metadata["ocr"]


def test_capture_keeps_error_when_backend_fails(storage, config, tmp_path):
    backend = RecordingBackend(langs=("eng",), error="language_unavailable")
    collector = Collector(
        storage, config, ocr=OcrEngine(backend=backend, locale_hint="en_US")
    )
    item = ClipboardItem(kind="image", image_path=str(_image(tmp_path)), hash="img-err")
    _outcome, saved = collector.process(item)

    entry = saved.metadata["ocr"]
    assert entry["error"] == "language_unavailable"
    assert "error_detail" in entry


def test_capture_records_exception_as_error(storage, config, tmp_path):
    class BoomBackend(StubBackend):
        def recognize(self, image_path, languages):
            raise RuntimeError("子进程炸了")

    collector = _collector(storage, config, BoomBackend())
    item = ClipboardItem(kind="image", image_path=str(_image(tmp_path)), hash="img-boom")
    outcome, saved = collector.process(item)

    # OCR 是增强能力，炸了不能影响采集本身
    assert outcome == CAPTURED
    assert saved.metadata["ocr"]["error"] == "exception"


# ---------- 真实 tesseract：只有装了才跑 ----------

_needs_tesseract = pytest.mark.skipif(
    not shutil.which("tesseract"), reason="本机未安装 tesseract"
)


@_needs_tesseract
def test_real_backend_lists_languages():
    langs = TesseractBackend().languages()
    assert isinstance(langs, tuple)
    assert all(isinstance(lang, str) for lang in langs)


@_needs_tesseract
def test_real_backend_reports_language_unavailable():
    """真实 tesseract 上验证失败分类：请求一个必然不存在的语言。"""
    backend = TesseractBackend()
    image = Path("__definitely_not_a_language__.png")
    image.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    try:
        result = backend.recognize(image, "xx_not_a_lang")
    finally:
        image.unlink()
    assert result.text == ""
    assert result.failed is True
    assert result.error == "language_unavailable"


@_needs_tesseract
def test_real_backend_languages_are_cached():
    """`--list-langs` 不该每次识别都重查一遍。"""
    backend = TesseractBackend()
    first = backend.languages()
    assert backend.languages() is first
    assert isinstance(backend.refresh_languages(), tuple)


@_needs_tesseract
def test_real_engine_never_fails_on_locale_without_data(tmp_path):
    """真实环境下确认：locale 语言没装时不再是「一张也识别不出来」。"""
    backend = TesseractBackend()
    if "jpn" in backend.languages():
        pytest.skip("本机装了 jpn，构造不出降级场景")
    engine = OcrEngine(backend=backend, locale_hint="ja_JP")
    assert engine.language_choice().languages != "jpn"
    assert engine.language_choice().usable is True


def test_language_choice_describe_partial_and_fallback():
    from linglong_clip.core.ocr import LanguageChoice

    partial = LanguageChoice(
        languages="chi_sim", wants="chi_sim+jpn", missing=("jpn",),
        degraded=True, reason="partial",
    )
    assert "jpn" in partial.describe() and "部分" in partial.describe()

    fallback = LanguageChoice(
        languages="eng", wants="jpn", missing=("jpn",), degraded=True, reason="fallback",
    )
    assert "jpn" in fallback.describe() and "eng" in fallback.describe()


def test_language_choice_describe_ok_case():
    from linglong_clip.core.ocr import LanguageChoice

    choice = LanguageChoice(languages="eng", wants="eng")
    assert choice.describe() == "按 eng 识别"


def test_engine_handles_backend_languages_probe_exception():
    """后端查已装语言时抛异常，不该把识别整个带崩，按「没有语言」处理。"""

    class Fragile(RecordingBackend):
        def languages(self):
            raise RuntimeError("--list-langs 炸了")

    engine = OcrEngine(backend=Fragile(langs=("eng",)), locale_hint="en_US")
    result = engine.recognize_full(_image(Path("/tmp")))
    assert result.error == "no_language_data"
    assert result.failed is True


def test_engine_language_choice_with_custom_backend_keeps_request():
    """非 tesseract 后端不提供已装语言列表，沿用期望串即可（不擅自改写）。"""
    engine = OcrEngine(backend=NullBackend(), locale_hint="zh_CN")
    choice = engine.language_choice()
    assert choice.languages == "chi_sim"
    assert choice.degraded is False


def test_map_language_region_suffix_falls_back_to_base():
    """`de_DE` 的 base `de` 已在映射表里，应命中 deu。"""
    assert resolve_languages("de_DE", "") == "deu"
    # 未知地区后缀退回主语言本身
    assert resolve_languages("pt_BR", "") == "pt"


def test_real_backend_returns_empty_when_no_executable():
    backend = TesseractBackend(executable="__no_such_tesseract__")
    assert backend.languages() == ()
    result = backend.recognize(Path("/tmp/x.png"), "eng")
    assert result.error == "executable_missing"
