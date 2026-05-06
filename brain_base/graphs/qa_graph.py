"""
QA 主图：用户问答全流程。

流程（含自动外检 + 入库回路）：
    probe → crystallized_check → normalize → decompose → rewrite → search → judge
                                                                              ├── 充足 → answer
                                                                              └── 不足 → get_info_trigger
                                                                                          ├── 不需 → answer
                                                                                          └── 需要 → web_research
                                                                                                    → select_candidates
                                                                                                    → ingest_candidates
                                                                                                    → re_search
                                                                                                    → judge（第二轮 attempted=True 强制 answer）
    answer → self_check → crystallize_answer → END

条件边：
- after_crystallized_check：固化命中 hit_fresh/cold_promoted → answer，否则 normalize
- after_judge：sufficient 或 attempted → answer，否则 get_info_trigger
- after_get_info_trigger：trigger_get_info=True → web_research，否则 answer
"""

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from brain_base.config import GetInfoConfig
from brain_base.graph.conditional_logic import ConditionalLogic
from brain_base.nodes.qa import (
    create_answer_node,
    create_decompose_node,
    create_get_info_trigger_node,
    create_ingest_candidates_node,
    create_judge_node,
    create_normalize_node,
    create_rewrite_node,
    create_select_candidates_node,
    create_self_check_node,
    create_web_research_node,
    crystallize_answer_node,
    crystallized_check_node,
    probe_node,
    re_search_node,
    search_node,
)


class QaState(TypedDict, total=False):
    """QA 主图状态"""
    question: str
    infra_status: dict
    crystallized_status: str
    crystallized_answer: str
    skill_id: str
    cold_evidence_summary: str
    normalized_query: str
    entities: list[str]
    time_sensitive: bool
    sub_queries: list[str]
    decomposition_needed: bool
    rewritten_queries: list[str]
    evidence: list[dict]
    evidence_sufficient: bool
    coverage_score: float
    missing_aspects: list[str]
    answer: str
    self_check_passed: bool
    self_check_skipped: bool
    self_check_result: dict
    crystallize_result: dict
    error: str
    # T10：自动外检 + 入库回路
    trigger_get_info: bool
    search_hint: str
    get_info_reason: str
    get_info_attempted: bool
    get_info_candidates: list[dict]
    ingest_targets: list[dict]
    get_info_ingested: list[str]
    ingest_errors: list[str]


class QaGraph:
    """QA 主图

    Args:
        llm: 可选的 LangChain BaseChatModel 实例；None 时所有 LLM 节点走降级路径。
        get_info_config: 自动外检 + 入库回路参数；不传走 GetInfoConfig 默认值。
    """

    def __init__(self, llm: Any = None, get_info_config: GetInfoConfig | None = None):
        self.llm = llm
        self.config = get_info_config or GetInfoConfig()
        self.routing = ConditionalLogic()
        workflow = StateGraph(QaState)

        # 原 10 个节点
        workflow.add_node("probe", probe_node)
        workflow.add_node("crystallized_check", crystallized_check_node)
        workflow.add_node("normalize", create_normalize_node(llm))
        workflow.add_node("decompose", create_decompose_node(llm))
        workflow.add_node("rewrite", create_rewrite_node(llm))
        workflow.add_node("search", search_node)
        workflow.add_node("judge", create_judge_node(llm))
        workflow.add_node("answer", create_answer_node(llm))
        workflow.add_node("self_check", create_self_check_node(llm))
        workflow.add_node("crystallize_answer", crystallize_answer_node)
        # T10 新增 5 个节点
        workflow.add_node("get_info_trigger", create_get_info_trigger_node(llm, self.config))
        workflow.add_node("web_research", create_web_research_node(llm, self.config))
        workflow.add_node("select_candidates", create_select_candidates_node(self.config))
        workflow.add_node("ingest_candidates", create_ingest_candidates_node(llm, self.config))
        workflow.add_node("re_search", re_search_node)

        # 边 / 路由
        workflow.set_entry_point("probe")
        workflow.add_edge("probe", "crystallized_check")
        workflow.add_conditional_edges(
            "crystallized_check",
            self.routing.after_crystallized_check,
            {"answer": "answer", "normalize": "normalize"},
        )
        workflow.add_edge("normalize", "decompose")
        workflow.add_edge("decompose", "rewrite")
        workflow.add_edge("rewrite", "search")
        workflow.add_edge("search", "judge")
        # judge 后：充足 / 已 attempted → answer；否则 → get_info_trigger
        workflow.add_conditional_edges(
            "judge",
            self.routing.after_judge,
            {"answer": "answer", "get_info_trigger": "get_info_trigger"},
        )
        # get_info_trigger 后：needed=True → web_research；否则 → answer
        workflow.add_conditional_edges(
            "get_info_trigger",
            self.routing.after_get_info_trigger,
            {"web_research": "web_research", "answer": "answer"},
        )
        # 外检 → 选择 → 入库 → 重检索 → 回 judge（防死循环靠 get_info_attempted）
        workflow.add_edge("web_research", "select_candidates")
        workflow.add_edge("select_candidates", "ingest_candidates")
        workflow.add_edge("ingest_candidates", "re_search")
        workflow.add_edge("re_search", "judge")
        # answer 后续
        workflow.add_edge("answer", "self_check")
        workflow.add_edge("self_check", "crystallize_answer")
        workflow.add_edge("crystallize_answer", END)

        self.graph = workflow.compile()

    def run(self, question: str) -> dict[str, Any]:
        """执行 QA 全流程"""
        initial: QaState = {"question": question}
        # 外检回路涉及 judge 第二次访问，需要更高 recursion_limit
        result = self.graph.invoke(initial, config={"recursion_limit": 50})
        return dict(result)
