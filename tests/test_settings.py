"""settings.py 的配置解析测试。"""
from __future__ import annotations

import pytest

from wxcopy.settings import (
    OcrCfg,
    VideoCfg,
    _deep_merge,
    _take,
    expand_env,
    load_settings,
)

# ---------------- 环境变量插值 ----------------

def test_expand_env_replaces_var(monkeypatch):
    monkeypatch.setenv("WX_TEST_KEY", "secret")
    assert expand_env("url=${WX_TEST_KEY}") == "url=secret"


def test_expand_env_default_when_unset(monkeypatch):
    monkeypatch.delenv("WX_MISSING", raising=False)
    assert expand_env("v=${WX_MISSING:-fallback}") == "v=fallback"


def test_expand_env_empty_when_unset_and_no_default(monkeypatch):
    monkeypatch.delenv("WX_MISSING", raising=False)
    assert expand_env("v=${WX_MISSING}") == "v="


def test_expand_env_recurses_into_containers(monkeypatch):
    monkeypatch.setenv("WX_TEST_KEY", "s")
    got = expand_env({"a": ["${WX_TEST_KEY}", 1], "b": {"c": "${WX_TEST_KEY}"}})
    assert got == {"a": ["s", 1], "b": {"c": "s"}}


# ---------------- _take（字典 -> dataclass） ----------------

def test_take_ignores_unknown_keys():
    cfg = _take({"bg_color": "#000000", "不存在的键": 1}, VideoCfg)
    assert cfg.bg_color == "#000000"


def test_take_keeps_defaults_for_missing():
    cfg = _take({}, OcrCfg)
    assert cfg.provider == "auto"
    assert cfg.min_score == 0.5


# ---------------- deep merge ----------------

def test_deep_merge_nested():
    base = {"a": {"b": 1, "c": 2}, "z": 0}
    over = {"a": {"c": 9}}
    assert _deep_merge(base, over) == {"a": {"b": 1, "c": 9}, "z": 0}


def test_deep_merge_overrides_scalar_and_list():
    """list 是整体替换，不做追加——配置项语义如此。"""
    base = {"k": [1, 2]}
    assert _deep_merge(base, {"k": [3]}) == {"k": [3]}


# ---------------- load_settings ----------------

def test_load_settings_has_sane_defaults():
    st = load_settings()
    assert st.llm.provider == "ollama"
    assert st.tts.provider == "edge"
    assert st.ocr.provider == "auto"
    assert st.video.style in ("classic", "big")


def test_load_settings_merges_explicit_file(tmp_path):
    p = tmp_path / "my.yaml"
    p.write_text(
        "llm:\n  provider: deepseek\n  model: deepseek-chat\n"
        "video:\n  style: big\n",
        encoding="utf-8",
    )
    st = load_settings(str(p))
    assert st.llm.provider == "deepseek"
    assert st.llm.model == "deepseek-chat"
    # 没覆盖的子项要保留默认，而不是被清空
    assert st.llm.temperature > 0
    assert st.video.style == "big"


def test_load_settings_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_settings(str(tmp_path / "nope.yaml"))


def test_provider_falls_back_to_openai_compatible():
    """没预设的厂商名按 OpenAI 兼容处理，这样加厂商不用改代码。"""
    st = load_settings()
    pc = st.provider("某未知厂商")
    assert pc.type == "openai"
