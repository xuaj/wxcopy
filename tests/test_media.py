"""core/media.py 的纯函数测试（不涉及真实的 ffmpeg 调用）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from wxcopy.core.media import safe_stem


def test_safe_stem_removes_illegal_chars():
    """Windows 文件名非法字符必须被替换，否则落盘会直接报错。"""
    assert safe_stem(Path('a<b>c:d"e/f|g?h*i')) == "a_b_c_d_e_f_g_h_i"


def test_safe_stem_strips_trailing_dots_and_spaces():
    assert safe_stem(Path("  abc...  ")) == "abc"


def test_safe_stem_respects_limit():
    long_name = "x" * 100
    assert len(safe_stem(Path(long_name), limit=20)) == 20


def test_safe_stem_fallback_for_empty():
    """空名字不能返回空串，否则产物会写成 '001_.mp3' 这种。"""
    assert safe_stem(Path("...")) == "untitled"
    assert safe_stem(Path("   ")) == "untitled"


def test_safe_stem_keeps_chinese():
    assert safe_stem(Path("门店引流方案")) == "门店引流方案"


# ---------------- 没有 ffmpeg 时必须优雅降级 ----------------


def test_get_ffmpeg_raises_helpful_error(monkeypatch):
    """真需要转码时该明确报错，并把安装方式说清楚。"""
    import wxcopy.core.media as m

    monkeypatch.setattr(m.shutil, "which", lambda _: None)
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        m.get_ffmpeg()


def test_has_ffmpeg_false_without_ffmpeg(monkeypatch):
    import wxcopy.core.media as m

    monkeypatch.setattr(m, "get_ffmpeg", _raise_no_ffmpeg)
    assert m.has_ffmpeg() is False


def test_probe_duration_returns_zero_without_ffmpeg(tmp_path, monkeypatch):
    """探测时长只是「尽力而为」，没装 ffmpeg 也该返回 0，而不是抛异常。

    这个坑真实发生过：CI 里没装 imageio-ffmpeg，publish 的 plan_row 想给清单
    填个时长，结果整条命令崩掉——三个平台的 CI 全红。
    """
    import wxcopy.core.media as m

    f = tmp_path / "v.mp4"
    f.write_bytes(b"fake")
    monkeypatch.setattr(m.shutil, "which", lambda _: None)  # 连 ffprobe 也没有
    monkeypatch.setattr(m, "get_ffmpeg", _raise_no_ffmpeg)
    assert m.probe_duration(f) == 0.0


def test_probe_duration_missing_file_is_zero(tmp_path):
    from wxcopy.core.media import probe_duration

    assert probe_duration(tmp_path / "没有这个文件.mp4") == 0.0


def _raise_no_ffmpeg():
    raise RuntimeError("未找到 ffmpeg。")
