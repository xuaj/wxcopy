"""core/media.py 的纯函数测试（不涉及 ffmpeg 调用部分）。"""

from __future__ import annotations

from pathlib import Path

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
