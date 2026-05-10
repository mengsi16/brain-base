# -*- coding: utf-8 -*-
"""QA 多跳问题分解 fan-out（T12）端到端集成测试。

不依赖真 LLM / Milvus / Playwright；用 fake LLM + monkeypatch multi_query_search
驱动整张 QaGraph，验证：

1. 单一问题（"什么是 LiteLLM"） → 走 rewrite → search 单链路，evidence 不带 sub_idx
2. 多部问题 → 走 subquery_fanout，每子问题独立 evidence 组
3. 时序问题（"ragflow 历史变化"） → DECOMPOSE 输出多子问题 → 走 fanout
4. 子问题部分缺证据 → judge 判定不足 → 触发 get_info_trigger（playwright 不可用时降级到 answer）

运行：
    pytest tests/e2e/test_qa_multihop.py -v
"""
from __future__ import annotations

import sys

import pytest

import brain_base.nodes.qa as qa_mod
from brain_base.agents.schemas import (
    DecomposedQuestion,
    EvidenceJudgment,
    RewrittenQueries,
    RewrittenQuery,
    SelfCheckResult,
    SubQuestion,
)
from brain_base.graphs.qa_graph import QaGraph


# ---------------------------------------------------------------------------
# Fake LLM：按 schema 名分发结构化输出 + 文本回退
# ---------------------------------------------------------------------------


class _FakeLLM:
    """按 schema 类名查表分发结构化输出。

    structured: dict[schema_name -> Pydantic instance]
    text_response: 调 llm.invoke([msgs]) 时（answer 节点）的文本返回
    """

    def __init__(
        self,
        structured: dict[str, object] | None = None,
        text_response: str = "FAKE-ANSWER",
    ):
        self._structured = structured or {}
        self._text_response = text_response

    def with_structured_output(self, schema):
        outer = self
        name = schema.__name__

        class _Bound:
            def invoke(self_inner, _msgs):
                if name not in outer._structured:
                    raise RuntimeError(f"_FakeLLM 未注册 schema={name}")
                return outer._structured[name]

        return _Bound()

    def invoke(self, _msgs):
        class _Resp:
            def __init__(self, text):
                self.content = text

        return _Resp(self._text_response)


def _baseline_structured() -> dict[str, object]:
    """所有 LLM 节点（除 decompose）的稳定 stub，避免节点报错。"""
    return {
        "NormalizedQuestion": _normalized_default(),
        "RewrittenQueries": RewrittenQueries(
            queries=[
                RewrittenQuery(text="rewritten-l0", layer="L0"),
                RewrittenQuery(text="rewritten-l1", layer="L1"),
            ]
        ),
        "EvidenceJudgment": EvidenceJudgment(
            sufficient=True,
            recommendation="generate_answer",
            coverage=0.9,
            reason="ok",
        ),
        "SelfCheckResult": SelfCheckResult(
            faithfulness="pass",
            completeness="pass",
            consistency="pass",
            revised_answer="",
            notes="",
        ),
        # GetInfoTrigger 不会被 fan-out + 充足证据路径用到，这里给个兜底
        "GetInfoTrigger": _trigger_default(),
    }


def _normalized_default():
    from brain_base.agents.schemas import NormalizedQuestion

    return NormalizedQuestion(
        normalized="normalized-q",
        expected_type="procedure",
        time_sensitive=False,
        language="zh",
    )


def _trigger_default():
    from brain_base.agents.schemas import GetInfoTrigger

    return GetInfoTrigger(
        needed=False,
        reason="ok",
        suggested_keywords=[],
        time_range_hint="none",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_search_returns_one_per_call(monkeypatch):
    """每次调 multi_query_search 返回 1 条结果（按调用顺序编号）。"""
    counter = {"n": 0}

    def _fake(**kw):
        counter["n"] += 1
        return {
            "results": [
                {
                    "chunk_id": f"chunk-call{counter['n']}",
                    "summary": f"summary call {counter['n']}",
                    "chunk_text": f"chunk text call {counter['n']}",
                }
            ]
        }

    monkeypatch.setattr(qa_mod, "multi_query_search", _fake)
    return counter


@pytest.fixture
def patch_search_empty(monkeypatch):
    """所有检索都返回空，模拟"本地无证据"。"""
    monkeypatch.setattr(qa_mod, "multi_query_search", lambda **kw: {"results": []})


@pytest.fixture
def patch_infra_milvus_only(monkeypatch):
    """probe 节点的两个探测：Milvus 可用、Playwright 不可用。

    Playwright 不可用 → get_info_trigger 软依赖降级 → answer。
    """
    monkeypatch.setattr(
        qa_mod, "probe_milvus", lambda: {"available": True}
    )
    monkeypatch.setattr(
        qa_mod, "probe_playwright", lambda: {"available": False}
    )


# ---------------------------------------------------------------------------
# 测试 1：单一问题不走 fanout
# ---------------------------------------------------------------------------


def test_single_question_takes_rewrite_path(
    patch_search_returns_one_per_call,
    patch_infra_milvus_only,
):
    """简单问题 'X 是什么' → decomposition_needed=False → 走 rewrite → search。

    断言：sub_question_evidence 为空 / 不存在；evidence 不带 sub_idx。
    """
    structured = _baseline_structured()
    structured["DecomposedQuestion"] = DecomposedQuestion(
        needs_decompose=False, sub_questions=[]
    )
    llm = _FakeLLM(structured=structured, text_response="single-answer")

    g = QaGraph(llm=llm)
    out = g.run("什么是 LiteLLM")

    assert out.get("decomposition_needed", False) is False
    assert not out.get("sub_question_evidence")
    # evidence 应来自 search_node，不带 sub_idx
    for e in out.get("evidence", []):
        assert "sub_idx" not in e, (
            f"单链路模式下 evidence 不应带 sub_idx 标签：{e}"
        )
    # search 路径每个 query 调一次 multi_query_search；至少调过 1 次
    assert patch_search_returns_one_per_call["n"] >= 1
    assert out.get("answer", "").strip() != ""


# ---------------------------------------------------------------------------
# 测试 2：多部问题走 fanout，evidence 按子问题分组
# ---------------------------------------------------------------------------


def test_multipart_question_runs_fanout(
    patch_search_returns_one_per_call,
    patch_infra_milvus_only,
):
    """openclaw 三部问 → 拆 3 子问题 → 每子问题独立 1 条 evidence。"""
    structured = _baseline_structured()
    structured["DecomposedQuestion"] = DecomposedQuestion(
        needs_decompose=True,
        sub_questions=[
            SubQuestion(text="openclaw 是什么", type="sub-fact"),
            SubQuestion(text="怎么启动 openclaw", type="sub-fact"),
            SubQuestion(text="怎么卸载 openclaw", type="sub-fact"),
        ],
    )
    llm = _FakeLLM(structured=structured, text_response="multi-answer")

    g = QaGraph(llm=llm)
    out = g.run("openclaw 是什么，怎么启动，怎么卸载")

    assert out["decomposition_needed"] is True
    groups = out["sub_question_evidence"]
    assert len(groups) == 3, f"应拆 3 子问题，实际 {len(groups)}"

    # 每子问题各 1 条 evidence
    for g_meta in groups:
        assert g_meta["evidence_count"] == 1, g_meta
    # 全局 evidence 也是 3 条，且都带 sub_idx
    evs = out["evidence"]
    assert len(evs) == 3
    indices = sorted(e["sub_idx"] for e in evs)
    assert indices == [0, 1, 2]

    # 没有走 search 节点 → call counter 等于子问题数
    # （subquery_fanout 内每子问题调 1 次 multi_query_search）
    assert patch_search_returns_one_per_call["n"] == 3, (
        f"fan-out 应只调 3 次 multi_query_search，实际 {patch_search_returns_one_per_call['n']}"
    )


# ---------------------------------------------------------------------------
# 测试 3：时序问题被拆 → 走 fanout
# ---------------------------------------------------------------------------


def test_temporal_question_runs_fanout(
    patch_search_returns_one_per_call,
    patch_infra_milvus_only,
):
    """ragflow 历史变化 → 拆 3 子问题 → 走 fanout。"""
    structured = _baseline_structured()
    structured["DecomposedQuestion"] = DecomposedQuestion(
        needs_decompose=True,
        sub_questions=[
            SubQuestion(text="ragflow 当前是什么", type="sub-fact"),
            SubQuestion(text="ragflow 历史版本和重要节点", type="sub-fact"),
            SubQuestion(text="ragflow 演进过程中的关键变化", type="synthesis"),
        ],
    )
    llm = _FakeLLM(structured=structured, text_response="temporal-answer")

    g = QaGraph(llm=llm)
    out = g.run("ragflow 历史变化")

    assert out["decomposition_needed"] is True
    assert len(out["sub_question_evidence"]) == 3


# ---------------------------------------------------------------------------
# 测试 4：子问题缺证据 → judge 判不足 → trigger 不可用降级到 answer
# ---------------------------------------------------------------------------


def test_subquery_missing_evidence_triggers_judge_insufficient(
    patch_search_empty,
    patch_infra_milvus_only,
):
    """子问题全部检索为空 → judge 判 insufficient，trigger 因 playwright 降级回 answer。

    防死循环：T10 的 get_info_attempted 路由保证不会无限触发外检。
    """
    structured = _baseline_structured()
    structured["DecomposedQuestion"] = DecomposedQuestion(
        needs_decompose=True,
        sub_questions=[
            SubQuestion(text="子问题 1", type="sub-fact"),
            SubQuestion(text="子问题 2", type="sub-fact"),
        ],
    )
    llm = _FakeLLM(structured=structured, text_response="degraded-answer")

    g = QaGraph(llm=llm)
    out = g.run("多跳问题")

    # judge 应判 insufficient（任一子问题 evidence_count=0 触发硬规则）
    assert out.get("evidence_sufficient", True) is False
    reason = out.get("judge_reason", "")
    assert "缺证据" in reason or "missing" in reason.lower()
    # 没有 evidence 时 answer 应给降级文本，但流程必须走完不阻断
    assert out.get("answer", "").strip() != ""


# -----------------------------------------------------------------------------
# 脚本入口
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
