"""
Azure OpenAI LLM 客户端。
"""

from typing import Any, Optional

from .base_client import BaseLLMClient


class AzureOpenAIClient(BaseLLMClient):
    """Azure OpenAI 客户端"""

    def __init__(self, model: str, base_url: Optional[str] = None, **kwargs):
        super().__init__(model, base_url, **kwargs)
        self.provider = "azure"

    def get_llm(self) -> Any:
        from langchain_openai import AzureChatOpenAI
        kwargs = dict(self.kwargs)
        if self.base_url:
            kwargs["azure_endpoint"] = self.base_url
        return AzureChatOpenAI(model=self.model, **kwargs)

    def validate_model(self) -> bool:
        return True  # Azure 部署名自定义，不验证
