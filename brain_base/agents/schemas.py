"""
Pydantic schemas 用于 agent 结构化输出。

参考 TradingAgents 的 agents/schemas.py 模式。

约定：
- 业务最终结果（IngestResult / QaAnswer / CrystallizeResult / LifecycleResult）：
  描述 agent/graph 对外返回的稳定数据
- LLM 中间步骤结构化输出（NormalizedQuestion / RewrittenQueries / ...）：
  通过 llm.with_structured_output(Schema) 让 LLM 直接返回类型化对象，
  避免在提示词中重复写"输出 JSON 格式"段落
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 入库结果
# ---------------------------------------------------------------------------


class IngestStatus(str, Enum):
    OK = "ok"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    FAILED = "failed"
    INSUFFICIENT_CONTENT = "insufficient-content"
    SPA_FAILED = "spa-failed"


class IngestResult(BaseModel):
    """单个文档入库结果"""
    doc_id: str = Field(description="文档 ID")
    status: IngestStatus = Field(description="入库状态")
    raw_path: str = Field(default="", description="raw 文件路径")
    chunk_count: int = Field(default=0, description="chunk 数量")
    milvus_inserted: int = Field(default=0, description="Milvus 插入行数")
    error: str = Field(default="", description="错误信息")


# ---------------------------------------------------------------------------
# QA 结果
# ---------------------------------------------------------------------------


class ExtractionStatus(str, Enum):
    OK = "ok"
    SPA_FAILED = "spa-failed"
    INSUFFICIENT_CONTENT = "insufficient-content"
    OVER_CLEANED = "over-cleaned"
    DEGRADED = "degraded"


class QaAnswer(BaseModel):
    """QA 回答结构化输出"""
    answer: str = Field(description="答案正文")
    evidence_count: int = Field(default=0, description="证据条数")
    crystallized_status: str = Field(default="miss", description="固化层命中状态")
    extraction_status: ExtractionStatus = Field(default=ExtractionStatus.OK)
    source_type: str = Field(default="local", description="证据来源类型")


# ---------------------------------------------------------------------------
# 固化层结果
# ---------------------------------------------------------------------------


class CrystallizeLayer(str, Enum):
    HOT = "hot"
    COLD = "cold"


class CrystallizeResult(BaseModel):
    """固化层写入结果"""
    skill_id: str = Field(description="固化条目 ID")
    status: str = Field(description="created_hot / created_cold / updated / skipped")
    layer: Optional[CrystallizeLayer] = Field(default=None)
    value_score: float = Field(default=0.0, description="价值评分")


# ---------------------------------------------------------------------------
# 生命周期结果
# ---------------------------------------------------------------------------


class LifecycleAction(str, Enum):
    REMOVE_DOC = "remove_doc"
    ARCHIVE_DOC = "archive_doc"


class LifecycleResult(BaseModel):
    """生命周期操作结果"""
    action: LifecycleAction = Field(description="操作类型")
    doc_ids: list[str] = Field(default_factory=list, description="涉及的 doc_id")
    confirm: bool = Field(default=False, description="是否已确认执行")
    docs_removed: int = Field(default=0)
    chunks_removed: int = Field(default=0)
    milvus_rows_removed: int = Field(default=0)
    errors: list[str] = Field(default_factory=list)


# ===========================================================================
# LLM 中间步骤结构化输出
#
# 节点工厂（create_xxx_node）通过 llm.with_structured_output(Schema) 让 LLM
# 直接返回类型化对象，避免在 prompts 里写「输出 JSON 格式如下：…」。
# 字段数量约束（min_length / max_length）由 Pydantic 强制，不再写进 prompt。
# ===========================================================================


# ---------------------------------------------------------------------------
# QA 子图：normalize / decompose / rewrite / judge / answer / self_check
# ---------------------------------------------------------------------------


QuestionType = Literal["fact", "procedure", "concept", "comparison", "opinion"]


class NormalizedQuestion(BaseModel):
    """qa.normalize 节点输出。把口语化问题归一成 RAG 友好形式。"""
    normalized: str = Field(description="归一后的检索问题")
    expected_type: QuestionType = Field(description="期望答案类型")
    time_sensitive: bool = Field(default=False, description="是否对时效性敏感")
    language: Literal["zh", "en", "mixed"] = Field(default="zh")
    # T31 新增：时间归一化（time_sensitive=True 且原句含模糊时间词时输出）
    time_range: list[str] | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        description=(
            "time_sensitive=True 且原句含模糊时间词（最近/今年/过去一周等）时，"
            "输出 [start_iso, end_iso]（YYYY-MM-DD 格式，end 不晚于今天）；否则 null。"
        ),
    )
    # T31 新增：缩写歧义消解（≥2 个常见解读时输出候选清单）
    abbreviation_hints: list[str] | None = Field(
        default=None,
        description=(
            '缩写有 >=2 个常见解读时输出候选清单'
            '（每条形如 "RAGFlow（检索增强生成框架）"）；'
            "单义缩写或无缩写时 null。"
        ),
    )
    # T37 新增：对话历史指代消解后的独立完整问题
    contextualized_query: str | None = Field(
        default=None,
        description=(
            "当对话历史存在且当前问题包含指代/省略时，"
            "输出消解后的独立完整问题；否则 null。"
        ),
    )
    # T47.2 新增（契约 §11 + D4 拍板）：多轮对话的本轮上下文摘要，
    # 由 normalize 节点顺便产出，供 intent_planner 节点接收（避免完整 history 撑爆 prompt）。
    conversation_history_summary: str | None = Field(
        default=None,
        description=(
            "≤2 句简短摘要，总结上一轮问答的核心主题与未解决疑问。"
            "首轮对话（无对话历史）时输出空串或 null；"
            "intent_planner 用此字段做多轮上下文，避免完整 history 撑爆 prompt。"
        ),
    )


class SubQuestion(BaseModel):
    """单个子问题。"""
    text: str = Field(description="子问题文本")
    type: Literal["sub-fact", "synthesis"] = Field(description="子问题类型")


class DecomposedQuestion(BaseModel):
    """qa.decompose 节点输出。复杂问题拆成 ≤ 4 个子问题。"""
    needs_decompose: bool = Field(description="是否需要分解；False 时 sub_questions 为空")
    sub_questions: list[SubQuestion] = Field(
        default_factory=list, max_length=4, description="子问题列表"
    )


QueryLayer = Literal["L0", "L1", "L2", "L3"]


class RewrittenQuery(BaseModel):
    """单条改写查询，按 L0–L3 分层。"""
    text: str = Field(description="改写后的检索查询")
    layer: QueryLayer = Field(description="L0 原文 / L1 同义改写 / L2 上下位扩展 / L3 反向假设")


class RewrittenQueries(BaseModel):
    """qa.rewrite 节点输出。

    LLM 一次性产出两个字段：
    - ``queries``：长句分层改写，给 Milvus hybrid 多路检索；
    - ``lexical_query``：sparse gate 用的短自然语言串（T30 取代旧 ``grep_keywords``）。

    ``lexical_query`` 设计：
    - 不再是关键词列表（grep AND 字面匹配），而是 1 段简短自然语言查询（≤30 字）；
    - 喂给 milvus ``text_search`` 走 sparse 通道（bge-m3 sparse + tf-idf）；
    - top-3 score 平均若 < 阈值 0.20 → ``needs_get_info=true`` 走外检；
    - 与 grep 字面 AND 匹配相比，sparse tokenizer 能 handle "字面 vs 语义"
      不匹配（如 "RAGFlow 定义/核心概念/架构" 这种抽象词，sparse 仍能命中
      "RAGFlow 系统概述/简介" 类文档）。
    """
    queries: list[RewrittenQuery] = Field(min_length=1, max_length=6)
    lexical_query: str = Field(
        min_length=2, max_length=30,
        description="sparse gate 检索用短串（≤30 字）：包含主实体词（产品/项目/专有名词，保留原大小写）+ 用户意图核心动作或属性。例：'RAGFlow 部署' / 'RAG-Anything 用法' / 'YOLOv8 性能'。不要疑问词、不要分隔符、不要长句改写。",
    )


JudgeRecommendation = Literal["generate_answer", "trigger_get_info", "degrade"]


class EvidenceJudgment(BaseModel):
    """qa.judge 节点输出：基于检索得分判定下一步走向。"""
    sufficient: bool = Field(description="证据是否足以生成答案")
    avg_score: float = Field(default=0.0, ge=0.0, le=1.0)
    coverage: float = Field(default=0.0, ge=0.0, le=1.0, description="子意图覆盖率")
    recommendation: JudgeRecommendation
    reason: str = Field(default="", max_length=200)


CheckVerdict = Literal["pass", "fail"]


class SelfCheckResult(BaseModel):
    """qa.self_check 节点输出：Maker-Checker 三维度自检。"""
    faithfulness: CheckVerdict = Field(description="是否仅基于证据，未编造")
    completeness: CheckVerdict = Field(description="是否覆盖问题主要子意图")
    consistency: CheckVerdict = Field(description="证据之间是否冲突已标注")
    revised_answer: str = Field(default="", description="如有不通过项的修正版本；自检只删不增")
    notes: str = Field(default="", max_length=300)


TimeRangeHint = Literal["1mo", "3mo", "1y", "none"]


class GetInfoTrigger(BaseModel):
    """qa.get_info_trigger 节点输出：触发外部补库时给 GetInfoGraph 的提示。"""
    needed: bool
    reason: str = Field(default="", max_length=200)
    time_range_hint: TimeRangeHint = Field(default="none")
    suggested_keywords: list[str] = Field(default_factory=list, max_length=8)


# T47.6 删除：T40 场景化搜索策略 schemas（SearchScenario / SearchStrategy /
# SearchStrategyBatch）— search_strategy 节点已随 T47.4 从主图拔除，T47.6
# 删除函数实现后该组 schema 零使用。


# ---------------------------------------------------------------------------
# Crystallize 子图：value_score / hit_check / skill_gen
# ---------------------------------------------------------------------------


RecommendedLayer = Literal["hot", "cold", "skip"]

# T41 新增：固化层场景枚举（hit_check scenario 二次过滤用）
CrystallizedScenario = Literal[
    "definition",    # X 是什么 / 用途 / 简介
    "howto",         # 怎么做 / 如何 / 安装 / 部署 / 使用
    "compare",       # X vs Y / 区别 / 差异
    "troubleshoot",  # 报错 / 失败 / 调试
    "config",        # 配置 / 参数 / 选项
    "update",        # 最新 / 最近更新 / 版本
    "general",       # 以上都不匹配
]


class ValueScore(BaseModel):
    """crystallize.value_score 节点输出：四维度评分。"""
    generality: float = Field(ge=0.0, le=1.0, description="问题普适性")
    stability: float = Field(ge=0.0, le=1.0, description="答案稳定性 / 时效")
    evidence_quality: float = Field(ge=0.0, le=1.0, description="证据质量")
    cost_benefit: float = Field(ge=0.0, le=1.0, description="复用收益 / 计算成本比")
    composite_score: float = Field(ge=0.0, le=1.0)
    recommended_layer: RecommendedLayer
    # T41 收紧：entities 是主匹配字段，必须 1-5 项，是专有名词/产品/版本
    entities: list[str] = Field(
        default_factory=list,
        max_length=5,
        description=(
            "问题/答案中的专有名词（产品 / 框架 / 工具 / 版本 / 平台）。"
            "禁止疑问词 / 泛词 / 动词。用于 hit_check 主匹配。"
        ),
    )
    scenario: CrystallizedScenario = Field(
        default="general",
        description="问题场景类别，hit_check 二次过滤用。",
    )
    # trigger_keywords 保留但降级为辅助描述字段（不再用于命中判断）
    trigger_keywords: list[str] = Field(default_factory=list, max_length=10)
    reason: str = Field(default="", max_length=300)


HitStatus = Literal[
    "hit_fresh",
    "hit_stale",
    "cold_observed",
    "cold_promoted",
    "miss",
    "degraded",
]


class HitCheckResult(BaseModel):
    """crystallize.hit_check 节点输出：固化层命中状态。"""
    status: HitStatus
    skill_id: str = Field(default="")
    last_confirmed_at: str = Field(default="", description="ISO 日期；hit_stale 时关键")
    freshness_ttl_days: int = Field(default=0, ge=0)
    reason: str = Field(default="", max_length=200)


class CrystallizedSkill(BaseModel):
    """crystallize.skill_gen 节点输出：固化条目骨架。"""
    skill_id: str
    title: str = Field(max_length=120)
    description: str = Field(max_length=400)
    # T41 收紧：entities 是主匹配字段（必须 1-5 项专有名词），hit_check 强制要求
    entities: list[str] = Field(
        min_length=1, max_length=5,
        description=(
            "专有名词列表：产品名 / 框架名 / 工具名 / 版本号 / 平台名。"
            "禁止疑问词、泛词、动词。hit_check 主匹配字段。"
        ),
    )
    scenario: CrystallizedScenario = Field(
        default="general",
        description="问题场景类别。hit_check scenario 过滤用。",
    )
    # trigger_keywords 降级为描述辅助字段（可空），hit_check 不再强依赖
    trigger_keywords: list[str] = Field(default_factory=list, max_length=10)
    layer: Literal["hot", "cold"]
    answer_markdown: str = Field(description="可直接展示的固化答案 Markdown")


# ---------------------------------------------------------------------------
# Persistence 子图：enrich
# ---------------------------------------------------------------------------


class ChunkEnrichment(BaseModel):
    """persistence.enrich 节点输出：写回 chunk frontmatter 的四件套。

    T26.1-a：从 backup `chunk-enrichment` skill 升级，加 title 字段。
    title 是 chunk 章节级标题（不是 doc 级页面 title），由 LLM 从首个 H1/H2/H3
    或首段提炼，覆盖 chunker 透传的 doc 级 title。
    """
    title: str = Field(
        min_length=1, max_length=80,
        description="chunk 章节级标题，从首个 H1/H2/H3 提取并精简",
    )
    summary: str = Field(min_length=10, max_length=200, description="一段话摘要")
    # 数量边界放宽：prompt 软约束目标 5-10，但 LLM 对数量上限遵守度差（实测 chunk-022 输出 11 个 keyword 超 max=10 导致整 chunk ValidationError）。
    # 上限提到 30 防爆 + 下限放到 1 防完全 lazy，实际数量由 prompt 引导而非 schema 强制。
    keywords: list[str] = Field(min_length=1, max_length=30)
    questions: list[str] = Field(
        min_length=1, max_length=15,
        description="doc2query 反向问题（六维度按 chunk 适用性选择）；prompt 引导 3-8，schema 实际接受 1-15",
    )


class DocEnrichment(BaseModel):
    """upload 路径 doc 级 LLM 富化输出（T32 新增）：写回 raw md frontmatter 的 2 字段。

    与 ChunkEnrichment 区别（设计决策见 md/research/2026-05-10-t32-upload-path-execution-plan.md D1）：
    - 不含 questions：doc 级问题语义模糊，chunk 级 doc2query 已覆盖检索召回。
    - 不含 title：frontmatter_node H1 提取已稳定，doc_enrich 不覆盖。
    - summary 上限放宽到 400（doc 级需 3 句概括）。
    - keywords 上限放宽到 15（doc 级覆盖面更广）。
    """
    summary: str = Field(min_length=20, max_length=400, description="doc 级 3 句以内全文概括")
    # 数量边界与 ChunkEnrichment 同步放宽（理由同 ChunkEnrichment.keywords 注释）。
    keywords: list[str] = Field(min_length=1, max_length=30, description="doc 级关键词，覆盖面比 chunk 级广；prompt 引导 5-15，schema 实际接受 1-30")


# ---------------------------------------------------------------------------
# GetInfo 子图：plan_next_query / classify_url
# ---------------------------------------------------------------------------


PlanMode = Literal["broaden", "narrow", "site_search", "translate"]


class NextQueryPlan(BaseModel):
    """get_info.plan_next_query 节点输出：下一轮搜索策略。"""
    query: str = Field(description="本轮搜索查询")
    mode: PlanMode
    target_engine: Literal["google", "bing"] = Field(default="google")
    reason: str = Field(default="", max_length=200)


SourceTypeOpt = Literal["official-doc", "community", "discard"]


class UrlClassification(BaseModel):
    """get_info.classify_url 节点输出：单 URL 分类。"""
    url: str
    source_type: SourceTypeOpt
    confidence: float = Field(ge=0.0, le=1.0)
    title_hint: str = Field(default="", max_length=200)
    reason: str = Field(default="", max_length=200)


class UrlClassificationBatch(BaseModel):
    """LLM 一次评估多个 URL 时的批量输出。"""
    classifications: list[UrlClassification] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# T16：preview_fetch + score_candidates（Agent 化候选选择）
# ---------------------------------------------------------------------------


class CandidatePreview(BaseModel):
    """preview_fetch 节点输出（纯爬取结果，无 LLM 介入）。

    单次抓取的精简 snapshot：仅取 title + 首个 heading + 前 800 字 innerText。
    用于喂给 score_candidates 节点的 LLM 评分；**不写入向量库**——向量库的内容
    仍然走 select_candidates → ingest_candidates → IngestUrlGraph 的完整链路。
    """
    url: str
    fetched: bool = Field(description="抓取是否成功；失败时 title/heading/preview_text 为空")
    title: str = Field(default="", max_length=300)
    heading: str = Field(default="", max_length=300, description="首个 h1/h2 文本")
    preview_text: str = Field(default="", max_length=1200, description="正文前 ~800 字 innerText")
    error: str = Field(default="", max_length=200)


class CandidateScore(BaseModel):
    """score_candidates 节点单候选输出（LLM 看真内容评分）。

    单候选独立 prompt，prompt 短（仅含原问题 + 该候选 url/title/heading/preview）。
    禁止把多个候选拼到同一 prompt 里——上下文隔离原则（用户冻结）。
    """
    priority_score: int = Field(
        ge=0, le=100,
        description="0–100：综合相关性 + 信息密度 + 文档质量。100 = 完美的项目文档；50 = 一般技术文章；0 = 完全无关或营销页。"
    )
    relevance_reason: str = Field(default="", max_length=200, description="评分理由，便于审计")
    is_docs: bool = Field(description="是否真文档（README / readthedocs / 官方 docs）")
    is_landing: bool = Field(description="是否营销页 / landing / 仅有 testimonial 没有实质内容")


# ---------------------------------------------------------------------------
# T25：fetch_extract（多 URL 爬取处理 — LLM 一次产 6 字段）
# ---------------------------------------------------------------------------


FetchExtractType = Literal["official-doc", "community", "discard"]


class FetchExtractResult(BaseModel):
    """qa_get_info.fetch_extract_one 节点 LLM 输出。

    LLM 一次性产 6 字段，针对单 URL 的完整 markdown 给出整体评估：
    - ``score``：0-100 相关性
    - ``type``：文档类型三选一
    - ``summary``：200-400 字摘要
    - ``keywords``：3-10 个关键词
    - ``whether_in``：是否纳入知识库（False=与所有子问题都无关或质量过低）
    - ``reason``：whether_in 判定理由

    分解模式（user_prompt 含子问题列表）下，whether_in 服务"任一子问题相关"
    即 True；reason 中应标注主要服务的 sub_idx。
    """
    score: int = Field(ge=0, le=100, description="相关性 0-100")
    type: FetchExtractType = Field(
        description="文档类型：official-doc 官方文档 / community 社区 / discard 垃圾"
    )
    summary: str = Field(
        max_length=500,
        description="200-400 字摘要，覆盖与用户问题相关的核心信息",
    )
    keywords: list[str] = Field(
        min_length=3,
        max_length=10,
        description="3-10 个关键词：主实体词 + 该 URL 特有的差异化词",
    )
    whether_in: bool = Field(
        description="是否纳入知识库；False=与所有子问题都无关或质量过低",
    )
    reason: str = Field(default="", max_length=200, description="whether_in 判定理由")


# ---------------------------------------------------------------------------
# T46：Agentic-RAG 工具化检索 + 迭代多跳
# ---------------------------------------------------------------------------


# T47.6 删除：T46 迭代多跳三路分流 schemas（RetrievalPlan / HopPlan /
# HopObservation）— classify_plan / hop_planner / hop_observer / tool_executor
# 节点已随 T47.4 / T47.6 删除，该组 schema 零使用。统一意图识别 Agent-Loop
# 契约下的 schema 见下方 Evidence / IntentAction / IntentPlan / IntentObservation。
#
# ---------------------------------------------------------------------------
# T47：统一意图识别 Agent-Loop（替代 T46 hop 循环 + classify_plan 三路分流）
# 契约引用：md/research/2026-05-17-t47-unified-intent-agent-contract.md §4-§7
# ---------------------------------------------------------------------------


class Evidence(BaseModel):
    """evidence_pool 元素：intent_executor 工具执行结果汇总后的标准化证据。

    与 hops[].observation 不同：Evidence 是平铺给 merge_evidence
    转 get_info_candidates 用的，含完整的 url/title/markdown/score 等持久化所需字段。

    score 范围 0-100 与 ``FetchExtractResult.score`` 对齐——fetch_extract_one
    是当前最大上游证据来源，避免 merge_evidence 写额外转换层。
    """
    url: str = Field(description="证据来源 URL（去重 key）")
    title: str = Field(default="", max_length=500)
    content: str = Field(default="", description="markdown 正文")
    score: float = Field(
        default=0.0, ge=0.0, le=100.0,
        description="LLM 评估分（0-100），与 FetchExtractResult.score 对齐",
    )
    sha256_hash: str = Field(default="", description="content 的 SHA-256，dedup 用")
    from_queries: list[str] = Field(
        default_factory=list,
        description="该证据为哪些 sub_question / 改写 query 服务",
    )
    snippet: str = Field(default="", max_length=500, description="证据摘要")
    source_type: str = Field(
        default="community",
        description="official-doc / community / discard，与 FetchExtractType 对齐",
    )
    tool_name: str = Field(
        default="",
        description="哪个工具产出（fetch_url / search_web_dual / fetch_extract_one / arxiv_pdf / github_raw 等）",
    )
    raw_path: str = Field(
        default="",
        description=(
            "T48.3：持久化已落盘的 raw md 路径（fast-path 工具填，如 arxiv_pdf）。"
            "下游 write_raw_one 看到该字段已含路径 + 文件存在 → 跳过 fetch + write，"
            "直接调 chunker；observer 仍用 content (前 3000 字) 评分，需全文时读 raw_path。"
        ),
    )


class IntentAction(BaseModel):
    """intent_planner 输出的单个动作：调用 TOOL_REGISTRY 工具的参数化指令。

    长度由 IntentPlan.next_actions list 控制，本 schema 只描述单个动作。
    """
    tool_name: str = Field(
        description="工具名，必须在 TOOL_REGISTRY 白名单内（fetch_url / search_web_dual / fetch_extract_one / arxiv_pdf / github_raw）",
    )
    tool_args: dict = Field(
        default_factory=dict,
        description="工具入参（含 url / query / topk 等，按 ToolSpec 约定）",
    )
    purpose: str = Field(
        default="",
        max_length=300,
        description="该动作为了回答哪个 sub_question / 满足什么信息缺口（供 intent_observer 关联评估）",
    )


class IntentPlan(BaseModel):
    """intent_planner 节点输出：本跳的动作计划。

    **fan-out 语义**（D1 拍板）：
    - ``len(next_actions) == 0``：无动作（配 ``early_exit=False`` 时由 should_continue_intent
      触发 'no_action' 早退，配 ``early_exit=True`` 表示信息已充分）；
    - ``len(next_actions) == 1``：串行单工具；
    - ``len(next_actions) > 1``：fan-out 并发（intent_executor 内 ``asyncio.gather``）。
    """
    next_actions: list[IntentAction] = Field(
        default_factory=list,
        description="≥0 个动作；为空且 early_exit=False 时 should_continue_intent 触发 'no_action' 早退",
    )
    reasoning: str = Field(
        default="",
        max_length=500,
        description="LLM 决策理由（debug + 审计用，不影响下游逻辑）",
    )
    early_exit: bool = Field(
        default=False,
        description="True 表示信息已充分，intent_executor 跳过执行，should_continue_intent 路由到 merge_evidence",
    )


class IntentObservation(BaseModel):
    """intent_observer 节点 LLM 总结：本跳所得 + 信息充分性评估。

    ``confidence >= 0.85 and remaining_gaps == []`` 由 intent_observer 翻译为
    ``intent_sufficient = True``，should_continue_intent 据此 5 级判断之一早退。
    """
    new_evidence_count: int = Field(
        default=0, ge=0,
        description="本跳新增几条 evidence（去重后）",
    )
    coverage_summary: str = Field(
        default="",
        max_length=500,
        description="LLM 总结当前 evidence_pool 已覆盖 sub_questions 的哪些部分",
    )
    remaining_gaps: list[str] = Field(
        default_factory=list,
        description="仍未回答的子问题列表（空表示全覆盖）",
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="信心 0-1，越高越接近充分",
    )


# ---------------------------------------------------------------------------
# IngestUrl 子图：completeness check
# ---------------------------------------------------------------------------


CompletenessStatus = Literal["ok", "spa-failed", "insufficient-content", "over-cleaned"]


class CompletenessJudgment(BaseModel):
    """ingest_url.completeness_check 节点输出。"""
    status: CompletenessStatus
    chars: int = Field(default=0, ge=0)
    reason: str = Field(default="", max_length=200)


# ---------------------------------------------------------------------------
# Self-heal 子图：recall_diagnosis
# ---------------------------------------------------------------------------


RecallDimension = Literal[
    "direct", "action", "comparison", "fault", "alias", "version"
]
RecallRootCause = Literal[
    "missing_doc",
    "missing_dimension",
    "stale_evidence",
    "source_conflict",
    "ranking_failure",
    "ok",
]


class RecallDiagnosis(BaseModel):
    """self_heal.recall_diagnosis 节点输出：召回失败根因。"""
    root_cause: RecallRootCause
    missing_dimensions: list[RecallDimension] = Field(default_factory=list)
    suggested_action: Literal["self_heal", "trigger_get_info", "manual_review", "no_op"]
    reason: str = Field(default="", max_length=300)
