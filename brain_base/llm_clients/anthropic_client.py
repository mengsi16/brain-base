"""
Anthropic LLM 客户端。
"""

from typing import Any, Optional

from .base_client import BaseLLMClient


class AnthropicClient(BaseLLMClient):
    """Anthropic Claude 客户端"""

    KNOWN_MODELS = {
        "claude-3-5-sonnet", "claude-3-5-haiku", "claude-3-opus",
        "claude-sonnet-4", "claude-sonnet-4-20250514", "claude-haiku-4",
        # MiniMax 通过 Anthropic 兼容端点支持的模型
        "MiniMax-M2.7", "MiniMax-M2.1",
    }

    def __init__(self, model: str, base_url: Optional[str] = None, **kwargs):
        super().__init__(model, base_url, **kwargs)
        self.provider = "anthropic"

    def get_llm(self) -> Any:
        from langchain_anthropic import ChatAnthropic
        kwargs = dict(self.kwargs)
        if self.base_url:
            # langchain-anthropic 新版用 base_url；老版本支持的 anthropic_api_url 已弃用。
            kwargs["base_url"] = self.base_url
        return ChatAnthropic(model=self.model, **kwargs)

    def validate_model(self) -> bool:
        return self.model in self.KNOWN_MODELS
