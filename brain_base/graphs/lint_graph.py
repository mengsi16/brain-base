"""
LintGraph：固化层周期清理。

参考 TradingAgents 的 GraphSetup 类模式。
"""

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from brain_base.nodes.lint import (
    check_freshness_node,
    delete_rejected_node,
    degrade_expired_node,
    scan_crystallized_node,
)


class LintState(TypedDict, total=False):
    """固化层清理状态"""
    entries: list[dict]
    scan_status: str
    to_degrade: list[str]
    to_delete: list[str]
    to_keep: list[str]
    degraded: list[str]
    deleted: list[str]


class LintGraph:
    """固化层清理图"""

    def __init__(self):
        workflow = StateGraph(LintState)

        workflow.add_node("scan", scan_crystallized_node)
        workflow.add_node("check", check_freshness_node)
        workflow.add_node("degrade", degrade_expired_node)
        workflow.add_node("delete", delete_rejected_node)

        workflow.set_entry_point("scan")
        workflow.add_edge("scan", "check")
        workflow.add_edge("check", "degrade")
        workflow.add_edge("degrade", "delete")
        workflow.add_edge("delete", END)

        self.graph = workflow.compile()

    def run(self) -> dict[str, Any]:
        result = self.graph.invoke({})
        return dict(result)
