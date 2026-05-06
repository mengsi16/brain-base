"""
LifecycleGraph：文档生命周期管理（删除/归档）。

参考 TradingAgents 的 GraphSetup 类模式。
"""

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from brain_base.graph.conditional_logic import ConditionalLogic
from brain_base.nodes.lifecycle import (
    audit_log_node,
    clean_index_node,
    delete_files_node,
    delete_milvus_node,
    dry_run_report_node,
    resolve_doc_ids_node,
    scan_impact_node,
)


class LifecycleState(TypedDict, total=False):
    """生命周期管理状态"""
    doc_ids: list[str]
    urls: list[str]
    sha256: str
    confirm: bool
    force_recent: bool
    reason: str
    resolved_doc_ids: list[str]
    targets: list[dict]
    dry_run_report: dict
    milvus_delete_result: dict
    milvus_delete_failed: bool
    file_delete_errors: list[str]
    index_clean_errors: list[str]
    audit_log_path: str
    error: str


class LifecycleGraph:
    """文档生命周期管理图"""

    def __init__(self):
        self.routing = ConditionalLogic()
        workflow = StateGraph(LifecycleState)

        workflow.add_node("resolve", resolve_doc_ids_node)
        workflow.add_node("scan", scan_impact_node)
        workflow.add_node("dry_run", dry_run_report_node)
        workflow.add_node("delete_milvus", delete_milvus_node)
        workflow.add_node("delete_files", delete_files_node)
        workflow.add_node("clean_index", clean_index_node)
        workflow.add_node("audit", audit_log_node)

        workflow.set_entry_point("resolve")
        workflow.add_edge("resolve", "scan")
        workflow.add_edge("scan", "dry_run")
        workflow.add_conditional_edges(
            "dry_run",
            self.routing.should_execute_lifecycle,
            {"continue": "delete_milvus", "end": END},
        )
        workflow.add_edge("delete_milvus", "delete_files")
        workflow.add_edge("delete_files", "clean_index")
        workflow.add_edge("clean_index", "audit")
        workflow.add_edge("audit", END)

        self.graph = workflow.compile()

    def run(
        self,
        doc_ids: list[str] | None = None,
        urls: list[str] | None = None,
        sha256: str = "",
        confirm: bool = False,
        force_recent: bool = False,
        reason: str = "",
    ) -> dict[str, Any]:
        initial: LifecycleState = {
            "doc_ids": doc_ids or [],
            "urls": urls or [],
            "sha256": sha256,
            "confirm": confirm,
            "force_recent": force_recent,
            "reason": reason,
        }
        result = self.graph.invoke(initial)
        return dict(result)
