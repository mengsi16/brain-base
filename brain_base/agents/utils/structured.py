"""
LLM 结构化输出工具。

`with_structured_output(Schema)` 是 langchain BaseChatModel 的标准 API，
但实际 provider 实现差异大；本模块统一封装：
1. 优先调 `llm.with_structured_output(schema).invoke(messages)` 拿到 schema 实例。
2. 不支持时退化为 `llm.invoke` + 文本 JSON 解析 + `schema.model_validate(...)`。
3. 解析仍失败时调用降级回调（节点 llm=None 时的兜底逻辑）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


def _extract_json_block(text: str) -> str:
    """从 LLM 输出里抠出 JSON 字符串。

    兼容三种形态：纯 JSON / `\u200d```json … ```` / 前后含多余说明文本。
    """
    text = (text or "").strip()
    if not text:
        return ""
    fence = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    # 抠首尾大括号
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        return text[first : last + 1]
    return text


def _coerce_content_to_text(content: Any) -> str:
    """把 langchain `AIMessage.content` 归一化为 str。

    Anthropic / MiniMax (Anthropic 兼容) 返回的 content 可能是 `list[dict]`——
    典型形态为 ``[{"type": "text", "text": "..."}, {"type": "thinking", ...}]``。
    直接返回 list 会让下游 ``text.strip()`` 抛 ``'list' object has no attribute
    'strip'``；这里把 list 里的所有 text 块拼起来。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                # LangChain Anthropic block 格式：{"type": "text", "text": "..."}
                # 也见过 {"type": "thinking", "thinking": "..."}——思考块对结构化
                # 解析无价值，只保留 text/output_text。
                text_val = block.get("text") or block.get("output_text") or ""
                if text_val:
                    parts.append(str(text_val))
        return "".join(parts)
    return str(content) if content is not None else ""


def _llm_invoke_text(llm: Any, system_prompt: str, user_prompt: str) -> str:
    """裸调用 LLM，返回 content 字符串。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    response = llm.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
    )
    content = response.content if hasattr(response, "content") else response
    return _coerce_content_to_text(content)


def invoke_structured(
    llm: Any,
    schema: type[T],
    system_prompt: str,
    user_prompt: str,
    fallback: Callable[[], T] | None = None,
) -> T:
    """让 LLM 返回 Pydantic 实例。

    流程：
    1. 优先 `llm.with_structured_output(schema).invoke(...)`。
    2. 失败 → 文本调用 + JSON 解析 + `schema.model_validate(...)`。
    3. 仍失败 → 调用 fallback（若提供）；fallback 也未提供则 raise。
    """
    if llm is None:
        if fallback is not None:
            return fallback()
        raise ValueError("invoke_structured: llm 为 None 且无 fallback")

    from langchain_core.messages import HumanMessage, SystemMessage

    # 路径 1：with_structured_output
    if hasattr(llm, "with_structured_output"):
        try:
            structured = llm.with_structured_output(schema)
            result = structured.invoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_prompt),
                ]
            )
            if isinstance(result, schema):
                return result
            if isinstance(result, dict):
                return schema.model_validate(result)
        except Exception:
            # 不在这里 swallow——继续走路径 2，最终若都失败再决定 raise/fallback
            pass

    # 路径 2：文本 + JSON 解析
    try:
        raw = _llm_invoke_text(llm, system_prompt, user_prompt)
        block = _extract_json_block(raw)
        data = json.loads(block)
        return schema.model_validate(data)
    except (json.JSONDecodeError, ValidationError, ValueError):
        if fallback is not None:
            return fallback()
        raise


def bind_structured(llm: Any, schema: type[T]) -> Callable[[str, str], T]:
    """返回一个绑定了 schema 的轻量调用函数，节点工厂内部调用。"""

    def _call(system_prompt: str, user_prompt: str) -> T:
        return invoke_structured(llm, schema, system_prompt, user_prompt)

    return _call
