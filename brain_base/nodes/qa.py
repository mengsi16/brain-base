"""
QA 主图节点函数。

流程：probe → crystallized_check → normalize → decompose → rewrite →
       search → judge → answer → self_check → crystallize_answer

设计原则：
- 纯逻辑节点（probe / crystallized_check / search / crystallize_answer）：模块级函数。
- LLM 节点：`create_xxx_node(llm)` 工厂，内部用 `invoke_structured(...)` 拿到
  Pydantic schema 实例，避免在 prompt 里塞 JSON 格式段。
- llm=None 时走降级分支（启发式规则），不阻断流程（CLAUDE.md 硬约束 14）。
"""

from __future__ import annotations

from pathlib import Path as _P
from typing import Any, Callable

import time

from brain_base.agents.schemas import (
    DecomposedQuestion,
    EvidenceJudgment,
    GetInfoTrigger,
    NormalizedQuestion,
    RewrittenQueries,
    SelfCheckResult,
)
from brain_base.agents.utils.structured import invoke_structured
from brain_base.config import GetInfoConfig
from brain_base.nodes._probe import probe_milvus, probe_playwright
from brain_base.prompts.qa_prompts import (
    ANSWER_SYSTEM_PROMPT,
    ANSWER_USER_PROMPT_TEMPLATE,
    DECOMPOSE_SYSTEM_PROMPT,
    GET_INFO_TRIGGER_SYSTEM_PROMPT,
    JUDGE_EVIDENCE_SYSTEM_PROMPT,
    NORMALIZE_SYSTEM_PROMPT,
    REWRITE_SYSTEM_PROMPT,
    SELF_CHECK_SYSTEM_PROMPT,
)
from brain_base.tools.milvus_client import list_docs, multi_query_search

# ---------------------------------------------------------------------------
# evidence 渲染辅助
# ---------------------------------------------------------------------------


def _evidence_body(e: dict[str, Any], max_chars: int = 800) -> str:
    """从一条 evidence dict 取最有用的正文片段。

    优先级：``chunk_text``（Milvus 实际正文）→ ``summary``（短摘要）→ ``path``。
    历史数据里 summary 偶有字面量 ``'""'``（两个引号字符）这种脏值——frontmatter
    解析器没去引号导致；这里用 ``strip(' \\"')`` 顺手清掉空字面量，返回 chunk_text。
    """
    chunk_text = (e.get("chunk_text") or "").strip()
    if chunk_text:
        return chunk_text[:max_chars]
    summary = (e.get("summary") or "").strip().strip('"').strip()
    if summary:
        return summary[:max_chars]
    return (e.get("path") or "")[:max_chars]


# ---------------------------------------------------------------------------
# 纯逻辑节点（无 LLM）
# ---------------------------------------------------------------------------


def probe_node(state: dict[str, Any]) -> dict[str, Any]:
    """基础设施快速探测（非阻断）。"""
    milvus = probe_milvus()
    playwright = probe_playwright()
    return {
        "infra_status": {
            "milvus_available": bool(milvus.get("available")),
            "playwright_available": bool(playwright.get("available")),
            "crystallized_available": _P("data/crystallized/index.json").is_file(),
        }
    }


def crystallized_check_node(state: dict[str, Any]) -> dict[str, Any]:
    """固化层命中判断。"""
    from brain_base.graphs.crystallize_graph import CrystallizeGraph

    question = state.get("question", "")
    infra = state.get("infra_status", {})

    if not infra.get("crystallized_available", False):
        return {"crystallized_status": "degraded"}

    cg = CrystallizeGraph()
    result = cg.hit_check(user_question=question)

    status = result.get("status", "miss")
    ret: dict[str, Any] = {"crystallized_status": status}

    if status in ("hit_fresh", "cold_promoted"):
        ret["crystallized_answer"] = result.get("answer_markdown", "")
        ret["skill_id"] = result.get("skill_id", "")
    elif status == "cold_observed":
        ret["cold_evidence_summary"] = result.get("cold_evidence_summary", "")
    elif status == "hit_stale":
        ret["skill_id"] = result.get("skill_id", "")

    return ret


def search_node(state: dict[str, Any]) -> dict[str, Any]:
    """本地证据检索：文件系统 grep + Milvus 向量。"""
    queries = state.get("rewritten_queries", [])
    infra = state.get("infra_status", {})
    evidence: list[dict] = []

    question = state.get("question", "")
    chunks_dir = _P("data/docs/chunks")
    if chunks_dir.is_dir():
        for chunk_file in chunks_dir.glob("*.md"):
            text = chunk_file.read_text(encoding="utf-8", errors="ignore")
            if question.lower() in text.lower():
                evidence.append({
                    "source": "filesystem",
                    "path": str(chunk_file),
                    "match_type": "grep",
                })

    if infra.get("milvus_available", False) and queries:
        try:
            result = multi_query_search(
                queries=queries,
                top_k_per_query=20,
                final_k=10,
                rrf_k=60,
                use_rerank=True,
            )
            for hit in result.get("results", []):
                evidence.append({"source": "milvus", "match_type": "vector", **hit})
        except Exception:
            # Milvus 可达但 collection 缺失 / 查询失败 → 静默降级，不阻断流程
            pass

    return {"evidence": evidence}


def crystallize_answer_node(state: dict[str, Any]) -> dict[str, Any]:
    """委托固化层写入答案。"""
    from brain_base.graphs.crystallize_graph import CrystallizeGraph

    crystallized_status = state.get("crystallized_status", "miss")
    if crystallized_status == "degraded":
        return {}

    answer = state.get("answer", "")
    question = state.get("question", "")

    if not answer:
        return {}

    result = CrystallizeGraph.crystallize(
        user_question=question,
        answer_markdown=answer,
        value_score=0.5,
        trigger_keywords=[question[:20]],
        description=question[:80],
    )
    return {"crystallize_result": result}


# ---------------------------------------------------------------------------
# LLM 节点工厂（schema 强制结构化输出）
# ---------------------------------------------------------------------------


def create_normalize_node(llm: Any = None) -> Callable:
    """规范化用户问题节点工厂。"""

    def normalize_node(state: dict[str, Any]) -> dict[str, Any]:
        question = state.get("question", "")

        if llm is None:
            return {"normalized_query": question}

        try:
            result = invoke_structured(
                llm,
                NormalizedQuestion,
                NORMALIZE_SYSTEM_PROMPT,
                f"用户问题：{question}",
            )
        except Exception:
            return {"normalized_query": question}

        return {
            "normalized_query": result.normalized,
            "expected_type": result.expected_type,
            "time_sensitive": result.time_sensitive,
            "language": result.language,
        }

    return normalize_node


def create_decompose_node(llm: Any = None) -> Callable:
    """复杂问题分解节点工厂。"""

    def decompose_node(state: dict[str, Any]) -> dict[str, Any]:
        question = state.get("normalized_query", state.get("question", ""))

        if llm is None:
            return {"sub_queries": [], "decomposition_needed": False}

        try:
            result = invoke_structured(
                llm,
                DecomposedQuestion,
                DECOMPOSE_SYSTEM_PROMPT,
                f"用户问题：{question}",
            )
        except Exception:
            return {"sub_queries": [], "decomposition_needed": False}

        return {
            "sub_queries": [sq.text for sq in result.sub_questions],
            "decomposition_needed": result.needs_decompose,
        }

    return decompose_node


def create_rewrite_node(llm: Any = None) -> Callable:
    """Query 改写节点工厂（L0–L3 fan-out）。"""

    def rewrite_node(state: dict[str, Any]) -> dict[str, Any]:
        question = state.get("normalized_query", state.get("question", ""))

        if llm is None:
            return {"rewritten_queries": [question]}

        try:
            result = invoke_structured(
                llm,
                RewrittenQueries,
                REWRITE_SYSTEM_PROMPT,
                f"用户问题：{question}",
            )
        except Exception:
            return {"rewritten_queries": [question]}

        queries = [q.text for q in result.queries if q.text]
        if question and question not in queries:
            queries.insert(0, question)
        return {"rewritten_queries": queries[:6]}

    return rewrite_node


def create_judge_node(llm: Any = None) -> Callable:
    """证据充分性判断节点工厂。"""

    def judge_node(state: dict[str, Any]) -> dict[str, Any]:
        evidence = state.get("evidence", [])
        question = state.get("question", "")

        if llm is None:
            return {"evidence_sufficient": len(evidence) > 0}

        evidence_summary = "\n".join(
            f"- [{e.get('source', '?')}] {_evidence_body(e, max_chars=200)}"
            for e in evidence[:10]
        )
        try:
            result = invoke_structured(
                llm,
                EvidenceJudgment,
                JUDGE_EVIDENCE_SYSTEM_PROMPT,
                f"用户问题：{question}\n\n证据列表：\n{evidence_summary}",
            )
        except Exception:
            return {"evidence_sufficient": len(evidence) > 0}

        return {
            "evidence_sufficient": result.sufficient,
            "evidence_recommendation": result.recommendation,
            "coverage_score": result.coverage,
            "judge_reason": result.reason,
        }

    return judge_node


def create_answer_node(llm: Any = None) -> Callable:
    """基于证据生成答案节点工厂。

    answer 节点是自由文本输出（含 markdown 格式与证据表），不走
    `with_structured_output`——结构由 prompt 模板与渲染规则保证。
    """

    def answer_node(state: dict[str, Any]) -> dict[str, Any]:
        crystallized_status = state.get("crystallized_status", "miss")
        if crystallized_status in ("hit_fresh", "cold_promoted"):
            return {"answer": state.get("crystallized_answer", "")}

        evidence = state.get("evidence", [])
        question = state.get("question", "")

        if not evidence:
            return {
                "answer": f"未能找到关于「{question}」的本地证据。",
                "evidence_sufficient": False,
            }

        evidence_text = "\n".join(
            f"[{i+1}] {e.get('source', '?')} | {_evidence_body(e, max_chars=800)}"
            for i, e in enumerate(evidence[:10])
        )

        if llm is None:
            return {"answer": f"基于本地证据回答「{question}」：\n\n{evidence_text}"}

        from langchain_core.messages import HumanMessage, SystemMessage

        try:
            response = llm.invoke([
                SystemMessage(content=ANSWER_SYSTEM_PROMPT),
                HumanMessage(content=ANSWER_USER_PROMPT_TEMPLATE.format(
                    question=question,
                    evidence=evidence_text,
                )),
            ])
            content = response.content if hasattr(response, "content") else str(response)
            # MiniMax / Anthropic 兼容端点的 content 可能是 ContentBlock 数组：
            # [{"type": "thinking", ...}, {"type": "text", "text": "..."}]
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text" and block.get("text"):
                            parts.append(block["text"])
                    elif isinstance(block, str):
                        parts.append(block)
                answer = "\n".join(parts) if parts else str(content)
            else:
                answer = content
            return {"answer": answer}
        except Exception as exc:
            return {"answer": f"LLM 生成失败（{exc}），证据：\n{evidence_text}"}

    return answer_node


def create_self_check_node(llm: Any = None) -> Callable:
    """答案自检节点工厂（Maker-Checker）。

    - 降级模式跳过自检（CLAUDE.md 规则 35）。
    - 自检只能删除或标注，不能凭空添加（CLAUDE.md 规则 34）。
    """

    def self_check_node(state: dict[str, Any]) -> dict[str, Any]:
        answer = state.get("answer", "")
        question = state.get("question", "")
        evidence = state.get("evidence", [])
        crystallized_status = state.get("crystallized_status", "miss")

        if crystallized_status in ("hit_fresh", "cold_promoted", "degraded"):
            return {"self_check_passed": True, "self_check_skipped": True}

        if not answer or llm is None:
            return {"self_check_passed": True, "self_check_skipped": True}

        evidence_text = "\n".join(
            f"[{i+1}] {_evidence_body(e, max_chars=400)}"
            for i, e in enumerate(evidence[:10])
        )
        user_prompt = (
            f"用户问题：{question}\n\n"
            f"已生成答案：\n{answer}\n\n"
            f"可用证据：\n{evidence_text}"
        )
        try:
            result = invoke_structured(
                llm,
                SelfCheckResult,
                SELF_CHECK_SYSTEM_PROMPT,
                user_prompt,
            )
        except Exception:
            return {"self_check_passed": True, "self_check_skipped": True}

        passed = (
            result.faithfulness == "pass"
            and result.completeness == "pass"
            and result.consistency == "pass"
        )
        out: dict[str, Any] = {
            "self_check_passed": passed,
            "self_check_skipped": False,
            "self_check_result": result.model_dump(),
        }
        # revised_answer 仅在自检不通过且 LLM 有提供时覆盖（自检只删不增）
        if not passed and result.revised_answer:
            out["answer"] = result.revised_answer
        return out

    return self_check_node


# ---------------------------------------------------------------------------
# T10：自动外检 + 入库回路（5 个新节点）
# ---------------------------------------------------------------------------


def _list_ingested_urls() -> set[str]:
    """读 raw 目录所有 frontmatter 的 url 字段，返回已入库 URL 集合（去重用）。

    Milvus 不可用 / raw 目录不存在时返回空集合，调用方自行决定降级。
    """
    try:
        info = list_docs()
    except Exception:
        return set()
    urls: set[str] = set()
    for entry in info.get("docs", []) or []:
        url = (entry.get("url") or "").strip()
        if url:
            urls.add(url)
    return urls


def create_get_info_trigger_node(
    llm: Any = None,
    config: GetInfoConfig | None = None,
) -> Callable:
    """get_info_trigger 节点工厂：判定是否触发外部补库。

    判定优先级（从硬到软）：
    1. config.enable=False → 强制 needed=False
    2. 已尝试过外检（防死循环）→ needed=False
    3. playwright 不可用 → needed=False（软依赖降级）
    4. evidence_sufficient=True → needed=False（已经够了）
    5. 启发式（llm=None）：evidence=[] && playwright_available → needed=True
    6. LLM `GetInfoTrigger` schema 综合判断
    """
    cfg = config or GetInfoConfig()

    def get_info_trigger_node(state: dict[str, Any]) -> dict[str, Any]:
        question = state.get("question", "")
        evidence = state.get("evidence", []) or []
        infra = state.get("infra_status", {}) or {}
        playwright_ok = bool(infra.get("playwright_available", False))

        # ---- 硬阻断 ----
        if not cfg.enable:
            return {
                "trigger_get_info": False,
                "get_info_reason": "config.enable=False",
                "search_hint": "",
            }
        if state.get("get_info_attempted", False):
            return {
                "trigger_get_info": False,
                "get_info_reason": "已尝试过一轮外检（防死循环）",
                "search_hint": "",
            }
        if not playwright_ok:
            return {
                "trigger_get_info": False,
                "get_info_reason": "playwright 不可用，外检无法执行",
                "search_hint": "",
            }
        if state.get("evidence_sufficient", False):
            return {
                "trigger_get_info": False,
                "get_info_reason": "evidence_sufficient=True",
                "search_hint": "",
            }

        # ---- 启发式（无 LLM）----
        if llm is None:
            needed = len(evidence) == 0
            return {
                "trigger_get_info": needed,
                "get_info_reason": "evidence 为空" if needed else "evidence 非空",
                "search_hint": question,
            }

        # ---- LLM 综合判断 ----
        evidence_summary = "\n".join(
            f"- [{e.get('source', '?')}] {_evidence_body(e, max_chars=120)}"
            for e in evidence[:5]
        ) or "(无)"
        user_prompt = (
            f"用户问题：{question}\n\n"
            f"本地检索到的证据数量：{len(evidence)}\n"
            f"证据预览：\n{evidence_summary}\n\n"
            f"infra_status：{infra}"
        )
        try:
            result = invoke_structured(
                llm,
                GetInfoTrigger,
                GET_INFO_TRIGGER_SYSTEM_PROMPT,
                user_prompt,
            )
        except Exception as exc:
            # LLM 调用失败 → 启发式回退
            return {
                "trigger_get_info": len(evidence) == 0,
                "get_info_reason": f"LLM 失败回退启发式: {exc}",
                "search_hint": question,
            }

        # 拼搜索提示：原问题 + LLM 给的关键词
        keywords = " ".join(result.suggested_keywords[:5])
        hint = f"{question} {keywords}".strip()
        return {
            "trigger_get_info": result.needed,
            "get_info_reason": result.reason or "(LLM 未给理由)",
            "search_hint": hint,
        }

    return get_info_trigger_node


def create_web_research_node(
    llm: Any = None,
    config: GetInfoConfig | None = None,
) -> Callable:
    """web_research 节点工厂：调 GetInfoGraph 拿候选 URL 列表。

    始终标记 get_info_attempted=True，无论成功失败（防死循环）。
    """
    cfg = config or GetInfoConfig()

    def web_research_node(state: dict[str, Any]) -> dict[str, Any]:
        from brain_base.graphs.get_info_graph import GetInfoGraph

        hint = state.get("search_hint") or state.get("question", "")
        try:
            sub = GetInfoGraph(llm=llm).run(
                user_question=hint,
                max_iterations=cfg.get_info_max_iter,
                target_official_count=cfg.get_info_target_official,
                total_timeout=cfg.get_info_total_timeout,
            )
            candidates = sub.get("candidates", []) or []
        except Exception as exc:
            return {
                "get_info_candidates": [],
                "get_info_attempted": True,
                "ingest_errors": [f"web_research failed: {exc}"],
            }

        return {
            "get_info_candidates": candidates,
            "get_info_attempted": True,
        }

    return web_research_node


def create_select_candidates_node(
    config: GetInfoConfig | None = None,
) -> Callable:
    """select_candidates 节点工厂：按配额筛选 + 去重，输出最终入库目标。

    顺序：
    1. 过滤 discard
    2. 按已入库 URL 去重
    3. official-doc 取前 max_official 个
    4. community 取前 max_community 个
    5. 合并后取前 max_total 个（official 优先）
    """
    cfg = config or GetInfoConfig()

    def select_candidates_node(state: dict[str, Any]) -> dict[str, Any]:
        candidates = state.get("get_info_candidates", []) or []
        ingested = _list_ingested_urls()

        # 1+2 过滤 discard 和已入库
        survived = [
            c for c in candidates
            if c.get("source_type") != "discard"
            and (c.get("url") or "").strip()
            and c.get("url") not in ingested
        ]

        # 3+4 分类配额
        official = [c for c in survived if c.get("source_type") == "official-doc"][:cfg.max_official]
        community = [c for c in survived if c.get("source_type") == "community"][:cfg.max_community]

        # 5 合并 + 总数上限（official 优先）
        targets = (official + community)[:cfg.max_total]

        return {"ingest_targets": targets}

    return select_candidates_node


def create_ingest_candidates_node(
    llm: Any = None,
    config: GetInfoConfig | None = None,
) -> Callable:
    """ingest_candidates 节点工厂：串行调 IngestUrlGraph 入库每个 target。

    单 URL 失败不阻断后续；整批超时后跳过剩余。
    """
    cfg = config or GetInfoConfig()

    def ingest_candidates_node(state: dict[str, Any]) -> dict[str, Any]:
        from brain_base.graphs.ingest_url_graph import IngestUrlGraph

        targets = state.get("ingest_targets", []) or []
        ingested: list[str] = []
        errors: list[str] = list(state.get("ingest_errors", []) or [])

        if not targets:
            return {"get_info_ingested": ingested, "ingest_errors": errors}

        graph = IngestUrlGraph(llm=llm)
        batch_started = time.time()

        for tgt in targets:
            url = tgt.get("url", "")
            if not url:
                continue
            if time.time() - batch_started > cfg.batch_timeout:
                errors.append(f"batch_timeout 超时，跳过剩余 {len(targets) - len(ingested) - len(errors)} 个候选")
                break
            try:
                result = graph.run(
                    url=url,
                    source_type=tgt.get("source_type", "community"),
                    topic=state.get("question", "")[:40] or "untitled",
                    title_hint=tgt.get("title_hint", ""),
                )
                doc_id = result.get("doc_id", "")
                if doc_id:
                    ingested.append(doc_id)
                else:
                    errors.append(f"{url}: 入库无 doc_id（completeness={result.get('completeness_status', '?')}）")
            except Exception as exc:
                errors.append(f"{url}: {str(exc)[:200]}")

        return {"get_info_ingested": ingested, "ingest_errors": errors}

    return ingest_candidates_node


def re_search_node(state: dict[str, Any]) -> dict[str, Any]:
    """re_search 节点：在新内容入库后重检索 Milvus，覆盖原 evidence。

    复用 search_node 的 multi_query_search 逻辑，但只走 Milvus（已经 grep 过一次），
    且 evidence 直接覆盖（旧 evidence 已被 judge 判为不足）。
    """
    queries = state.get("rewritten_queries", []) or []
    infra = state.get("infra_status", {}) or {}

    if not infra.get("milvus_available", False) or not queries:
        return {"evidence": state.get("evidence", []) or []}

    try:
        result = multi_query_search(
            queries=queries,
            top_k_per_query=20,
            final_k=10,
            rrf_k=60,
            use_rerank=True,
        )
    except Exception:
        return {"evidence": state.get("evidence", []) or []}

    evidence = [
        {"source": "milvus", "match_type": "vector", **hit}
        for hit in result.get("results", []) or []
    ]
    return {"evidence": evidence}


# ---------------------------------------------------------------------------
# 向后兼容：无 LLM 版本（图未注入 LLM 时仍可工作）
# ---------------------------------------------------------------------------


normalize_node = create_normalize_node(llm=None)
decompose_node = create_decompose_node(llm=None)
rewrite_node = create_rewrite_node(llm=None)
judge_node = create_judge_node(llm=None)
answer_node = create_answer_node(llm=None)
self_check_node = create_self_check_node(llm=None)
get_info_trigger_node = create_get_info_trigger_node(llm=None)
web_research_node = create_web_research_node(llm=None)
select_candidates_node = create_select_candidates_node()
ingest_candidates_node = create_ingest_candidates_node(llm=None)
