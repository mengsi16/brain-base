"""
GetInfo 子图：多步搜索循环（plan-search-classify-loop）。

拓扑：
    init → plan → search → classify → check_continue
                                          │
                                          ├── continue → 回到 plan
                                          └── end → END

终止条件全部由 `check_continue_node` 用 Python 判定（CLAUDE.md：路由
属代码层，不放在 prompt 里）。
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from brain_base.nodes.get_info import (
    check_continue_node,
    create_classify_node,
    create_plan_node,
    init_state_node,
    search_web_node,
)


class GetInfoState(TypedDict, total=False):
    """GetInfo 子图状态。"""
    user_question: str
    queries_tried: list[str]
    candidates: list[dict]
    raw_serp: list[dict]
    next_query: str
    next_mode: str
    next_engine: str
    iteration: int
    max_iterations: int
    target_official_count: int
    per_iteration_timeout: float
    total_timeout: float
    started_at: float
    degraded: bool
    degraded_reason: str | None
    _route: str


def _route_after_check(state: dict[str, Any]) -> str:
    """check_continue 的条件边路由。"""
    return state.get("_route", "end")


class GetInfoGraph:
    """多步搜索循环图。

    Args:
        llm: 可选；llm=None 时 plan 节点退化为「首轮直接用 user_question
             搜索一次后终止」，classify 节点用域名启发式。
    """

    def __init__(self, llm: Any = None):
        self.llm = llm
        workflow = StateGraph(GetInfoState)

        workflow.add_node("init", init_state_node)
        workflow.add_node("plan", create_plan_node(llm))
        workflow.add_node("search", search_web_node)
        workflow.add_node("classify", create_classify_node(llm))
        workflow.add_node("check_continue", check_continue_node)

        workflow.set_entry_point("init")
        workflow.add_edge("init", "plan")
        workflow.add_edge("plan", "search")
        workflow.add_edge("search", "classify")
        workflow.add_edge("classify", "check_continue")
        workflow.add_conditional_edges(
            "check_continue",
            _route_after_check,
            {"continue": "plan", "end": END},
        )

        self.graph = workflow.compile()

    def run(
        self,
        user_question: str,
        max_iterations: int = 5,
        target_official_count: int = 3,
        total_timeout: float = 90.0,
    ) -> dict[str, Any]:
        """执行多步搜索。"""
        initial: GetInfoState = {
            "user_question": user_question,
            "max_iterations": max_iterations,
            "target_official_count": target_official_count,
            "total_timeout": total_timeout,
        }
        # langgraph 默认有循环步数上限，按 max_iter * 5 设置 recursion_limit
        config = {"recursion_limit": max_iterations * 5 + 10}
        result = self.graph.invoke(initial, config=config)
        return dict(result)
