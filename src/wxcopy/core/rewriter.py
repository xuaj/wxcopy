"""文案仿写。

- 输入：Excel/CSV 里的文案列
- 处理：套用可替换的 prompt 模板，交给任意 LLM 后端（本地 Ollama 或云厂商）
- 输出：回写新列，支持断点续跑与并发

内置模板见 `wxcopy/prompts/rewrite/*.md`，也可用 `--prompt-file` 指定自己的 md。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..logging_setup import get_logger
from ..settings import RewriteCfg
from .llm import BaseLLM

log = get_logger()

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts" / "rewrite"

BUILTIN: dict[str, str] = {
    "default": (
        "你是资深短视频文案策划，擅长把一条爆款文案改写成结构相同、表达全新、"
        "更适合口播的原创文案。\n"
        "要求：\n"
        "1. 保留原文案的卖点、逻辑与转化路径，但全部换用自己的话重新表达；\n"
        "2. 禁止照抄原句，禁止连续复述超过 8 个原字；\n"
        "3. 开头必须有一个能在 3 秒内抓住注意力的钩子（提问/反常识/痛点/数字）；\n"
        "4. 语言口语化，短句为主，适合朗读，避免书面语和长定语从句；\n"
        "5. 结尾给出明确的行动引导；\n"
        "6. 只输出仿写后的正文，不要解释、不要加标题、不要加 Markdown 标记。"
    ),
    "爆款口播": (
        "你是短视频爆款口播文案写手。\n"
        "仿写结构固定为四段：钩子（1 句抓人）→ 痛点放大（2-3 句）→ "
        "方案与证据（3-5 句，给具体做法或数字）→ 行动号召（1 句）。\n"
        "要求：全程口语、节奏快、每句不超过 20 字；不要空话套话；"
        "不要出现“首先/其次/综上所述”这类书面连接词；"
        "不得照抄原句。只输出正文。"
    ),
    "种草测评": (
        "你是好物推荐类短视频文案写手，语气真实、像朋友安利。\n"
        "仿写时保留原文案推荐的产品与核心卖点，但换成第一人称体验式表达："
        "先说使用场景，再说解决了什么问题，再给 2-3 个具体细节，最后给购买建议。\n"
        "不要夸张到失真，不要堆砌形容词。不得照抄原句。只输出正文。"
    ),
    "知识科普": (
        "你是知识类短视频文案作者，擅长把复杂概念讲得通俗。\n"
        "仿写结构：抛出一个反常识结论 → 用类比或生活例子解释原理 → "
        "给出 1-2 条可立刻用的建议 → 引导互动。\n"
        "语言准确但通俗，避免术语堆砌。不得照抄原句。只输出正文。"
    ),
    "情绪共鸣": (
        "你擅长写引发共鸣的情感类短视频文案。\n"
        "仿写时抓住原文案的情绪内核（焦虑/委屈/期待/成就感等），"
        "用具体的生活场景和细节把它写出来，多用“你”来对话读者。\n"
        "避免空喊口号，避免说教。不得照抄原句。只输出正文。"
    ),
}

FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*\n?|\s*```\s*$", re.MULTILINE)
LEAD_RE = re.compile(r"^\s*(仿写文案|改写|输出|正文|文案)[:：]\s*")
HASHTAG_RE = re.compile(r"#[^\s#]+")


def list_prompts() -> list[str]:
    names = set(BUILTIN)
    if PROMPT_DIR.exists():
        names |= {p.stem for p in PROMPT_DIR.glob("*.md")}
    return sorted(names)


def load_prompt(name_or_path: str) -> str:
    """按名字或文件路径加载 system prompt。"""
    if not name_or_path:
        return BUILTIN["default"]
    p = Path(name_or_path)
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    if (PROMPT_DIR / f"{name_or_path}.md").exists():
        return (PROMPT_DIR / f"{name_or_path}.md").read_text(encoding="utf-8").strip()
    if name_or_path in BUILTIN:
        return BUILTIN[name_or_path]
    raise KeyError(f"找不到 prompt 模板 {name_or_path}，可用：{', '.join(list_prompts())}")


@dataclass
class RewriteStyle:
    """影响 user 侧指令的可调项。"""
    length: str = "与原文案相当"      # 如 "150字左右" / "更短更精炼"
    tone: str = ""                    # 如 "接地气" / "专业克制"
    audience: str = ""                # 如 "餐饮小店老板"
    cta: str = ""                     # 自定义行动号召
    keep_hashtags: bool = False
    extra: str = ""
    keywords: list[str] = field(default_factory=list)


def build_user(text: str, style: RewriteStyle) -> str:
    lines = [f"【原始文案】\n{text.strip()}", "", "【仿写要求】"]
    lines.append(f"- 篇幅：{style.length}")
    if style.tone:
        lines.append(f"- 语气：{style.tone}")
    if style.audience:
        lines.append(f"- 目标受众：{style.audience}")
    if style.keywords:
        lines.append(f"- 必须自然包含这些关键词：{'、'.join(style.keywords)}")
    if style.cta:
        lines.append(f"- 结尾行动号召：{style.cta}")
    if style.extra:
        lines.append(f"- 其他：{style.extra}")
    lines.append("- 保留原意与卖点，但全部用自己的话重写，禁止照抄原句")
    if style.keep_hashtags:
        lines.append("- 保留原文案末尾的 #标签")
    else:
        lines.append("- 不要输出 #标签")
    lines.append("")
    lines.append("请直接输出仿写后的文案正文：")
    return "\n".join(lines)


def clean_output(raw: str, keep_hashtags: bool = False) -> str:
    """去掉代码块围栏、前缀标签、多余空白。"""
    out = FENCE_RE.sub("", raw or "").strip()
    out = LEAD_RE.sub("", out).strip()
    out = out.strip('"“” \n\t')
    if not keep_hashtags:
        out = HASHTAG_RE.sub("", out)
        out = re.sub(r"[ \t]{2,}", " ", out)
        out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


class Rewriter:
    def __init__(self, llm: BaseLLM, cfg: RewriteCfg, style: RewriteStyle | None = None,
                 system_prompt: str | None = None):
        self.llm = llm
        self.cfg = cfg
        self.style = style or RewriteStyle(keep_hashtags=cfg.keep_hashtags)
        self.system = system_prompt or BUILTIN["default"]

    def rewrite_one(self, text: str) -> str:
        text = (text or "").strip()
        if len(text) < (self.cfg.min_chars or 0):
            return ""
        if len(text) > self.cfg.max_chars:
            text = text[: self.cfg.max_chars]
        raw = self.llm.complete(self.system, build_user(text, self.style))
        return clean_output(raw, self.style.keep_hashtags)

    def rewrite_many(
        self,
        items: list[tuple[Any, str]],
        on_done: Callable[[Any, str], None] | None = None,
        skip: Callable[[Any], bool] | None = None,
    ) -> dict[Any, str]:
        """items: [(key, text)]。返回 {key: 仿写结果}。"""
        todo = [(k, t) for k, t in items if not (skip and skip(k))]
        log.info("待仿写 %d 条（跳过 %d 条已完成）", len(todo), len(items) - len(todo))
        results: dict[Any, str] = {}
        total = len(todo)
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, self.cfg.concurrency)) as pool:
            for (key, _), out in zip(todo, pool.map(lambda kv: self._safe(kv[1]), todo)):
                done += 1
                results[key] = out
                if on_done:
                    on_done(key, out)
                if done % 5 == 0 or done == total:
                    log.info("仿写进度 %d/%d", done, total)
        return results

    def _safe(self, text: str) -> str:
        try:
            return self.rewrite_one(text)
        except Exception as e:  # noqa: BLE001
            log.error("仿写失败：%s", str(e)[:160])
            return ""
