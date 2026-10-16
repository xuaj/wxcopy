"""验证 BGM 混音是否真的生效（会调用 ffmpeg，属于手动验证脚本，不进单测）。

单测只能证明「滤镜串拼对了」；这个脚本证明「拼出来的滤镜串真的干活了」——
用可控的合成素材 + 频谱分析，把三个容易做错的点量出来：

1. BGM 比视频短时能不能循环铺满
2. 人声出现时 BGM 有没有被压低（闪避），以及压低了多少 dB
3. 结尾有没有淡出

用法：
    python scripts/verify_bgm.py            # 产物落在 _bgm_probe/
"""

from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wxcopy.core.video import VideoCfg, get_ffmpeg, make_video, probe_duration  # noqa: E402

OUT = Path("_bgm_probe")
SR = 24000
F_BGM = 440.0
# 人声用 300/320Hz、BGM 用 440Hz，频谱上互不干扰，才能单独量出 BGM 的电平
F_VOICE = (300.0, 320.0)
SPEECH = ((0.3, 1.8), (5.3, 6.0))  # 假人声的「说话」窗口
SILENT = (3.0, 4.8)  # 假人声的「静音」窗口


def _tone(freq: float, dur: float, amp: float) -> np.ndarray:
    t = np.arange(int(dur * SR)) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _write_wav(path: Path, sig: np.ndarray) -> None:
    pcm = np.clip(sig * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def make_assets() -> tuple[Path, Path]:
    """造素材：假人声（说话-静音-说话）+ 只有 3 秒的 BGM（必须靠循环铺满）。"""
    OUT.mkdir(exist_ok=True)
    voice = OUT / "voice.wav"
    _write_wav(
        voice,
        np.concatenate(
            [_tone(F_VOICE[0], 2.0, 0.70), np.zeros(int(3.0 * SR), np.float32), _tone(F_VOICE[1], 2.0, 0.70)]
        ),
    )

    bgm_wav = OUT / "bgm.wav"
    _write_wav(bgm_wav, _tone(F_BGM, 3.0, 0.35))
    bgm = OUT / "bgm.mp3"
    subprocess.run(
        [get_ffmpeg(), "-y", "-loglevel", "error", "-i", str(bgm_wav), "-c:a", "libmp3lame", "-b:a", "128k", str(bgm)],
        check=True,
    )
    return voice, bgm


def _pcm(path: Path) -> np.ndarray:
    r = subprocess.run(
        [
            get_ffmpeg(),
            "-v",
            "error",
            "-i",
            str(path),
            "-vn",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "44100",
            "-ac",
            "1",
            "-",
        ],
        capture_output=True,
    )
    return np.frombuffer(r.stdout, "<i2").astype(np.float64) / 32768.0


def _tone_db(a: np.ndarray, t0: float, t1: float, freq: float = F_BGM, bw: float = 6.0) -> float:
    """窗口内 freq±bw 频率槽的能量（dB），相当于对准这个音的带通电平。"""
    sr = 44100
    s = a[int(t0 * sr) : int(t1 * sr)]
    # 窗长按实际切片长度取，别用 (t1-t0)*sr 重算——取整方向不同会差一个样点
    s = s * np.hanning(len(s))
    sp = np.abs(np.fft.rfft(s)) ** 2
    freqs = np.fft.rfftfreq(len(s), 1 / sr)
    return 10 * np.log10(max(float(sp[(freqs > freq - bw) & (freqs < freq + bw)].sum()), 1e-20))


def main() -> int:
    voice, bgm = make_assets()
    base = dict(width=360, height=640, fps=24, bg_color="#101828", subtitle=False, preset="ultrafast")
    # BGM 音量刻意调到 3.0（>1）：让 BGM 在人声段也盖得住人声，
    # 这样「总量」口径也能看出差别；不然人声一大就把 BGM 淹了，量不出来。
    loud = dict(base, bgm=str(bgm), bgm_volume=3.0)

    make_video(voice, OUT / "duck_on.mp4", "", VideoCfg(**loud, bgm_duck=True))
    make_video(voice, OUT / "duck_off.mp4", "", VideoCfg(**loud, bgm_duck=False))

    on, off = _pcm(OUT / "duck_on.mp4"), _pcm(OUT / "duck_off.mp4")
    dur = probe_duration(OUT / "duck_on.mp4")

    print(f"\n视频时长 {dur:.2f}s（假人声 7.00s + 尾部留白 0.60s ≈ 7.60s）")
    print(f"{'窗口':<16}{'闪避开':>12}{'闪避关':>12}{'压低':>10}")

    ok = True
    for name, t0, t1 in (("说话 0.3-1.8s", *SPEECH[0]), ("静音 3.0-4.8s", *SILENT), ("说话 5.3-6.0s", *SPEECH[1])):
        a, b = _tone_db(on, t0, t1), _tone_db(off, t0, t1)
        print(f"{name:<16}{a:>10.1f}dB{b:>10.1f}dB{b - a:>8.1f}dB")

    # 静音段还能听到 BGM，说明 3 秒的 BGM 确实被循环铺到了 7.6 秒
    looped = _tone_db(on, *SILENT) > -60 and _tone_db(on, *SPEECH[1]) > -60
    print(f"\n[{'OK' if looped else 'FAIL'}] BGM 循环铺满整段")

    duck = min(_tone_db(off, *w) - _tone_db(on, *w) for w in SPEECH)
    print(f"[{'OK' if duck >= 6 else 'FAIL'}] 人声段 BGM 被压低 {duck:.1f}dB（期望 >= 6dB）")
    ok &= duck >= 6 and looped

    quiet = _tone_db(off, *SILENT) - _tone_db(on, *SILENT)
    print(f"[{'OK' if abs(quiet) < 1 else 'FAIL'}] 静音段不受闪避影响（{quiet:.1f}dB，期望 ≈0）")
    ok &= abs(quiet) < 1

    # 淡出：最后 0.5s 的 BGM 应明显低于中段
    tail, mid = _tone_db(on, 7.0, 7.5), _tone_db(on, *SILENT)
    print(f"[{'OK' if tail < mid - 3 else 'FAIL'}] 结尾淡出（末段 {tail:.1f}dB vs 中段 {mid:.1f}dB）")
    ok &= tail < mid - 3

    print("\n产物：" + ", ".join(sorted(p.name for p in OUT.glob("*.mp4"))))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
