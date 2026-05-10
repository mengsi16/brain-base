# -*- coding: utf-8 -*-
"""QA fanout_prep 节点（rewrite + sparse gate）单元测试。

T30：grep AND gate 换为 milvus ``text_search`` sparse gate（top-3 平均分 +
阈值 0.20）。mock 点从 ``grep_keywords_and`` 换为 ``_sparse_gate_score``。

覆盖：
- prep_one_subquery：LLM 正常返回高分 → needs_get_info=False
- prep_one_subquery：低分 (< 0.20) → needs_get_info=True
- prep_one_subquery：sparse 调用抛错 → 保守降级
  needs_get_info=True (契约 §5)
- prep_one_subquery：保留 L0 原句、lexical_query 兜底、queries 截断
- prep_one_subquery：lexical_query > 30 字 → 截到 30
- fanout_prep_dispatcher：sub_questions 非空 → list[Send]
- fanout_prep_dispatcher：sub_questions 空 → "barrier1" 短路
- barrier1_node：按 sub_idx 排序聚合扁平字段 (sub_lexical_*)
"""
from __future__ import annotations

import asyncio

import pytest

from brain_base.agents.schemas import RewrittenQueries, RewrittenQuery
from brain_base.nodes.qa_prep import (
    barrier1_node,
    create_prep_one_subquery,
    fanout_prep_dispatcher,
)


# ---------------------------------------------------------------------------
# Fake LLM
# ---------------------------------------------------------------------------


class _FakeLLM:
    """按 schema 名查表返回；可指定哪些 schema 抛错。"""

    def __init__(self, structured: dict | None = None, raise_on_schema: set | None = None):
        self._structured = structured or {}
        self._raise = raise_on_schema or set()

    def with_structured_output(self, schema, **kwargs):
        name = schema.__name__
        outer = self

        class _Bound:
            def invoke(self, _msgs):
                if name in outer._raise:
                    raise RuntimeError(f"forced failure for {name}")
                if name in outer._structured:
                    return outer._structured[name]
                raise RuntimeError(f"_FakeLLM 未注册 schema={name}")

        return _Bound()

    def invoke(self, _msgs):
        """invoke_structured 路径 2 兜底用：返回非 JSON 字符串触发 fallback。"""
        class _Resp:
            content = "(non-json fake text)"
        return _Resp()


def _run(coro):
    """同步运行 async：每次新建 loop 避免污染。"""
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# prep_one_subquery
# ---------------------------------------------------------------------------


def test_prep_one_normal_llm_high_score(monkeypatch):
    """LLM 正常返回 queries+lexical_query，sparse 高分 → needs_get_info=False。"""
    monkeypatch.setattr(
        "brain_base.nodes.qa_prep._sparse_gate_score",
        lambda lq: 0.32,  # > 0.20 阈值
    )
    llm = _FakeLLM(structured={
        "RewrittenQueries": RewrittenQueries(
            queries=[
                RewrittenQuery(text="openclaw 是什么", layer="L0"),
                RewrittenQuery(text="什么是 openclaw 项目", layer="L1"),
            ],
            lexical_query="openclaw 介绍",
        )
    })
    node = create_prep_one_subquery(llm)

    out = _run(node({"sub_idx": 0, "sub_question": "openclaw 是什么"}))

    assert "sub_prep_results" in out and len(out["sub_prep_results"]) == 1
    item = out["sub_prep_results"][0]
    assert item["sub_idx"] == 0
    assert item["sub_question"] == "openclaw 是什么"
    assert any(q["layer"] == "L0" for q in item["queries"])
    assert item["lexical_query"] == "openclaw 介绍"
    assert item["lexical_score"] == pytest.approx(0.32)
    assert item["needs_get_info"] is False


def test_prep_one_low_score_triggers_external(monkeypatch):
    """sparse 低分 (< 0.20) → needs_get_info=True 走外检。"""
    monkeypatch.setattr(
        "brain_base.nodes.qa_prep._sparse_gate_score",
        lambda lq: 0.10,  # < 0.20 阈值
    )
    llm = _FakeLLM(structured={
        "RewrittenQueries": RewrittenQueries(
            queries=[RewrittenQuery(text="未入库主题 X", layer="L0")],
            lexical_query="未入库主题 X",
        )
    })
    node = create_prep_one_subquery(llm)

    out = _run(node({"sub_idx": 0, "sub_question": "未入库主题 X"}))
    item = out["sub_prep_results"][0]
    assert item["lexical_score"] == pytest.approx(0.10)
    assert item["needs_get_info"] is True


def test_prep_one_sparse_failure_safe_degrade(monkeypatch):
    """sparse 调用抛错 (text_search 不可用) → score=0.0 保守降级 needs_get_info=True。

    契约 §5：milvus 不可用 / sparse 字段缺失 是基础设施级问题，不应
    阻断 QA 流程——走外检路径接管。实际 _sparse_gate_score 内部会 try-except
    并 logger.warning，本测试 monkeypatch 该函数直接返 0.0。
    """
    monkeypatch.setattr(
        "brain_base.nodes.qa_prep._sparse_gate_score",
        lambda lq: 0.0,
    )
    llm = _FakeLLM(structured={
        "RewrittenQueries": RewrittenQueries(
            queries=[RewrittenQuery(text="qq", layer="L0")],
            lexical_query="qq",
        )
    })
    node = create_prep_one_subquery(llm)

    out = _run(node({"sub_idx": 0, "sub_question": "qq"}))
    item = out["sub_prep_results"][0]
    assert item["lexical_score"] == 0.0
    assert item["needs_get_info"] is True


# T27 删：test_prep_one_llm_none_fallback / test_prep_one_llm_raises_uses_fallback
# 原因：QA 主图 LLM 节点 fail-fast，invoke_structured 不再有 fallback 形参；
# llm=None / LLM 抛错都直接上拋到 LangGraph runtime 而不是降级。


def test_prep_one_prepends_l0_if_missing(monkeypatch):
    """LLM 输出不含原句 → 节点自动 prepend L0 原句。"""
    monkeypatch.setattr(
        "brain_base.nodes.qa_prep._sparse_gate_score",
        lambda lq: 0.25,
    )
    llm = _FakeLLM(structured={
        "RewrittenQueries": RewrittenQueries(
            queries=[
                RewrittenQuery(text="什么是 RAGFlow 系统", layer="L1"),
                RewrittenQuery(text="RAGFlow 项目介绍", layer="L2"),
            ],
            lexical_query="RAGFlow 介绍",
        )
    })
    node = create_prep_one_subquery(llm)

    out = _run(node({"sub_idx": 0, "sub_question": "RAGFlow 是什么"}))

    queries = out["sub_prep_results"][0]["queries"]
    assert queries[0] == {"text": "RAGFlow 是什么", "layer": "L0"}


def test_prep_one_truncates_queries_to_6(monkeypatch):
    """LLM 给 >6 条 queries → 截断保留前 6（保护 token）。"""
    monkeypatch.setattr(
        "brain_base.nodes.qa_prep._sparse_gate_score",
        lambda lq: 0.21,
    )
    fake = RewrittenQueries.model_construct(
        queries=[RewrittenQuery(text=f"q{i}", layer="L1") for i in range(10)],
        lexical_query="foo bar",
    )
    llm = _FakeLLM(structured={"RewrittenQueries": fake})
    node = create_prep_one_subquery(llm)

    out = _run(node({"sub_idx": 0, "sub_question": "q0"}))

    assert len(out["sub_prep_results"][0]["queries"]) <= 6


def test_prep_one_truncates_lexical_query_to_30_chars(monkeypatch):
    """LLM 给 lexical_query > 30 字 → 被 _normalize_lexical_query 截断到 30。

    使用 model_construct 跳过 pydantic max_length 校验（模拟 LLM 偶尔超长场景，
    代码仍需兜底保护）。
    """
    monkeypatch.setattr(
        "brain_base.nodes.qa_prep._sparse_gate_score",
        lambda lq: 0.25,
    )
    long_query = "中文错误超长查询串" * 10  # 90 字
    fake = RewrittenQueries.model_construct(
        queries=[RewrittenQuery(text="abc", layer="L0")],
        lexical_query=long_query,
    )
    llm = _FakeLLM(structured={"RewrittenQueries": fake})
    node = create_prep_one_subquery(llm)

    out = _run(node({"sub_idx": 0, "sub_question": "abc"}))

    assert len(out["sub_prep_results"][0]["lexical_query"]) == 30


# ---------------------------------------------------------------------------
# fanout_prep_dispatcher
# ---------------------------------------------------------------------------


def test_fanout_dispatcher_sends_n():
    """N 个子问题 → 返回 N 个 Send，每个携带 sub_idx + sub_question。"""
    from langgraph.types import Send

    state = {"sub_questions": ["A 是什么", "如何启动 A", "如何卸载 A"]}
    out = fanout_prep_dispatcher(state)

    assert isinstance(out, list)
    assert len(out) == 3
    for i, send in enumerate(out):
        assert isinstance(send, Send)
        assert send.node == "subquery_prep"
        assert send.arg["sub_idx"] == i
        assert send.arg["sub_question"] == state["sub_questions"][i]


def test_fanout_dispatcher_empty_short_circuits():
    """sub_questions 空 → 返回 'barrier1' 字符串避免无边卡住。"""
    out = fanout_prep_dispatcher({"sub_questions": []})
    assert out == "barrier1"


def test_fanout_dispatcher_missing_field():
    """sub_questions 字段缺失 → 同样短路 'barrier1'。"""
    out = fanout_prep_dispatcher({})
    assert out == "barrier1"


# ---------------------------------------------------------------------------
# barrier1_node
# ---------------------------------------------------------------------------


def test_barrier1_aggregates_in_order():
    """barrier 收 reducer 合并的 sub_prep_results（乱序），按 sub_idx 排序后拆扁平字段。"""
    state = {
        "sub_prep_results": [
            {
                "sub_idx": 2,
                "sub_question": "C",
                "queries": [{"text": "c0", "layer": "L0"}],
                "lexical_query": "C 介绍",
                "lexical_score": 0.05,
                "needs_get_info": True,
            },
            {
                "sub_idx": 0,
                "sub_question": "A",
                "queries": [{"text": "a0", "layer": "L0"}, {"text": "a1", "layer": "L1"}],
                "lexical_query": "A 部署",
                "lexical_score": 0.32,
                "needs_get_info": False,
            },
            {
                "sub_idx": 1,
                "sub_question": "B",
                "queries": [{"text": "b0", "layer": "L0"}],
                "lexical_query": "B 用法",
                "lexical_score": 0.21,
                "needs_get_info": False,
            },
        ]
    }

    out = barrier1_node(state)

    assert out["sub_lexical_scores"] == [pytest.approx(0.32), pytest.approx(0.21), pytest.approx(0.05)]
    assert out["sub_needs_get_info"] == [False, False, True]
    assert out["sub_lexical_queries"] == ["A 部署", "B 用法", "C 介绍"]
    assert len(out["sub_queries"]) == 3
    assert out["sub_queries"][0][0] == {"text": "a0", "layer": "L0"}


def test_barrier1_empty_input():
    """sub_prep_results 空 / 缺失 → 返回空列表，不抛错。"""
    out = barrier1_node({"sub_prep_results": []})
    assert out == {
        "sub_queries": [],
        "sub_lexical_queries": [],
        "sub_lexical_scores": [],
        "sub_needs_get_info": [],
    }
    out2 = barrier1_node({})
    assert out2 == out
