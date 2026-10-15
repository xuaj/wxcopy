"""文案转配音（TTS）。

- edge:    微软 Edge 在线神经音色，免费、中文自然度最好，默认
- openai:  走 /v1/audio/speech 兼容端点（硅基流动 CosyVoice、OpenAI、豆包等）
- pyttsx3: 完全离线兜底，中文效果一般，仅应急
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..logging_setup import get_logger
from ..settings import Settings, TTSCfg, api_key_of
from .media import probe_duration

log = get_logger()

MAX_EDGE_CHARS = 3000  # 单次合成的安全长度，超长会分片
CUES_SUFFIX = ".cues.json"  # 配音旁挂的逐字时间戳
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac"}


class TTSError(RuntimeError):
    pass


# ---------------------------------------------------------------- 逐字时间戳
# edge-tts 在合成时会回吐 WordBoundary 事件（每个字的 offset/duration），
# 存成旁挂 json 后，视频阶段就能按「真实语音时间轴」排字幕，而不是按字数猜。
# 这是字幕不跑偏的关键：TTS 在标点处有停顿，字符速率并不均匀。


def cues_path_of(audio: Path | str) -> Path:
    """配音音频对应的逐字时间戳文件路径（``001_x.mp3`` -> ``001_x.cues.json``）。"""
    return Path(audio).with_suffix(CUES_SUFFIX)


def read_cues_raw(audio: Path | str) -> list[dict]:
    """读原始 cues 记录 ``[{w,t,d}, ...]``；缺失或损坏一律返回空列表。"""
    p = cues_path_of(audio)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:  # noqa: BLE001
        log.warning("逐字时间戳不可用 %s：%s", p.name, str(e)[:80])
        return []
    return [it for it in data if isinstance(it, dict) and str(it.get("w", "")).strip()]


def write_cues(audio: Path | str, cues: list[dict]) -> Path | None:
    """把逐字时间戳写到音频旁挂文件；空列表则不落盘。"""
    if not cues:
        return None
    p = cues_path_of(audio)
    try:
        p.write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        log.warning("逐字时间戳写入失败 %s：%s", p.name, str(e)[:80])
        return None
    return p


def read_cues(audio: Path | str) -> list[tuple[str, float]]:
    """读逐字时间戳 -> ``[(字, 起始秒)]``，供视频阶段对齐字幕。"""
    return [(str(it["w"]), float(it.get("t", 0.0))) for it in read_cues_raw(audio)]


def has_cues(audio: Path | str) -> bool:
    return cues_path_of(audio).is_file()


class BaseTTS(ABC):
    kind: str = "base"
    name: str = ""

    @abstractmethod
    def synth(self, text: str, out_path: Path, **kw) -> Path: ...

    @staticmethod
    def split_text(text: str, limit: int = MAX_EDGE_CHARS) -> list[str]:
        """按句号/换行做安全切分，避免单次请求过长。"""
        text = (text or "").strip()
        if len(text) <= limit:
            return [text] if text else []
        parts, buf = [], ""
        for ch in text:
            buf += ch
            if len(buf) >= limit and ch in "。！？!?；;\n":
                parts.append(buf.strip())
                buf = ""
        if buf.strip():
            parts.append(buf.strip())
        return parts


class EdgeTTS(BaseTTS):
    kind = "edge"

    def __init__(self, cfg: TTSCfg, voice: str | None = None):
        self.cfg = cfg
        self.voice = voice or cfg.voice
        self.name = f"edge:{self.voice}"

    def synth(self, text: str, out_path: Path, **kw) -> Path:
        import edge_tts

        voice = kw.get("voice") or self.voice
        rate = kw.get("rate") or self.cfg.rate
        volume = kw.get("volume") or self.cfg.volume
        pitch = kw.get("pitch") or self.cfg.pitch
        # 默认开：逐字时间戳是「字幕和语音对得上」的唯一依据
        want_cues = bool(getattr(self.cfg, "cues", True))
        out_path.parent.mkdir(parents=True, exist_ok=True)

        async def _run() -> list[dict]:
            comm = edge_tts.Communicate(
                text,
                voice,
                rate=rate,
                volume=volume,
                pitch=pitch,
                boundary="WordBoundary" if want_cues else "SentenceBoundary",
            )
            if not want_cues:
                await comm.save(str(out_path))
                return []
            cues: list[dict] = []
            with open(out_path, "wb") as f:
                async for chunk in comm.stream():
                    kind = chunk.get("type")
                    if kind == "audio":
                        f.write(chunk["data"])
                    elif kind == "WordBoundary":
                        cues.append(
                            {
                                "w": str(chunk.get("text", "")),
                                "t": round(float(chunk.get("offset", 0)) / 1e7, 4),
                                "d": round(float(chunk.get("duration", 0)) / 1e7, 4),
                            }
                        )
            return cues

        cues = asyncio.run(_run())
        write_cues(out_path, cues)
        return out_path

    def synth_long(self, text: str, out_path: Path, **kw) -> Path:
        """超长文本分成多段分别合成，再按顺序拼接（时间戳按累计时长顺移）。"""
        parts = self.split_text(text)
        if len(parts) <= 1:
            return self.synth(text, out_path, **kw)
        tmp_files = []
        merged: list[dict] = []
        offset = 0.0
        for i, part in enumerate(parts):
            tmp = self.synth(part, out_path.with_name(f"{out_path.stem}_p{i}{out_path.suffix}"), **kw)
            for it in read_cues_raw(tmp):
                merged.append({"w": it["w"], "t": round(float(it.get("t", 0.0)) + offset, 4), "d": it.get("d", 0.0)})
            offset += probe_duration(tmp) or 0.0
            cues_path_of(tmp).unlink(missing_ok=True)
            tmp_files.append(tmp)
        _concat_audio(tmp_files, out_path)
        for f in tmp_files:
            f.unlink(missing_ok=True)
        write_cues(out_path, merged)
        return out_path


class OpenAICompatTTS(BaseTTS):
    kind = "openai"

    def __init__(self, cfg: TTSCfg, voice: str | None = None, model: str | None = None):
        from openai import OpenAI  # 延迟导入

        self.cfg = cfg
        self.ocfg = cfg.openai
        self.voice = voice or self.ocfg.voice
        self.model = model or self.ocfg.model
        self.name = f"openai-tts:{self.model}"
        api_key = api_key_of(self.ocfg.api_key_env)
        if not api_key:
            raise TTSError(f"未提供 API Key，请设置环境变量 {self.ocfg.api_key_env}")
        self.client = OpenAI(base_url=self.ocfg.base_url or None, api_key=api_key, timeout=600)

    def synth(self, text: str, out_path: Path, **kw) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        resp = self.client.audio.speech.create(
            model=kw.get("model") or self.model,
            voice=kw.get("voice") or self.voice,
            input=text,
            response_format=kw.get("format") or self.cfg.format or "mp3",
        )
        resp.write_to_file(str(out_path))
        return out_path


class Pyttsx3TTS(BaseTTS):
    kind = "pyttsx3"

    def __init__(self, cfg: TTSCfg, voice: str | None = None):
        self.cfg = cfg
        self.voice = voice or cfg.voice
        self.name = "pyttsx3:offline"

    def synth(self, text: str, out_path: Path, **kw) -> Path:
        import pyttsx3

        out_path.parent.mkdir(parents=True, exist_ok=True)
        engine = pyttsx3.init()
        if self.voice:
            for v in engine.getProperty("voices"):
                if self.voice.lower() in (v.id or "").lower() or self.voice in (v.name or ""):
                    engine.setProperty("voice", v.id)
                    break
        engine.save_to_file(text, str(out_path))
        engine.runAndWait()
        return out_path


def _concat_audio(files: list[Path], out_path: Path) -> Path:
    """用 ffmpeg concat 拼接音频分片。"""
    from .media import get_ffmpeg

    lst = out_path.with_suffix(".concat.txt")
    lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in files), encoding="utf-8")
    cmd = [
        get_ffmpeg(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(lst),
        "-c",
        "copy",
        str(out_path),
    ]
    import subprocess

    subprocess.run(cmd, check=True)
    lst.unlink(missing_ok=True)
    return out_path


def supports_cues(tts: BaseTTS) -> bool:
    """该引擎这次合成会不会产出逐字时间戳（目前只有 edge 有）。"""
    return isinstance(tts, EdgeTTS) and bool(getattr(tts.cfg, "cues", True))


def build_tts(settings: Settings, voice: str | None = None) -> BaseTTS:
    cfg = settings.tts
    if cfg.provider == "edge":
        return EdgeTTS(cfg, voice)
    if cfg.provider == "openai":
        return OpenAICompatTTS(cfg, voice)
    if cfg.provider == "pyttsx3":
        return Pyttsx3TTS(cfg, voice)
    raise TTSError(f"未知的 tts.provider: {cfg.provider}")


def list_edge_voices(locale: str = "zh-") -> list[dict]:
    """列出 Edge 可用音色（需联网）。"""
    import edge_tts

    async def _run():
        return await edge_tts.list_voices()

    voices = asyncio.run(_run())
    return [v for v in voices if v["ShortName"].lower().startswith(locale.lower())]


def synth_many(
    items: list[tuple[str, str]], out_dir: Path, tts: BaseTTS, concurrency: int = 2, ext: str | None = None
) -> dict[str, Path]:
    """items: [(name, text)] -> {name: 音频路径}。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = ext or getattr(tts.cfg, "format", "mp3") or "mp3"
    results: dict[str, Path] = {}

    def _one(kv):
        name, text = kv
        if not (text or "").strip():
            return name, None
        target = out_dir / f"{name}.{ext}"
        try:
            if isinstance(tts, EdgeTTS):
                tts.synth_long(text, target)
            else:
                tts.synth(text, target)
            return name, target
        except Exception as e:  # noqa: BLE001
            log.error("合成失败 %s：%s", name, str(e)[:140])
            return name, None

    total = len(items)
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for name, path in pool.map(_one, items):
            done += 1
            if path:
                results[name] = path
            if done % 5 == 0 or done == total:
                log.info("配音进度 %d/%d（成功 %d）", done, total, len(results))
    return results
