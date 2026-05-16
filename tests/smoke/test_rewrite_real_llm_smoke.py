# -*- coding: utf-8 -*-
"""T42 真调回归：验证 REWRITE_SYSTEM_PROMPT + invoke_structured 真调 LLM
能稳定输出符合 RewrittenQueries schema 的结构。

**默认必跑**（按 CLAUDE.md 规则 14：LLM 测试不跳过）。跑前在 .env 配任一 key：
    MINIMAX_API_KEY  (首选，默认 provider)
    GLM_API_KEY      (可选)
    BB_LLM_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY

与 `tests/smoke/test_rewritten_queries_contract.py` 配对：
- 那个：固定错误 payload → 应被 schema 拒绝（验 schema 严格性）
- 本文件：真调 LLM → 必须返回符合 schema 的 payload（验 prompt + retry feedback 有效）

T42 修复后两测都应 pass：
- prompt 加 schema 段让 LLM attempt 1 倾向正确格式
- retry feedback 让 attempt 2 在 attempt 1 失配时能精准纠正
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# 加载 .env（按规则 12：测试脚本用 load_dotenv 而非 $env:）
try:
    from dotenv import load_dotenv

    _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    load_dotenv(_PROJECT_ROOT / ".env")
except Exception:
    pass

from brain_base.agents.schemas import RewrittenQueries
from brain_base.agents.utils.structured import invoke_structured
from brain_base.prompts.qa_prompts import REWRITE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# LLM 凭证（与 test_t31/t32 同款模板，T43 抽 fixture 后可复用 conftest.real_llm）
# ---------------------------------------------------------------------------


def _resolve_llm_credentials() -> dict | None:
    """从 env 找 LLM 凭证；MINIMAX 优先，GLM 次之，BB_LLM_* 兜底。"""
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
        "model": (
            os.environ.get("BB_DEEP_THINK_LLM")
            or "claude-sonnet-4-20250514"
        ),
        "base_url": (os.environ.get("BB_LLM_BASE_URL") or "").strip() or None,
        "api_key": api_key,
    }


def _build_llm():
    creds = _resolve_llm_credentials()
    if creds is None:
        return None
    from brain_base.llm_clients.factory import create_llm_client

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
    return client.get_llm()


@pytest.fixture(scope="module")
def real_llm():
    """module-scope 真调 LLM。缺 key fail 不 skip（规则 14）。"""
    llm = _build_llm()
    if llm is None:
        pytest.fail(
            "未配置 LLM API key：请在 .env 加 MINIMAX_API_KEY（首选）/ GLM_API_KEY / "
            "BB_LLM_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY 中任一。"
            "LLM 测试是核心必跑（CLAUDE.md 规则 14）不允许跳过。"
        )
    return llm


# ---------------------------------------------------------------------------
# Case 1：CTM 案——T42 bug 复现入口
# ---------------------------------------------------------------------------


@pytest.mark.requires_llm
def test_rewrite_real_llm_ctm_case(real_llm):
    """T42 bug 复现入口：CTM 这种全英文专有名词 + 中文翻译的混合 case，
    LLM 真调输出必须符合 RewrittenQueries schema（queries 是对象数组而非字符串数组）。
    """
    result = invoke_structured(
        real_llm,
        RewrittenQueries,
        REWRITE_SYSTEM_PROMPT,
        "用户问题：Continuous Thought Machine（CTM）连续思维机是什么？",
    )

    assert isinstance(result, RewrittenQueries), (
        f"应返回 RewrittenQueries 实例：{type(result)}"
    )

    # queries 应为 1-6 项的对象列表，每项含 text + layer
    assert 1 <= len(result.queries) <= 6, (
        f"queries 数量应在 1-6 之间：{len(result.queries)}"
    )
    for i, q in enumerate(result.queries):
        assert hasattr(q, "text") and isinstance(q.text, str) and q.text.strip(), (
            f"queries[{i}].text 应为非空字符串：{q!r}"
        )
        assert hasattr(q, "layer") and q.layer in ("L0", "L1", "L2", "L3"), (
            f"queries[{i}].layer 应为 L0-L3 之一：{q.layer!r}"
        )

    # 应至少含 1 条 L0 原句（按 prompt 规则强制保留）
    assert any(q.layer == "L0" for q in result.queries), (
        f"必须保留 1 条 L0 原句：{[(q.text, q.layer) for q in result.queries]}"
    )

    # lexical_query 长度 2-30 字
    assert 2 <= len(result.lexical_query) <= 30, (
        f"lexical_query 长度应在 2-30：{result.lexical_query!r} (len={len(result.lexical_query)})"
    )


# ---------------------------------------------------------------------------
# Case 2：RAGFlow 部署案——典型常规 case
# ---------------------------------------------------------------------------


@pytest.mark.requires_llm
def test_rewrite_real_llm_typical_rag_question(real_llm):
    """常规 case：单一中文事实问，验证常规 RAG 风格输入能走通同样 schema。"""
    result = invoke_structured(
        real_llm,
        RewrittenQueries,
        REWRITE_SYSTEM_PROMPT,
        "用户问题：RAGFlow 怎么部署？",
    )

    assert isinstance(result, RewrittenQueries)
    assert 1 <= len(result.queries) <= 6
    # L0 原句必有
    assert any(q.layer == "L0" for q in result.queries)
    # 主实体词 RAGFlow 应出现在 lexical_query 里（保留原大小写）
    assert "RAGFlow" in result.lexical_query, (
        f"lexical_query 应包含主实体 'RAGFlow'：{result.lexical_query!r}"
    )
    # lexical_query 长度边界
    assert 2 <= len(result.lexical_query) <= 30
