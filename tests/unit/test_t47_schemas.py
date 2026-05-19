# -*- coding: utf-8 -*-
"""T47.1 单元测试：新增 schemas 字段约束 + GetInfoConfig + QaState 字段。

覆盖：
- Evidence 默认字段 + score 范围 0-100
- IntentAction / IntentPlan 三态（next_actions 长度=0 / =1 / >1）
- IntentObservation confidence 必须在 0-1 范围内

不调 LLM，纯结构验证（CLAUDE.md 规则 14：mock LLM 仅限非语义 schema 测试）。

契约引用：md/research/2026-05-17-t47-unified-intent-agent-contract.md §4-§9
执行计划：md/research/2026-05-17-t47.1-schemas-execution-plan.md
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class TestEvidence:
    """Evidence schema：evidence_pool 元素，score 0-100 与 FetchExtractResult 对齐。"""

    def test_minimal_construction_only_url(self):
        """url 是唯一 required 字段，其他都有合理默认值。"""
        from brain_base.agents.schemas import Evidence
        ev = Evidence(url="https://example.com/doc")
        assert ev.url == "https://example.com/doc"
        assert ev.title == ""
        assert ev.content == ""
        assert ev.score == 0.0
        assert ev.sha256_hash == ""
        assert ev.from_queries == []
        assert ev.snippet == ""
        assert ev.source_type == "community"
        assert ev.tool_name == ""

    def test_score_accepts_0_to_100(self):
        """score 范围与 FetchExtractResult.score 对齐（0-100，不是 0-1）。"""
        from brain_base.agents.schemas import Evidence
        # 边界值
        Evidence(url="x", score=0.0)
        Evidence(url="x", score=100.0)
        Evidence(url="x", score=50.5)

    def test_score_rejects_out_of_range(self):
        """score < 0 或 > 100 应触发 pydantic ValidationError。"""
        from brain_base.agents.schemas import Evidence
        with pytest.raises(ValidationError):
            Evidence(url="x", score=-0.1)
        with pytest.raises(ValidationError):
            Evidence(url="x", score=100.1)

    def test_full_construction(self):
        """完整字段构造：模拟 fetch_extract_one 输出转 Evidence 的场景。"""
        from brain_base.agents.schemas import Evidence
        ev = Evidence(
            url="https://docs.python.org/3/tutorial/index.html",
            title="The Python Tutorial",
            content="# Tutorial\n\nPython is...",
            score=87.5,
            sha256_hash="a" * 64,
            from_queries=["python 入门", "python 教程"],
            snippet="Python is an easy to learn, powerful programming language.",
            source_type="official-doc",
            tool_name="fetch_extract_one",
        )
        assert ev.score == 87.5
        assert ev.source_type == "official-doc"
        assert ev.tool_name == "fetch_extract_one"
        assert len(ev.from_queries) == 2


# ---------------------------------------------------------------------------
# IntentPlan / IntentAction
# ---------------------------------------------------------------------------


class TestIntentPlan:
    """IntentPlan schema：next_actions 三态（D1 拍板 fan-out 语义）。"""

    def test_zero_actions_for_early_exit(self):
        """next_actions = [] 且 early_exit=True 表示信息已充分。"""
        from brain_base.agents.schemas import IntentPlan
        plan = IntentPlan(next_actions=[], reasoning="evidence sufficient", early_exit=True)
        assert plan.next_actions == []
        assert plan.early_exit is True
        assert plan.reasoning == "evidence sufficient"

    def test_zero_actions_no_early_exit_triggers_no_action_branch(self):
        """next_actions = [] 且 early_exit=False（合法但 should_continue_intent 会触发 'no_action' 早退）。"""
        from brain_base.agents.schemas import IntentPlan
        plan = IntentPlan(next_actions=[], early_exit=False)
        assert plan.next_actions == []
        assert plan.early_exit is False

    def test_single_action_serial(self):
        """next_actions = [1 个] 表示串行单工具。"""
        from brain_base.agents.schemas import IntentAction, IntentPlan
        action = IntentAction(
            tool_name="fetch_url",
            tool_args={"url": "https://example.com", "question": "what is X"},
            purpose="深挖用户提供的 URL",
        )
        plan = IntentPlan(next_actions=[action], reasoning="follow user URL")
        assert len(plan.next_actions) == 1
        assert plan.next_actions[0].tool_name == "fetch_url"
        assert plan.next_actions[0].tool_args["url"] == "https://example.com"
        assert plan.early_exit is False  # default

    def test_multi_actions_fanout(self):
        """next_actions = [N>1 个] 表示 fan-out 并发执行（D1 拍板）。"""
        from brain_base.agents.schemas import IntentAction, IntentPlan
        actions = [
            IntentAction(tool_name="web_search", tool_args={"query": "Q1"}, purpose="覆盖 sub_q1"),
            IntentAction(tool_name="web_search", tool_args={"query": "Q2"}, purpose="覆盖 sub_q2"),
            IntentAction(tool_name="fetch_url", tool_args={"url": "https://x.com"}, purpose="深挖 URL"),
        ]
        plan = IntentPlan(next_actions=actions, reasoning="fan-out across 3 sub-questions")
        assert len(plan.next_actions) == 3
        assert {a.tool_name for a in plan.next_actions} == {"web_search", "fetch_url"}

    def test_action_tool_args_default_dict(self):
        """IntentAction.tool_args 默认为空 dict（避免 None 检查）。"""
        from brain_base.agents.schemas import IntentAction
        action = IntentAction(tool_name="web_search")
        assert action.tool_args == {}
        assert action.purpose == ""


# ---------------------------------------------------------------------------
# IntentObservation
# ---------------------------------------------------------------------------


class TestIntentObservation:
    """IntentObservation schema：confidence 严格 0-1 范围内。"""

    def test_default_construction(self):
        """全默认值：confidence=0.0, gaps=[], summary=''。"""
        from brain_base.agents.schemas import IntentObservation
        obs = IntentObservation()
        assert obs.new_evidence_count == 0
        assert obs.coverage_summary == ""
        assert obs.remaining_gaps == []
        assert obs.confidence == 0.0

    def test_confidence_boundaries(self):
        """confidence 边界 0.0 和 1.0 都接受。"""
        from brain_base.agents.schemas import IntentObservation
        IntentObservation(confidence=0.0)
        IntentObservation(confidence=1.0)
        IntentObservation(confidence=0.85)

    def test_confidence_rejects_negative(self):
        """confidence < 0 应触发 ValidationError。"""
        from brain_base.agents.schemas import IntentObservation
        with pytest.raises(ValidationError):
            IntentObservation(confidence=-0.01)

    def test_confidence_rejects_gt_one(self):
        """confidence > 1 应触发 ValidationError（与 ge=0,le=1 一致）。"""
        from brain_base.agents.schemas import IntentObservation
        with pytest.raises(ValidationError):
            IntentObservation(confidence=1.01)

    def test_new_evidence_count_rejects_negative(self):
        """new_evidence_count >= 0 强制约束。"""
        from brain_base.agents.schemas import IntentObservation
        with pytest.raises(ValidationError):
            IntentObservation(new_evidence_count=-1)

    def test_full_construction(self):
        """模拟 intent_observer 输出：本跳新增 2 条，覆盖部分子问题。"""
        from brain_base.agents.schemas import IntentObservation
        obs = IntentObservation(
            new_evidence_count=2,
            coverage_summary="已覆盖 sub_q1（X 是什么）和 sub_q2（X 用途）",
            remaining_gaps=["X 与 Y 区别"],
            confidence=0.6,
        )
        assert obs.new_evidence_count == 2
        assert "sub_q1" in obs.coverage_summary
        assert obs.remaining_gaps == ["X 与 Y 区别"]
        assert obs.confidence == 0.6


# ---------------------------------------------------------------------------
# GetInfoConfig.max_intent_iterations
# ---------------------------------------------------------------------------


class TestGetInfoConfigT47:
    """GetInfoConfig 新增 max_intent_iterations 默认值（D5 拍板）。"""

    def test_default_value_is_5(self):
        """默认值 5（契约 D5 拍板）。"""
        from brain_base.config import GetInfoConfig
        cfg = GetInfoConfig()
        assert cfg.max_intent_iterations == 5

    def test_can_override(self):
        """可通过构造函数覆盖（场景化覆盖默认值）。"""
        from brain_base.config import GetInfoConfig
        cfg = GetInfoConfig(max_intent_iterations=10)
        assert cfg.max_intent_iterations == 10


# ---------------------------------------------------------------------------
# QaState 字段（T47 新增 11 字段）
# ---------------------------------------------------------------------------


class TestQaStateT47Fields:
    """QaState TypedDict 新增 T47 字段——通过 type hints 验证字段已注册。"""

    def test_qa_state_has_t47_fields(self):
        """grep-style 验证：所有 11 个新字段在 QaState 注解中。"""
        from brain_base.graphs.qa_graph import QaState
        annotations = QaState.__annotations__
        expected_t47_fields = {
            "url_pre_fetch_content",
            "evidence_pool",
            "visited_urls",
            "iteration_count",
            "max_iterations",
            "intent_sufficient",
            "consecutive_intent_errors",
            "current_intent_plan",
            "current_action_results",
            "last_intent_observation",
            "conversation_history_summary",
        }
        missing = expected_t47_fields - set(annotations.keys())
        assert not missing, f"QaState 缺失 T47 字段：{missing}"

    def test_qa_state_drops_t46_hop_fields(self):
        """T47.6 已删除 T46 迭代多跳字段（保留 user_urls 作为 T47 输入字段）。

        反向断言：plan_type / max_hops / hops / hop_count / consecutive_tool_errors /
        current_tool_selection / current_tool_result 必须不在 QaState 中（T47.6 已删
        定义 + 初始化 + 全部读写节点）。user_urls 仍保留——extract_urls 写入、
        url_pre_fetch / intent_planner 读取，是 T47 拓扑的输入字段不是 T46 分流标志。
        """
        from brain_base.graphs.qa_graph import QaState
        annotations = QaState.__annotations__
        deleted_t46_fields = {
            "plan_type", "max_hops", "hops", "hop_count",
            "consecutive_tool_errors", "current_tool_selection", "current_tool_result",
        }
        leaked = deleted_t46_fields & set(annotations.keys())
        assert not leaked, f"T47.6 应删除 T46 hop 字段，但仍存在：{leaked}"
        # user_urls 保留：T47 extract_urls / url_pre_fetch / intent_planner 仍依赖
        assert "user_urls" in annotations, "user_urls 是 T47 输入字段，不能误删"
