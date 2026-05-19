# -*- coding: utf-8 -*-
"""T47.3b 单元测试：intent_observer 节点。

覆盖（4 条）：
- test_observer_factory_rejects_none_llm（非 LLM）：T27 fail-fast
- test_observer_appends_evidence_and_dedupes（mock LLM）：聚合 + 去重 + visited_urls 累积
- test_observer_consecutive_errors_counter（mock LLM）：全失败 +1 / 任一成功归 0 / early_exit 不变
- test_observer_minimax_judges_sufficiency（**Minimax 真调**）：充分证据触发 intent_sufficient

mock LLM 策略：测试 *节点状态聚合逻辑* 用 _FakeObserver 直返 IntentObservation，
LLM 语义验证留给条 4 真调（CLAUDE.md 规则 14）。

契约引用：md/research/2026-05-17-t47-unified-intent-agent-contract.md §6
"""

from __future__ import annotations

import os
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


def _state_skeleton(**overrides) -> dict[str, Any]:
    base = {
        "question": "",
        "normalized_query": "",
        "sub_questions": [],
        "current_intent_plan": {},
        "current_action_results": [],
        "evidence_pool": [],
        "visited_urls": [],
        "iteration_count": 0,
        "consecutive_intent_errors": 0,
        "last_intent_observation": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 非 LLM 测试：节点工厂 + mock LLM 验聚合逻辑
# ---------------------------------------------------------------------------


class _FakeObserverLLM:
    """mock LLM：with_structured_output 返回固定 IntentObservation。

    测试聚合逻辑用，不验 LLM 语义（CLAUDE.md 规则 14 豁免：测确定性状态聚合）。
    """

    def __init__(
        self,
        new_evidence_count: int = 0,
        coverage_summary: str = "mock summary",
        remaining_gaps: list[str] = None,
        confidence: float = 0.5,
    ):
        from brain_base.agents.schemas import IntentObservation
        self._obs = IntentObservation(
            new_evidence_count=new_evidence_count,
            coverage_summary=coverage_summary,
            remaining_gaps=remaining_gaps or [],
            confidence=confidence,
        )

    def with_structured_output(self, schema, **kwargs):
        outer_obs = self._obs

        class _Bound:
            def invoke(self, messages):
                return outer_obs

        return _Bound()

    def invoke(self, messages):
        raise AssertionError("should hit with_structured_output path")


class TestObserverFactoryAndAggregation:
    """工厂校验 + 状态聚合（去重、累积、错误计数）。"""

    def test_observer_factory_rejects_none_llm(self):
        """T27 fail-fast：llm=None 必须 raise（CLAUDE.md 规则 14）。"""
        from brain_base.nodes.qa_intent import create_intent_observer
        with pytest.raises(ValueError, match="non-None llm"):
            create_intent_observer(None)

    def test_observer_appends_evidence_and_dedupes(self):
        """新 result 追加 evidence_pool；同 url 二次出现去重；visited_urls 累积。"""
        from brain_base.nodes.qa_intent import create_intent_observer

        node = create_intent_observer(_FakeObserverLLM(
            new_evidence_count=2,
            coverage_summary="覆盖部分",
            remaining_gaps=["gap_b"],
            confidence=0.5,
        ))

        # 历史 evidence_pool 已有 1 条 url=A
        existing_evidence = {
            "url": "https://existing.example.com/a",
            "title": "Existing A",
            "content": "old markdown content for A",
            "score": 60.0,
            "sha256_hash": "old_sha_a",
            "from_queries": ["q_old"],
            "snippet": "existing snippet",
            "source_type": "community",
            "tool_name": "web_search",
        }
        state = _state_skeleton(
            normalized_query="test query",
            sub_questions=["sub_a", "sub_b"],
            current_intent_plan={
                "next_actions": [
                    {"tool_name": "web_search", "tool_args": {"query": "x"}, "purpose": "for sub_a"},
                    {"tool_name": "fetch_url", "tool_args": {"url": "https://existing.example.com/a"}, "purpose": "重复 url 应去重"},
                    {"tool_name": "fetch_url", "tool_args": {"url": "https://new.example.com/b"}, "purpose": "for sub_b"},
                ],
                "reasoning": "test",
                "early_exit": False,
            },
            current_action_results=[
                # 1. 新 url
                {
                    "tool_name": "web_search",
                    "tool_args": {"query": "x"},
                    "purpose": "for sub_a",
                    "markdown": "# new content for sub_a",
                    "source_url": "https://new.example.com/sub_a",
                    "title": "New A",
                    "summary": "snippet a",
                    "score": 75.0,
                    "error": "",
                },
                # 2. 重复 url - 应被跳过
                {
                    "tool_name": "fetch_url",
                    "tool_args": {"url": "https://existing.example.com/a"},
                    "purpose": "重复 url 应去重",
                    "markdown": "should be skipped",
                    "source_url": "https://existing.example.com/a",
                    "title": "Dup",
                    "summary": "dup",
                    "score": 80.0,
                    "error": "",
                },
                # 3. 又一个新 url
                {
                    "tool_name": "fetch_url",
                    "tool_args": {"url": "https://new.example.com/b"},
                    "purpose": "for sub_b",
                    "markdown": "# new content for sub_b",
                    "source_url": "https://new.example.com/b",
                    "title": "New B",
                    "summary": "snippet b",
                    "score": 88.0,
                    "error": "",
                },
            ],
            evidence_pool=[existing_evidence],
            visited_urls=["https://existing.example.com/a"],
            iteration_count=0,
            consecutive_intent_errors=0,
        )

        out = node(state)

        # evidence_pool：1 历史 + 2 新增（重复 url 跳过）
        pool = out["evidence_pool"]
        assert len(pool) == 3, f"expected 3 evidences (1 existing + 2 new, dup skipped), got {len(pool)}"
        # 第一条仍是 existing
        assert pool[0]["url"] == "https://existing.example.com/a"
        # 后两条是新增
        new_urls = {e["url"] for e in pool[1:]}
        assert new_urls == {"https://new.example.com/sub_a", "https://new.example.com/b"}

        # visited_urls：累积去重，按出现顺序
        visited = out["visited_urls"]
        assert visited == [
            "https://existing.example.com/a",
            "https://new.example.com/sub_a",
            "https://new.example.com/b",
        ]

        # iteration_count: 0 → 1
        assert out["iteration_count"] == 1
        # consecutive_errors: 有成功 → 归 0
        assert out["consecutive_intent_errors"] == 0
        # last_intent_observation: mock LLM 给的
        assert out["last_intent_observation"]["coverage_summary"] == "覆盖部分"
        # intent_sufficient：confidence=0.5 < 0.85 → False
        assert out["intent_sufficient"] is False

    def test_observer_consecutive_errors_counter(self):
        """全失败 +1 / 任一成功归 0 / early_exit (空 results) 不变。"""
        from brain_base.nodes.qa_intent import create_intent_observer

        node = create_intent_observer(_FakeObserverLLM(confidence=0.3))

        # 场景 1：本跳全失败（2 条都 error）→ counter +1
        state_all_fail = _state_skeleton(
            normalized_query="q",
            sub_questions=["s1"],
            current_intent_plan={
                "next_actions": [
                    {"tool_name": "fetch_url", "tool_args": {}, "purpose": "p1"},
                    {"tool_name": "web_search", "tool_args": {}, "purpose": "p2"},
                ],
                "early_exit": False,
            },
            current_action_results=[
                {"tool_name": "fetch_url", "purpose": "p1", "markdown": "", "source_url": "",
                 "title": "", "summary": "", "score": 0.0, "error": "fetch failed"},
                {"tool_name": "web_search", "purpose": "p2", "markdown": "", "source_url": "",
                 "title": "", "summary": "", "score": 0.0, "error": "search failed"},
            ],
            consecutive_intent_errors=1,
        )
        out_fail = node(state_all_fail)
        assert out_fail["consecutive_intent_errors"] == 2  # 1 → 2
        assert out_fail["iteration_count"] == 1
        assert out_fail["evidence_pool"] == []  # 全 error 不追加

        # 场景 2：任一成功 → counter 归 0
        state_one_ok = _state_skeleton(
            normalized_query="q",
            sub_questions=["s1"],
            current_intent_plan={"next_actions": [
                {"tool_name": "fetch_url", "tool_args": {}, "purpose": "p1"},
                {"tool_name": "web_search", "tool_args": {}, "purpose": "p2"},
            ], "early_exit": False},
            current_action_results=[
                {"tool_name": "fetch_url", "purpose": "p1", "markdown": "", "source_url": "",
                 "title": "", "summary": "", "score": 0.0, "error": "fail"},
                {"tool_name": "web_search", "purpose": "p2", "markdown": "ok content",
                 "source_url": "https://ok.com", "title": "OK", "summary": "s", "score": 70.0, "error": ""},
            ],
            consecutive_intent_errors=2,  # 之前累积过
        )
        out_ok = node(state_one_ok)
        assert out_ok["consecutive_intent_errors"] == 0  # 归 0
        assert len(out_ok["evidence_pool"]) == 1  # 只 1 条成功的写入

        # 场景 3：early_exit (空 results) → 不变
        state_empty = _state_skeleton(
            normalized_query="q",
            sub_questions=["s1"],
            current_intent_plan={"next_actions": [], "early_exit": True},
            current_action_results=[],
            consecutive_intent_errors=1,
            last_intent_observation={
                "new_evidence_count": 3,
                "coverage_summary": "充分",
                "remaining_gaps": [],
                "confidence": 0.91,
            },
        )
        out_empty = node(state_empty)
        # 空 results 跳过 LLM 调用，counter 保持原值
        assert out_empty["consecutive_intent_errors"] == 1
        # iteration 仍 +1
        assert out_empty["iteration_count"] == 1
        # last_intent_observation 透传上跳，但 new_evidence_count 强制清 0
        assert out_empty["last_intent_observation"]["new_evidence_count"] == 0
        assert out_empty["last_intent_observation"]["confidence"] == 0.91
        # 透传 confidence 0.91 + remaining_gaps=[] → intent_sufficient=True
        assert out_empty["intent_sufficient"] is True


# ---------------------------------------------------------------------------
# LLM 真调测试
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def observer_node(real_llm):
    """observer 节点工厂封装（real_llm 见 conftest.py，缺 key 直接 fail）。"""
    from brain_base.nodes.qa_intent import create_intent_observer
    return create_intent_observer(real_llm)


class TestObserverLLMRealCall:
    """observer LLM 真调（Minimax 优先）。"""

    def test_observer_minimax_judges_sufficiency(self, observer_node):
        """喂高质量 result（覆盖全部 sub_questions）→ confidence 较高 + remaining_gaps 趋空。

        宽松断言：LLM 评估有波动，主要验证 *字段类型 + 大致语义方向*，不卡死值。
        """
        state = _state_skeleton(
            normalized_query="RAGFlow 怎么部署？",
            sub_questions=["RAGFlow Docker 部署步骤", "RAGFlow 端口配置"],
            current_intent_plan={
                "next_actions": [
                    {"tool_name": "web_search", "tool_args": {"query": "RAGFlow 部署"},
                     "purpose": "查 Docker 部署 + 端口"},
                ],
                "reasoning": "首跳",
                "early_exit": False,
            },
            current_action_results=[
                {
                    "tool_name": "web_search",
                    "tool_args": {"query": "RAGFlow 部署"},
                    "purpose": "查 Docker 部署 + 端口",
                    "markdown": (
                        "# RAGFlow 部署完整指南\n\n"
                        "## Docker 部署\n"
                        "1. clone 仓库：`git clone https://github.com/infiniflow/ragflow`\n"
                        "2. cd ragflow/docker\n"
                        "3. 执行 `docker compose -f docker-compose.yml up -d`\n\n"
                        "## 端口配置\n"
                        "默认服务端口为 80（Web UI）+ 9380（API）+ 19530（Milvus）。\n"
                        "可通过 docker-compose.yml 中的 ports 字段修改。\n"
                    ),
                    "source_url": "https://github.com/infiniflow/ragflow/blob/main/docs/install.md",
                    "title": "RAGFlow Install Docs",
                    "summary": "RAGFlow Docker 部署步骤 + 端口配置完整说明",
                    "score": 92.0,
                    "error": "",
                },
            ],
            evidence_pool=[],
            visited_urls=[],
            iteration_count=0,
            consecutive_intent_errors=0,
        )

        out = observer_node(state)

        # 字段类型断言
        assert isinstance(out["evidence_pool"], list)
        assert isinstance(out["visited_urls"], list)
        assert isinstance(out["iteration_count"], int)
        assert isinstance(out["consecutive_intent_errors"], int)
        assert isinstance(out["last_intent_observation"], dict)
        assert isinstance(out["intent_sufficient"], bool)

        # 状态聚合
        assert len(out["evidence_pool"]) == 1
        assert out["evidence_pool"][0]["score"] == 92.0
        assert out["visited_urls"] == [
            "https://github.com/infiniflow/ragflow/blob/main/docs/install.md"
        ]
        assert out["iteration_count"] == 1
        assert out["consecutive_intent_errors"] == 0

        # LLM 输出语义检查（宽松）：
        obs = out["last_intent_observation"]
        assert obs["new_evidence_count"] >= 0  # 类型对
        assert 0.0 <= obs["confidence"] <= 1.0
        # markdown 完整覆盖了 Docker 部署 + 端口 → confidence 应较高（>0.5）
        assert obs["confidence"] >= 0.5, (
            f"高质量证据 + 覆盖 sub_questions 时 confidence 应 >0.5；"
            f"obs={obs}"
        )
        # remaining_gaps 必须从 sub_questions 摘录或为空
        for gap in obs["remaining_gaps"]:
            assert gap in state["sub_questions"], (
                f"remaining_gaps={gap!r} 不在 sub_questions={state['sub_questions']!r}"
            )
