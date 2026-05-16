# -*- coding: utf-8 -*-
"""T46: 工具注册表 + 工具规范。

迭代多跳路径（hop_planner → tool_selector → tool_executor）通过
TOOL_REGISTRY 按名称 dispatch 到具体工具函数。

设计权衡见契约文档 §7：
- web_search / fetch_url 为 async（内部走 playwright fetch）
- raw_text / local_search 为 sync（tool_executor 用 asyncio.to_thread 包装）
- arxiv_pdf 待 T47 实现后注册

工具函数统一返回 ToolResult dict（evidence + markdown + source_url + title），
供 tool_executor 内 LLM 提取 HopObservation 使用。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ToolSpec 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """单个工具的注册规范。

    Attributes:
        name: 工具名，与 TOOL_REGISTRY key 一致。
        description: 自然语言描述，注入 hop_planner prompt 供 LLM 选择。
        requires: 该工具依赖的基础设施标记（与 infra_status key 对齐）。
        gpu: 是否需要 GPU（影响并发约束判断）。
        parallel_ok: 是否允许并行调用。
        is_async: fn 是否为 async 函数。True → tool_executor 直接 await；
                  False → tool_executor 用 asyncio.to_thread 包装。
        fn: 工具函数。签名见各函数 docstring。
    """
    name: str
    description: str
    requires: list[str] = field(default_factory=list)
    gpu: bool = False
    parallel_ok: bool = True
    is_async: bool = True
    fn: Callable[..., Any] | None = None


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


async def execute_web_search(
    tool_args: dict[str, Any],
    llm: Any,
    cfg: Any,
) -> dict[str, Any]:
    """Google + Bing 搜索 → 取 top URL → fetch + evaluate → 返回最佳证据。

    简化版 SERP 流水（不经过 search_strategy 节点，见契约 §10.1）：
    1. 用 tool_args["query"] 单 query 搜索
    2. 取 top N URL（N = min(3, serp_results)）
    3. 对每个 URL 调 _fetch_and_evaluate
    4. 返回 score 最高且 whether_in=True 的候选

    tool_args:
        query: str — 搜索关键词
    """
    from brain_base.nodes.qa_get_info import _fetch_and_evaluate
    from brain_base.tools.web_fetcher import search_bing, search_google

    query = tool_args.get("query", "")
    if not query:
        return {"error": "empty query", "markdown": "", "source_url": "", "title": ""}

    # 并行搜 Google + Bing 各 1 页
    google_task = search_google(query, num_results=5, page=1)
    bing_task = search_bing(query, num_results=5, page=1)
    results = await asyncio.gather(google_task, bing_task, return_exceptions=True)

    # 合并去重
    seen: set[str] = set()
    urls: list[dict[str, str]] = []
    for res in results:
        if isinstance(res, Exception) or not isinstance(res, list):
            continue
        for item in res:
            u = item.get("url", "")
            if u and u not in seen:
                seen.add(u)
                urls.append(item)

    if not urls:
        return {"error": "no serp results", "markdown": "", "source_url": "", "title": ""}

    # 取 top 3 尝试 fetch + evaluate
    best: dict[str, Any] | None = None
    for item in urls[:3]:
        try:
            candidate = await _fetch_and_evaluate(
                item.get("url", ""), query, llm, cfg,
                title_hint=item.get("title", ""),
                snippet=item.get("snippet", ""),
            )
            if candidate is None:
                continue
            if not candidate.get("whether_in", False):
                continue
            if best is None or candidate.get("score", 0) > best.get("score", 0):
                best = candidate
        except Exception as exc:
            logger.warning(
                "web_search fetch fail: url=%s err=%s",
                item.get("url", ""), str(exc)[:200],
            )

    if best:
        return {
            "markdown": best.get("markdown", ""),
            "source_url": best.get("url", ""),
            "title": best.get("title", ""),
            "summary": best.get("summary", ""),
        }
    return {"error": "all fetches failed or irrelevant", "markdown": "", "source_url": "", "title": ""}


async def execute_fetch_url(
    tool_args: dict[str, Any],
    llm: Any,
    cfg: Any,
) -> dict[str, Any]:
    """抓取指定 URL → HTML → Markdown → LLM 评估 → 返回证据。

    与 fetch_user_urls 共享 _fetch_and_evaluate helper。

    tool_args:
        url: str — 目标 URL
        question: str — 用户原始问题（用于 LLM 评估上下文）
    """
    from brain_base.nodes.qa_get_info import _fetch_and_evaluate

    url = tool_args.get("url", "")
    question = tool_args.get("question", "")
    if not url:
        return {"error": "empty url", "markdown": "", "source_url": "", "title": ""}

    candidate = await _fetch_and_evaluate(url, question, llm, cfg)
    if candidate is None:
        return {"markdown": "", "source_url": url, "title": "", "note": "hash_hit_skip"}
    return {
        "markdown": candidate.get("markdown", ""),
        "source_url": candidate.get("url", ""),
        "title": candidate.get("title", ""),
        "summary": candidate.get("summary", ""),
    }


def execute_raw_text(
    tool_args: dict[str, Any],
    llm: Any,
    cfg: Any,
) -> dict[str, Any]:
    """GitHub / GitLab / arXiv abs / RFC 直取纯文本。

    内部走 raw_text_extractor.try_raw_text(url)，按 URL host 自动路由。
    sync 函数——tool_executor 用 asyncio.to_thread 包装。

    tool_args:
        url: str — 目标 URL
    """
    from brain_base.tools.raw_text_extractor import try_raw_text

    url = tool_args.get("url", "")
    if not url:
        return {"error": "empty url", "markdown": "", "source_url": "", "title": ""}

    result = try_raw_text(url)
    if result is None:
        return {"error": "unsupported url or fetch failed", "markdown": "", "source_url": url, "title": ""}
    return {
        "markdown": result.get("markdown", ""),
        "source_url": result.get("source_url", url),
        "title": result.get("title", ""),
    }


def execute_local_search(
    tool_args: dict[str, Any],
    llm: Any,
    cfg: Any,
) -> dict[str, Any]:
    """Milvus 本地知识库混合检索。

    sync 函数——tool_executor 用 asyncio.to_thread 包装。

    tool_args:
        query: str — 检索关键词
        top_k: int — 返回条数（默认 8）
    """
    from brain_base.tools.milvus_client import multi_query_search

    query = tool_args.get("query", "")
    top_k = int(tool_args.get("top_k", 8))
    if not query:
        return {"error": "empty query", "markdown": "", "source_url": "", "title": ""}

    result = multi_query_search(
        queries=[query],
        top_k_per_query=max(top_k, 12),
        final_k=top_k,
    )
    candidates = result.get("candidates", []) or []
    if not candidates:
        return {"markdown": "", "source_url": "local_milvus", "title": ""}

    # 拼接 top candidates 的 text 作为 markdown evidence
    parts: list[str] = []
    for c in candidates[:top_k]:
        text = c.get("text", "") or c.get("content", "")
        doc_id = c.get("doc_id", "")
        if text:
            parts.append(f"[{doc_id}] {text}")

    return {
        "markdown": "\n\n---\n\n".join(parts),
        "source_url": "local_milvus",
        "title": f"Milvus search: {query[:50]}",
    }


# ---------------------------------------------------------------------------
# TOOL_REGISTRY
# ---------------------------------------------------------------------------


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "web_search": ToolSpec(
        name="web_search",
        description="Google + Bing 搜索，适用于需要最新网络信息的场景",
        requires=["playwright"],
        gpu=False,
        parallel_ok=True,
        is_async=True,
        fn=execute_web_search,
    ),
    "fetch_url": ToolSpec(
        name="fetch_url",
        description="抓取指定 URL 内容（HTML → Markdown → LLM 评估）",
        requires=["playwright"],
        gpu=False,
        parallel_ok=True,
        is_async=True,
        fn=execute_fetch_url,
    ),
    "raw_text": ToolSpec(
        name="raw_text",
        description="GitHub / GitLab / arXiv abs / RFC 直取纯文本（按 URL host 自动路由）",
        requires=[],
        gpu=False,
        parallel_ok=True,
        is_async=False,
        fn=execute_raw_text,
    ),
    "local_search": ToolSpec(
        name="local_search",
        description="Milvus 本地知识库混合检索，适用于已入库文档的精确查找",
        requires=["milvus"],
        gpu=False,
        parallel_ok=True,
        is_async=False,
        fn=execute_local_search,
    ),
    # arxiv_pdf: T47 待实现后注册
    # "arxiv_pdf": ToolSpec(
    #     name="arxiv_pdf",
    #     description="arXiv PDF 全文（MinerU 解析），适用于需要论文完整内容的场景",
    #     requires=["mineru"],
    #     gpu=True,
    #     parallel_ok=False,
    #     is_async=False,
    #     fn=None,  # T47
    # ),
}
