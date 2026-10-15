"""逐字时间戳（cues）与字幕时间轴对齐的测试。

为什么值得测：字幕时间轴以前按字数均分，TTS 在标点处有停顿、字符速率并不均匀，
长文案到后半段能差出 6 秒以上——单靠肉眼看片子很难发现，必须用数据卡住。
"""

from __future__ import annotations

from pathlib import Path

from wxcopy.core.tts import (
    CUES_SUFFIX,
    cues_path_of,
    read_cues,
    read_cues_raw,
    supports_cues,
    write_cues,
)
from wxcopy.core.video import VideoCfg, align_timings, write_ass
from wxcopy.pipeline import _cues_digest, _find_voice, _video_sig

# ---------------- 时间戳文件的读写 ----------------


def test_cues_path_of():
    assert cues_path_of("voice/001_x.mp3").name == "001_x" + CUES_SUFFIX
    assert cues_path_of(Path("a/b/c.wav")).name == "c" + CUES_SUFFIX


def test_write_read_cues_roundtrip(tmp_path):
    audio = tmp_path / "001_x.mp3"
    audio.write_bytes(b"fake")
    assert write_cues(audio, [{"w": "你", "t": 0.0, "d": 0.2}, {"w": "好", "t": 0.2, "d": 0.2}])

    assert read_cues(audio) == [("你", 0.0), ("好", 0.2)]
    assert [it["w"] for it in read_cues_raw(audio)] == ["你", "好"]


def test_write_cues_empty_writes_nothing(tmp_path):
    """没有时间戳就别落盘，免得留一堆空 json。"""
    audio = tmp_path / "a.mp3"
    assert write_cues(audio, []) is None
    assert not cues_path_of(audio).exists()


def test_read_cues_missing_returns_empty(tmp_path):
    assert read_cues(tmp_path / "nope.mp3") == []
    assert read_cues_raw(tmp_path / "nope.mp3") == []


def test_read_cues_corrupt_returns_empty(tmp_path):
    audio = tmp_path / "a.mp3"
    cues_path_of(audio).write_text("{ not json", encoding="utf-8")
    assert read_cues(audio) == []


def test_read_cues_raw_skips_junk(tmp_path):
    audio = tmp_path / "a.mp3"
    cues_path_of(audio).write_text('[{"w":"你","t":0},{"w":"  ","t":1},"x",{"t":2}]', encoding="utf-8")
    assert [it["w"] for it in read_cues_raw(audio)] == ["你"]


def test_supports_cues_only_for_edge():
    class _Fake:
        pass

    assert not supports_cues(_Fake())

    class _Cfg:
        cues = True

    class _FakeEdge:
        cfg = _Cfg()

    # 只要不是 EdgeTTS 实例就不该声称有时间戳
    assert not supports_cues(_FakeEdge())


# ---------------- 对齐算法 ----------------


def test_align_timings_exact_sequence():
    cues = [("做", 0.0), ("视", 0.2), ("频", 0.4), ("号", 0.6), ("最", 0.8), ("费", 1.0)]
    got = align_timings(["做视频", "号最费"], cues, 2.0)
    assert got == [(0.0, 0.6), (0.6, 2.0)]


def test_align_timings_interpolates_inside_word():
    """edge 有时按词给时间戳（「视频」是一个条），词内要按字数摊开。"""
    cues = [("甲", 0.0), ("视频", 1.0), ("乙", 3.0)]
    got = align_timings(["甲", "视", "频乙"], cues, 4.0)
    assert got[1][0] == 1.0, "「视」就是词的起点"
    assert got[2][0] == 2.0, "「频」应摊到词中间，而不是沿用词起点 1.0"


def test_align_timings_ignores_punctuation_on_both_sides():
    """时间戳里没有标点（edge 不给标点发 WordBoundary），对齐前必须剥掉。"""
    cues = [("你", 0.5), ("好", 0.7)]
    got = align_timings(["你好，", "世界。"], cues, 1.0)
    assert got[0][0] == 0.5


def test_align_timings_empty_group_does_not_break():
    """整组都是标点（没有读音字符）时，起点沿用上一组，不能崩也不能倒退。"""
    got = align_timings(["。", "你好"], [("你", 0.5), ("好", 0.7)], 1.0)
    assert got == [(0.0, 0.5), (0.5, 1.0)]


def test_align_timings_last_group_runs_to_total():
    cues = [("甲", 0.0), ("乙", 0.1)]
    got = align_timings(["甲", "乙"], cues, 9.0)
    assert got[-1][1] == 9.0


def test_align_timings_monotonic():
    """起点必须单调不减，否则 ASS 会出现结束早于开始的事件被 libass 丢掉。"""
    cues = [("甲", 0.0), ("乙", 3.0), ("丙", 1.0), ("丁", 4.0)]
    got = align_timings(["甲乙", "丙丁"], cues, 5.0)
    starts = [s for s, _ in got]
    assert starts == sorted(starts)
    assert all(en > st for st, en in got)


def test_align_timings_no_cues_returns_none():
    assert align_timings(["甲"], [], 1.0) is None
    assert align_timings(["甲"], None, 1.0) is None


def test_align_timings_empty_groups_returns_none():
    assert align_timings([], [("甲", 0.0)], 1.0) is None
    assert align_timings(["。"], [("甲", 0.0)], 1.0) is None


def test_align_timings_rejects_far_off_text():
    """时间戳跟文案根本不是同一段话时，宁可退回按字数，也不要硬对。"""
    assert align_timings(["甲乙丙丁"], [("w", 0.0)] * 4, 2.0) is None


def test_align_timings_tolerates_minor_mismatch():
    """ASR/切分导致少一个字时，用序列相似度兜住，别整段放弃。"""
    cues = [("你", 0.0), ("世", 2.0), ("界", 3.0)]
    got = align_timings(["你好", "世界"], cues, 4.0)
    assert got is not None
    assert got[0][0] == 0.0
    assert got[1][0] == 2.0


def test_align_timings_better_than_by_weight():
    """真实场景：句尾标点停顿大 → 按字数均分后半段会明显提前。"""
    # 4 个字 + 长停顿 + 4 个字；按字数均分会把后半段排在 total/2
    cues = [("甲", 0.0), ("乙", 0.2), ("丙", 0.4), ("丁", 0.6), ("戊", 6.0), ("己", 6.2), ("庚", 6.4), ("辛", 6.6)]
    got = align_timings(["甲乙丙丁", "戊己庚辛"], cues, 8.0)
    assert got[1][0] == 6.0, "后半段必须等到 6 秒才出现，而不是按字数排在 4 秒"


def test_write_ass_big_uses_cues(tmp_path):
    """端到端一点点：开了 cues，ASS 里第一条 Dialogue 的起点就是首字的真实时间。"""
    cfg = VideoCfg(width=1080, height=1920, style="big", typewriter=False)
    cues = [("做", 1.5), ("视", 1.7), ("频", 1.9), ("号", 2.1)]
    ass = write_ass(["做视频号"], [(1.5, 4.0)], tmp_path / "a.ass", cfg, total=4.0, cues=cues)

    text = ass.read_text(encoding="utf-8")
    dialogue = [ln for ln in text.splitlines() if ln.startswith("Dialogue")]
    assert dialogue, "应当生成了字幕事件"
    assert dialogue[0].split(",")[1] == "0:00:01.50", "起点应来自逐字时间戳，而不是 0"


def test_write_ass_big_without_cues_falls_back(tmp_path):
    """没有时间戳时退回按字数均分，不能报错。"""
    cfg = VideoCfg(width=1080, height=1920, style="big")
    ass = write_ass(["做视频号"], [(0.0, 4.0)], tmp_path / "a.ass", cfg, total=4.0, cues=None)
    assert "Dialogue" in ass.read_text(encoding="utf-8")


# ---------------- 与 pipeline 的衔接 ----------------


def test_find_voice_ignores_cues_json(tmp_path):
    """旁挂的 .cues.json 排在 .mp3 前面，不过滤就会被当成音频。"""
    (tmp_path / "001_a.cues.json").write_text("[]", encoding="utf-8")
    (tmp_path / "001_a.mp3").write_bytes(b"fake")
    assert _find_voice(tmp_path, "001_a").name == "001_a.mp3"


def test_find_voice_none_when_only_cues(tmp_path):
    (tmp_path / "001_a.cues.json").write_text("[]", encoding="utf-8")
    assert _find_voice(tmp_path, "001_a") is None


def test_find_voice_none_when_missing(tmp_path):
    assert _find_voice(tmp_path, "nope") is None


def test_cues_digest_follows_content(tmp_path):
    audio = tmp_path / "001_a.mp3"
    audio.write_bytes(b"fake")
    assert _cues_digest(str(audio)) == "", "没有时间戳就不该影响指纹"
    assert _cues_digest("") == ""

    write_cues(audio, [{"w": "甲", "t": 0.0, "d": 0.1}])
    first = _cues_digest(str(audio))
    assert first

    write_cues(audio, [{"w": "甲", "t": 5.0, "d": 0.1}])
    assert _cues_digest(str(audio)) != first, "重新配音后时间轴变了，视频必须重出"


def test_video_sig_includes_cues_digest():
    cfg = VideoCfg()
    assert _video_sig("文案", cfg, "abc") != _video_sig("文案", cfg, "")
    assert _video_sig("文案", cfg, "abc") == _video_sig("文案", cfg, "abc")
