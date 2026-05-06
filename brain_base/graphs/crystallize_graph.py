"""
Crystallize 子图：固化层命中判断 + 写入。

参考 TradingAgents 的 GraphSetup 类模式。
两种调用模式：
1. hit_check：qa-workflow 步骤 0 调用，判断固化层是否命中
2. crystallize：qa-workflow 步骤末尾调用，写入新固化条目
"""

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from brain_base.graph.conditional_logic import ConditionalLogic
from brain_base.nodes.crystallize import (
    crystallize_write_node,
    freshness_check_node,
    hit_check_node,
)


class CrystallizeState(TypedDict, total=False):
    """固化层状态"""
    mode: str  # hit_check / crystallize
    user_question: str
    extracted_entities: list[str]
    status: str  # hit_fresh / hit_stale / cold_observed / cold_promoted / miss / degraded
    skill_id: str
    answer_markdown: str
    cold_evidence_summary: str
    layer: str
    last_confirmed_at: str
    freshness_ttl_days: int
    value_score: float
    trigger_keywords: list[str]
    description: str


class CrystallizeGraph:
    """固化层子图"""

    def __init__(self):
        self.routing = ConditionalLogic()
        workflow = StateGraph(CrystallizeState)

        workflow.add_node("hit_check", hit_check_node)
        workflow.add_node("freshness_check", freshness_check_node)

        workflow.set_entry_point("hit_check")
        workflow.add_conditional_edges(
            "hit_check",
            self.routing.after_hit_check,
            {"freshness": "freshness_check", "end": END},
        )
        workflow.add_edge("freshness_check", END)

        self.graph = workflow.compile()

    def hit_check(self, user_question: str, extracted_entities: list[str] | None = None) -> dict[str, Any]:
        """固化层命中判断"""
        initial: CrystallizeState = {
            "mode": "hit_check",
            "user_question": user_question,
            "extracted_entities": extracted_entities or [],
        }
        result = self.graph.invoke(initial)
        return dict(result)

    @staticmethod
    def crystallize(
        user_question: str,
        answer_markdown: str,
        value_score: float = 0.0,
        trigger_keywords: list[str] | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        """写入新固化条目（直接调用节点，不走图）"""
        state: dict[str, Any] = {
            "mode": "crystallize",
            "user_question": user_question,
            "answer_markdown": answer_markdown,
            "value_score": value_score,
            "trigger_keywords": trigger_keywords or [],
            "description": description,
        }
        return crystallize_write_node(state)
