"""生成一段可自由使用的演示 BGM（纯合成，无版权问题）。

仓库里带一段 BGM 是为了让 `wxcopy video --bgm` 开箱可试；
与其塞一个来源不明的音频文件，不如给个能复现的生成脚本。

用法：
    python scripts/make_demo_bgm.py            # 写 assets/bgm_demo.mp3
    python scripts/make_demo_bgm.py out.mp3 32 # 自定义输出与时长
"""

from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wxcopy.core.video import get_ffmpeg  # noqa: E402

SR = 44100
# Am - F - C - G，写成半音偏移，够温和不抢人声
PROGRESSION = [
    (220.00, 261.63, 329.63),  # Am
    (174.61, 220.00, 261.63),  # F
    (196.00, 261.63, 329.63),  # C
    (196.00, 246.94, 293.66),  # G
]


def build(total: float = 24.0, channels: int = 2) -> np.ndarray:
    """逐段生成和弦再首尾相接，最后做立体声轻微错开。"""
    per = total / len(PROGRESSION)
    n = int(per * SR)
    t = np.arange(n) / SR
    env = np.clip(np.minimum(t / (per * 0.35), (per - t) / (per * 0.45)), 0.0, 1.0) ** 1.5

    parts: list[np.ndarray] = []
    for chord in PROGRESSION:
        seg = np.zeros(n, np.float32)
        for f in chord:
            seg += (0.33 * np.sin(2 * np.pi * f * t)).astype(np.float32)
            seg += (0.10 * np.sin(2 * np.pi * f * 2 * t)).astype(np.float32)
            seg += (0.05 * np.sin(2 * np.pi * f * 3 * t)).astype(np.float32)
        parts.append(seg * env)

    mono = np.concatenate(parts)
    mono /= max(1e-9, float(np.max(np.abs(mono))))  # 归一化，防止削顶
    mono *= 0.9
    if channels == 1:
        return mono
    # 轻微左右错开，做出一点立体声宽度
    left = np.concatenate([mono, np.zeros(int(0.012 * SR), np.float32)])[: len(mono)]
    right = np.concatenate([np.zeros(int(0.012 * SR), np.float32), mono])[: len(mono)]
    return np.stack([left, right], axis=1)


def write_wav(path: Path, sig: np.ndarray) -> None:
    pcm = np.clip(sig * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1 if sig.ndim == 1 else sig.shape[1])
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def main() -> int:
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "assets/bgm_demo.mp3")
    total = float(sys.argv[2]) if len(sys.argv) > 2 else 24.0

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".wav")
    write_wav(tmp, build(total))
    subprocess.run(
        [get_ffmpeg(), "-y", "-loglevel", "error", "-i", str(tmp), "-c:a", "libmp3lame", "-b:a", "160k", str(dest)],
        check=True,
    )
    tmp.unlink(missing_ok=True)
    print(f"已生成 {dest}（{total:.0f}s，{dest.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
