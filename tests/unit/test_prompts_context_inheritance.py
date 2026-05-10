# -*- coding: utf-8 -*-
"""T24 验证：QA 各节点 user_prompt 上下文继承 + system prompt 条款补全。

设计：
- `CapturingLLM` 记录每次 `with_structured_output(schema).invoke(messages)` 收到的
  system+user 对，让测试直接断言 user_prompt 字面量。
- 既测节点 user_prompt 拼装行为（运行时），也测 system prompt 文本本身（编译时）。

目标：保证 `question` / `sub_questions` 这两条上下文骨架在 rewrite / judge /
self_check 节点的 user_prompt 中可见，避免子问题 LLM 调用脱离全局语境。
"""
from __future__ import annotations

import asyncio

import pytest

from brain_base.agents.schemas import (
    DecomposedQuestion,
    EvidenceJudgment,
    RewrittenQueries,
    RewrittenQuery,
    SelfCheckResult,
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
from brain_base.prompts.qa_prompts import (
    DECOMPOSE_SYSTEM_PROMPT,
    JUDGE_EVIDENCE_SYSTEM_PROMPT,
    REWRITE_SYSTEM_PROMPT,
    SELF_CHECK_SYSTEM_PROMPT,
)


# ---------------------------------------------------------------------------
# CapturingLLM：mock LLM，记录每次 invoke 的 (system, user) 对
# ---------------------------------------------------------------------------


class CapturingLLM:
    """记录 with_structured_output(...).invoke(messages) 收到的 system+user 对。"""

    def __init__(self, schemas_to_return: dict):
        self._returns = schemas_to_return
        self.captured: list[tuple[str, str, str]] = []  # (schema_name, system, user)

    def with_structured_output(self, schema, **kwargs):
        name = schema.__name__
        outer = self

        class _Bound:
            def invoke(self, msgs):
                # msgs[0] = SystemMessage, msgs[1] = HumanMessage
                outer.captured.append((name, msgs[0].content, msgs[1].content))
                if name in outer._returns:
                    return outer._returns[name]
                raise RuntimeError(f"CapturingLLM 未注册 schema={name}")

        return _Bound()

    def invoke(self, _msgs):
        # 路径 2 兜底：返回非 JSON 触发 fallback
        class _Resp:
            content = "(non-json fake text)"
        return _Resp()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# fanout_prep_dispatcher：Send 携带 question + sub_questions
# ---------------------------------------------------------------------------


def test_dispatcher_carries_question_and_sub_questions():
    """T24：dispatcher 把 question + sub_questions 一并塞进 Send payload。"""
    from langgraph.types import Send

    state = {
        "question": "RAGFlow 怎么启动？openclaw 怎么彻底卸载？",
        "sub_questions": ["RAGFlow 服务启动的步骤", "openclaw 彻底卸载的方法"],
    }
    out = fanout_prep_dispatcher(state)

    assert isinstance(out, list) and len(out) == 2
    for i, send in enumerate(out):
        assert isinstance(send, Send)
        assert send.arg["sub_idx"] == i
        assert send.arg["sub_question"] == state["sub_questions"][i]
        assert send.arg["question"] == state["question"]
        assert send.arg["sub_questions"] == state["sub_questions"]


def test_dispatcher_question_missing_falls_back_to_empty():
    """state 没有 question 字段时不抛错，Send.arg.question="" 兜底。"""
    out = fanout_prep_dispatcher({"sub_questions": ["A"]})
    assert isinstance(out, list) and out[0].arg["question"] == ""


# ---------------------------------------------------------------------------
# rewrite (prep_one_subquery)：多跳/单跳 user_prompt
# ---------------------------------------------------------------------------


def test_rewrite_multi_hop_user_prompt_contains_question_and_siblings(monkeypatch):
    """多跳：user_prompt 必须含原 question + 同级子问题列表 + 当前任务标记。"""
    monkeypatch.setattr(
        "brain_base.nodes.qa_prep._sparse_gate_score",
        lambda lq: 0.0,  # 低分；context 测试不关心阈值
    )
    llm = CapturingLLM({
        "RewrittenQueries": RewrittenQueries(
            queries=[RewrittenQuery(text="openclaw 卸载", layer="L0")],
            lexical_query="openclaw 卸载",
        )
    })
    node = create_prep_one_subquery(llm)

    _run(node({
        "sub_idx": 1,
        "sub_question": "openclaw 彻底卸载的方法",
        "question": "RAGFlow 怎么启动？openclaw 怎么彻底卸载？",
        "sub_questions": ["RAGFlow 服务启动的步骤", "openclaw 彻底卸载的方法"],
    }))

    assert len(llm.captured) == 1
    _, _, user = llm.captured[0]
    assert "用户原始问题：RAGFlow 怎么启动？openclaw 怎么彻底卸载？" in user
    assert "该问题被拆成 2 个子问题" in user
    assert "RAGFlow 服务启动的步骤" in user
    assert "openclaw 彻底卸载的方法" in user
    assert "← 当前任务" in user
    assert "当前要改写的子问题：openclaw 彻底卸载的方法" in user


def test_rewrite_single_hop_user_prompt_simple(monkeypatch):
    """单跳：user_prompt 保持简洁原格式，不出现"被拆成"或"原始问题"段落。"""
    monkeypatch.setattr(
        "brain_base.nodes.qa_prep._sparse_gate_score",
        lambda lq: 0.30,
    )
    llm = CapturingLLM({
        "RewrittenQueries": RewrittenQueries(
            queries=[RewrittenQuery(text="X", layer="L0")],
            lexical_query="X Y",
        )
    })
    node = create_prep_one_subquery(llm)

    _run(node({
        "sub_idx": 0,
        "sub_question": "RAGFlow 是什么",
        "question": "RAGFlow 是什么",
        "sub_questions": ["RAGFlow 是什么"],
    }))

    _, _, user = llm.captured[0]
    assert user == "用户问题：RAGFlow 是什么"
    assert "被拆成" not in user
    assert "← 当前任务" not in user


def test_rewrite_dispatcher_legacy_state_falls_back(monkeypatch):
    """sub_state 没塞 question/sub_questions（旧调用方）→ 走单跳格式不抛错。"""
    monkeypatch.setattr(
        "brain_base.nodes.qa_prep._sparse_gate_score",
        lambda lq: 0.0,
    )
    llm = CapturingLLM({
        "RewrittenQueries": RewrittenQueries(
            queries=[RewrittenQuery(text="A", layer="L0")],
            lexical_query="A B",
        )
    })
    node = create_prep_one_subquery(llm)

    _run(node({"sub_idx": 0, "sub_question": "A 是什么"}))

    _, _, user = llm.captured[0]
    assert user == "用户问题：A 是什么"


# ---------------------------------------------------------------------------
# decompose：补 expected_type / time_sensitive
# ---------------------------------------------------------------------------


def test_decompose_user_prompt_contains_expected_type_and_time_sensitive():
    llm = CapturingLLM({
        "DecomposedQuestion": DecomposedQuestion(
            needs_decompose=False, sub_questions=[]
        )
    })
    node = create_decompose_node(llm)

    node({
        "normalized_query": "RAGFlow 当前最新版本",
        "expected_type": "fact",
        "time_sensitive": True,
    })

    assert len(llm.captured) == 1
    _, _, user = llm.captured[0]
    assert "用户问题：RAGFlow 当前最新版本" in user
    assert "期望答案类型：fact" in user
    assert "时效敏感：True" in user


def test_decompose_user_prompt_default_when_fields_missing():
    """expected_type 缺省 → 用'未指定'兜底，time_sensitive 缺省 → False。"""
    llm = CapturingLLM({
        "DecomposedQuestion": DecomposedQuestion(
            needs_decompose=False, sub_questions=[]
        )
    })
    node = create_decompose_node(llm)
    node({"normalized_query": "X 是什么"})

    _, _, user = llm.captured[0]
    assert "期望答案类型：未指定" in user
    assert "时效敏感：False" in user


# ---------------------------------------------------------------------------
# judge：多跳/单跳 user_prompt
# ---------------------------------------------------------------------------


def test_judge_multi_hop_user_prompt_contains_sub_questions():
    """多跳：judge user_prompt 必须含子问题列表 + sub_idx 索引格式。"""
    llm = CapturingLLM({
        "EvidenceJudgment": EvidenceJudgment(
            sufficient=True,
            avg_score=0.8,
            coverage=1.0,
            recommendation="generate_answer",
            reason="ok",
        )
    })
    node = create_judge_node(llm)

    node({
        "evidence": [
            {"source": "doc1", "summary": "RAGFlow 启动用 docker-compose up", "score": 0.9},
            {"source": "doc2", "summary": "openclaw 用 pip uninstall 卸载", "score": 0.8},
        ],
        "question": "RAGFlow 怎么启动？openclaw 怎么彻底卸载？",
        "sub_questions": ["RAGFlow 服务启动的步骤", "openclaw 彻底卸载的方法"],
    })

    assert len(llm.captured) == 1
    _, _, user = llm.captured[0]
    assert "用户原始问题：RAGFlow 怎么启动？openclaw 怎么彻底卸载？" in user
    assert "子问题列表（按 sub_idx 索引）：" in user
    assert "[s0] RAGFlow 服务启动的步骤" in user
    assert "[s1] openclaw 彻底卸载的方法" in user


def test_judge_single_hop_user_prompt_no_sub_questions_section():
    """单跳：judge user_prompt 保持原格式，不出现'子问题列表'段落。"""
    llm = CapturingLLM({
        "EvidenceJudgment": EvidenceJudgment(
            sufficient=True,
            avg_score=0.8,
            coverage=1.0,
            recommendation="generate_answer",
            reason="ok",
        )
    })
    node = create_judge_node(llm)

    node({
        "evidence": [{"source": "doc", "summary": "X is Y", "score": 0.9}],
        "question": "X 是什么",
        "sub_questions": ["X 是什么"],
    })

    _, _, user = llm.captured[0]
    assert "用户问题：X 是什么" in user
    assert "子问题列表" not in user


# ---------------------------------------------------------------------------
# self_check：多跳/单跳 user_prompt
# ---------------------------------------------------------------------------


def test_self_check_multi_hop_user_prompt_contains_sub_questions():
    llm = CapturingLLM({
        "SelfCheckResult": SelfCheckResult(
            faithfulness="pass",
            completeness="pass",
            consistency="pass",
        )
    })
    node = create_self_check_node(llm)

    node({
        "answer": "## 子问题 1\n...\n## 子问题 2\n...",
        "question": "RAGFlow 启动？openclaw 卸载？",
        "evidence": [{"source": "doc", "summary": "...", "score": 0.9}],
        "sub_questions": ["RAGFlow 启动", "openclaw 卸载"],
        "crystallized_status": "miss",
    })

    assert len(llm.captured) == 1
    _, _, user = llm.captured[0]
    assert "用户原始问题：RAGFlow 启动？openclaw 卸载？" in user
    assert "子问题列表：" in user
    assert "1. RAGFlow 启动" in user
    assert "2. openclaw 卸载" in user


def test_self_check_single_hop_user_prompt_no_sub_questions_section():
    llm = CapturingLLM({
        "SelfCheckResult": SelfCheckResult(
            faithfulness="pass",
            completeness="pass",
            consistency="pass",
        )
    })
    node = create_self_check_node(llm)

    node({
        "answer": "X is Y",
        "question": "X 是什么",
        "evidence": [{"source": "doc", "summary": "...", "score": 0.9}],
        "sub_questions": ["X 是什么"],
        "crystallized_status": "miss",
    })

    _, _, user = llm.captured[0]
    assert "用户问题：X 是什么" in user
    assert "子问题列表" not in user


# ---------------------------------------------------------------------------
# system prompts：补条款编译期断言
# ---------------------------------------------------------------------------


def test_rewrite_system_prompt_contains_sibling_distinction_clause():
    assert "与同级子问题区分" in REWRITE_SYSTEM_PROMPT
    assert "该问题被拆成 N 个子问题" in REWRITE_SYSTEM_PROMPT
    assert "动作词区分" in REWRITE_SYSTEM_PROMPT


def test_judge_system_prompt_contains_decompose_mode_clause():
    assert "分解模式" in JUDGE_EVIDENCE_SYSTEM_PROMPT
    assert "子问题列表" in JUDGE_EVIDENCE_SYSTEM_PROMPT
    assert "逐子问题评估覆盖度" in JUDGE_EVIDENCE_SYSTEM_PROMPT


def test_self_check_system_prompt_contains_decompose_mode_clause():
    assert "分解模式" in SELF_CHECK_SYSTEM_PROMPT
    assert "completeness" in SELF_CHECK_SYSTEM_PROMPT
    assert "每个子问题对应的小节" in SELF_CHECK_SYSTEM_PROMPT


def test_decompose_system_prompt_references_time_sensitive():
    assert "time_sensitive=True" in DECOMPOSE_SYSTEM_PROMPT
