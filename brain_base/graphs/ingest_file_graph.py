"""
IngestFile 图：本地文件上传 → 转换 → frontmatter → 持久化。

参考 TradingAgents 的 GraphSetup 类模式。
"""

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from brain_base.nodes.ingest_file import convert_node, create_persist_node, frontmatter_node


class IngestFileState(TypedDict, total=False):
    """文件入库状态"""
    input_files: list[str]
    upload_date: str
    converted: list[dict]
    conversion_errors: list[dict]
    raw_paths: list[str]
    persistence_results: list[dict]
    error: str


class IngestFileGraph:
    """本地文件上传入库图

    Args:
        llm: 可选 LLM 实例，透传给 PersistenceGraph 的 enrich 节点
    """

    def __init__(self, llm: Any = None):
        self.llm = llm
        workflow = StateGraph(IngestFileState)
        workflow.add_node("convert", convert_node)
        workflow.add_node("frontmatter", frontmatter_node)
        workflow.add_node("persist", create_persist_node(llm))

        workflow.set_entry_point("convert")
        workflow.add_edge("convert", "frontmatter")
        workflow.add_edge("frontmatter", "persist")
        workflow.add_edge("persist", END)

        self.graph = workflow.compile()

    def run(self, input_files: list[str], upload_date: str | None = None) -> dict[str, Any]:
        from datetime import date
        initial_state: IngestFileState = {
            "input_files": input_files,
            "upload_date": upload_date or date.today().isoformat(),
        }
        result = self.graph.invoke(initial_state)
        return dict(result)
