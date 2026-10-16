"""封面模块测试：排版/断行纯逻辑 + 真实渲染 + 流水线接入。

排版逻辑全部不依赖 Pillow，所以即使在没装 Pillow 的环境里也能跑；
真正出图的那几条会 `importorskip`，并在系统没有中文字体时跳过。
"""

from __future__ import annotations

import dataclasses

import pytest

from wxcopy.core.cover import (
    CoverCfg,
    CoverError,
    _hex_to_rgb,
    _tw,
    clip,
    find_fonts,
    make_cover,
    make_many,
    pick_font,
    plan_lines,
    title_of,
    wrap_lines,
)
from wxcopy.io.table import export_rows, read_table
from wxcopy.pipeline import CoverOverrides, _cover_fresh, _cover_sig, stage_cover
from wxcopy.settings import Settings

NO_LEAD = "，。、；：,.!?！？）】》」』…"


def _pillow_ok() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def _font_ok() -> bool:
    return bool(find_fonts())


needs_pillow = pytest.mark.skipif(not _pillow_ok(), reason="没装 Pillow")
needs_render = pytest.mark.skipif(not (_pillow_ok() and _font_ok()), reason="没装 Pillow 或系统没有中文字体")


# ---------------- 裁字 ----------------


def test_clip_short_text_untouched():
    assert clip("视频号文案神器", 20) == "视频号文案神器"


def test_clip_strips_trailing_punct_before_ellipsis():
    assert clip("一二三四五六七八九十", 5) == "一二三四五…"
    # 截断点正好落在标点上时，不能出现「…，…」这种叠字
    assert clip("一二三四，五六七八", 5) == "一二三四…"


def test_clip_handles_empty():
    assert clip("", 10) == ""
    assert clip(None, 10) == ""


# ---------------- 显示宽度 ----------------


def test_tw_counts_ascii_as_half_width():
    """按字数算的话「开源免费AI工具推荐」会被切在 A|I 中间，必须按显示宽度算。"""
    assert _tw("中文") == 2.0
    assert _tw("AI") == pytest.approx(1.1)
    assert _tw("中A") == pytest.approx(1.55)


# ---------------- 断行 ----------------


def test_wrap_never_starts_line_with_punct():
    """标点不能出现在行首，否则看着像错别字。"""
    lines = wrap_lines("做视频号最费时间的不是拍摄，是文案", 6, 99)
    assert lines == ["做视频号最费", "时间的不是拍摄，", "是文案"]


def test_plan_lines_never_starts_line_with_punct():
    cases = [
        "做视频号最费时间的不是拍摄，是文案",
        "告别文案烦恼，用AI批量生成口播视频",
        "四步自动化，轻松搞定视频号文案",
        "用AI把对标视频变成自己的原创口播短视频",
    ]
    for c in cases:
        for ln in plan_lines(c, 3):
            assert ln[0] not in NO_LEAD, f"{c} 断出了行首标点：{ln}"


def test_wrap_keeps_english_word_whole():
    """普通英文单词不能被拆开。"""
    lines = wrap_lines("用 Ollama 本地仿写", 6, 99)
    assert "Ollama" in "".join(lines)
    assert not any(ln.rstrip().endswith("Oll") for ln in lines)


def test_wrap_hard_cuts_overlong_token():
    """单个 token 比一行还宽（超长英文串、链接）时只能硬切。"""
    lines = wrap_lines("看看supercalifragilistic这个词", 6, 99)
    assert len(lines) > 1
    assert lines[0] == "看看"
    assert lines[1] == "superc"


def test_wrap_short_text_single_line():
    assert wrap_lines("视频号文案神器", 8, 3) == ["视频号文案神器"]


def test_wrap_respects_max_lines():
    lines = wrap_lines("一二三四五六七八九十一二三四五六七八九十", 4, 3)
    assert len(lines) == 3
    assert lines[-1].endswith("…")


def test_plan_lines_counts():
    assert plan_lines("视频号文案神器", 3) == ["视频号文案神器"]  # 短标题一行
    assert len(plan_lines("开源免费AI工具推荐", 3)) == 2
    assert len(plan_lines("用AI把对标视频变成自己的原创口播短视频", 3)) <= 3


def test_plan_lines_empty_falls_back():
    assert plan_lines("", 3) == ["未命名"]
    assert plan_lines(None, 3) == ["未命名"]


# ---------------- 标题提炼 ----------------


def test_title_of_takes_first_sentence():
    assert title_of("四步自动化，轻松搞定视频号文案。第二句不要。", 20) == "四步自动化，轻松搞定视频号文案"


def test_title_of_falls_back_to_first_clause_when_too_long():
    """整句太长时退一步取逗号前的分句，硬截会断在半截话上。"""
    got = title_of("开源免费AI工具推荐：wxcopy，本地Ollama免费仿写，不花一分钱。", 20)
    assert got == "开源免费AI工具推荐：wxcopy"
    assert len(got) <= 20


def test_title_of_handles_empty():
    assert title_of("", 12) == ""


# ---------------- 颜色 / 配置 ----------------


def test_hex_to_rgb():
    assert _hex_to_rgb("#00F090", (0, 0, 0)) == (0, 240, 144)
    assert _hex_to_rgb("00f090", (0, 0, 0)) == (0, 240, 144)
    assert _hex_to_rgb("乱写", (1, 2, 3)) == (1, 2, 3)
    assert _hex_to_rgb("", (1, 2, 3)) == (1, 2, 3)


def test_cover_overrides_none_means_keep_config():
    """None 一律表示「不覆盖」——默认成 0/"" 会把配置里的值冲掉。"""
    ov = CoverOverrides()
    assert all(getattr(ov, f.name) is None for f in dataclasses.fields(CoverOverrides))

    got = CoverOverrides.coerce({"style": "light", "不存在的键": 1})
    assert got.style == "light"
    assert not hasattr(got, "不存在的键")


# ---------------- 字体 ----------------


def test_find_fonts_returns_paths_that_exist():
    for name, path in find_fonts():
        assert name and path.is_file()


def test_pick_font_prefers_explicit_path(tmp_path):
    if not _font_ok():
        pytest.skip("系统没有中文字体")
    real = find_fonts()[0][1]
    assert pick_font(str(real)) == real


def test_pick_font_unknown_name_falls_back():
    if not _font_ok():
        pytest.skip("系统没有中文字体")
    assert pick_font("绝不存在的字体名") is not None


# ---------------- 真实渲染 ----------------


@needs_render
def test_make_cover_writes_vertical_image(tmp_path):
    p = make_cover("开源免费AI工具推荐", tmp_path / "c.png", CoverCfg(), subtitle="wxcopy")
    assert p.is_file() and p.suffix == ".png"

    from PIL import Image

    im = Image.open(p)
    assert im.size == (1080, 1920), "竖屏封面必须是 1080x1920"


@needs_render
def test_make_cover_jpeg_switches_suffix(tmp_path):
    p = make_cover("标题", tmp_path / "c.png", CoverCfg(jpeg=True))
    assert p.suffix == ".jpg" and p.is_file()


@needs_render
def test_light_style_is_actually_light(tmp_path):
    """浅色风格曾经是「浅底 + 深字」，但底色默认写成深色，字直接看不见了。"""
    from PIL import Image, ImageStat

    p = make_cover("开源免费AI工具推荐", tmp_path / "light.png", CoverCfg(style="light"))
    mean = ImageStat.Stat(Image.open(p).convert("L")).mean[0]
    assert mean > 180, f"light 风格底色不够浅（均值 {mean:.0f}）"


@needs_render
def test_tech_style_is_not_dead_black(tmp_path):
    """tech 风格曾因为把整图 blend 到黑，把光晕一起压没了，整张死黑。"""
    from PIL import Image, ImageStat

    p = make_cover("开源免费AI工具推荐", tmp_path / "tech.png", CoverCfg(style="tech"))
    mean = ImageStat.Stat(Image.open(p).convert("L")).mean[0]
    assert mean > 12, f"tech 风格太黑（均值 {mean:.0f}）"


@needs_render
def test_make_cover_uses_background_image(tmp_path):
    from PIL import Image

    bg = tmp_path / "bg.png"
    Image.new("RGB", (400, 400), (200, 60, 60)).save(bg)
    p = make_cover("标题", tmp_path / "c.png", CoverCfg(bg=str(bg)))
    assert Image.open(p).size == (1080, 1920)


def test_make_cover_without_pillow_raises_covverror(tmp_path, monkeypatch):
    """Pillow 是可选依赖：缺了要给出明确的安装提示，而不是抛 ImportError。"""
    import builtins

    real_import = builtins.__import__

    def fake(name, *a, **kw):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no PIL")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(CoverError, match="Pillow"):
        make_cover("标题", tmp_path / "c.png", CoverCfg())


@needs_render
def test_make_many_isolates_failures(tmp_path, monkeypatch):
    """一条坏数据不能拖垮整批。"""
    import wxcopy.core.cover as cv

    real = cv.make_cover

    def flaky(title, out, cfg=None, subtitle=""):
        if title == "炸":
            raise RuntimeError("boom")
        return real(title, out, cfg, subtitle)

    monkeypatch.setattr(cv, "make_cover", flaky)
    errs = []
    got = make_many(
        [("a", "好标题", ""), ("b", "炸", ""), ("c", "也好", "")],
        tmp_path,
        CoverCfg(),
        on_error=lambda n, m: errs.append(n),
    )
    assert set(got) == {"a", "c"}
    assert errs == ["b"]


# ---------------- 配置默认值 ----------------


def test_settings_has_cover_section():
    st = Settings()
    assert st.cover.style == "tech"
    assert (st.cover.width, st.cover.height) == (1080, 1920)
    assert st.cover_dir.name == "cover"


def test_cover_sig_covers_every_core_field():
    """指纹必须跟着 CoreCoverCfg 的**全部**字段走，漏一个就会出现「改了参数看到旧图」。"""
    from wxcopy.core.cover import CoverCfg as CoreCoverCfg

    base = CoreCoverCfg()
    base_sig = _cover_sig("标题", "副标题", base)
    for f in dataclasses.fields(CoreCoverCfg):
        changed = dataclasses.replace(
            base,
            **{f.name: (not base.__dict__[f.name]) if isinstance(base.__dict__[f.name], bool) else "__变了__"},
        )
        assert _cover_sig("标题", "副标题", changed) != base_sig, f"字段 {f.name} 没进封面指纹"


def test_cover_sig_reacts_to_title_and_subtitle():
    cfg = CoverCfg()
    assert _cover_sig("甲", "", cfg) != _cover_sig("乙", "", cfg)
    assert _cover_sig("甲", "", cfg) != _cover_sig("甲", "副标题", cfg)


def test_cover_fresh(tmp_path):
    sig = _cover_sig("标题", "", CoverCfg())
    assert not _cover_fresh(tmp_path, "a", sig, ".png"), "没有图时应重画"

    (tmp_path / "a.png").write_bytes(b"fake")
    assert not _cover_fresh(tmp_path, "a", sig, ".png"), "没有指纹文件时应重画"

    (tmp_path / "a.cover.txt").write_text(sig, encoding="utf-8")
    assert _cover_fresh(tmp_path, "a", sig, ".png")
    assert not _cover_fresh(tmp_path, "a", "别的指纹", ".png"), "指纹变了必须重画"


# ---------------- stage_cover ----------------


def _table(tmp_path, rows):
    p = tmp_path / "t.xlsx"
    export_rows(p, rows, ["序号", "仿写文案"], sheet="文案")
    return p


def test_stage_cover_single_mode_needs_no_table(tmp_path):
    """单张模式：给 --text 就出图，不必先有表格。"""
    if not (_pillow_ok() and _font_ok()):
        pytest.skip("没装 Pillow 或系统没有中文字体")
    st = Settings()
    p = stage_cover(
        st,
        None,
        CoverOverrides(),
        text="四步自动化，轻松搞定视频号文案。第二句。",
        out_dir=tmp_path,
        name="demo",
    )
    assert p.is_file() and p.name == "demo.png"


@needs_render
def test_stage_cover_batch_writes_column_and_skips_second_run(tmp_path):
    table = _table(
        tmp_path,
        [
            {"序号": 1, "仿写文案": "四步自动化，轻松搞定视频号文案。"},
            {"序号": 2, "仿写文案": "开源免费AI工具推荐：wxcopy，本地Ollama免费仿写。"},
        ],
    )
    st = Settings()
    out = tmp_path / "cover"
    stage_cover(st, table, CoverOverrides(), out_dir=out, skip_existing=True)

    rows, cols = read_table(table, sheet="文案")
    assert "封面" in cols
    assert all(r["封面"] and r["封面"].endswith(".png") for r in rows)

    # 二次运行：指纹一致，全部沿用
    mtime = (out / "001_1.png").stat().st_mtime_ns
    stage_cover(st, table, CoverOverrides(), out_dir=out, skip_existing=True)
    assert (out / "001_1.png").stat().st_mtime_ns == mtime, "指纹一致时不该重画"

    # 换个风格：必须重画
    stage_cover(st, table, CoverOverrides(style="light"), out_dir=out, skip_existing=True)
    assert (out / "001_1.png").stat().st_mtime_ns != mtime, "改了风格必须重画"


@needs_render
def test_stage_cover_prefers_title_column(tmp_path):
    """有「推荐标题」列时优先用它，而不是从长文案里现抠。"""
    p = tmp_path / "t.xlsx"
    export_rows(
        p,
        [{"序号": 1, "仿写文案": "很长的文案" * 10, "推荐标题": "短标题"}],
        ["序号", "仿写文案", "推荐标题"],
        sheet="文案",
    )
    st = Settings()
    out = tmp_path / "cover"
    stage_cover(st, p, CoverOverrides(), out_dir=out)
    assert (out / "001_1.cover.txt").is_file()
    # 指纹里应当含「短标题」而不是那串长文案
    assert _cover_sig("短标题", "", CoverCfg()) == (out / "001_1.cover.txt").read_text(encoding="utf-8")


@needs_render
def test_stage_cover_force_redraws(tmp_path):
    table = _table(tmp_path, [{"序号": 1, "仿写文案": "标题一。"}])
    st = Settings()
    out = tmp_path / "cover"
    stage_cover(st, table, CoverOverrides(), out_dir=out)
    before = (out / "001_1.png").stat().st_mtime_ns
    stage_cover(st, table, CoverOverrides(), out_dir=out, skip_existing=False)
    assert (out / "001_1.png").stat().st_mtime_ns != before
