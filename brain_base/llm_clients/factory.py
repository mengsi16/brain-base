"""
LLM 客户端工厂。

参考 TradingAgents 的 llm_clients/factory.py 模式。
按 provider 惰性导入对应客户端模块。
"""

from typing import Optional

from .base_client import BaseLLMClient

# 使用 OpenAI 兼容 API 的 provider
_OPENAI_COMPATIBLE = (
    "openai", "xai", "deepseek", "qwen", "glm", "ollama", "openrouter",
)


def create_llm_client(
    provider: str,
    model: str,
    base_url: Optional[str] = None,
    **kwargs,
) -> BaseLLMClient:
    """创建 LLM 客户端

    provider 模块惰性导入，避免导入时拉入重量级 SDK。

    Args:
        provider: LLM provider 名称
        model: 模型名称
        base_url: 可选 API 端点 URL
        **kwargs: provider 特定参数

    Returns:
        配置好的 BaseLLMClient 实例
    """
    provider_lower = provider.lower()

    if provider_lower in _OPENAI_COMPATIBLE:
        from .openai_client import OpenAIClient
        return OpenAIClient(model, base_url, provider=provider_lower, **kwargs)

    if provider_lower == "anthropic":
        from .anthropic_client import AnthropicClient
        return AnthropicClient(model, base_url, **kwargs)

    if provider_lower == "google":
        from .google_client import GoogleClient
        return GoogleClient(model, base_url, **kwargs)

    if provider_lower == "azure":
        from .azure_client import AzureOpenAIClient
        return AzureOpenAIClient(model, base_url, **kwargs)

    raise ValueError(f"不支持的 LLM provider: {provider}")
