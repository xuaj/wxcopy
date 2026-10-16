"""成片封面生成：把「标题 + 副标题 + 页脚」画成一张 1080×1920 竖图。

封面是发布必需的一环，但以前得手工写 PIL 脚本（调字体、算坐标、配辉光），
这里把它固化成命令：`wxcopy cover`，并让 `publish` 阶段能自动带上封面路径。

设计取舍：
- **不依赖 AI 生图**：渐变 + 光晕 + 网格在本地就能画，零成本、可控、无水印，
  也不受网络影响。想用现成背景图就传 `--bg`。
- **Pillow 是可选依赖**：没装会给出明确的安装提示，而不是让整条流水线挂掉。
- **字号自动缩放**：标题长短不一，硬编码字号必然要么溢出要么小得可怜。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..logging_setup import get_logger

log = get_logger()


class CoverError(RuntimeError):
    pass


# ---------------------------------------------------------------- 字体


# (字体名, 候选文件名) 按优先级排列；跨平台挑第一个真实存在的
_FONT_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SimHei", ("simhei.ttf",)),
    ("Microsoft YaHei Bold", ("msyhbd.ttc", "msyh.ttc")),
    ("PingFang SC", ("PingFang.ttc",)),
    # 各发行版的文件名不统一，全都列上，命中哪个算哪个
    (
        "Noto Sans CJK SC",
        (
            "NotoSansCJKsc-Bold.otf",
            "NotoSansCJKsc-Regular.otf",
            "NotoSansCJK-Bold.ttc",
            "NotoSansCJK-Regular.ttc",
        ),
    ),
    ("WenQuanYi Zen Hei", ("wqy-zenhei.ttc",)),
    ("WenQuanYi Micro Hei", ("wqy-microhei.ttc",)),
    ("Arial Unicode MS", ("Arial Unicode.ttf",)),
)

_FONT_DIRS: tuple[str, ...] = (
    "%WINDIR%/Fonts",
    "C:/Windows/Fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
    "/usr/share/fonts/truetype",
    "/usr/share/fonts/opentype",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "~/.local/share/fonts",
)


def _expand(p: str) -> Path:
    import os

    return Path(os.path.expandvars(p)).expanduser()


def find_fonts() -> list[tuple[str, Path]]:
    """返回本机可用的中文字体 [(字体名, 路径)]，按优先级排序。

    先按「目录 / 文件名」直查；直查不到再在该目录下递归找一层——
    Debian/Ubuntu 把 Noto CJK 放在 `/usr/share/fonts/opentype/noto/` 这种子目录里，
    只查顶层的话会误判成「没有中文字体」。
    """
    found: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for name, files in _FONT_CANDIDATES:
        hit: Path | None = None
        for d in _FONT_DIRS:
            root = _expand(d)
            if not root.is_dir():
                continue
            for fn in files:
                cand = root / fn
                if cand.is_file():
                    hit = cand
                    break
                sub = next(root.rglob(fn), None) if cand != root else None
                if sub is not None and sub.is_file():
                    hit = sub
                    break
            if hit:
                break
        if hit and hit not in seen:
            found.append((name, hit))
            seen.add(hit)
    return found


def pick_font(prefer: str = "") -> Path | None:
    """挑一个中文字体。prefer 可以是字体名、字体文件名或完整路径。"""
    if prefer:
        p = Path(prefer)
        if p.is_file():
            return p
        for name, path in find_fonts():
            if prefer.lower() in (name.lower(), path.name.lower()):
                return path
        log.warning("指定字体 %s 没找到，改用自动挑选", prefer)
    for _, path in find_fonts():
        return path
    return None


# ---------------------------------------------------------------- 配置


@dataclass
class CoverCfg:
    width: int = 1080
    height: int = 1920
    style: str = "tech"  # tech 深色渐变 | plain 纯色 | light 浅色
    bg: str = ""  # 背景图；留空则按 style 本地生成
    bg_color: str = ""  # plain / light 的底色；留空按 style 取默认
    accent: str = "#00F090"  # 强调色（分隔线、标签框）
    title_max: int = 20  # 主标题最多几个字（超出才截；断行由 wrap_lines 负责）
    subtitle_max: int = 24
    footer: str = ""
    font: str = ""
    jpeg: bool = False


_STYLES = ("tech", "plain", "light")


def _hex_to_rgb(s: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    s = (s or "").strip().lstrip("#")
    if len(s) == 6:
        try:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        except ValueError:
            pass
    return default


# ---------------------------------------------------------------- 排版


def clip(text: str, limit: int) -> str:
    """截断并补省略号，不把最后一个字切一半。"""
    t = (text or "").strip()
    if limit and len(t) > limit:
        return t[:limit].rstrip("，。、；：,. ") + "…"
    return t


# 断行的最小单元：一个连续的英文/数字串算一个整体（不能从中间切开），
# 其余字符（含中文）逐字。
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+\-]*|.")
# 这些标点必须跟在上一行末尾，不能出现在行首
_TAIL_PUNCT = set("，。、；：,.!?！？）】》」』…")


def _tw(s: str) -> float:
    """粗略显示宽度：ASCII 约占半个字，其余（中日韩、全角标点）算一个字。

    不按显示宽度算的话，「开源免费AI工具推荐」会被切成「开源免费A / I工具推荐」——
    数字符数是 10 个字，但视觉上是 9 个字宽，两种口径混用必然错位。
    """
    return sum(0.55 if ord(c) < 128 else 1.0 for c in s)


def wrap_lines(text: str, per_line: float, max_lines: int = 3) -> list[str]:
    """按显示宽度贪心装箱断行，英文单词不拆开，收尾标点不离群。"""
    t = (text or "").strip()
    if not t:
        return []
    per = max(1.0, float(per_line))
    toks = _TOKEN_RE.findall(t)
    lines: list[str] = []
    buf = ""
    for i, tk in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        # ① 单个 token 比一行还宽（超长英文串、链接）：只能硬切，切完再继续装箱
        if _tw(tk) > per:
            if buf:
                lines.append(buf)
                buf = ""
            while _tw(tk) > per:
                cut = tk[: max(1, int(per))]
                lines.append(cut)
                tk = tk[len(cut) :]
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
        # ② 装不下就换行。但两种情况下**不换**：
        #    · 当前字符是收尾标点——标点不能出现在行首；
        #    · 下一个字符是收尾标点——让本行多容一个字，
        #      否则「时间的不是拍摄，是文案」会被切成「…不是拍 / 摄，是文案」，
        #      逗号跑到行首，看着像错别字。
        if buf and tk not in _TAIL_PUNCT and nxt not in _TAIL_PUNCT and _tw(buf) + _tw(tk) > per:
            lines.append(buf)
            buf = tk
        else:
            buf += tk
    if buf:
        lines.append(buf)

    if len(lines) > max_lines:  # 真的放不下才截，并明确标出
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip("，。、；：,. ") + "…"
    return lines


def plan_lines(text: str, max_lines: int = 3) -> list[str]:
    """决定标题分几行。

    先按「尽量均匀」的目标每行宽度试装，装不下就放宽一点重试——
    比事先拍一个固定字数靠谱：中英混排时字数不等于宽度。
    """
    t = (text or "").strip()
    if not t:
        return ["未命名"]
    n = len(t)
    if n <= 8 or max_lines <= 1:
        return [t]
    rows = 2 if n <= 16 else max_lines
    for per in range(max(3, math.ceil(n / rows)), n + 1):
        lines = wrap_lines(t, per, max_lines=99)
        if len(lines) <= rows:
            return lines
    return wrap_lines(t, math.ceil(n / max_lines), max_lines=max_lines)


def _fit_size(draw, font_path: Path, text: str, max_width: int, start: int, min_size: int = 24) -> int:
    """从 start 开始往下试，找到能塞进 max_width 的最大字号。"""
    from PIL import ImageFont

    for size in range(int(start), int(min_size) - 1, -4):
        f = ImageFont.truetype(str(font_path), size)
        if draw.textlength(text, font=f) <= max_width:
            return size
    return min_size


def _draw_centered(
    draw,
    xy: tuple[int, int],
    text: str,
    font_path: Path,
    size: int,
    fill: tuple[int, int, int],
) -> None:
    from PIL import ImageFont

    f = ImageFont.truetype(str(font_path), size)
    draw.text(xy, text, font=f, fill=fill, anchor="mm")


# ---------------------------------------------------------------- 背景


def _gradient_bg(w: int, h: int, top: tuple[int, int, int], bottom: tuple[int, int, int]):
    """低分辨率构图再放大：竖屏纯色渐变，比逐像素快得多。"""
    from PIL import Image

    small = Image.new("RGB", (2, 256))
    px = small.load()
    for y in range(256):
        t = y / 255
        px[0, y] = px[1, y] = (
            int(top[0] + (bottom[0] - top[0]) * t),
            int(top[1] + (bottom[1] - top[1]) * t),
            int(top[2] + (bottom[2] - top[2]) * t),
        )
    return small.resize((w, h), Image.BICUBIC)


def _tech_bg(w: int, h: int, accent: tuple[int, int, int]):
    """深色科技感：暗蓝渐变 + 底部光晕 + 细网格。

    网格必须画在**独立透明层**上用低 alpha 合成。
    早期版本图省事把整图 blend 到黑来压网格，结果把光晕一起压没了，整张封面死黑。
    """
    from PIL import Image, ImageDraw, ImageFilter

    img = _gradient_bg(w, h, (16, 24, 46), (7, 10, 20))

    # 底部光晕：小图画再放大，快且柔和
    gw, gh = max(1, w // 8), max(1, h // 8)
    glow = Image.new("RGB", (gw, gh), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse(
        [gw * 0.02, gh * 0.60, gw * 0.98, gh * 1.10],
        fill=(accent[0] // 2, accent[1] // 2, accent[2] // 2),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(gw / 4)).resize((w, h), Image.BICUBIC)
    img = Image.blend(img, glow, 0.45).convert("RGBA")

    # 细网格：alpha 很低，只压网格不压光晕
    grid = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gdd = ImageDraw.Draw(grid)
    step = w // 8
    for x in range(step, w, step):
        gdd.line([(x, 0), (x, h)], fill=(255, 255, 255, 14), width=1)
    for y in range(step, h, step):
        gdd.line([(0, y), (w, y)], fill=(255, 255, 255, 14), width=1)
    return Image.alpha_composite(img, grid).convert("RGB")


def _solid_bg(w: int, h: int, color: tuple[int, int, int]):
    from PIL import Image

    return Image.new("RGB", (w, h), color)


# ---------------------------------------------------------------- 主入口


def make_cover(
    title: str,
    out_path: Path,
    cfg: CoverCfg | None = None,
    subtitle: str = "",
) -> Path:
    """画一张封面。缺 Pillow 或字体时抛 CoverError（由调用方降级为跳过）。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError as e:  # pragma: no cover - 取决于环境
        raise CoverError("生成封面需要 Pillow：`pip install Pillow` 或 `pip install -e '.[cover]'`") from e

    cfg = cfg or CoverCfg()
    w, h = cfg.width, cfg.height
    accent = _hex_to_rgb(cfg.accent, (0, 240, 144))
    style = (cfg.style or "tech").lower()

    font_path = pick_font(cfg.font)
    if font_path is None:
        raise CoverError("没找到可用的中文字体，请用 --font 指定字体文件路径")

    # ---- 背景 ----
    bgp = (cfg.bg or "").strip()
    if bgp and Path(bgp).is_file():
        img = Image.open(bgp).convert("RGB")
        sw, sh = img.size
        scale = max(w / sw, h / sh)
        img = img.resize((max(1, math.ceil(sw * scale)), max(1, math.ceil(sh * scale))), Image.LANCZOS)
        left = (img.width - w) // 2
        top = (img.height - h) // 2
        img = img.crop((left, top, left + w, top + h))
        # 压暗，保证白字可读
        img = Image.blend(img, Image.new("RGB", (w, h), (0, 0, 0)), 0.35)
    elif style == "light":
        img = _solid_bg(w, h, _hex_to_rgb(cfg.bg_color, (246, 248, 251)))
    elif style == "plain":
        img = _solid_bg(w, h, _hex_to_rgb(cfg.bg_color, (15, 23, 42)))
    else:
        img = _tech_bg(w, h, accent)

    # light 风格是浅底深字，其余都是深底浅字
    dark = style != "light"
    fg = (255, 255, 255) if dark else (24, 32, 48)
    soft = (176, 196, 220) if dark else (90, 104, 124)

    d = ImageDraw.Draw(img)
    cx = w // 2
    margin = int(w * 0.09)
    max_w = w - margin * 2

    # ---- 主标题 ----
    t = clip(title, cfg.title_max)
    lines = plan_lines(t, max_lines=3)

    # 先按单行的情况试字号，再按行数缩
    longest = max(lines, key=len)
    base = _fit_size(d, font_path, longest, max_w, start=int(w * 0.24))
    if len(lines) >= 2:
        base = min(base, int(h * 0.115))
    line_h = int(base * 1.32)

    block_h = line_h * len(lines)
    y = int(h * 0.40) - block_h // 2
    for ln in lines:
        _draw_centered(d, (cx, y + line_h // 2), ln, font_path, base, fg)
        y += line_h

    top_after_title = y

    # ---- 强调分隔线 ----
    d.rounded_rectangle([cx - 110, top_after_title + 26, cx + 110, top_after_title + 32], radius=3, fill=accent)

    # ---- 副标题 ----
    sub = clip(subtitle, cfg.subtitle_max)
    if sub:
        ssize = _fit_size(d, font_path, sub, max_w, start=int(w * 0.062))
        _draw_centered(d, (cx, top_after_title + 90), sub, font_path, ssize, soft)

    # ---- 页脚 ----
    foot = clip(cfg.footer, 30)
    if foot:
        fsize = _fit_size(d, font_path, foot, max_w, start=int(w * 0.05))
        _draw_centered(d, (cx, int(h * 0.90)), foot, font_path, fsize, soft)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if cfg.jpeg:
        out_path = out_path.with_suffix(".jpg")
        img.save(out_path, quality=92)
    else:
        out_path = out_path.with_suffix(".png")
        img.save(out_path)
    return out_path


def make_many(
    items: list[tuple[str, str, str]],
    out_dir: Path,
    cfg: CoverCfg | None = None,
    on_error: Callable[[str, str], None] | None = None,
) -> dict[str, Path]:
    """items: [(name, title, subtitle)] -> {name: 封面路径}。单条失败不影响其他。"""
    cfg = cfg or CoverCfg()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Path] = {}
    for name, title, subtitle in items:
        try:
            results[name] = make_cover(title or name, out_dir / f"{name}.png", cfg, subtitle=subtitle)
        except CoverError as e:
            log.error("封面生成失败 %s：%s", name, str(e)[:120])
            if on_error:
                on_error(name, str(e))
        except Exception as e:  # noqa: BLE001
            log.error("封面生成失败 %s：%s", name, str(e)[:120])
            if on_error:
                on_error(name, str(e))
    return results


def title_of(text: str, limit: int = 12) -> str:
    """从一段文案里提炼封面主标题：取第一句。

    第一句本身就超长时，再退一步取第一个逗号前的分句——
    「开源免费AI工具推荐：wxcopy，本地Ollama免费仿写」直接按字数截会变成
    「…本地Oll…」，话没说完整就断了，摆在封面上很怪。
    """
    t = (text or "").strip()
    if not t:
        return ""
    for sep in "。！？!?\n":
        idx = t.find(sep)
        if idx > 0:
            t = t[:idx]
            break
    if limit and len(t) > limit:
        # 逗号优先（切出来最长），其次分号、冒号
        for sep in "，,；;：:":
            idx = t.find(sep)
            if 0 < idx <= limit:
                return t[:idx].strip()
    return clip(t, limit)
