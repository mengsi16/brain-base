"""
QA 主图：用户问答全流程。

流程（T28 PIPE2 重构后：ingest 后接第二段子图 fanout_search × N + barrier2）：

    probe → crystallized_check → normalize → decompose → fanout_prep × N → barrier1
         → merge_search_keywords → search_web_dual → fanout_extract_dispatcher (条件边)
             → fetch_extract_one × N (Send) → barrier_extract
             或短路 → barrier_extract
         → fanout_persist_dispatcher (条件边)
             → write_raw_one × N (Send) → barrier_raw
             或短路 → ingest
         → fanout_enrich_dispatcher (条件边)
             → enrich_one × M (Send) → barrier_enrich → ingest
             或短路 → ingest
         → ingest → fanout_search_dispatcher (条件边)
             → subquery_search_one × N (Send) → barrier2
             或短路 → barrier2
         → barrier2 → judge → answer → self_check → crystallize_answer → END

T25 变化：
- 删 ``get_info_trigger`` / ``web_research`` / ``select_candidates`` / ``ingest_candidates`` /
  ``re_search`` 5 个老节点（judge 后外检回路），职责转移到 search 前的
  ``fanout_extract_dispatcher`` 5 重 gate + ``fetch_extract_one`` Send fan-out

T26.1 变化：
- ``barrier_extract`` 后插入持久化流水 (write_raw_one / barrier_raw / enrich_one / barrier_enrich / ingest)
- 2 条件边 fanout_persist_dispatcher / fanout_enrich_dispatcher 短路目标原为 legacy_dense_search

T28 变化：
- **删** ``legacy_dense_search`` 节点（T23 临时桥接，扫平搜索被强子问题霆榜问题）
- **改** fanout_persist_dispatcher / fanout_enrich_dispatcher 短路目标：``legacy_dense_search`` → ``ingest``
  （ingest_node 在 enriched_chunks=[] 时空跑返 ingested_count=0，不拋错）
- **加** PIPE2 第二段子图：fanout_search_dispatcher (条件边) → subquery_search_one × N (Send) → barrier2 → judge
- ingest 后 fanout_search_dispatcher 根据 sub_queries 是否为空决定 fan-out N 个 Send 或短路 barrier2
- subquery_search_one 调 ``multi_query_search(use_rerank=True)`` 每子问题独立 top-K + bge-reranker 重排
- barrier2 flatten + 加 sub_idx / sub_question 标签 → evidence，让 answer 节点可按子问题分组

条件边：
- ``after_crystallized_check``：固化命中 hit_fresh/cold_promoted → answer，否则 normalize
- ``fanout_prep_dispatcher``：N 个子问题 → N 个 Send 到 subquery_prep
- ``after_barrier1``（T30.1）：任一 sub_needs_get_info=True → merge_search_keywords (GI 流水)，
  全 False → ingest (跳过 GI，空跑 → PIPE2)
- ``fanout_extract_dispatcher``：5 重 gate 短路 或 N 个 Send 到 fetch_extract_one
- ``fanout_persist_dispatcher``（1 重 gate）：candidates 空短路 → ingest
- ``fanout_enrich_dispatcher``（1 重 gate）：chunk_files 空短路 → ingest
- ``fanout_search_dispatcher``（1 重 gate）：sub_queries 空短路 → barrier2 或 N 个 Send 到 subquery_search_one
"""

from operator import add
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from brain_base.config import GetInfoConfig
from brain_base.graph.conditional_logic import ConditionalLogic
from brain_base.nodes.qa import (
    create_answer_node,
    create_crystallize_answer_node,
    create_decompose_node,
    create_judge_node,
    create_normalize_node,
    create_self_check_node,
    crystallized_check_node,
    probe_node,
)
from brain_base.nodes.qa_get_info import (
    barrier_extract_node,
    create_fetch_extract_one,
    create_search_strategy_node,
    fanout_extract_dispatcher,
    merge_search_keywords_node,
    search_web_dual_node,
)
from brain_base.nodes.qa_persist import (
    barrier_enrich_node,
    barrier_raw_node,
    create_enrich_one,
    fanout_enrich_dispatcher,
    fanout_persist_dispatcher,
    ingest_node,
    write_raw_one,
)
from brain_base.nodes.qa_prep import (
    barrier1_node,
    create_prep_one_subquery,
    fanout_prep_dispatcher,
)
from brain_base.nodes.qa_search import (
    barrier2_node,
    create_subquery_search_one,
    fanout_search_dispatcher,
)


class QaState(TypedDict, total=False):
    """QA 主图状态"""
    question: str
    # T36 新增：多轮对话上下文（外部会话管理，不用 LangGraph checkpointer）
    conversation_history: list[dict]   # [{"role": "user"|"ai", "text": str, "ts": str}, ...]
    # T37 新增：指代消解后的独立问题（观测/调试用，实际传递通过 normalized_query）
    contextualized_query: str | None
    # T38 新增：外检触发原因列表（值域：sparse_miss / time_sensitive / none）
    gi_trigger_reasons: list[str]
    # T39 新增：结构化外检决策（per sub-question）
    gi_decisions: list[dict]
    # T40 新增：场景化搜索策略（观测/调试用）
    search_strategies: list[dict]
    infra_status: dict
    crystallized_status: str
    crystallized_answer: str
    skill_id: str
    cold_evidence_summary: str
    normalized_query: str
    entities: list[str]
    time_sensitive: bool
    # T31 新增：normalize 节点输出
    time_range: list[str] | None           # [start_iso, end_iso] 或 None；time_sensitive=True 且含模糊时间词时输出
    abbreviation_hints: list[str] | None   # 缩写有 >=2 解读时的候选清单，否则 None
    sub_questions: list[str]               # T23 decompose 输出（重命名自 sub_queries）
    decomposition_needed: bool
    # T23 fanout_prep 子节点 reducer：N 个 Send 返回各自单元素 list，
    # operator.add 自动拼接。初始化必须给空 list（否则首次 add 报错）。
    sub_prep_results: Annotated[list[dict], add]
    # T23 barrier1 输出的扁平字段（按 sub_idx 索引对齐）
    sub_queries: list[list[dict]]          # 每子问题一组 [{text, layer}, ...]
    # T30：原 sub_grep_keywords (list[list[str]]) / sub_grep_hits (list[int])
    # 重命名为 sub_lexical_queries (list[str]) / sub_lexical_scores (list[float])。
    # sparse gate (milvus text_search top-3 平均分) 取代 grep AND 字面命中计数。
    sub_lexical_queries: list[str]         # 每子问题 1 个短自然语言串（≤30 字）
    sub_lexical_scores: list[float]        # 每子问题 sparse top-3 平均分（IP 内积）
    sub_needs_get_info: list[bool]         # score < LEXICAL_GATE_THRESHOLD (0.20)
    rewritten_queries: list[str]
    evidence: list[dict]
    evidence_sufficient: bool
    evidence_recommendation: str
    coverage_score: float
    judge_reason: str
    missing_aspects: list[str]
    answer: str
    self_check_passed: bool
    self_check_skipped: bool
    self_check_result: dict
    crystallize_result: dict
    error: str
    # T10：自动外检 + 入库回路（T25 后部分字段变为老节点遗留、不再写入）
    trigger_get_info: bool
    search_hint: str
    get_info_reason: str
    get_info_attempted: bool
    get_info_candidates: list[dict]
    ingest_targets: list[dict]
    get_info_ingested: list[str]
    ingest_errors: list[str]
    # T12：多跳问题分解 fan-out
    sub_question_evidence: list[dict]
    # T25：fetch_extract 多 URL 爬取处理
    get_info_config: Any
    """GetInfoConfig 实例；search_web_dual / fetch_extract_one 节点从 state 读取。"""
    search_keywords: list[str]
    """merge_search_keywords 输出：每子问题 keywords 空格 join 后的 query 列表。"""
    serp_urls: list[dict]
    """search_web_dual 输出：去重后的 SERP URL 列表（含 from_engines / from_queries 越源标签）。"""
    extract_results: Annotated[list[dict], add]
    """fetch_extract_one × N reducer：N 个 Send 各返回单元素 list，operator.add 自动拼接。初始化必须为 []。"""
    extract_errors: list[str]
    """barrier_extract 输出：fetch 失败 / readability 失败 / LLM 失败的错误聚合。"""
    # T26.1：chunk → enrich → ingest 持久化流水
    persist_results: Annotated[list[dict], add]
    """write_raw_one × N reducer：各 doc 落盘结果，含 doc_id / chunk_files / success / error?。初始化必须为 []。"""
    chunk_files: list[str]
    """barrier_raw 输出：所有 success doc 的 chunk 文件路径 flatten 列表，供 fanout_enrich_dispatcher 读。"""
    enrich_results: Annotated[list[dict], add]
    """enrich_one × M reducer：各 chunk 富化结果，含 chunk_file / success / error?。初始化必须为 []。"""
    enriched_chunks: list[str]
    """barrier_enrich 输出：成功富化的 chunk 文件路径（过滤失败），供 ingest 调用。"""
    ingested_count: int
    """ingest_node 输出：实际入 Milvus 的行数（包含 chunk 行 + question 行）。"""
    persist_errors: list[str]
    """barrier_raw / barrier_enrich 累加错误聚合供调试 / 监控。"""
    # T28 PIPE2 第二段子图：subquery_search_one × N reducer → barrier2 flatten
    sub_evidence: Annotated[list[dict], add]
    """subquery_search_one × N reducer：各子问题 milvus + rerank 结果，各 Send 返单元素 list，operator.add 拼接。初始化必须为 []。"""
    search_errors: list[str]
    """barrier2 聚合 sub_evidence 失败时的错误归集（fan-out 单 Send 隔离描述）。"""


class QaGraph:
    """QA 主图

    Args:
        llm: LangChain BaseChatModel 实例。**T27 fail-fast：不接受 None**——
            另外 invoke_structured / 5 个 LLM 节点工厂也不再接受降级路径。
            cli 上层应 fail-fast 在 ``cmd_ask`` 退出；测试所有节点拓扑用
            ``tests/conftest.py::mock_llm`` sentinel。
        get_info_config: 自动外检 + 入库回路参数；不传走 GetInfoConfig 默认值。

    Raises:
        ValueError: 当 ``llm is None`` 时拋（T27 fail-fast 入口硬化）。
    """

    def __init__(self, llm: Any, get_info_config: GetInfoConfig | None = None):
        if llm is None:
            raise ValueError(
                "QaGraph requires a non-None llm. "
                "使用 brain_base.cli._build_llm_from_env() 从 .env 构造，"
                "或在测试里传 tests/conftest.py::mock_llm sentinel。"
            )
        self.llm = llm
        self.config = get_info_config or GetInfoConfig()
        self.routing = ConditionalLogic()
        workflow = StateGraph(QaState)

        # 主图节点
        workflow.add_node("probe", probe_node)
        workflow.add_node("crystallized_check", crystallized_check_node)
        workflow.add_node("normalize", create_normalize_node(llm))
        workflow.add_node("decompose", create_decompose_node(llm))
        # T23 第一段 fanout_prep（rewrite + grep, async）
        workflow.add_node("subquery_prep", create_prep_one_subquery(llm))
        workflow.add_node("barrier1", barrier1_node)
        # T25 第二段 fetch_extract（多 URL 爬取处理，位于 search 前）
        workflow.add_node("merge_search_keywords", merge_search_keywords_node)
        workflow.add_node("search_web_dual", search_web_dual_node)
        workflow.add_node("fetch_extract_one", create_fetch_extract_one(llm, self.config))
        workflow.add_node("barrier_extract", barrier_extract_node)
        # T26.1 持久化流水（barrier_extract 后）
        workflow.add_node("write_raw_one", write_raw_one)
        workflow.add_node("barrier_raw", barrier_raw_node)
        workflow.add_node("enrich_one", create_enrich_one(llm, self.config))
        workflow.add_node("barrier_enrich", barrier_enrich_node)
        workflow.add_node("ingest", ingest_node)
        # T28 PIPE2：第二段子图（每子问题独立 milvus + rerank → barrier2 聚合 evidence）
        workflow.add_node("subquery_search_one", create_subquery_search_one(self.config))
        workflow.add_node("barrier2", barrier2_node)
        workflow.add_node("judge", create_judge_node(llm))
        workflow.add_node("answer", create_answer_node(llm))
        workflow.add_node("self_check", create_self_check_node(llm))
        workflow.add_node("crystallize_answer", create_crystallize_answer_node(llm))

        # 边 / 路由
        workflow.set_entry_point("probe")
        workflow.add_edge("probe", "crystallized_check")
        workflow.add_conditional_edges(
            "crystallized_check",
            self.routing.after_crystallized_check,
            {"answer": "answer", "normalize": "normalize"},
        )
        workflow.add_edge("normalize", "decompose")
        # T23：decompose 后条件边 fanout_prep_dispatcher 发 N 个 Send 到 subquery_prep；
        # sub_questions 为空时返回 "barrier1" 短路避免无边卡住（参考 T16 审计陷阱 D）。
        workflow.add_conditional_edges(
            "decompose",
            fanout_prep_dispatcher,
            {"barrier1": "barrier1"},
        )
        # subquery_prep 节点返回 → reducer add 合并 sub_prep_results → 入 barrier1
        workflow.add_edge("subquery_prep", "barrier1")
        # T25：barrier1 拆主图扁平字段
        # T30.1：barrier1 后加条件边 after_barrier1：
        #   - 任一 sub_needs_get_info=True → merge_search_keywords (走 GI 流水)
        #   - 全 False                    → ingest (跳过 GI，空跑 → PIPE2)
        # 修复前 add_edge("barrier1","merge_search_keywords") 是无条件边，
        # 全 PASS 也会浪费 SERP 抓取时间（fanout_extract_dispatcher 5 重
        # gate 第 2 重才会短路，但此时 search_web_dual 已抓完）。
        workflow.add_conditional_edges(
            "barrier1",
            self.routing.after_barrier1,
            {
                "merge_search_keywords": "merge_search_keywords",
                "ingest": "ingest",
            },
        )
        # T40：search_strategy 可选节点（enable_search_strategy 控制）
        if self.config.enable_search_strategy:
            workflow.add_node("search_strategy", create_search_strategy_node(llm))
            workflow.add_edge("merge_search_keywords", "search_strategy")
            workflow.add_edge("search_strategy", "search_web_dual")
        else:
            workflow.add_edge("merge_search_keywords", "search_web_dual")
        workflow.add_conditional_edges(
            "search_web_dual",
            fanout_extract_dispatcher,
            {"barrier_extract": "barrier_extract"},
        )
        # fetch_extract_one 返回 → reducer add 合并 extract_results → 入 barrier_extract
        workflow.add_edge("fetch_extract_one", "barrier_extract")
        # T26.1 持久化流水：barrier_extract → fanout_persist_dispatcher（1 重 gate）
        # → write_raw_one × N 或短路 → barrier_raw → fanout_enrich_dispatcher → enrich_one × M 或短路 → barrier_enrich → ingest
        # T28：短路目标从 legacy_dense_search 改为 ingest（ingest 在 enriched_chunks=[] 时空跑）
        workflow.add_conditional_edges(
            "barrier_extract",
            fanout_persist_dispatcher,
            {"ingest": "ingest"},
        )
        # write_raw_one × N 返回 → reducer add 合并 persist_results → 入 barrier_raw
        workflow.add_edge("write_raw_one", "barrier_raw")
        workflow.add_conditional_edges(
            "barrier_raw",
            fanout_enrich_dispatcher,
            {"ingest": "ingest"},
        )
        # enrich_one × M 返回 → reducer add 合并 enrich_results → 入 barrier_enrich
        workflow.add_edge("enrich_one", "barrier_enrich")
        # barrier_enrich → ingest（fail-fast）
        workflow.add_edge("barrier_enrich", "ingest")
        # T28 PIPE2：ingest → fanout_search_dispatcher (条件边，sub_queries 空短路 barrier2)
        # → subquery_search_one × N 或短路 → barrier2 → judge
        workflow.add_conditional_edges(
            "ingest",
            fanout_search_dispatcher,
            {"barrier2": "barrier2"},
        )
        # subquery_search_one × N 返回 → reducer add 合并 sub_evidence → 入 barrier2
        workflow.add_edge("subquery_search_one", "barrier2")
        workflow.add_edge("barrier2", "judge")
        # T25 简化 judge 后路由：外检已在 search 前完成，judge 不再需要外检回路。
        workflow.add_edge("judge", "answer")
        workflow.add_edge("answer", "self_check")
        workflow.add_edge("self_check", "crystallize_answer")
        workflow.add_edge("crystallize_answer", END)

        self.graph = workflow.compile()

    def run(self, question: str, conversation_history: list[dict] | None = None) -> dict[str, Any]:
        """执行 QA 全流程。

        T23 后包含 async 节点 ``subquery_prep``（fanout_prep + LLM rewrite），
        走 ``ainvoke()`` + ``asyncio.run()`` 打包 sync 接口（参考 GetInfoGraph 同样路径）。

        T29 追加：主协程包一层 finally 主动 ``await web_fetcher.shutdown()``，
        让 playwright subprocess transport 在 event loop 内优雅关闭——解决 Windows
        ProactorEventLoop 下 asyncio.run 退出后 GC 析构 transport 触发
        ``Exception ignored: I/O operation on closed pipe`` 满屏噪音的问题。
        """
        import asyncio
        from brain_base.tools.web_fetcher import shutdown as _pw_shutdown

        initial: QaState = {
            "question": question,
            # T36 多轮对话历史（None/[] 退化为单轮）
            "conversation_history": conversation_history or [],
            # T23 reducer 字段初始化为空 list，首次 add 才不报错（审计陷阱 A）
            "sub_prep_results": [],
            # T25 reducer 字段同陷阱：fetch_extract_one × N 首次 add 需初始化
            "extract_results": [],
            # T26.1 reducer 字段：write_raw_one × N / enrich_one × M 首次 add 需初始化
            "persist_results": [],
            "enrich_results": [],
            # T26.1 错误聚合 + Milvus 计数初始值（barrier / ingest 会覆写）
            "persist_errors": [],
            "ingested_count": 0,
            # T28 PIPE2 reducer 字段：subquery_search_one × N 首次 add 需初始化
            "sub_evidence": [],
            "search_errors": [],
            # T25 dispatcher 判 get_info_attempted 防死循环；barrier_extract 会写 True
            "get_info_attempted": False,
            # T25 search_web_dual / fetch_extract_one 从 state 读 config
            "get_info_config": self.config,
        }

        async def _invoke_with_cleanup() -> dict[str, Any]:
            # 无论 ainvoke 成功抛错，finally 都主动关 playwright subprocess
            try:
                return await self.graph.ainvoke(
                    initial, config={"recursion_limit": 50}
                )
            finally:
                await _pw_shutdown()

        result = asyncio.run(_invoke_with_cleanup())
        return dict(result)
