"""可切换的 LLM 抽象层。

设计要点：
- 所有云端厂商（DeepSeek / 通义 / 智谱 / Kimi / 豆包 / 硅基流动 / OpenAI）都提供
  OpenAI 兼容端点，因此统一走 OpenAI SDK，只换 base_url 与 api_key。
- Ollama 本地模型也提供 /v1 兼容端点，同样走 OpenAI SDK；
  额外用原生 /api/tags、/api/pull 做模型列举与拉取。
- 新增厂商 = 在配置 providers 里加一段，无需改代码。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterable

from ..logging_setup import get_logger
from ..settings import LLMCfg, ProviderCfg, Settings, api_key_of

log = get_logger()


class LLMError(RuntimeError):
    pass


@dataclass
class Message:
    role: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class BaseLLM(ABC):
    """所有模型后端的统一接口。"""

    kind: str = "base"
    name: str = ""

    def __init__(self, provider: ProviderCfg, gen: LLMCfg, model: str | None = None):
        self.provider = provider
        self.gen = gen
        self.model = model or provider.model or gen.model

    @abstractmethod
    def chat(self, messages: Iterable[Message], **overrides: Any) -> str:
        """返回模型输出文本。"""

    def complete(self, system: str, user: str, **overrides: Any) -> str:
        msgs = []
        if system:
            msgs.append(Message("system", system))
        msgs.append(Message("user", user))
        return self.chat(msgs, **overrides)

    @abstractmethod
    def list_models(self) -> list[str]:
        """列出该后端可用模型。"""

    def health(self) -> tuple[bool, str]:
        try:
            models = self.list_models()
            return True, f"{len(models)} 个模型可用"
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:200]

    def _retry(self, fn):
        last: Exception | None = None
        for attempt in range(1, (self.gen.max_retries or 1) + 1):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < (self.gen.max_retries or 1):
                    wait = 2**attempt
                    log.warning("第 %d 次调用失败，%d 秒后重试：%s", attempt, wait, str(e)[:120])
                    time.sleep(wait)
        raise LLMError(f"{self.name} 调用失败：{last}")


class OpenAICompatLLM(BaseLLM):
    """OpenAI 兼容端点（覆盖绝大多数云厂商）。"""

    kind = "openai"

    def __init__(self, provider: ProviderCfg, gen: LLMCfg, model: str | None = None):
        super().__init__(provider, gen, model)
        from openai import OpenAI  # 延迟导入，避免未安装时整体不可用

        api_key = provider.api_key or api_key_of(provider.api_key_env)
        if not api_key:
            raise LLMError(
                f"未提供 API Key：请在环境变量 {provider.api_key_env or '(未配置)'} 中设置，或在配置里写死 api_key"
            )
        self.client = OpenAI(
            base_url=provider.base_url or None,
            api_key=api_key,
            timeout=gen.timeout,
            max_retries=0,  # 重试由本层统一控制
        )
        self.name = f"openai:{self.model}"

    def chat(self, messages: Iterable[Message], **overrides: Any) -> str:
        payload: dict[str, Any] = {
            "model": overrides.get("model") or self.model,
            "messages": [m.as_dict() if isinstance(m, Message) else m for m in messages],
            "temperature": overrides.get("temperature", self.gen.temperature),
            "top_p": overrides.get("top_p", self.gen.top_p),
        }
        max_tokens = overrides.get("max_tokens", self.gen.max_tokens)
        if max_tokens:
            payload["max_tokens"] = max_tokens
        extra = overrides.get("extra_body") or self.provider.extra or None
        if extra:
            payload["extra_body"] = extra

        def _call():
            resp = self.client.chat.completions.create(**payload)
            if not resp.choices:
                raise LLMError("模型返回为空")
            return (resp.choices[0].message.content or "").strip()

        return self._retry(_call)

    def list_models(self) -> list[str]:
        resp = self.client.models.list()
        return sorted({m.id for m in resp.data})


class OllamaLLM(OpenAICompatLLM):
    """本地 Ollama。

    Ollama 自带 /v1 兼容端点，所以复用 OpenAICompatLLM；
    但模型列举/拉取走原生 API，能拿到更准确的信息。
    """

    kind = "ollama"

    def __init__(self, provider: ProviderCfg, gen: LLMCfg, model: str | None = None):
        # Ollama 不需要 key，塞一个占位值跳过校验
        provider = ProviderCfg(
            type="ollama",
            base_url=(provider.base_url or "http://127.0.0.1:11434").rstrip("/") + "/v1",
            api_key="ollama",
            model=provider.model,
            keep_alive=provider.keep_alive,
            extra=({"keep_alive": provider.keep_alive} if provider.keep_alive else {}),
        )
        super().__init__(provider, gen, model)
        self.root = self.provider.base_url[: -len("/v1")]
        self.name = f"ollama:{self.model}"

    def list_models(self) -> list[str]:
        import json
        import urllib.request

        with urllib.request.urlopen(f"{self.root}/api/tags", timeout=10) as resp:
            data = json.load(resp)
        return sorted(m["name"] for m in data.get("models", []))

    def pull(self, model: str | None = None) -> bool:
        """确保模型已下载。"""
        import json
        import urllib.request

        body = json.dumps({"name": model or self.model, "stream": False}).encode()
        req = urllib.request.Request(f"{self.root}/api/pull", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3600) as resp:
            resp.read()
        return True


def build_llm(
    settings: Settings,
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
) -> BaseLLM:
    """按配置/命令行覆盖构造 LLM 后端。"""
    gen = LLMCfg(**{**settings.llm.__dict__})
    if temperature is not None:
        gen.temperature = temperature

    pname = provider or settings.llm.provider
    pcfg = settings.provider(pname)
    # 命令行直接给了模型名，但预设里没这个 key 时，把模型名当自定义处理
    if pname not in settings.providers and model:
        pcfg = ProviderCfg(type="ollama" if pname == "ollama" else "openai", model=model)

    if pcfg.type == "ollama":
        return OllamaLLM(pcfg, gen, model)
    if pcfg.type == "openai":
        return OpenAICompatLLM(pcfg, gen, model)
    raise LLMError(f"未知的 provider 类型: {pcfg.type}（可用: ollama / openai）")


def probe_all(settings: Settings) -> list[dict[str, Any]]:
    """探测所有预置后端的可用性与模型列表，用于 CLI 展示。"""
    rows: list[dict[str, Any]] = []
    for name in settings.providers:
        try:
            llm = build_llm(settings, provider=name)
            ok, msg = llm.health()
            rows.append(
                {
                    "provider": name,
                    "type": settings.providers[name].type,
                    "ok": ok,
                    "detail": msg,
                    "models": llm.list_models() if ok else [],
                }
            )
        except Exception as e:  # noqa: BLE001
            rows.append(
                {
                    "provider": name,
                    "type": settings.providers[name].type,
                    "ok": False,
                    "detail": str(e)[:160],
                    "models": [],
                }
            )
    return rows
