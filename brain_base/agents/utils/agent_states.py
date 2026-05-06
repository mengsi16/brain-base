"""
共享状态定义。

参考 TradingAgents 的 agents/utils/agent_states.py 模式。
所有图共享的 TypedDict 状态集中定义在此。
"""

from typing import Annotated, Any, Optional
from typing_extensions import TypedDict


class IngestFileState(TypedDict, total=False):
    """文件入库图状态"""
    input_files: list[str]
    upload_date: str
    converted_files: list[dict]
    doc_id: str
    raw_md_path: str
    frontmatter: dict
    persistence_result: dict
    error: str


class IngestUrlState(TypedDict, total=False):
    """URL 入库图状态。

    字段命名以 ``brain_base/nodes/ingest_url.py`` 节点实际读写的为准（``raw_html``
    / ``cleaned_md`` / ``extraction_status`` / ``completeness_status`` 等），
    未声明字段会被 langgraph 默认 reducer 丢弃，导致下游节点拿到空值。
    """
    url: str
    source_type: str
    topic: str
    title_hint: str
    # fetch_node 输出
    raw_html: str
    raw_content: str
    extraction_status: str  # ok / spa-failed / missing-input / insufficient-content
    # clean_node 输出
    cleaned_md: str
    # completeness_check_node 输出
    completeness_status: str  # ok / spa-failed / insufficient-content / over-cleaned
    completeness_chars: int
    completeness_reason: str
    # frontmatter / persist 输出
    doc_id: str
    raw_md_path: str
    frontmatter: dict
    persistence_result: dict
    error: str


class PersistenceState(TypedDict, total=False):
    """持久化子图状态"""
    raw_md_path: str
    doc_id: str
    chunk_files: list[str]
    enriched_count: int
    milvus_result: dict
    error: str


class QaState(TypedDict, total=False):
    """QA 主图状态"""
    question: str
    infra_status: dict
    crystallized_status: str
    crystallized_answer: str
    skill_id: str
    cold_evidence_summary: str
    normalized_query: str
    sub_queries: list[str]
    rewritten_queries: list[str]
    evidence: list[dict]
    evidence_sufficient: bool
    answer: str
    crystallize_result: dict
    error: str
    # 自动外检 + 入库回路（T10）
    trigger_get_info: bool
    """get_info_trigger 节点输出：是否需要外检。"""
    search_hint: str
    """get_info_trigger 给 GetInfoGraph 的搜索提示（user_question 之外的补充信息）。"""
    get_info_reason: str
    """触发外检的判定理由，便于日志和审计。"""
    get_info_attempted: bool
    """是否已尝试过一轮外检；防死循环（第二次到 judge 强制 answer）。"""
    get_info_candidates: list[dict]
    """GetInfoGraph 返回的全部 URL 候选列表。"""
    ingest_targets: list[dict]
    """select_candidates 节点按配额筛选 + 去重后的最终入库目标。"""
    get_info_ingested: list[str]
    """成功入库的 doc_id 列表。"""
    ingest_errors: list[str]
    """入库失败的 URL + 错误信息（不阻断后续候选）。"""


class CrystallizeState(TypedDict, total=False):
    """固化层子图状态"""
    mode: str
    user_question: str
    extracted_entities: list[str]
    status: str
    skill_id: str
    answer_markdown: str
    cold_evidence_summary: str
    layer: str
    last_confirmed_at: str
    freshness_ttl_days: int
    value_score: float
    trigger_keywords: list[str]
    description: str


class LifecycleState(TypedDict, total=False):
    """生命周期管理图状态"""
    doc_ids: list[str]
    urls: list[str]
    sha256: str
    confirm: bool
    force_recent: bool
    reason: str
    resolved_doc_ids: list[str]
    targets: list[dict]
    dry_run_report: dict
    milvus_delete_result: dict
    milvus_delete_failed: bool
    file_delete_errors: list[str]
    index_clean_errors: list[str]
    audit_log_path: str
    error: str


class LintState(TypedDict, total=False):
    """固化层清理图状态"""
    entries: list[dict]
    scan_status: str
    to_degrade: list[str]
    to_delete: list[str]
    to_keep: list[str]
    degraded: list[str]
    deleted: list[str]


class BrainBaseState(TypedDict, total=False):
    """顶层编排图状态（对标 TradingAgents 的 AgentState）"""
    mode: str  # ask / ingest-file / ingest-url / remove-doc / lint
    question: str
    input_files: list[str]
    url: str
    source_type: str
    topic: str
    doc_ids: list[str]
    confirm: bool
    reason: str
    # 各子图结果
    qa_result: dict
    ingest_file_result: dict
    ingest_url_result: dict
    lifecycle_result: dict
    lint_result: dict
    crystallize_result: dict
    error: str
