"""BGM 混音与自动闪避的纯逻辑测试（不调 ffmpeg）。

滤镜串是拼字符串拼出来的，一旦拼错只有跑起来才会发现，
所以这里把关键约束都钉死：闪避必须有、`amix` 必须关 normalize、
人声必须排在第一位（否则无限流的 BGM 会让输出永不结束）。
"""

from __future__ import annotations

import dataclasses

from wxcopy.core.video import AUDIO_SR, VideoCfg, build_audio_chain, file_stamp
from wxcopy.pipeline import _SIG_FIELDS, _video_sig


def _chain(**over) -> str:
    return build_audio_chain(VideoCfg(**over), total=20.0, bgm_ok=bool(over.get("bgm")))


# ---------------- 没有 BGM：退化成单路 ----------------


def test_no_bgm_is_single_track():
    got = build_audio_chain(VideoCfg(), 10.0, bgm_ok=False)
    assert "sidechaincompress" not in got
    assert "amix" not in got
    assert got.endswith("[a]")
    assert got.count(";") == 0, "单路不该有多余的分支"


def test_no_bgm_keeps_original_format():
    """没有 BGM 就别动声道/采样率，免得不需要混音的人也被改了音频格式。"""
    got = build_audio_chain(VideoCfg(tail_pad=1.25), 10.0, bgm_ok=False)
    assert got == "[1:a]apad=pad_dur=1.25[a]"


def test_bgm_set_but_file_missing_falls_back_to_single():
    """BGM 文件找不到时按无人声背景乐处理，不能让整条流水线挂掉。"""
    got = build_audio_chain(VideoCfg(bgm="不存在.mp3"), 10.0, bgm_ok=False)
    assert "amix" not in got and got.endswith("[a]")


# ---------------- 有 BGM ----------------


def test_bgm_chain_has_ducking():
    got = _chain(bgm="bgm.mp3")
    assert "sidechaincompress" in got
    assert "asplit=2[voice][voice_sc]" in got, "人声必须先分流，否则 ffmpeg 会报 matches no streams"
    assert "[bgm0][voice_sc]sidechaincompress" in got


def test_bgm_duck_can_be_turned_off():
    got = _chain(bgm="bgm.mp3", bgm_duck=False)
    assert "sidechaincompress" not in got
    assert "asplit" not in got


def test_amix_disables_normalize():
    """amix 默认 normalize=1 会把每路各降一半，人声直接变小。"""
    for got in (_chain(bgm="bgm.mp3"), _chain(bgm="bgm.mp3", bgm_duck=False)):
        assert "normalize=0" in got


def test_amix_duration_first_and_voice_goes_first():
    """duration=first 取第一路的长度；BGM 是无限流，必须排在人声后面。"""
    got = _chain(bgm="bgm.mp3")
    assert "duration=first" in got
    assert "[voice][bgmd]amix" in got

    got2 = _chain(bgm="bgm.mp3", bgm_duck=False)
    assert "[voice][bgm0]amix" in got2


def test_bgm_volume_applied():
    assert "volume=0.180" in _chain(bgm="bgm.mp3")
    assert "volume=0.500" in _chain(bgm="bgm.mp3", bgm_volume=0.5)


def test_duck_ratio_applied():
    assert "ratio=8" in _chain(bgm="bgm.mp3")
    assert "ratio=20" in _chain(bgm="bgm.mp3", bgm_duck_ratio=20)


def test_fade_out_position_is_relative_to_total():
    """淡出必须落在结尾：st = 总时长 - 淡出时长。"""
    got = build_audio_chain(VideoCfg(bgm="bgm.mp3", bgm_fade_out=2.0), total=12.5, bgm_ok=True)
    assert "afade=t=out:st=10.50:d=2.00" in got


def test_fade_out_can_be_disabled():
    got = _chain(bgm="bgm.mp3", bgm_fade_out=0)
    assert "afade" not in got


def test_fade_out_never_negative():
    """音频比淡出还短时不能算出负的起始时间，否则 ffmpeg 直接报错。"""
    got = build_audio_chain(VideoCfg(bgm="bgm.mp3", bgm_fade_out=5.0), total=1.0, bgm_ok=True)
    assert "afade=t=out:st=0.00:d=5.00" in got


def test_all_branches_share_same_sample_format():
    got = _chain(bgm="bgm.mp3")
    assert got.count(f"sample_rates={AUDIO_SR}") == 2, "人声与 BGM 都要统一采样格式才能混音"


def test_chain_labels_are_balanced():
    """每段链里被引用的标签都必须先被定义过，否则 ffmpeg 解析直接失败。"""
    got = _chain(bgm="bgm.mp3")
    defined: set[str] = set()
    for part in got.split(";"):
        ins, outs = _split_labels(part)
        for label in ins:
            # [1:a] / [2:a] 这类是输入流，天然存在
            assert label in defined or ":" in label, f"用了未定义的标签 [{label}]：{part}"
        defined.update(outs)
    assert "a" in defined, "最终必须产出 [a]"


def _split_labels(part: str) -> tuple[list[str], list[str]]:
    """把一段链拆成 (前面的输入标签, 后面的输出标签)。

    `asplit=2[voice][voice_sc]` 这种一段出两个标签的写法也要能正确识别。
    """
    import re

    m = re.match(r"^((?:\[[^\]]*\])*)(.*?)((?:\[[^\]]*\])*)$", part)
    assert m, f"解析不了这段滤镜链：{part}"
    grab = lambda s: re.findall(r"\[([^\]]*)\]", s)  # noqa: E731
    return grab(m.group(1)), grab(m.group(3))


# ---------------- 文件指纹 ----------------


def test_file_stamp_empty_returns_empty():
    """空路径必须显式返回空串：Path("") 是当前目录，时间戳会一直变。"""
    assert file_stamp("") == ""
    assert file_stamp("   ") == ""


def test_file_stamp_missing_returns_empty(tmp_path):
    assert file_stamp(str(tmp_path / "没有这个文件.mp3")) == ""


def test_file_stamp_changes_when_file_changes(tmp_path):
    p = tmp_path / "bgm.mp3"
    p.write_bytes(b"a" * 100)
    first = file_stamp(str(p))
    assert first
    p.write_bytes(b"b" * 200)
    assert file_stamp(str(p)) != first


def test_file_stamp_ignores_directories(tmp_path):
    assert file_stamp(str(tmp_path)) == ""


# ---------------- 指纹必须带上 BGM ----------------


def test_sig_fields_include_bgm():
    for name in ("bgm", "bgm_volume", "bgm_duck", "bgm_duck_ratio", "bgm_fade_out"):
        assert name in _SIG_FIELDS, f"{name} 不在 _SIG_FIELDS 里，改了它不会重出视频"


def test_sig_fields_all_exist_on_videocfg():
    real = {f.name for f in dataclasses.fields(VideoCfg)}
    for name in _SIG_FIELDS:
        assert name in real, f"_SIG_FIELDS 里的 {name} 不是 VideoCfg 的字段"


def test_sig_changes_when_bgm_params_change():
    cfg = VideoCfg()
    base = _video_sig("文案", cfg)
    assert _video_sig("文案", dataclasses.replace(cfg, bgm="a.mp3")) != base
    assert _video_sig("文案", dataclasses.replace(cfg, bgm_volume=0.5)) != base
    assert _video_sig("文案", dataclasses.replace(cfg, bgm_duck=False)) != base
    assert _video_sig("文案", dataclasses.replace(cfg, bgm_fade_out=3.0)) != base


def test_sig_changes_when_bgm_file_replaced(tmp_path):
    """同一个路径换了文件内容，也必须重出，否则听到的是旧音乐。"""
    p = tmp_path / "bgm.mp3"
    p.write_bytes(b"x" * 64)
    cfg = VideoCfg(bgm=str(p))
    first = _video_sig("文案", cfg)
    p.write_bytes(b"y" * 128)
    assert _video_sig("文案", cfg) != first


def test_sig_stable_without_bgm():
    assert _video_sig("文案", VideoCfg()) == _video_sig("文案", VideoCfg())
