"""
QA 主图：用户问答全流程。

流程（T47.4 统一意图识别 Agent-Loop + T28 PIPE2）：

    probe → crystallized_check
         ├ hit_fresh/cold_promoted ──► answer ──► self_check ──► crystallize_answer ──► END
         └ 其余 ──► extract_urls
                     ├ user_urls 非空 ──► url_pre_fetch ──► normalize
                     └ user_urls 空 ────────────────────► normalize
                                                            ↓
                                                          decompose
                                                            ↓
                                                  intent_planner ◄─────┐
                                                            ↓          │ should_continue_intent
                                                  intent_executor      │ (5 级早退)
                                                            ↓          │
                                                  intent_observer ─────┘
                                                            ↓
                                                  merge_evidence
                                                            ↓
                          fanout_persist_dispatcher (条件边)
                                  → write_raw_one × N (Send) → barrier_raw
                                  或短路 → ingest
                                            ↓
                          fanout_enrich_dispatcher (条件边)
                                  → enrich_one × M (Send) → barrier_enrich → ingest
                                  或短路 → ingest
                                            ↓
                                          ingest
                                            ↓
                          fanout_search_dispatcher (条件边)
                                  → subquery_search_one × N (Send) → barrier2
                                  或短路 → barrier2
                                            ↓
                                          judge → answer → self_check → crystallize_answer → END

T47 架构（T47.0-T47.6 完成后的稳态）：
- 入口分流：after_crystallized_check → extract_urls（固化命中 → answer）
- URL 路径：extract_urls → url_pre_fetch → normalize（user_urls 非空）或直达 normalize
- 主图意图循环：intent_planner → intent_executor → intent_observer，
  should_continue_intent 路由 5 级早退（连错 ≥2 / 充分 / 上限 / no_action / 继续）
- 汇聚转换：merge_evidence 输出与原 T46 三路汇聚后一致的 get_info_candidates 13 字段，
  下游持久化流水 + PIPE2 + judge/answer/self_check/crystallize_answer 零改动

T25/T26.1/T28/T30/T34/T38 历史变化（不变量，保留）：
- ingest 后 fanout_search_dispatcher 根据 sub_queries 是否为空决定 fan-out N 个 Send 或短路 barrier2
- subquery_search_one 调 ``multi_query_search(use_rerank=True)`` 每子问题独立 top-K + bge-reranker 重排
- barrier2 flatten + 加 sub_idx / sub_question 标签 → evidence
- crystallized_check 显式化 6 状态（hit_fresh / cold_promoted / hit_stale / cold_observed / miss / degraded）

条件边（T47.4 后）：
- ``after_crystallized_check``：固化命中 hit_fresh/cold_promoted → answer，否则 extract_urls
- ``route_after_extract_urls``：user_urls 非空 → url_pre_fetch，空 → normalize
- ``should_continue_intent``（5 级）：连错 ≥2 / 充分 / 上限 / no_action → merge_evidence；
  其余 → intent_planner
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
# T47.4 新增：extract_urls + url_pre_fetch（D6 + D7 A 方案）
from brain_base.nodes.qa_extract_urls import create_extract_urls
from brain_base.nodes.qa_url_pre_fetch import create_url_pre_fetch
# T47.4 新增：统一意图识别 Agent-Loop 4 节点 + merge_evidence
from brain_base.nodes.qa_intent import (
    create_intent_executor,
    create_intent_observer,
    create_intent_planner,
    merge_evidence_node,
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
    # T28 PIPE2 上游输入：fanout_search_dispatcher / subquery_search_one 读取。
    # T47.4 后由 intent_executor 间接生产（intent_observer 写 evidence_pool、
    # merge_evidence 不增写该字段）；默认空 list 时 fanout_search_dispatcher 短路 barrier2。
    sub_queries: list[list[dict]]          # 每子问题一组 [{text, layer}, ...]
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
    # T25：fetch_extract 多 URL 爬取处理
    get_info_config: Any
    """GetInfoConfig 实例；url_pre_fetch / intent_executor 等节点从 state 读取。"""
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
    user_urls: list[str]
    """extract_urls 提取的用户问题中显式 URL（正则，不调 LLM）。url_pre_fetch 依赖该字段判是否需要浅抓。"""
    # T47：统一意图识别 Agent-Loop 状态字段
    # 契约引用：md/research/2026-05-17-t47-unified-intent-agent-contract.md §8
    url_pre_fetch_content: list[dict]
    """url_pre_fetch 输出：[{url, title, markdown_excerpt}, ...]，
    normalize 改写时作为上下文输入，**不写持久化**（AGENTS.md 规则 41）。
    user_urls 为空时此字段为 []，normalize 退化为只看 question 改写。"""
    evidence_pool: list[dict]
    """intent_observer 累积的所有 Evidence（dict 序列化），merge_evidence 转 get_info_candidates。
    不使用 reducer add：意图识别循环是顺序循环，不是 fan-out 并发，每次 intent_observer 直接覆盖写完整结构。"""
    visited_urls: list[str]
    """intent_observer 累积的已抓 URL 列表，intent_planner 用作去重参考避免重复抓同 URL。"""
    iteration_count: int
    """intent_observer 每跳 +1，should_continue_intent 与 max_iterations 比较；
    超上限触发强制早退到 merge_evidence（D5 拍板）。"""
    max_iterations: int
    """GetInfoConfig.max_intent_iterations 同步值（默认 5，D5 拍板）。"""
    intent_sufficient: bool
    """intent_observer LLM 评估的"信息已充分"信号，should_continue_intent 5 级判断之一。"""
    consecutive_intent_errors: int
    """连续 intent_executor 失败计数；>=2 触发 should_continue_intent 早退。"""
    current_intent_plan: dict
    """intent_planner 输出（IntentPlan model_dump 后的 dict），intent_executor 消费 next_actions 执行工具。"""
    current_action_results: list[dict]
    """intent_executor 执行后的 ToolResult 列表，intent_observer 消费写入 evidence_pool。"""
    last_intent_observation: dict
    """intent_observer 输出（IntentObservation model_dump 后的 dict），intent_planner 下跳读取避免重复决策。"""
    conversation_history_summary: str
    """normalize 顺便产出的多轮对话摘要（≤2 句，含上轮 answer 摘要 + 未解决疑问），
    intent_planner 接收避免完整 history 撑爆 prompt（D4 拍板）。首轮对话=""。"""


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

        # ------------------------------------------------------------------
        # 主图节点注册（T47.4 重组）
        # ------------------------------------------------------------------
        # T47.4 前置 + crystallized_check + 6 状态路由（不变）
        workflow.add_node("probe", probe_node)
        workflow.add_node("crystallized_check", crystallized_check_node)
        # T47.4 新增：extract_urls + url_pre_fetch（D6 + D7 A 方案）
        # extract_urls 同步无 LLM，正则提取 user_urls
        workflow.add_node("extract_urls", create_extract_urls())
        # url_pre_fetch async，浅抓 user_urls 内容供 normalize 改写时作上下文（不写持久化）
        workflow.add_node("url_pre_fetch", create_url_pre_fetch(self.config))
        # normalize / decompose（normalize T47.2 已扩展接 url_pre_fetch_content 输入 +
        # 产出 conversation_history_summary；不变量在 T47.2/T47.3a/T47.3b 已验证）
        workflow.add_node("normalize", create_normalize_node(llm))
        workflow.add_node("decompose", create_decompose_node(llm))
        # T47.4 新增：统一意图识别 Agent-Loop 4 节点（T47.0 §4-7）
        # planner / observer 是 sync（LLM invoke_structured），executor 是 async（asyncio.gather）
        # T27 fail-fast 已在节点工厂内固化（llm=None 直接抛 ValueError）
        workflow.add_node("intent_planner", create_intent_planner(llm))
        workflow.add_node("intent_executor", create_intent_executor(llm, self.config))
        workflow.add_node("intent_observer", create_intent_observer(llm))
        # T47.4 新增：merge_evidence（纯格式转换，Evidence pool → get_info_candidates 13 字段）
        workflow.add_node("merge_evidence", merge_evidence_node)
        # T26.1 持久化流水（merge_evidence 后接入，下游完全不变）
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

        # ------------------------------------------------------------------
        # 边 / 路由（T47.4 重组）
        # ------------------------------------------------------------------
        workflow.set_entry_point("probe")
        workflow.add_edge("probe", "crystallized_check")
        # T47.4 改 mapping：miss/stale/observed/degraded → extract_urls 替代 normalize
        workflow.add_conditional_edges(
            "crystallized_check",
            self.routing.after_crystallized_check,
            {"answer": "answer", "extract_urls": "extract_urls"},
        )
        # T47.4 新增：extract_urls → user_urls 非空 → url_pre_fetch / 空 → normalize（D7 A 方案）
        workflow.add_conditional_edges(
            "extract_urls",
            self.routing.route_after_extract_urls,
            {"url_pre_fetch": "url_pre_fetch", "normalize": "normalize"},
        )
        # url_pre_fetch → normalize（不变 wiring，url_pre_fetch 跑完无条件接 normalize）
        workflow.add_edge("url_pre_fetch", "normalize")
        # normalize → decompose（不变）
        workflow.add_edge("normalize", "decompose")
        # T47.4 替代：decompose → intent_planner（不再 → classify_plan 三路分流）
        workflow.add_edge("decompose", "intent_planner")
        # T47.4 新增：统一意图识别 Agent-Loop 4 节点串联
        workflow.add_edge("intent_planner", "intent_executor")
        workflow.add_edge("intent_executor", "intent_observer")
        # T47.4 新增：should_continue_intent 5 级早退路由
        #   - consecutive_intent_errors ≥2 / intent_sufficient / 上限 / no_action → merge_evidence
        #   - 其余 → intent_planner（继续下一跳）
        workflow.add_conditional_edges(
            "intent_observer",
            self.routing.should_continue_intent,
            {
                "intent_planner": "intent_planner",
                "merge_evidence": "merge_evidence",
            },
        )
        # T47.4 替代 T46 三路汇聚：merge_evidence → fanout_persist_dispatcher
        # merge_evidence 输出 get_info_candidates 13 字段格式与 T46 三路汇聚后完全一致（T47.3b 已验证），
        # 下游持久化流水 / PIPE2 / judge / answer / self_check / crystallize_answer 零改动
        workflow.add_conditional_edges(
            "merge_evidence",
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

        T47.4 后主图含多个 async 节点（``url_pre_fetch`` httpx asyncio.gather 浅抓 +
        ``intent_executor`` 多工具 fan-out 并发），走 ``ainvoke()`` + ``asyncio.run()``
        打包 sync 接口（参考 GetInfoGraph 同样路径）。

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
            # T26.1 reducer 字段：write_raw_one × N / enrich_one × M 首次 add 需初始化
            "persist_results": [],
            "enrich_results": [],
            # T26.1 错误聚合 + Milvus 计数初始值（barrier / ingest 会覆写）
            "persist_errors": [],
            "ingested_count": 0,
            # T28 PIPE2 reducer 字段：subquery_search_one × N 首次 add 需初始化
            "sub_evidence": [],
            "search_errors": [],
            # T25 dispatcher 判 get_info_attempted 防死循环；T47.4 后由 merge_evidence 写 True
            "get_info_config": self.config,
            "get_info_attempted": False,
            # T47.6 删除 T46 hop 字段初始化（plan_type / max_hops / pending_goals /
            # resolved_entities / hops / hop_count / current_tool_selection /
            # current_tool_result / consecutive_tool_errors）— 字段定义 + 写入节点都已删除。
            "user_urls": [],
            # T47 统一意图识别 Agent-Loop 字段初始化（T47.2-T47.4 全节点已接入主图）
            "url_pre_fetch_content": [],
            "evidence_pool": [],
            "visited_urls": [],
            "iteration_count": 0,
            "max_iterations": self.config.max_intent_iterations,
            "intent_sufficient": False,
            "consecutive_intent_errors": 0,
            "current_intent_plan": {},
            "current_action_results": [],
            "last_intent_observation": {},
            "conversation_history_summary": "",
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
