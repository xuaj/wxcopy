"""后台任务管理：跑流水线 + 捕获日志 + 解析进度。

流水线函数本身不回调进度，这里通过拦截 wxcopy 的日志来还原阶段与进度，
好处是不用改动 pipeline 的既有签名。
"""

from __future__ import annotations

import logging
import re
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..logging_setup import get_logger
from ..settings import Settings

log = get_logger()

MAX_LOGS = 3000

# 进度识别规则：(正则, 阶段名)
PROGRESS_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\[(\d+)/(\d+)\]\s*(?:转写|跳过)"), "转写"),
    (re.compile(r"仿写进度\s*(\d+)/(\d+)"), "仿写"),
    (re.compile(r"配音进度\s*(\d+)/(\d+)"), "配音"),
    (re.compile(r"合成进度\s*(\d+)/(\d+)"), "合成视频"),
]

# 阶段识别：命中关键字即切阶段
STAGE_RULES: list[tuple[str, str]] = [
    ("共识别到", "扫描视频"),
    ("加载 Whisper 模型", "加载模型"),
    ("转写结果已写入", "转写完成"),
    ("使用模型：", "仿写"),
    ("仿写完成", "仿写完成"),
    ("使用配音引擎", "配音"),
    ("配音完成", "配音完成"),
    ("视频参数：", "合成视频"),
    ("视频完成", "视频完成"),
]


@dataclass
class Job:
    id: str
    params: dict[str, Any]
    status: str = "pending"  # pending | running | done | error | cancelled
    stage: str = ""
    current: int = 0
    total: int = 0
    logs: list[dict] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    cancel_flag: bool = False

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 0
        return min(100, int(self.current * 100 / self.total))

    def add_log(self, level: str, msg: str) -> None:
        self.logs.append({"t": time.strftime("%H:%M:%S"), "level": level, "msg": msg})
        if len(self.logs) > MAX_LOGS:
            del self.logs[: len(self.logs) - MAX_LOGS]
        for pattern, stage in PROGRESS_RULES:
            m = pattern.search(msg)
            if m:
                self.stage = stage
                self.current = int(m.group(1))
                self.total = int(m.group(2))
                return
        for kw, stage in STAGE_RULES:
            if kw in msg:
                self.stage = stage
                return

    def as_dict(self, since: int = 0) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "current": self.current,
            "total": self.total,
            "percent": self.percent,
            "error": self.error,
            "outputs": self.outputs,
            "params": self.params,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1) if self.started_at else 0,
            "logs": self.logs[since:],
            "total_logs": len(self.logs),
        }


class _JobLogHandler(logging.Handler):
    """把 wxcopy 日志转发到指定 Job。"""

    def __init__(self, job: Job):
        super().__init__(level=logging.INFO)
        self.job = job

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.job.add_log(record.levelname.lower(), record.getMessage())
        except Exception:  # noqa: BLE001
            pass


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

    def create(self, params: dict) -> Job:
        job = Job(id=uuid.uuid4().hex[:8], params=params)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def start(self, job: Job, runner: Callable[[Job], dict]) -> Job:
        job.status = "running"
        job.started_at = time.time()

        def _wrap():
            handler = _JobLogHandler(job)
            logger = get_logger()
            logger.addHandler(handler)
            try:
                out = runner(job) or {}
                job.outputs = out
                if job.cancel_flag:
                    job.status = "cancelled"
                else:
                    job.status = "done"
            except Exception as e:  # noqa: BLE001
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"
                job.add_log("error", traceback.format_exc()[-1500:])
            finally:
                logger.removeHandler(handler)
                job.finished_at = time.time()
                job.stage = {"done": "全部完成", "error": "失败", "cancelled": "已取消"}.get(job.status, job.stage)

        t = threading.Thread(target=_wrap, daemon=True)
        self._threads[job.id] = t
        t.start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def latest(self) -> Job | None:
        with self._lock:
            return list(self._jobs.values())[-1] if self._jobs else None

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status != "running":
            return False
        job.cancel_flag = True
        return True

    def is_busy(self) -> bool:
        return any(j.status == "running" for j in self._jobs.values())


def make_runner(params: dict) -> Callable[[Job], dict]:
    """构造真正执行流水线的函数。"""
    from ..core.tts import AUDIO_EXTS
    from ..pipeline import run_pipeline  # 延迟导入，避免循环
    from ..settings import load_settings

    def _runner(job: Job) -> dict:
        st: Settings = load_settings(params.get("config") or None)
        # 统一转成绝对路径：相对路径会以服务进程的工作目录为基准，
        # 用户按自己在页面里填的路径去找就会找不到产物。
        workdir = str(Path(params.get("workdir") or st.paths.workdir).expanduser().resolve())
        st.paths.workdir = workdir
        if params.get("asr_model"):
            st.asr.model = params["asr_model"]
        if params.get("language"):
            st.asr.language = params["language"]
        if params.get("provider"):
            st.llm.provider = params["provider"]
        if params.get("model"):
            st.llm.model = params["model"]
        if params.get("temperature") is not None:
            st.llm.temperature = float(params["temperature"])
        if params.get("prompt"):
            st.rewrite.prompt = params["prompt"]
        if params.get("concurrency"):
            st.rewrite.concurrency = int(params["concurrency"])
        if params.get("tts_provider"):
            st.tts.provider = params["tts_provider"]
        if params.get("voice"):
            st.tts.voice = params["voice"]
        # ---- 原始文案来源（音视频 / 手输 / 图片OCR / 表格）----
        src = params.get("source") or {}
        if src.get("ocr_provider"):
            st.ocr.provider = src["ocr_provider"]
        if src.get("ocr_model") is not None:
            st.ocr.model = src["ocr_model"]
        # ---- 背景音乐：和视频分开处理 ----
        # BGM 只能挂在视频上，但用户可能只勾了 BGM 忘了勾视频，
        # 所以这几个设置无条件写进 st，最后由 run_pipeline 判断要不要出片。
        vp = params.get("video") or {}
        bgm_path = (vp.get("bgm") or "").strip()
        if bgm_path:
            st.video.bgm = bgm_path
        if vp.get("bgm_volume") is not None:
            st.video.bgm_volume = float(vp["bgm_volume"])
        if vp.get("bgm_duck") is not None:
            st.video.bgm_duck = bool(vp["bgm_duck"])
        if vp.get("bgm_duck_ratio") is not None:
            st.video.bgm_duck_ratio = float(vp["bgm_duck_ratio"])
        if vp.get("bgm_fade_out") is not None:
            st.video.bgm_fade_out = float(vp["bgm_fade_out"])

        # ---- 封面 ----
        cp = params.get("cover") or {}
        gen_cover = bool(cp.get("enabled"))
        if gen_cover:
            if cp.get("style"):
                st.cover.style = cp["style"]
            if cp.get("bg") is not None:
                st.cover.bg = cp["bg"]
            if cp.get("bg_color") is not None:
                st.cover.bg_color = cp["bg_color"]
            if cp.get("accent"):
                st.cover.accent = cp["accent"]
            if cp.get("footer") is not None:
                st.cover.footer = cp["footer"]
            if cp.get("font") is not None:
                st.cover.font = cp["font"]
            if cp.get("title_max"):
                st.cover.title_max = int(cp["title_max"])
            if cp.get("subtitle_max"):
                st.cover.subtitle_max = int(cp["subtitle_max"])
            if cp.get("jpeg") is not None:
                st.cover.jpeg = bool(cp["jpeg"])

        # ---- 发布素材包 ----
        pb = params.get("publish") or {}
        gen_publish = bool(pb.get("enabled"))
        if gen_publish:
            if pb.get("titles"):
                st.publish.titles = int(pb["titles"])
            if pb.get("desc_max"):
                st.publish.desc_max = int(pb["desc_max"])
            if pb.get("topics"):
                st.publish.topics = int(pb["topics"])
            if pb.get("platforms"):
                st.publish.platforms = pb["platforms"]
            if pb.get("concurrency"):
                st.publish.concurrency = int(pb["concurrency"])
            if pb.get("prompt") is not None:
                st.publish.prompt = pb["prompt"]
            if pb.get("auto_cover") is not None:
                st.publish.auto_cover = bool(pb["auto_cover"])

        # ---- 视频合成 ----
        gen_video = bool(vp.get("enabled"))
        if gen_video:
            st.video.bg_path = (vp.get("bg_path") or "").strip()
            if vp.get("bg_color"):
                st.video.bg_color = vp["bg_color"]
            if vp.get("fit"):
                st.video.fit = vp["fit"]
            if vp.get("size"):
                st.video.size = vp["size"]
            if vp.get("style"):
                st.video.style = vp["style"]
            if vp.get("subtitle") is not None:
                st.video.subtitle = bool(vp["subtitle"])
            if vp.get("font"):
                st.video.font = vp["font"]
            if vp.get("bold") is not None:
                st.video.bold = bool(vp["bold"])
            if vp.get("big_font_size") is not None:
                st.video.big_font_size = int(vp["big_font_size"])
            if vp.get("highlight_mode"):
                st.video.highlight_mode = vp["highlight_mode"]
            if vp.get("highlight_color"):
                st.video.highlight_color = vp["highlight_color"]
            if vp.get("keywords") is not None:
                kws = vp["keywords"]
                if isinstance(kws, str):
                    kws = [k.strip() for k in kws.replace("，", ",").split(",") if k.strip()]
                st.video.keywords = list(kws)
            if vp.get("big_chars_per_line"):
                st.video.big_chars_per_line = int(vp["big_chars_per_line"])
            if vp.get("big_lines_per_screen"):
                st.video.big_lines_per_screen = int(vp["big_lines_per_screen"])
            if vp.get("typewriter") is not None:
                st.video.typewriter = bool(vp["typewriter"])
            if vp.get("type_speed") is not None:
                st.video.type_speed = float(vp["type_speed"])
            if vp.get("type_hold") is not None:
                st.video.type_hold = float(vp["type_hold"])
            if vp.get("type_min") is not None:
                st.video.type_min = float(vp["type_min"])
            if vp.get("font_size"):
                st.video.font_size = int(vp["font_size"])
            if vp.get("margin_v") is not None:
                st.video.margin_v = int(vp["margin_v"])
            if vp.get("chars_per_line"):
                st.video.chars_per_line = int(vp["chars_per_line"])
            if vp.get("crf"):
                st.video.crf = int(vp["crf"])
            if vp.get("tail_pad") is not None:
                st.video.tail_pad = float(vp["tail_pad"])
            if vp.get("fade_in") is not None:
                st.video.fade_in = float(vp["fade_in"])
            if vp.get("concurrency"):
                st.video.concurrency = int(vp["concurrency"])

        res = run_pipeline(
            st,
            media=Path(params.get("media") or ""),
            workdir=workdir,
            provider=params.get("provider"),
            model=params.get("model"),
            prompt=params.get("prompt"),
            voice=params.get("voice"),
            limit=params.get("limit"),
            recursive=bool(params.get("recursive")),
            asr_language=params.get("language"),
            bg_path=(vp.get("bg_path") or None) or None,
            bgm=(bgm_path or None),
            size=vp.get("size"),
            subtitle=(bool(vp["subtitle"]) if vp.get("subtitle") is not None else None),
            make_video=gen_video,
            make_cover=gen_cover,
            make_publish=gen_publish,
            cover=(cp or None),
            publish=(pb or None),
            cover_title_column=(cp.get("title_column") or None),
            cover_subtitle_column=(cp.get("subtitle_column") or None),
            source=(src or None),  # dict -> pipeline 里转成 SourceSpec
        )

        out: dict[str, Any] = {k: str(v) for k, v in res.items()}
        # 把音频文件直接列出来，省得用户还要自己在 voice 子目录里翻
        vd = Path(out.get("voice_dir") or "")
        # 必须判空：Path("") 等于当前目录，不判会把仓库根目录下的文件当成配音产物
        if out.get("voice_dir") and vd.is_dir():
            # 只认音频：配音旁边挂着 <name>.cues.json（逐字时间戳），
            # 不过滤的话页面会把 json 也当成一条配音，数量直接翻倍。
            # 用 pipeline 里同一份常量，避免两处定义漂移。
            files = sorted(p for p in vd.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
            out["voice_count"] = len(files)
            out["voice_files"] = [str(p) for p in files[:50]]
            get_logger().info("音频产物：%d 个文件，位于 %s", len(files), vd)
        hd = Path(out.get("video_dir") or "")
        if out.get("video_dir") and hd.is_dir():
            vids = sorted(p for p in hd.iterdir() if p.suffix.lower() == ".mp4")
            out["video_count"] = len(vids)
            out["video_files"] = [str(p) for p in vids[:50]]
            get_logger().info("视频产物：%d 个文件，位于 %s", len(vids), hd)
        # 封面是图片，页面要直接显示缩略图，所以这里也列出来
        cv = Path(out.get("cover_dir") or "")
        if out.get("cover_dir") and cv.is_dir():
            imgs = sorted(p for p in cv.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})
            out["cover_count"] = len(imgs)
            out["cover_files"] = [str(p) for p in imgs[:50]]
            get_logger().info("封面产物：%d 张，位于 %s", len(imgs), cv)
        pp = Path(out.get("publish_plan") or "")
        if out.get("publish_plan"):
            out["publish_plan_exists"] = pp.is_file()
            out["publish_plan_size"] = pp.stat().st_size if pp.is_file() else 0
            get_logger().info("发布清单：%s", pp)
        tbl = Path(out.get("table", ""))
        out["table_exists"] = tbl.is_file()
        out["table_size"] = tbl.stat().st_size if tbl.is_file() else 0
        return out

    return _runner
