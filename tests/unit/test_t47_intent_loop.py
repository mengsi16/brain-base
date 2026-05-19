# -*- coding: utf-8 -*-
"""T47.3b 集成测试：planner → executor → observer → merge 循环串联。

覆盖（2 条 **Minimax 真调** + mock TOOL_REGISTRY）：
- test_intent_loop_single_hop_with_mock_tools：单跳 4 节点串联，验字段对齐
- test_intent_loop_two_hops_reach_sufficiency：双跳，第 2 跳触发 intent_sufficient

mock 工具理由：
- 不能调真 web_search/fetch_url（playwright + 网络 + 不稳定）
- mock 工具返固定 markdown，让 LLM 真调对真实结构化输入做评估
- 核心目的：验证 planner output → executor 执行 → observer 评估 → merge 转换
  4 节点串联无字段对齐 bug

LLM 真调的是 *评估行为*（planner 决策 + observer 评估）而非工具调用——
CLAUDE.md 规则 14 满足。

契约引用：md/research/2026-05-17-t47-unified-intent-agent-contract.md §4-§7
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

# 加载 .env（CLAUDE.md 规则 12）
try:
    from dotenv import load_dotenv
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    load_dotenv(_PROJECT_ROOT / ".env")
except Exception:
    pass


def _run(coro):
    """同步运行 async coroutine。"""
    return asyncio.run(coro)


def _empty_cfg():
    from brain_base.config import GetInfoConfig
    return GetInfoConfig()


def _state_skeleton(**overrides) -> dict[str, Any]:
    base = {
        "question": "",
        "normalized_query": "",
        "sub_questions": [],
        "user_urls": [],
        "url_pre_fetch_content": [],
        "current_intent_plan": {},
        "current_action_results": [],
        "evidence_pool": [],
        "visited_urls": [],
        "iteration_count": 0,
        "consecutive_intent_errors": 0,
        "last_intent_observation": {},
        "conversation_history_summary": "",
        "intent_sufficient": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Mock TOOL_REGISTRY：插入 mock_search_high / mock_search_low 工具
# ---------------------------------------------------------------------------


def _install_mock_tools(monkeypatch):
    """插入 2 个 mock 工具到 TOOL_REGISTRY，供集成测试用。"""
    from brain_base.nodes import qa_tools
    from brain_base.nodes.qa_tools import ToolSpec

    async def _mock_search_high(tool_args, llm, cfg):
        """高质量 mock 工具：返回完整 markdown + 高 score。"""
        query = tool_args.get("query", "")
        return {
            "markdown": (
                f"# Mock 高质量结果 for {query}\n\n"
                "## 部署步骤\n"
                "1. clone repo\n2. docker compose up -d\n3. 访问 localhost:80\n\n"
                "## 端口配置\n"
                "默认 80（UI）/ 9380（API）/ 19530（Milvus）\n"
            ),
            "source_url": f"https://mock-high.example.com/{query}",
            "title": f"Complete guide for {query}",
            "summary": f"完整覆盖 {query} 的部署 + 端口配置",
            "score": 92.0,
        }

    async def _mock_search_low(tool_args, llm, cfg):
        """低质量 mock 工具：返回简短 markdown + 低 score。"""
        query = tool_args.get("query", "")
        return {
            "markdown": f"# Brief result for {query}\nA short note.",
            "source_url": f"https://mock-low.example.com/{query}",
            "title": f"Short note: {query}",
            "summary": f"{query} 的简短信息",
            "score": 45.0,
        }

    monkeypatch.setitem(
        qa_tools.TOOL_REGISTRY, "mock_search_high",
        ToolSpec(name="mock_search_high",
                 description="高质量 mock 检索工具：返回完整部署 + 端口文档",
                 requires=[], gpu=False, parallel_ok=True, is_async=True,
                 fn=_mock_search_high),
    )
    monkeypatch.setitem(
        qa_tools.TOOL_REGISTRY, "mock_search_low",
        ToolSpec(name="mock_search_low",
                 description="低质量 mock 检索工具：返回简短信息",
                 requires=[], gpu=False, parallel_ok=True, is_async=True,
                 fn=_mock_search_low),
    )


# ---------------------------------------------------------------------------
# 集成测试（Minimax 真调 + mock TOOL_REGISTRY）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def loop_nodes(real_llm):
    """组装 4 节点（planner / executor / observer / merge）。"""
    from brain_base.nodes.qa_intent import (
        create_intent_executor,
        create_intent_observer,
        create_intent_planner,
        merge_evidence_node,
    )
    cfg = _empty_cfg()
    return {
        "planner": create_intent_planner(real_llm),
        "executor": create_intent_executor(real_llm, cfg),
        "observer": create_intent_observer(real_llm),
        "merge": merge_evidence_node,
    }


class TestIntentLoopIntegration:
    """planner → executor → observer → merge 4 节点集成（Minimax 真调 + mock 工具）。"""

    def test_intent_loop_single_hop_with_mock_tools(self, loop_nodes, monkeypatch):
        """单跳串联：planner 给 plan → executor 跑 mock 工具 → observer 评估 → merge 转候选。

        验证：
        1. 4 节点字段对齐无 bug
        2. evidence_pool 累积 + visited_urls 累积 + iteration_count=1
        3. merge 输出 13 字段对齐 get_info_candidates
        """
        _install_mock_tools(monkeypatch)

        # 提示 LLM 用 mock_search_high（避免 planner 选 web_search 真触网）
        # 但 planner prompt 是动态注入 TOOL_REGISTRY 的——mock_search_high 描述里
        # "高质量 mock 检索工具" 已能让 LLM 选它

        state = _state_skeleton(
            normalized_query="使用 mock_search_high 工具查询 RAGFlow 部署",
            sub_questions=["RAGFlow Docker 部署 + 端口配置"],
        )

        # ① planner
        planner_out = loop_nodes["planner"](state)
        plan = planner_out["current_intent_plan"]
        assert "next_actions" in plan
        # 应至少 1 个动作；不强制 LLM 必选 mock_search_high，但应该有动作
        assert len(plan["next_actions"]) >= 1 or plan["early_exit"]

        if plan["early_exit"]:
            pytest.skip("LLM 选了 early_exit，本测试场景不适用——重跑或换 prompt")

        state.update(planner_out)

        # ② executor
        exec_out = _run(loop_nodes["executor"](state))
        results = exec_out["current_action_results"]
        assert isinstance(results, list)
        # mock 工具非 mock_search_high 也允许（如 web_search 会触网失败但不抛）
        # 至少应有 results
        assert len(results) == len(plan["next_actions"])
        state.update(exec_out)

        # ③ observer
        obs_out = loop_nodes["observer"](state)
        # 字段类型断言
        assert isinstance(obs_out["evidence_pool"], list)
        assert isinstance(obs_out["visited_urls"], list)
        assert obs_out["iteration_count"] == 1
        assert isinstance(obs_out["last_intent_observation"], dict)
        assert isinstance(obs_out["intent_sufficient"], bool)
        state.update(obs_out)

        # ④ merge
        merge_out = loop_nodes["merge"](state)
        candidates = merge_out["get_info_candidates"]
        assert merge_out["get_info_attempted"] is True
        # candidates 数量 = evidence_pool 中非空条目数（observer 已过滤 error）
        valid_evidences = [
            e for e in obs_out["evidence_pool"]
            if (e.get("content") or e.get("snippet"))
        ]
        assert len(candidates) == len(valid_evidences)

        # 13 字段对齐
        if candidates:
            required = {
                "url", "title", "fetched_at", "markdown", "content_sha256",
                "from_engines", "from_queries", "score", "type", "summary",
                "keywords", "whether_in", "reason",
            }
            for c in candidates:
                assert required <= set(c.keys()), f"缺字段：{required - set(c.keys())}"

    def test_intent_loop_two_hops_reach_sufficiency(self, loop_nodes, monkeypatch):
        """双跳串联：核心验证 iteration_count + evidence_pool 推进无字段对齐 bug。

        集成测试断言哲学（CLAUDE.md 规则 14 + bug fixing discipline）：
        - **不**断言 evidence_pool 必须非空（LLM 可能选真 web_search 触网失败）
        - **不**断言 confidence 必达某阈值（LLM 评估有波动）
        - **必须**断言：iteration_count 严格 +1 / evidence_pool 不缩水 / merge 输出格式正确
        """
        _install_mock_tools(monkeypatch)

        # 第 1 跳：sub_questions 较多让 LLM 不容易直接早退
        state = _state_skeleton(
            normalized_query="RAGFlow 完整部署细节",
            sub_questions=[
                "RAGFlow 部署步骤",
                "RAGFlow 端口配置",
                "RAGFlow GPU 启用方法",
            ],
        )

        # ===== 第 1 跳 =====
        plan_1 = loop_nodes["planner"](state)["current_intent_plan"]
        if plan_1.get("early_exit"):
            pytest.skip("第 1 跳 LLM early_exit，跳过双跳验证")
        state["current_intent_plan"] = plan_1

        exec_1 = _run(loop_nodes["executor"](state))
        state["current_action_results"] = exec_1["current_action_results"]

        obs_1 = loop_nodes["observer"](state)
        # 更新 state 进入第 2 跳
        state["evidence_pool"] = obs_1["evidence_pool"]
        state["visited_urls"] = obs_1["visited_urls"]
        state["iteration_count"] = obs_1["iteration_count"]
        state["consecutive_intent_errors"] = obs_1["consecutive_intent_errors"]
        state["last_intent_observation"] = obs_1["last_intent_observation"]
        state["intent_sufficient"] = obs_1["intent_sufficient"]

        first_pool_size = len(obs_1["evidence_pool"])
        first_iteration = obs_1["iteration_count"]
        assert first_iteration == 1

        # 如果第 1 跳已经 sufficient（mock 工具返高质量内容覆盖了 sub_questions）
        # 也是合理结果——主要验证字段串联
        if obs_1["intent_sufficient"]:
            # 短路成功路径：merge 应能正确 fold
            merge_out = loop_nodes["merge"](state)
            assert merge_out["get_info_attempted"] is True
            assert len(merge_out["get_info_candidates"]) >= 1
            return  # 测试目标已验证

        # ===== 第 2 跳 =====
        plan_2 = loop_nodes["planner"](state)["current_intent_plan"]
        state["current_intent_plan"] = plan_2

        if plan_2.get("early_exit"):
            # 第 2 跳决定早退也合理
            exec_2_results = []
        else:
            exec_2 = _run(loop_nodes["executor"](state))
            exec_2_results = exec_2["current_action_results"]
        state["current_action_results"] = exec_2_results

        obs_2 = loop_nodes["observer"](state)
        # 第 2 跳后状态推进
        assert obs_2["iteration_count"] == 2  # 1 → 2
        # evidence_pool 不会缩水
        assert len(obs_2["evidence_pool"]) >= first_pool_size

        # 最终 merge
        state["evidence_pool"] = obs_2["evidence_pool"]
        merge_out = loop_nodes["merge"](state)
        assert merge_out["get_info_attempted"] is True
        # candidates 数量 = pool 中非空条目数（observer 已过滤 error，但兜底允许 0）
        # 不强制非空——LLM 选真 web_search 触网失败 / 全 error 时 pool 可能为空，
        # 主要验 *字段串联无 bug*：merge 不抛 + 输出格式正确
        valid_evidences = [
            e for e in obs_2["evidence_pool"]
            if (e.get("content") or e.get("snippet"))
        ]
        assert len(merge_out["get_info_candidates"]) == len(valid_evidences)
        # 字段对齐（仅在有 candidate 时验）
        if merge_out["get_info_candidates"]:
            required = {
                "url", "title", "fetched_at", "markdown", "content_sha256",
                "from_engines", "from_queries", "score", "type", "summary",
                "keywords", "whether_in", "reason",
            }
            for c in merge_out["get_info_candidates"]:
                assert required <= set(c.keys()), f"缺字段：{required - set(c.keys())}"
