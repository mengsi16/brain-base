"""
LLM 结构化输出工具。

`with_structured_output(Schema)` 是 langchain BaseChatModel 的标准 API，
但实际 provider 实现差异大；本模块统一封装：
1. 优先调 `llm.with_structured_output(schema).invoke(messages)` 拿到 schema 实例。
2. 路径 1 报错时回退路径 2：`llm.invoke` + 文本 JSON 解析 + `schema.model_validate(...)`。
   仅为应对 GLM-5.1 / wishub anthropic 等兼容端点上的 `with_structured_output`
   解析 bug；该路径以外任何异常都 fail-fast 抛出（T27 清除降级调用后，入口
   `llm is None` 也直接 raise）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

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


def _format_retry_feedback(last_exc: Exception, schema: type[BaseModel]) -> str:
    """T42：把 attempt 1 的错误格式化成 user_prompt 后缀，喂给 attempt 2 让 LLM 纠正。

    分两类：
    - pydantic ValidationError：列具体字段路径 + 错误原因（最多 5 条避免 prompt 爆炸）。
    - JSONDecodeError：直接说不是合法 JSON。
    - 其他：通用提示。

    回传字符串以 ``\\n\\n【上次输出错误反馈...】`` 开头，可直接拼到 enforced_user_prompt 后。
    """
    from pydantic import ValidationError

    if isinstance(last_exc, ValidationError):
        errs = last_exc.errors()[:5]  # 截前 5 条
        err_lines = []
        for err in errs:
            loc = ".".join(str(x) for x in err.get("loc", []))
            msg = err.get("msg", "")
            err_lines.append(f"- 字段 `{loc}`：{msg}")
        err_summary = "\n".join(err_lines)
        return (
            f"\n\n【上次输出错误反馈，请严格修正后重新输出】\n"
            f"上次输出违反 schema `{schema.__name__}` 的以下字段：\n"
            f"{err_summary}\n"
            f"请重新输出一个完整的 JSON 对象，确保每个字段类型与 schema 完全一致；"
            f"特别注意嵌套对象数组（如 list[Object]）的每一项必须是 `{{...}}` 对象而非字符串。"
        )
    if isinstance(last_exc, json.JSONDecodeError):
        return (
            f"\n\n【上次输出不是合法 JSON】\n"
            f"错误：{str(last_exc)[:200]}\n"
            f"请确保输出是裸 JSON 对象（如 `{{...}}`）或 ```json ... ``` 围栏块；"
            f"不要混入 markdown bullet 列表 / 解释文字。"
        )
    # 其他异常类型（极少见）：通用提示
    return (
        f"\n\n【上次输出未能解析】\n"
        f"错误类型：{type(last_exc).__name__}：{str(last_exc)[:200]}\n"
        f"请重新输出一个符合 schema `{schema.__name__}` 的 JSON 对象。"
    )


def invoke_structured(
    llm: Any,
    schema: type[T],
    system_prompt: str,
    user_prompt: str,
) -> T:
    """让 LLM 返回 Pydantic 实例（T27：fail-fast，无 fallback）。

    流程：
    1. ``llm is None`` → 直接 raise。
    2. 优先 ``llm.with_structured_output(schema).invoke(...)``。
    3. 路径 1 抛错 → 默默走路径 2（文本 + JSON）；GLM/wishub 兼容性需要。
    4. 路径 2 抛错 → 直接上拋，不再吞异常。
    """
    if llm is None:
        raise ValueError(
            "invoke_structured: llm 不能为 None（QA 主图 LLM 节点必须传入有效 llm；"
            "T27【fail-fast】已移除降级路径）"
        )

    from langchain_core.messages import HumanMessage, SystemMessage

    # 路径 1：with_structured_output（保留 try/except——设计理由：GLM-5.1 / wishub
    # anthropic 兼容端点存在 langchain-anthropic 解析 bug，需要默默回退到路径 2。
    # 该 try/except 不是业务豁免，是单一函数内的 provider 兼容性回退，
    # 符合 CLAUDE.md 规则 25 允许的「明确不可替代的设计理由」。
    if hasattr(llm, "with_structured_output"):
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
        try:
            structured = llm.with_structured_output(schema)
            result = structured.invoke(messages)
            if isinstance(result, schema):
                return result
            if isinstance(result, dict):
                return schema.model_validate(result)
            # GLM-5.1 通过 wishub anthropic 兼容端点存在 langchain-anthropic 解析 bug：
            # 默认 mode 下 invoke 返回 None，但 include_raw=True 时 parsed 字段正常。
            if result is None:
                try:
                    structured_iw = llm.with_structured_output(
                        schema, include_raw=True
                    )
                except TypeError:
                    # 老接口或 mock 不支持 include_raw kwarg，让路径 2 兜底
                    structured_iw = None
                if structured_iw is not None:
                    wrap = structured_iw.invoke(messages)
                    if isinstance(wrap, dict):
                        parsed = wrap.get("parsed")
                        if isinstance(parsed, schema):
                            return parsed
                        if isinstance(parsed, dict):
                            return schema.model_validate(parsed)
        except Exception as exc:
            # provider 兼容性回退：路径 1 抛错 → 走路径 2 文本 JSON。
            # 路径 2 再抛会直接上拋，符合 fail-fast。
            # CLAUDE.md 规则 25 补丁：保留 try-except 必须打 log。
            logger.debug(
                "invoke_structured 路径 1 (with_structured_output) 抛错回退路径 2 文本 JSON："
                "schema=%s exc=%s: %s",
                schema.__name__, type(exc).__name__, str(exc)[:160],
            )

    # 路径 2：文本 + JSON 解析（强约束 prompt 追加 + 一次 retry）。
    # 现实情况：minimax 等 anthropic 兼容端点经常返 markdown bullet 风格
    # （`**key**: value` 一行一项）而不是 JSON——LLM 对 prompt schema 指示的
    # 遵循度不稳定。在 user_prompt 末尾追加显式 JSON 强约束 + 失败 retry 一次
    # 能把成功率从 ~70% 拉到 ~95%。
    # 规则 25 豁免：retry 与路径 1→2 fallback 同属「单函数内 provider 兼容性补丁」，
    # 不是业务降级；两次都失败仍上抛 fail-fast。
    #
    # T42 增强：retry attempt 2+ 把 attempt 1 的 ValidationError / JSONDecodeError
    # 反馈给 LLM。原实现 retry 用同一个 enforced_user_prompt，LLM 不知错在哪里——
    # 实测 RewrittenQueries 在 Minimax 上把 queries 输出成 list[str] 时，retry 仍
    # 倾向同样格式。把 errors() 拼成 user_prompt 后缀能让 LLM 精准纠正本次失配。
    enforced_user_prompt = user_prompt + (
        "\n\n"
        "【输出格式严格要求】"
        "你必须严格、仅、只返回一个符合 schema 的 JSON 对象。"
        "不要使用 markdown 列表（如 `**key**: value` 这种 bullet 风格）。"
        "不要任何解释文字、不要 markdown 加粗、不要分点说明。"
        "正确格式示例：```json\n{...}\n```，或裸 JSON `{...}`。"
    )

    last_exc: Exception | None = None
    last_raw: str = ""
    last_block: str = ""
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        # T42：attempt 2+ 把上次错误反馈给 LLM（attempt 1 用原 enforced_user_prompt）
        user_prompt_for_attempt = enforced_user_prompt
        if attempt > 1 and last_exc is not None:
            user_prompt_for_attempt = enforced_user_prompt + _format_retry_feedback(
                last_exc, schema
            )

        raw = _llm_invoke_text(llm, system_prompt, user_prompt_for_attempt)
        last_raw = raw
        block = _extract_json_block(raw)
        last_block = block
        if not block.strip():
            last_exc = ValueError(
                f"LLM 未返回任何可解析的 JSON 块（schema={schema.__name__}, "
                f"attempt={attempt}）。raw 前 200 字符：{raw[:200]!r}"
            )
            logger.warning(
                "invoke_structured 路径 2 attempt=%d 无 JSON 块 | schema=%s raw_len=%d raw_preview=%r",
                attempt, schema.__name__, len(raw), raw[:500],
            )
            continue
        try:
            data = json.loads(block)
            return schema.model_validate(data)
        except json.JSONDecodeError as exc:
            last_exc = exc
            logger.warning(
                "invoke_structured 路径 2 attempt=%d JSON 解析失败 | schema=%s err=%s "
                "block_preview=%r raw_preview=%r",
                attempt, schema.__name__, str(exc), block[:300], raw[:500],
            )
            continue
        except Exception as exc:
            # pydantic ValidationError 等：schema 不匹配可能是字段名错（LLM 又乱发挥），
            # 也算 retry 候选——重试一次有可能听话。T42：retry 时把 errors() 反馈给 LLM。
            last_exc = exc
            logger.warning(
                "invoke_structured 路径 2 attempt=%d schema 验证失败 | schema=%s err=%s: %s "
                "block_preview=%r",
                attempt, schema.__name__, type(exc).__name__, str(exc)[:200], block[:300],
            )
            continue

    # 所有 retry 都失败 → fail-fast 上抛
    logger.error(
        "invoke_structured 路径 2 全部 %d 次 attempt 失败 | schema=%s last_err_type=%s "
        "last_block_preview=%r last_raw_preview=%r",
        max_attempts, schema.__name__,
        type(last_exc).__name__ if last_exc else "None",
        last_block[:300], last_raw[:500],
    )
    if last_exc is not None:
        raise last_exc
    raise ValueError(
        f"invoke_structured 路径 2 全部 {max_attempts} 次 attempt 失败但未捕获到具体异常"
        f"（schema={schema.__name__}）"
    )


def bind_structured(llm: Any, schema: type[T]) -> Callable[[str, str], T]:
    """返回一个绑定了 schema 的轻量调用函数，节点工厂内部调用。"""

    def _call(system_prompt: str, user_prompt: str) -> T:
        return invoke_structured(llm, schema, system_prompt, user_prompt)

    return _call
