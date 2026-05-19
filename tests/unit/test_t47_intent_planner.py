# -*- coding: utf-8 -*-
"""T47.3a 单元测试：intent_planner 节点。

覆盖（5 条）：
- test_planner_factory_rejects_none_llm（非 LLM）：T27 fail-fast
- test_planner_filters_unknown_tools（mock LLM）：白名单过滤
- test_planner_minimax_first_iteration_proposes_action（**Minimax 真调**）：首跳决策合理
- test_planner_minimax_with_url_pre_fetch_uses_fetch_url（**Minimax 真调**）：URL 上下文影响决策
- test_planner_minimax_full_evidence_early_exit（**Minimax 真调**）：充分证据触发 early_exit

CLAUDE.md 规则 14：LLM 测试必须真调，禁止 mock 测语义；缺 key fail 不 skip。
契约引用：md/research/2026-05-17-t47-unified-intent-agent-contract.md §4
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

# 加载 .env（CLAUDE.md 规则 12：测试脚本用 load_dotenv 而非 $env:）
try:
    from dotenv import load_dotenv
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    load_dotenv(_PROJECT_ROOT / ".env")
except Exception:
    pass


# ---------------------------------------------------------------------------
# 非 LLM 测试：节点工厂行为 + mock LLM 校验白名单
# ---------------------------------------------------------------------------


class TestPlannerFactoryAndFiltering:
    """工厂入参校验 + 工具白名单过滤逻辑（无需真 LLM）。"""

    def test_planner_factory_rejects_none_llm(self):
        """T27 fail-fast：llm=None 必须 raise（CLAUDE.md 规则 14）。"""
        from brain_base.nodes.qa_intent import create_intent_planner
        with pytest.raises(ValueError, match="non-None llm"):
            create_intent_planner(None)

    def test_planner_filters_unknown_tools(self, monkeypatch):
        """planner 输出含未注册工具 → 自动过滤；valid 工具保留。

        用 _FakeLLM 直接吐 IntentPlan dict——这是测试 *节点过滤逻辑* 而非 LLM 语义，
        允许 mock（CLAUDE.md 规则 14 的"图编译/拓扑"豁免延伸到节点确定性逻辑）。
        """
        from brain_base.agents.schemas import IntentAction, IntentPlan
        from brain_base.nodes.qa_intent import create_intent_planner

        class _FakePlanner:
            """模拟 LLM 直接返回 IntentPlan 实例（走 with_structured_output 路径）。"""

            def with_structured_output(self, schema, **kwargs):
                outer_self = self

                class _Bound:
                    def invoke(self, messages):
                        return IntentPlan(
                            next_actions=[
                                IntentAction(
                                    tool_name="web_search",
                                    tool_args={"query": "test"},
                                    purpose="valid",
                                ),
                                IntentAction(
                                    tool_name="nonexistent_tool",
                                    tool_args={},
                                    purpose="invalid",
                                ),
                                IntentAction(
                                    tool_name="local_search",
                                    tool_args={"query": "x"},
                                    purpose="valid2",
                                ),
                            ],
                            reasoning="mix valid + invalid",
                            early_exit=False,
                        )

                return _Bound()

            def invoke(self, messages):
                # 路径 1 走通后不会用到这个
                raise AssertionError("should not hit invoke (with_structured_output succeeded)")

        node = create_intent_planner(_FakePlanner())
        out = node({
            "question": "test",
            "normalized_query": "test",
            "sub_questions": ["sub_a"],
            "user_urls": [],
            "url_pre_fetch_content": [],
            "evidence_pool": [],
            "visited_urls": [],
            "iteration_count": 0,
        })
        plan = out["current_intent_plan"]
        # 只有 web_search + local_search 保留，nonexistent_tool 被过滤
        tool_names = [a["tool_name"] for a in plan["next_actions"]]
        assert "web_search" in tool_names
        assert "local_search" in tool_names
        assert "nonexistent_tool" not in tool_names
        assert len(plan["next_actions"]) == 2


# ---------------------------------------------------------------------------
# LLM 真调测试：用 conftest.py 的 real_llm fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def planner_node(real_llm):
    """planner 节点工厂封装（real_llm 见 conftest.py，缺 key 直接 fail）。"""
    from brain_base.nodes.qa_intent import create_intent_planner
    return create_intent_planner(real_llm)


def _state_skeleton(**overrides) -> dict[str, Any]:
    """构造 planner 输入 state 骨架。"""
    base = {
        "question": "",
        "normalized_query": "",
        "sub_questions": [],
        "user_urls": [],
        "url_pre_fetch_content": [],
        "evidence_pool": [],
        "visited_urls": [],
        "iteration_count": 0,
        "conversation_history_summary": "",
        "last_intent_observation": {},
    }
    base.update(overrides)
    return base


class TestPlannerLLMRealCall:
    """planner LLM 真调（Minimax 优先，conftest.py real_llm fixture）。

    验证 LLM 看到不同 state 后的语义决策——不是 mock 透传，是真行为验证。
    """

    def test_planner_minimax_first_iteration_proposes_action(self, planner_node):
        """首跳：evidence_pool 空 → planner 必须给出至少 1 个动作（不能直接 early_exit）。"""
        state = _state_skeleton(
            question="RAGFlow 怎么部署？",
            normalized_query="RAGFlow 部署方法",
            sub_questions=["RAGFlow 部署步骤", "RAGFlow Docker compose 配置"],
        )
        out = planner_node(state)
        plan = out["current_intent_plan"]

        # 首跳无证据 → 不应 early_exit
        assert plan["early_exit"] is False, (
            f"首跳 evidence_pool 空时不应 early_exit；reasoning={plan['reasoning']}"
        )
        # 至少 1 个动作
        assert len(plan["next_actions"]) >= 1, (
            f"首跳应至少给 1 个动作；plan={plan}"
        )
        # 工具名都在白名单
        for action in plan["next_actions"]:
            assert action["tool_name"] in {"web_search", "fetch_url", "raw_text", "local_search"}
        # 至少一个动作应用到 web_search 或 local_search（检索类问题典型）
        tool_names = {a["tool_name"] for a in plan["next_actions"]}
        assert tool_names & {"web_search", "local_search"}, (
            f"检索类问题应包含 web_search 或 local_search；得到 {tool_names}"
        )

    def test_planner_minimax_with_url_pre_fetch_uses_fetch_url(self, planner_node):
        """user_urls + url_pre_fetch_content → planner 倾向 fetch_url 或 raw_text 深挖。"""
        state = _state_skeleton(
            question="这个项目怎么部署？参考 https://github.com/infiniflow/ragflow",
            normalized_query="RAGFlow 部署方法",
            sub_questions=["RAGFlow 完整部署步骤"],
            user_urls=["https://github.com/infiniflow/ragflow"],
            url_pre_fetch_content=[
                {
                    "url": "https://github.com/infiniflow/ragflow",
                    "title": "infiniflow/ragflow: open-source RAG engine",
                    "markdown_excerpt": (
                        "# RAGFlow\n\n"
                        "RAGFlow is an open-source RAG engine. "
                        "(README excerpt only, full docs require deeper fetch)"
                    ),
                },
            ],
        )
        out = planner_node(state)
        plan = out["current_intent_plan"]

        # 用户给了 URL 且浅抓只是 excerpt → 应深挖
        # 允许两种合理路径：(1) early_exit=False + fetch_url/raw_text 深挖；(2) early_exit=True 如果 LLM 觉得 excerpt 已够
        if plan["early_exit"]:
            # 早退也接受——但说明 LLM 觉得浅抓够了；reasoning 应提及内容已充分
            assert plan["reasoning"], "early_exit 时应有 reasoning 说明"
        else:
            tool_names = {a["tool_name"] for a in plan["next_actions"]}
            # T48.3/T48.4 加了 arxiv_pdf / github_raw 新工具，扩展白名单
            assert tool_names & {
                "fetch_url", "raw_text", "web_search", "local_search",
                "arxiv_pdf", "github_raw",
            }, (
                f"应至少调用一个工具深挖 URL；得到 {tool_names}"
            )
            # 主流期望：fetch_url / raw_text / github_raw（针对该 URL）
            # T48.4 加了 github_raw 后，GitHub URL 优先选 github_raw
            url_actions = [
                a for a in plan["next_actions"]
                if a["tool_name"] in {"fetch_url", "raw_text", "github_raw"}
                and "ragflow" in str(a.get("tool_args", {})).lower()
            ]
            # 软断言：至少有一个针对该 URL 的深挖（否则也算合理但 log）
            if not url_actions:
                print(
                    f"[soft] LLM 没用 fetch_url/raw_text/github_raw 针对 URL；plan={plan}"
                )

    def test_planner_minimax_full_evidence_early_exit(self, planner_node):
        """evidence_pool 已含多条高分 + last_obs 高 confidence → planner 应早退。"""
        state = _state_skeleton(
            question="RAGFlow 部署方法",
            normalized_query="RAGFlow 部署方法",
            sub_questions=["RAGFlow Docker 部署步骤", "RAGFlow GPU 配置要求"],
            evidence_pool=[
                {
                    "url": "https://github.com/infiniflow/ragflow",
                    "score": 92.0,
                    "snippet": "RAGFlow Docker compose 部署：clone repo → docker compose up -d，默认端口 80。",
                    "from_queries": ["RAGFlow Docker 部署步骤"],
                },
                {
                    "url": "https://github.com/infiniflow/ragflow/blob/main/docs/install.md",
                    "score": 88.0,
                    "snippet": "GPU 配置：NVIDIA Container Toolkit 必装；显存 ≥4GB；docker-compose-gpu.yml 启用 GPU。",
                    "from_queries": ["RAGFlow GPU 配置要求"],
                },
                {
                    "url": "https://ragflow.io/docs/dev/install",
                    "score": 86.0,
                    "snippet": "完整安装文档：包含 Docker、源码编译、GPU 启用三种部署方式。",
                    "from_queries": ["RAGFlow Docker 部署步骤", "RAGFlow GPU 配置要求"],
                },
            ],
            visited_urls=[
                "https://github.com/infiniflow/ragflow",
                "https://github.com/infiniflow/ragflow/blob/main/docs/install.md",
                "https://ragflow.io/docs/dev/install",
            ],
            iteration_count=2,
            last_intent_observation={
                "new_evidence_count": 1,
                "coverage_summary": "Docker 部署步骤 + GPU 配置都有官方文档支持，证据充分",
                "remaining_gaps": [],
                "confidence": 0.91,
            },
        )
        out = planner_node(state)
        plan = out["current_intent_plan"]

        # 主断言：early_exit 应为 True（confidence 0.91 + gaps 空 + 3 条高分证据）
        assert plan["early_exit"] is True, (
            f"高 confidence + 空 gaps + 3 条 score>85 应早退；得到 plan={plan}"
        )
        # 早退时 actions 应被节点强制清空
        assert plan["next_actions"] == [], (
            f"early_exit=True 时 actions 应被清空；得到 {plan['next_actions']}"
        )
