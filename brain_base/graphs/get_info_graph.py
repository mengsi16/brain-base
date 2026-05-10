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

import asyncio
from operator import add
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from brain_base.nodes.get_info import (
    check_continue_node,
    create_classify_node,
    create_fan_out_to_preview,
    create_plan_node,
    create_preview_score_one,
    init_state_node,
    merge_scores_node,
    search_web_node,
)


class GetInfoState(TypedDict, total=False):
    """GetInfo 子图状态。

    T16 新增 ``scored_candidates`` reducer 字段：N 个 Send 并行返回的
    评分项通过 ``operator.add`` 自动合并。该字段在 ``GetInfoGraph.run()``
    初始化时必须给空 list ——否则 reducer 首次 add 会报错（审计陷阱 A）。
    """
    user_question: str
    queries_tried: list[str]
    candidates: list[dict]
    scored_candidates: Annotated[list[dict], add]  # T16 reducer
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
        # T16：新增并行预抳+评分节点（async def）
        workflow.add_node("preview_score_one", create_preview_score_one(llm))
        workflow.add_node("merge_scores", merge_scores_node)
        workflow.add_node("check_continue", check_continue_node)

        workflow.set_entry_point("init")
        workflow.add_edge("init", "plan")
        workflow.add_edge("plan", "search")
        workflow.add_edge("search", "classify")
        # T16 拓扑：classify 后 fan-out 到 preview_score_one（N 并行 Send）
        # 或者 llm=None / 全已评分时跳到 merge_scores。
        # path_map 只需映射字符串返回值；list[Send] 自动到目标节点（审计陷阱 D）。
        workflow.add_conditional_edges(
            "classify",
            create_fan_out_to_preview(llm),
            {"merge_scores": "merge_scores"},
        )
        workflow.add_edge("preview_score_one", "merge_scores")
        workflow.add_edge("merge_scores", "check_continue")
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
        """执行多步搜索。

        T16 后内部包含 async 节点 ``preview_score_one``，所以走
        ``graph.ainvoke()`` + ``asyncio.run()`` 包一层；上层调用者（QaGraph
        web_research_node）仍是 sync 不受影响。
        """
        initial: GetInfoState = {
            "user_question": user_question,
            "max_iterations": max_iterations,
            "target_official_count": target_official_count,
            "total_timeout": total_timeout,
            "scored_candidates": [],  # T16 reducer 字段必须初始化（审计陷阱 A）
        }
        # langgraph 默认有循环步数上限，按 max_iter * 5 设置 recursion_limit
        # T16 增加 preview_score_one + merge_scores 两节点，预留额外余量
        config = {"recursion_limit": max_iterations * 8 + 20}
        # T29：主协程 finally 主动关 playwright，避免 Windows ProactorEventLoop GC 噪音
        from brain_base.tools.web_fetcher import _with_shutdown as _pw_with_shutdown
        result = asyncio.run(_pw_with_shutdown(
            self.graph.ainvoke(initial, config=config)
        ))
        return dict(result)
