"""
Google Gemini LLM 客户端。
"""

from typing import Any, Optional

from .base_client import BaseLLMClient


class GoogleClient(BaseLLMClient):
    """Google Gemini 客户端"""

    KNOWN_MODELS = {"gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-2.5-pro", "gemini-2.5-flash"}

    def __init__(self, model: str, base_url: Optional[str] = None, **kwargs):
        super().__init__(model, base_url, **kwargs)
        self.provider = "google"

    def get_llm(self) -> Any:
        from langchain_google_genai import ChatGoogleGenerativeAI
        kwargs = dict(self.kwargs)
        return ChatGoogleGenerativeAI(model=self.model, **kwargs)

    def validate_model(self) -> bool:
        return self.model in self.KNOWN_MODELS
