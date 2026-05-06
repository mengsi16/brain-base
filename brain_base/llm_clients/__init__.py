"""
brain_base LLM 客户端包。

参考 TradingAgents 的 llm_clients/ 模式。
支持多 provider（OpenAI / Anthropic / Google / DeepSeek / Ollama 等）。
"""

from .base_client import BaseLLMClient
from .factory import create_llm_client

__all__ = ["BaseLLMClient", "create_llm_client"]
