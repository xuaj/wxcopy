"""语音转文字（ASR）。

- faster-whisper：本地离线，CTranslate2 推理，中文效果与速度平衡最好，默认走它
- openai：走 OpenAI 兼容的 /v1/audio/transcriptions，适合没显卡或想省事的场景
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..settings import Settings, api_key_of
from .media import load_audio_mono16k

log = get_logger()


def apply_hf_env(cfg) -> None:
    """设置 HuggingFace 下载环境：走镜像 + 关掉 Xet 通道。

    不关 Xet 时国内常见 `CAS Client Error: 401 Unauthorized`，导致模型下不下来。
    """
    if getattr(cfg, "disable_xet", True):
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    # Windows 默认没有符号链接权限，hub 会建成 0 字节的空链接，
    # 表现为 "model.bin is incomplete: failed to read a value of size 4"。
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    endpoint = getattr(cfg, "hf_endpoint", "") or ""
    if endpoint:
        os.environ.setdefault("HF_ENDPOINT", endpoint)


MEDIA_EXTS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".flv",
    ".wmv",
    ".webm",
    ".m4v",
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
}


class ASRError(RuntimeError):
    pass


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    source: str
    text: str = ""
    language: str = ""
    duration: float = 0.0
    segments: list[Segment] = field(default_factory=list)

    def to_txt(self) -> str:
        return self.text

    def to_srt(self) -> str:
        def ts(t: float) -> str:
            h = int(t // 3600)
            m = int(t % 3600 // 60)
            s = int(t % 60)
            ms = int(round((t - int(t)) * 1000))
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        lines = []
        for i, seg in enumerate(self.segments, 1):
            lines.append(f"{i}\n{ts(seg.start)} --> {ts(seg.end)}\n{seg.text.strip()}\n")
        return "\n".join(lines)

    def save(self, path: Path, with_srt: bool = True) -> list[Path]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.text, encoding="utf-8")
        written = [path]
        if with_srt and self.segments:
            srt = path.with_suffix(".srt")
            srt.write_text(self.to_srt(), encoding="utf-8")
            written.append(srt)
        return written


class BaseASR(ABC):
    kind: str = "base"
    name: str = ""

    @abstractmethod
    def transcribe(self, media: Path, language: str | None = None) -> Transcript: ...


class FasterWhisperASR(BaseASR):
    kind = "faster-whisper"

    def __init__(self, cfg):
        from faster_whisper import WhisperModel  # 延迟导入

        self.cfg = cfg
        self.name = f"faster-whisper:{cfg.model}"
        self._model = None
        self._cls = WhisperModel

    @property
    def model(self):
        if self._model is None:
            log.info("加载 Whisper 模型 %s (device=%s)…首次会联网下载", self.cfg.model, self.cfg.device)
            self._model = self._cls(
                self.cfg.model,
                device=self.cfg.device,
                compute_type=self.cfg.compute_type,
            )
        return self._model

    def _run(self, audio, language: str | None):
        """audio 可以是文件路径，也可以是 float32 单声道 ndarray（回退路径用）。"""
        kwargs: dict[str, Any] = {
            "beam_size": self.cfg.beam_size,
            "vad_filter": self.cfg.vad_filter,
        }
        if language:
            kwargs["language"] = language
        if getattr(self.cfg, "initial_prompt", ""):
            kwargs["initial_prompt"] = self.cfg.initial_prompt
        # ndarray 必须原样传给 faster-whisper，不能 str() 化
        src = str(audio) if isinstance(audio, (str, Path)) else audio
        return self.model.transcribe(src, **kwargs)

    def transcribe(self, media: Path, language: str | None = None) -> Transcript:
        lang = language or (self.cfg.language or None)
        try:
            segments, info = self._run(media, lang)
            segs = [Segment(s.start, s.end, s.text) for s in segments]
            dur = getattr(info, "duration", 0.0) or 0.0
        except Exception as e:  # noqa: BLE001
            # 常见失败：PyAV 版本与 faster-whisper 接口不兼容（如 metadata_errors），
            # 或容器格式解不了。改用 ffmpeg 解码成 ndarray 再转，绕开 PyAV。
            log.warning("PyAV 解码失败（%s），改用 ffmpeg 解码后重试", str(e)[:100])
            audio = load_audio_mono16k(media)
            segments, info = self._run(audio, lang)
            segs = [Segment(s.start, s.end, s.text) for s in segments]
            dur = getattr(info, "duration", 0.0) or (len(audio) / 16000.0)

        text = "".join(s.text for s in segs).strip()
        return Transcript(
            source=str(media),
            text=text,
            language=getattr(info, "language", "") or "",
            duration=dur,
            segments=segs,
        )


class OpenAICompatASR(BaseASR):
    kind = "openai"

    def __init__(self, cfg):
        from openai import OpenAI  # 延迟导入

        self.cfg = cfg
        self.name = f"openai-asr:{cfg.model}"
        api_key = api_key_of(cfg.api_key_env)
        if not api_key:
            raise ASRError(f"未提供 API Key，请设置环境变量 {cfg.api_key_env}")
        self.client = OpenAI(base_url=cfg.base_url or None, api_key=api_key, timeout=600)

    def transcribe(self, media: Path, language: str | None = None) -> Transcript:
        with open(media, "rb") as fh:
            kwargs: dict[str, Any] = {"model": self.cfg.model, "file": fh}
            if language:
                kwargs["language"] = language
            resp = self.client.audio.transcriptions.create(**kwargs)
        text = (getattr(resp, "text", "") or "").strip()
        return Transcript(source=str(media), text=text, language=language or "")


def prefetch_model(cfg) -> Path:
    """预先下载 Whisper 模型，便于在正式跑批前把网络问题解决掉。"""
    apply_hf_env(cfg)
    from huggingface_hub import snapshot_download

    repo = f"Systran/faster-whisper-{cfg.model}" if "/" not in cfg.model else cfg.model
    log.info("下载模型 %s（来自 %s）", repo, os.environ.get("HF_ENDPOINT", "huggingface.co"))
    path = snapshot_download(repo)
    log.info("模型已就绪：%s", path)
    return Path(path)


def build_asr(settings: Settings) -> BaseASR:
    cfg = settings.asr
    if cfg.provider == "faster-whisper":
        apply_hf_env(cfg)
        return FasterWhisperASR(cfg)
    if cfg.provider == "openai":
        return OpenAICompatASR(cfg.openai)
    raise ASRError(f"未知的 asr.provider: {cfg.provider}")


def find_media(root: Path, recursive: bool = False) -> list[Path]:
    """列出目录内的音视频文件。"""
    if root.is_file():
        return [root]
    it = root.rglob("*") if recursive else root.glob("*")
    return sorted(p for p in it if p.is_file() and p.suffix.lower() in MEDIA_EXTS)


def ffmpeg_version() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return ""
