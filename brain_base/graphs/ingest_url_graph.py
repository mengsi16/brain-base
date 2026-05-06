"""
IngestUrl 图：URL 抓取 → 清洗 → frontmatter → 持久化。

参考 TradingAgents 的 GraphSetup 类模式。
"""

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from brain_base.graph.conditional_logic import ConditionalLogic
from brain_base.nodes.ingest_url import (
    clean_node,
    create_completeness_check_node,
    create_persist_node,
    fetch_node,
    frontmatter_node,
)


class IngestUrlState(TypedDict, total=False):
    """URL 入库状态。

    字段命名以 ``brain_base/nodes/ingest_url.py`` 节点实际读写的为准；未声明
    字段（如 ``raw_html``）会被 langgraph 默认 reducer 丢弃，导致 fetch_node
    抓到的 HTML 传不到 clean_node，clean_node 只能报"raw_html 为空"。
    """
    url: str
    source_type: str
    topic: str
    title_hint: str
    raw_html: str
    raw_content: str
    cleaned_md: str
    raw_md_path: str
    doc_id: str
    persistence_result: dict
    extraction_status: str
    completeness_status: str
    completeness_chars: int
    completeness_reason: str
    error: str


class IngestUrlGraph:
    """URL 抓取入库图

    Args:
        llm: 可选 LLM 实例，透传给 PersistenceGraph 的 enrich 节点
    """

    def __init__(self, llm: Any = None):
        self.llm = llm
        self.routing = ConditionalLogic()
        workflow = StateGraph(IngestUrlState)
        workflow.add_node("fetch", fetch_node)
        workflow.add_node("clean", clean_node)
        workflow.add_node("completeness", create_completeness_check_node(llm))
        workflow.add_node("frontmatter", frontmatter_node)
        workflow.add_node("persist", create_persist_node(llm))

        workflow.set_entry_point("fetch")
        workflow.add_edge("fetch", "clean")
        workflow.add_edge("clean", "completeness")
        # completeness != ok 不写任何文件，直接 END
        workflow.add_conditional_edges(
            "completeness",
            self.routing.after_completeness_check,
            {"frontmatter": "frontmatter", "end": END},
        )
        workflow.add_edge("frontmatter", "persist")
        workflow.add_edge("persist", END)

        self.graph = workflow.compile()

    def run(
        self,
        url: str,
        source_type: str = "community",
        topic: str = "untitled",
        title_hint: str = "",
        raw_content: str = "",
    ) -> dict[str, Any]:
        initial_state: IngestUrlState = {
            "url": url,
            "source_type": source_type,
            "topic": topic,
            "title_hint": title_hint,
            "raw_content": raw_content,
        }
        result = self.graph.invoke(initial_state)
        return dict(result)
