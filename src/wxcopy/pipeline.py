"""四阶段流水线编排：收集文案 -> 仿写 -> 配音 -> 合成视频。

每个阶段都能单独调用，产物落盘，重跑自动跳过已完成的部分。
阶段一的文案来源有四种：音视频转写 / 手输 / 图片 OCR / Excel 表格，见 core.source。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core.llm import build_llm
from .core.media import safe_stem
from .core.rewriter import Rewriter, RewriteStyle, load_prompt
from .core.tts import AUDIO_EXTS, build_tts, cues_path_of, supports_cues, synth_many
from .io.table import export_rows, read_table, resolve_column, write_column
from .logging_setup import get_logger
from .settings import Settings

log = get_logger()

ASR_COLUMNS = ["序号", "文件原名", "时长(秒)", "语言", "转写文案", "文案文件"]


def _find_voice(voice_dir: Path, name: str) -> Path | None:
    """在配音目录里按基础名找音频。

    必须按音频后缀过滤：配音旁还挂着 `<name>.cues.json`（逐字时间戳），
    不过滤的话 name.json 会排在 name.mp3 前面被当成音频。
    """
    hits = sorted(p for p in voice_dir.glob(f"{name}.*") if p.suffix.lower() in AUDIO_EXTS)
    return hits[0] if hits else None


def _cues_digest(audio: str) -> str:
    """逐字时间戳的指纹：音频重录（语速/音色变了、时间轴变了）时让视频跟着重出。"""
    if not audio:
        return ""
    p = cues_path_of(audio)
    try:
        return hashlib.sha1(p.read_bytes()).hexdigest()[:12] if p.is_file() else ""
    except OSError:
        return ""


def _default_name_col(columns: list[str]) -> str:
    """未显式指定命名列时的兜底。

    直接用 columns[0] 通常是「序号」，产物会叫 001_1.mp4，事后根本看不出是哪条；
    优先挑有信息量的列。
    """
    for c in ("文件原名", "标题", "名称", "文案"):
        if c in columns:
            return c
    return columns[0] if columns else "序号"


def _row_name(row: dict, name_col: str, index: int, limit: int = 40) -> str:
    """产物基础名：`行号_名字`。

    行号必须稳定——配音和视频都按它命名，行序一漂同一条就会被反复重做。
    """
    base = safe_stem(Path(str(row.get(name_col, "") or f"row_{index}")), limit=limit)
    return f"{index:03d}_{base}" if base else f"{index:03d}"


@dataclass
class VideoOverrides:
    """阶段四的渲染参数覆盖项。

    `None` 一律表示「不覆盖，沿用配置/默认值」——这点很重要，
    否则 CLI 里没传的参数会把配置文件里的值冲成 0 / 空串。
    """

    bg_path: str | None = None
    bg_color: str | None = None
    fit: str | None = None
    size: str | None = None
    width: int | None = None
    height: int | None = None
    subtitle: bool | None = None
    # ---- classic 字幕 ----
    font: str | None = None
    font_size: int | None = None
    margin_v: int | None = None
    chars_per_line: int | None = None
    bold: bool | None = None
    subtitle_fade: float | None = None
    # ---- big 字幕 ----
    style: str | None = None
    highlight_mode: str | None = None
    highlight_color: str | None = None
    keywords: list[str] | None = field(default_factory=lambda: None)
    big_chars_per_line: int | None = None
    big_lines_per_screen: int | None = None
    big_top_ratio: float | None = None
    big_line_spacing: float | None = None
    big_font_size: int | None = None
    # ---- 打字机 ----
    typewriter: bool | None = None
    type_speed: float | None = None
    type_hold: float | None = None
    type_min: float | None = None
    # ---- 编码 ----
    tail_pad: float | None = None
    fade_in: float | None = None
    crf: int | None = None
    # ---- BGM ----
    bgm: str | None = None
    bgm_volume: float | None = None
    bgm_duck: bool | None = None
    bgm_duck_ratio: float | None = None
    bgm_fade_out: float | None = None

    @classmethod
    def coerce(cls, data: dict | None) -> "VideoOverrides":
        """从 dict 构造，忽略未知键（Web 端会传来一堆别的字段）。"""
        if not data:
            return cls()
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in names})


def _coerce_legacy(overrides: VideoOverrides | dict | None, legacy: dict) -> VideoOverrides:
    """归一化 overrides，并把旧式的扁平关键字合并进来。

    这么写是为了让早期写的调用方式（stage_video(..., style="big", crf=18)）
    继续可用，同时新代码可以传一个干净的对象。
    """
    if isinstance(overrides, VideoOverrides):
        ov = overrides
    else:
        ov = VideoOverrides.coerce(overrides)
    if legacy:
        given = {k: v for k, v in legacy.items() if v is not None}
        if given:
            log.debug("stage_video 收到 %d 个旧式关键字参数，已合并进 overrides", len(given))
            merged = {**{f: getattr(ov, f) for f in VideoOverrides.__dataclass_fields__}, **given}
            ov = VideoOverrides.coerce(merged)
    return ov


def _merge_asr_rows(
    existing_rows: list[dict], existing_cols: list[str], new_rows: list[dict]
) -> tuple[list[dict], list[str]]:
    """把新一轮转写结果并进已有表格，按「文件原名」对齐。

    不这么做的话 stage_asr 会用 ASR_COLUMNS 整表重写，把「仿写文案 / 配音 / 视频」
    三列连同用户手改的内容一起清掉，下次跑就得重新调大模型仿写。
    """
    extra_cols = [c for c in existing_cols if c not in ASR_COLUMNS]
    by_new: dict[str, dict] = {}
    for r in new_rows:
        nm = str(r.get("文件原名", "") or "").strip()
        if nm:
            by_new.setdefault(nm, r)

    def blend(nr: dict | None, old: dict | None) -> dict:
        """ASR 列取新值，其余列（仿写/配音/视频…）取旧值。"""
        row = {c: (nr or {}).get(c, "") for c in ASR_COLUMNS}
        for c in extra_cols:
            row[c] = (old or {}).get(c, "")
        return row

    # 关键：沿用旧表的行序。行序一变，行号就漂移，
    # stage_tts/stage_video 按「行号_名字」命名产物，会导致同一条被反复重做。
    out: list[dict] = []
    used: set[str] = set()
    kept = 0
    for r in existing_rows:
        nm = str(r.get("文件原名", "") or "").strip()
        nr = by_new.get(nm)
        if nr is None:
            # 本轮没扫到的历史行原样保留，避免已生成的配音/视频记录丢失
            out.append(r)
            kept += 1
            if nm:
                used.add(nm)
        else:
            out.append(blend(nr, r))
            used.add(nm)
    for nr in new_rows:
        nm = str(nr.get("文件原名", "") or "").strip()
        if nm and nm in used:
            continue
        out.append(blend(nr, None))
        if nm:
            used.add(nm)

    for i, row in enumerate(out, 1):
        row["序号"] = i
    if kept:
        log.info("保留了 %d 行历史记录（不在本轮扫描范围内）", kept)
    return out, ASR_COLUMNS + extra_cols


def stage_source(
    settings: Settings,
    spec,  # core.source.SourceSpec
    out: Path | str | None = None,
    language: str | None = None,
    skip_existing: bool = True,
    with_srt: bool = True,
) -> Path:
    """阶段一：按来源收集原始文案 -> 文案表。

    来源有四种：音视频转写 / 手输 / 图片 OCR / Excel 表格，见 core.source。
    """
    from .core.source import collect

    out_path = Path(out) if out else settings.workdir / "transcripts.xlsx"
    settings.text_dir.mkdir(parents=True, exist_ok=True)

    log.info("文案来源：%s", spec.describe())
    items = collect(
        settings, spec, language=language, skip_existing=skip_existing, with_srt=with_srt, text_dir=settings.text_dir
    )
    if not items:
        raise ValueError(f"没有从来源中取到任何文案：{spec.describe()}")

    rows = [it.as_row(i) for i, it in enumerate(items, 1)]
    empty = sum(1 for it in items if not it.text.strip())
    if empty:
        log.warning("其中 %d 条没有识别出文字，已跳过", empty)
    log.info("共取得 %d 条原始文案", len(rows))

    # 已有表格时按文件名合并，保住「仿写文案 / 配音 / 视频」等列
    if out_path.exists() and out_path.stat().st_size > 0:
        try:
            old_rows, old_cols = read_table(out_path)
        except Exception as e:  # noqa: BLE001
            log.warning("读取已有表格失败（将重建）：%s", str(e)[:120])
        else:
            rows, cols = _merge_asr_rows(old_rows, old_cols, rows)
            export_rows(out_path, rows, cols)
            log.info("文案表已合并写入 %s（%d 行）", out_path, len(rows))
            return out_path

    export_rows(out_path, rows, ASR_COLUMNS)
    log.info("文案表已写入 %s", out_path)
    return out_path


def stage_asr(
    settings: Settings,
    media: Path | str,
    out: Path | str | None = None,
    recursive: bool = False,
    language: str | None = None,
    limit: int | None = None,
    skip_existing: bool = True,
    with_srt: bool = True,
) -> Path:
    """阶段一（音视频专用）：视频/音频 -> 文案表。等价于 kind=media 的来源。"""
    from .core.source import SourceSpec

    spec = SourceSpec(kind="media", media=str(media), recursive=recursive, limit=limit)
    return stage_source(settings, spec, out=out, language=language, skip_existing=skip_existing, with_srt=with_srt)


def stage_rewrite(
    settings: Settings,
    table: Path | str,
    text_column: str,
    out_column: str = "仿写文案",
    provider: str | None = None,
    model: str | None = None,
    prompt: str | None = None,
    prompt_file: str | None = None,
    temperature: float | None = None,
    concurrency: int | None = None,
    length: str | None = None,
    tone: str | None = None,
    audience: str | None = None,
    cta: str | None = None,
    keep_hashtags: bool | None = None,
    keywords: list[str] | None = None,
    sheet: str | None = None,
    key_column: str | None = None,
    out: Path | str | None = None,
    limit: int | None = None,
    skip_existing: bool = True,
) -> Path:
    """阶段二：读取表格中的文案列 -> 仿写 -> 回写新列。"""
    path = Path(table)
    rows, columns = read_table(path, sheet=sheet)
    if not rows:
        raise ValueError(f"{path} 没有数据行")

    src_col = resolve_column(columns, text_column)
    log.info("读取 %d 行，文案列 = %s", len(rows), src_col)

    if limit:
        rows = rows[:limit]

    cfg = settings.rewrite
    if concurrency:
        cfg.concurrency = concurrency
    system = load_prompt(prompt_file or cfg.prompt_file or prompt or cfg.prompt)
    style = RewriteStyle(
        length=length or "与原文案相当",
        tone=tone or "",
        audience=audience or "",
        cta=cta or "",
        keep_hashtags=cfg.keep_hashtags if keep_hashtags is None else keep_hashtags,
        keywords=keywords or [],
    )
    llm = build_llm(settings, provider=provider, model=model, temperature=temperature)
    log.info("使用模型：%s", llm.name)
    rewriter = Rewriter(llm, cfg, style=style, system_prompt=system)

    # 断点续跑：目标列已有内容的行跳过
    existing = {}
    if skip_existing and out_column in columns:
        existing = {i: r.get(out_column, "") for i, r in enumerate(rows)}

    key_col = None
    if key_column:
        key_col = resolve_column(columns, key_column)

    items: list[tuple[Any, str]] = []
    for i, r in enumerate(rows):
        text = r.get(src_col, "")
        if not (text or "").strip():
            continue
        if skip_existing and str(existing.get(i, "")).strip():
            continue
        key = r.get(key_col, i) if key_col else i
        items.append((key, text))

    results = rewriter.rewrite_many(items)

    # 汇总（含已存在的）后回写
    final: list[tuple[Any, str]] = []
    for i, r in enumerate(rows):
        key = r.get(key_col, i) if key_col else i
        val = results.get(key, "")
        if not val and skip_existing:
            val = str(existing.get(i, "") or "")
        final.append((key, val))

    target = Path(out) if out else path
    if target != path:
        shutil.copy(path, target)
    write_column(target, out_column, final, sheet=sheet, key_column=key_col)
    ok = sum(1 for _, v in final if str(v).strip())
    log.info("仿写完成：成功 %d 条，已写入列「%s」 -> %s", ok, out_column, target)
    return target


def stage_tts(
    settings: Settings,
    table: Path | str,
    text_column: str,
    name_column: str | None = None,
    out_dir: Path | str | None = None,
    voice: str | None = None,
    out_column: str = "配音",
    sheet: str | None = None,
    concurrency: int = 2,
    limit: int | None = None,
    skip_existing: bool = True,
) -> Path:
    """阶段三：表格中的仿写文案 -> 音频文件，并把路径回写。"""
    path = Path(table)
    rows, columns = read_table(path, sheet=sheet)
    src_col = resolve_column(columns, text_column)
    name_col = resolve_column(columns, name_column) if name_column else _default_name_col(columns)

    if limit:
        rows = rows[:limit]

    voice_dir = Path(out_dir) if out_dir else settings.voice_dir
    voice_dir.mkdir(parents=True, exist_ok=True)
    tts = build_tts(settings, voice=voice)
    log.info("使用配音引擎：%s", tts.name)

    items: list[tuple[str, str]] = []
    for i, r in enumerate(rows, 1):
        text = (r.get(src_col) or "").strip()
        if not text:
            continue
        name = _row_name(r, name_col, i)
        if skip_existing:
            hit = _find_voice(voice_dir, name)
            # 有音频就跳过，除非「该产出逐字时间戳却没有」——那说明是旧版本录的，
            # 字幕只能退回按字数均分，长文案会明显跑偏，这里重录一次补上。
            if hit and not (supports_cues(tts) and not cues_path_of(hit).is_file()):
                continue
        items.append((name, text))

    log.info("待合成 %d 条音频（跳过已存在的 %d 条）", len(items), len(rows) - len(items))
    results = synth_many(items, voice_dir, tts, concurrency=concurrency)

    final: list[tuple[Any, str]] = []
    for i, r in enumerate(rows, 1):
        name = _row_name(r, name_col, i)
        hit = results.get(name) or _find_voice(voice_dir, name)
        final.append((i, str(hit) if hit else ""))

    write_column(path, out_column, final, sheet=sheet)
    log.info("配音完成：%d 个音频 -> %s", len(results), voice_dir)
    return path


# 参与「指纹」的渲染参数：任何一个变了就得重出，否则改了参数却看到旧片
_SIG_FIELDS = (
    "width",
    "height",
    "fps",
    "style",
    "subtitle",
    "bg_path",
    "bg_color",
    "fit",
    "font",
    "bold",
    "font_size",
    "font_color",
    "outline_color",
    "outline",
    "shadow",
    "margin_v",
    "chars_per_line",
    "align",
    "subtitle_fade",
    "big_font",
    "big_font_size",
    "big_chars_per_line",
    "big_lines_per_screen",
    "big_top_ratio",
    "big_line_spacing",
    "highlight_mode",
    "highlight_color",
    "highlight_outline_color",
    "keywords",
    "typewriter",
    "type_speed",
    "type_hold",
    "type_min",
    "crf",
    "preset",
    "audio_bitrate",
    "fade_in",
    "tail_pad",
    # BGM：必须在这里，否则改了 BGM/音量却看到旧片（有测试盯着）
    "bgm",
    "bgm_volume",
    "bgm_duck",
    "bgm_duck_ratio",
    "bgm_fade_out",
)


def _video_sig(text: str, cfg, cues_digest: str = "") -> str:
    """文案 + 渲染参数 + 语音时间轴 + BGM 文件的指纹。任一变化 → 指纹变化 → 重出。"""
    from .core.video import file_stamp

    payload = {
        "t": text,
        "c": {k: getattr(cfg, k, None) for k in _SIG_FIELDS},
        "a": cues_digest,
        # 换一个同名 BGM 文件时，光比路径看不出来，得比文件本身
        "m": file_stamp(getattr(cfg, "bgm", "") or ""),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _video_fresh(vdir: Path, name: str, sig: str) -> bool:
    """视频已存在、且旁挂指纹文件里的签名与当前一致，才算「不用重出」。"""
    if not (vdir / f"{name}.mp4").is_file():
        return False
    side = vdir / f"{name}.src.txt"
    if not side.is_file():
        # 老版本产出的视频没有指纹：重出一次，保证与当前文案/参数一致
        return False
    try:
        return side.read_text(encoding="utf-8").strip() == sig
    except OSError:
        return False


def stage_video(
    settings: Settings,
    table: Path | str,
    overrides: VideoOverrides | dict | None = None,
    *,
    text_column: str = "仿写文案",
    name_column: str | None = None,
    audio_column: str = "配音",
    out_dir: Path | str | None = None,
    out_column: str = "视频",
    sheet: str | None = None,
    limit: int | None = None,
    concurrency: int | None = None,
    skip_existing: bool = True,
    **legacy,  # 兼容旧的扁平关键字调用（见 _coerce_legacy）
) -> Path:
    """阶段四：仿写文案 + 配音 + 背景 -> 口播视频，并把路径回写。

    渲染参数统一走 `overrides`（VideoOverrides 或其 dict），
    不再用几十个扁平关键字——那样每加一个参数都要改三处签名。
    """
    from .core.video import PRESETS, hex_to_ass, make_many
    from .core.video import VideoCfg as CoreVideoCfg

    ov = _coerce_legacy(overrides, legacy)

    path = Path(table)
    rows, columns = read_table(path, sheet=sheet)
    src_col = resolve_column(columns, text_column)
    name_col = resolve_column(columns, name_column) if name_column else _default_name_col(columns)
    audio_col = resolve_column(columns, audio_column) if audio_column in columns else ""

    if limit:
        rows = rows[:limit]

    v = settings.video
    w, h = PRESETS.get(ov.size or v.size, PRESETS["竖屏 1080x1920"])
    if ov.width:
        w = int(ov.width)
    if ov.height:
        h = int(ov.height)
    vstyle = (ov.style or v.style or "classic").lower()
    hl = (ov.highlight_mode if ov.highlight_mode is not None else v.highlight_mode) or "none"
    cfg = CoreVideoCfg(
        width=w,
        height=h,
        fps=v.fps,
        bg_path=(ov.bg_path if ov.bg_path is not None else v.bg_path),
        bg_color=(ov.bg_color or v.bg_color),
        fit=(ov.fit or v.fit),
        # ---- 字幕 ----
        style=vstyle,
        subtitle=(v.subtitle if ov.subtitle is None else ov.subtitle),
        font=(ov.font or v.font),
        bold=(v.bold if ov.bold is None else ov.bold),
        font_size=int(ov.font_size or v.font_size),
        font_color=v.font_color,
        outline_color=v.outline_color,
        outline=v.outline,
        shadow=v.shadow,
        margin_v=int(ov.margin_v if ov.margin_v is not None else v.margin_v),
        chars_per_line=int(ov.chars_per_line or v.chars_per_line),
        align=v.align,
        subtitle_fade=float(ov.subtitle_fade if ov.subtitle_fade is not None else v.subtitle_fade),
        # ---- big 样式 ----
        big_font=v.big_font,
        big_font_size=int(ov.big_font_size if ov.big_font_size is not None else v.big_font_size),
        big_chars_per_line=int(ov.big_chars_per_line or v.big_chars_per_line),
        big_lines_per_screen=int(ov.big_lines_per_screen or v.big_lines_per_screen),
        big_top_ratio=float(ov.big_top_ratio if ov.big_top_ratio is not None else v.big_top_ratio),
        big_line_spacing=float(ov.big_line_spacing if ov.big_line_spacing is not None else v.big_line_spacing),
        highlight_mode=str(hl).lower(),
        # 配置里写 #00F090 或 &H0090F000 都能吃
        highlight_color=hex_to_ass(ov.highlight_color or v.highlight_color, "&H0090F000"),
        highlight_outline_color=hex_to_ass(v.highlight_outline_color, "&H00FFFFFF"),
        keywords=list(ov.keywords if ov.keywords is not None else (v.keywords or [])),
        # ---- 打字机 ----
        typewriter=(v.typewriter if ov.typewriter is None else bool(ov.typewriter)),
        type_speed=float(ov.type_speed if ov.type_speed is not None else v.type_speed),
        type_hold=float(ov.type_hold if ov.type_hold is not None else v.type_hold),
        type_min=float(ov.type_min if ov.type_min is not None else v.type_min),
        # ---- 编码 ----
        crf=int(ov.crf or v.crf),
        preset=v.preset,
        audio_bitrate=v.audio_bitrate,
        tail_pad=float(ov.tail_pad if ov.tail_pad is not None else v.tail_pad),
        fade_in=float(ov.fade_in if ov.fade_in is not None else v.fade_in),
        # ---- BGM ----
        bgm=(ov.bgm if ov.bgm is not None else v.bgm),
        bgm_volume=float(ov.bgm_volume if ov.bgm_volume is not None else v.bgm_volume),
        bgm_duck=(v.bgm_duck if ov.bgm_duck is None else bool(ov.bgm_duck)),
        bgm_duck_ratio=float(ov.bgm_duck_ratio if ov.bgm_duck_ratio is not None else v.bgm_duck_ratio),
        bgm_fade_out=float(ov.bgm_fade_out if ov.bgm_fade_out is not None else v.bgm_fade_out),
    )
    from .core.video import big_font_size_of, pick_font

    font_desc = f"，字体={pick_font(cfg)}"
    if vstyle == "big":
        font_desc += f"，字号={big_font_size_of(cfg)}，高亮={cfg.highlight_mode}"
        if cfg.keywords:
            font_desc += f"，关键词={len(cfg.keywords)}个"
        if cfg.typewriter:
            font_desc += (
                f"，打字机({'固定' if cfg.type_speed > 0 else '自适应'}"
                f"{f' {cfg.type_speed:g}s/字' if cfg.type_speed > 0 else f' 停留{cfg.type_hold:.0%}'})"
            )
    log.info(
        "视频参数：%dx%d，背景=%s，字幕=%s%s%s",
        cfg.width,
        cfg.height,
        cfg.bg_path or f"纯色{cfg.bg_color}",
        cfg.subtitle,
        font_desc,
        (
            f"，BGM={Path(cfg.bgm).name}（音量{cfg.bgm_volume:g}，闪避{'开' if cfg.bgm_duck else '关'}，"
            f"淡出{cfg.bgm_fade_out:g}s）"
            if (cfg.bgm or "").strip()
            else ""
        ),
    )

    vdir = Path(out_dir) if out_dir else settings.video_dir
    vdir.mkdir(parents=True, exist_ok=True)

    items: list[tuple[str, str, str]] = []
    for i, r in enumerate(rows, 1):
        text = (r.get(src_col) or "").strip()
        if not text:
            continue
        name = _row_name(r, name_col, i)
        # 音频优先取表格里的「配音」列，取不到就按命名规则在 voice 目录里找
        audio = str(r.get(audio_col) or "").strip() if audio_col else ""
        if not audio or not Path(audio).is_file():
            found = _find_voice(settings.voice_dir, name)
            audio = str(found) if found else ""
        # 只认「文案 + 渲染参数 + 语音时间轴都一样」的产物，否则改了参数会看到旧片
        if skip_existing and _video_fresh(vdir, name, _video_sig(text, cfg, _cues_digest(audio))):
            continue
        items.append((name, text, audio))

    no_audio = sum(1 for _, _, a in items if not a)
    log.info("待合成 %d 个视频（跳过已存在 %d 个，缺少配音 %d 个）", len(items), len(rows) - len(items), no_audio)

    results = make_many(items, vdir, cfg, concurrency=concurrency or v.concurrency)

    # 记下每个成品对应的「文案 + 参数 + 时间轴」指纹，下次据此判断「要不要重出」
    for nm, text, au in items:
        if (vdir / f"{nm}.mp4").is_file():
            try:
                (vdir / f"{nm}.src.txt").write_text(_video_sig(text, cfg, _cues_digest(au)), encoding="utf-8")
            except OSError as e:  # noqa: BLE001
                log.warning("写入指纹失败 %s：%s", nm, str(e)[:80])

    final: list[tuple[Any, str]] = []
    for i, r in enumerate(rows, 1):
        name = _row_name(r, name_col, i)
        hit = results.get(name) or ((vdir / f"{name}.mp4") if (vdir / f"{name}.mp4").is_file() else None)
        final.append((i, str(hit) if hit else ""))

    write_column(path, out_column, final, sheet=sheet)
    log.info("视频完成：%d 个 -> %s", len(results), vdir)
    return path


def _pick_cover(row: dict, cover_col: str, cover_arg: str, name: str) -> str:
    """封面优先取表格里的「封面」列，其次从 --cover 指定的目录里按名字找。"""
    v = str(row.get(cover_col) or "").strip() if cover_col else ""
    if v and Path(v).is_file():
        return str(Path(v).resolve())
    if cover_arg:
        p = Path(cover_arg)
        if p.is_file():
            return str(p.resolve())
        if p.is_dir():
            for ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                hit = p / f"{name}{ext}"
                if hit.is_file():
                    return str(hit.resolve())
    return ""


@dataclass
class CoverOverrides:
    """封面渲染参数的覆盖项。

    和 VideoOverrides 同规矩：`None` = 不覆盖，沿用配置。
    默认成 0 / "" 会把配置里的值冲掉，这种坑踩过一次就够了。
    """

    style: str | None = None
    width: int | None = None
    height: int | None = None
    accent: str | None = None
    bg: str | None = None
    bg_color: str | None = None
    footer: str | None = None
    font: str | None = None
    title_max: int | None = None
    subtitle_max: int | None = None
    jpeg: bool | None = None

    @classmethod
    def coerce(cls, data: dict | None) -> "CoverOverrides":
        if not data:
            return cls()
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in names})


def _cover_sig(title: str, subtitle: str, cfg) -> str:
    """封面指纹：标题 / 副标题 / 任一渲染参数变了都要重画。

    参数字段**直接取自 CoreCoverCfg 的 dataclass 字段**，而不是手写一份清单——
    手写清单迟早会漏（改了参数却看到旧图）。视频阶段就为此专门加过两条测试。
    """
    from .core.cover import CoverCfg as CoreCoverCfg

    payload = {
        "t": title,
        "s": subtitle,
        "c": {k: getattr(cfg, k, None) for k in CoreCoverCfg.__dataclass_fields__},
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _cover_fresh(cdir: Path, name: str, sig: str, ext: str) -> bool:
    img = cdir / f"{name}{ext}"
    side = cdir / f"{name}.cover.txt"
    if not img.is_file() or not side.is_file():
        return False
    try:
        return side.read_text(encoding="utf-8").strip() == sig
    except OSError:
        return False


def stage_cover(
    settings: Settings,
    table: Path | str | None = None,
    overrides: CoverOverrides | dict | None = None,
    *,
    text: str | None = None,
    title: str | None = None,
    subtitle: str = "",
    name: str = "",
    title_column: str = "推荐标题",
    text_column: str = "仿写文案",
    subtitle_column: str = "",
    name_column: str | None = None,
    out_dir: Path | str | None = None,
    out_column: str = "封面",
    sheet: str | None = None,
    limit: int | None = None,
    skip_existing: bool = True,
) -> Path:
    """生成成片封面（默认 1080×1920 竖图）。

    两种用法：
    - **单张**：给 `text`（或 `title`），出一张，返回图片路径。
    - **批量**：给 `table`，逐行出图并回写「封面」列，返回表格路径。

    标题取值优先级：表格的「推荐标题」列 > `title_of(「仿写文案」列)` > 规则名。
    生成只用到 Pillow（可选依赖），字体在系统里自动挑；两者缺一都会明确报错。
    """
    from .core.cover import CoverCfg as CoreCoverCfg
    from .core.cover import make_cover, title_of

    ov = overrides if isinstance(overrides, CoverOverrides) else CoverOverrides.coerce(overrides)
    c = settings.cover
    cfg = CoreCoverCfg(
        width=int(ov.width or c.width),
        height=int(ov.height or c.height),
        style=(ov.style or c.style),
        bg=(ov.bg if ov.bg is not None else c.bg),
        bg_color=(ov.bg_color if ov.bg_color is not None else c.bg_color),
        accent=(ov.accent or c.accent),
        title_max=int(ov.title_max or c.title_max),
        subtitle_max=int(ov.subtitle_max or c.subtitle_max),
        footer=(ov.footer if ov.footer is not None else c.footer),
        font=(ov.font if ov.font is not None else c.font),
        jpeg=(c.jpeg if ov.jpeg is None else bool(ov.jpeg)),
    )
    ext = ".jpg" if cfg.jpeg else ".png"

    cdir = Path(out_dir) if out_dir else settings.cover_dir

    # ---------- 单张模式 ----------
    if table is None:
        t = (title or "").strip() or title_of(text or "", cfg.title_max) or "未命名"
        nm = name or "cover"
        cdir.mkdir(parents=True, exist_ok=True)
        p = make_cover(t, cdir / f"{nm}{ext}", cfg, subtitle=subtitle)
        log.info("封面完成：%s（%s）", p, t)
        return p

    # ---------- 批量模式 ----------
    from .core.cover import CoverError

    path = Path(table)
    rows, columns = read_table(path, sheet=sheet)
    if not rows:
        raise ValueError(f"{path} 没有数据行")

    text_col = resolve_column(columns, text_column) if text_column in columns else ""
    title_col = resolve_column(columns, title_column) if title_column in columns else ""
    sub_col = resolve_column(columns, subtitle_column) if subtitle_column in columns else ""
    name_col = resolve_column(columns, name_column) if name_column else _default_name_col(columns)

    if limit:
        rows = rows[:limit]
    cdir.mkdir(parents=True, exist_ok=True)

    ok = 0
    reused = 0
    failed = 0
    paths: dict[str, Path] = {}
    for i, r in enumerate(rows, 1):
        nm = _row_name(r, name_col, i)
        raw_title = str(r.get(title_col) or "").strip() if title_col else ""
        body = str(r.get(text_col) or "").strip() if text_col else ""
        t = raw_title or title_of(body, cfg.title_max) or nm
        sub = (str(r.get(sub_col) or "").strip() if sub_col else "") or subtitle

        sig = _cover_sig(t, sub, cfg)
        if skip_existing and _cover_fresh(cdir, nm, sig, ext):
            paths[nm] = cdir / f"{nm}{ext}"
            reused += 1
            continue
        try:
            p = make_cover(t, cdir / f"{nm}{ext}", cfg, subtitle=sub)
        except CoverError:
            # Pillow 没装 / 系统里没有中文字体：这是环境问题，不是某一行的数据问题。
            # 静默吞掉的话用户会看到「完成 0 张」却不知道原因，所以直接抛出去。
            raise
        except Exception as e:  # noqa: BLE001
            log.error("封面生成失败 %s：%s", nm, str(e)[:120])
            failed += 1
            continue
        try:
            (cdir / f"{nm}.cover.txt").write_text(sig, encoding="utf-8")
        except OSError as e:  # noqa: BLE001
            log.warning("写入封面指纹失败 %s：%s", nm, str(e)[:80])
        paths[nm] = p
        ok += 1

    log.info("封面完成 %d 张（沿用已有 %d 张，失败 %d 张）-> %s", ok, reused, failed, cdir)

    final: list[tuple[Any, str]] = []
    for i, r in enumerate(rows, 1):
        nm = _row_name(r, name_col, i)
        hit = paths.get(nm) or (cdir / f"{nm}{ext}")
        final.append((i, str(hit.resolve()) if hit.is_file() else ""))
    write_column(path, out_column, final, sheet=sheet)
    return path


def stage_publish(
    settings: Settings,
    table: Path | str,
    *,
    text_column: str = "仿写文案",
    name_column: str | None = None,
    video_column: str = "视频",
    cover_column: str = "封面",
    title_column: str = "推荐标题",
    topic_column: str = "话题",
    out_dir: Path | str | None = None,
    platforms: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    prompt: str | None = None,
    prompt_file: str | None = None,
    keywords: list[str] | None = None,
    cover: str | None = None,
    sheet: str | None = None,
    limit: int | None = None,
    concurrency: int | None = None,
    skip_existing: bool = True,
    use_llm: bool = True,
    bundle: bool = False,
) -> Path:
    """阶段五（可选）：成片 -> 发布素材包 + 清单。

    注意这里**不发视频**：视频号、抖音都没有对普通开发者开放的「代用户发布」接口。
    本阶段只把「上传前要填的字」（标题候选 / 简介 / 话题）准备好，
    再给一份清单和可选的素材包目录，最后一步仍需手动到平台上传。
    """
    from .core.publish import PLAN_COLUMNS, generate_many, load_copy, plan_row, save_copy, write_bundle
    from .core.publish import PublishCfg as CorePublishCfg
    from .core.publish import load_prompt as load_publish_prompt

    path = Path(table)
    rows, columns = read_table(path, sheet=sheet)
    if not rows:
        raise ValueError(f"{path} 没有数据行")

    src_col = resolve_column(columns, text_column)
    name_col = resolve_column(columns, name_column) if name_column else _default_name_col(columns)
    video_col = resolve_column(columns, video_column) if video_column in columns else ""
    cover_col = resolve_column(columns, cover_column) if cover_column in columns else ""

    if limit:
        rows = rows[:limit]

    cfg = CorePublishCfg(
        titles=settings.publish.titles,
        desc_max=settings.publish.desc_max,
        topics=settings.publish.topics,
        platforms=platforms or settings.publish.platforms,
        concurrency=concurrency or settings.publish.concurrency,
        prompt=prompt or prompt_file or settings.publish.prompt,
    )
    pdir = Path(out_dir) if out_dir else settings.publish_dir
    pdir.mkdir(parents=True, exist_ok=True)

    system = load_publish_prompt(prompt_file or settings.publish.prompt or prompt or "")

    llm = None
    if use_llm:
        try:
            llm = build_llm(settings, provider=provider, model=model)
            log.info("发布文案模型：%s", llm.name)
        except Exception as e:  # noqa: BLE001
            log.warning("模型不可用（%s），改用规则生成标题/简介", str(e)[:100])
    if llm is None:
        log.info("发布文案：规则模式（无模型）")

    # 断点续跑：已经出过文案的直接用上次的
    names: list[str] = []
    copies: dict[str, Any] = {}
    todo: list[tuple[str, str]] = []
    for i, r in enumerate(rows, 1):
        name = _row_name(r, name_col, i)
        names.append(name)
        text = (r.get(src_col) or "").strip()
        if not text:
            continue
        old = load_copy(pdir, name) if skip_existing else None
        if old is not None:
            copies[name] = old
        else:
            todo.append((name, text))

    if todo:
        log.info("待生成发布文案 %d 条（沿用已有 %d 条）", len(todo), len(names) - len(todo))
        fresh = generate_many(todo, llm, cfg, keywords=keywords or [], system=system)
        for name, c in fresh.items():
            save_copy(pdir, name, c)
        copies.update(fresh)
    else:
        log.info("发布文案 %d 条均已存在，跳过", len(names))

    # 素材包（可选）+ 清单
    plan: list[dict] = []
    for i, (r, name) in enumerate(zip(rows, names), 1):
        c = copies.get(name) or load_copy(pdir, name)
        if c is None:
            continue
        video = str(r.get(video_col) or "").strip() if video_col else ""
        if video and not Path(video).is_file():
            found = settings.video_dir / f"{name}.mp4"
            video = str(found) if found.is_file() else ""
        # 封面兜底顺序：「封面」列 -> --cover 指定 -> 封面目录里按名字找
        cover_arg = cover or (str(settings.cover_dir) if settings.publish.auto_cover else "")
        cover_path = _pick_cover(r, cover_col, cover_arg, name)
        if bundle:
            write_bundle(pdir, name, c, video=video, cover=cover_path)
        plan.append(plan_row(i, name, c, video=video, cover=cover_path, platforms=cfg.platforms))

    plan_path = pdir / "publish_plan.xlsx"
    export_rows(plan_path, plan, PLAN_COLUMNS, sheet="发布清单")
    log.info("发布清单已写入 %s（%d 行）", plan_path, len(plan))

    # 回写表格，一表看全
    if plan:
        by_index = {p["序号"]: p for p in plan}
        write_column(
            path,
            title_column,
            [(i, by_index[i]["推荐标题"] if i in by_index else "") for i in range(1, len(rows) + 1)],
            sheet=sheet,
        )
        write_column(
            path,
            topic_column,
            [(i, by_index[i]["话题"] if i in by_index else "") for i in range(1, len(rows) + 1)],
            sheet=sheet,
        )
    return plan_path


def run_pipeline(
    settings: Settings,
    media: Path | str = "",
    workdir: Path | str | None = None,
    provider: str | None = None,
    model: str | None = None,
    prompt: str | None = None,
    voice: str | None = None,
    limit: int | None = None,
    recursive: bool = False,
    asr_language: str | None = None,
    bg_path: str | None = None,
    bgm: str | None = None,
    size: str | None = None,
    subtitle: bool | None = None,
    make_video: bool = False,
    source=None,  # core.source.SourceSpec | dict；None 时按 media 走音视频
    **rewrite_kwargs,
) -> dict[str, Path]:
    """一条龙：原始文案 -> 文案表 -> 仿写 -> 配音 -> 口播视频。

    media 参数保留是为了兼容旧调用；新的多来源请用 source=SourceSpec(...)。
    """
    from .core.source import SourceSpec

    st = settings
    if workdir:
        st.paths.workdir = str(workdir)

    if source is None:
        spec = SourceSpec(kind="media", media=str(media or ""), recursive=recursive, limit=limit)
    elif isinstance(source, dict):
        spec = SourceSpec(**{k: v for k, v in source.items() if k in SourceSpec.__dataclass_fields__})
    else:
        spec = source
    if limit and not spec.limit:
        spec.limit = limit
    if recursive:
        spec.recursive = True

    table = stage_source(
        st, spec, out=st.workdir / "transcripts.xlsx", language=asr_language, skip_existing=True, with_srt=True
    )
    stage_rewrite(
        st,
        table,
        text_column="转写文案",
        out_column="仿写文案",
        provider=provider,
        model=model,
        prompt=prompt,
        limit=limit,
        **rewrite_kwargs,
    )
    stage_tts(st, table, text_column="仿写文案", name_column="文件原名", voice=voice, limit=limit)
    out = {"table": table, "voice_dir": st.voice_dir}
    # 传了背景（或 BGM）才出视频，避免老用户升级后白跑一轮 ffmpeg
    if (bg_path or "").strip() or (bgm or "").strip() or make_video:
        stage_video(
            st,
            table,
            overrides=VideoOverrides(bg_path=bg_path, bgm=bgm, size=size, subtitle=subtitle),
            name_column="文件原名",
            limit=limit,
        )
        out["video_dir"] = st.video_dir
    return out
