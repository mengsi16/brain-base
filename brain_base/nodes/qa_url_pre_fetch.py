# -*- coding: utf-8 -*-
"""T47.2：url_pre_fetch 节点。

并发浅抓 ``user_urls`` 列表，输出 ``url_pre_fetch_content``（含 url/title/excerpt）
供 normalize 改写时作 LLM 上下文。

**设计原则**（契约 §2）：
1. **不写持久化**：不调 ingest，不入 Milvus（AGENTS.md 规则 41）
2. **不调 LLM**：只取原始 markdown 截断（默认前 2000 字符）
3. **软依赖降级**（CLAUDE.md 项目硬约束 14）：单 URL 失败 log warning，跳过该 URL；
   全部失败时 ``url_pre_fetch_content=[]``，normalize 退化为只看 question 改写
4. **抓取链路两段式**：
   - 路径 A：``try_raw_text``（github / gitlab / arxiv / RFC）— 同步 HTTP GET 短路
   - 路径 B：``fetch_page``（playwright）+ ``convert_html_to_markdown_readability`` 兜底

**节点位置**（T47.4 接入主图）：
``extract_urls (user_urls 非空) → url_pre_fetch → normalize``

**与 T46 ``fetch_user_urls`` 区别**（契约 §0 误判分析）：
- 旧 ``fetch_user_urls`` 是分流通道节点，浅抓后**直接走持久化** + 跳过意图识别
- 新 ``url_pre_fetch`` 只做改写上下文，**不写持久化**，意图识别 Agent 仍可主动深挖

契约引用：md/research/2026-05-17-t47-unified-intent-agent-contract.md §2
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from brain_base.config import GetInfoConfig

logger = logging.getLogger(__name__)


# 默认 excerpt 长度（契约 §2 输出表）；可按需通过工厂参数覆盖
DEFAULT_EXCERPT_CHARS = 2000
# 单 URL 抓取超时（playwright 路径）
DEFAULT_FETCH_TIMEOUT = 20.0


def create_url_pre_fetch(
    cfg: GetInfoConfig | None = None,
    *,
    excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
    fetch_timeout: float = DEFAULT_FETCH_TIMEOUT,
) -> Callable:
    """工厂函数：返回 url_pre_fetch async 节点。

    Args:
        cfg: ``GetInfoConfig`` 实例（暂未消费，预留给后续 timeout/retry 配置）。
        excerpt_chars: markdown 截断字符上限（默认 2000）。
        fetch_timeout: 单 URL playwright 抓取超时秒数。

    Returns:
        async 节点函数，输入 ``user_urls``，输出 ``url_pre_fetch_content``。
    """
    _cfg = cfg or GetInfoConfig()

    async def url_pre_fetch_node(state: dict[str, Any]) -> dict[str, Any]:
        user_urls = state.get("user_urls", []) or []
        if not user_urls:
            # 软依赖：user_urls 空时直接返回 []，normalize 退化为只看 question 改写
            return {"url_pre_fetch_content": []}

        # 并发抓所有 URL；return_exceptions 防止单 URL 异常炸整个 gather
        results = await asyncio.gather(
            *[_fetch_one(u, excerpt_chars, fetch_timeout) for u in user_urls],
            return_exceptions=True,
        )

        contents: list[dict[str, Any]] = []
        for url, r in zip(user_urls, results):
            if isinstance(r, Exception):
                logger.warning(
                    "url_pre_fetch fail url=%s err=%s",
                    url, f"{type(r).__name__}: {str(r)[:200]}",
                )
                continue
            if r is None:
                # _fetch_one 内部已 log；这里只过滤
                continue
            contents.append(r)

        return {"url_pre_fetch_content": contents}

    return url_pre_fetch_node


async def _fetch_one(
    url: str,
    excerpt_chars: int,
    fetch_timeout: float,
) -> dict[str, Any] | None:
    """单 URL 浅抓：try_raw_text 优先 → fetch_page + readability 兜底。

    返回 ``{url, title, markdown_excerpt}`` 或 None（抓取失败）。
    任何异常都被 ``asyncio.gather(return_exceptions=True)`` 捕获，工厂层 log。
    """
    # 路径 A：github / gitlab / arxiv / RFC 直取纯文本（T48.2 D3：纯 async 路径）
    # 用 try_raw_text_async 直接 await fetch_page，与主图共享同一 loop 单例稳定不重启
    try:
        from brain_base.tools.raw_text_extractor import try_raw_text_async
        raw = await try_raw_text_async(url)
    except Exception as exc:
        logger.warning(
            "url_pre_fetch raw_text fail url=%s err=%s",
            url, f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        raw = None

    if raw is not None:
        markdown = (raw.get("markdown") or "").strip()
        if markdown:
            return {
                "url": url,
                "title": (raw.get("title") or "")[:300],
                "markdown_excerpt": markdown[:excerpt_chars],
            }

    # 路径 B：playwright fetch + readability 兜底
    try:
        from brain_base.tools.web_fetcher import fetch_page
        fetched = await fetch_page(url, timeout=fetch_timeout)
    except Exception as exc:
        logger.warning(
            "url_pre_fetch playwright fail url=%s err=%s",
            url, f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        return None

    if not isinstance(fetched, dict):
        return None
    html = fetched.get("html") or ""
    title = fetched.get("title", "") or ""
    if not html.strip():
        return None

    try:
        from brain_base.tools.doc_converter_tool import (
            convert_html_to_markdown,
            convert_html_to_markdown_readability,
        )
        try:
            markdown = await asyncio.to_thread(
                convert_html_to_markdown_readability, html
            )
        except Exception:
            markdown = await asyncio.to_thread(convert_html_to_markdown, html)
    except Exception as exc:
        logger.warning(
            "url_pre_fetch html→md fail url=%s err=%s",
            url, f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        return None

    markdown = (markdown or "").strip()
    if not markdown:
        return None

    return {
        "url": url,
        "title": title[:300],
        "markdown_excerpt": markdown[:excerpt_chars],
    }
