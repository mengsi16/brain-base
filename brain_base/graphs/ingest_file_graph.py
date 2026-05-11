"""
IngestFile 图：本地文件上传 → 转换 → frontmatter → 持久化。

参考 TradingAgents 的 GraphSetup 类模式。
"""

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from brain_base.nodes.ingest_file import (
    convert_node,
    create_doc_enrich_node,
    create_persist_node,
    frontmatter_node,
)


class IngestFileState(TypedDict, total=False):
    """文件入库状态。

    T32 扩展：增 doc 级 LLM 富化字段 doc_enriched / doc_enriched_count / doc_enrich_errors。
    """
    input_files: list[str]
    upload_date: str
    converted: list[dict]
    conversion_errors: list[dict]
    raw_paths: list[str]
    # T33 新增：frontmatter_node dedup short-circuit 命中跳过清单
    dedup_skipped: list[dict]
    # T32 新增：doc 级 LLM 富化输出
    doc_enriched: bool
    doc_enriched_count: int
    doc_enrich_errors: list[dict]
    persistence_results: list[dict]
    error: str


class IngestFileGraph:
    """本地文件上传入库图。

    T32 拓扑：convert → frontmatter → doc_enrich → persist

    Args:
        llm: LLM 实例。**必须传入不允许 None**——upload 路径属核心 Agent 节点，
             LLM 缺失 fail-fast（CLAUDE.md 规则 14）。调用方（如 cli）负责加载
             LLM 与错误提示；本类不提供 llm=None 默认值。
    """

    def __init__(self, llm: Any):
        if llm is None:
            raise RuntimeError(
                "IngestFileGraph: llm 必须提供。upload 路径属核心 Agent 节点，"
                "LLM 缺失不能走降级（CLAUDE.md 规则 14）。请在 cli/agent 加载 LLM 后传入。"
            )
        self.llm = llm
        workflow = StateGraph(IngestFileState)
        workflow.add_node("convert", convert_node)
        workflow.add_node("frontmatter", frontmatter_node)
        workflow.add_node("doc_enrich", create_doc_enrich_node(llm))
        workflow.add_node("persist", create_persist_node(llm))

        workflow.set_entry_point("convert")
        workflow.add_edge("convert", "frontmatter")
        workflow.add_edge("frontmatter", "doc_enrich")
        workflow.add_edge("doc_enrich", "persist")
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
