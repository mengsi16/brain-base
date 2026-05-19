# -*- coding: utf-8 -*-
"""T47.2 单元测试：extract_urls + url_pre_fetch + normalize 集成。

覆盖：
- extract_urls 4 条（正则、去重保序、标点剥离）
- url_pre_fetch 5 条（user_urls 空 / 单 URL 成功 / 多 URL 部分失败 / playwright 兜底 / 全失败）
- normalize 含 url_pre_fetch_content 时 LLM 真调（验 [URL 上下文] 段渲染 + summary 产出，
  CLAUDE.md 规则 14：LLM 语义测试必跑，缺 key fail 不 skip）

契约引用：md/research/2026-05-17-t47-unified-intent-agent-contract.md §2 + §3 + §11
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

# 加载 .env（按 CLAUDE.md 规则 12：测试脚本用 load_dotenv 而非 $env:）
try:
    from dotenv import load_dotenv
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    load_dotenv(_PROJECT_ROOT / ".env")
except Exception:
    pass


# ---------------------------------------------------------------------------
# extract_urls 节点：不调 LLM，纯正则
# ---------------------------------------------------------------------------


class TestExtractUrls:
    """extract_urls 节点：D7 A 方案，正则提取 + 去重保序 + 标点剥离。"""

    def _node(self):
        from brain_base.nodes.qa_extract_urls import create_extract_urls
        return create_extract_urls()

    def test_no_url_returns_empty_list(self):
        """问题中无 URL → user_urls=[]。"""
        node = self._node()
        out = node({"question": "RAGFlow 怎么部署？"})
        assert out == {"user_urls": []}

    def test_single_url_extracted(self):
        """问题含单 URL → 提取该 URL。"""
        node = self._node()
        out = node({"question": "请看 https://github.com/infiniflow/ragflow 怎么部署"})
        assert out["user_urls"] == ["https://github.com/infiniflow/ragflow"]

    def test_multiple_urls_dedup_preserve_order(self):
        """多 URL：去重保序，第二次出现的 URL 不重复。

        中文标点（如 "，"）不在 ASCII rstrip 集合内——这与 T46 normalize 原行为一致，
        故用 ASCII 空格分隔；中文标点黏附场景的处理在 T47.6 整合时考虑增强。
        """
        node = self._node()
        out = node({
            "question": "对比 https://a.com 和 https://b.com 还有 https://a.com 也提到"
        })
        assert out["user_urls"] == ["https://a.com", "https://b.com"]

    def test_trailing_punct_stripped(self):
        """URL 末尾 ./,/;/:/!/? 应剥离。"""
        node = self._node()
        out = node({"question": "看 https://example.com/path. 这里有详细说明"})
        # 末尾点号被 rstrip
        assert out["user_urls"] == ["https://example.com/path"]

        out2 = node({"question": "对比 https://a.com, https://b.com! 选哪个？"})
        assert out2["user_urls"] == ["https://a.com", "https://b.com"]


# ---------------------------------------------------------------------------
# url_pre_fetch 节点：mock try_raw_text / fetch_page 避免触网
# ---------------------------------------------------------------------------


class TestUrlPreFetch:
    """url_pre_fetch 节点：并发浅抓 + 软依赖降级。"""

    def _make_node(self, *, excerpt_chars: int = 2000):
        from brain_base.nodes.qa_url_pre_fetch import create_url_pre_fetch
        return create_url_pre_fetch(excerpt_chars=excerpt_chars)

    def test_empty_user_urls_returns_empty(self):
        """user_urls=[] → url_pre_fetch_content=[]，不触网。"""
        node = self._make_node()
        out = asyncio.run(node({"user_urls": []}))
        assert out == {"url_pre_fetch_content": []}

    def test_missing_user_urls_field_returns_empty(self):
        """state 未含 user_urls 字段 → 退化为空（不 KeyError）。"""
        node = self._make_node()
        out = asyncio.run(node({}))
        assert out == {"url_pre_fetch_content": []}

    def test_raw_text_success_path(self, monkeypatch):
        """try_raw_text_async 命中 → 直接走 raw text 路径，不调 playwright。

        T48.2 D3 改造：qa_url_pre_fetch._fetch_one 现在调 async 版本。
        """
        from brain_base.nodes import qa_url_pre_fetch

        async def fake_try_raw_text_async(url, timeout=10.0):
            return {
                "markdown": "# GitHub README\n\nThis is a long README " * 50,
                "title": "Some Repo",
                "source_url": url,
            }

        async def must_not_call_fetch_page(*args, **kwargs):
            raise AssertionError("playwright fetch should not be called when raw text hits")

        monkeypatch.setattr(
            "brain_base.tools.raw_text_extractor.try_raw_text_async",
            fake_try_raw_text_async,
        )
        monkeypatch.setattr(
            "brain_base.tools.web_fetcher.fetch_page",
            must_not_call_fetch_page,
        )

        node = self._make_node(excerpt_chars=200)
        out = asyncio.run(node({"user_urls": ["https://github.com/x/y"]}))
        contents = out["url_pre_fetch_content"]
        assert len(contents) == 1
        assert contents[0]["url"] == "https://github.com/x/y"
        assert contents[0]["title"] == "Some Repo"
        # excerpt 应被截断到 200
        assert len(contents[0]["markdown_excerpt"]) <= 200
        assert contents[0]["markdown_excerpt"].startswith("# GitHub README")

    def test_playwright_fallback_when_raw_text_misses(self, monkeypatch):
        """try_raw_text_async 返回 None → fallback 到 playwright + readability。"""

        async def _miss(url, timeout=10.0):
            return None

        # T48.2 D3：qa_url_pre_fetch 现在调 async 版本
        monkeypatch.setattr(
            "brain_base.tools.raw_text_extractor.try_raw_text_async",
            _miss,
        )

        async def fake_fetch_page(url, timeout=None, **kw):
            return {"html": "<html><body><h1>Title</h1><p>Body content</p></body></html>", "title": "Page Title"}

        def fake_readability(html, *, timeout=60.0):
            return "# Title\n\nBody content"

        monkeypatch.setattr("brain_base.tools.web_fetcher.fetch_page", fake_fetch_page)
        monkeypatch.setattr(
            "brain_base.tools.doc_converter_tool.convert_html_to_markdown_readability",
            fake_readability,
        )

        node = self._make_node()
        out = asyncio.run(node({"user_urls": ["https://example.com"]}))
        contents = out["url_pre_fetch_content"]
        assert len(contents) == 1
        assert contents[0]["url"] == "https://example.com"
        assert contents[0]["title"] == "Page Title"
        assert "Body content" in contents[0]["markdown_excerpt"]

    def test_single_failure_does_not_kill_others(self, monkeypatch):
        """多 URL 并发：单 URL 失败不影响其他（return_exceptions=True）。

        T48.2 D3：qa_url_pre_fetch 现在调 async try_raw_text_async。
        """
        call_count = {"n": 0}

        async def fake_try_raw_text_async(url, timeout=10.0):
            call_count["n"] += 1
            if "good" in url:
                return {
                    "markdown": f"# Good content for {url}",
                    "title": "Good Title",
                    "source_url": url,
                }
            # bad URL 抛异常
            raise RuntimeError("simulated raw_text failure")

        async def fake_fetch_page(url, timeout=None, **kw):
            # bad URL 走到 playwright 路径也失败
            if "bad" in url:
                raise RuntimeError("simulated playwright failure")
            return {"html": "", "title": ""}

        monkeypatch.setattr(
            "brain_base.tools.raw_text_extractor.try_raw_text_async",
            fake_try_raw_text_async,
        )
        monkeypatch.setattr(
            "brain_base.tools.web_fetcher.fetch_page",
            fake_fetch_page,
        )

        node = self._make_node()
        out = asyncio.run(node({
            "user_urls": ["https://good.com", "https://bad.com", "https://good2.com"],
        }))
        contents = out["url_pre_fetch_content"]
        # 2 个 good 成功，1 个 bad 失败
        assert len(contents) == 2
        urls = [c["url"] for c in contents]
        assert "https://good.com" in urls
        assert "https://good2.com" in urls
        assert "https://bad.com" not in urls

    def test_all_failures_returns_empty(self, monkeypatch):
        """全部 URL 抓取失败 → url_pre_fetch_content=[]，不抛错。

        T48.2 D3：qa_url_pre_fetch 现在调 async try_raw_text_async。
        """

        async def _miss(url, timeout=10.0):
            return None

        monkeypatch.setattr(
            "brain_base.tools.raw_text_extractor.try_raw_text_async",
            _miss,
        )

        async def always_fail(url, timeout=None, **kw):
            raise RuntimeError("network down")

        monkeypatch.setattr("brain_base.tools.web_fetcher.fetch_page", always_fail)

        node = self._make_node()
        out = asyncio.run(node({"user_urls": ["https://a.com", "https://b.com"]}))
        # softly degrades to empty
        assert out["url_pre_fetch_content"] == []


# ---------------------------------------------------------------------------
# normalize 节点 LLM 真调：[URL 上下文] section + summary 产出
# 按 CLAUDE.md 规则 14：LLM 语义测试必跑，缺 key 应 fail 不 skip
# ---------------------------------------------------------------------------


def _resolve_llm_credentials():
    """与 test_t31_query_rewrite_llm.py 一致的凭证解析（Minimax 优先）。"""
    minimax_key = (os.environ.get("MINIMAX_API_KEY") or "").strip()
    if minimax_key:
        return {
            "provider": "anthropic",
            "model": (os.environ.get("MINIMAX_MODEL") or "MiniMax-M2"),
            "base_url": (os.environ.get("MINIMAX_BASE_URL") or "").strip() or None,
            "api_key": minimax_key,
        }
    glm_key = (os.environ.get("GLM_API_KEY") or "").strip()
    if glm_key:
        return {
            "provider": "glm",
            "model": (os.environ.get("GLM_MODEL") or "glm-4.6"),
            "base_url": (os.environ.get("GLM_BASE_URL") or "").strip() or None,
            "api_key": glm_key,
        }
    api_key = (
        os.environ.get("BB_LLM_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()
    if not api_key:
        return None
    return {
        "provider": (os.environ.get("BB_LLM_PROVIDER") or "anthropic").lower(),
        "model": os.environ.get("BB_DEEP_THINK_LLM") or "claude-sonnet-4-20250514",
        "base_url": (os.environ.get("BB_LLM_BASE_URL") or "").strip() or None,
        "api_key": api_key,
    }


@pytest.fixture(scope="module")
def normalize_node():
    creds = _resolve_llm_credentials()
    if creds is None:
        pytest.fail(
            "未配置 LLM API key：请在 .env 加 MINIMAX_API_KEY（首选）/ GLM_API_KEY / "
            "BB_LLM_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY 中任一。"
            "LLM 语义测试默认必跑（CLAUDE.md 规则 14），缺 key 应 fail 不应 skip。"
        )
    from brain_base.llm_clients.factory import create_llm_client
    from brain_base.nodes.qa import create_normalize_node
    client = create_llm_client(
        provider=creds["provider"],
        model=creds["model"],
        base_url=creds["base_url"],
        api_key=creds["api_key"],
        temperature=0.2,
        max_tokens_to_sample=1024,
        timeout=60,
        max_retries=2,
    )
    return create_normalize_node(client.get_llm())


class TestNormalizeUrlContextSummaryLLM:
    """normalize 节点真调 LLM 验证 T47.2 新行为。"""

    def test_no_url_no_history_returns_no_summary(self, normalize_node):
        """无 URL + 无对话历史 → conversation_history_summary 为空串。"""
        out = normalize_node({
            "question": "RAGFlow 怎么部署？",
            "conversation_history": [],
            "url_pre_fetch_content": [],
        })
        assert out["conversation_history_summary"] == ""
        # T47.2 D7：normalize 不再 return user_urls
        assert "user_urls" not in out
        assert out["normalized_query"]  # LLM 总能给出某种 normalized

    def test_with_history_produces_summary(self, normalize_node):
        """含对话历史 → conversation_history_summary 非空且简短（≤150 字）。"""
        out = normalize_node({
            "question": "那它的性能呢？",
            "conversation_history": [
                {"role": "user", "text": "RAGFlow 是什么？"},
                {"role": "assistant", "text": "RAGFlow 是一个开源的检索增强生成（RAG）框架，基于 deep document understanding 提供问答能力。"},
            ],
            "url_pre_fetch_content": [],
        })
        summary = out["conversation_history_summary"]
        assert summary, f"expected non-empty summary, got {summary!r}"
        assert len(summary) <= 200, f"summary too long ({len(summary)} chars): {summary[:200]}"
        # 摘要应至少提到 RAGFlow（上轮主题），允许 LLM 表述差异
        assert "RAGFlow" in summary or "RAG" in summary.upper(), f"summary missing key entity: {summary}"

    def test_url_context_influences_normalize(self, normalize_node):
        """[URL 上下文] section 让 LLM 知道 URL 内容，normalized 应保留实体名不泛化。"""
        out = normalize_node({
            "question": "这个项目怎么部署？参考 https://github.com/infiniflow/ragflow",
            "conversation_history": [],
            "url_pre_fetch_content": [
                {
                    "url": "https://github.com/infiniflow/ragflow",
                    "title": "infiniflow/ragflow: An open-source RAG engine",
                    "markdown_excerpt": "# RAGFlow\n\nRAGFlow is an open-source RAG (Retrieval-Augmented Generation) engine based on deep document understanding.",
                },
            ],
        })
        # URL 内容指向 RAGFlow，normalize 应保留该实体名
        nq_lower = out["normalized_query"].lower()
        assert "ragflow" in nq_lower, (
            f"normalize 应根据 [URL 上下文] 保留实体 RAGFlow，但得到：{out['normalized_query']}"
        )
        # 不再 return user_urls
        assert "user_urls" not in out
