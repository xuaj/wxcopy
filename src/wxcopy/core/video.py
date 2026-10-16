"""仿写文案 -> 口播视频。

流程：配音音频（时长） + 用户选的背景（图片/视频/纯色） + 烧录字幕 -> mp4

- 背景用 scale+cover 裁剪铺满目标分辨率（默认 1080x1920 竖屏，适配视频号/抖音）
- 字幕走 ASS（libass），中文渲染比 drawtext 更稳，支持描边与自动换行
- 时间轴优先用配音旁的逐字时间戳（``<name>.cues.json``，TTS 合成时生成），
  没有时才退回按字数占全文比例分配——后者在长文案下会明显跑偏

两套字幕样式：
- ``classic``：底部居中整句小字，一行十几个字，适合信息型口播
- ``big``    ：中上部超大粗体短句，每行 2~4 字、隔行染霓虹绿，复刻主流爆款口播号的字幕
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..logging_setup import get_logger
from .media import get_ffmpeg, probe_duration

log = get_logger()

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".m4v"}

PRESETS = {
    "竖屏 1080x1920": (1080, 1920),
    "竖屏 720x1280": (720, 1280),
    "横屏 1920x1080": (1920, 1080),
    "方形 1080x1080": (1080, 1080),
}

DEFAULT_FONT = "Microsoft YaHei"
# big 样式默认用更粗的黑体：爆款口播号的字幕笔画都很饱满，雅黑 Bold 偏细
BIG_FONT = "SimHei"
_FONT_FILES = {"SimHei": "simhei.ttf", "Microsoft YaHei": "msyh.ttc"}

# big 样式的字号 / 位置按画面宽度比例自适应（比例来自对参考爆款视频的实测采样）
BIG_FONT_RATIO = 0.147  # 字号 ≈ 画面宽 × 0.147（1080 → 159）
BIG_LINE_SPACING = 1.5  # 行距 = 字号 × 1.5
BIG_TOP_RATIO = 0.16  # 首行顶部距画面顶部
BIG_CHARS_PER_LINE = 4  # 每行最多几个字
BIG_LINES_PER_SCREEN = 3  # 每屏最多几行

# auto 高亮模式：命中这些模式/词就染成高亮色
AUTO_HIGHLIGHT_PATTERNS = [
    r"\d+(?:\.\d+)?\s*[%％]",  # 百分比
    r"\d+(?:\.\d+)?\s*(?:万|亿|千)?\s*(?:元|块|倍|个|天|年|月|日|次|人|家|平|单|款)",  # 数量
]
AUTO_HIGHLIGHT_WORDS = [
    "免费",
    "省钱",
    "省时",
    "省力",
    "省一半",
    "翻倍",
    "翻番",
    "风口",
    "秘诀",
    "内幕",
    "限时",
    "成本",
    "利润",
    "效率",
    "关键",
    "必须",
    "千万",
    "记住",
    "一定",
    "只要",
    "就能",
    "不用",
    "直接",
    "马上",
    "立刻",
    "第一个",
    "最重要",
    "最赚钱",
    "暴利",
    "稳赚",
    "赚钱",
    "红利",
    "机会",
    "趋势",
]


class VideoError(RuntimeError):
    pass


@dataclass
class VideoCfg:
    width: int = 1080
    height: int = 1920
    fps: int = 30

    bg_path: str = ""  # 背景图片或视频；留空用纯色
    bg_color: str = "#1e293b"  # 纯色背景（bg_path 为空时生效）
    fit: str = "cover"  # cover（裁满）| contain（居中留边）

    # ---- 字幕样式 ----
    style: str = "classic"  # classic（底部整句小字）| big（中上部大字 + 关键词高亮）
    subtitle: bool = True
    font: str = "Microsoft YaHei"
    bold: bool = False
    font_size: int = 76
    font_color: str = "&H00FFFFFF"  # ASS 颜色为 &H00BBGGRR
    outline_color: str = "&H00000000"
    outline: float = 3.0
    shadow: float = 1.0
    margin_v: int = 300  # classic：距底部
    chars_per_line: int = 16
    align: int = 2  # 2=底部居中 5=垂直居中

    # ---- big 样式专用（classic 下不生效）----
    big_font: str = ""  # 空 = 自动（优先 SimHei 等粗黑体）
    big_font_size: int = 0  # 0 = 按画面宽度自适应（width × 0.147）
    big_chars_per_line: int = BIG_CHARS_PER_LINE
    big_lines_per_screen: int = BIG_LINES_PER_SCREEN
    big_top_ratio: float = BIG_TOP_RATIO
    big_line_spacing: float = BIG_LINE_SPACING
    highlight_mode: str = "none"  # none | alternate | keywords | auto
    highlight_color: str = "&H0090F000"  # #00F090 霓虹绿（ASS 为 &H00BBGGRR）
    highlight_outline_color: str = "&H00FFFFFF"
    keywords: list[str] = field(default_factory=list)
    subtitle_fade: float = 0.08  # 每条字幕淡入淡出秒数，0=硬切

    # ---- 打字机效果（big 样式下逐字打出）----
    typewriter: bool = False
    type_speed: float = 0.0  # 每字秒数；0 = 按屏时长自动算
    type_hold: float = 0.3  # 整屏打完后停留时间占屏时长的比例
    type_min: float = 0.045  # 自动算出的每字时长下限（秒）

    crf: int = 23
    preset: str = "veryfast"
    audio_bitrate: str = "192k"
    fade_in: float = 0.0
    tail_pad: float = 0.6  # 语音结束后追加的静音秒数

    # ---- BGM（背景音乐）----
    bgm: str = ""  # 背景音乐文件；留空则只有人声
    bgm_volume: float = 0.18  # BGM 相对人声的音量（0~1）；0.18 ≈ 比人声轻 15dB
    bgm_duck: bool = True  # 人声出现时自动压低 BGM（sidechaincompress 闪避）
    bgm_fade_out: float = 1.5  # BGM 结尾淡出秒数；0 = 不淡出
    bgm_duck_ratio: float = 8.0  # 闪避压缩比，越大压得越狠


# 句末标点：优先在这里断
SENT_END = "。！？!?；;\n"
# 句中可断标点：行长不够时退而求其次
SUB_PUNCT = "，,、 　：:—"
# 不允许出现在行首的标点
NO_LEAD = "，,、。！？；：!?;:"


def split_lines(text: str, per_line: int = 16) -> list[str]:
    """断行：先按句末标点切句，长句再按字数切，尽量落在标点上。

    切点会把标点留在前一行，并把下一行开头的标点并回上一行，
    避免出现「，顾客自己下单」这种以标点起头的行（big 样式每行只有 3~4 字时尤其明显）。
    """
    text = (text or "").strip().replace("\r", "")
    if not text:
        return []
    per_line = max(1, int(per_line))

    # 先按自然句切开
    chunks: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in SENT_END:
            chunks.append(buf.strip())
            buf = ""
    if buf.strip():
        chunks.append(buf.strip())
    chunks = [c for c in chunks if c]

    # 长句再按字数二次切
    lines: list[str] = []
    for c in chunks:
        while len(c) > per_line:
            cut = per_line
            # 尽量在逗号/顿号/空格处断，避免把词切碎
            for i in range(per_line, max(per_line // 2, 1), -1):
                if c[i - 1] in SUB_PUNCT:
                    cut = i
                    break
            seg, c = c[:cut].strip(), c[cut:].strip()
            # 下一段以标点开头 -> 并回本行末尾，别让它孤零零起一行
            while c and c[0] in NO_LEAD:
                seg += c[0]
                c = c[1:].lstrip()
            if seg:
                lines.append(seg)
        if c:
            lines.append(c)
    return [ln for ln in lines if ln]


# jieba 默认词典对短视频领域的词切得不好（「小程序」会被切成「小/程序」），补一批
EXTRA_WORDS = [
    "小程序",
    "视频号",
    "公众号",
    "直播带货",
    "私域",
    "引流",
    "获客",
    "复购",
    "转化率",
    "翻台率",
    "扫码点餐",
    "共享经济",
    "共享模式",
    "点单",
    "团购",
]
_extra_added: set[str] = set()


def _load_extra_words(extra=None) -> bool:
    """往 jieba 词典里补领域词（只补一次）。jieba 没装返回 False。"""
    try:
        import jieba
    except ImportError:
        return False
    for w in list(EXTRA_WORDS) + list(extra or []):
        w = (w or "").strip()
        if len(w) >= 2 and w not in _extra_added:
            jieba.add_word(w)
            _extra_added.add(w)
    return True


def _tokenize(text: str, extra_words=None) -> list[str] | None:
    """中文分词；jieba 没装就返回 None（调用方退回按字数切）。"""
    if not _load_extra_words(extra_words):
        return None
    try:
        import jieba

        return [w for w in jieba.lcut(text, cut_all=False) if w.strip()]
    except Exception:  # noqa: BLE001
        return None


_PUNCT_ONLY = re.compile(r"^[^\w\u4e00-\u9fff]+$")


def split_by_words(text: str, per_line: int = 4, extra_words=None) -> list[str]:
    """按词断行：贪心装箱，标点不落行首也不单独成行。

    big 样式每行只有 3~4 字，纯按字数切会把「有没有想过」切成「有没有想 / 过，如果」，
    读起来别扭。按词切能保证「有没有 / 想过，/ 如果」这种语义完整的短句。
    jieba 不可用时自动退回 ``split_lines``。
    """
    text = (text or "").strip().replace("\r", "")
    if not text:
        return []
    per_line = max(1, int(per_line))

    words = _tokenize(text, extra_words)
    if words is None:
        return split_lines(text, per_line)

    # 标点并入前一个词，避免标点单独一行
    merged: list[str] = []
    for w in words:
        if merged and _PUNCT_ONLY.match(w):
            merged[-1] += w
        else:
            merged.append(w)

    # 贪心装箱
    lines: list[str] = []
    cur = ""
    for w in merged:
        if not cur:
            cur = w
        elif len(cur) + len(w) <= per_line:
            cur += w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)

    # 收尾：把单字尾巴并回上一行（能并下的话），避免一个字占一屏
    out: list[str] = []
    for ln in lines:
        if out and len(ln) <= 1 and len(out[-1]) + len(ln) <= per_line:
            out[-1] += ln
        else:
            out.append(ln)
    return [ln for ln in out if ln]


def _alloc_by_weights(weights: list[int], total: float) -> list[tuple[float, float]]:
    if not weights:
        return []
    s = sum(weights) or 1
    out, acc = [], 0.0
    for i, w in enumerate(weights):
        end = total if i == len(weights) - 1 else acc + total * w / s
        out.append((round(acc, 3), round(end, 3)))
        acc = end
    return out


def alloc_timings(lines: list[str], total: float) -> list[tuple[float, float]]:
    """按字数比例给每行分配时间段。"""
    return _alloc_by_weights([max(1, len(ln)) for ln in lines], total)


def pack_screens(lines: list[str], per_screen: int = BIG_LINES_PER_SCREEN) -> list[list[str]]:
    """把行按「每屏 N 行」分组，同屏的行同时出现。"""
    per_screen = max(1, per_screen)
    return [lines[i : i + per_screen] for i in range(0, len(lines), per_screen)]


def alloc_screen_timings(screens: list[list[str]], total: float) -> list[tuple[float, float]]:
    """按每屏的总字数分配时间。"""
    return _alloc_by_weights([max(1, sum(len(ln) for ln in s)) for s in screens], total)


# ---------- 按 TTS 逐字时间戳对齐（优先于按字数均分）----------


def _reading_chars(text: str) -> str:
    """只保留有读音的字符（去掉标点与空白），用来和时间戳做序列对齐。"""
    return "".join(ch for ch in text if ch.isalnum())


def _cue_char_times(cues: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """把词级时间戳展开成字级。

    edge 的 WordBoundary 有时按词给（「视频」「星标」），有时按字给。
    一个词里的几个字平均分这段时间，断行刚好落在词中间时才不会整体提前。
    """
    items = [(w, float(t)) for w, t in cues if _reading_chars(w)]
    out: list[tuple[str, float]] = []
    for i, (w, t) in enumerate(items):
        chars = _reading_chars(w)
        end = items[i + 1][1] if i + 1 < len(items) else t
        step = (end - t) / len(chars) if end > t else 0.0
        out.extend((ch, t + k * step) for k, ch in enumerate(chars))
    return out


def align_timings(
    groups: list[str], cues: list[tuple[str, float]] | None, total: float
) -> list[tuple[float, float]] | None:
    """用 TTS 逐字时间戳给每个「组」分配时间段。

    组可以是 classic 的一行，也可以是 big 的一屏：组的起点 = 该组第一个字的
    真实发声时刻，终点 = 下一组起点（最后一组铺到 total）。

    按字数均分之所以会跑偏，是因为 TTS 在标点处要停顿，字符速率并不均匀；
    长文案下半段能差出好几秒。时间戳缺失或和文案差太多时返回 None，
    调用方回退到按字数均分。
    """
    if not groups or not cues:
        return None
    cue_chars = _cue_char_times(cues)
    if not cue_chars:
        return None

    group_chars = [_reading_chars(g) for g in groups]
    flat = "".join(group_chars)
    if not flat:
        return None

    cue_text = "".join(ch for ch, _ in cue_chars)
    if flat == cue_text:
        pos = list(range(len(flat)))
    else:
        import difflib

        sm = difflib.SequenceMatcher(None, flat, cue_text, autojunk=False)
        ratio = sm.ratio()
        if ratio < 0.6:
            log.warning("逐字时间戳与文案差异过大（相似度 %.2f），改回按字数分配字幕时间", ratio)
            return None
        pos = [-1] * len(flat)
        for blk in sm.get_matching_blocks():
            for k in range(blk.size):
                pos[blk.a + k] = blk.b + k

    # 每个组的起点 = 组内第一个「能对上时间」的字
    starts: list[float] = []
    last = 0.0
    n = 0
    for g in group_chars:
        hit = next((pos[p] for p in range(n, n + len(g)) if pos[p] >= 0), -1)
        st = cue_chars[hit][1] if hit >= 0 else last
        st = max(float(st), last)  # 保证单调不减
        starts.append(st)
        last = st
        n += len(g)

    span = max(float(total), starts[-1] + 0.001)
    out: list[tuple[float, float]] = []
    for k, st in enumerate(starts):
        en = starts[k + 1] if k + 1 < len(starts) else span
        if en <= st:
            en = st + 0.05
        out.append((round(st, 3), round(min(en, span), 3)))
    return out


# ---------- 关键词高亮 ----------
def _find_highlight_spans(line: str, cfg: VideoCfg, partial: bool = False) -> list[tuple[int, int]]:
    """返回该行里需要染高亮色的 [起, 止) 区间，已去重叠。

    ``partial=True`` 额外把「行尾刚好是某个关键词的前缀」也算高亮——
    打字机效果下关键词是逐字打出来的，打到一半就该是绿的，而不是打完整词才突然变绿。
    """
    mode = (cfg.highlight_mode or "none").lower()
    if mode in ("none", "", "alternate"):
        return []

    spans: list[tuple[int, int]] = []
    if mode == "keywords":
        for kw in set(cfg.keywords or []):
            kw = (kw or "").strip()
            if not kw:
                continue
            start = 0
            while True:
                i = line.find(kw, start)
                if i < 0:
                    break
                spans.append((i, i + len(kw)))
                start = i + len(kw)
            if partial:
                # 打到一半：取行尾与关键词前缀的最大重合部分，让关键词逐字变绿
                best = 0
                for m in range(min(len(line), len(kw)), 0, -1):
                    if line[-m:] == kw[:m]:
                        best = m
                        break
                if best:
                    spans.append((len(line) - best, len(line)))
    elif mode == "auto":
        for pat in AUTO_HIGHLIGHT_PATTERNS:
            spans += [m.span() for m in re.finditer(pat, line)]
        for w in AUTO_HIGHLIGHT_WORDS:
            start = 0
            while True:
                i = line.find(w, start)
                if i < 0:
                    break
                spans.append((i, i + len(w)))
                start = i + len(w)

    if not spans:
        return []
    # 长的优先，再按位置贪心去重叠
    spans.sort(key=lambda s: (-(s[1] - s[0]), s[0]))
    kept: list[tuple[int, int]] = []
    for a, b in spans:
        if any(not (b <= x or a >= y) for x, y in kept):
            continue
        kept.append((a, b))
    return sorted(kept)


def _paint(line: str, cfg: VideoCfg, spans: list[tuple[int, int]]) -> str:
    """把区间染成高亮色（含独立描边色），返回带 ASS 内联覆盖标签的文本。"""
    if not spans:
        return line
    on = f"{{\\c{cfg.highlight_color}\\3c{cfg.highlight_outline_color}}}"
    off = f"{{\\c{cfg.font_color}\\3c{cfg.outline_color}}}"
    for a, b in sorted(spans, reverse=True):  # 从后往前插，避免位置偏移
        line = line[:a] + on + line[a:b] + off + line[b:]
    return line


def decorate(line: str, cfg: VideoCfg, row_index: int, partial: bool = False) -> str:
    """按当前样式给一行文字加上高亮覆盖标签。

    ``partial=True`` 时 line 是打字机过程中的累积前缀，关键词打到一半也要是绿的。
    """
    mode = (cfg.highlight_mode or "none").lower()
    if mode == "alternate":
        # 复刻爆款：首行绿、次行白、隔行再绿
        return _paint(line, cfg, [(0, len(line))]) if row_index % 2 == 0 else line
    return _paint(line, cfg, _find_highlight_spans(line, cfg, partial=partial))


def _ass_time(t: float) -> str:
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _ass_head(cfg: VideoCfg, font_size: int, align: int, margin_v: int, outline: float, font: str | None = None) -> str:
    bold = 1 if cfg.bold else 0
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {cfg.width}\n"
        f"PlayResY: {cfg.height}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour,"
        " BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle,"
        " BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font or cfg.font},{font_size},{cfg.font_color},&H000000FF,"
        f"{cfg.outline_color},&H80000000,{bold},0,0,0,100,100,0,0,1,{outline},{cfg.shadow},"
        f"{align},80,80,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def _fade_tag(cfg: VideoCfg) -> str:
    f = max(0.0, float(cfg.subtitle_fade or 0.0))
    if f <= 0:
        return ""
    ms = int(f * 1000)
    return f"{{\\fad({ms},{ms})}}"


def hex_to_ass(color: str, fallback: str = "&H00FFFFFF") -> str:
    """``#RRGGBB`` → ASS 的 ``&H00BBGGRR``；已是 ``&H`` 开头则原样返回。

    配置里两种写法都接受，省得用户记 ASS 的 BGR 反序。
    """
    c = (color or "").strip()
    if not c:
        return fallback
    if c.upper().startswith("&H"):
        return c
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", c):
        return fallback
    r, g, b = c[0:2], c[2:4], c[4:6]
    return f"&H00{b}{g}{r}".upper()


def _font_installed(name: str) -> bool:
    """Windows 下按字体文件名粗查；其他平台交给 libass 自己 fallback。"""
    if os.name != "nt":
        return True
    fn = _FONT_FILES.get(name)
    if not fn:
        return True
    root = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    return (root / fn).exists()


def pick_font(cfg: VideoCfg) -> str:
    """big 样式优先用粗黑体：显式指定 > 用户改过 font > SimHei > 原 font。"""
    if (cfg.style or "classic").lower() != "big":
        return cfg.font
    if cfg.big_font:
        return cfg.big_font
    if cfg.font and cfg.font != DEFAULT_FONT:
        return cfg.font  # 用户在配置里显式改过，尊重
    return BIG_FONT if _font_installed(BIG_FONT) else cfg.font


def big_font_size_of(cfg: VideoCfg) -> int:
    """big 样式的实际字号（未指定时按画面宽度自适应）。"""
    return int(cfg.big_font_size or max(24, round(cfg.width * BIG_FONT_RATIO)))


def write_ass(
    lines: list[str],
    timings: list[tuple[float, float]],
    path: Path,
    cfg: VideoCfg,
    total: float | None = None,
    cues: list[tuple[str, float]] | None = None,
) -> Path:
    """按 cfg.style 生成 ASS。

    classic：一行一条 Dialogue，按 timings 依次显示。
    big    ：按「每屏 N 行」分组，同屏的行用 \\pos 摆在中上部并同时出现。

    ``cues`` 为 TTS 逐字时间戳；给了就按真实语音对齐（big 样式内部自行分配）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if (cfg.style or "classic").lower() == "big":
        return _write_ass_big(lines, path, cfg, total, cues=cues)
    head = _ass_head(cfg, cfg.font_size, cfg.align, cfg.margin_v, cfg.outline)
    fade = _fade_tag(cfg)
    body = "".join(
        f"Dialogue: 0,{_ass_time(st)},{_ass_time(en)},Default,,0,0,0,,{fade}{decorate(line, cfg, i)}\n"
        for i, (line, (st, en)) in enumerate(zip(lines, timings))
    )
    path.write_text(head + body, encoding="utf-8")
    return path


def _big_dialogues(
    screen: list[str], st: float, en: float, cfg: VideoCfg, y0: int, line_h: int, cx: int, fade: str
) -> list[str]:
    """一屏的事件。cfg.typewriter 为真时逐字打出，否则整屏一次显示。"""
    out: list[str] = []

    def dia(a: float, b: float, y: int, text: str) -> str:
        return f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Default,,0,0,0,,{fade}{{\\an5\\pos({cx},{y})}}{text}\n"

    if not cfg.typewriter:
        y = y0
        for row, line in enumerate(screen):
            out.append(dia(st, en, y, decorate(line, cfg, row)))
            y += line_h
        return out

    # ---- 打字机：一行一行、一个字一个字累积出现 ----
    n = sum(len(ln) for ln in screen)
    if n <= 0:
        return out
    span = max(0.0, en - st)
    if cfg.type_speed and cfg.type_speed > 0:
        d = float(cfg.type_speed)
    else:
        hold = min(max(cfg.type_hold, 0.0), 0.9)
        d = max(cfg.type_min, span * (1.0 - hold) / n)
    # 兜底：打得太慢就把整屏时间吃光，留一点点收尾
    if d * n > span * 0.98:
        d = max(0.02, span * 0.98 / n)

    t = st
    for row, line in enumerate(screen):
        y = y0 + row * line_h
        for i in range(1, len(line) + 1):
            a = t + (i - 1) * d
            out.append(dia(a, a + d, y, decorate(line[:i], cfg, row, partial=True)))
        t += len(line) * d
        # 打完的整行保持显示到本屏结束
        if t < en - 0.005:
            out.append(dia(t, en, y, decorate(line, cfg, row)))
    return out


def _write_ass_big(
    lines: list[str],
    path: Path,
    cfg: VideoCfg,
    total: float | None = None,
    cues: list[tuple[str, float]] | None = None,
) -> Path:
    """中上部超大粗体短句 + 隔行/关键词高亮，复刻主流爆款口播号字幕。"""
    font_size = big_font_size_of(cfg)
    outline = max(2.0, round(font_size * 0.028, 1))  # 描边随字号等比缩放
    line_h = int(font_size * max(1.0, cfg.big_line_spacing))
    y0 = int(cfg.height * max(0.0, cfg.big_top_ratio)) + font_size // 2
    cx = cfg.width // 2

    head = _ass_head(cfg, font_size, 5, 0, outline, font=pick_font(cfg))
    screens = pack_screens(lines, cfg.big_lines_per_screen)
    if not screens or not total or total <= 0:
        path.write_text(head, encoding="utf-8")
        return path

    # 有逐字时间戳就按真实语音排，否则退回按字数均分
    timings = align_timings(["".join(s) for s in screens], cues, total) or alloc_screen_timings(screens, total)

    # 打字机是硬切换，带淡入反而糊，直接关掉
    fade = "" if cfg.typewriter else _fade_tag(cfg)
    body: list[str] = []
    for (st, en), screen in zip(timings, screens):
        body += _big_dialogues(screen, st, en, cfg, y0, line_h, cx, fade)
    path.write_text(head + "".join(body), encoding="utf-8")
    return path


def _bg_input(cfg: VideoCfg, dur: float) -> tuple[list[str], str]:
    """返回 (输入参数片段, 背景源标号)。路径必须是绝对路径（ffmpeg 的 cwd 被改过）。

    背景一律做成"无限长"，实际长度由 -shortest 跟着音频走。
    这样即使时长探测有偏差，也绝不会把音频尾巴砍掉。
    """
    p = (cfg.bg_path or "").strip()
    if p and Path(p).exists():
        p = str(Path(p).resolve())
        ext = Path(p).suffix.lower()
        if ext in VIDEO_EXTS:
            # 视频背景：循环播放到音频结束
            return ["-stream_loop", "-1", "-i", p], "0"
        if ext in IMAGE_EXTS:
            return ["-loop", "1", "-i", p], "0"
        raise VideoError(f"不支持的背景格式：{ext}（支持 {sorted(IMAGE_EXTS | VIDEO_EXTS)}）")
    # 纯色兜底：不写 d= 即无限长
    color = cfg.bg_color.lstrip("#")
    return ["-f", "lavfi", "-i", f"color=c=0x{color}:s={cfg.width}x{cfg.height}:r={cfg.fps}"], "0"


def _bg_filter(cfg: VideoCfg) -> str:
    w, h = cfg.width, cfg.height
    if cfg.fit == "contain":
        return f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"


# 混音前统一采样格式：edge-tts 输出 24k 单声道，音乐多为 44.1k 立体声，
# 不统一的话 amix 会因为格式不一致报错或静默丢声道。
AUDIO_SR = 44100


def build_audio_chain(cfg: VideoCfg, total: float, bgm_ok: bool = False) -> str:
    """构造音频滤镜链（filter_complex 里 `;` 之后的那一段）。

    输入下标约定：`[0:v]` 背景、`[1:a]` 配音、`[2:a]` BGM。

    有 BGM 时：
        [1:a] apad 补尾部静音 -> aformat -> asplit   => [voice] / [voice_sc]
        [2:a] 音量 -> 结尾淡出 -> aformat             => [bgm0]
        [bgm0][voice_sc] sidechaincompress            => [bgmd]  ← 人声当侧链，自动闪避
        [voice][bgmd]    amix(duration=first)         => [a]

    四个容易踩的点：
    - `amix` 默认 `normalize=1`，会把每路各降一半，人声直接变小；必须关掉。
    - `amix` 的 `duration=first` 取的是**第一路**的长度，所以人声要放第一位，
      否则 BGM 是无限流（`-stream_loop -1`）会导致输出永不结束。
    - 人声必须先用 `asplit` 分成两路再分别喂给 `sidechaincompress` 和 `amix`。
      同一路标签被两个滤镜直接消费、且图里还有视频流时，ffmpeg 会报
      `Stream specifier '...' in filtergraph description ... matches no streams`。
      实测 ffmpeg 7.1；同一张图单独跑音频却完全正常，所以这个坑很难查。
    - 混音前必须统一采样格式：edge-tts 是 24k 单声道，音乐多为 44.1k 立体声。
    """
    total = max(0.1, float(total))
    pad = f"apad=pad_dur={max(0.0, cfg.tail_pad):.2f}"

    bgm = (cfg.bgm or "").strip()
    if not (bgm and bgm_ok):
        # 没有 BGM 时保持原样：只补尾部静音。
        # 不要顺手统一采样格式——那会把所有人的单声道 24k 音频都变成立体声 44.1k，
        # 对不需要混音的场景纯属扩大影响面。
        return f"[1:a]{pad}[a]"

    # 混音要一个共同格式：edge-tts 是 24k 单声道，音乐多是 44.1k 立体声。
    # 统一到立体声后每声道幅度约 0.707 倍，总功率不变，但峰值低 3dB，混音更不容易削顶。
    fmt = f"aformat=sample_fmts=fltp:sample_rates={AUDIO_SR}:channel_layouts=stereo"

    steps = [f"volume={max(0.0, float(cfg.bgm_volume)):.3f}"]
    fade = max(0.0, float(cfg.bgm_fade_out))
    if fade > 0:
        st = max(0.0, total - fade)
        steps.append(f"afade=t=out:st={st:.2f}:d={fade:.2f}")
    steps.append(fmt)
    bgm_line = f"[2:a]{','.join(steps)}[bgm0]"

    mix_tail = "amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
    if cfg.bgm_duck:
        ratio = max(1.0, float(cfg.bgm_duck_ratio))
        voice_line = f"[1:a]{pad},{fmt},asplit=2[voice][voice_sc]"
        # level_sc 把人声侧链抬高一点，轻人声也能触发压缩
        duck_line = (
            f"[bgm0][voice_sc]sidechaincompress=threshold=0.030:ratio={ratio:g}"
            f":attack=20:release=400:level_sc=1.5[bgmd]"
        )
        return f"{voice_line};{bgm_line};{duck_line};[voice][bgmd]{mix_tail}"

    voice_line = f"[1:a]{pad},{fmt}[voice]"
    return f"{voice_line};{bgm_line};[voice][bgm0]{mix_tail}"


def file_stamp(p: str) -> str:
    """文件的「指纹」：大小 + 修改时间，不读全文件（BGM 动辄几 MB）。

    只比对路径的话，用户换个同名文件就会被当成「没变」，听到的却是旧音乐。
    空路径必须显式返回空串——`Path("")` 是当前目录，会拿到 cwd 的时间戳，
    而产物目录每写一个文件就会变，等于让指纹永远失效。
    """
    s = (p or "").strip()
    if not s:
        return ""
    f = Path(s).expanduser()
    try:
        if not f.is_file():
            return ""
        st = f.stat()
    except OSError:
        return ""
    return f"{st.st_size}:{st.st_mtime_ns}"


def make_video(
    audio: Path, out_path: Path, text: str = "", cfg: VideoCfg | None = None, duration: float | None = None
) -> Path:
    """把一条配音 + 背景 + 字幕合成 mp4。"""
    cfg = cfg or VideoCfg()
    ffmpeg = get_ffmpeg()
    # 必须转绝对路径：下面 ffmpeg 会以 cwd=产物目录 运行，相对路径会找不到
    audio = Path(audio).expanduser().resolve()
    out_path = Path(out_path).expanduser().resolve()
    if not audio.is_file():
        raise VideoError(f"配音文件不存在：{audio}")

    dur = duration or probe_duration(audio)
    if not dur or dur <= 0:
        # 探测彻底失败才按字节粗估（只影响字幕节奏，不影响视频长度）
        dur = max(3.0, audio.stat().st_size / 6000.0)  # 6000B/s ≈ 48kbps，edge-tts 的量级
        log.warning("无法探测 %s 的时长，按文件大小粗估为 %.1fs（仅用于分配字幕）", audio.name, dur)
    dur = round(dur, 2)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    workdir = out_path.parent

    bg_args, _ = _bg_input(cfg, dur)
    chain = [_bg_filter(cfg), f"fps={cfg.fps}"]

    # 配音旁的逐字时间戳（合成时由 edge-tts 的 WordBoundary 记下）
    from .tts import read_cues

    cues = read_cues(audio)

    ass_rel = None
    if cfg.subtitle and text.strip():
        is_big = (cfg.style or "classic").lower() == "big"
        if is_big:
            # 按词断行，语义完整；用户填的关键词也一并加进词典
            lines = split_by_words(text, cfg.big_chars_per_line, extra_words=cfg.keywords)
        else:
            lines = split_lines(text, cfg.chars_per_line)
        # 字幕只铺满语音部分，尾部留白不压字
        total = max(1.0, dur)
        timings = alloc_timings(lines, total)
        if cues:
            aligned = align_timings(lines, cues, total)
            if aligned:
                timings = aligned
            log.info(
                "   %s 字幕时间轴：%s（%d 个字的时间戳）",
                out_path.name,
                "按 TTS 逐字时间戳对齐" if aligned else "按字数比例分配",
                len(cues),
            )
        else:
            log.info("   %s 字幕时间轴：按字数比例分配（无逐字时间戳）", out_path.name)
        ass = write_ass(lines, timings, workdir / (out_path.stem + ".ass"), cfg, total=total, cues=cues)
        ass_rel = ass.name
        chain.append(f"subtitles=f='{ass_rel}':fontsdir='C\\:/Windows/Fonts'")

    if cfg.fade_in > 0:
        chain.append(f"fade=t=in:st=0:d={cfg.fade_in}")

    # BGM：路径要在脚本侧转绝对（下面 ffmpeg 的 cwd 是产物目录），文件不在就降级为不加
    bgm_path = ""
    if (cfg.bgm or "").strip():
        bp = Path(cfg.bgm).expanduser()
        if bp.is_file():
            bgm_path = str(bp.resolve())
        else:
            log.warning("BGM 文件不存在，本条按无人声背景乐处理：%s", cfg.bgm)

    # 画面总时长 = 语音 + 尾部留白（BGM 的淡出位置按这个算）
    total = max(1.0, dur) + max(0.0, cfg.tail_pad)
    audio_chain = build_audio_chain(cfg, total, bgm_ok=bool(bgm_path))
    if bgm_path:
        log.info(
            "   %s BGM：%s，音量 %.2f，闪避 %s，淡出 %.1fs",
            out_path.name,
            Path(bgm_path).name,
            cfg.bgm_volume,
            "开" if cfg.bgm_duck else "关",
            cfg.bgm_fade_out,
        )

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    cmd += bg_args
    cmd += ["-i", str(audio)]
    if bgm_path:
        # 循环到画面结束；实际长度由 amix(duration=first) 跟着人声走
        cmd += ["-stream_loop", "-1", "-i", bgm_path]
    cmd += [
        "-filter_complex",
        f"[0:v]{','.join(chain)}[v];{audio_chain}",
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        cfg.preset,
        "-crf",
        str(cfg.crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        cfg.audio_bitrate,
        # 不用 -t 硬截断（会把音频砍掉），靠 -shortest 让画面跟着补过静音的音频走
        "-shortest",
        str(out_path),
    ]

    log.debug("合成命令：%s", " ".join(cmd))
    # cwd 设到产物目录，ASS 用相对名，避免 Windows 路径里的冒号/反斜杠破坏滤镜参数
    r = subprocess.run(cmd, cwd=str(workdir), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not out_path.is_file():
        raise VideoError(f"合成失败：{(r.stderr or '')[-600:]}")

    # 自检：视频绝不能比音频短，短了说明语音被截掉
    vdur = probe_duration(out_path, exact=False)
    if vdur and vdur + 0.15 < dur:
        log.warning(
            "⚠ %s 视频 %.2fs 短于音频 %.2fs，语音可能被截断，请检查背景文件是否可循环", out_path.name, vdur, dur
        )
    else:
        log.info("   %s 音频 %.2fs -> 视频 %.2fs", out_path.name, dur, vdur or 0.0)
    return out_path


def make_many(
    items: list[tuple[str, str, str]], out_dir: Path, cfg: VideoCfg | None = None, concurrency: int = 1
) -> dict[str, Path]:
    """items: [(name, 文案, 音频路径)] -> {name: 视频路径}。"""
    from concurrent.futures import ThreadPoolExecutor

    cfg = cfg or VideoCfg()
    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Path] = {}
    total = len(items)
    done = 0

    def _one(it):
        name, text, audio = it
        if not audio or not Path(audio).is_file():
            log.warning("跳过 %s：没有配音文件", name)
            return name, None
        target = out_dir / f"{name}.mp4"
        try:
            return name, make_video(Path(audio), target, text, cfg)
        except Exception as e:  # noqa: BLE001
            log.error("合成失败 %s：%s", name, str(e)[:160])
            return name, None

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for name, path in pool.map(_one, items):
            done += 1
            if path:
                results[name] = path
            log.info("合成进度 %d/%d（成功 %d）", done, total, len(results))
    return results
