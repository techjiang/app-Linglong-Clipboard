"""图片文字识别（OCR）：让截图也能被文字搜到。

设计取舍：
- **走 tesseract 命令行，不引入 Python 绑定**。`pytesseract` 只是 CLI 的薄封装，
  多一层依赖却换不来功能；直接 `subprocess` 调用反而更容易控制超时与降级。
  也不选 PaddleOCR/EasyOCR：它们要拖进数百 MB 模型与深度学习运行时，
  而剪贴板 OCR 是低频后台任务，精度够用即可。
- **识别结果写回 `item.content`**，因此自动进入既有的 FTS5 索引，
  搜索路径完全不用改；`kind` 仍是 `image`，类型筛选不受影响。
- **后端可插拔**。`OcrBackend` 是协议，默认给 tesseract 实现与降级实现；
  测试可注入桩后端，不依赖系统是否装了 tesseract。
- **失败一律降级，不抛异常到采集管线**。OCR 只是增强能力，
  缺依赖或识别失败时截图照常入库，只是搜不到文字。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

from ..utils.logger import get_logger

_log = get_logger("ocr")

#: 单张图片识别的默认超时（秒）。剪贴板截图通常很小，超过说明出了问题。
DEFAULT_TIMEOUT = 8.0

#: tesseract 的 `--psm`：6 = 假定为单个统一文本块，对截图最稳。
DEFAULT_PSM = 6

#: 会话/页面切分的语言代码，用于把 `zh_CN` 之类映射成 tesseract 的语言串。
_LANG_ALIASES: Dict[str, str] = {
    "zh": "chi_sim",
    "zh_cn": "chi_sim",
    "zh_sim": "chi_sim",
    "zh_hans": "chi_sim",
    "zh_tw": "chi_tra",
    "zh_hk": "chi_tra",
    "zh_tra": "chi_tra",
    "en": "eng",
    "ja": "jpn",
    "ko": "kor",
    "fr": "fra",
    "de": "deu",
    "es": "spa",
    "ru": "rus",
}


@dataclass
class OcrResult:
    """一次识别的结论。"""

    text: str = ""
    #: 平均置信度（0-100），来自 tesseract 的 TSV 输出；不可得时为 0
    confidence: float = 0.0
    language: str = ""
    backend: str = ""
    duration_ms: int = 0
    #: 失败原因。`""` 表示没出错（可能是图里本来就没文字）。
    #: 区分「没识别到文字」与「识别根本没跑起来」—— 前者是正常结果，
    #: 后者需要用户处理（缺语言数据、缺可执行文件、超时…）。
    error: str = ""
    error_detail: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()

    @property
    def failed(self) -> bool:
        """识别过程本身出错（区别于「图里没有文字」）。"""
        return bool(self.error)


class OcrBackend(Protocol):
    """OCR 后端协议。实现只要保证「不抛异常、失败返回空结果」即可。"""

    name: str

    def available(self) -> bool:  # pragma: no cover - 协议声明
        ...

    def languages(self) -> Sequence[str]:  # pragma: no cover - 协议声明
        ...

    def recognize(self, image_path: Path, languages: str) -> OcrResult:  # pragma: no cover
        ...


class NullBackend:
    """降级后端：永远返回空文本，让上层逻辑无分支。"""

    name = "null"

    def available(self) -> bool:
        return False

    def languages(self) -> Sequence[str]:
        return ()

    def recognize(self, image_path: Path, languages: str) -> OcrResult:
        return OcrResult(backend=self.name, language=languages)


class TesseractBackend:
    """调用系统 `tesseract` 可执行文件识图。"""

    name = "tesseract"

    def __init__(self, executable: str = "tesseract", timeout: float = DEFAULT_TIMEOUT) -> None:
        self.executable = executable
        self.timeout = timeout
        self._locked = threading.Lock()
        #: `--list-langs` 的结果缓存。每次识别前都查一次会白起一个进程，
        #: 而语言数据在一次会话里不会变。`None` 表示还没查过。
        self._langs_cache: Optional[Tuple[str, ...]] = None

    # ---- 探测 ----

    def path(self) -> Optional[str]:
        found = shutil.which(self.executable)
        return found or None

    def available(self) -> bool:
        return self.path() is not None

    def languages(self) -> Sequence[str]:
        """列出已安装的语言数据，用于挑选实际可用的语言串。

        结果在实例内缓存：语言数据不会在会话中途变化，而每次识别都起一个
        子进程去查是纯浪费。需要刷新时调 `refresh_languages()`。
        """
        with self._locked:
            cached = self._langs_cache
        if cached is not None:
            return cached
        return self.refresh_languages()

    def refresh_languages(self) -> Sequence[str]:
        """强制重新查询已安装语言（用户刚装完语言包时用）。"""
        exe = self.path()
        if exe is None:
            with self._locked:
                self._langs_cache = ()
            return ()
        self._langs_cache = self._query_languages(exe)
        return self._langs_cache

    def _query_languages(self, exe: str) -> Tuple[str, ...]:
        try:
            proc = subprocess.run(
                [exe, "--list-langs"],
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _log.debug("查询 tesseract 语言失败：%s", exc)
            return ()
        out = (proc.stdout or b"").decode("utf-8", "replace")
        langs: List[str] = []
        for line in out.splitlines():
            line = line.strip()
            # 第一行是 "List of available languages ..."，跳过
            if not line or line.lower().startswith("list of"):
                continue
            langs.append(line)
        return tuple(langs)

    # ---- 识别 ----

    def recognize(self, image_path: Path, languages: str) -> OcrResult:
        exe = self.path()
        if exe is None:
            return OcrResult(
                backend=self.name,
                language=languages,
                error="executable_missing",
                error_detail=f"未找到可执行文件 {self.executable}",
            )

        args = [exe, str(image_path), "stdout", "-l", languages or "eng", "--psm", str(DEFAULT_PSM)]
        # TSV 输出带置信度，且 tesseract 保证 stdout 是文本，stderr 只放日志
        tsv_args = args[:-2] + ["--psm", str(DEFAULT_PSM), "tsv"]

        import time

        started = time.monotonic()
        try:
            # tesseract 不是线程安全的（内部有共享状态），串行化保护。
            with self._locked:
                proc = subprocess.run(
                    args, capture_output=True, timeout=self.timeout, check=False
                )
                tsv = subprocess.run(
                    tsv_args, capture_output=True, timeout=self.timeout, check=False
                )
        except subprocess.TimeoutExpired:
            _log.warning("OCR 超时（%ss）：%s", self.timeout, image_path.name)
            return OcrResult(
                backend=self.name,
                language=languages,
                error="timeout",
                error_detail=f"识别超时（>{self.timeout}s）",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _log.debug("OCR 调用失败：%s", exc)
            return OcrResult(
                backend=self.name,
                language=languages,
                error="invoke_failed",
                error_detail=str(exc),
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        text = (proc.stdout or b"").decode("utf-8", "replace")
        stderr = (proc.stderr or b"").decode("utf-8", "replace")

        # 退出码非 0 且没有输出，说明识别过程本身失败，而不是「图里没文字」。
        # 最常见的原因是语言数据缺失 —— tesseract 遇到任何一个未安装的语言
        # 就整批放弃，stdout 同样为空。不区分这两种情况的话，用户只会看到
        # 「截图搜不到」却完全不知道是语言没装。
        if proc.returncode != 0 and not text.strip():
            error, detail = _classify_tesseract_failure(proc.returncode, stderr, languages)
            _log.warning("OCR 失败（%s）：%s（语言 %s）", error, detail, languages)
            return OcrResult(
                backend=self.name,
                language=languages,
                duration_ms=duration_ms,
                error=error,
                error_detail=detail,
            )

        confidence = _parse_confidence((tsv.stdout or b"").decode("utf-8", "replace"))
        return OcrResult(
            text=normalize_text(text),
            confidence=confidence,
            language=languages,
            backend=self.name,
            duration_ms=duration_ms,
            # stderr 有内容但退出码为 0 时，仍留下线索供排查（不算失败）
            error_detail=stderr.strip() if stderr.strip() else "",
        )


#: tesseract stderr 里表示「语言数据加载失败」的特征串
_LANG_FAILURE_RE = re.compile(
    r"(Failed loading language|Error opening data file|"
    r"Could not initialize tesseract|Tesseract couldn't load any languages)",
    re.I,
)


def _classify_tesseract_failure(
    returncode: int, stderr: str, languages: str
) -> Tuple[str, str]:
    """把 tesseract 的失败归到具体原因，便于给出可执行的提示。

    只做字符串判断，不起额外进程、不读文件系统。
    """
    text = (stderr or "").strip()
    first_line = text.splitlines()[0].strip() if text else ""
    if _LANG_FAILURE_RE.search(text):
        return (
            "language_unavailable",
            f"语言数据不可用（请求 {languages}）：{first_line or 'tesseract 无法加载语言'}",
        )
    if not text:
        return "unknown", f"tesseract 退出码 {returncode} 且无输出"
    return "unknown", first_line


def _parse_confidence(tsv: str) -> float:
    """从 tesseract 的 TSV 输出算平均置信度，忽略表头与 `-1` 噪音行。"""
    values: List[float] = []
    for line in tsv.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 11:
            continue
        raw = parts[10].strip()
        if not raw or raw == "-1":
            continue
        try:
            score = float(raw)
        except ValueError:
            continue
        if score >= 0:
            values.append(score)
    if not values:
        return 0.0
    return round(sum(values) / len(values), 2)


#: tesseract 用 `+` 连接多语言，这里把 `+`、逗号与空白都当作分隔符
_LANG_SEP = re.compile(r"[,\s+]+")


def resolve_languages(locale_hint: str = "", requested: str = "") -> str:
    """把用户配置或系统语言映射成 tesseract 认识的语言串。

    多个语言用 `+` 连接（tesseract 语法），无法识别时退回 `eng`。

    .. note::
       这里只做**语言名映射，不关心是否已安装**。直接把结果交给 tesseract 是
       危险的：只要串里有一个语言的数据文件不存在，tesseract 会**整批失败**
       （`Failed loading language`、退出码 1、stdout 为空），而不是忽略该语言。
       也就是说 `chi_sim+jpn` 在只装了 `chi_sim` 的机器上等于**一个都识别不了**。
       需要真正可用的语言串时请用 :func:`select_languages`。
    """
    # 显式配置优先：用户写了语言就不再叠加系统语言，避免识别串变得又长又慢
    for source in (requested, locale_hint):
        tokens: List[str] = []
        for token in _LANG_SEP.split(source or ""):
            mapped = _map_language(token)
            if mapped and mapped not in tokens:
                tokens.append(mapped)
        if tokens:
            return "+".join(tokens)
    return "eng"


def select_languages(
    installed: Sequence[str],
    locale_hint: str = "",
    requested: str = "",
) -> "LanguageChoice":
    """挑出**实际可用**的语言串，并说明是否发生了降级。

    `resolve_languages()` 的结果只是「想要的」，这里拿已安装语言做交集，
    得到「能用的」。两者不一致时必须降级，否则 tesseract 整批失败、
    截图一条都识别不了 —— 而这在界面上看不出任何异常。

    选择顺序：

    1. 想要的语言里，已安装的部分（保序保留，`chi_sim+eng` 这种中英混排不被拆散）
    2. 想要的全都不可用时，退回 `eng`（tesseract 的默认语言，绝大多数发行版随包装上）
    3. `eng` 也没有时，退回任意一个已安装语言
    4. 一个都没装 —— 此时没有可用语言，返回空串并说明原因
    """
    wanted = [tok for tok in _LANG_SEP.split(resolve_languages(locale_hint, requested)) if tok]
    available = [str(lang) for lang in installed if lang]
    # `osd` 只用于方向检测，不是可识别的正文语言，不该被挑出来当兜底
    usable = [lang for lang in available if lang != "osd"]

    if not usable:
        return LanguageChoice(
            languages="",
            wants="+".join(wanted),
            missing=tuple(wanted),
            degraded=False,
            reason="no_language_data",
        )

    kept = [lang for lang in wanted if lang in usable]
    if kept and len(kept) == len(wanted):
        return LanguageChoice(languages="+".join(kept), wants="+".join(wanted))

    missing = tuple(lang for lang in wanted if lang not in usable)
    if kept:
        # 部分可用：只丢掉缺数据的那些，保住其余语言
        return LanguageChoice(
            languages="+".join(kept),
            wants="+".join(wanted),
            missing=missing,
            degraded=True,
            reason="partial",
        )

    # 想要的全都不可用。优先 eng：它是 tesseract 的默认值，也最可能已安装
    fallback = "eng" if "eng" in usable else usable[0]
    return LanguageChoice(
        languages=fallback,
        wants="+".join(wanted),
        missing=missing,
        degraded=True,
        reason="fallback",
    )


@dataclass(frozen=True)
class LanguageChoice:
    """语言挑选结论。

    存在的意义是把「降级」这件事**显式化**：调用方不该拿到一个字符串却
    不知道它是否等于用户想要的。
    """

    languages: str = ""
    #: 用户配置/系统语言推断出的「想要的」语言串
    wants: str = ""
    #: 想要的但没有语言数据的
    missing: Tuple[str, ...] = ()
    degraded: bool = False
    #: `""` / `partial` / `fallback` / `no_language_data`
    reason: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.languages)

    def describe(self) -> str:
        if not self.usable:
            return "无可用的语言数据（tesseract 一个语言都没装）"
        if self.reason == "no_language_data":
            return "无可用的语言数据"
        if self.reason == "partial":
            return f"部分语言未安装（缺 {'、'.join(self.missing)}），已按可用的识别"
        if self.reason == "fallback":
            missing = "、".join(self.missing) or self.wants or "未知"
            return f"缺少 {'、'.join(self.missing) if self.missing else missing} 的数据，已回退到 {self.languages}"
        return f"按 {self.languages} 识别"


#: tesseract 自带语言名，出现在这里说明已经是最终形态，不再二次映射
_TESSERACT_LANGS = frozenset(
    {
        "eng", "chi_sim", "chi_tra", "jpn", "kor", "fra", "deu", "spa", "rus",
        "ita", "por", "nld", "pol", "tur", "ara", "hin", "tha", "vie", "osd",
        "equ", "frk", "deu_latf", "script_latin", "script_han",
    }
)


def _map_language(token: str) -> str:
    """把单个语言标记映射为 tesseract 语言名，未知标记原样返回。"""
    token = token.strip().lower().replace("-", "_")
    if not token:
        return ""
    if token in _TESSERACT_LANGS:
        return token
    direct = _LANG_ALIASES.get(token)
    if direct:
        return direct
    # `en_US` / `de_DE` 这类带地区后缀的，退回主语言再查一次
    base = token.split("_", 1)[0]
    if base in _TESSERACT_LANGS:
        return base
    return _LANG_ALIASES.get(base, base)


_WHITESPACE_RUN = re.compile(r"[ \t\u00a0]{2,}")


def normalize_text(text: str) -> str:
    """规整 OCR 输出：去掉逐行零散空白，压掉多空白，裁剪首尾。"""
    lines: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            lines.append(_WHITESPACE_RUN.sub(" ", stripped))
    return "\n".join(lines).strip()


@dataclass
class OcrEngine:
    """OCR 门面：负责语言解析、结果缓存与统一降级。"""

    backend: Optional[OcrBackend] = None
    language: str = ""
    locale_hint: str = "zh_CN"
    min_confidence: float = 0.0
    max_chars: int = 10_000
    #: 结果按文件内容指纹缓存，同一张截图不会重复识别
    cache_size: int = 128
    _cache: Dict[str, OcrResult] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        if self.backend is None:
            self.backend = TesseractBackend()

    @property
    def enabled(self) -> bool:
        return bool(self.backend) and self.backend.available()

    def languages(self) -> str:
        """实际会交给 tesseract 的语言串（已按已装语言过滤）。

        与 `resolve_languages()` 不同：这里返回的是**能用的**，不是**想要的**。
        后者交给 tesseract 会在语言缺失时整批失败。想要「用户原本要什么」，
        看 `language_choice().wants`。
        """
        return self.language_choice().languages

    def language_choice(self) -> LanguageChoice:
        """挑选可用语言，附带降级说明，供设置页与自检展示。"""
        backend = self.backend
        if backend is None:
            return LanguageChoice(reason="no_language_data")

        installed_fn = getattr(backend, "languages", None)
        installed: Sequence[str] = ()
        if callable(installed_fn):
            try:
                installed = installed_fn()
            except Exception as exc:  # noqa: BLE001 - 探测失败按「没有语言」处理
                _log.debug("查询已装语言失败：%s", exc)

        if backend.name != TesseractBackend.name:
            # 桩后端/自定义后端不提供已装语言，直接沿用想要的串
            wanted = resolve_languages(self.locale_hint, self.language)
            return LanguageChoice(languages=wanted, wants=wanted)

        return select_languages(installed, self.locale_hint, self.language)

    def recognize(self, image_path: str | Path) -> Optional[str]:
        """识别图片，返回文本；不适用或失败时返回 `None`。"""
        return self.recognize_full(image_path).text or None

    def recognize_full(self, image_path: str | Path) -> OcrResult:
        backend = self.backend
        if backend is None or not backend.available():
            return OcrResult(backend=getattr(backend, "name", "null"))

        path = Path(image_path)
        if not path.is_file():
            return OcrResult(backend=backend.name)

        fingerprint = _file_fingerprint(path)
        if fingerprint is None:
            return OcrResult(backend=backend.name)

        with self._lock:
            cached = self._cache.get(fingerprint)
        if cached is not None:
            return cached

        choice = self.language_choice()
        if not choice.usable:
            # 一个语言都没装。不必去调 tesseract —— 结果必然是失败，
            # 还要白起两个子进程。直接把原因记在结果上。
            return OcrResult(
                backend=backend.name,
                error="no_language_data",
                error_detail="tesseract 未安装任何语言数据，OCR 无法进行",
            )

        languages = choice.languages
        result = backend.recognize(path, languages)
        if result.is_empty or (self.min_confidence and result.confidence < self.min_confidence):
            # 空白与低置信结果也缓存，避免反复对同一张图跑 OCR。
            # 注意保留 error / error_detail —— 否则「语言数据缺失」会被洗成
            # 「正常的空结果」，又回到用户查不出原因的状态。
            result = OcrResult(
                text="",
                confidence=result.confidence,
                language=result.language,
                backend=result.backend,
                duration_ms=result.duration_ms,
                error=result.error,
                error_detail=result.error_detail,
            )
        elif len(result.text) > self.max_chars:
            result = OcrResult(
                text=result.text[: self.max_chars],
                confidence=result.confidence,
                language=result.language,
                backend=result.backend,
                duration_ms=result.duration_ms,
                error=result.error,
                error_detail=result.error_detail,
            )

        with self._lock:
            if len(self._cache) >= self.cache_size:
                self._cache.clear()
            self._cache[fingerprint] = result
        return result

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def status(self) -> Dict[str, object]:
        backend = self.backend
        choice = self.language_choice()
        installed: List[str] = []
        if self.enabled and backend:
            try:
                installed = sorted(backend.languages())
            except Exception:  # noqa: BLE001
                installed = []
        return {
            "enabled": self.enabled,
            # 「装了 tesseract、语言也对得上」才算真的能用。只看 enabled 会把
            # 「语言数据缺失」这种必然失败的情况报成可用。
            "usable": bool(self.enabled) and choice.usable,
            "backend": getattr(backend, "name", "null"),
            "languages": choice.languages,
            "requested": choice.wants,
            "missing": list(choice.missing),
            "degraded": choice.degraded,
            "reason": choice.reason,
            "detail": choice.describe(),
            "available": installed,
            "min_confidence": self.min_confidence,
        }


def _file_fingerprint(path: Path) -> Optional[str]:
    """用「路径 + 大小 + mtime」做缓存键，够快且能命中同一张图。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    return f"{path}:{stat.st_size}:{int(stat.st_mtime)}"


def build_engine(config=None) -> OcrEngine:
    """按配置构造引擎；未启用或后端缺失时返回禁用状态的引擎。"""
    if config is None:
        return OcrEngine(backend=NullBackend())

    if not config.get("ocr.enabled", True):
        return OcrEngine(backend=NullBackend(), language="")

    executable = str(config.get("ocr.executable", "tesseract") or "tesseract")
    backend = TesseractBackend(executable=executable, timeout=_as_float(config.get("ocr.timeout", DEFAULT_TIMEOUT), DEFAULT_TIMEOUT))
    engine = OcrEngine(
        backend=backend,
        language=str(config.get("ocr.languages", "") or ""),
        locale_hint=str(config.get("ui.language", "zh_CN") or "zh_CN"),
        min_confidence=_as_float(config.get("ocr.min_confidence", 0.0), 0.0),
        max_chars=int(_as_float(config.get("ocr.max_chars", 10_000), 10_000)),
    )
    if not engine.enabled:
        _log.info("OCR 不可用（未找到 tesseract），截图将不参与文字检索")
    return engine


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
