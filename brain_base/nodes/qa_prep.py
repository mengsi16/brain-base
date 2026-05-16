"""QA 第一段 fan-out 节点：subquery_prep（rewrite + sparse gate）。

每个子问题独立实例化跑一次（通过 ``Send("subquery_prep", PrepState)`` ×
N 派发，N 个实例 LangGraph 自动并行）：

    rewrite (LLM, asyncio.to_thread invoke_structured)
        → 产出 queries: list[{text, layer}] + lexical_query: str
    sparse gate (milvus text_search top-3 平均)
        → 产出 lexical_score: float + needs_get_info: bool

T30：原本地 ``grep_keywords_and`` (字面 AND 匹配) 换为 milvus ``text_search``
(sparse 向量检索，bge-m3 sparse + tf-idf)。联动原因：
- LLM 倒向生成抽象元语词（定义 / 核心概念），与文档实际字面不匹配
  → grep AND 可能 0 hit，误触发外检；
- sparse tokenizer + tf-idf 能 handle 字面 vs 语义不匹配，同样
  “RAGFlow 定义 核心概念 架构” 仍能命中 “RAGFlow 系统概述/简介”。

节点返回 ``{"sub_prep_results": [{...}]}``——主图 ``QaState.sub_prep_results``
是 ``Annotated[list[dict], add]`` reducer 字段，N 个 Send 各返回单元素 list，
``operator.add`` 自动合并；barrier 1 节点再按 sub_idx 拆成主图扁平字段
（``sub_queries`` / ``sub_lexical_queries`` / ``sub_lexical_scores`` / ``sub_needs_get_info``）。

设计参考 ``brain_base/nodes/get_info.py::create_preview_score_one``（T16）：
单 async 节点 + asyncio.to_thread 调同步 LLM；不嵌套子图，因为子图作为节点
返回时其 state 字段无法自然回传到主图 reducer 字段。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, TypedDict

from brain_base.agents.schemas import RewrittenQueries
from brain_base.agents.utils.structured import invoke_structured
from brain_base.prompts.qa_prompts import REWRITE_SYSTEM_PROMPT
from brain_base.tools.milvus_client import text_search

logger = logging.getLogger(__name__)

# T30 sparse gate 阈值：text_search top-3 平均分 ≥ 该值 → 认为本地有相关内容
# (needs_get_info=False)；< 该值 → 走外检 (needs_get_info=True)。
# 阈值由 T30 探针实测决定：HIT 真集 top-3 avg ∈ [0.288, 0.337]，
# MISS 真集 top-3 avg ∈ [0.017, 0.124]，gap 中点 ≈ 0.21，取 0.20 预留安全边。
LEXICAL_GATE_THRESHOLD: float = 0.20
LEXICAL_GATE_TOP_K: int = 3


class PrepState(TypedDict, total=False):
    """fanout_prep dispatcher 通过 Send 携带的子状态字段。

    LangGraph Send API 把 dict 当作子调用临时 state；prep_one_subquery
    节点读这些字段，主图聚合时按 sub_idx 索引对齐。

    T24：加 question + sub_questions 上下文字段，让 rewrite LLM 看到用户原始
    问题 + 所有同级子问题——避免同级子问题之间 keywords 撞车（如同时拆出
    "X 启动" / "X 卸载" 时两份都只出主实体词）。
    """
    sub_idx: int
    sub_question: str
    question: str
    sub_questions: list[str]


def _normalize_queries(result: RewrittenQueries, sub_question: str) -> list[dict]:
    """把 LLM 输出转成 list[dict]，强制保留 L0 原句并截断到 6 条。"""
    queries_dict: list[dict] = []
    for q in result.queries:
        if isinstance(q, dict):
            queries_dict.append({
                "text": q.get("text", ""),
                "layer": q.get("layer", "L0"),
            })
        else:
            queries_dict.append({"text": q.text, "layer": q.layer})

    if not any(q.get("text") == sub_question for q in queries_dict):
        queries_dict.insert(0, {"text": sub_question, "layer": "L0"})

    return queries_dict[:6]


def _normalize_lexical_query(result: RewrittenQueries, sub_question: str) -> str:
    """lexical_query 兜底：LLM 给空串时退到 sub_question 自身；截断到 30 字。"""
    raw = (result.lexical_query or "").strip()
    if not raw:
        raw = sub_question.strip()
    return raw[:30]


def _sparse_gate_score(lexical_query: str) -> float:
    """调 milvus ``text_search``拿 top-K 平均分 (T30 sparse gate)。

    返回 top-K 分数平均 (IP 内积，越大越相似)；检索异常时返回 0.0
    (保守降级 → needs_get_info=True 走外检)。保留 try-except 原因：
    milvus 临时不可用 / collection 未建 / sparse 字段缺失 是基础设施级问题，
    不应阻断整个 QA 流程——让 GI 路径接管 (软依赖设计，CLAUDE.md 规则
    14)。同时 logger.warning 打错保证可追踪 (规则 25)。
    """
    if not lexical_query:
        return 0.0
    try:
        hits = text_search(lexical_query, top_k=LEXICAL_GATE_TOP_K)
    except Exception as exc:
        logger.warning(
            "sparse_gate text_search failed: type=%s msg=%.200s lexical_query=%r",
            type(exc).__name__, str(exc), lexical_query[:60],
        )
        return 0.0
    if not hits:
        logger.info(
            "sparse_gate text_search empty hits | lexical_query=%r threshold=%.2f -> avg=0.0",
            lexical_query, LEXICAL_GATE_THRESHOLD,
        )
        return 0.0
    scores = [float(h.get("score", 0.0) or 0.0) for h in hits[:LEXICAL_GATE_TOP_K]]
    if not scores:
        logger.info(
            "sparse_gate text_search no scores | lexical_query=%r hits_count=%d -> avg=0.0",
            lexical_query, len(hits),
        )
        return 0.0
    avg = sum(scores) / len(scores)
    # 记录 top-3 各 score + 阈值判定，方便排障“为何这题走/不走外检”
    top_doc_ids = [h.get("doc_id") or h.get("chunk_id") for h in hits[: LEXICAL_GATE_TOP_K]]
    logger.info(
        "sparse_gate score | lexical_query=%r top%d_scores=%s avg=%.4f threshold=%.2f -> %s | top_doc_ids=%s",
        lexical_query, LEXICAL_GATE_TOP_K,
        [round(s, 4) for s in scores], avg, LEXICAL_GATE_THRESHOLD,
        "PASS (不外检)" if avg >= LEXICAL_GATE_THRESHOLD else "FAIL (走外检)",
        top_doc_ids,
    )
    return avg


def create_prep_one_subquery(llm: Any = None) -> Callable:
    """fanout_prep 实例节点工厂（async）。

    每个 Send 实例独立调用一次 LLM，同步 invoke_structured 通过
    ``asyncio.to_thread`` 扔到线程池——多 Send 实例真并行。

    返回值统一塞进 ``sub_prep_results`` 单元素 list，主图 reducer 按
    ``operator.add`` 自动合并 N 份成一个 list。
    """

    async def prep_one_subquery(sub_state: PrepState) -> dict[str, Any]:
        sub_idx = sub_state.get("sub_idx", 0)
        sub_question = sub_state.get("sub_question", "") or ""
        # T24：上下文继承——question 默认 fallback 到 sub_question，sub_questions
        # 默认 fallback 到 [sub_question]，兼容 dispatcher 未塞新字段的历史调用
        question = sub_state.get("question", "") or sub_question
        sub_questions_list = sub_state.get("sub_questions", []) or [sub_question]

        # T24：多跳模式拼进 原问题 + 同级子问题列表；单跳保持原简洁格式
        if len(sub_questions_list) > 1:
            siblings = "\n".join(
                f"  {i + 1}. {sq}" + ("  ← 当前任务" if i == sub_idx else "")
                for i, sq in enumerate(sub_questions_list)
            )
            user_prompt = (
                f"用户原始问题：{question}\n\n"
                f"该问题被拆成 {len(sub_questions_list)} 个子问题，本次为第 {sub_idx + 1} 个改写：\n"
                f"{siblings}\n\n"
                f"当前要改写的子问题：{sub_question}"
            )
        else:
            user_prompt = f"用户问题：{sub_question}"

        # Step 1：rewrite（同步 LLM 扔到线程池，多实例并行）
        # T27 fail-fast：不再传 fallback；LLM 异常直接抛到 LangGraph runtime。
        result: RewrittenQueries = await asyncio.to_thread(
            invoke_structured,
            llm,
            RewrittenQueries,
            REWRITE_SYSTEM_PROMPT,
            user_prompt,
        )
        queries = _normalize_queries(result, sub_question)
        lexical_query = _normalize_lexical_query(result, sub_question)

        # Step 2：sparse gate (milvus text_search top-3 平均)
        # 异常时 score=0.0 → needs_get_info=True 保守降级 (详见 _sparse_gate_score)
        score = await asyncio.to_thread(_sparse_gate_score, lexical_query)
        needs_get_info = score < LEXICAL_GATE_THRESHOLD

        # 子问题级汇总 log：一行看完“谁谁”走不走外检
        logger.info(
            "prep_one_subquery done | sub_idx=%d sub_question=%r lexical_query=%r "
            "queries_count=%d lexical_score=%.4f threshold=%.2f needs_get_info=%s",
            sub_idx, sub_question, lexical_query,
            len(queries), score, LEXICAL_GATE_THRESHOLD, needs_get_info,
        )

        return {
            "sub_prep_results": [{
                "sub_idx": sub_idx,
                "sub_question": sub_question,
                "queries": queries,
                "lexical_query": lexical_query,
                "lexical_score": score,
                "needs_get_info": needs_get_info,
            }]
        }

    return prep_one_subquery


def fanout_prep_dispatcher(state: dict[str, Any]) -> Any:
    """decompose 后的 conditional edge：N 个子问题 → N 个 Send。

    返回 list[Send] 让 LangGraph 并行触发 N 个 ``subquery_prep`` 实例。
    sub_questions 为空时（异常状态）返回 ``"barrier1"`` 字符串，跳过
    fan-out 直接进 barrier，避免主图卡住（参考 T16 审计陷阱 D）。

    T24：Send payload 加携 question + sub_questions，让 prep_one_subquery 能
    在 user_prompt 里包含原问题 + 同级子问题列表（上下文骨架）。
    """
    from langgraph.types import Send  # 局部 import 避免顶层强依赖

    sub_questions = state.get("sub_questions", []) or []
    if not sub_questions:
        return "barrier1"

    question = state.get("question", "") or ""
    return [
        Send("subquery_prep", {
            "sub_idx": i,
            "sub_question": sq,
            "question": question,
            "sub_questions": sub_questions,
        })
        for i, sq in enumerate(sub_questions)
    ]


def barrier1_node(state: dict[str, Any]) -> dict[str, Any]:
    """barrier 1：把 reducer 收到的 sub_prep_results 拆成主图扁平字段。

    输入字段（reducer 自动 add 后）：
        sub_prep_results: list[{sub_idx, sub_question, queries, lexical_query,
                               lexical_score, needs_get_info}]

    输出字段（按 sub_idx 排序后转 list）：
        sub_queries: list[list[{text, layer}]]
        sub_lexical_queries: list[str]
        sub_lexical_scores: list[float]
        sub_needs_get_info: list[bool]
        gi_trigger_reasons: list[str]  （T38 新增）
        gi_decisions: list[dict]       （T39 新增）

    T30：原 sub_grep_keywords / sub_grep_hits 重命名为 sub_lexical_queries /
    sub_lexical_scores，sparse gate top-3 平均分取代原 grep AND hits 计数。
    """
    results = list(state.get("sub_prep_results", []) or [])
    results.sort(key=lambda r: r.get("sub_idx", 0))

    time_sensitive = state.get("time_sensitive", False)

    # barrier1 汇总 log：一行看完 N 子问题的 lexical_score + gate 决策
    any_sparse_miss = False
    if results:
        summary = " | ".join(
            f"#{r.get('sub_idx', '?')}:'{(r.get('sub_question') or '')[:20]}' "
            f"lex={r.get('lexical_query', '')!r} "
            f"score={r.get('lexical_score', 0.0):.4f} "
            f"GI={r.get('needs_get_info', False)}"
            for r in results
        )
        any_sparse_miss = any(r.get("needs_get_info", False) for r in results)
        logger.info(
            "barrier1 aggregate | n=%d any_needs_get_info=%s time_sensitive=%s | %s",
            len(results), any_sparse_miss, time_sensitive, summary,
        )

    # T38：gi_trigger_reasons（聚合级触发原因）
    gi_trigger_reasons: list[str] = []
    if time_sensitive:
        gi_trigger_reasons.append("time_sensitive")
    if any_sparse_miss:
        gi_trigger_reasons.append("sparse_miss")
    if not gi_trigger_reasons:
        gi_trigger_reasons.append("none")

    # T39：gi_decisions（per sub-question 结构化决策）
    gi_decisions: list[dict] = []
    for r in results:
        sub_triggered = bool(r.get("needs_get_info", False))
        reason = "sparse_miss" if sub_triggered else "none"
        # T38：time_sensitive 全局覆盖——即使 sparse gate PASS 也标记 triggered
        if time_sensitive and not sub_triggered:
            sub_triggered = True
            reason = "time_sensitive"
        gi_decisions.append({
            "sub_idx": r.get("sub_idx", 0),
            "sub_question": r.get("sub_question", ""),
            "triggered": sub_triggered,
            "reason": reason,
            "sparse_score": float(r.get("lexical_score", 0.0) or 0.0),
            "threshold": LEXICAL_GATE_THRESHOLD,
        })

    return {
        "sub_queries": [r.get("queries", []) for r in results],
        "sub_lexical_queries": [str(r.get("lexical_query", "") or "") for r in results],
        "sub_lexical_scores": [float(r.get("lexical_score", 0.0) or 0.0) for r in results],
        "sub_needs_get_info": [bool(r.get("needs_get_info", False)) for r in results],
        "gi_trigger_reasons": gi_trigger_reasons,
        "gi_decisions": gi_decisions,
    }
