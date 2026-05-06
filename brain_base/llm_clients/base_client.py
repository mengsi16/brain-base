"""
LLM 客户端基类。

参考 TradingAgents 的 llm_clients/base_client.py 模式。
"""

from abc import ABC, abstractmethod
from typing import Any, Optional
import warnings


def normalize_content(response):
    """将 LLM 响应 content 归一化为字符串"""
    content = response.content
    if isinstance(content, list):
        texts = [
            item.get("text", "") if isinstance(item, dict) and item.get("type") == "text"
            else item if isinstance(item, str) else ""
            for item in content
        ]
        response.content = "\n".join(t for t in texts if t)
    return response


class BaseLLMClient(ABC):
    """LLM 客户端抽象基类"""

    def __init__(self, model: str, base_url: Optional[str] = None, **kwargs):
        self.model = model
        self.base_url = base_url
        self.kwargs = kwargs

    def get_provider_name(self) -> str:
        provider = getattr(self, "provider", None)
        if provider:
            return str(provider)
        return self.__class__.__name__.removesuffix("Client").lower()

    def warn_if_unknown_model(self) -> None:
        if self.validate_model():
            return
        warnings.warn(
            f"Model '{self.model}' is not in the known model list for "
            f"provider '{self.get_provider_name()}'. Continuing anyway.",
            RuntimeWarning,
            stacklevel=2,
        )

    @abstractmethod
    def get_llm(self) -> Any:
        """返回配置好的 LLM 实例"""
        pass

    @abstractmethod
    def validate_model(self) -> bool:
        """验证模型是否受支持"""
        pass
