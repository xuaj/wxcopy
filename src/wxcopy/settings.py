"""配置加载：YAML 文件 + 环境变量插值。

优先级（后者覆盖前者）：
    包内默认配置 config/config.example.yaml
    -> ~/.wxcopy/config.yaml
    -> ./wxcopy.yaml
    -> 命令行 --config 指定的文件
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PACKAGE_ROOT / "config" / "config.example.yaml"


def expand_env(node: Any) -> Any:
    """递归把字符串里的 ${VAR} / ${VAR:-默认} 替换为环境变量。"""
    if isinstance(node, dict):
        return {k: expand_env(v) for k, v in node.items()}
    if isinstance(node, list):
        return [expand_env(v) for v in node]
    if isinstance(node, str):

        def _sub(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            val = os.environ.get(name)
            if val:
                return val
            return default if default is not None else ""

        return ENV_PATTERN.sub(_sub, node)
    return node


def _take(data: dict, cls, **extra):
    """用字典里同名字段构造 dataclass，忽略未知键。"""
    names = set(getattr(cls, "__dataclass_fields__", {}))
    kwargs = {k: v for k, v in data.items() if k in names}
    kwargs.update(extra)
    return cls(**kwargs)


@dataclass
class PathsCfg:
    workdir: str = "./output"
    audio_dir: str = ""
    text_dir: str = ""
    voice_dir: str = ""
    video_dir: str = ""
    cover_dir: str = ""
    publish_dir: str = ""


@dataclass
class CoverCfg:
    """封面图的默认参数（对应 core.cover.CoverCfg）。

    画封面只用到 Pillow，不走 AI 生图：零成本、可控、无水印、不受网络影响。
    Pillow 是可选依赖，没装时 cover 阶段会明确报错并提示安装方式。
    """

    style: str = "tech"  # tech 深色渐变 | plain 纯色 | light 浅色
    width: int = 1080
    height: int = 1920
    accent: str = "#00F090"  # 强调色（分隔线）
    bg: str = ""  # 背景图路径；留空按 style 本地生成
    bg_color: str = ""  # plain/light 的底色；留空按 style 取默认
    footer: str = ""  # 页脚小字，如项目名
    font: str = ""  # 字体名或字体文件路径；留空自动挑中文字体
    title_max: int = 20  # 主标题最多几个字（超出截断，断行另有算法）
    subtitle_max: int = 24
    jpeg: bool = False  # 输出 .jpg（体积小）而非 .png


@dataclass
class PublishCfg:
    """发布素材包（标题 / 简介 / 话题）的默认偏好。

    注意：短视频平台没有对普通开发者开放的「代用户发布」接口，
    这里只准备「上传前要填的那些字」，最后一步仍要手动发布。
    """

    titles: int = 5  # 候选标题数量
    desc_max: int = 100  # 简介最大字数
    topics: int = 6  # 话题标签数量
    platforms: str = "视频号,抖音"  # 清单里的默认平台
    concurrency: int = 1
    prompt: str = ""  # prompt 名或 md 路径，留空用内置 default
    auto_cover: bool = True  # 「封面」列/cover 目录都取不到时，自动用封面目录兜底


@dataclass
class ASROpenAICfg:
    base_url: str = "https://api.siliconflow.cn/v1"
    api_key_env: str = "SILICONFLOW_API_KEY"
    model: str = "FunAudioLLM/SenseVoiceSmall"


@dataclass
class ASRCfg:
    provider: str = "faster-whisper"
    model: str = "small"
    language: str = "zh"
    device: str = "auto"
    compute_type: str = "default"
    vad_filter: bool = True
    beam_size: int = 5
    # 引导模型用简体中文输出，实测能显著减少「繁体/错别字」问题
    initial_prompt: str = "以下是普通话的句子，请使用简体中文输出。"
    # 国内直连 HuggingFace 常被拦，默认走镜像；海外可设为空字符串
    hf_endpoint: str = "https://hf-mirror.com"
    disable_xet: bool = True  # Xet 通道在国内常返回 401，默认关掉
    openai: ASROpenAICfg = field(default_factory=ASROpenAICfg)


@dataclass
class ProviderCfg:
    type: str = "openai"  # openai | ollama
    base_url: str = ""
    api_key_env: str = ""
    api_key: str = ""
    model: str = ""
    keep_alive: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class LLMCfg:
    provider: str = "ollama"
    model: str = "qwen3:8b"
    temperature: float = 0.85
    max_tokens: int = 2048
    top_p: float = 0.9
    timeout: int = 600
    max_retries: int = 3


@dataclass
class RewriteCfg:
    prompt: str = "default"
    prompt_file: str = ""
    concurrency: int = 1
    max_chars: int = 6000
    keep_hashtags: bool = False
    min_chars: int = 20


@dataclass
class TTSOpenAICfg:
    base_url: str = "https://api.siliconflow.cn/v1"
    api_key_env: str = "SILICONFLOW_API_KEY"
    model: str = "FunAudioLLM/CosyVoice2-0.5B"
    voice: str = "FunAudioLLM/CosyVoice2-0.5B:alex"


@dataclass
class TTSCfg:
    provider: str = "edge"
    voice: str = "zh-CN-XiaoxiaoNeural"
    rate: str = "+0%"
    volume: str = "+0%"
    pitch: str = "+0Hz"
    format: str = "mp3"
    cues: bool = True  # 记录逐字时间戳（<name>.cues.json），让字幕对齐真实语音
    openai: TTSOpenAICfg = field(default_factory=TTSOpenAICfg)


@dataclass
class RunCfg:
    concurrency: int = 1
    skip_existing: bool = True


@dataclass
class OcrCfg:
    """图片转文字（用于「图片」这种原始文案来源）。"""

    provider: str = "auto"  # auto | rapidocr | tesseract | openai | ollama
    model: str = ""  # 视觉模型名（openai/ollama 必填，如 qwen2.5vl:7b）
    base_url: str = ""  # 留空时 ollama 用 127.0.0.1:11434
    api_key_env: str = ""  # openai 后端从该环境变量取 Key
    api_key: str = ""  # 也可直接写死（不推荐）
    prompt: str = ""  # 留空用 core.source.DEFAULT_OCR_PROMPT
    max_side: int = 1600  # 送视觉模型前把长边缩到多少，省 token
    min_score: float = 0.5  # rapidocr 置信度过滤
    languages: str = "chi_sim+eng"  # 仅 tesseract
    concurrency: int = 1


@dataclass
class VideoCfg:
    """仿写文案 -> 口播视频 的默认参数。"""

    bg_path: str = ""  # 背景图片/视频，留空用纯色
    bg_color: str = "#1e293b"
    fit: str = "cover"  # cover | contain
    size: str = "竖屏 1080x1920"  # 见 core.video.PRESETS
    width: int = 0  # >0 时覆盖 size
    height: int = 0
    fps: int = 30

    # ---- 字幕样式 ----
    style: str = "classic"  # classic（底部整句）| big（中上部大字 + 关键词高亮）
    subtitle: bool = True
    font: str = "Microsoft YaHei"
    bold: bool = False
    font_size: int = 76
    font_color: str = "&H00FFFFFF"
    outline_color: str = "&H00000000"
    outline: float = 3.0
    shadow: float = 1.0
    margin_v: int = 300
    chars_per_line: int = 16
    align: int = 2
    subtitle_fade: float = 0.08

    # ---- big 样式专用 ----
    big_font: str = ""  # 空 = 自动挑粗黑体（SimHei）
    big_font_size: int = 0  # 0 = 按画面宽度自适应
    big_chars_per_line: int = 4
    big_lines_per_screen: int = 3
    big_top_ratio: float = 0.16
    big_line_spacing: float = 1.5
    highlight_mode: str = "none"  # none | alternate | keywords | auto
    highlight_color: str = "&H0090F000"  # #00F090 霓虹绿
    highlight_outline_color: str = "&H00FFFFFF"
    keywords: list[str] = field(default_factory=list)

    # ---- 打字机效果（逐字打出，仅 big 样式生效）----
    typewriter: bool = False
    type_speed: float = 0.0  # 每字固定秒数；0 = 按本屏时长自动算
    type_hold: float = 0.3  # 打完后整行停留时间占本屏时长的比例
    type_min: float = 0.045  # 自动模式下每字时长下限（秒）

    crf: int = 23
    preset: str = "veryfast"
    audio_bitrate: str = "192k"
    fade_in: float = 0.0
    tail_pad: float = 0.6  # 语音结束后追加的静音秒数（尾部留白）

    # ---- BGM（背景音乐）----
    bgm: str = ""  # 背景音乐文件；留空则只有人声
    bgm_volume: float = 0.18  # BGM 相对人声的音量
    bgm_duck: bool = True  # 人声出现时自动压低 BGM
    bgm_duck_ratio: float = 8.0  # 闪避压缩比
    bgm_fade_out: float = 1.5  # BGM 结尾淡出秒数

    concurrency: int = 1


@dataclass
class Settings:
    paths: PathsCfg = field(default_factory=PathsCfg)
    asr: ASRCfg = field(default_factory=ASRCfg)
    llm: LLMCfg = field(default_factory=LLMCfg)
    providers: dict[str, ProviderCfg] = field(default_factory=dict)
    rewrite: RewriteCfg = field(default_factory=RewriteCfg)
    tts: TTSCfg = field(default_factory=TTSCfg)
    video: VideoCfg = field(default_factory=VideoCfg)
    ocr: OcrCfg = field(default_factory=OcrCfg)
    cover: CoverCfg = field(default_factory=CoverCfg)
    publish: PublishCfg = field(default_factory=PublishCfg)
    run: RunCfg = field(default_factory=RunCfg)
    config_path: str = ""

    # ---- 便捷属性 ----
    @property
    def workdir(self) -> Path:
        return Path(self.paths.workdir).expanduser().resolve()

    @property
    def audio_dir(self) -> Path:
        return Path(self.paths.audio_dir or self.workdir / "audio").expanduser().resolve()

    @property
    def text_dir(self) -> Path:
        return Path(self.paths.text_dir or self.workdir / "text").expanduser().resolve()

    @property
    def voice_dir(self) -> Path:
        return Path(self.paths.voice_dir or self.workdir / "voice").expanduser().resolve()

    @property
    def video_dir(self) -> Path:
        return Path(self.paths.video_dir or self.workdir / "video").expanduser().resolve()

    @property
    def cover_dir(self) -> Path:
        return Path(self.paths.cover_dir or self.workdir / "cover").expanduser().resolve()

    @property
    def publish_dir(self) -> Path:
        return Path(self.paths.publish_dir or self.workdir / "publish").expanduser().resolve()

    def provider(self, name: str | None = None) -> ProviderCfg:
        key = name or self.llm.provider
        if key in self.providers:
            return self.providers[key]
        # 没预设时，把名字当成自定义：默认按 openai 兼容处理
        return ProviderCfg(type="openai", model=key)


def _load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(config: str | None = None) -> Settings:
    """按优先级合并多份配置并构造 Settings。"""
    raw: dict = {}
    loaded_from = ""
    for candidate in [DEFAULT_CONFIG, Path.home() / ".wxcopy" / "config.yaml", Path("wxcopy.yaml")]:
        if candidate and Path(candidate).exists():
            raw = _deep_merge(raw, _load_yaml(Path(candidate)))
            loaded_from = str(candidate)
    if config:
        p = Path(config).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"配置文件不存在: {p}")
        raw = _deep_merge(raw, _load_yaml(p))
        loaded_from = str(p)

    raw = expand_env(raw)

    providers = {}
    for name, pdata in (raw.get("providers") or {}).items():
        providers[name] = _take(pdata or {}, ProviderCfg)

    return Settings(
        paths=_take(raw.get("paths") or {}, PathsCfg),
        asr=ASRCfg(
            **{
                **_take(raw.get("asr") or {}, ASRCfg).__dict__,
                "openai": _take((raw.get("asr") or {}).get("openai") or {}, ASROpenAICfg),
            },
        ),
        llm=_take(raw.get("llm") or {}, LLMCfg),
        providers=providers,
        rewrite=_take(raw.get("rewrite") or {}, RewriteCfg),
        tts=TTSCfg(
            **{
                **_take(raw.get("tts") or {}, TTSCfg).__dict__,
                "openai": _take((raw.get("tts") or {}).get("openai") or {}, TTSOpenAICfg),
            },
        ),
        run=_take(raw.get("run") or {}, RunCfg),
        video=_take(raw.get("video") or {}, VideoCfg),
        ocr=_take(raw.get("ocr") or {}, OcrCfg),
        cover=_take(raw.get("cover") or {}, CoverCfg),
        publish=_take(raw.get("publish") or {}, PublishCfg),
        config_path=loaded_from,
    )


def api_key_of(env_name: str) -> str:
    return os.environ.get(env_name or "", "")


def write_example_config(dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(DEFAULT_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return dest
