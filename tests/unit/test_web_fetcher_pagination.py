# -*- coding: utf-8 -*-
"""T25-A search_google / search_bing 翻页参数测试。

只测 URL 拼装是否正确，不真起 playwright（mock _serp 拦截）。

T29: web_fetcher 迁移为 async API，search_google/search_bing 都是 coroutine，
mock 的 _serp 也是 async 。调用需要 ``asyncio.run`` 包装。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from brain_base.tools import web_fetcher


def _captured_urls() -> list[str]:
    """工厂：返回一个会捕获 url 的 async mock 实现。"""
    captured: list[str] = []

    async def _mock(*, url: str, **_kwargs):
        captured.append(url)
        return []

    return captured, _mock


# ---------------------------------------------------------------------------
# search_google
# ---------------------------------------------------------------------------


def test_search_google_default_page_no_start_param():
    """默认 page=1 不带 &start= 参数（向后兼容旧调用方）。"""
    urls, mock = _captured_urls()
    with patch.object(web_fetcher, "_serp", side_effect=mock):
        asyncio.run(web_fetcher.search_google("RAGFlow"))
    assert len(urls) == 1
    assert "&start=" not in urls[0]
    assert "q=RAGFlow" in urls[0]


def test_search_google_page_2_appends_start_10():
    """page=2 + num_results=10 → &start=10。"""
    urls, mock = _captured_urls()
    with patch.object(web_fetcher, "_serp", side_effect=mock):
        asyncio.run(web_fetcher.search_google("RAGFlow", num_results=10, page=2))
    assert "&start=10" in urls[0]


def test_search_google_page_3_appends_start_20():
    """page=3 + num_results=10 → &start=20。"""
    urls, mock = _captured_urls()
    with patch.object(web_fetcher, "_serp", side_effect=mock):
        asyncio.run(web_fetcher.search_google("RAGFlow", num_results=10, page=3))
    assert "&start=20" in urls[0]


def test_search_google_custom_num_results_with_page():
    """num_results=5 + page=2 → &start=5。"""
    urls, mock = _captured_urls()
    with patch.object(web_fetcher, "_serp", side_effect=mock):
        asyncio.run(web_fetcher.search_google("RAGFlow", num_results=5, page=2))
    assert "&start=5" in urls[0]
    assert "num=5" in urls[0]


# ---------------------------------------------------------------------------
# search_bing
# ---------------------------------------------------------------------------


def test_search_bing_default_page_no_first_param():
    """默认 page=1 不带 &first= 参数（向后兼容旧调用方）。"""
    urls, mock = _captured_urls()
    with patch.object(web_fetcher, "_serp", side_effect=mock):
        asyncio.run(web_fetcher.search_bing("openclaw"))
    assert len(urls) == 1
    assert "&first=" not in urls[0]
    assert "q=openclaw" in urls[0]
    assert "ensearch=1" in urls[0]


def test_search_bing_page_2_appends_first_11():
    """page=2 + num_results=10 → &first=11（Bing 翻页约定 first=offset+1）。"""
    urls, mock = _captured_urls()
    with patch.object(web_fetcher, "_serp", side_effect=mock):
        asyncio.run(web_fetcher.search_bing("openclaw", num_results=10, page=2))
    assert "&first=11" in urls[0]


def test_search_bing_page_3_appends_first_21():
    """page=3 + num_results=10 → &first=21。"""
    urls, mock = _captured_urls()
    with patch.object(web_fetcher, "_serp", side_effect=mock):
        asyncio.run(web_fetcher.search_bing("openclaw", num_results=10, page=3))
    assert "&first=21" in urls[0]
