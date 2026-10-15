"""pipeline.py 里几个「不跑模型、不调 ffmpeg」的关键纯函数测试。"""
from __future__ import annotations

import dataclasses

import pytest

from wxcopy.core.video import VideoCfg
from wxcopy.pipeline import (
    _SIG_FIELDS,
    ASR_COLUMNS,
    VideoOverrides,
    _coerce_legacy,
    _default_name_col,
    _merge_asr_rows,
    _row_name,
    _video_fresh,
    _video_sig,
)

# ---------------- 表格合并（保住已有成果的关键） ----------------

def test_merge_keeps_extra_columns():
    """转写结果并回旧表时，「仿写文案/配音/视频」等列不能被清掉。"""
    old = [{"文件原名": "a.mp4", "转写文案": "旧文", "仿写文案": "已仿写", "视频": "1.mp4"}]
    old_cols = ASR_COLUMNS + ["仿写文案", "视频"]
    new = [{"文件原名": "a.mp4", "转写文案": "新文"}]

    rows, cols = _merge_asr_rows(old, old_cols, new)
    assert rows[0]["转写文案"] == "新文"      # ASR 列取新值
    assert rows[0]["仿写文案"] == "已仿写"    # 其他列保住
    assert rows[0]["视频"] == "1.mp4"
    assert "仿写文案" in cols and "视频" in cols


def test_merge_preserves_old_row_order():
    """行序必须稳定：产物叫「行号_名字」，行序一漂同一条就会被反复重做。"""
    old = [
        {"文件原名": "a.mp4", "转写文案": "A"},
        {"文件原名": "b.mp4", "转写文案": "B"},
    ]
    new = [
        {"文件原名": "b.mp4", "转写文案": "B2"},
        {"文件原名": "a.mp4", "转写文案": "A2"},
    ]
    rows, _ = _merge_asr_rows(old, ASR_COLUMNS, new)
    assert [r["文件原名"] for r in rows] == ["a.mp4", "b.mp4"]
    assert [r["转写文案"] for r in rows] == ["A2", "B2"]


def test_merge_appends_new_rows_at_end():
    old = [{"文件原名": "a.mp4", "转写文案": "A"}]
    new = [
        {"文件原名": "b.mp4", "转写文案": "B"},
        {"文件原名": "a.mp4", "转写文案": "A2"},
    ]
    rows, _ = _merge_asr_rows(old, ASR_COLUMNS, new)
    assert [r["文件原名"] for r in rows] == ["a.mp4", "b.mp4"]


def test_merge_renumbers_sequentially():
    old = [{"文件原名": "a.mp4"}, {"文件原名": "b.mp4"}]
    new = [{"文件原名": "c.mp4"}]
    rows, _ = _merge_asr_rows(old, ASR_COLUMNS, new)
    assert [r["序号"] for r in rows] == [1, 2, 3]


def test_merge_keeps_rows_not_in_this_scan():
    """本轮没扫到的历史行要原样留着，否则已生成的配音/视频记录会丢。"""
    old = [
        {"文件原名": "a.mp4", "转写文案": "A", "配音": "a.mp3"},
        {"文件原名": "gone.mp4", "转写文案": "G", "配音": "g.mp3"},
    ]
    old_cols = ASR_COLUMNS + ["配音"]
    new = [{"文件原名": "a.mp4", "转写文案": "A2"}]
    rows, _ = _merge_asr_rows(old, old_cols, new)
    assert len(rows) == 2
    assert rows[1]["配音"] == "g.mp3"


# ---------------- 视频指纹（断点续跑） ----------------

def _cfg(**over):
    return VideoCfg(**over)


def test_sig_changes_when_text_changes():
    assert _video_sig("文案一", _cfg()) != _video_sig("文案二", _cfg())


def test_sig_changes_when_render_param_changes():
    """改了字幕参数必须重出——否则用户以为参数没生效。"""
    a = _video_sig("同一段文案", _cfg(style="classic"))
    b = _video_sig("同一段文案", _cfg(style="big"))
    assert a != b

    c = _video_sig("同一段文案", _cfg(typewriter=False))
    d = _video_sig("同一段文案", _cfg(typewriter=True))
    assert c != d


def test_sig_stable_for_same_input():
    assert _video_sig("同一段文案", _cfg()) == _video_sig("同一段文案", _cfg())


def test_sig_fields_all_exist_on_videocfg():
    """_SIG_FIELDS 是手工维护的，一旦拼错或字段改名就会静默失效。"""
    real = {f.name for f in dataclasses.fields(VideoCfg)}
    for name in _SIG_FIELDS:
        assert name in real, f"_SIG_FIELDS 里的 {name} 不是 VideoCfg 的字段"


def test_video_fresh(tmp_path):
    sig = _video_sig("文案", _cfg())
    (tmp_path / "v.mp4").write_bytes(b"fake")

    assert not _video_fresh(tmp_path, "v", sig), "没有指纹文件时应重出"

    (tmp_path / "v.src.txt").write_text(sig, encoding="utf-8")
    assert _video_fresh(tmp_path, "v", sig), "指纹一致时可跳过"

    assert not _video_fresh(tmp_path, "v", "other-sig"), "指纹变了必须重出"
    assert not _video_fresh(tmp_path, "missing", sig), "视频不存在时必须重出"


def test_video_fresh_treats_legacy_output_as_stale(tmp_path):
    """老版本产出的视频没有旁挂指纹，应当重出一次保证与当前文案一致。"""
    (tmp_path / "old.mp4").write_bytes(b"fake")
    assert not _video_fresh(tmp_path, "old", _video_sig("文案", _cfg()))


@pytest.mark.parametrize("missing", ["typewriter", "type_hold", "type_min"])
def test_sig_covers_typewriter_params(missing):
    """打字机参数是新加的，必须进入指纹，否则改了节奏看不到新片。"""
    assert missing in _SIG_FIELDS


# ---------------- VideoOverrides（阶段四参数收敛） ----------------

def test_overrides_defaults_are_none():
    """None 表示「不覆盖」；默认成 0/False 会把配置里的值冲掉。"""
    ov = VideoOverrides()
    assert ov.crf is None
    assert ov.subtitle is None
    assert ov.typewriter is None


def test_overrides_coerce_ignores_unknown_keys():
    """Web 端会传整包 payload，未知字段必须忽略而不是报错。"""
    ov = VideoOverrides.coerce({"style": "big", "乱七八糟": 1, "workdir": "x"})
    assert ov.style == "big"
    assert not hasattr(ov, "乱七八糟")
    assert not hasattr(ov, "workdir")


def test_overrides_coerce_none_gives_empty():
    assert VideoOverrides.coerce(None).style is None
    assert VideoOverrides.coerce({}).style is None


def test_legacy_flat_kwargs_still_work():
    """旧调用方式 stage_video(..., style='big', crf=18) 必须继续可用。"""
    ov = _coerce_legacy(None, {"style": "big", "crf": 18, "subtitle": None})
    assert ov.style == "big"
    assert ov.crf == 18
    assert ov.subtitle is None, "显式传 None 应视为未设置"


def test_legacy_kwargs_merge_with_overrides():
    ov = VideoOverrides(style="classic", crf=23)
    merged = _coerce_legacy(ov, {"crf": 18})
    assert merged.crf == 18
    assert merged.style == "classic", "未出现在 legacy 里的字段要保留"


def test_overrides_accepts_dict_directly():
    ov = _coerce_legacy({"style": "big"}, {})
    assert ov.style == "big"


def test_row_name_is_stable_and_zero_padded():
    row = {"文件原名": "门店引流"}
    assert _row_name(row, "文件原名", 1) == "001_门店引流"
    assert _row_name(row, "文件原名", 42) == "042_门店引流"


def test_row_name_falls_back_to_rownumber():
    """命名列为空时不能产出 '001_' 这种没名字的产物。"""
    assert _row_name({}, "文件原名", 3) == "003_row_3"


def test_row_name_sanitizes_slashes():
    """命名列里带 / 时不能只剩最后一段（Windows 路径语义）。"""
    row = {"标题": "门店/引流"}
    assert _row_name(row, "标题", 1) == "001_门店_引流"


def test_default_name_col_prefers_informative_column():
    """单独跑 speak/video 不传 --name-column 时，产物不能都叫 001_1.mp4。"""
    assert _default_name_col(["序号", "文件原名", "转写文案"]) == "文件原名"
    assert _default_name_col(["序号", "标题"]) == "标题"
    assert _default_name_col(["序号", "备注"]) == "序号", "没有更像名字的列时退回第一列"
    assert _default_name_col([]) == "序号"
