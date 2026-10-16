"""本地 Web 控制台后端。

只监听 127.0.0.1，纯本机使用。提供：
- 目录浏览（服务端列目录，解决浏览器拿不到真实路径的问题）
- 模型 / 仿写风格 / 音色 的枚举
- 流水线任务的启动、进度、日志、取消
"""

from __future__ import annotations

import os
import string
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response

from ..core.asr import MEDIA_EXTS
from ..core.rewriter import list_prompts
from ..core.tts import list_edge_voices
from ..logging_setup import get_logger, setup_logging
from ..settings import Settings, load_settings
from .jobs import JobManager, make_runner

log = get_logger()

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 模型枚举结果缓存（探测云厂商会联网，避免每次刷新都打一遍）
_models_cache: dict[str, Any] = {"at": 0.0, "data": None}
_voices_cache: dict[str, Any] = {"at": 0.0, "data": None}
CACHE_TTL = 300

manager = JobManager()


def create_app() -> FastAPI:
    # 直接用 uvicorn 起（不经过 serve）时也要把日志级别放开，
    # 否则 root logger 默认 WARNING，INFO 级进度日志会被丢掉，页面看不到进度。
    setup_logging(os.environ.get("WXCOPY_LOG_LEVEL", "INFO"))
    app = FastAPI(title="wxcopy 控制台", docs_url="/api/docs", redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "pid": os.getpid(), "python": sys.version.split()[0]}

    # ---------- 目录浏览 ----------
    @app.get("/api/roots")
    def roots() -> dict:
        items: list[dict] = []
        if os.name == "nt":
            for d in string.ascii_uppercase:
                p = Path(f"{d}:\\")
                if p.exists():
                    items.append({"name": f"{d}: 盘", "path": str(p)})
        else:
            items.append({"name": "根目录", "path": "/"})
        home = Path.home()
        for label, p in [("用户目录", home), ("桌面", home / "Desktop"), ("下载", home / "Downloads")]:
            if p.exists():
                items.append({"name": label, "path": str(p)})
        return {"roots": items}

    @app.get("/api/browse")
    def browse(path: str = "", kind: str = "media") -> dict:
        if not path:
            return {"path": "", "parent": None, "dirs": [], "files": [], "media_count": 0, "error": ""}
        from ..core.source import TABLE_EXTS
        from ..core.video import IMAGE_EXTS, VIDEO_EXTS

        wanted = {
            "media": MEDIA_EXTS,
            "audio": MEDIA_EXTS - VIDEO_EXTS,
            "video": VIDEO_EXTS,
            "image": IMAGE_EXTS,
            "table": TABLE_EXTS,  # 选 Excel/CSV 当文案来源
            "bg": VIDEO_EXTS | IMAGE_EXTS,  # 背景：图片或视频都行
            "any": None,
        }.get(kind, MEDIA_EXTS)

        p = Path(path)
        if not p.exists():
            raise HTTPException(400, f"路径不存在：{path}")
        if not p.is_dir():
            p = p.parent
        dirs, files = [], []
        try:
            for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                if child.name.startswith("."):
                    continue
                if child.is_dir():
                    dirs.append({"name": child.name, "path": str(child)})
                elif wanted is None or child.suffix.lower() in wanted:
                    files.append({"name": child.name, "path": str(child), "size": child.stat().st_size})
        except PermissionError:
            return {
                "path": str(p),
                "parent": str(p.parent) if p.parent else None,
                "dirs": dirs,
                "files": files[:50],
                "media_count": len(files),
                "error": "无权限读取该目录",
            }
        return {
            "path": str(p),
            "parent": str(p.parent) if p.parent != p else None,
            "dirs": dirs,
            "files": files[:50],
            "media_count": len(files),
            "kind": kind,
            "error": "",
        }

    # ---------- 枚举：模型 / 风格 / 音色 ----------
    @app.get("/api/models")
    def models(refresh: bool = False) -> dict:
        import time

        now = time.time()
        if not refresh and _models_cache["data"] and now - _models_cache["at"] < CACHE_TTL:
            return {"cached": True, **_models_cache["data"]}
        st = load_settings()
        from ..core.llm import probe_all

        rows = probe_all(st)
        for r in rows:
            r["models"] = r.get("models") or []
            r["model_count"] = len(r["models"])
        data = {"providers": rows, "default_provider": st.llm.provider, "default_model": st.llm.model}
        _models_cache.update(at=now, data=data)
        return {"cached": False, **data}

    @app.get("/api/prompts")
    def prompts() -> dict:
        return {"prompts": list_prompts()}

    @app.get("/api/voices")
    def voices(refresh: bool = False) -> dict:
        import time

        now = time.time()
        if not refresh and _voices_cache["data"] and now - _voices_cache["at"] < CACHE_TTL:
            return {"cached": True, **_voices_cache["data"]}
        try:
            vs = list_edge_voices("zh-")
            data = {
                "voices": [
                    {"id": v["ShortName"], "gender": v.get("Gender", ""), "locale": v.get("Locale", "")} for v in vs
                ],
                "error": "",
            }
        except Exception as e:  # noqa: BLE001
            data = {
                "voices": [
                    {"id": "zh-CN-XiaoxiaoNeural", "gender": "Female", "locale": "zh-CN"},
                    {"id": "zh-CN-YunxiNeural", "gender": "Male", "locale": "zh-CN"},
                    {"id": "zh-CN-YunjianNeural", "gender": "Male", "locale": "zh-CN"},
                    {"id": "zh-CN-XiaoyiNeural", "gender": "Female", "locale": "zh-CN"},
                ],
                "error": f"在线音色拉取失败（{str(e)[:80]}），已回退到常用列表",
            }
        _voices_cache.update(at=now, data=data)
        return {"cached": False, **data}

    @app.get("/api/defaults")
    def defaults() -> dict:
        from ..core.publish import list_prompts as list_publish_prompts
        from ..core.source import ocr_backends
        from ..core.video import PRESETS

        st: Settings = load_settings()
        return {
            "workdir": str(st.workdir),
            "asr_models": ["tiny", "base", "small", "medium", "large-v3"],
            "asr_model": st.asr.model,
            "language": st.asr.language,
            # ---- 原始文案来源 ----
            "source_kinds": [
                {"id": "media", "name": "音视频目录（转写）"},
                {"id": "text", "name": "手输文案"},
                {"id": "image", "name": "图片 OCR"},
                {"id": "table", "name": "Excel / CSV 表格"},
            ],
            "ocr_provider": st.ocr.provider,
            "ocr_model": st.ocr.model,
            "ocr_engines": ocr_backends(st),
            "provider": st.llm.provider,
            "model": st.llm.model,
            "temperature": st.llm.temperature,
            "prompt": st.rewrite.prompt,
            "concurrency": st.rewrite.concurrency,
            "tts_provider": st.tts.provider,
            "voice": st.tts.voice,
            # ---- 视频合成 ----
            "video_presets": list(PRESETS.keys()),
            "video_styles": [
                {"id": "classic", "name": "经典 · 底部整句"},
                {"id": "big", "name": "大字 · 中上部关键词高亮"},
            ],
            "video_highlights": [
                {"id": "none", "name": "不高亮"},
                {"id": "alternate", "name": "隔行高亮（首行绿）"},
                {"id": "auto", "name": "自动（数字/营销词）"},
                {"id": "keywords", "name": "指定关键词"},
            ],
            "video_size": st.video.size,
            "video_bg_path": st.video.bg_path,
            "video_bg_color": st.video.bg_color,
            "video_fit": st.video.fit,
            "video_style": st.video.style,
            "video_subtitle": st.video.subtitle,
            "video_font": st.video.font,
            "video_font_size": st.video.font_size,
            "video_margin_v": st.video.margin_v,
            "video_chars_per_line": st.video.chars_per_line,
            "video_highlight_mode": st.video.highlight_mode,
            "video_highlight_color": st.video.highlight_color,
            "video_keywords": st.video.keywords,
            "video_big_chars_per_line": st.video.big_chars_per_line,
            "video_big_lines_per_screen": st.video.big_lines_per_screen,
            "video_typewriter": st.video.typewriter,
            "video_type_speed": st.video.type_speed,
            "video_type_hold": st.video.type_hold,
            "video_type_min": st.video.type_min,
            "video_crf": st.video.crf,
            "video_tail_pad": st.video.tail_pad,
            "video_fade_in": st.video.fade_in,
            "video_concurrency": st.video.concurrency,
            # ---- 背景音乐 ----
            "video_bgm": st.video.bgm,
            "video_bgm_volume": st.video.bgm_volume,
            "video_bgm_duck": st.video.bgm_duck,
            "video_bgm_duck_ratio": st.video.bgm_duck_ratio,
            "video_bgm_fade_out": st.video.bgm_fade_out,
            # ---- 封面 ----
            "cover_styles": [
                {"id": "tech", "name": "科技 · 深色渐变 + 网格"},
                {"id": "plain", "name": "纯色 · 干净"},
                {"id": "light", "name": "浅色 · 白底深字"},
            ],
            "cover_style": st.cover.style,
            "cover_bg": st.cover.bg,
            "cover_bg_color": st.cover.bg_color,
            "cover_accent": st.cover.accent,
            "cover_footer": st.cover.footer,
            "cover_font": st.cover.font,
            "cover_title_max": st.cover.title_max,
            "cover_subtitle_max": st.cover.subtitle_max,
            "cover_jpeg": st.cover.jpeg,
            "cover_dir": str(st.cover_dir),
            # ---- 发布素材包 ----
            "publish_titles": st.publish.titles,
            "publish_desc_max": st.publish.desc_max,
            "publish_topics": st.publish.topics,
            "publish_platforms": st.publish.platforms,
            "publish_concurrency": st.publish.concurrency,
            "publish_auto_cover": st.publish.auto_cover,
            "publish_prompts": list_publish_prompts(),
            "voice_dir": str(st.voice_dir),
            "video_dir": str(st.video_dir),
        }

    @app.get("/api/ocr")
    def ocr_engines() -> dict:
        from ..core.source import ocr_backends

        st = load_settings()
        return {"engines": ocr_backends(st), "default": st.ocr.provider, "model": st.ocr.model}

    @app.post("/api/table/columns")
    def table_columns(payload: dict) -> dict:
        """列出表格的列名，供前端选「文案列 / 命名列」。"""
        path = (payload.get("path") or "").strip()
        if not path:
            raise HTTPException(400, "未选择表格文件")
        p = Path(path)
        if not p.is_file():
            raise HTTPException(400, f"文件不存在：{p}")
        if p.suffix.lower() not in {".xlsx", ".xlsm", ".xls", ".csv", ".tsv"}:
            raise HTTPException(400, f"不是表格文件：{p.suffix}")
        try:
            from ..io.table import list_sheets, read_table

            sheets = list_sheets(p)
            sheet = (payload.get("sheet") or "").strip() or None
            rows, columns = read_table(p, sheet=sheet)
            # 猜一个最像「文案」的列，省得用户自己找
            guess = ""
            for key in ("文案", "内容", "正文", "原文", "标题", "文本"):
                hit = [c for c in columns if key in str(c)]
                if hit:
                    guess = hit[0]
                    break
            return {
                "columns": columns,
                "sheets": sheets,
                "rows": len(rows),
                "guess": guess or (columns[0] if columns else ""),
            }
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"读取表格失败：{str(e)[:160]}") from e

    # ---------- 封面预览 ----------
    @app.post("/api/cover/preview")
    def cover_preview(payload: dict) -> Response:
        """按当前参数实时画一张封面给页面看。

        不写进产物目录（那会让断点续跑的指纹误判成「已生成」），
        出图后立刻把临时文件删掉。
        """
        import tempfile

        from ..core.cover import CoverCfg as CoreCoverCfg
        from ..core.cover import CoverError, make_cover

        st = load_settings()
        c = st.cover
        cfg = CoreCoverCfg(
            width=int(payload.get("width") or c.width),
            height=int(payload.get("height") or c.height),
            style=(payload.get("style") or c.style),
            bg=(payload.get("bg", c.bg) or ""),
            bg_color=(payload.get("bg_color", c.bg_color) or ""),
            accent=(payload.get("accent") or c.accent),
            title_max=int(payload.get("title_max") or c.title_max),
            subtitle_max=int(payload.get("subtitle_max") or c.subtitle_max),
            footer=(payload.get("footer", c.footer) or ""),
            font=(payload.get("font", c.font) or ""),
            jpeg=bool(payload.get("jpeg", c.jpeg)),
        )
        title = (payload.get("title") or "").strip() or "封面标题预览"
        sub = (payload.get("subtitle") or "").strip()
        ext = ".jpg" if cfg.jpeg else ".png"
        fd, tmp = tempfile.mkstemp(prefix="wxcopy-cover-", suffix=ext)
        os.close(fd)
        try:
            make_cover(title, Path(tmp), cfg, subtitle=sub)
            data = Path(tmp).read_bytes()
        except CoverError as e:
            # 缺 Pillow / 缺中文字体：告诉用户怎么补，别甩一堆 traceback
            raise HTTPException(400, str(e)) from e
        finally:
            Path(tmp).unlink(missing_ok=True)
        return Response(content=data, media_type="image/jpeg" if cfg.jpeg else "image/png")

    # ---------- 任务 ----------
    @app.post("/api/run")
    def run(payload: dict) -> dict:
        src = payload.get("source") or {}
        kind = (src.get("kind") or payload.get("source_kind") or "media").lower()
        # 来源不再是「必须是音视频目录」，按类型分别校验
        if kind == "media":
            media = (payload.get("media") or src.get("media") or "").strip()
            if not media:
                raise HTTPException(400, "请先选择音视频目录")
            if not Path(media).exists():
                raise HTTPException(400, f"目录不存在：{media}")
        elif kind == "text":
            if not (src.get("text") or "").strip():
                raise HTTPException(400, "请先输入文案内容")
        elif kind == "image":
            if not src.get("images"):
                raise HTTPException(400, "请先选择图片文件或目录")
        elif kind == "table":
            if not (src.get("table") or "").strip():
                raise HTTPException(400, "请先选择表格文件")
            if not (src.get("table_column") or "").strip():
                raise HTTPException(400, "请选择文案所在的列")
            if not Path(str(src["table"])).is_file():
                raise HTTPException(400, f"表格不存在：{src['table']}")
        else:
            raise HTTPException(400, f"未知的文案来源：{kind}")
        if manager.is_busy():
            raise HTTPException(409, "已有任务在运行，请先等待或取消")
        job = manager.create(payload)
        manager.start(job, make_runner(payload))
        return {"job_id": job.id, "status": job.status}

    @app.get("/api/job/{job_id}")
    def job_status(job_id: str, since: int = 0) -> dict:
        job = manager.get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        return job.as_dict(since=since)

    @app.get("/api/job")
    def current_job(since: int = 0) -> dict:
        job = manager.latest()
        if not job:
            return {"id": "", "status": "idle", "logs": [], "total_logs": 0}
        return job.as_dict(since=since)

    @app.post("/api/job/{job_id}/cancel")
    def cancel(job_id: str) -> dict:
        return {"ok": manager.cancel(job_id)}

    # ---------- 产物 ----------
    @app.get("/api/download")
    def download(path: str) -> FileResponse:
        p = Path(path)
        if not p.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(p, filename=p.name)

    @app.post("/api/open")
    def open_path(payload: dict) -> dict:
        """在文件管理器里打开目录/文件。"""
        p = Path(payload.get("path", ""))
        if not p.exists():
            raise HTTPException(404, "路径不存在")
        try:
            if os.name == "nt":
                os.startfile(str(p))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.run(["open", str(p)], check=False)
            else:
                subprocess.run(["xdg-open", str(p)], check=False)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:200]}

    return app


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True, log_level: str = "INFO") -> None:
    setup_logging(log_level)
    import uvicorn

    url = f"http://{host}:{port}"
    log.info("wxcopy 控制台启动：%s", url)
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host=host, port=port, log_level=log_level.lower())


__all__ = ["create_app", "serve", "manager"]
