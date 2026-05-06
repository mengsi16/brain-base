"""
OpenAI 兼容 LLM 客户端。

支持 OpenAI / xAI / DeepSeek / Qwen / GLM / Ollama / OpenRouter。
"""

from typing import Any, Optional

from langchain_openai import ChatOpenAI

from .base_client import BaseLLMClient, normalize_content


class NormalizedChatOpenAI(ChatOpenAI):
    """ChatOpenAI with normalized content output"""

    def invoke(self, input, config=None, **kwargs):
        return normalize_content(super().invoke(input, config, **kwargs))

    def with_structured_output(self, schema, *, method=None, **kwargs):
        if method is None:
            method = "function_calling"
        return super().with_structured_output(schema, method=method, **kwargs)


class OpenAIClient(BaseLLMClient):
    """OpenAI 兼容客户端"""

    # 已知模型列表（部分示例）
    KNOWN_MODELS = {
        "openai": {"gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o1", "o1-mini", "o3", "o3-mini"},
        "deepseek": {"deepseek-chat", "deepseek-reasoner"},
        "qwen": {"qwen-max", "qwen-plus", "qwen-turbo"},
        "glm": {"glm-4", "glm-4-flash"},
        "xai": {"grok-2", "grok-3"},
        "ollama": set(),  # 本地模型，不验证
        "openrouter": set(),
    }

    def __init__(self, model: str, base_url: Optional[str] = None, provider: str = "openai", **kwargs):
        super().__init__(model, base_url, **kwargs)
        self.provider = provider

    def get_llm(self) -> Any:
        kwargs = dict(self.kwargs)
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return NormalizedChatOpenAI(model=self.model, **kwargs)

    def validate_model(self) -> bool:
        known = self.KNOWN_MODELS.get(self.provider, set())
        if not known:
            return True  # 本地/openrouter 不验证
        return self.model in known
