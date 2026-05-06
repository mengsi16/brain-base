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

from brain_base.agents.schemas import (
    NextQueryPlan,
    UrlClassification,
    UrlClassificationBatch,
)
from brain_base.agents.utils.structured import invoke_structured
from brain_base.prompts.get_info_prompts import (
    CLASSIFY_URL_SYSTEM_PROMPT,
    PLAN_NEXT_QUERY_SYSTEM_PROMPT,
)
from brain_base.tools.web_fetcher import search_bing, search_google


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
        # Google 在 Windows + playwright-cli 下经常 0 结果（反爬），仅当
        # 用户/LLM 明确指定且非首轮时尝试 Google，失败回退 Bing。
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
