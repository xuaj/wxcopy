"""Web 控制台的后端测试（不联网、不调 ffmpeg）。

三块内容：
1. `/api/defaults` 要把 v0.5.0 新增的 BGM / 封面 / 发布默认值都带出去，
   少一个键前端就会渲染成 undefined。
2. `/api/cover/preview` 能真的吐出一张图。
3. `make_runner` 的「参数落地」——前端传来的字典必须变成 Settings 上的值
   并原样传给 `run_pipeline`，这是最容易悄悄断掉的一环。

fastapi 是可选依赖（`web` extra），装了才跑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from wxcopy.web.app import create_app  # noqa: E402

# ---------------- /api/defaults ----------------

# 新增的键；少一个前端就会拿到 undefined
NEW_DEFAULT_KEYS = [
    "video_bgm",
    "video_bgm_volume",
    "video_bgm_duck",
    "video_bgm_duck_ratio",
    "video_bgm_fade_out",
    "cover_styles",
    "cover_style",
    "cover_bg",
    "cover_bg_color",
    "cover_accent",
    "cover_footer",
    "cover_font",
    "cover_title_max",
    "cover_subtitle_max",
    "cover_jpeg",
    "cover_dir",
    "publish_titles",
    "publish_desc_max",
    "publish_topics",
    "publish_platforms",
    "publish_concurrency",
    "publish_auto_cover",
    "publish_prompts",
]


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:
        yield c


def test_defaults_exposes_bgm_cover_publish(client):
    d = client.get("/api/defaults").json()
    missing = [k for k in NEW_DEFAULT_KEYS if k not in d]
    assert not missing, f"/api/defaults 缺少键：{missing}"


def test_defaults_cover_styles_are_selectable(client):
    styles = client.get("/api/defaults").json()["cover_styles"]
    ids = {s["id"] for s in styles}
    assert {"tech", "plain", "light"} <= ids
    # 风格必须有名字，否则下拉框是空的
    assert all(s.get("name") for s in styles)


def test_defaults_publish_prompts_not_empty(client):
    prompts = client.get("/api/defaults").json()["publish_prompts"]
    assert prompts and all(isinstance(p, str) for p in prompts)


# ---------------- /api/cover/preview ----------------


def test_cover_preview_returns_png(client):
    r = client.post("/api/cover/preview", json={"title": "三分钟看懂", "subtitle": "本地跑"})
    if r.status_code == 400:
        # 环境里没有 Pillow 或没有中文字体，属于预期内的降级
        pytest.skip("封面依赖不满足：" + r.json().get("detail", "")[:80])
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    # PNG 文件头
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_cover_preview_does_not_touch_workdir(client, tmp_path):
    """预览绝不能写进产物目录——那会让指纹误判成「已生成」从而跳过真跑。"""
    r = client.post("/api/cover/preview", json={"title": "预览", "subtitle": ""})
    if r.status_code == 400:
        pytest.skip("封面依赖不满足")
    cover_dir = Path(client.get("/api/defaults").json()["cover_dir"])
    if cover_dir.is_dir():
        assert not list(cover_dir.iterdir()), f"预览把文件写进了 {cover_dir}"


def test_cover_preview_rejects_bad_payload_gracefully(client):
    """空 body 也要按默认值出图，不能 500。"""
    r = client.post("/api/cover/preview", json={})
    assert r.status_code in (200, 400)


# ---------------- make_runner 参数落地 ----------------


def _params(tmp_path: Path, **over) -> dict:
    base = {
        "workdir": str(tmp_path),
        "source": {"kind": "text", "text": "你好世界", "name": "demo"},
        "video": {
            "enabled": True,
            "bgm": str(tmp_path / "bgm.mp3"),
            "bgm_volume": 0.33,
            "bgm_duck": False,
            "bgm_duck_ratio": 12,
            "bgm_fade_out": 2.5,
        },
        "cover": {
            "enabled": True,
            "style": "light",
            "accent": "#FF8800",
            "footer": "@测试号",
            "font": "",
            "title_max": 18,
            "subtitle_max": 30,
            "jpeg": True,
            "title_column": "标题列",
            "subtitle_column": "副标题列",
        },
        "publish": {
            "enabled": True,
            "platforms": "视频号",
            "titles": 3,
            "topics": 4,
            "desc_max": 80,
            "concurrency": 2,
            "auto_cover": False,
            "bundle": True,
        },
    }
    base.update(over)
    return base


def test_runner_forwards_bgm_cover_publish(monkeypatch, tmp_path):
    from wxcopy.web.jobs import Job, make_runner

    (tmp_path / "bgm.mp3").write_bytes(b"ID3\x00")

    captured: dict = {}
    cover_dir = tmp_path / "cover"
    cover_dir.mkdir()
    (cover_dir / "1_demo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    plan = tmp_path / "publish_plan.xlsx"
    plan.write_bytes(b"PK\x03\x04")

    def fake_run_pipeline(st, **kw):
        captured["st"] = st
        captured["kw"] = kw
        return {
            "table": str(tmp_path / "transcripts.xlsx"),
            "cover_dir": str(cover_dir),
            "publish_plan": str(plan),
        }

    monkeypatch.setattr("wxcopy.pipeline.run_pipeline", fake_run_pipeline)

    job = Job(id="t1", params=_params(tmp_path))
    res = make_runner(job.params)(job)

    st = captured["st"]
    kw = captured["kw"]
    # --- BGM 落到 Settings ---
    assert st.video.bgm == str(tmp_path / "bgm.mp3")
    assert st.video.bgm_volume == 0.33
    assert st.video.bgm_duck is False
    assert st.video.bgm_duck_ratio == 12.0
    assert st.video.bgm_fade_out == 2.5
    assert kw["bgm"] == str(tmp_path / "bgm.mp3")
    # --- 封面 ---
    assert st.cover.style == "light"
    assert st.cover.accent == "#FF8800"
    assert st.cover.footer == "@测试号"
    assert st.cover.title_max == 18
    assert st.cover.jpeg is True
    assert kw["make_cover"] is True
    assert kw["cover_title_column"] == "标题列"
    assert kw["cover_subtitle_column"] == "副标题列"
    # --- 发布 ---
    assert st.publish.platforms == "视频号"
    assert st.publish.titles == 3
    assert st.publish.auto_cover is False
    assert kw["make_publish"] is True
    assert kw["publish"]["bundle"] is True
    # --- 产物汇总（runner 返回，由 JobManager.start 写回 job.outputs）---
    assert res["cover_count"] == 1
    assert res["cover_files"] == [str(cover_dir / "1_demo.png")]
    assert res["publish_plan_exists"] is True
    # 没有 voice_dir 时不能把当前目录的文件当配音产物报出来
    assert "voice_count" not in res


def test_runner_keeps_stages_off_when_not_asked(monkeypatch, tmp_path):
    """没勾封面/发布时不能偷偷跑——发布会调 LLM，那是要花钱和时间的。"""
    from wxcopy.web.jobs import Job, make_runner

    captured: dict = {}

    def fake_run_pipeline(st, **kw):
        captured["kw"] = kw
        return {"table": str(tmp_path / "transcripts.xlsx")}

    monkeypatch.setattr("wxcopy.pipeline.run_pipeline", fake_run_pipeline)

    params = _params(tmp_path, cover={"enabled": False}, publish={"enabled": False})
    job = Job(id="t2", params=params)
    res = make_runner(params)(job)

    assert captured["kw"]["make_cover"] is False
    assert captured["kw"]["make_publish"] is False
    assert "cover_count" not in res


def test_runner_bgm_only_still_applies_settings(monkeypatch, tmp_path):
    """只勾了 BGM 没勾视频时，音量/闪避这些也要生效（视频由 run_pipeline 兜底触发）。"""
    from wxcopy.web.jobs import Job, make_runner

    (tmp_path / "bgm.mp3").write_bytes(b"ID3\x00")
    captured: dict = {}

    def fake_run_pipeline(st, **kw):
        captured["st"] = st
        return {"table": str(tmp_path / "transcripts.xlsx")}

    monkeypatch.setattr("wxcopy.pipeline.run_pipeline", fake_run_pipeline)

    params = _params(tmp_path, video={"enabled": False, "bgm": str(tmp_path / "bgm.mp3"), "bgm_volume": 0.5})
    job = Job(id="t3", params=params)
    make_runner(params)(job)

    assert captured["st"].video.bgm == str(tmp_path / "bgm.mp3")
    assert captured["st"].video.bgm_volume == 0.5


def test_runner_voice_list_excludes_cues_json(monkeypatch, tmp_path):
    """配音目录里还躺着 <name>.cues.json（逐字时间戳），不能算成配音文件。

    不按后缀过滤的话页面显示的配音数量会直接翻倍，用户会以为重跑出了两份。
    """
    from wxcopy.web.jobs import Job, make_runner

    vd = tmp_path / "voice"
    vd.mkdir()
    (vd / "1_a.mp3").write_bytes(b"x")
    (vd / "1_a.cues.json").write_text("{}", encoding="utf-8")
    (vd / "2_b.wav").write_bytes(b"x")

    def fake_run_pipeline(st, **kw):
        return {"table": str(tmp_path / "t.xlsx"), "voice_dir": str(vd)}

    monkeypatch.setattr("wxcopy.pipeline.run_pipeline", fake_run_pipeline)

    params = {"workdir": str(tmp_path), "source": {"kind": "text", "text": "x"}}
    res = make_runner(params)(Job(id="t4", params=params))

    assert res["voice_count"] == 2
    assert not any(f.endswith(".json") for f in res["voice_files"])


# ---------------- run_pipeline 开关 ----------------


def test_run_pipeline_runs_cover_and_publish_on_demand(monkeypatch, tmp_path):
    from wxcopy import pipeline

    seen: list[str] = []
    tbl = tmp_path / "transcripts.xlsx"

    def fake_source(*a, **k):
        seen.append("source")
        return tbl

    def fake_rewrite(*a, **k):
        seen.append("rewrite")
        return tbl

    def fake_tts(*a, **k):
        seen.append("tts")
        return tmp_path / "voice"

    def fake_video(*a, **k):
        seen.append("video")
        return tmp_path / "video"

    def fake_cover(*a, **k):
        seen.append("cover")
        return tbl

    def fake_publish(*a, **k):
        seen.append("publish")
        return tmp_path / "publish_plan.xlsx"

    for name, fn in [
        ("stage_source", fake_source),
        ("stage_rewrite", fake_rewrite),
        ("stage_tts", fake_tts),
        ("stage_video", fake_video),
        ("stage_cover", fake_cover),
        ("stage_publish", fake_publish),
    ]:
        monkeypatch.setattr(pipeline, name, fn)

    from wxcopy.settings import load_settings

    st = load_settings()
    st.paths.workdir = str(tmp_path)

    # 默认两个新阶段都不跑
    out = pipeline.run_pipeline(st, workdir=str(tmp_path), source={"kind": "text", "text": "你好"})
    assert "cover" not in seen and "publish" not in seen
    assert "cover_dir" not in out and "publish_plan" not in out

    # 打开后才跑
    seen.clear()
    out = pipeline.run_pipeline(
        st,
        workdir=str(tmp_path),
        source={"kind": "text", "text": "你好"},
        make_video=True,
        make_cover=True,
        make_publish=True,
        cover={"style": "light"},
        publish={"bundle": True},
    )
    assert seen == ["source", "rewrite", "tts", "video", "cover", "publish"]
    assert out["cover_dir"] == st.cover_dir
    assert out["publish_plan"] == tmp_path / "publish_plan.xlsx"


def test_run_pipeline_cover_failure_does_not_kill_pipeline(monkeypatch, tmp_path):
    """缺 Pillow / 缺中文字体时封面该跳过，而不是让整条流水线挂掉。"""
    from wxcopy import pipeline
    from wxcopy.core.cover import CoverError

    tbl = tmp_path / "transcripts.xlsx"
    monkeypatch.setattr(pipeline, "stage_source", lambda *a, **k: tbl)
    monkeypatch.setattr(pipeline, "stage_rewrite", lambda *a, **k: tbl)
    monkeypatch.setattr(pipeline, "stage_tts", lambda *a, **k: tmp_path / "voice")

    def boom(*a, **k):
        raise CoverError("生成封面需要 Pillow")

    monkeypatch.setattr(pipeline, "stage_cover", boom)

    from wxcopy.settings import load_settings

    st = load_settings()
    st.paths.workdir = str(tmp_path)
    out = pipeline.run_pipeline(st, workdir=str(tmp_path), source={"kind": "text", "text": "x"}, make_cover=True)
    # 没抛异常，且因为没生成成功所以不带 cover_dir
    assert "cover_dir" not in out


def test_run_pipeline_bgm_implies_video(monkeypatch, tmp_path):
    """只给 BGM 也得出片——否则用户的音乐白选了。"""
    from wxcopy import pipeline

    seen: list[str] = []
    tbl = tmp_path / "transcripts.xlsx"
    monkeypatch.setattr(pipeline, "stage_source", lambda *a, **k: tbl)
    monkeypatch.setattr(pipeline, "stage_rewrite", lambda *a, **k: tbl)
    monkeypatch.setattr(pipeline, "stage_tts", lambda *a, **k: tmp_path / "voice")

    def fake_video(*a, **k):
        seen.append("video")
        return tmp_path / "video"

    monkeypatch.setattr(pipeline, "stage_video", fake_video)

    from wxcopy.settings import load_settings

    st = load_settings()
    st.paths.workdir = str(tmp_path)
    out = pipeline.run_pipeline(st, workdir=str(tmp_path), source={"kind": "text", "text": "x"}, bgm="bgm.mp3")
    assert seen == ["video"]
    assert "video_dir" in out


# ---------------- 前端控件 ----------------

HTML = Path(__file__).resolve().parents[1] / "src/wxcopy/web/static/index.html"


@pytest.mark.skipif(not HTML.is_file(), reason="静态页面不在源码树里")
def test_console_html_has_new_controls():
    html = HTML.read_text(encoding="utf-8")
    for need in [
        'id="vBgmOn"',
        'id="vBgm"',
        'id="vBgmVol"',
        'id="cEnabled"',
        'id="cStyle"',
        'id="cAccentHex"',
        'id="cTitleCol"',
        'id="btnCoverPrev"',
        'id="pEnabled"',
        'id="pPlatforms"',
        'id="pBundle"',
    ]:
        assert need in html, f"页面缺少控件 {need}"


@pytest.mark.skipif(not HTML.is_file(), reason="静态页面不在源码树里")
def test_console_html_every_dollar_id_exists():
    """JS 里 $('xxx') 引用的 id，HTML 里必须真有——拼错一个 id 就是静默失效。"""
    import re

    html = HTML.read_text(encoding="utf-8")
    js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    used = set(re.findall(r"\$\('([A-Za-z0-9_]+)'\)", js))
    defined = set(re.findall(r'id="([A-Za-z0-9_]+)"', html))
    missing = sorted(used - defined)
    assert not missing, f"页面缺少这些 id：{missing}"
