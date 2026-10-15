"""成片 -> 发布素材包（标题 / 简介 / 话题 + 清单）。

短视频平台（视频号、抖音）**没有对普通开发者开放的「代用户发布视频」接口**，
本模块不做、也做不到自动发布。它负责的是「上传前的最后一步」：

    成片 + 封面 + 标题候选 + 简介 + 话题  ->  一个能直接照着发的素材包和清单

生成文案用 LLM（默认走本地 Ollama，免费）；没模型也能跑，会退化成规则截取，
保证清单永远出得来。
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..logging_setup import get_logger
from ..settings import PublishCfg
from .llm import BaseLLM
from .media import probe_duration

log = get_logger()

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts" / "publish"

TITLE_MAX = 30  # 保护性上限：提示词里要求 ≤20，这里只在模型放飞时兜底截断
DESC_MAX = 100
TOPIC_MAX = 8

BUILTIN: dict[str, str] = {
    "default": (
        "你是短视频运营，负责给成片配「标题 + 简介 + 话题」。\n"
        "你要输出严格的 JSON，不要任何解释文字，键名固定为 titles / desc / topics。\n"
        "\n"
        "要求：\n"
        "1. titles：5 个候选标题，每个不超过 20 字（必须写完整的词，不许截断）；风格要拉开——\n"
        "   提问钩子、反常识、数字清单、痛点直击、利益承诺各来一条；\n"
        "   不要用「震惊」「速看」这类平台不喜欢的词；不要标题党到与内容不符。\n"
        "2. desc：一段不超过 100 字的简介，口语化，补一句行动引导\n"
        "   （如「需要的话评论区扣 1」「链接在评论区」），不要重复标题。\n"
        "3. topics：6~8 个话题标签，每个以 # 开头，不带空格；\n"
        "   兼顾大流量泛标签和精准垂类标签，不要堆无关热词。\n"
        "\n"
        "只输出 JSON，示例：\n"
        '{"titles":["...","..."],"desc":"...","topics":["#xxx","#yyy"]}'
    ),
}

PUNCT_SPLIT = re.compile(r"[。！？!?；;\n]")
_SPACES = re.compile(r"\s+")
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class PublishError(RuntimeError):
    pass


@dataclass
class PublishCopy:
    """一条成片的发布文案。"""

    titles: list[str] = field(default_factory=list)
    desc: str = ""
    topics: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        """推荐标题（第一个候选）。"""
        return self.titles[0] if self.titles else ""

    def as_text(self, name: str = "") -> str:
        """排版成人能直接复制粘贴的样子。"""
        lines: list[str] = []
        if name:
            lines.append(f"# {name}")
            lines.append("")
        if self.titles:
            lines.append("【标题候选】")
            lines += [f"{i}. {t}" for i, t in enumerate(self.titles, 1)]
            lines.append("")
        if self.desc:
            lines.append("【简介】")
            lines.append(self.desc)
            lines.append("")
        if self.topics:
            lines.append("【话题】")
            lines.append(" ".join(self.topics))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- 提示词


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


def build_user(text: str, cfg: PublishCfg, keywords: Iterable[str] = (), extra: str = "") -> str:
    lines = [f"【成片口播文案】\n{text.strip()}", "", "【生成要求】"]
    lines.append(f"- 候选标题 {max(1, cfg.titles)} 个，每个不超过 20 字，写完整的词")
    lines.append(f"- 简介不超过 {cfg.desc_max} 字")
    lines.append(f"- 话题标签 {max(1, cfg.topics)} 个，以 # 开头，标签内部不能有空格")
    kws = [str(k).strip() for k in keywords if str(k).strip()]
    if kws:
        lines.append(f"- 话题里必须包含：{'、'.join(kws)}")
    if extra:
        lines.append(f"- {extra}")
    lines.append("- 只输出 JSON")
    return "\n".join(lines)


# ---------------------------------------------------------------- 解析
# 小模型经常在 JSON 外面裹 ```json 围栏、或者先来一句「好的，这是：」，
# 所以这里做多档降级，而不是一上来就 strict json.loads。


def _strip_noise(raw: str) -> str:
    s = (raw or "").strip()
    m = _FENCE.search(s)
    if m:
        return m.group(1).strip()
    return s


def _first_json_object(s: str) -> str | None:
    """抓出第一个完整的大括号对象（模型爱在 JSON 前后加话）。"""
    i = s.find("{")
    if i < 0:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(i, len(s)):
        ch = s[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[i : j + 1]
    return None


def _as_list(v: Any) -> list[str]:
    if isinstance(v, str):
        # 「#a #b」或「a、b」都收
        parts = re.split(r"[、,，\s]+", v.strip())
        return [p for p in parts if p]
    if isinstance(v, Iterable):
        return [str(x).strip() for x in v if str(x).strip()]
    return []


def _norm_topics(items: list[str], limit: int) -> list[str]:
    """话题标签不能含空格——平台会把空格后面的字当成正文。"""
    out: list[str] = []
    for t in items:
        t = _SPACES.sub("", t.strip().lstrip("#").strip())
        if not t:
            continue
        t = "#" + t
        if t not in out:
            out.append(t)
        if len(out) >= max(1, limit):
            break
    return out


def parse_copy(raw: str, cfg: PublishCfg | None = None) -> PublishCopy:
    """把模型输出解析成 PublishCopy；解析不出来就返回空对象（调用方降级）。"""
    cfg = cfg or PublishCfg()
    s = _strip_noise(raw)
    blob = _first_json_object(s)
    if not blob:
        return PublishCopy()
    try:
        data = json.loads(blob)
    except ValueError:
        return PublishCopy()
    if not isinstance(data, dict):
        return PublishCopy()

    titles = [t.strip() for t in _as_list(data.get("titles")) if t.strip()]
    titles = [t[:TITLE_MAX] for t in titles][: max(1, cfg.titles)]
    desc = str(data.get("desc", "") or "").strip()
    if cfg.desc_max and len(desc) > cfg.desc_max:
        desc = desc[: cfg.desc_max].rstrip("，。、；") + "…"
    topics = _norm_topics(_as_list(data.get("topics")), cfg.topics)
    return PublishCopy(titles=titles, desc=desc, topics=topics)


# ---------------------------------------------------------------- 降级


def fallback_copy(text: str, keywords: Iterable[str] = (), cfg: PublishCfg | None = None) -> PublishCopy:
    """没有 LLM（或模型掉链子）时的规则兜底：清单永远出得来。"""
    cfg = cfg or PublishCfg()
    body = (text or "").strip()
    first = PUNCT_SPLIT.split(body)[0].strip() if body else ""
    title = first[:TITLE_MAX] or (body[:TITLE_MAX] if body else "")
    desc = body[: cfg.desc_max] if cfg.desc_max else body
    topics = _norm_topics([str(k) for k in keywords], cfg.topics)
    return PublishCopy(titles=[title] if title else [], desc=desc, topics=topics)


def generate_copy(
    llm: BaseLLM | None,
    text: str,
    cfg: PublishCfg | None = None,
    keywords: Iterable[str] = (),
    system: str | None = None,
) -> PublishCopy:
    """生成一条发布文案；失败自动退回规则兜底，不抛异常。"""
    cfg = cfg or PublishCfg()
    if llm is None or not (text or "").strip():
        return fallback_copy(text, keywords, cfg)
    try:
        raw = llm.complete(system or BUILTIN["default"], build_user(text, cfg, keywords))
    except Exception as e:  # noqa: BLE001
        log.warning("发布文案生成失败，改用规则兜底：%s", str(e)[:120])
        return fallback_copy(text, keywords, cfg)
    got = parse_copy(raw, cfg)
    if not got.titles and not got.desc:
        log.warning("模型输出无法解析，改用规则兜底")
        return fallback_copy(text, keywords, cfg)
    return got


def generate_many(
    items: list[tuple[str, str]],
    llm: BaseLLM | None,
    cfg: PublishCfg | None = None,
    keywords: Iterable[str] = (),
    system: str | None = None,
) -> dict[str, PublishCopy]:
    """items: [(name, 文案)] -> {name: PublishCopy}。"""
    cfg = cfg or PublishCfg()
    out: dict[str, PublishCopy] = {}
    total = len(items)
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, cfg.concurrency)) as pool:
        futures = [(name, pool.submit(generate_copy, llm, text, cfg, keywords, system)) for name, text in items]
        for name, fu in futures:
            try:
                out[name] = fu.result()
            except Exception as e:  # noqa: BLE001
                log.error("发布文案生成失败 %s：%s", name, str(e)[:120])
                out[name] = PublishCopy()
            done += 1
            if done % 5 == 0 or done == total:
                log.info("发布文案进度 %d/%d", done, total)
    return out


# ---------------------------------------------------------------- 清单与素材包

PLAN_COLUMNS = [
    "序号",
    "文件名",
    "推荐标题",
    "标题候选",
    "简介",
    "话题",
    "视频路径",
    "封面路径",
    "时长(秒)",
    "平台",
    "状态",
    "计划发布时间",
]


def plan_row(
    index: int,
    name: str,
    copy: PublishCopy,
    video: str = "",
    cover: str = "",
    platforms: str = "",
) -> dict:
    dur = ""
    if video and Path(video).is_file():
        d = probe_duration(Path(video))
        if d and d > 0:
            dur = round(d, 1)
    return {
        "序号": index,
        "文件名": name,
        "推荐标题": copy.title,
        "标题候选": " | ".join(copy.titles),
        "简介": copy.desc,
        "话题": " ".join(copy.topics),
        "视频路径": video,
        "封面路径": cover,
        "时长(秒)": dur,
        "平台": platforms,
        "状态": "待发布",
        "计划发布时间": "",
    }


def save_copy(out_dir: Path, name: str, copy: PublishCopy) -> Path:
    """落盘发布文案：`.txt` 给人复制粘贴，`.copy.json` 给下次重跑时还原清单。"""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.txt").write_text(copy.as_text(name), encoding="utf-8")
    (d / f"{name}.copy.json").write_text(json.dumps(asdict(copy), ensure_ascii=False, indent=2), encoding="utf-8")
    return d / f"{name}.txt"


def load_copy(out_dir: Path, name: str) -> PublishCopy | None:
    """读回上次生成的发布文案；没有或损坏返回 None。"""
    p = Path(out_dir) / f"{name}.copy.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return PublishCopy(
        titles=[str(t) for t in _as_list(data.get("titles"))],
        desc=str(data.get("desc", "") or ""),
        topics=[str(t) for t in _as_list(data.get("topics"))],
    )


def write_bundle(
    out_dir: Path,
    name: str,
    copy: PublishCopy,
    video: str = "",
    cover: str = "",
) -> Path:
    """把「视频 + 封面 + 发布文案」复制进一个文件夹，方便整体拖拽上传。"""
    import shutil

    d = Path(out_dir) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "publish.txt").write_text(copy.as_text(name), encoding="utf-8")
    for src, suffix in ((video, "mp4"), (cover, "png")):
        if src and Path(src).is_file():
            dst = d / f"{name}.{suffix}"
            try:
                shutil.copy2(Path(src), dst)
            except OSError as e:  # noqa: BLE001
                log.warning("复制%s失败 %s：%s", suffix, name, str(e)[:80])
    return d
