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
    """qa.rewrite 节点输出。"""
    queries: list[RewrittenQuery] = Field(min_length=1, max_length=6)


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


# ---------------------------------------------------------------------------
# Crystallize 子图：value_score / hit_check / skill_gen
# ---------------------------------------------------------------------------


RecommendedLayer = Literal["hot", "cold", "skip"]


class ValueScore(BaseModel):
    """crystallize.value_score 节点输出：四维度评分。"""
    generality: float = Field(ge=0.0, le=1.0, description="问题普适性")
    stability: float = Field(ge=0.0, le=1.0, description="答案稳定性 / 时效")
    evidence_quality: float = Field(ge=0.0, le=1.0, description="证据质量")
    cost_benefit: float = Field(ge=0.0, le=1.0, description="复用收益 / 计算成本比")
    composite_score: float = Field(ge=0.0, le=1.0)
    recommended_layer: RecommendedLayer
    trigger_keywords: list[str] = Field(min_length=3, max_length=8)
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
    trigger_keywords: list[str] = Field(min_length=3, max_length=8)
    layer: Literal["hot", "cold"]
    answer_markdown: str = Field(description="可直接展示的固化答案 Markdown")


# ---------------------------------------------------------------------------
# Persistence 子图：enrich
# ---------------------------------------------------------------------------


class ChunkEnrichment(BaseModel):
    """persistence.enrich 节点输出：写回 chunk frontmatter 的三件套。"""
    summary: str = Field(min_length=10, max_length=200, description="一段话摘要")
    keywords: list[str] = Field(min_length=5, max_length=10)
    questions: list[str] = Field(
        min_length=3, max_length=8, description="doc2query 反向问题"
    )


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
