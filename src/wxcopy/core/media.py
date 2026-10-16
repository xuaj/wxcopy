"""音视频底层工具：抽音轨、探测时长、定位 ffmpeg。"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger()

# Windows 中文环境下 ffmpeg 输出是 GBK，text=True 不指定编码会直接 UnicodeDecodeError
_FF_ENC = dict(encoding="utf-8", errors="replace")

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", re.I)
_TIME_RE = re.compile(r"\btime=(\d+):(\d+):(\d+(?:\.\d+)?)", re.I)


def get_ffmpeg() -> str:
    """优先用系统 ffmpeg，其次用 imageio-ffmpeg 内置的静态二进制。"""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("未找到 ffmpeg。请安装系统 ffmpeg，或 pip install imageio-ffmpeg 使用内置二进制。") from e


def has_ffmpeg() -> bool:
    """是否拿得到 ffmpeg。

    「要转码」和「顺手探测一下」对 ffmpeg 的缺失容忍度完全不同：
    前者该直接报错，后者该安静地降级（比如 publish 只想给清单填个时长）。
    """
    try:
        get_ffmpeg()
        return True
    except RuntimeError:
        return False


def extract_audio(src: Path, dst: Path, sample_rate: int = 16000, channels: int = 1) -> Path:
    """把任意音视频转成 16kHz 单声道 wav，ASR 前统一格式用。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        get_ffmpeg(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vn",  # 丢掉视频流
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_s16le",
        str(dst),
    ]
    subprocess.run(cmd, check=True)
    return dst


def load_audio_mono16k(path: Path, sample_rate: int = 16000):
    """用 ffmpeg 解码成 float32 单声道 ndarray。

    直接把 ndarray 交给 faster-whisper 可以绕开 PyAV 解码，
    从而避免 av 版本升级导致的 `metadata_errors` 一类接口不兼容问题。
    """
    import numpy as np

    cmd = [
        get_ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-f",
        "s16le",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-",
    ]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    if not raw:
        raise RuntimeError(f"ffmpeg 未能解出音频流: {path}")
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _hms(g) -> float:
    return int(g[0]) * 3600 + int(g[1]) * 60 + float(g[2])


def _ffmpeg_duration(path: Path, exact: bool = False) -> float:
    """用 ffmpeg 自身探测时长（imageio-ffmpeg 只带 ffmpeg、不带 ffprobe，所以必须有这条路）。

    exact=False：只读头部的 Duration 行，几乎不耗时，对 CBR 文件准确。
    exact=True ：把整个文件解码一遍取最后的 time=，对 VBR / 头部信息缺失的文件最准，
                 代价是要真解码（几秒的配音完全可以接受）。
    """
    ff = get_ffmpeg()
    p = str(path)
    if exact:
        # -f null - 丢弃输出，只要 stderr 里的进度时间戳
        cmd = [ff, "-hide_banner", "-i", p, "-f", "null", "-"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, **_FF_ENC)
        except Exception:  # noqa: BLE001
            return 0.0
        last = None
        for m in _TIME_RE.finditer(r.stderr or ""):
            last = m
        return _hms(last.groups()) if last else 0.0

    cmd = [ff, "-hide_banner", "-i", p]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, **_FF_ENC)
    except Exception:  # noqa: BLE001
        return 0.0
    m = _DUR_RE.search(r.stderr or "")
    return _hms(m.groups()) if m else 0.0


def probe_duration(path: Path, exact: bool = True) -> float:
    """探测媒体时长（秒），失败返回 0。

    ffprobe 优先；本机没有 ffprobe 时（imageio-ffmpeg 只带 ffmpeg）退回用 ffmpeg 探测，
    默认走 exact=True 全解码，保证 VBR mp3 一类文件也拿得到真实时长。

    **没有 ffmpeg 时也必须返回 0 而不是抛异常**：这个函数在所有调用点都是
    「尽力探测」的语义（挑时长分配字幕、给发布清单填个时长）。整机没装 ffmpeg
    却因此把 publish 整条命令带崩，是踩过的坑。
    """
    path = Path(path)
    if not path.is_file():
        return 0.0

    exe = shutil.which("ffprobe")
    if exe:
        cmd = [
            exe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, **_FF_ENC, timeout=30)
            return float((r.stdout or "").strip())
        except Exception:  # noqa: BLE001
            pass

    if not has_ffmpeg():
        return 0.0

    d = _ffmpeg_duration(path, exact=False)
    if d <= 0 and exact:
        d = _ffmpeg_duration(path, exact=True)
    return d


def safe_stem(path: Path, limit: int = 60) -> str:
    """生成可安全落盘的文件名（去掉 Windows 非法字符、限制长度）。

    先把路径分隔符替换掉再取 stem：否则 Windows 下 Path("门店/引流").stem
    只会剩「引流」，表格「命名列」里带 / 或 \\ 时名字会被截掉一半。
    """
    src = str(path).replace("/", "_").replace("\\", "_")
    stem = Path(src).stem
    for ch in '<>:"/\\|?*':
        stem = stem.replace(ch, "_")
    stem = stem.strip().rstrip(".")
    return stem[:limit] or "untitled"
