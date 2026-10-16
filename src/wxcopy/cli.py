"""wxcopy 命令行入口。

wxcopy source       --from media ./videos   # 原始文案来源 -> 文案表
wxcopy transcribe   ./videos            # 视频 -> 文案表
wxcopy rewrite      ./in.xlsx --column 文案
wxcopy speak        ./in.xlsx --column 仿写文案
wxcopy video        ./in.xlsx --column 仿写文案 -b bg.jpg
wxcopy cover        ./in.xlsx            # 批量出封面；--text 出单张
wxcopy publish      ./in.xlsx            # 发布素材包 + 清单
wxcopy run          ./videos            # 一条龙
wxcopy models                           # 查看可用模型后端
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table

from .core.rewriter import list_prompts
from .core.tts import list_edge_voices
from .logging_setup import setup_logging
from .settings import PACKAGE_ROOT, Settings, load_settings, write_example_config

app = typer.Typer(
    help="视频号文案流水线：收集文案 / 多模型仿写 / 文案转配音 / 口播视频合成",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _boot(config: Optional[str], log_level: str, log_file: Optional[str]) -> Settings:
    setup_logging(log_level, log_file)
    st = load_settings(config)
    if st.config_path:
        console.print(f"[dim]已加载配置: {st.config_path}[/dim]")
    return st


@app.command("transcribe", help="阶段一：音视频转文字")
def transcribe(
    media: Path = typer.Argument(..., help="视频/音频文件或目录"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="输出表格路径"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    provider: Optional[str] = typer.Option(None, "--provider", help="faster-whisper | openai"),
    model: Optional[str] = typer.Option(None, "--model", help="whisper 模型尺寸，如 small/medium/large-v3"),
    language: Optional[str] = typer.Option(None, "--language", "-l", help="语言代码，如 zh；留空自动检测"),
    recursive: bool = typer.Option(False, "--recursive", "-r"),
    limit: Optional[int] = typer.Option(None, "--limit", help="只处理前 N 个"),
    no_srt: bool = typer.Option(False, "--no-srt", help="不生成字幕文件"),
    force: bool = typer.Option(False, "--force", help="忽略已转写结果，全部重跑"),
    workdir: Optional[Path] = typer.Option(None, "--workdir", "-w"),
    log_level: str = typer.Option("INFO", "--log-level"),
    log_file: Optional[str] = typer.Option(None, "--log-file"),
):
    st = _boot(config, log_level, log_file)
    if provider:
        st.asr.provider = provider
    if model:
        st.asr.model = model
    if workdir:
        st.paths.workdir = str(workdir)
    from .pipeline import stage_asr

    path = stage_asr(
        st,
        media,
        out=out,
        recursive=recursive,
        language=language,
        limit=limit,
        skip_existing=not force,
        with_srt=not no_srt,
    )
    console.print(f"[green]完成[/green] -> {path}")


@app.command("source", help="阶段一：收集原始文案（音视频 / 手输 / 图片OCR / Excel）-> 文案表")
def source(
    kind: str = typer.Option(
        "media", "--from", "-f", help="来源类型：media 音视频 | text 手输 | image 图片OCR | table 表格文档"
    ),
    media: Optional[Path] = typer.Option(None, "--media", "-m", help="[media] 音视频文件或目录"),
    text: Optional[str] = typer.Option(None, "--text", "-t", help="[text] 直接写一段文案"),
    name: Optional[str] = typer.Option(None, "--name", help="[text] 这条文案的命名"),
    image: Optional[List[Path]] = typer.Option(None, "--image", "-i", help="[image] 图片文件或目录，可重复"),
    table: Optional[Path] = typer.Option(None, "--table", help="[table] Excel/CSV 路径"),
    column: Optional[str] = typer.Option(None, "--column", help="[table] 文案所在列名或列序号(1起)"),
    name_column: Optional[str] = typer.Option(None, "--name-column", help="[table] 用该列命名产物"),
    sheet: Optional[str] = typer.Option(None, "--sheet"),
    ocr_provider: Optional[str] = typer.Option(
        None, "--ocr-provider", help="auto | rapidocr | tesseract | ollama | openai"
    ),
    ocr_model: Optional[str] = typer.Option(None, "--ocr-model", help="视觉模型，如 qwen2.5vl:7b"),
    recursive: bool = typer.Option(False, "--recursive", "-r"),
    limit: Optional[int] = typer.Option(None, "--limit", help="只处理前 N 个"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="输出表格路径"),
    no_srt: bool = typer.Option(False, "--no-srt", help="[media] 不生成字幕文件"),
    force: bool = typer.Option(False, "--force", help="[media] 忽略已转写结果，全部重跑"),
    workdir: Optional[Path] = typer.Option(None, "--workdir", "-w"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    log_level: str = typer.Option("INFO", "--log-level"),
    log_file: Optional[str] = typer.Option(None, "--log-file"),
):
    st = _boot(config, log_level, log_file)
    if workdir:
        st.paths.workdir = str(workdir)
    from .core.source import SourceSpec
    from .pipeline import stage_source

    spec = SourceSpec(
        kind=(kind or "media").lower(),
        media=str(media) if media else "",
        text=text or "",
        name=name or "",
        images=[str(p) for p in (image or [])],
        table=str(table) if table else "",
        table_column=column or "",
        table_sheet=sheet or "",
        name_column=name_column or "",
        ocr_provider=ocr_provider or "",
        ocr_model=ocr_model or "",
        recursive=recursive,
        limit=limit,
    )
    path = stage_source(st, spec, out=out, skip_existing=not force, with_srt=not no_srt)
    console.print(f"[green]完成[/green] -> {path}")


@app.command("ocr", help="图片转文字（单张或整个目录）")
def ocr(
    image: Optional[List[Path]] = typer.Argument(None, help="图片文件或目录，可多个；--engines 时可省略"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="结果写到文件；默认只打印"),
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p", help="auto | rapidocr | tesseract | ollama | openai"
    ),
    model: Optional[str] = typer.Option(None, "--model", help="视觉模型，如 qwen2.5vl:7b"),
    list_engines: bool = typer.Option(False, "--engines", help="只看有哪些 OCR 引擎可用"),
    workdir: Optional[Path] = typer.Option(None, "--workdir", "-w"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    log_level: str = typer.Option("INFO", "--log-level"),
    log_file: Optional[str] = typer.Option(None, "--log-file"),
):
    st = _boot(config, log_level, log_file)
    if workdir:
        st.paths.workdir = str(workdir)
    from .core.source import find_images, ocr_backends

    if list_engines:
        t = Table(title="OCR 引擎")
        t.add_column("id")
        t.add_column("名称")
        t.add_column("可用")
        t.add_column("说明")
        for r in ocr_backends(st):
            t.add_row(r["id"], r["name"], "[green]是[/green]" if r["ok"] else "[red]否[/red]", r["detail"])
        console.print(t)
        return

    from .core.source import build_ocr

    engine = build_ocr(st, provider=provider, model=model)
    files: list[Path] = []
    for x in image or []:
        files += find_images(x)
    if not files:
        raise typer.BadParameter("没有找到图片")
    console.print(f"[dim]引擎：{engine.name}，共 {len(files)} 张[/dim]")
    parts = []
    for i, f in enumerate(files, 1):
        try:
            txt = engine.read(f)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]{f.name} 识别失败：{str(e)[:150]}[/red]")
            continue
        console.print(f"[cyan]{f.name}[/cyan] ({len(txt)} 字)")
        console.print(txt or "[dim](空白)[/dim]")
        parts.append(f"# {f.name}\n{txt}")
    if out and parts:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n\n".join(parts), encoding="utf-8")
        console.print(f"[green]已写入[/green] -> {out}")


@app.command("rewrite", help="阶段二：读取表格文案列并仿写")
def rewrite(
    table: Path = typer.Argument(..., help="Excel/CSV 路径"),
    column: str = typer.Option("文案", "--column", help="文案所在列名或列序号(1起)"),
    out_column: str = typer.Option("仿写文案", "--out-column"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="另存为；默认原地回写"),
    sheet: Optional[str] = typer.Option(None, "--sheet"),
    key_column: Optional[str] = typer.Option(None, "--key-column", help="按该列匹配行（乱序安全）"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    provider: Optional[str] = typer.Option(None, "--provider", help="ollama | deepseek | dashscope | ..."),
    model: Optional[str] = typer.Option(None, "--model"),
    prompt: Optional[str] = typer.Option(None, "--prompt", help="内置模板名，见 prompts 命令"),
    prompt_file: Optional[Path] = typer.Option(None, "--prompt-file", help="自定义 md 提示词文件"),
    temperature: Optional[float] = typer.Option(None, "--temperature", "-t"),
    concurrency: Optional[int] = typer.Option(None, "--concurrency", "-j"),
    length: Optional[str] = typer.Option(None, "--length", help="篇幅要求，如 '150字左右'"),
    tone: Optional[str] = typer.Option(None, "--tone", help="语气，如 '接地气'"),
    audience: Optional[str] = typer.Option(None, "--audience", help="目标受众"),
    cta: Optional[str] = typer.Option(None, "--cta", help="结尾行动号召"),
    keep_hashtags: bool = typer.Option(False, "--keep-hashtags", help="保留 #标签"),
    keyword: Optional[str] = typer.Option(None, "--keyword", help="必须包含的关键词，逗号分隔"),
    limit: Optional[int] = typer.Option(None, "--limit"),
    force: bool = typer.Option(False, "--force", help="忽略已有仿写结果"),
    workdir: Optional[Path] = typer.Option(None, "--workdir", "-w"),
    log_level: str = typer.Option("INFO", "--log-level"),
    log_file: Optional[str] = typer.Option(None, "--log-file"),
):
    st = _boot(config, log_level, log_file)
    if workdir:
        st.paths.workdir = str(workdir)
    from .pipeline import stage_rewrite

    kws = [k.strip() for k in (keyword or "").split(",") if k.strip()]
    path = stage_rewrite(
        st,
        table,
        text_column=column,
        out_column=out_column,
        provider=provider,
        model=model,
        prompt=prompt,
        prompt_file=str(prompt_file) if prompt_file else None,
        temperature=temperature,
        concurrency=concurrency,
        length=length,
        tone=tone,
        audience=audience,
        cta=cta,
        keep_hashtags=keep_hashtags or None,
        keywords=kws,
        sheet=sheet,
        key_column=key_column,
        out=out,
        limit=limit,
        skip_existing=not force,
    )
    console.print(f"[green]完成[/green] -> {path}")


@app.command("speak", help="阶段三：文案转音频")
def speak(
    table: Path = typer.Argument(..., help="Excel/CSV 路径"),
    column: str = typer.Option("仿写文案", "--column"),
    name_column: Optional[str] = typer.Option(None, "--name-column", help="用该列的值命名音频文件"),
    out_dir: Optional[Path] = typer.Option(None, "--out-dir"),
    out_column: str = typer.Option("配音", "--out-column"),
    sheet: Optional[str] = typer.Option(None, "--sheet"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    provider: Optional[str] = typer.Option(None, "--provider", help="edge | openai | pyttsx3"),
    voice: Optional[str] = typer.Option(None, "--voice", help="音色，如 zh-CN-XiaoxiaoNeural"),
    rate: Optional[str] = typer.Option(None, "--rate", help="语速，如 +10%"),
    concurrency: int = typer.Option(2, "--concurrency", "-j"),
    limit: Optional[int] = typer.Option(None, "--limit"),
    force: bool = typer.Option(False, "--force"),
    workdir: Optional[Path] = typer.Option(None, "--workdir", "-w"),
    log_level: str = typer.Option("INFO", "--log-level"),
    log_file: Optional[str] = typer.Option(None, "--log-file"),
):
    st = _boot(config, log_level, log_file)
    if provider:
        st.tts.provider = provider
    if rate:
        st.tts.rate = rate
    if workdir:
        st.paths.workdir = str(workdir)
    from .pipeline import stage_tts

    path = stage_tts(
        st,
        table,
        text_column=column,
        name_column=name_column,
        out_dir=out_dir,
        voice=voice,
        out_column=out_column,
        sheet=sheet,
        concurrency=concurrency,
        limit=limit,
        skip_existing=not force,
    )
    console.print(f"[green]完成[/green] -> {path}")


@app.command("video", help="阶段四：仿写文案 + 配音 -> 口播视频（可指定图片/视频背景）")
def video(
    table: Path = typer.Argument(..., help="Excel/CSV 路径"),
    column: str = typer.Option("仿写文案", "--column", help="文案所在列"),
    name_column: Optional[str] = typer.Option(None, "--name-column", help="用该列命名视频文件"),
    audio_column: str = typer.Option("配音", "--audio-column", help="配音路径所在列"),
    out_dir: Optional[Path] = typer.Option(None, "--out-dir"),
    out_column: str = typer.Option("视频", "--out-column"),
    bg: Optional[Path] = typer.Option(None, "--bg", "-b", help="背景图片或视频；留空用 --bg-color 纯色"),
    bg_color: Optional[str] = typer.Option(None, "--bg-color", help="纯色背景，如 #1e293b"),
    fit: Optional[str] = typer.Option(None, "--fit", help="cover（裁满，默认）| contain（居中留边）"),
    size: Optional[str] = typer.Option(
        None, "--size", help="竖屏 1080x1920 / 竖屏 720x1280 / 横屏 1920x1080 / 方形 1080x1080"
    ),
    width: Optional[int] = typer.Option(None, "--width"),
    height: Optional[int] = typer.Option(None, "--height"),
    no_subtitle: bool = typer.Option(False, "--no-subtitle", help="不烧录字幕"),
    style: Optional[str] = typer.Option(
        None, "--style", help="字幕样式：classic（底部整句，默认）| big（中上部大字 + 关键词高亮）"
    ),
    highlight: Optional[str] = typer.Option(
        None, "--highlight", help="big 样式高亮：none | alternate（隔行绿）| keywords | auto"
    ),
    keyword: Optional[str] = typer.Option(None, "--keyword", help="--highlight keywords 时要染绿的关键词，逗号分隔"),
    highlight_color: Optional[str] = typer.Option(None, "--highlight-color", help="高亮色，默认 #00F090"),
    big_chars_per_line: Optional[int] = typer.Option(None, "--big-chars-per-line", help="big 每行最多几个字，默认 4"),
    big_lines_per_screen: Optional[int] = typer.Option(None, "--big-lines-per-screen", help="big 每屏最多几行，默认 3"),
    typewriter: Optional[bool] = typer.Option(
        None, "--typewriter/--no-typewriter", help="big 样式大字逐字打出（打字机效果）"
    ),
    type_speed: Optional[float] = typer.Option(
        None, "--type-speed", help="打字机每字固定秒数，如 0.08；0 = 按本屏时长自适应"
    ),
    type_hold: Optional[float] = typer.Option(
        None, "--type-hold", help="自适应模式下打完整行后停留占屏时长比例，默认 0.3"
    ),
    type_min: Optional[float] = typer.Option(None, "--type-min", help="自适应模式每字时长下限（秒），默认 0.045"),
    font: Optional[str] = typer.Option(None, "--font"),
    font_size: Optional[int] = typer.Option(None, "--font-size"),
    margin_v: Optional[int] = typer.Option(None, "--margin-v", help="字幕距底部像素"),
    chars_per_line: Optional[int] = typer.Option(None, "--chars-per-line", help="每行最多几个字"),
    tail_pad: Optional[float] = typer.Option(None, "--tail-pad", help="语音结束后的留白秒数，默认 0.6"),
    bgm: Optional[Path] = typer.Option(None, "--bgm", help="背景音乐文件；人声出现时自动压低（闪避）"),
    bgm_volume: Optional[float] = typer.Option(None, "--bgm-volume", help="BGM 相对人声的音量（0~1），默认 0.18"),
    bgm_duck: Optional[bool] = typer.Option(None, "--bgm-duck/--no-bgm-duck", help="人声出现时自动压低 BGM，默认开"),
    bgm_duck_ratio: Optional[float] = typer.Option(None, "--bgm-duck-ratio", help="闪避压缩比，越大压得越狠，默认 8"),
    bgm_fade_out: Optional[float] = typer.Option(None, "--bgm-fade-out", help="BGM 结尾淡出秒数，默认 1.5"),
    fade_in: Optional[float] = typer.Option(None, "--fade-in", help="开头淡入秒数"),
    crf: Optional[int] = typer.Option(None, "--crf", help="画质，越小越清晰（18-28）"),
    sheet: Optional[str] = typer.Option(None, "--sheet"),
    limit: Optional[int] = typer.Option(None, "--limit"),
    concurrency: Optional[int] = typer.Option(None, "--concurrency", "-j"),
    force: bool = typer.Option(False, "--force"),
    workdir: Optional[Path] = typer.Option(None, "--workdir", "-w"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    log_level: str = typer.Option("INFO", "--log-level"),
    log_file: Optional[str] = typer.Option(None, "--log-file"),
):
    st = _boot(config, log_level, log_file)
    if workdir:
        st.paths.workdir = str(workdir)
    from .pipeline import VideoOverrides, stage_video

    ov = VideoOverrides(
        bg_path=str(bg) if bg else None,
        bg_color=bg_color,
        fit=fit,
        size=size,
        width=width,
        height=height,
        subtitle=(False if no_subtitle else None),
        style=style,
        highlight_mode=highlight,
        keywords=[k.strip() for k in (keyword or "").split(",") if k.strip()] or None,
        highlight_color=highlight_color,
        big_chars_per_line=big_chars_per_line,
        big_lines_per_screen=big_lines_per_screen,
        typewriter=typewriter,
        type_speed=type_speed,
        type_hold=type_hold,
        type_min=type_min,
        font=font,
        font_size=font_size,
        margin_v=margin_v,
        chars_per_line=chars_per_line,
        tail_pad=tail_pad,
        fade_in=fade_in,
        crf=crf,
        bgm=str(bgm) if bgm else None,
        bgm_volume=bgm_volume,
        bgm_duck=bgm_duck,
        bgm_duck_ratio=bgm_duck_ratio,
        bgm_fade_out=bgm_fade_out,
    )
    stage_video(
        st,
        table,
        ov,
        text_column=column,
        name_column=name_column,
        audio_column=audio_column,
        out_dir=out_dir,
        out_column=out_column,
        concurrency=concurrency,
        sheet=sheet,
        limit=limit,
        skip_existing=not force,
    )
    console.print(f"[green]完成[/green] -> {out_dir or st.video_dir}")


@app.command("cover", help="成片封面：标题/副标题 -> 1080x1920 竖图（可单张也可整表批量）")
def cover(
    table: Optional[Path] = typer.Argument(None, help="Excel/CSV 路径；单张模式可省略"),
    text: Optional[str] = typer.Option(None, "--text", "-t", help="单张模式：直接给一段文案（自动提炼标题）"),
    title: Optional[str] = typer.Option(None, "--title", help="单张模式：直接指定封面主标题"),
    subtitle: Optional[str] = typer.Option(None, "--subtitle", help="副标题（批量时作为所有行的默认值）"),
    name: Optional[str] = typer.Option(None, "--name", help="单张模式：输出文件名（不含后缀）"),
    title_column: str = typer.Option("推荐标题", "--title-column", help="批量：主标题所在列（没有则从文案列提炼）"),
    column: str = typer.Option("仿写文案", "--column", help="批量：文案所在列，用来提炼标题"),
    subtitle_column: Optional[str] = typer.Option(None, "--subtitle-column", help="批量：副标题所在列"),
    name_column: Optional[str] = typer.Option(None, "--name-column", help="批量：用该列命名产物"),
    out_dir: Optional[Path] = typer.Option(None, "--out-dir", help="输出目录，默认 {workdir}/cover"),
    out_column: str = typer.Option("封面", "--out-column", help="批量：回写封面路径的列名"),
    style: Optional[str] = typer.Option(None, "--style", help="tech 深色渐变（默认）| plain 纯色 | light 浅色"),
    bg: Optional[Path] = typer.Option(None, "--bg", "-b", help="背景图；留空按风格本地生成"),
    bg_color: Optional[str] = typer.Option(None, "--bg-color", help="plain/light 的底色，如 #0f172a"),
    accent: Optional[str] = typer.Option(None, "--accent", help="强调色，默认 #00F090"),
    footer: Optional[str] = typer.Option(None, "--footer", help="页脚小字，如你的账号名"),
    font: Optional[str] = typer.Option(None, "--font", help="字体名或字体文件路径；留空自动挑中文字体"),
    title_max: Optional[int] = typer.Option(None, "--title-max", help="主标题最多几个字，默认 20"),
    subtitle_max: Optional[int] = typer.Option(None, "--subtitle-max", help="副标题最多几个字，默认 24"),
    jpeg: Optional[bool] = typer.Option(None, "--jpeg/--png", help="输出 .jpg（体积小）而不是 .png"),
    sheet: Optional[str] = typer.Option(None, "--sheet"),
    limit: Optional[int] = typer.Option(None, "--limit"),
    force: bool = typer.Option(False, "--force", help="忽略已有封面，全部重画"),
    workdir: Optional[Path] = typer.Option(None, "--workdir", "-w"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    log_level: str = typer.Option("INFO", "--log-level"),
    log_file: Optional[str] = typer.Option(None, "--log-file"),
):
    st = _boot(config, log_level, log_file)
    if workdir:
        st.paths.workdir = str(workdir)
    from .core.cover import CoverError
    from .pipeline import CoverOverrides, stage_cover

    if table is None and not (text or title):
        raise typer.BadParameter("要么给一个表格路径，要么用 --text / --title 出单张")

    ov = CoverOverrides(
        style=style,
        bg=str(bg) if bg else None,
        bg_color=bg_color,
        accent=accent,
        footer=footer,
        font=font,
        title_max=title_max,
        subtitle_max=subtitle_max,
        jpeg=jpeg,
    )
    try:
        out = stage_cover(
            st,
            table,
            ov,
            text=text,
            title=title,
            subtitle=subtitle or "",
            name=name or "",
            title_column=title_column,
            text_column=column,
            subtitle_column=subtitle_column or "",
            name_column=name_column,
            out_dir=out_dir,
            out_column=out_column,
            sheet=sheet,
            limit=limit,
            skip_existing=not force,
        )
    except CoverError as e:
        console.print(f"[red]封面生成失败[/red]：{e}")
        raise typer.Exit(code=1) from e
    console.print(f"[green]完成[/green] -> {out}")


@app.command("publish", help="阶段五：成片 -> 发布素材包（标题/简介/话题 + 清单）")
def publish(
    table: Path = typer.Argument(..., help="Excel/CSV 路径（即文案表）"),
    column: str = typer.Option("转写文案", "--column", help="文案所在列（用来提炼标题）"),
    name_column: Optional[str] = typer.Option(None, "--name-column", help="用该列命名产物"),
    video_column: str = typer.Option("视频", "--video-column"),
    cover_column: str = typer.Option("封面", "--cover-column"),
    title_column: str = typer.Option("推荐标题", "--title-column", help="回写的标题列名"),
    topic_column: str = typer.Option("话题", "--topic-column", help="回写的话题列名"),
    out_dir: Optional[Path] = typer.Option(None, "--out-dir", help="素材包目录，默认 {workdir}/publish"),
    cover: Optional[Path] = typer.Option(None, "--cover", help="封面文件或目录（目录里按名字找同名图）"),
    platforms: Optional[str] = typer.Option(None, "--platforms", help="清单里的平台，如 '视频号,抖音'"),
    keyword: Optional[str] = typer.Option(None, "--keyword", help="话题里必带的关键词，逗号分隔"),
    sheet: Optional[str] = typer.Option(None, "--sheet"),
    provider: Optional[str] = typer.Option(None, "--provider", help="ollama | deepseek | ..."),
    model: Optional[str] = typer.Option(None, "--model"),
    prompt: Optional[str] = typer.Option(None, "--prompt", help="内置模板名，见 prompts 命令"),
    prompt_file: Optional[Path] = typer.Option(None, "--prompt-file", help="自定义 md 提示词文件"),
    no_llm: bool = typer.Option(False, "--no-llm", help="不调模型，纯规则生成（离线可用）"),
    bundle: bool = typer.Option(False, "--bundle", help="把视频+封面+文案复制进独立文件夹"),
    concurrency: Optional[int] = typer.Option(None, "--concurrency", "-j"),
    limit: Optional[int] = typer.Option(None, "--limit"),
    force: bool = typer.Option(False, "--force", help="已有文案也重新生成"),
    workdir: Optional[Path] = typer.Option(None, "--workdir", "-w"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    log_level: str = typer.Option("INFO", "--log-level"),
    log_file: Optional[str] = typer.Option(None, "--log-file"),
):
    st = _boot(config, log_level, log_file)
    if workdir:
        st.paths.workdir = str(workdir)
    from .pipeline import stage_publish

    kws = [k.strip() for k in (keyword or "").split(",") if k.strip()]
    plan = stage_publish(
        st,
        table,
        text_column=column,
        name_column=name_column,
        video_column=video_column,
        cover_column=cover_column,
        title_column=title_column,
        topic_column=topic_column,
        out_dir=out_dir,
        platforms=platforms,
        provider=provider,
        model=model,
        prompt=prompt,
        prompt_file=str(prompt_file) if prompt_file else None,
        keywords=kws,
        cover=str(cover) if cover else None,
        sheet=sheet,
        limit=limit,
        concurrency=concurrency,
        skip_existing=not force,
        use_llm=not no_llm,
        bundle=bundle,
    )
    console.print(f"[green]完成[/green] -> {plan}")
    console.print("[dim]提示：视频号/抖音没有开放的发布接口，请到平台手动上传，素材在清单里。[/dim]")


@app.command("run", help="一条龙：原始文案 -> 仿写 -> 配音 -> 视频")
def run(
    media: Optional[Path] = typer.Argument(None, help="[media] 音视频目录；用其他来源时可省略"),
    workdir: Path = typer.Option(Path("./output"), "--workdir", "-w"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    kind: str = typer.Option(
        "media", "--from", "-f", help="来源：media 音视频 | text 手输 | image 图片OCR | table 表格"
    ),
    text: Optional[str] = typer.Option(None, "--text", "-t", help="[text] 手输文案"),
    image: Optional[List[Path]] = typer.Option(None, "--image", "-i", help="[image] 图片文件或目录"),
    table: Optional[Path] = typer.Option(None, "--table", help="[table] Excel/CSV 路径"),
    column: Optional[str] = typer.Option(None, "--column", help="[table] 文案所在列"),
    name_column: Optional[str] = typer.Option(None, "--name-column", help="[table] 命名列"),
    ocr_provider: Optional[str] = typer.Option(None, "--ocr-provider"),
    ocr_model: Optional[str] = typer.Option(None, "--ocr-model"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    model: Optional[str] = typer.Option(None, "--model"),
    prompt: Optional[str] = typer.Option(None, "--prompt"),
    voice: Optional[str] = typer.Option(None, "--voice"),
    limit: Optional[int] = typer.Option(None, "--limit"),
    recursive: bool = typer.Option(False, "--recursive", "-r"),
    language: Optional[str] = typer.Option(None, "--language", "-l"),
    bg: Optional[Path] = typer.Option(None, "--bg", "-b", help="给了背景就顺带合成口播视频"),
    bgm: Optional[Path] = typer.Option(None, "--bgm", help="背景音乐；给了也会触发视频合成"),
    size: Optional[str] = typer.Option(None, "--size"),
    no_subtitle: bool = typer.Option(False, "--no-subtitle"),
    log_level: str = typer.Option("INFO", "--log-level"),
    log_file: Optional[str] = typer.Option(None, "--log-file"),
):
    st = _boot(config, log_level, log_file)
    from .core.source import SourceSpec
    from .pipeline import run_pipeline

    spec = SourceSpec(
        kind=(kind or "media").lower(),
        media=str(media) if media else "",
        text=text or "",
        images=[str(p) for p in (image or [])],
        table=str(table) if table else "",
        table_column=column or "",
        name_column=name_column or "",
        ocr_provider=ocr_provider or "",
        ocr_model=ocr_model or "",
        recursive=recursive,
        limit=limit,
    )
    res = run_pipeline(
        st,
        media or "",
        workdir=workdir,
        provider=provider,
        model=model,
        prompt=prompt,
        voice=voice,
        limit=limit,
        recursive=recursive,
        asr_language=language,
        bg_path=str(bg) if bg else None,
        bgm=str(bgm) if bgm else None,
        size=size,
        subtitle=(False if no_subtitle else None),
        make_video=bool(bg or bgm),
        source=spec,
    )
    console.print(f"[green]全部完成[/green]\n  表格: {res['table']}\n  音频: {res['voice_dir']}")
    if res.get("video_dir"):
        console.print(f"  视频: {res['video_dir']}")


@app.command("models", help="查看模型后端与可用模型")
def models(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    provider: Optional[str] = typer.Option(None, "--provider", help="只看某一个"),
    as_json: bool = typer.Option(False, "--json"),
):
    st = load_settings(config)
    from .core.llm import build_llm, probe_all

    if provider:
        llm = build_llm(st, provider=provider)
        ok, msg = llm.health()
        console.print(f"{provider}: {'可用' if ok else '不可用'} ({msg})")
        if ok:
            for m in llm.list_models():
                console.print(f"  - {m}")
        return

    rows = probe_all(st)
    if as_json:
        console.print_json(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    t = Table(title="模型后端")
    t.add_column("provider")
    t.add_column("类型")
    t.add_column("状态")
    t.add_column("说明")
    for r in rows:
        t.add_row(
            r["provider"],
            r["type"],
            "[green]可用[/green]" if r["ok"] else "[red]不可用[/red]",
            (r["detail"] or "")[:60],
        )
    console.print(t)
    for r in rows:
        if r["ok"] and r["models"]:
            console.print(
                f"[bold]{r['provider']}[/bold]: {', '.join(r['models'][:12])}"
                + (" ..." if len(r["models"]) > 12 else "")
            )


@app.command("fetch-model", help="预先下载 Whisper 模型（跑批前先把网络问题解决掉）")
def fetch_model(
    model: str = typer.Option("small", "--model", help="tiny/base/small/medium/large-v3"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
):
    st = load_settings(config)
    st.asr.model = model
    from .core.asr import prefetch_model

    p = prefetch_model(st.asr)
    console.print(f"[green]模型已就绪[/green] {model} -> {p}")


@app.command("voices", help="列出可用音色")
def voices(
    locale: str = typer.Option("zh-", "--locale"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
):
    st = load_settings(config)
    if st.tts.provider != "edge":
        console.print(
            f"当前 provider={st.tts.provider}，音色列表仅 edge 支持在线枚举；其他引擎请在配置里直接写音色 id。"
        )
        return
    try:
        vs = list_edge_voices(locale)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]拉取失败（需要联网）[/red]: {e}")
        return
    t = Table(title=f"Edge 音色（{locale}*）")
    t.add_column("音色 id")
    t.add_column("性别")
    t.add_column("地区")
    for v in vs:
        t.add_row(v["ShortName"], v.get("Gender", ""), v.get("Locale", ""))
    console.print(t)


@app.command("prompts", help="列出内置仿写模板")
def prompts():
    for name in list_prompts():
        console.print(f"  - {name}")


@app.command("init-config", help="生成一份配置模板")
def init_config(
    dest: Path = typer.Option(Path("./wxcopy.yaml"), "--dest", "-d"),
):
    p = write_example_config(Path(dest))
    console.print(f"[green]已生成[/green] {p}\n云端厂商请设置对应环境变量（见 .env.example），本地 Ollama 开箱即用。")


@app.command("web", help="启动本地 Web 控制台（选目录/选模型/选风格/看进度）")
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址，默认只本机可访问"),
    port: int = typer.Option(8765, "--port", "-p"),
    no_browser: bool = typer.Option(False, "--no-browser", help="不自动打开浏览器"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    log_level: str = typer.Option("INFO", "--log-level"),
):
    from .web.app import serve

    setup_logging(log_level)
    console.print(f"[green]wxcopy 控制台已启动[/green]  http://{host}:{port}")
    console.print("[dim]Ctrl+C 停止服务[/dim]")
    if config:
        console.print(f"[dim]配置文件: {config}[/dim]")
    serve(host=host, port=port, open_browser=not no_browser, log_level=log_level)


@app.command("version")
def version():
    from . import __version__

    console.print(f"wxcopy {__version__}  (项目根目录 {PACKAGE_ROOT})")


if __name__ == "__main__":
    app()
