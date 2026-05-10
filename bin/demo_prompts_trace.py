# -*- coding: utf-8 -*-
"""T24 prompt 上下文继承演示——把每步真实 system + user prompt 全文打印出来。

用法：python bin/demo_prompts_trace.py

不依赖真实 LLM API：mock 拦截 invoke_structured 调用，记录每次 (system, user)
对，按节点顺序打印。两个场景：
- 场景 1：多跳问题（2 个子问题，rewrite × 2 + judge + self_check 进入分解模式）
- 场景 2：单跳问题（1 个子问题，所有节点走简洁分支）
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 让 bin/ 下脚本能 import brain_base
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brain_base.nodes.qa_prep as qp_mod
from brain_base.agents.schemas import (
    DecomposedQuestion,
    EvidenceJudgment,
    RewrittenQueries,
    RewrittenQuery,
    SelfCheckResult,
    SubQuestion,
)
from brain_base.nodes.qa import (
    create_decompose_node,
    create_judge_node,
    create_self_check_node,
)
from brain_base.nodes.qa_prep import (
    create_prep_one_subquery,
    fanout_prep_dispatcher,
)


# ---------------------------------------------------------------------------
# Trace 用 mock LLM
# ---------------------------------------------------------------------------


class TraceLLM:
    """记录每次 invoke_structured 收到的 (system, user) 并打印。"""

    def __init__(self, returns: dict, label: str = ""):
        self._returns = returns
        self._step = 0
        self._label = label

    def with_structured_output(self, schema, **kwargs):
        name = schema.__name__
        outer = self

        class _Bound:
            def invoke(self, msgs):
                outer._step += 1
                print()
                print("─" * 78)
                print(f"  STEP {outer._step}  ·  schema={name}  ·  {outer._label}")
                print("─" * 78)
                print()
                print(">>> SYSTEM PROMPT")
                print(msgs[0].content)
                print()
                print(">>> USER PROMPT")
                print(msgs[1].content)
                print()
                return outer._returns[name]

        return _Bound()

    def invoke(self, _):
        class _R:
            content = "{}"

        return _R()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# 场景 1：多跳
# ---------------------------------------------------------------------------


def scenario_multi_hop() -> None:
    print()
    print("=" * 78)
    print("  场景 1：多跳问题（拆成 2 个子问题）")
    print("=" * 78)
    print('  原始问题："RAGFlow 怎么启动？openclaw 怎么彻底卸载？"')
    print()

    llm = TraceLLM(
        {
            "DecomposedQuestion": DecomposedQuestion(
                needs_decompose=True,
                sub_questions=[
                    SubQuestion(text="RAGFlow 服务启动的步骤", type="sub-fact"),
                    SubQuestion(text="openclaw 彻底卸载的方法", type="sub-fact"),
                ],
            ),
            "RewrittenQueries": RewrittenQueries(
                queries=[RewrittenQuery(text="占位", layer="L0")],
                lexical_query="占位 x",
            ),
            "EvidenceJudgment": EvidenceJudgment(
                sufficient=True,
                avg_score=0.85,
                coverage=1.0,
                recommendation="generate_answer",
                reason="ok",
            ),
            "SelfCheckResult": SelfCheckResult(
                faithfulness="pass", completeness="pass", consistency="pass"
            ),
        },
        label="multi-hop",
    )

    # 1) decompose
    state = {
        "normalized_query": "RAGFlow 怎么启动？openclaw 怎么彻底卸载？",
        "expected_type": "procedure",
        "time_sensitive": False,
    }
    decompose_out = create_decompose_node(llm)(state)
    sub_questions = decompose_out["sub_questions"]
    state.update(
        {
            "question": "RAGFlow 怎么启动？openclaw 怎么彻底卸载？",
            "sub_questions": sub_questions,
        }
    )

    # 2) fanout_prep_dispatcher（不调 LLM，只看派发）
    sends = fanout_prep_dispatcher(state)
    print()
    print("─" * 78)
    print(f"  fanout_prep_dispatcher → 派发 {len(sends)} 个 Send")
    print("─" * 78)
    for s in sends:
        print(f"  Send.arg: {s.arg}")
    print()

    # 3) prep_one_subquery × N（rewrite + sparse gate）
    qp_mod._sparse_gate_score = lambda lq: 0.0  # mock sparse 低分 → needs_get_info=True
    rewrite = create_prep_one_subquery(llm)
    for send in sends:
        _run(rewrite(send.arg))

    # 4) judge
    create_judge_node(llm)(
        {
            "evidence": [
                {
                    "source": "doc1",
                    "summary": "RAGFlow 启动: docker-compose up -d",
                    "score": 0.9,
                },
                {
                    "source": "doc2",
                    "summary": "openclaw 卸载: pip uninstall openclaw",
                    "score": 0.8,
                },
            ],
            "question": state["question"],
            "sub_questions": sub_questions,
        }
    )

    # 5) self_check
    create_self_check_node(llm)(
        {
            "answer": "## 子问题 1：RAGFlow 启动\n...\n## 子问题 2：openclaw 卸载\n...",
            "question": state["question"],
            "evidence": [{"source": "doc", "summary": "...", "score": 0.9}],
            "sub_questions": sub_questions,
            "crystallized_status": "miss",
        }
    )


# ---------------------------------------------------------------------------
# 场景 2：单跳
# ---------------------------------------------------------------------------


def scenario_single_hop() -> None:
    print()
    print("=" * 78)
    print("  场景 2：单跳问题（不分解）")
    print("=" * 78)
    print('  原始问题："RAGFlow 是什么"')
    print()

    llm = TraceLLM(
        {
            "DecomposedQuestion": DecomposedQuestion(
                needs_decompose=False, sub_questions=[]
            ),
            "RewrittenQueries": RewrittenQueries(
                queries=[RewrittenQuery(text="占位", layer="L0")],
                lexical_query="占位 x",
            ),
            "EvidenceJudgment": EvidenceJudgment(
                sufficient=True,
                avg_score=0.85,
                coverage=1.0,
                recommendation="generate_answer",
                reason="ok",
            ),
            "SelfCheckResult": SelfCheckResult(
                faithfulness="pass", completeness="pass", consistency="pass"
            ),
        },
        label="single-hop",
    )

    state = {
        "normalized_query": "RAGFlow 是什么",
        "expected_type": "concept",
        "time_sensitive": False,
    }
    decompose_out = create_decompose_node(llm)(state)
    sub_questions = decompose_out.get("sub_questions") or [state["normalized_query"]]
    state.update(
        {
            "question": "RAGFlow 是什么",
            "sub_questions": sub_questions,
        }
    )

    sends = fanout_prep_dispatcher(state)
    print()
    print("─" * 78)
    print(f"  fanout_prep_dispatcher → 派发 {len(sends)} 个 Send")
    print("─" * 78)
    for s in sends:
        print(f"  Send.arg: {s.arg}")
    print()

    qp_mod._sparse_gate_score = lambda lq: 0.30  # mock sparse 高分 → needs_get_info=False
    rewrite = create_prep_one_subquery(llm)
    for send in sends:
        _run(rewrite(send.arg))

    create_judge_node(llm)(
        {
            "evidence": [
                {"source": "doc1", "summary": "RAGFlow 是 RAG 引擎", "score": 0.9}
            ],
            "question": state["question"],
            "sub_questions": sub_questions,
        }
    )

    create_self_check_node(llm)(
        {
            "answer": "RAGFlow 是 RAG 引擎，主要做 ...",
            "question": state["question"],
            "evidence": [{"source": "doc", "summary": "...", "score": 0.9}],
            "sub_questions": sub_questions,
            "crystallized_status": "miss",
        }
    )


if __name__ == "__main__":
    scenario_multi_hop()
    scenario_single_hop()
    print()
    print("=" * 78)
    print("  trace 结束")
    print("=" * 78)
