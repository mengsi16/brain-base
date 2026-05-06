"""
KnowledgePersistence 子图。

流程：chunk → enrich → ingest
参考 TradingAgents 的 GraphSetup 类模式。
"""

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from brain_base.nodes.persistence import chunk_node, create_enrich_node, ingest_node


class PersistenceState(TypedDict, total=False):
    """持久化管道状态"""
    raw_md_path: str
    doc_id: str
    chunk_dir: str
    chunk_files: list[str]
    enriched: bool
    enriched_count: int
    skipped_count: int
    chunks_to_enrich: list[str]
    milvus_inserted: int
    error: str


class PersistenceGraph:
    """KnowledgePersistence 子图，参考 TradingAgents GraphSetup 模式

    Args:
        llm: 可选 LLM 实例；None 时 enrich 节点只标记待处理，不调 LLM
    """

    def __init__(self, llm: Any = None):
        self.llm = llm
        workflow = StateGraph(PersistenceState)
        workflow.add_node("chunk", chunk_node)
        workflow.add_node("enrich", create_enrich_node(llm))
        workflow.add_node("ingest", ingest_node)

        workflow.set_entry_point("chunk")
        workflow.add_edge("chunk", "enrich")
        workflow.add_edge("enrich", "ingest")
        workflow.add_edge("ingest", END)

        self.graph = workflow.compile()

    def run(self, raw_md_path: str, doc_id: str, chunk_dir: str = "data/docs/chunks") -> dict[str, Any]:
        """执行持久化管道"""
        initial_state: PersistenceState = {
            "raw_md_path": raw_md_path,
            "doc_id": doc_id,
            "chunk_dir": chunk_dir,
        }
        result = self.graph.invoke(initial_state)
        return dict(result)
