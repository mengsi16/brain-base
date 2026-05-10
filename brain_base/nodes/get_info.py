"""
GetInfo 子图节点函数：plan-search-classify-loop。

设计：
- plan_next_query（LLM）：根据已尝试查询和现有候选给出下一个 query 与 mode。
- search_web（Python）：调 `tools.web_fetcher.search_*` 拿候选 URL。
- classify_results（LLM）：把候选分类为 official-doc / community / discard。
- check_continue（Python）：根据轮次 / 总超时 / 已找到的 official-doc 数量决定继续或终止。

终止条件（纯代码）：
- iteration >= max_iterations。
- time.time() - started_at > total_timeout。
- 已找到 >= target_official_count 篇 official-doc。
"""

from __future__ import annotations

import time
from typing import Any, Callable

import asyncio

from brain_base.agents.schemas import (
    CandidateScore,
    NextQueryPlan,
    UrlClassification,
    UrlClassificationBatch,
)
from brain_base.agents.utils.structured import invoke_structured
from brain_base.prompts.get_info_prompts import (
    CLASSIFY_URL_SYSTEM_PROMPT,
    PLAN_NEXT_QUERY_SYSTEM_PROMPT,
    SCORE_CANDIDATE_SYSTEM_PROMPT,
)
from brain_base.tools.web_fetcher import _with_shutdown as _pw_with_shutdown
from brain_base.tools.web_fetcher import search_bing as _search_bing_async
from brain_base.tools.web_fetcher import search_google as _search_google_async
from brain_base.tools.web_fetcher_async import fetch_preview


# T29: web_fetcher 已迁移为 async API。本模块（GetInfoGraph 老路径，QaGraph 主线已不
# 调用，T25 后改走 qa_get_info.py）的 search_web_node 是 sync 函数，对 async coroutine
# 走 ``asyncio.run(_with_shutdown(...))`` 包装：兼容 sync + 主协程 finally 主动关
# playwright，避免 Windows ProactorEventLoop GC 触发 pipe 已关的满屏噪音。
def search_google(query: str, num_results: int = 10, page: int = 1) -> list[dict[str, Any]]:
    """sync 兼容包装：``asyncio.run(_with_shutdown(_search_google_async(...)))``。"""
    return asyncio.run(_pw_with_shutdown(
        _search_google_async(query, num_results=num_results, page=page)
    ))


def search_bing(query: str, num_results: int = 10, page: int = 1) -> list[dict[str, Any]]:
    """sync 兼容包装：``asyncio.run(_with_shutdown(_search_bing_async(...)))``。"""
    return asyncio.run(_pw_with_shutdown(
        _search_bing_async(query, num_results=num_results, page=page)
    ))


def init_state_node(state: dict[str, Any]) -> dict[str, Any]:
    """初始化循环状态。"""
    return {
        "iteration": state.get("iteration", 0),
        "queries_tried": state.get("queries_tried", []),
        "candidates": state.get("candidates", []),
        "max_iterations": state.get("max_iterations", 5),
        "target_official_count": state.get("target_official_count", 3),
        "per_iteration_timeout": state.get("per_iteration_timeout", 20.0),
        "total_timeout": state.get("total_timeout", 90.0),
        "started_at": state.get("started_at") or time.time(),
        "degraded": False,
        "degraded_reason": None,
    }


def create_plan_node(llm: Any = None) -> Callable:
    """规划下一轮搜索查询。

    llm=None：把用户问题直接当查询返回（仅一次有效，第二轮起返回空跳过循环）。
    """

    def plan_node(state: dict[str, Any]) -> dict[str, Any]:
        user_question = state.get("user_question", "")
        queries_tried = state.get("queries_tried", [])

        if llm is None:
            if not queries_tried and user_question:
                return {
                    "next_query": user_question,
                    "next_mode": "broaden",
                    "next_engine": "bing",  # cn.bing.com 国内可用，Google 易被反爬
                }
            # 没 LLM 又跑过一轮 → 让循环自然终止
            return {"next_query": "", "next_mode": "broaden", "next_engine": "bing"}

        candidates_brief = "\n".join(
            f"- [{c.get('source_type', '?')}] {c.get('title_hint', '') or '(无标题)'}\n  {c.get('url', '')}"
            for c in state.get("candidates", [])[:10]
        )
        user_prompt = (
            f"用户问题：{user_question}\n\n"
            f"已尝试过的查询：\n{chr(10).join('- ' + q for q in queries_tried) or '(无)'}\n\n"
            f"已收集到的候选（按 [类型] 标题 / URL 列出，注意有官网但缺 docs/api 时应输出 site_search）：\n"
            f"{candidates_brief or '(无)'}"
        )
        try:
            plan = invoke_structured(
                llm,
                NextQueryPlan,
                PLAN_NEXT_QUERY_SYSTEM_PROMPT,
                user_prompt,
            )
        except Exception:
            return {"next_query": "", "next_mode": "broaden", "next_engine": "bing"}

        return {
            "next_query": plan.query,
            "next_mode": plan.mode,
            "next_engine": plan.target_engine,
        }

    return plan_node


def search_web_node(state: dict[str, Any]) -> dict[str, Any]:
    """调 web_fetcher 抓 SERP。失败时记入 degraded 但不抛错（CLAUDE.md 14 软依赖）。"""
    query = state.get("next_query", "")
    engine = state.get("next_engine", "bing")
    queries_tried = list(state.get("queries_tried", []))

    if not query:
        return {"raw_serp": [], "queries_tried": queries_tried}

    queries_tried.append(query)
    try:
        # 优先 Bing：cn.bing.com 国内可用且对 playwright 自动化友好；
        # Google 即使配反检测 stealth 仍可能命中边缘检测（unusual traffic 页），
        # 仅当用户/LLM 明确指定且非首轮时尝试 Google，失败回退 Bing。
        if engine == "google":
            results = search_google(query, num_results=10)
            if not results:
                results = search_bing(query, num_results=10)
        else:
            results = search_bing(query, num_results=10)
    except Exception as exc:
        return {
            "raw_serp": [],
            "queries_tried": queries_tried,
            "degraded": True,
            "degraded_reason": f"search_{engine} 失败: {str(exc)[:200]}",
        }

    return {"raw_serp": results, "queries_tried": queries_tried}


def create_classify_node(llm: Any = None) -> Callable:
    """把 SERP 候选分类为 official-doc / community / discard。

    llm=None：用启发式（域名是否含 docs/官方关键词）粗分。
    """

    def classify_node(state: dict[str, Any]) -> dict[str, Any]:
        serp = state.get("raw_serp", []) or []
        if not serp:
            return {"raw_serp": []}

        prior_candidates = list(state.get("candidates", []))
        seen_urls = {c.get("url") for c in prior_candidates}

        if llm is None:
            classified = [_heuristic_classify(item) for item in serp]
        else:
            user_prompt = "候选 URL（只看 URL+标题+摘要做分类，不抓取）：\n" + "\n".join(
                f"- url: {it.get('url', '')}\n  title: {it.get('title', '')}\n  snippet: {it.get('snippet', '')[:200]}"
                for it in serp
            )
            try:
                batch = invoke_structured(
                    llm,
                    UrlClassificationBatch,
                    CLASSIFY_URL_SYSTEM_PROMPT,
                    user_prompt,
                )
                cls_by_url = {c.url: c for c in batch.classifications}
                classified = []
                for item in serp:
                    url = item.get("url", "")
                    cls = cls_by_url.get(url)
                    if cls is None:
                        classified.append(_heuristic_classify(item))
                    else:
                        classified.append(_to_candidate(item, cls))
            except Exception:
                classified = [_heuristic_classify(item) for item in serp]

        new_candidates = [
            c for c in classified if c.get("source_type") != "discard" and c.get("url") not in seen_urls
        ]
        merged = prior_candidates + new_candidates
        return {"candidates": merged, "raw_serp": []}

    return classify_node


def check_continue_node(state: dict[str, Any]) -> dict[str, Any]:
    """终止判定（纯代码）：达到 max_iter / 超时 / 找到足够 official-doc。

    返回值会被 conditional_edges 读取（_route 字段）。
    """
    iteration = int(state.get("iteration", 0)) + 1
    max_iter = int(state.get("max_iterations", 5))
    total_timeout = float(state.get("total_timeout", 90.0))
    started_at = float(state.get("started_at", time.time()))
    target_official = int(state.get("target_official_count", 3))
    candidates = state.get("candidates", []) or []

    official_count = sum(
        1 for c in candidates if c.get("source_type") == "official-doc"
    )

    elapsed = time.time() - started_at
    if iteration >= max_iter:
        return {
            "iteration": iteration,
            "_route": "end",
            "degraded_reason": "max_iterations",
        }
    if elapsed > total_timeout:
        return {
            "iteration": iteration,
            "_route": "end",
            "degraded_reason": "total_timeout",
            "degraded": True,
        }
    if official_count >= target_official:
        return {"iteration": iteration, "_route": "end"}
    if state.get("degraded") and not candidates:
        return {
            "iteration": iteration,
            "_route": "end",
            "degraded_reason": state.get("degraded_reason") or "search_unavailable",
        }
    if not state.get("next_query"):
        # plan 没给出新查询 → 没法继续
        return {
            "iteration": iteration,
            "_route": "end",
            "degraded_reason": "no_next_query",
        }
    return {"iteration": iteration, "_route": "continue"}


# ---------------------------------------------------------------------------
# 启发式分类（llm=None 兜底）
# ---------------------------------------------------------------------------

_OFFICIAL_DOMAIN_HINTS = (
    "docs.", "developer.", ".org/docs", ".io/docs", "/reference/",
    "/api/", "/guide/", "github.com/", "rfc-editor.org",
)
_DISCARD_DOMAIN_HINTS = ("pinterest.", "facebook.", "twitter.com/i/", "reddit.com/poll")


def _heuristic_classify(item: dict[str, Any]) -> dict[str, Any]:
    url = item.get("url", "")
    title = item.get("title", "")
    snippet = item.get("snippet", "")
    lower = url.lower()
    if any(h in lower for h in _DISCARD_DOMAIN_HINTS):
        st = "discard"
    elif any(h in lower for h in _OFFICIAL_DOMAIN_HINTS):
        st = "official-doc"
    else:
        st = "community"
    return {
        "url": url,
        "title_hint": title,
        "source_type": st,
        "confidence": 0.5,
        "snippet": snippet,
    }


def _to_candidate(item: dict[str, Any], cls: UrlClassification) -> dict[str, Any]:
    return {
        "url": cls.url or item.get("url", ""),
        "title_hint": cls.title_hint or item.get("title", ""),
        "source_type": cls.source_type,
        "confidence": cls.confidence,
        "snippet": item.get("snippet", ""),
    }


# ---------------------------------------------------------------------------
# T16：Agent 化候选选择（Send fan-out + 并发 preview + 并发 LLM 评分）
# ---------------------------------------------------------------------------


def create_fan_out_to_preview(llm: Any) -> Callable:
    """classify 后的条件路由工厂。

    返回值：
    - ``list[Send]``：未评分的新候选 → 每个一个 Send，并行触发 preview_score_one
    - ``"merge_scores"``：无 LLM 或无新候选 → 跳过 fan-out，直接 merge

    审计陷阱 D：返回空 list 等于无边，下游卡住——必须返回字符串路由到默认节点。
    """
    from langgraph.types import Send  # 局部 import 避免顶层强依赖 langgraph 子模块

    def fan_out(state: dict[str, Any]) -> Any:
        if llm is None:
            return "merge_scores"
        candidates = state.get("candidates", []) or []
        scored_urls = {
            c.get("url")
            for c in (state.get("scored_candidates", []) or [])
            if c.get("url")
        }
        new_cands = [
            c for c in candidates
            if c.get("url") and c["url"] not in scored_urls
        ]
        if not new_cands:
            return "merge_scores"
        user_question = state.get("user_question", "")
        return [
            Send(
                "preview_score_one",
                {"candidate": c, "user_question": user_question},
            )
            for c in new_cands
        ]

    return fan_out


def create_preview_score_one(llm: Any) -> Callable:
    """单候选并行节点工厂（async def）。

    每个 Send → 独立 async task：
    1. async playwright 抓 preview（单候选独立 chromium，并行启动）
    2. preview 失败 → 写入 priority_score=0 + relevance_reason="抓取失败..."
    3. ``asyncio.to_thread(invoke_structured, ...)`` 调同步 LLM
    4. LLM 失败 → 不写 priority_score，让 select 退到 T14 静态分 fallback

    返回 ``{"scored_candidates": [{...}]}``——通过 reducer ``operator.add``
    自动合并到主 state。
    """

    async def preview_score_one(sub_state: dict[str, Any]) -> dict[str, Any]:
        candidate = dict(sub_state.get("candidate") or {})
        url = candidate.get("url", "")
        user_question = sub_state.get("user_question", "")

        # Step 1：抓 preview（playwright async 单 URL）
        preview = await fetch_preview(url, timeout=15.0)
        preview_dict = preview.model_dump()

        if not preview.fetched:
            return {
                "scored_candidates": [
                    {
                        **candidate,
                        "preview": preview_dict,
                        "priority_score": 0,
                        "relevance_reason": f"抓取失败: {preview.error[:80]}",
                        "is_docs": False,
                        "is_landing": False,
                    }
                ]
            }

        # Step 2：LLM 评分（同步函数扔到线程池跑，多 Send 并行各自调用 LLM）
        user_prompt = (
            f"原问题：{user_question}\n"
            f"URL：{url}\n"
            f"标题：{preview.title}\n"
            f"标题块：{preview.heading}\n"
            f"正文预览：\n{preview.preview_text}"
        )
        try:
            score = await asyncio.to_thread(
                invoke_structured,
                llm,
                CandidateScore,
                SCORE_CANDIDATE_SYSTEM_PROMPT,
                user_prompt,
            )
        except Exception as exc:  # noqa: BLE001 — LLM 失败必须降级
            return {
                "scored_candidates": [
                    {
                        **candidate,
                        "preview": preview_dict,
                        "score_error": str(exc)[:120],
                    }
                ]
            }

        return {
            "scored_candidates": [
                {
                    **candidate,
                    "preview": preview_dict,
                    "priority_score": score.priority_score,
                    "relevance_reason": score.relevance_reason,
                    "is_docs": score.is_docs,
                    "is_landing": score.is_landing,
                }
            ]
        }

    return preview_score_one


def merge_scores_node(state: dict[str, Any]) -> dict[str, Any]:
    """fan-in 后把 scored_candidates 的 LLM 字段写回 candidates。

    候选列表的主键是 url；scored_candidates 是 reducer 累加的 list（含历史轮次评分），
    用 url → 最新一条 score 的字典做 lookup。
    """
    scored_by_url: dict[str, dict[str, Any]] = {}
    for sc in state.get("scored_candidates", []) or []:
        url = sc.get("url")
        if url:
            scored_by_url[url] = sc  # 后写覆盖前写，保留最新一轮评分

    merged: list[dict[str, Any]] = []
    for c in state.get("candidates", []) or []:
        sc = scored_by_url.get(c.get("url"))
        if sc:
            c = {
                **c,
                "preview": sc.get("preview", {}),
            }
            for key in ("priority_score", "relevance_reason", "is_docs", "is_landing"):
                if key in sc:
                    c[key] = sc[key]
            if "score_error" in sc:
                c["score_error"] = sc["score_error"]
        merged.append(c)
    return {"candidates": merged}
