"""QA 第二段 fan-out 节点：subquery_search_one（每子问题独立 milvus + rerank）。

T28 PIPE2 重构。每个子问题独立实例化跑一次（通过 ``Send("subquery_search_one",
SearchState)`` × N 派发，N 个实例 LangGraph 自动并行）：

    multi_query_search(queries=该子问题的改写, use_rerank=True)
        → milvus hybrid (dense + sparse + RRF) → bge-reranker-v2-m3 重排 → top-K
    返回 ``{"sub_evidence": [{sub_idx, sub_question, chunks, error?}]}``

节点返回 ``{"sub_evidence": [{...}]}``——主图 ``QaState.sub_evidence`` 是
``Annotated[list[dict], add]`` reducer 字段，N 个 Send 各返回单元素 list，
``operator.add`` 自动合并；barrier2 节点再 flatten + 加 sub_idx / sub_question
标签写入主图 ``evidence`` 字段。

设计参考：
- T23 ``brain_base/nodes/qa_prep.py``（fanout_prep + barrier1，第一段子图模式）
- T25 ``brain_base/nodes/qa_get_info.py::create_fetch_extract_one``（async + Semaphore）
- ToDo T28 描述（CLAUDE.md 主流程图设计图最后一处空白的兑现）

关键约束：
- **每子问题独立 top-K**：避免 legacy_dense_search 全局 top-K 让强子问题霸榜
- **rerank 软依赖**：``multi_query_search`` 内部已封装（reranker=None 静默回退到 RRF top-K，
  见 ``bin/milvus-cli.py:rerank``），本节点不直接处理软依赖
- **fan-out 单 Send 失败隔离**（CLAUDE.md 规则 25 允许的设计需要）：milvus 抛错单 Send
  返回 ``{"sub_evidence": [{error: ...}]}`` + ``logger.warning`` 不阻断其他 Send
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, TypedDict

from brain_base.config import GetInfoConfig
from brain_base.tools.milvus_client import multi_query_search

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Send 子状态
# ---------------------------------------------------------------------------


class SearchState(TypedDict, total=False):
    """fanout_search_dispatcher 通过 Send 派发的子状态字段。

    每个 Send 实例的字段是当前子问题的 rewrite + grep 结果（来自 barrier1 输出
    的扁平字段 ``sub_queries`` / ``sub_questions`` 按 sub_idx 索引）。

    subquery_search_one 节点读这些字段；结果通过 reducer add 合并到主图
    ``sub_evidence`` 字段。
    """

    sub_idx: int
    sub_question: str
    queries: list[dict]    # [{text: str, layer: "L0"|"L1"|"L2"|"L3"}]


# ---------------------------------------------------------------------------
# Semaphore 模块级 lazy-create（与 GetInfoConfig.search_concurrency 对齐）
# ---------------------------------------------------------------------------


_search_semaphore: asyncio.Semaphore | None = None
_search_semaphore_size: int | None = None
_search_semaphore_loop_id: int | None = None


def _get_search_semaphore(size: int) -> asyncio.Semaphore:
    """惰性创建 / 重建 Semaphore；当 cached size 与 cfg 不一致或 loop 切换时重建。

    参考 ``qa_get_info._get_semaphore`` 的同款模式（loop id 检查防止
    多次 ``asyncio.run()`` 复用旧 loop sem 报 ``bound to different event loop``）。
    """
    global _search_semaphore, _search_semaphore_size, _search_semaphore_loop_id
    try:
        current_loop_id: int | None = id(asyncio.get_running_loop())
    except RuntimeError:
        current_loop_id = None
    if (_search_semaphore is None
            or _search_semaphore_size != size
            or _search_semaphore_loop_id != current_loop_id):
        _search_semaphore = asyncio.Semaphore(size)
        _search_semaphore_size = size
        _search_semaphore_loop_id = current_loop_id
    return _search_semaphore


# ---------------------------------------------------------------------------
# 条件边：fanout_search_dispatcher
# ---------------------------------------------------------------------------


def fanout_search_dispatcher(state: dict[str, Any]) -> Any:
    """ingest 后的 conditional edge：N 个子问题 → N 个 Send。

    返回 list[Send] 让 LangGraph 并行触发 N 个 ``subquery_search_one`` 实例。
    sub_queries 整体空时（异常状态）返回 ``"barrier2"`` 字符串短路，让 barrier2
    聚合空 sub_evidence 输出空 evidence，由 answer 节点的"未找到本地证据"分支处理。

    sub_questions 与 sub_queries 长度不一致时（理论不应发生，barrier1 已对齐）按
    短的派发，防御性兜底。
    """
    from langgraph.types import Send  # 局部 import 避免顶层强依赖 langgraph 子模块

    sub_queries: list[list[dict]] = state.get("sub_queries", []) or []
    sub_questions: list[str] = state.get("sub_questions", []) or []

    # gate：sub_queries 整体空（含每条都为空）→ 短路 barrier2
    if not sub_queries or not any(sub_queries):
        return "barrier2"

    # 防御：sub_questions 缺失（异常状态，barrier1 应已对齐）→ 短路避免发空 sub_question 的 Send
    if not sub_questions:
        return "barrier2"

    # 防御：长度不一致按短的派发
    n = min(len(sub_queries), len(sub_questions))
    if n == 0:
        return "barrier2"

    return [
        Send(
            "subquery_search_one",
            {
                "sub_idx": i,
                "sub_question": sub_questions[i] if i < len(sub_questions) else "",
                "queries": sub_queries[i] or [],
            },
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# 节点工厂：create_subquery_search_one
# ---------------------------------------------------------------------------


def create_subquery_search_one(config: GetInfoConfig | None = None) -> Callable:
    """subquery_search_one async 节点工厂。

    每个 Send 实例独立 acquire 模块级 Semaphore（默认并发 3）；同步
    ``multi_query_search`` 通过 ``asyncio.to_thread`` 扔到线程池——多 Send 实例真并行。

    返回值统一塞进 ``sub_evidence`` 单元素 list，主图 reducer 按 ``operator.add``
    自动合并 N 份成一个 list。

    fan-out 单 Send 失败隔离（CLAUDE.md 规则 25）：milvus 抛错单 Send 返回
    ``{"sub_evidence": [{sub_idx, sub_question, chunks: [], error: ...}]}``，
    ``logger.warning`` 打错误信息（含 sub_idx / sub_question / 异常类型）。
    """
    cfg = config or GetInfoConfig()

    async def subquery_search_one(sub_state: SearchState) -> dict[str, Any]:
        sub_idx = int(sub_state.get("sub_idx", 0))
        sub_question = sub_state.get("sub_question", "") or ""
        queries: list[dict] = sub_state.get("queries", []) or []

        sem = _get_search_semaphore(cfg.search_concurrency)
        async with sem:
            try:
                # 提取 query 文本（从 RewrittenQueries.queries 的 [{text, layer}] 结构）
                texts: list[str] = [
                    (q.get("text", "") if isinstance(q, dict) else getattr(q, "text", ""))
                    for q in queries
                ]
                texts = [t for t in texts if t and t.strip()]

                if not texts:
                    # 防御：当前子问题没有可用改写 → 返回空 chunks（不算错误）
                    logger.info(
                        "subquery_search_one: empty queries, skipped. sub_idx=%s sub_question=%s",
                        sub_idx, sub_question,
                    )
                    return {
                        "sub_evidence": [
                            {
                                "sub_idx": sub_idx,
                                "sub_question": sub_question,
                                "chunks": [],
                            }
                        ]
                    }

                # 同步 multi_query_search 扔到线程池（每子问题 ≤6 改写）
                result = await asyncio.to_thread(
                    multi_query_search,
                    queries=texts[:6],
                    top_k_per_query=20,
                    final_k=10,
                    rrf_k=60,
                    use_rerank=True,
                )
                chunks = result.get("results", []) or []

                return {
                    "sub_evidence": [
                        {
                            "sub_idx": sub_idx,
                            "sub_question": sub_question,
                            "chunks": chunks,
                        }
                    ]
                }
            except Exception as e:
                # fan-out 单 Send 失败隔离（CLAUDE.md 规则 25 允许）。
                # 规则 25 补丁：必须 logger 打错误信息含关键上下文。
                logger.warning(
                    "subquery_search_one fan-out fail: sub_idx=%s sub_question=%s exc=%s: %s",
                    sub_idx, sub_question, type(e).__name__, str(e)[:200],
                )
                return {
                    "sub_evidence": [
                        {
                            "sub_idx": sub_idx,
                            "sub_question": sub_question,
                            "chunks": [],
                            "error": str(e)[:200],
                        }
                    ]
                }

    return subquery_search_one


# ---------------------------------------------------------------------------
# 节点：barrier2_node（sync, fan-in 聚合）
# ---------------------------------------------------------------------------


def barrier2_node(state: dict[str, Any]) -> dict[str, Any]:
    """fan-in：聚合 ``sub_evidence`` × N → 主图 ``evidence`` + ``search_errors``。

    入字段（reducer 自动 add 累加）：
        sub_evidence: list[{sub_idx, sub_question, chunks: list[dict], error?: str}]

    出字段：
        evidence: list[dict]（flatten + 加 sub_idx / sub_question / source / match_type 标签）
        search_errors: list[str]（聚合各子 Send 的 error 信息）

    排序语义：按 sub_idx 升序展开，让 evidence 列表里"子问题 0 的全部 chunks → 子问题 1
    的全部 chunks → ..."顺序确定，便于 answer 节点按子问题分组渲染。
    """
    sub_evidence: list[dict] = list(state.get("sub_evidence", []) or [])
    sub_evidence.sort(key=lambda x: int(x.get("sub_idx", 0)))

    flat_evidence: list[dict] = []
    errors: list[str] = []

    for se in sub_evidence:
        sub_idx = int(se.get("sub_idx", 0))
        sub_question = se.get("sub_question", "") or ""
        chunks = se.get("chunks", []) or []
        err = se.get("error", "") or ""

        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            flat_evidence.append(
                {
                    "source": "milvus",
                    "match_type": "vector",
                    "sub_idx": sub_idx,
                    "sub_question": sub_question,
                    **chunk,
                }
            )

        if err:
            errors.append(f"sub_{sub_idx}({sub_question}): {err}")

    return {
        "evidence": flat_evidence,
        "search_errors": errors,
    }
