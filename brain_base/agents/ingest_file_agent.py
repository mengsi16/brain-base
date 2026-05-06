"""
IngestFile Agent 工厂函数。
"""

from typing import Any, Callable

from brain_base.graphs.ingest_file_graph import IngestFileGraph


def create_ingest_file_agent(llm: Any = None) -> Callable:
    """创建 ingest-file agent 节点函数

    Args:
        llm: 可选 LLM 实例，透传给 IngestFileGraph 的 PersistenceGraph
    """

    def ingest_file_agent_node(state: dict[str, Any]) -> dict[str, Any]:
        graph = IngestFileGraph(llm=llm)
        input_files = state.get("input_files", [])
        upload_date = state.get("upload_date")
        if not input_files:
            return {"error": "ingest_file_agent: input_files 为空"}
        try:
            result = graph.run(input_files=input_files, upload_date=upload_date)
            return {"ingest_file_result": result}
        except Exception as exc:
            return {"error": f"ingest_file_agent 失败: {exc}"}

    return ingest_file_agent_node
