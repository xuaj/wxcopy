"""原始文案的来源：手输 / 图片 OCR / 表格文档 / 音视频转写。

以前流水线的入口只能是「一个装满视频的目录」。实际上要仿写的文案经常来自：
同事发的一张长图、自己在输入框里敲的一段话、或者一份现成的 Excel。
这里把四类来源统一成 SourceItem，后面的仿写/配音/合成不用关心它从哪来。

四类来源：
    text   手输文本 —— 一段话直接开工
    image  图片 OCR  —— 截图、长图、商品详情页
    table  表格文档  —— Excel/CSV 的某一列
    media  音视频    —— 走 ASR 转写（原有能力）
"""

from __future__ import annotations

import base64
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ..logging_setup import get_logger
from ..settings import OcrCfg, Settings, api_key_of
from .media import get_ffmpeg, safe_stem

log = get_logger()

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
TABLE_EXTS = {".xlsx", ".xlsm", ".xls", ".csv", ".tsv"}

SOURCE_KINDS = ("media", "text", "image", "table")

# 手输/表格来源在「文件原名」列里的前缀，方便在产物表上一眼看出来源
KIND_LABEL = {"media": "", "text": "手输", "image": "图片", "table": "表格"}

DEFAULT_OCR_PROMPT = (
    "请把图片里的所有文字完整、准确地识别出来。"
    "要求：只输出识别到的文字本身，不要解释、不要加标题、不要加```代码块；"
    "保持原文的段落与标点；如果是竖排或多栏，按正常阅读顺序输出；"
    "如果图片里没有文字，就只输出一个空字符串。"
)


class SourceError(RuntimeError):
    pass


@dataclass
class SourceItem:
    """一条待处理的原始文案。"""

    name: str  # 用于命名产物（音频/视频文件名）
    text: str
    origin: str = ""  # 来源文件路径（图片/表格），音视频为空
    duration: float = 0.0
    language: str = ""
    text_file: str = ""  # 落盘后的文案文件路径
    kind: str = "text"

    def as_row(self, index: int) -> dict:
        label = KIND_LABEL.get(self.kind, "")
        name = f"{label}_{self.name}" if label else self.name
        return {
            "序号": index,
            "文件原名": name,
            "时长(秒)": round(self.duration, 1) if self.duration else "",
            "语言": self.language,
            "转写文案": self.text,
            "文案文件": self.text_file,
        }


# ---------------------------------------------------------------- 枚举文件


def find_images(root: Path | str, recursive: bool = False) -> list[Path]:
    """列出目录内的图片；也可以直接给一个图片文件。"""
    p = Path(root)
    if p.is_file():
        return [p]
    it = p.rglob("*") if recursive else p.glob("*")
    return sorted(x for x in it if x.is_file() and x.suffix.lower() in IMAGE_EXTS)


def find_tables(root: Path | str) -> list[Path]:
    p = Path(root)
    if p.is_file():
        return [p]
    return sorted(x for x in p.glob("*") if x.is_file() and x.suffix.lower() in TABLE_EXTS)


def is_image(path: Path | str) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTS


def is_table(path: Path | str) -> bool:
    return Path(path).suffix.lower() in TABLE_EXTS


# ---------------------------------------------------------------- OCR 后端


class BaseOCR(ABC):
    kind: str = "base"
    name: str = ""
    offline: bool = True

    @abstractmethod
    def read(self, image: Path) -> str:
        """识别一张图，返回文字。"""

    def probe(self) -> None:
        """真正检查「现在能不能用」。构造成功不代表能跑（缺依赖/缺模型都会假阳性）。"""
        return None

    def read_many(self, images: Iterable[Path]) -> dict[str, str]:
        out: dict[str, str] = {}
        for img in images:
            try:
                out[str(img)] = self.read(img)
            except Exception as e:  # noqa: BLE001
                log.error("OCR 失败 %s：%s", img.name, str(e)[:160])
        return out


class RapidOCR(BaseOCR):
    """本地离线 OCR（rapidocr-onnxruntime）。

    PaddleOCR 的 ONNX 精简版，纯 CPU 可跑，中文效果不错，
    不需要系统装 tesseract，也不需要 GPU。首次调用会自动下载几 MB 的小模型。
    """

    kind = "rapidocr"
    offline = True

    def __init__(self, cfg: OcrCfg | None = None):
        self.cfg = cfg or OcrCfg()
        self.name = "rapidocr(本地离线)"
        self._engine = None

    @property
    def engine(self):
        if self._engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR  # 延迟导入
            except ImportError as e:  # pragma: no cover
                raise SourceError(
                    '未安装本地 OCR。执行：pip install "wxcopy[ocr]"\n（或 pip install rapidocr-onnxruntime）'
                ) from e
            self._engine = RapidOCR()
        return self._engine

    def probe(self) -> None:
        _ = self.engine  # 触发 import + 引擎构造

    def read(self, image: Path) -> str:
        result, _ = self.engine(str(image))
        if not result:
            return ""
        min_score = max(0.0, min(1.0, self.cfg.min_score))
        lines: list[str] = []
        for item in result:
            # 每项形如 [box, text, score]
            text = str(item[1]).strip()
            score = float(item[2]) if len(item) > 2 else 1.0
            if text and score >= min_score:
                lines.append(text)
        return _join_ocr_lines(lines)


class TesseractOCR(BaseOCR):
    """pytesseract（需要系统装好 tesseract 并配好中文语言包）。"""

    kind = "tesseract"
    offline = True

    def __init__(self, cfg: OcrCfg | None = None):
        self.cfg = cfg or OcrCfg()
        self.name = f"tesseract({self.cfg.languages})"
        self._mod = None

    @property
    def mod(self):
        if self._mod is None:
            try:
                import pytesseract  # 延迟导入
            except ImportError as e:  # pragma: no cover
                raise SourceError(
                    "未安装 pytesseract。执行：pip install pytesseract，并自行安装 tesseract 程序（含 chi_sim 语言包）"
                ) from e
            self._mod = pytesseract
        return self._mod

    def probe(self) -> None:
        _ = self.mod.get_tesseract_version()  # 没装 tesseract 程序会在这里炸

    def read(self, image: Path) -> str:
        from PIL import Image  # noqa: PLC0415

        with Image.open(image) as im:
            text = self.mod.image_to_string(im.convert("RGB"), lang=self.cfg.languages or "chi_sim+eng")
        return _join_ocr_lines([t for t in str(text).splitlines() if t.strip()])


class VisionLLMOCR(BaseOCR):
    """用多模态大模型做 OCR（OpenAI 兼容端点，含各家云厂商的视觉模型）。"""

    kind = "openai"
    offline = False

    def __init__(self, cfg: OcrCfg | None = None, model: str | None = None, timeout: int = 300):
        from openai import OpenAI  # 延迟导入

        self.cfg = cfg or OcrCfg()
        self.model = model or self.cfg.model
        if not self.model:
            raise SourceError("未指定视觉模型（ocr.model）")
        api_key = self.cfg.api_key or api_key_of(self.cfg.api_key_env)
        if not api_key:
            raise SourceError(f"未提供 API Key：请设置环境变量 {self.cfg.api_key_env or '(未配置 ocr.api_key_env)'}")
        self.client = OpenAI(base_url=self.cfg.base_url or None, api_key=api_key, timeout=timeout)
        self.name = f"vision:{self.model}"

    def probe(self) -> None:
        self.client.models.list()

    def read(self, image: Path) -> str:
        data = _image_data_uri(image, self.cfg.max_side)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.cfg.prompt or DEFAULT_OCR_PROMPT},
                        {"type": "image_url", "image_url": {"url": data}},
                    ],
                }
            ],
            "temperature": 0,
        }
        resp = self.client.chat.completions.create(**payload)
        if not resp.choices:
            raise SourceError("视觉模型返回为空")
        return _strip_fence((resp.choices[0].message.content or "").strip())


class OllamaVisionOCR(BaseOCR):
    """本地 Ollama 的视觉模型（qwen2.5vl / llama3.2-vision / minicpm-v 等）。"""

    kind = "ollama"
    offline = True

    def __init__(self, cfg: OcrCfg | None = None, model: str | None = None, base_url: str = "", timeout: int = 600):
        self.cfg = cfg or OcrCfg()
        self.model = model or self.cfg.model
        if not self.model:
            raise SourceError("未指定视觉模型（ocr.model），例如 qwen2.5vl:7b")
        self.root = (base_url or "http://127.0.0.1:11434").rstrip("/")
        if self.root.endswith("/v1"):
            self.root = self.root[: -len("/v1")]
        self.timeout = timeout
        self.name = f"ollama-vision:{self.model}"

    def probe(self) -> None:
        _assert_ollama_model(self.root, self.model)

    def read(self, image: Path) -> str:
        import json
        import urllib.request

        raw = _image_jpeg_bytes(image, self.cfg.max_side)
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": self.cfg.prompt or DEFAULT_OCR_PROMPT,
                        # Ollama 原生接口要的是裸 base64，不要 data: 前缀
                        "images": [base64.b64encode(raw).decode("ascii")],
                    }
                ],
                "stream": False,
                "options": {"temperature": 0},
            }
        ).encode()
        req = urllib.request.Request(f"{self.root}/api/chat", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.load(resp)
        return _strip_fence(str(data.get("message", {}).get("content", "")).strip())


def build_ocr(settings: Settings, provider: str | None = None, model: str | None = None) -> BaseOCR:
    """按配置构造 OCR 后端。provider=auto 时挑第一个真正能用的。"""
    cfg = settings.ocr
    want = (provider or cfg.provider or "auto").lower()

    if want == "rapidocr":
        return RapidOCR(cfg)
    if want == "tesseract":
        return TesseractOCR(cfg)
    if want == "openai":
        return VisionLLMOCR(cfg, model)
    if want == "ollama":
        return OllamaVisionOCR(cfg, model or cfg.model, cfg.base_url or "")
    if want != "auto":
        raise SourceError(f"未知的 ocr.provider: {want}（可用: auto/rapidocr/tesseract/openai/ollama）")

    # auto：本地优先（不花钱、不泄密），本地都不行再考虑云
    order = ["rapidocr", "tesseract", "ollama", "openai"]
    errors: list[str] = []
    for name in order:
        try:
            probe = build_ocr(settings, provider=name, model=model)
            probe.probe()  # 真跑一次可用性检查，避免「构造成功但用不了」
            log.info("使用 OCR 引擎：%s", probe.name)
            return probe
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {str(e)[:80]}")
    raise SourceError("没有可用的 OCR 引擎。\n  " + "\n  ".join(errors))


def _assert_ollama_model(root: str, model: str) -> None:
    import json
    import urllib.request

    with urllib.request.urlopen(f"{root}/api/tags", timeout=10) as resp:
        data = json.load(resp)
    names = {m["name"] for m in data.get("models", [])}
    if model not in names:
        base = model.split(":")[0]
        if not any(n.split(":")[0] == base for n in names):
            raise SourceError(f"本地没有视觉模型 {model}，请先 ollama pull {model}")


def ocr_backends(settings: Settings) -> list[dict[str, Any]]:
    """给前端枚举 OCR 后端及其可用性。"""
    rows: list[dict[str, Any]] = []
    for name, label, offline in [
        ("auto", "自动选择（本地优先）", True),
        ("rapidocr", "RapidOCR · 本地离线", True),
        ("tesseract", "Tesseract · 本地（需自行安装）", True),
        ("ollama", "Ollama 视觉模型 · 本地", True),
        ("openai", "云端视觉模型 · OpenAI 兼容", False),
    ]:
        ok, detail = False, ""
        try:
            eng = build_ocr(settings, provider=name)
            eng.probe()
            ok, detail = True, eng.name
        except Exception as e:  # noqa: BLE001
            detail = str(e)[:120]
        rows.append({"id": name, "name": label, "ok": ok, "detail": detail, "offline": offline})
    return rows


# ---------------------------------------------------------------- 图片预处理


def _image_jpeg_bytes(image: Path, max_side: int = 1600) -> bytes:
    """用 ffmpeg 把图缩到长边不超过 max_side 再转 jpg，省 token、提速度。"""
    src = Path(image)
    if max_side and max_side > 0:
        scale = f"scale='if(gt(iw,ih),{max_side},-2)':'if(gt(iw,ih),-2,{max_side})'"
        cmd = [
            get_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-vf",
            scale,
            "-q:v",
            "3",
            "-f",
            "mjpeg",
            "-",
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, check=True).stdout
            if out:
                return out
        except Exception as e:  # noqa: BLE001
            log.warning("缩放 %s 失败（%s），改用原图", src.name, str(e)[:80])
    return src.read_bytes()


def _image_data_uri(image: Path, max_side: int = 1600) -> str:
    mime = "image/png" if Path(image).suffix.lower() == ".png" else "image/jpeg"
    if max_side and max_side > 0:
        mime = "image/jpeg"
    b64 = base64.b64encode(_image_jpeg_bytes(image, max_side)).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _strip_fence(text: str) -> str:
    """模型爱用 ``` 包一层，去掉。"""
    t = (text or "").strip()
    if t.startswith("```") and t.endswith("```"):
        lines = t.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return t


def _join_ocr_lines(lines: list[str]) -> str:
    """OCR 常把段落切碎，能合并的合并一下。"""
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        out.append(s)
    if not out:
        return ""
    # 全是很短的碎片（名片/标签类）时用空格连，方便后续按标点断句
    if sum(len(s) for s in out) / len(out) < 8:
        return " ".join(out)
    return "\n".join(out)


# ---------------------------------------------------------------- 各类来源


def from_text(text: str, name: str | None = None) -> list[SourceItem]:
    """手输一段文案。"""
    body = (text or "").strip()
    if not body:
        return []
    nm = (name or "").strip()
    if not nm:
        head = re.sub(r"\s+", " ", body)[:14].strip()
        nm = head or datetime.now().strftime("%Y%m%d_%H%M%S")
    return [SourceItem(name=safe_stem(Path(nm), limit=40), text=body, kind="text")]


def from_images(
    images: Iterable[Path],
    settings: Settings,
    provider: str | None = None,
    model: str | None = None,
    recursive: bool = False,
) -> list[SourceItem]:
    """图片 OCR。每个入参可以是单张图，也可以是装图的目录。"""
    files: list[Path] = []
    for x in images or []:
        files += find_images(x, recursive=recursive)
    files = _dedup(files)
    if not files:
        return []

    engine = build_ocr(settings, provider=provider, model=model)
    log.info("共 %d 张图片，使用 OCR：%s", len(files), engine.name)
    items: list[SourceItem] = []
    for i, img in enumerate(files, 1):
        text = ""
        try:
            text = engine.read(img)
        except Exception as e:  # noqa: BLE001
            log.error("OCR 失败 %s：%s", img.name, str(e)[:160])
        log.info("[%d/%d] 识别 %s -> %d 字", i, len(files), img.name, len(text))
        items.append(SourceItem(name=safe_stem(img, limit=40), text=text, origin=str(img), kind="image"))
    return items


def from_table(
    table: Path | str,
    column: str,
    settings: Settings,
    sheet: str | None = None,
    name_column: str | None = None,
    limit: int | None = None,
) -> list[SourceItem]:
    """从 Excel/CSV 的某一列读文案。"""
    from ..io.table import read_table, resolve_column

    path = Path(table)
    if not path.is_file():
        raise SourceError(f"表格文件不存在：{path}")
    rows, columns = read_table(path, sheet=sheet)
    if not rows:
        raise SourceError(f"{path.name} 没有数据行")
    src_col = resolve_column(columns, column)
    name_col = resolve_column(columns, name_column) if name_column else None

    if limit:
        rows = rows[:limit]

    items: list[SourceItem] = []
    for i, r in enumerate(rows, 1):
        text = str(r.get(src_col, "") or "").strip()
        if not text:
            continue
        raw_name = str(r.get(name_col, "") or "").strip() if name_col else ""
        nm = raw_name or f"{path.stem}_{i:03d}"
        items.append(SourceItem(name=safe_stem(Path(nm), limit=40), text=text, origin=str(path), kind="table"))
    log.info("从 %s 的「%s」列读到 %d 条文案", path.name, src_col, len(items))
    return items


def from_media(
    media: Path | str,
    settings: Settings,
    recursive: bool = False,
    language: str | None = None,
    limit: int | None = None,
    skip_existing: bool = True,
    with_srt: bool = True,
    text_dir: Path | None = None,
) -> list[SourceItem]:
    """音视频转写（沿用原有 ASR 能力）。"""
    from .asr import build_asr, find_media

    root = Path(media)
    files = find_media(root, recursive=recursive)
    if limit:
        files = files[:limit]
    if not files:
        return []

    out_dir = Path(text_dir) if text_dir else settings.text_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    asr = build_asr(settings)
    items: list[SourceItem] = []
    for i, f in enumerate(files, 1):
        stem = safe_stem(f)
        txt_file = out_dir / f"{stem}.txt"
        if skip_existing and txt_file.exists() and txt_file.stat().st_size > 0:
            log.info("[%d/%d] 跳过（已有文案）%s", i, len(files), stem)
            items.append(
                SourceItem(name=stem, text=txt_file.read_text(encoding="utf-8"), text_file=str(txt_file), kind="media")
            )
            continue
        log.info("[%d/%d] 转写 %s", i, len(files), stem)
        try:
            tr = asr.transcribe(f, language=language)
        except Exception as e:  # noqa: BLE001
            log.error("转写失败 %s：%s", stem, str(e)[:160])
            continue
        tr.save(txt_file, with_srt=with_srt)
        items.append(
            SourceItem(
                name=stem,
                text=tr.text,
                duration=tr.duration,
                language=tr.language,
                text_file=str(txt_file),
                kind="media",
            )
        )
        log.info("   -> %d 字", len(tr.text))
    return items


def _dedup(files: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for f in files:
        key = str(Path(f).resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def save_items(items: list[SourceItem], text_dir: Path) -> list[SourceItem]:
    """非音视频来源也要把文案落盘，保证「文案文件」列有值、可追溯。"""
    text_dir.mkdir(parents=True, exist_ok=True)
    for i, it in enumerate(items, 1):
        if it.text_file and Path(it.text_file).is_file():
            continue
        stem = safe_stem(Path(it.name), limit=60) or f"row_{i:03d}"
        p = text_dir / f"{stem}.txt"
        p.write_text(it.text, encoding="utf-8")
        it.text_file = str(p)
    return items


@dataclass
class SourceSpec:
    """一次运行的「文案来源」描述。"""

    kind: str = "media"
    media: str = ""  # 音视频文件或目录
    text: str = ""  # 手输文案
    name: str = ""  # 手输文案的命名
    images: list[str] = field(default_factory=list)  # 图片文件或目录
    table: str = ""  # Excel/CSV 路径
    table_column: str = ""  # 文案所在列
    table_sheet: str = ""
    name_column: str = ""  # 用该列命名产物
    ocr_provider: str = ""  # 覆盖 ocr.provider
    ocr_model: str = ""
    recursive: bool = False
    limit: int | None = None

    def describe(self) -> str:
        if self.kind == "media":
            return f"音视频 {self.media or '(空)'}"
        if self.kind == "text":
            return f"手输文案（{len(self.text or '')} 字）"
        if self.kind == "image":
            return f"图片 OCR（{len(self.images)} 个路径）"
        if self.kind == "table":
            return f"表格 {self.table} 的「{self.table_column}」列"
        return self.kind


def collect(
    settings: Settings,
    spec: SourceSpec,
    language: str | None = None,
    skip_existing: bool = True,
    with_srt: bool = True,
    text_dir: Path | None = None,
) -> list[SourceItem]:
    """按 spec 收集原始文案，并把非音视频来源的文案落盘。"""
    kind = (spec.kind or "media").lower()
    if kind not in SOURCE_KINDS:
        raise SourceError(f"未知的文案来源：{kind}（可用: {'/'.join(SOURCE_KINDS)}）")

    out_dir = Path(text_dir) if text_dir else settings.text_dir

    if kind == "media":
        if not (spec.media or "").strip():
            raise SourceError("未指定音视频目录")
        items = from_media(
            spec.media,
            settings,
            recursive=spec.recursive,
            language=language,
            limit=spec.limit,
            skip_existing=skip_existing,
            with_srt=with_srt,
            text_dir=out_dir,
        )
    elif kind == "text":
        items = from_text(spec.text, spec.name)
    elif kind == "image":
        if not spec.images:
            raise SourceError("未指定图片文件或目录")
        items = from_images(
            spec.images,
            settings,
            provider=spec.ocr_provider or None,
            model=spec.ocr_model or None,
            recursive=spec.recursive,
        )
        if spec.limit:
            items = items[: spec.limit]
    else:  # table
        if not (spec.table or "").strip():
            raise SourceError("未指定表格文件")
        if not (spec.table_column or "").strip():
            raise SourceError("未指定表格里的文案列名")
        items = from_table(
            spec.table,
            spec.table_column,
            settings,
            sheet=spec.table_sheet or None,
            name_column=spec.name_column or None,
            limit=spec.limit,
        )

    items = [it for it in items if (it.text or "").strip()]
    save_items(items, out_dir)
    return items


def guess_kind(path: str | Path) -> str:
    """给个路径猜它是哪类来源，前端自动切 tab 用。"""
    p = Path(str(path or "").strip())
    if not p.suffix:
        return "media" if p.is_dir() else ""
    sfx = p.suffix.lower()
    if sfx in IMAGE_EXTS:
        return "image"
    if sfx in TABLE_EXTS:
        return "table"
    from .asr import MEDIA_EXTS

    if sfx in MEDIA_EXTS:
        return "media"
    return ""
