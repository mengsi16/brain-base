# -*- coding: utf-8 -*-
"""QA 公共 fetch + evaluate helper（T47.6 清退后）。

T47.6 历史：本文件原含 9 个 T46 parallel 通道 + direct_url 通道节点
（merge_search_keywords / search_web_dual / fetch_extract_one × N /
barrier_extract / fanout_extract_dispatcher / search_strategy /
fetch_user_urls 等），T47.4 已将通道从主图拔除，T47.6 已删除函数本体。

本文件 T47.6 后只保留 2 个公共 helper：

- ``_fetch_extract_user_prompt``：T24 风格上下文继承 prompt 拼装
- ``_fetch_and_evaluate``：URL → fetch → markdown → dedup → LLM 评估 → candidate dict

调用方：
- ``brain_base/nodes/qa_tools.py``：``web_search`` / ``fetch_url`` TOOL_REGISTRY 工具
  在 T47 intent_executor 调度下使用本 helper 完成单 URL 抓取 + 评估。

设计参考：契约 ``md/research/2026-05-17-t47-unified-intent-agent-contract.md`` §6（工具实现）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from brain_base.agents.schemas import FetchExtractResult
from brain_base.agents.utils.structured import invoke_structured
from brain_base.config import GetInfoConfig
from brain_base.nodes._hash import compute_body_sha256
from brain_base.prompts.get_info_prompts import FETCH_EXTRACT_SYSTEM_PROMPT
from brain_base.tools.doc_converter_tool import (
    convert_html_to_markdown,
    convert_html_to_markdown_readability,
)
from brain_base.tools.milvus_client import hash_lookup
from brain_base.tools.web_fetcher import fetch_page

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# user_prompt 拼装（T24 风格上下文继承）
# ---------------------------------------------------------------------------


def _fetch_extract_user_prompt(
    *,
    question: str,
    sub_questions: list[str],
    title: str,
    snippet: str,
    from_engines: list[str],
    from_queries: list[int],
    markdown: str,
) -> str:
    """T24 风格上下文继承的 user_prompt 拼装。

    多跳模式（``len(sub_questions) > 1``）：塞 question + 子问题列表 [s_idx]
    单跳模式：仅塞 question + SERP 元数据。
    """
    engines_str = ", ".join(from_engines) if from_engines else "未知"
    queries_str = (
        ", ".join(f"q{i}" for i in from_queries) if from_queries else "未知"
    )

    if len(sub_questions) > 1:
        sub_list = "\n".join(f"  [s{i}] {sq}" for i, sq in enumerate(sub_questions))
        return (
            f"用户原始问题：{question}\n\n"
            f"子问题列表（按 sub_idx 索引）：\n{sub_list}\n\n"
            f"SERP 召回背景：从 {engines_str} 召回，命中关键词组 [{queries_str}]\n"
            f"SERP 标题：{title}\n"
            f"SERP 摘要：{snippet}\n\n"
            f"完整 markdown 内容（已清洗）：\n{markdown}"
        )

    return (
        f"用户问题：{question}\n\n"
        f"SERP 召回背景：从 {engines_str} 召回\n"
        f"SERP 标题：{title}\n"
        f"SERP 摘要：{snippet}\n\n"
        f"完整 markdown 内容（已清洗）：\n{markdown}"
    )


# ---------------------------------------------------------------------------
# 公共 helper：_fetch_and_evaluate
# ---------------------------------------------------------------------------


async def _fetch_and_evaluate(
    url: str,
    question: str,
    llm: Any,
    cfg: GetInfoConfig,
    *,
    title_hint: str = "",
    sub_questions: list[str] | None = None,
    snippet: str = "",
    from_engines: list[str] | None = None,
    from_queries: list[int] | None = None,
) -> dict | None:
    """URL → fetch → markdown → dedup → LLM 评估 → candidate dict。

    T47 intent_executor 调度下 ``web_search`` / ``fetch_url`` 工具共用本 helper
    完成单 URL 抓取 + LLM 评估。SERP 元数据（from_engines / from_queries / snippet）
    为可选——不传时 user_prompt 用简化格式。

    Returns:
        candidate dict（含 url / title / markdown / score / whether_in 等）。
        hash 命中时返回 None（内容已在 KB 中，无需再入库）。

    Raises:
        RuntimeError: fetch 失败、markdown 为空等——调用方自行捕获做隔离。
    """
    # Step 1: fetch HTML
    fetched = await fetch_page(url)
    html = (fetched.get("html") or "") if isinstance(fetched, dict) else ""
    if not html.strip():
        raise RuntimeError("empty html")

    # Step 2: HTML → markdown (Readability 主, MinerU 兜底)
    try:
        markdown = await asyncio.to_thread(
            convert_html_to_markdown_readability, html
        )
    except Exception:
        markdown = await asyncio.to_thread(convert_html_to_markdown, html)

    if not markdown or not markdown.strip():
        raise RuntimeError("empty markdown")

    # Step 3: 算内容指纹 + raw 目录去重查询
    content_sha256 = await asyncio.to_thread(compute_body_sha256, markdown)
    lookup_result = await asyncio.to_thread(hash_lookup, content_sha256)
    resolved_title = title_hint or (
        fetched.get("title", "") if isinstance(fetched, dict) else ""
    )

    # Step 4: 命中分支 → 返回 None，调用方决定短路行为
    if lookup_result.get("status") == "hit":
        matches = lookup_result.get("matches") or []
        existing_doc_id = matches[0].get("doc_id", "") if matches else ""
        logger.info(
            "_fetch_and_evaluate: hash hit, skip. sha256=%s url=%s existing_doc_id=%s",
            content_sha256, url, existing_doc_id,
        )
        return None

    # Step 5: LLM 评估
    _from_engines = from_engines or []
    _from_queries = from_queries or []
    _sub_questions = sub_questions or []

    user_prompt = _fetch_extract_user_prompt(
        question=question,
        sub_questions=_sub_questions,
        title=title_hint,
        snippet=snippet,
        from_engines=list(_from_engines),
        from_queries=list(_from_queries),
        markdown=markdown,
    )
    result: FetchExtractResult = await asyncio.to_thread(
        invoke_structured,
        llm,
        FetchExtractResult,
        FETCH_EXTRACT_SYSTEM_PROMPT,
        user_prompt,
    )

    # Step 6: 组装候选 dict
    return {
        "url": url,
        "title": resolved_title,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "markdown": markdown,
        "content_sha256": content_sha256,
        "from_engines": list(_from_engines),
        "from_queries": list(_from_queries),
        "score": int(result.score),
        "type": result.type,
        "summary": result.summary,
        "keywords": list(result.keywords),
        "whether_in": bool(result.whether_in),
        "reason": result.reason,
    }
