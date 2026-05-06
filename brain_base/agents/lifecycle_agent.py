"""
Lifecycle Agent 工厂函数。
"""

from typing import Any, Callable

from brain_base.graphs.lifecycle_graph import LifecycleGraph


def create_lifecycle_agent() -> Callable:
    """创建 lifecycle agent 节点函数"""

    def lifecycle_agent_node(state: dict[str, Any]) -> dict[str, Any]:
        graph = LifecycleGraph()
        doc_ids = state.get("doc_ids", [])
        urls = state.get("urls", [])
        sha256 = state.get("sha256", "")
        if not (doc_ids or urls or sha256):
            return {"error": "lifecycle_agent: 必须提供 doc_ids / urls / sha256 之一"}
        try:
            result = graph.run(
                doc_ids=doc_ids,
                urls=urls,
                sha256=sha256,
                confirm=state.get("confirm", False),
                force_recent=state.get("force_recent", False),
                reason=state.get("reason", ""),
            )
            return {"lifecycle_result": result}
        except Exception as exc:
            return {"error": f"lifecycle_agent 失败: {exc}"}

    return lifecycle_agent_node
