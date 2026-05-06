"""
IngestUrl Agent 工厂函数。
"""

from typing import Any, Callable

from brain_base.graphs.ingest_url_graph import IngestUrlGraph


def create_ingest_url_agent(llm: Any = None) -> Callable:
    """创建 ingest-url agent 节点函数

    Args:
        llm: 可选 LLM 实例，透传给 IngestUrlGraph 的 PersistenceGraph
    """

    def ingest_url_agent_node(state: dict[str, Any]) -> dict[str, Any]:
        graph = IngestUrlGraph(llm=llm)
        url = state.get("url", "")
        if not url:
            return {"error": "ingest_url_agent: url 为空"}
        try:
            result = graph.run(
                url=url,
                source_type=state.get("source_type", "community"),
                topic=state.get("topic", "untitled"),
                title_hint=state.get("title_hint", ""),
                raw_content=state.get("raw_content", ""),
            )
            return {"ingest_url_result": result}
        except Exception as exc:
            return {"error": f"ingest_url_agent 失败: {exc}"}

    return ingest_url_agent_node
