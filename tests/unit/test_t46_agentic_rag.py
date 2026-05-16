# -*- coding: utf-8 -*-
"""T46 Agentic-RAG 单元测试。

覆盖：
- classify_plan 确定性快速路径（不调 LLM）
- hop_observer 纯状态更新
- merge_hop_evidence 格式转换
- TOOL_REGISTRY 注册完整性
- tool_selector 白名单校验 + fallback
- conditional_logic: after_classify_plan + should_continue_hopping
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# TOOL_REGISTRY
# ---------------------------------------------------------------------------


class TestToolRegistry:
    """TOOL_REGISTRY 注册完整性。"""

    def test_registry_has_4_tools(self):
        from brain_base.nodes.qa_tools import TOOL_REGISTRY
        assert set(TOOL_REGISTRY.keys()) == {"web_search", "fetch_url", "raw_text", "local_search"}

    def test_all_tools_have_fn(self):
        from brain_base.nodes.qa_tools import TOOL_REGISTRY
        for name, spec in TOOL_REGISTRY.items():
            assert spec.fn is not None, f"tool '{name}' has fn=None"
            assert spec.name == name

    def test_async_sync_flag(self):
        from brain_base.nodes.qa_tools import TOOL_REGISTRY
        assert TOOL_REGISTRY["web_search"].is_async is True
        assert TOOL_REGISTRY["fetch_url"].is_async is True
        assert TOOL_REGISTRY["raw_text"].is_async is False
        assert TOOL_REGISTRY["local_search"].is_async is False


# ---------------------------------------------------------------------------
# classify_plan 确定性快速路径
# ---------------------------------------------------------------------------


class TestClassifyPlanFastPath:
    """classify_plan 节点确定性路径，不调 LLM。"""

    def _make_node(self):
        """用 sentinel LLM 构造节点——快速路径不该触碰 LLM。"""
        from brain_base.nodes.qa import create_classify_plan_node

        class _Sentinel:
            def invoke(self, *a, **kw):
                raise AssertionError("LLM should not be called on fast path")
        return create_classify_plan_node(_Sentinel())

    def test_user_urls_triggers_direct_url(self):
        node = self._make_node()
        out = node({
            "user_urls": ["https://example.com"],
            "sub_questions": ["什么是 RAG？"],
            "decomposition_needed": False,
        })
        assert out["plan_type"] == "direct_url"
        assert out["max_hops"] == 0

    def test_multi_sub_questions_triggers_parallel(self):
        node = self._make_node()
        out = node({
            "user_urls": [],
            "sub_questions": ["Q1", "Q2", "Q3"],
            "decomposition_needed": True,
        })
        assert out["plan_type"] == "parallel"

    def test_single_sub_no_decomposition_triggers_parallel(self):
        """单子问题 + decomposition_needed=False → 不走第二快速路径，会调 LLM。
        但我们这里只测 user_urls 为空 + 多子问题但 decomposition_needed=False 的情况：
        不满足快速路径 2 → 会走 LLM 路径（这里会 raise）。
        """
        node = self._make_node()
        # 多子问题但 decomposition_needed=False → 不满足快速路径 2
        with pytest.raises(Exception):
            node({
                "user_urls": [],
                "sub_questions": ["Q1", "Q2"],
                "decomposition_needed": False,
            })


# ---------------------------------------------------------------------------
# hop_observer 纯状态更新
# ---------------------------------------------------------------------------


class TestHopObserver:
    """hop_observer_node 状态更新逻辑。"""

    def test_basic_state_update(self):
        from brain_base.nodes.qa_hop import hop_observer_node

        state = {
            "current_tool_selection": {
                "goal": "查找 X",
                "tool_name": "web_search",
                "tool_args": {"query": "X"},
                "stop_entity": "X 的值",
                "next_goals": ["查找 Y"],
                "reason": "需要先知道 X",
            },
            "current_tool_result": {
                "evidence": "X = 42",
                "resolved_entity": "42",
                "confidence": 0.9,
                "source_url": "https://example.com",
                "title": "Example",
                "markdown": "# X is 42",
            },
            "hops": [],
            "resolved_entities": {},
            "pending_goals": ["查找 X"],
            "hop_count": 0,
            "consecutive_tool_errors": 0,
        }
        out = hop_observer_node(state)

        assert out["hop_count"] == 1
        assert len(out["hops"]) == 1
        assert out["hops"][0]["goal"] == "查找 X"
        assert out["hops"][0]["resolved_entity"] == "42"
        assert out["resolved_entities"]["X 的值"] == "42"
        # pending_goals: pop "查找 X", append "查找 Y"
        assert out["pending_goals"] == ["查找 Y"]
        assert out["consecutive_tool_errors"] == 0

    def test_error_increments_consecutive(self):
        from brain_base.nodes.qa_hop import hop_observer_node

        state = {
            "current_tool_selection": {
                "goal": "查找 Z",
                "tool_name": "web_search",
                "tool_args": {},
                "stop_entity": "",
                "next_goals": [],
            },
            "current_tool_result": {
                "error": "timeout",
                "evidence": "",
                "resolved_entity": "",
            },
            "hops": [],
            "resolved_entities": {},
            "pending_goals": ["查找 Z"],
            "hop_count": 1,
            "consecutive_tool_errors": 1,
        }
        out = hop_observer_node(state)
        assert out["consecutive_tool_errors"] == 2
        assert out["hop_count"] == 2

    def test_template_substitution_in_next_goals(self):
        from brain_base.nodes.qa_hop import hop_observer_node

        state = {
            "current_tool_selection": {
                "goal": "查找导师",
                "tool_name": "web_search",
                "tool_args": {},
                "stop_entity": "导师",
                "next_goals": ["查找{导师}的导师"],
            },
            "current_tool_result": {
                "evidence": "导师是但昭义",
                "resolved_entity": "但昭义",
                "confidence": 0.95,
            },
            "hops": [],
            "resolved_entities": {},
            "pending_goals": ["查找导师"],
            "hop_count": 0,
            "consecutive_tool_errors": 0,
        }
        out = hop_observer_node(state)
        assert "查找但昭义的导师" in out["pending_goals"]


# ---------------------------------------------------------------------------
# merge_hop_evidence 格式转换
# ---------------------------------------------------------------------------


class TestMergeHopEvidence:
    """merge_hop_evidence_node 格式转换。"""

    def test_filters_errors_and_empty(self):
        from brain_base.nodes.qa_hop import merge_hop_evidence_node

        state = {
            "hops": [
                {"evidence": "good", "markdown": "# Good", "confidence": 0.9,
                 "source_url": "u1", "title": "T1", "goal": "G1"},
                {"error": "timeout", "evidence": "", "markdown": "",
                 "confidence": 0, "source_url": "", "title": "", "goal": "G2"},
                {"evidence": "", "markdown": "", "confidence": 0,
                 "source_url": "", "title": "", "goal": "G3"},
            ],
        }
        out = merge_hop_evidence_node(state)
        candidates = out["get_info_candidates"]
        assert len(candidates) == 1
        assert candidates[0]["url"] == "u1"
        assert candidates[0]["whether_in"] is True
        assert out["get_info_attempted"] is True

    def test_empty_hops(self):
        from brain_base.nodes.qa_hop import merge_hop_evidence_node

        out = merge_hop_evidence_node({"hops": []})
        assert out["get_info_candidates"] == []


# ---------------------------------------------------------------------------
# tool_selector 白名单校验
# ---------------------------------------------------------------------------


class TestToolSelector:
    """tool_selector_node 白名单 + fallback。"""

    def test_valid_tool_passes_through(self):
        from brain_base.nodes.qa_hop import tool_selector_node

        state = {
            "current_tool_selection": {
                "tool_name": "web_search",
                "goal": "查找 X",
                "tool_args": {"query": "X"},
            },
            "infra_status": {},
        }
        out = tool_selector_node(state)
        assert out["current_tool_selection"]["tool_name"] == "web_search"

    def test_invalid_tool_fallback(self):
        from brain_base.nodes.qa_hop import tool_selector_node

        state = {
            "current_tool_selection": {
                "tool_name": "nonexistent_tool",
                "goal": "查找 X",
                "tool_args": {"url": "http://x"},
            },
            "infra_status": {},
        }
        out = tool_selector_node(state)
        assert out["current_tool_selection"]["tool_name"] == "web_search"
        assert out["current_tool_selection"]["tool_args"] == {"query": "查找 X"}


# ---------------------------------------------------------------------------
# conditional_logic
# ---------------------------------------------------------------------------


class TestConditionalLogic:
    """after_classify_plan + should_continue_hopping。"""

    def setup_method(self):
        from brain_base.graph.conditional_logic import ConditionalLogic
        self.logic = ConditionalLogic()

    def test_after_classify_plan_parallel(self):
        assert self.logic.after_classify_plan({"plan_type": "parallel"}) == "parallel"

    def test_after_classify_plan_iterative(self):
        assert self.logic.after_classify_plan({"plan_type": "iterative"}) == "hop_planner"

    def test_after_classify_plan_direct_url(self):
        assert self.logic.after_classify_plan({"plan_type": "direct_url"}) == "fetch_user_urls"

    def test_after_classify_plan_default(self):
        assert self.logic.after_classify_plan({}) == "parallel"

    def test_hopping_errors_exit(self):
        assert self.logic.should_continue_hopping({
            "consecutive_tool_errors": 2,
            "hop_count": 0,
            "max_hops": 5,
            "pending_goals": ["G1"],
        }) == "merge_hop_evidence"

    def test_hopping_max_hops_exit(self):
        assert self.logic.should_continue_hopping({
            "consecutive_tool_errors": 0,
            "hop_count": 3,
            "max_hops": 3,
            "pending_goals": ["G1"],
        }) == "merge_hop_evidence"

    def test_hopping_empty_goals_exit(self):
        assert self.logic.should_continue_hopping({
            "consecutive_tool_errors": 0,
            "hop_count": 1,
            "max_hops": 5,
            "pending_goals": [],
        }) == "merge_hop_evidence"

    def test_hopping_continue(self):
        assert self.logic.should_continue_hopping({
            "consecutive_tool_errors": 0,
            "hop_count": 1,
            "max_hops": 5,
            "pending_goals": ["G2"],
        }) == "hop_planner"


# ---------------------------------------------------------------------------
# Graph compilation（拓扑完整性）
# ---------------------------------------------------------------------------


class TestGraphCompilation:
    """图能编译 = 所有节点已注册、所有边已连接、无悬空节点。"""

    def test_qa_graph_compiles(self, mock_llm):
        from brain_base.graphs.qa_graph import QaGraph

        g = QaGraph(llm=mock_llm)
        assert g.graph is not None

    def test_new_nodes_registered(self, mock_llm):
        from brain_base.graphs.qa_graph import QaGraph

        g = QaGraph(llm=mock_llm)
        node_names = set(g.graph.nodes.keys())
        for name in [
            "classify_plan", "hop_planner", "tool_selector",
            "tool_executor", "hop_observer", "merge_hop_evidence",
            "fetch_user_urls",
        ]:
            assert name in node_names, f"node '{name}' not found in graph"
