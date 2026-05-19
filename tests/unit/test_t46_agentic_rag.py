# -*- coding: utf-8 -*-
"""T46 Agentic-RAG 残留单元测试（T47.5 清退后）。

覆盖（仅保留与 T47 仍相关的部分）：
- TOOL_REGISTRY 注册完整性（intent_executor 仍依赖该注册表）
- 主图编译 + T47.4 节点拓扑断言（6 新节点 in / 14 老节点 out）

T47.5 删除的 class（节点函数本体 T47.6 一并删）：
- TestClassifyPlanFastPath：classify_plan 已被统一意图识别 Agent-Loop 替代
- TestHopObserver：hop_observer 已被 intent_observer 替代
  → 行为覆盖见 tests/unit/test_t47_intent_observer.py
- TestMergeHopEvidence：merge_hop_evidence 已被 merge_evidence_node 替代
  → 行为覆盖见 tests/unit/test_t47_merge_evidence.py
- TestToolSelector：tool_selector 节点已删，intent_executor 直接按
  TOOL_REGISTRY key dispatch → 行为覆盖见 tests/unit/test_t47_intent_executor.py
"""

from __future__ import annotations

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
# T47.5 删除：4 个直调旧节点函数的 class
# - TestClassifyPlanFastPath / TestHopObserver / TestMergeHopEvidence /
#   TestToolSelector
#
# 节点函数本身（qa.py:create_classify_plan_node / qa_hop.py:* 等）T47.6 才删
# 旧节点行为已被以下 T47 新测试替代：
# - test_t47_intent_planner.py：意图决策（替代 classify_plan）
# - test_t47_intent_observer.py：证据池聚合 + early-exit 判定（替代 hop_observer）
# - test_t47_merge_evidence.py：Evidence pool → get_info_candidates 13 字段
#   （替代 merge_hop_evidence）
# - test_t47_intent_executor.py：TOOL_REGISTRY dispatch + 白名单 + 失败隔离
#   （替代 tool_selector）
#
# T47.4 删除：TestConditionalLogic class（8 条调 after_classify_plan /
# should_continue_hopping 的测试）。两个路由函数本身已从 conditional_logic.py
# 删除。新路由（route_after_extract_urls / should_continue_intent / 改写后的
# after_crystallized_check）测试见 tests/unit/test_t47_routes.py。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Graph compilation（拓扑完整性）
# ---------------------------------------------------------------------------


class TestGraphCompilation:
    """图能编译 = 所有节点已注册、所有边已连接、无悬空节点。"""

    def test_qa_graph_compiles(self, mock_llm):
        from brain_base.graphs.qa_graph import QaGraph

        g = QaGraph(llm=mock_llm)
        assert g.graph is not None

    def test_t47_nodes_registered(self, mock_llm):
        """T47.4 主图重组后：验证 6 个新节点均注册，T46 老节点都不再出现。

        该测试替代 T46 时代的 test_new_nodes_registered（原断言主图含
        classify_plan / hop_* / fetch_user_urls，T47.4 后这些均从主图删除）。
        """
        from brain_base.graphs.qa_graph import QaGraph

        g = QaGraph(llm=mock_llm)
        node_names = set(g.graph.nodes.keys())

        # T47.4 新节点：6 个均需注册
        for name in [
            "extract_urls",
            "url_pre_fetch",
            "intent_planner",
            "intent_executor",
            "intent_observer",
            "merge_evidence",
        ]:
            assert name in node_names, f"T47 新节点 '{name}' 未注册到主图"

        # T46 老节点：14 个均不应出现在主图中（T47.4 已拔除，文件本身 T47.6 删）
        for name in [
            "classify_plan",
            "subquery_prep",
            "barrier1",
            "merge_search_keywords",
            "search_web_dual",
            "fetch_extract_one",
            "barrier_extract",
            "search_strategy",
            "hop_planner",
            "tool_selector",
            "tool_executor",
            "hop_observer",
            "merge_hop_evidence",
            "fetch_user_urls",
        ]:
            assert name not in node_names, f"T46 老节点 '{name}' 仍出现在主图中，T47.4 wiring 未拔除干净"
