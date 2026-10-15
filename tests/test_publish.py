"""publish 模块的纯逻辑测试（不联网、不调 ffmpeg）。

重点在解析：小模型输出 JSON 时经常裹 ```json 围栏、或前后加废话，
一上来就 json.loads 会大面积返工，所以这里把每种脏输入都钉死。
"""

from __future__ import annotations

import pytest

from wxcopy.core.publish import (
    BUILTIN,
    PLAN_COLUMNS,
    TITLE_MAX,
    PublishCopy,
    PublishError,
    build_user,
    fallback_copy,
    generate_copy,
    list_prompts,
    load_copy,
    load_prompt,
    parse_copy,
    plan_row,
    save_copy,
    write_bundle,
)
from wxcopy.settings import PublishCfg


def _cfg(**over) -> PublishCfg:
    return PublishCfg(**over)


# ---------------- 解析 ----------------


def test_parse_plain_json():
    raw = '{"titles":["甲","乙"],"desc":"一段简介","topics":["#a","#b"]}'
    got = parse_copy(raw, _cfg())
    assert got.titles == ["甲", "乙"]
    assert got.desc == "一段简介"
    assert got.topics == ["#a", "#b"]
    assert got.title == "甲"


def test_parse_fenced_json():
    """模型最爱裹一层 ```json 围栏。"""
    raw = '好的：\n```json\n{"titles":["甲"],"desc":"d","topics":["#a"]}\n```'
    got = parse_copy(raw, _cfg())
    assert got.titles == ["甲"]
    assert got.desc == "d"


def test_parse_json_with_chatter():
    raw = '这是你要的结果\n{"titles":["甲"],"desc":"d","topics":["#a"]}\n希望有帮助！'
    got = parse_copy(raw, _cfg())
    assert got.titles == ["甲"]


def test_parse_nested_braces():
    """第一个完整对象要能正确收尾，不能被后面的大括号带跑。"""
    raw = '{"titles":["甲"],"desc":"d","topics":["#a"]} 另：{"x":1}'
    got = parse_copy(raw, _cfg())
    assert got.titles == ["甲"]


@pytest.mark.parametrize("bad", ["", "   ", "不是 JSON", '{"titles":', "[1,2,3]", "null"])
def test_parse_garbage_returns_empty(bad):
    assert parse_copy(bad, _cfg()) == PublishCopy()


def test_parse_limits_title_count():
    raw = '{"titles":["1","2","3","4","5","6","7"],"desc":"d","topics":[]}'
    assert len(parse_copy(raw, _cfg(titles=3)).titles) == 3


def test_parse_truncates_overlong_title():
    """标题超上限要截，但不能像早期版本那样把「视频」切成「视」——这里只是兜底，
    正常标题由提示词控制在 20 字内，30 字才轮到代码动手。"""
    long = "字" * 50
    got = parse_copy(f'{{"titles":["{long}"],"desc":"","topics":[]}}', _cfg())
    assert len(got.titles[0]) == TITLE_MAX


def test_parse_topics_get_hash_and_drop_spaces():
    """话题里的空格会让平台把后面的字当正文，必须清掉。"""
    raw = '{"titles":["甲"],"desc":"d","topics":["开源","AI 仿写","#带井号"," 空 "]}'
    got = parse_copy(raw, _cfg(topics=5))
    assert got.topics == ["#开源", "#AI仿写", "#带井号", "#空"]


def test_parse_topics_dedupe_and_limit():
    raw = '{"titles":[],"desc":"","topics":["#a","a","#b","#c","#d"]}'
    got = parse_copy(raw, _cfg(topics=3))
    assert got.topics == ["#a", "#b", "#c"]


def test_parse_desc_truncated_with_ellipsis():
    got = parse_copy('{"titles":[],"desc":"' + "字" * 200 + '","topics":[]}', _cfg(desc_max=20))
    assert len(got.desc) == 21
    assert got.desc.endswith("…")


def test_parse_topics_as_string():
    """模型偶尔把 topics 输出成一整串。"""
    got = parse_copy('{"titles":[],"desc":"","topics":"#a #b"}', _cfg())
    assert got.topics == ["#a", "#b"]


# ---------------- 降级 ----------------


def test_fallback_takes_first_sentence():
    got = fallback_copy("第一句内容。第二句内容。", [], _cfg())
    assert got.title == "第一句内容"
    assert got.desc.startswith("第一句内容")


def test_fallback_uses_keywords_as_topics():
    got = fallback_copy("随便一段话", ["开源", "视频号"], _cfg())
    assert got.topics == ["#开源", "#视频号"]


def test_fallback_empty_text():
    assert fallback_copy("", [], _cfg()) == PublishCopy()


def test_generate_without_llm_falls_back():
    got = generate_copy(None, "甲。乙。", _cfg())
    assert got.title == "甲"


class _BoomLLM:
    name = "boom"

    def complete(self, system: str, user: str) -> str:
        raise RuntimeError("模型挂了")


class _GarbageLLM:
    name = "garbage"

    def complete(self, system: str, user: str) -> str:
        return "我不知道你在说什么"


def test_generate_falls_back_when_llm_raises():
    got = generate_copy(_BoomLLM(), "甲。乙。", _cfg())  # type: ignore[arg-type]
    assert got.title == "甲"


def test_generate_falls_back_on_unparsable_output():
    got = generate_copy(_GarbageLLM(), "甲。乙。", _cfg())  # type: ignore[arg-type]
    assert got.title == "甲"


class _OkLLM:
    name = "ok"

    def __init__(self, out: str):
        self.out = out
        self.seen: tuple[str, str] | None = None

    def complete(self, system: str, user: str) -> str:
        self.seen = (system, user)
        return self.out


def test_generate_uses_llm_output_and_passes_keywords():
    llm = _OkLLM('{"titles":["好标题"],"desc":"简介","topics":["#开源"]}')
    got = generate_copy(llm, "正文", _cfg(), keywords=["开源"])  # type: ignore[arg-type]
    assert got.title == "好标题"
    assert llm.seen is not None and "开源" in llm.seen[1], "关键词要进提示词，否则 --keyword 形同虚设"


# ---------------- 排版 ----------------


def test_as_text_contains_all_sections():
    c = PublishCopy(titles=["甲", "乙"], desc="简介", topics=["#a"])
    t = c.as_text("名")
    assert "【标题候选】" in t and "1. 甲" in t
    assert "【简介】" in t and "简介" in t
    assert "【话题】" in t and "#a" in t
    assert t.startswith("# 名")


def test_as_text_tolerates_empty():
    assert PublishCopy().as_text() == "\n"


# ---------------- 落盘与读回 ----------------


def test_save_and_load_copy_roundtrip(tmp_path):
    c = PublishCopy(titles=["甲"], desc="简介", topics=["#a"])
    save_copy(tmp_path, "n1", c)
    assert (tmp_path / "n1.txt").is_file()
    assert load_copy(tmp_path, "n1") == c


def test_load_copy_missing(tmp_path):
    assert load_copy(tmp_path, "nope") is None


def test_load_copy_corrupt(tmp_path):
    (tmp_path / "n1.copy.json").write_text("{bad", encoding="utf-8")
    assert load_copy(tmp_path, "n1") is None


# ---------------- 清单与素材包 ----------------


def test_plan_row_basic():
    c = PublishCopy(titles=["甲", "乙"], desc="简介", topics=["#a", "#b"])
    row = plan_row(1, "n1", c, platforms="视频号")
    assert row["序号"] == 1
    assert row["推荐标题"] == "甲"
    assert row["标题候选"] == "甲 | 乙"
    assert row["话题"] == "#a #b"
    assert row["状态"] == "待发布"
    assert row["时长(秒)"] == "", "没有视频就别乱填时长"
    assert set(row) == set(PLAN_COLUMNS)


def test_plan_row_probes_duration(tmp_path):
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"fake")
    row = plan_row(1, "n1", PublishCopy(titles=["甲"]), video=str(vid))
    # 假文件探测不到时长，必须是空而不是 0/NaN
    assert row["时长(秒)"] in ("", 0.0)


def test_write_bundle_copies_assets(tmp_path):
    vid = tmp_path / "v.mp4"
    cov = tmp_path / "c.png"
    vid.write_bytes(b"video")
    cov.write_bytes(b"cover")
    d = write_bundle(tmp_path / "out", "n1", PublishCopy(titles=["甲"]), video=str(vid), cover=str(cov))
    assert (d / "publish.txt").is_file()
    assert (d / "n1.mp4").is_file()
    assert (d / "n1.png").is_file()


def test_write_bundle_without_assets(tmp_path):
    d = write_bundle(tmp_path / "out", "n1", PublishCopy(titles=["甲"]))
    assert (d / "publish.txt").is_file()
    assert not (d / "n1.mp4").exists()


# ---------------- 提示词 ----------------


def test_load_prompt_default():
    assert load_prompt("") == BUILTIN["default"]
    assert load_prompt("default") == BUILTIN["default"]
    assert "default" in list_prompts()


def test_load_prompt_unknown_raises():
    with pytest.raises(KeyError):
        load_prompt("不存在的模板")


def test_load_prompt_from_file(tmp_path):
    p = tmp_path / "my.md"
    p.write_text("自定义提示词", encoding="utf-8")
    assert load_prompt(str(p)) == "自定义提示词"


def test_build_user_mentions_limits():
    u = build_user("正文", _cfg(titles=3, desc_max=50, topics=4))
    assert "3 个" in u and "50 字" in u and "4 个" in u


def test_publish_error_is_runtime_error():
    assert issubclass(PublishError, RuntimeError)
