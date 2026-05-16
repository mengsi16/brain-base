# -*- coding: utf-8 -*-
"""QA 第二段：get_info_block + 多 URL 爬取处理（T25）。

T23 barrier1 之后到候选数据完整化为止的 fan-out 阶段：

    merge_search_keywords (sync, sub_lexical_queries → search_keywords)
        → search_web_dual (async, google 2 页 + bing 2 页 全 query 并行)
        → fanout_extract_dispatcher (条件边, 5 重 gate, Send × N URL)
        → fetch_extract_one × N (async, Semaphore=cfg.fetch_extract_concurrency)
        → barrier_extract (过滤 whether_in=False / score 降序 / 错误聚合)

设计参考 ``qa_prep.py``（T23）：
- 子节点 state 通过 TypedDict + ``Send`` 派发
- async 节点 + ``asyncio.to_thread`` 把同步调用扔线程池
- reducer 字段 ``extract_results: Annotated[list[dict], add]`` 自动合并
- barrier 节点拆 reducer 字段成主图扁平输出

T24 风格上下文继承：
- Send arg 携带 ``question`` + ``sub_questions``
- ``fetch_extract_one`` user_prompt 多跳模式塞子问题列表 [s0]/[s1]，
  让 LLM 看到这个 URL 是哪个子问题召回的，避免误判 whether_in
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypedDict

from brain_base.agents.schemas import FetchExtractResult
from brain_base.agents.utils.structured import invoke_structured
from brain_base.config import GetInfoConfig
from brain_base.nodes._hash import compute_body_sha256
from brain_base.prompts.get_info_prompts import FETCH_EXTRACT_SYSTEM_PROMPT
from brain_base.tools.doc_converter_tool import (
    convert_html_to_markdown,
    convert_html_to_markdown_readability,
)
from brain_base.tools.milvus_client import hash_lookup
from brain_base.tools.web_fetcher import fetch_page, search_bing, search_google

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 模块级 Semaphore（fetch_extract_one 限流）
#
# Semaphore 必须在 event loop 内创建（绑当前 loop），用 lazy 模式延迟到第一次
# acquire；测试或 config 改变 concurrency 时重建。
# ---------------------------------------------------------------------------


_sem: asyncio.Semaphore | None = None
_sem_concurrency: int = 0
_sem_loop_id: int | None = None


def _get_semaphore(concurrency: int) -> asyncio.Semaphore:
    """惰性创建 Semaphore；concurrency 改变或 loop 切换时重建。

    Python 3.10+ asyncio.Semaphore 在首次 acquire 时 lazy-bind 到当前 loop。
    多次 ``asyncio.run()`` 会创建多个 loop，复用 sem 会报
    ``RuntimeError: <Semaphore> is bound to a different event loop``。
    用 ``id(loop)`` 作为 cache key，loop 变化时重建 sem。
    """
    global _sem, _sem_concurrency, _sem_loop_id
    try:
        current_loop_id: int | None = id(asyncio.get_running_loop())
    except RuntimeError:
        current_loop_id = None
    if (_sem is None
            or _sem_concurrency != concurrency
            or _sem_loop_id != current_loop_id):
        _sem = asyncio.Semaphore(concurrency)
        _sem_concurrency = concurrency
        _sem_loop_id = current_loop_id
    return _sem


# ---------------------------------------------------------------------------
# Node 1: merge_search_keywords (sync)
# ---------------------------------------------------------------------------


def merge_search_keywords_node(state: dict[str, Any]) -> dict[str, Any]:
    """收集各子问题的 lexical_query 成 search query 列表。

    入：``sub_lexical_queries: list[str]`` （每子问题 1 段 ≤30 字短自然语言串）
    出：``{"search_keywords": list[str]}``

    T30：原读 ``sub_grep_keywords: list[list[str]]`` 需要 join keywords list；
    新版 LLM 直接输出 ``lexical_query`` 短串（prompt 已要求"短自然语言查询"，
    SERP 友好），无需再 join，query 级别去重保序后即可作 SERP query。
    """
    sub_queries = state.get("sub_lexical_queries", []) or []
    queries: list[str] = []
    seen: set[str] = set()
    for q in sub_queries:
        q = (q or "").strip()
        if q and q not in seen:
            seen.add(q)
            queries.append(q)
    return {"search_keywords": queries}


# ---------------------------------------------------------------------------
# Node 1.5: search_strategy (T40, sync, optional)
# ---------------------------------------------------------------------------


def create_search_strategy_node(llm: Any) -> Callable:
    """场景化搜索策略节点工厂（T40）。

    位于 merge_search_keywords → search_web_dual 之间。
    读 search_keywords，为每条 query 判定场景 + 建议 site + 重写 query。
    输出 search_strategies（观测用）+ 覆盖 search_keywords（实际搜索用）。

    当 config.enable_search_strategy=False 时不注册此节点（由 graph 决定）。
    """
    from brain_base.agents.schemas import SearchStrategyBatch
    from brain_base.prompts.qa_prompts import SEARCH_STRATEGY_SYSTEM_PROMPT

    def search_strategy_node(state: dict[str, Any]) -> dict[str, Any]:
        keywords = state.get("search_keywords", []) or []
        if not keywords:
            return {"search_strategies": []}

        question = state.get("question", "")
        user_prompt = (
            f"用户问题：{question}\n\n"
            f"待搜索 query 列表：\n"
            + "\n".join(f"- {q}" for q in keywords)
        )

        result = invoke_structured(
            llm,
            SearchStrategyBatch,
            SEARCH_STRATEGY_SYSTEM_PROMPT,
            user_prompt,
        )

        strategies = [s.model_dump() for s in result.strategies]

        # 用 rewritten_query 覆盖 search_keywords（实际搜索用）
        rewritten = [s.get("rewritten_query", "") for s in strategies if s.get("rewritten_query")]
        if rewritten:
            return {
                "search_strategies": strategies,
                "search_keywords": rewritten,
            }
        return {"search_strategies": strategies}

    return search_strategy_node


# ---------------------------------------------------------------------------
# Node 2: search_web_dual (async)
# ---------------------------------------------------------------------------


async def _throttled_search(
    *,
    sem: asyncio.Semaphore,
    last_per_engine: dict[str, float],
    last_lock: asyncio.Lock,
    engine: str,
    fn: Callable[..., Awaitable[Any]],
    cfg: GetInfoConfig,
    fn_args: tuple,
    task_meta: tuple[str, int, int],
) -> Any:
    """SERP 节流包装：全局并发 ≤ cfg.serp_concurrency + 同 engine 间隔随机拖延。

    为什么这么设计：
    1. **全局 Semaphore**：同一时刻 chromium 里并发跑 ≤ N 个 SERP page，防资源耗尽 × 防同时多请求被反爬看穿。
    2. **per-engine 间隔**：google / bing 各自独立节流（不互相等；google 跑时 bing 可以并行）；同 engine 两个相邻 task 至少间隔 [min,max] 随机拖延，模拟人类点击节奏。
    3. **last_per_engine 原子读改**：多个 task 同时读取 last 会算出同一个 wait 值，导致改后同时释放 → 同时发起；进入 critical section 后原子 “读 → 估算 wait → 占住下一个时段”（last 设为 now+wait），sleep 在锁外走。
    """
    async with sem:
        # 原子取得该 engine 的下一个可用时段
        async with last_lock:
            now = time.monotonic()
            last = last_per_engine.get(engine, 0.0)
            elapsed = now - last
            gap = random.uniform(cfg.serp_min_interval_sec, cfg.serp_max_interval_sec)
            wait = max(0.0, gap - elapsed)
            # 领取下一个时段 → 后面 task 会看到这个新 last 接着排队
            last_per_engine[engine] = now + wait

        if wait > 0:
            logger.info(
                "serp throttle | engine=%s page=%d q_idx=%d query=%r wait=%.1fs",
                engine, task_meta[1], task_meta[2],
                fn_args[0][:80] if fn_args else "?", wait,
            )
            await asyncio.sleep(wait)

        logger.info(
            "serp launch | engine=%s page=%d q_idx=%d query=%r",
            engine, task_meta[1], task_meta[2],
            fn_args[0][:80] if fn_args else "?",
        )
        return await fn(*fn_args)


async def search_web_dual_node(state: dict[str, Any]) -> dict[str, Any]:
    """对每个 query 跑 google × pages + bing × pages 并行抓 SERP，URL 去重合并。

    入：``search_keywords: list[str]`` + ``get_info_config.search_pages_per_engine``
    出：``{"serp_urls": list[dict]}``，每条含
        ``{url, title, snippet, from_engines: list[str], from_queries: list[int]}``

    并行模型（T29 节流）：
    - 每 query × page × engine 一个 task，全部走 ``_throttled_search`` 包装。
    - 全局并发 ≤ ``cfg.serp_concurrency``（默认 3）；同 engine 相邻 task 间隔 [min,max] 随机。
    - asyncio.gather 启动顺序决定优先级：q_idx 小 × page 小 优先（拼 tasks 顺序）。

    失败语义：单 task 抛错 → 该路 SERP 空，不阻断其他；全失败 → ``serp_urls=[]``
    让 dispatcher 短路 barrier_extract。
    """
    queries = state.get("search_keywords", []) or []
    if not queries:
        return {"serp_urls": []}

    cfg: GetInfoConfig = state.get("get_info_config") or GetInfoConfig()
    pages = max(1, cfg.search_pages_per_engine)

    # T29 节流状态：sem 限全局并发，last_per_engine 记录每个 engine 上次发起时间，
    # last_lock 保护原子 read-modify-write（多 task 同时进 sem 但必须依次取得 last）。
    sem = asyncio.Semaphore(cfg.serp_concurrency)
    last_per_engine: dict[str, float] = {}
    last_lock = asyncio.Lock()

    # 构造 (engine, page, query_idx) 三元组任务 + 平行 meta 数组
    # T29: web_fetcher 已迁移为 async API，search_google/search_bing 是 coroutine，
    # 走 _throttled_search 节流包装后 await；同 event loop 单线程，playwright 内部调度
    # 并发 page，不需要 _LOCK 串行化。
    tasks: list = []
    task_meta: list[tuple[str, int, int]] = []
    for q_idx, q in enumerate(queries):
        for page in range(1, pages + 1):
            meta_g = ("google", page, q_idx)
            tasks.append(_throttled_search(
                sem=sem, last_per_engine=last_per_engine, last_lock=last_lock,
                engine="google", fn=search_google, cfg=cfg,
                fn_args=(q, 10, page), task_meta=meta_g,
            ))
            task_meta.append(meta_g)
            meta_b = ("bing", page, q_idx)
            tasks.append(_throttled_search(
                sem=sem, last_per_engine=last_per_engine, last_lock=last_lock,
                engine="bing", fn=search_bing, cfg=cfg,
                fn_args=(q, 10, page), task_meta=meta_b,
            ))
            task_meta.append(meta_b)

    logger.info(
        "search_web_dual launching %d tasks | concurrency=%d interval=[%.1f,%.1f]s queries=%d pages=%d",
        len(tasks), cfg.serp_concurrency, cfg.serp_min_interval_sec,
        cfg.serp_max_interval_sec, len(queries), pages,
    )
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 按 URL 聚合：同一 URL 多次出现合并 from_engines / from_queries
    aggregated: dict[str, dict[str, Any]] = {}
    # 失败计数（用于汇总日志，避免每条都刷屏）
    fail_summary: list[tuple[str, int, int, str, str]] = []
    for meta, res in zip(task_meta, results):
        engine, _page, q_idx = meta
        if isinstance(res, Exception):
            # 规则 25：保留 try-except（这里是 gather return_exceptions=True 的等价语义）必须打日志
            fail_summary.append((engine, _page, q_idx, type(res).__name__, str(res)[:200]))
            logger.warning(
                "search_web_dual task EXCEPTION | engine=%s page=%d q_idx=%d query=%r err=%s: %s",
                engine, _page, q_idx, queries[q_idx][:80] if q_idx < len(queries) else "?",
                type(res).__name__, str(res)[:200],
            )
            continue
        if not isinstance(res, list):
            fail_summary.append((engine, _page, q_idx, "non_list", type(res).__name__))
            logger.warning(
                "search_web_dual task NON-LIST | engine=%s page=%d q_idx=%d query=%r got_type=%s",
                engine, _page, q_idx, queries[q_idx][:80] if q_idx < len(queries) else "?",
                type(res).__name__,
            )
            continue
        if len(res) == 0:
            # 空 list 不算错（搜索返 0 结果是合法的），但 INFO 记录便于诊断
            logger.info(
                "search_web_dual task EMPTY | engine=%s page=%d q_idx=%d query=%r",
                engine, _page, q_idx, queries[q_idx][:80] if q_idx < len(queries) else "?",
            )
        for item in res:
            url = (item.get("url") or "").strip()
            if not url:
                continue
            entry = aggregated.setdefault(
                url,
                {
                    "url": url,
                    "title": item.get("title", ""),
                    "snippet": item.get("snippet", ""),
                    "from_engines": [],
                    "from_queries": [],
                },
            )
            if engine not in entry["from_engines"]:
                entry["from_engines"].append(engine)
            if q_idx not in entry["from_queries"]:
                entry["from_queries"].append(q_idx)

    # 汇总日志（无论成败都打，让 e2e 评判时一眼看到 search 战果）
    logger.info(
        "search_web_dual DONE | queries=%d total_tasks=%d fail_tasks=%d unique_urls=%d",
        len(queries), len(tasks), len(fail_summary), len(aggregated),
    )
    return {"serp_urls": list(aggregated.values())}


# ---------------------------------------------------------------------------
# Node 3: fanout_extract_dispatcher (条件边, sync)
# ---------------------------------------------------------------------------


class ExtractState(TypedDict, total=False):
    """fanout_extract_dispatcher 通过 Send 派发的子状态字段。

    fetch_extract_one 节点读这些字段；结果合并到主图 ``extract_results`` reducer。
    """
    url: str
    title: str
    snippet: str
    from_engines: list[str]
    from_queries: list[int]
    question: str
    sub_questions: list[str]


def fanout_extract_dispatcher(state: dict[str, Any]) -> Any:
    """条件边：5 重 gate 派发 Send × N（每个 URL 1 个）。

    Gate 顺序（任一短路 → 直接 ``"barrier_extract"`` 字符串）：

    1. ``cfg.enable=False`` → 短路（关闭外检）
    2. ``not any(sub_needs_get_info)`` → 短路（T23 grep 全命中，不需外检；
       T25 用此条件替代旧 ``get_info_trigger`` 节点的判定）
    3. ``get_info_attempted=True`` → 短路（防死循环；barrier_extract 第一次
       后会写 True）
    4. ``infra.playwright_available=False`` → 短路
    5. ``serp_urls=[]`` → 短路

    否则返回 ``list[Send]``，每个 Send 携带 T24 风格上下文（question +
    sub_questions）+ SERP 元数据。
    """
    from langgraph.types import Send  # 局部 import 避免顶层强依赖

    cfg: GetInfoConfig = state.get("get_info_config") or GetInfoConfig()
    if not cfg.enable:
        return "barrier_extract"

    sub_needs = state.get("sub_needs_get_info", []) or []
    if not any(sub_needs):
        return "barrier_extract"

    if state.get("get_info_attempted", False):
        return "barrier_extract"

    infra = state.get("infra", {}) or {}
    if not infra.get("playwright_available", True):
        return "barrier_extract"

    serp_urls = state.get("serp_urls", []) or []
    if not serp_urls:
        return "barrier_extract"

    question = state.get("question", "") or ""
    sub_questions = state.get("sub_questions", []) or []

    return [
        Send(
            "fetch_extract_one",
            {
                "url": item.get("url", ""),
                "title": item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "from_engines": item.get("from_engines", []),
                "from_queries": item.get("from_queries", []),
                "question": question,
                "sub_questions": sub_questions,
            },
        )
        for item in serp_urls
    ]


# ---------------------------------------------------------------------------
# Node 4: fetch_extract_one (async, Semaphore 限流)
# ---------------------------------------------------------------------------


def _fetch_extract_user_prompt(
    *,
    question: str,
    sub_questions: list[str],
    title: str,
    snippet: str,
    from_engines: list[str],
    from_queries: list[int],
    markdown: str,
) -> str:
    """T24 风格上下文继承的 user_prompt 拼装。

    多跳模式（``len(sub_questions) > 1``）：塞 question + 子问题列表 [s_idx]
    单跳模式：仅塞 question + SERP 元数据。
    """
    engines_str = ", ".join(from_engines) if from_engines else "未知"
    queries_str = (
        ", ".join(f"q{i}" for i in from_queries) if from_queries else "未知"
    )

    if len(sub_questions) > 1:
        sub_list = "\n".join(f"  [s{i}] {sq}" for i, sq in enumerate(sub_questions))
        return (
            f"用户原始问题：{question}\n\n"
            f"子问题列表（按 sub_idx 索引）：\n{sub_list}\n\n"
            f"SERP 召回背景：从 {engines_str} 召回，命中关键词组 [{queries_str}]\n"
            f"SERP 标题：{title}\n"
            f"SERP 摘要：{snippet}\n\n"
            f"完整 markdown 内容（已清洗）：\n{markdown}"
        )

    return (
        f"用户问题：{question}\n\n"
        f"SERP 召回背景：从 {engines_str} 召回\n"
        f"SERP 标题：{title}\n"
        f"SERP 摘要：{snippet}\n\n"
        f"完整 markdown 内容（已清洗）：\n{markdown}"
    )


# T27：删 _fetch_extract_fallback——invoke_structured 不再接受 fallback 形参；
# LLM 调用失败走 fetch_extract_one 外层 try/except（fan-out 单 Send 失败隔离），
# 成为含 error 字段的 candidate，不会走 discard。


def create_fetch_extract_one(
    llm: Any = None,
    config: GetInfoConfig | None = None,
) -> Callable:
    """fetch_extract_one async 节点工厂。

    每个 Send 实例独立 acquire 模块级 Semaphore，限制并发 N（默认 3）防 LLM
    API 限流。

    步骤：
        1. ``fetch_page(url)`` → 完整 HTML（playwright）
        2. ``convert_html_to_markdown_readability(html)`` 主路径 →
           ``convert_html_to_markdown(html)`` MinerU 兜底
        3. ``compute_body_sha256(markdown)`` 算内容指纹 →
           ``hash_lookup`` 查 ``data/raw/`` 已有文档（规则 11：dedup 走文件系统）
        4. 命中 → short-circuit：``logger.info`` 记一行 + ``return {"extract_results": []}``
           （命中 = 内容已在 Milvus 里，QA 后续 fanout_search 天然召回；让 candidate 走
           下游只产生"更新 fetched_at"这种没人消费的副作用）
        5. 未命中 → ``invoke_structured(llm, FetchExtractResult, ...)`` LLM 一次产 6 字段
        6. 组装 candidate dict 加 ``url/title/fetched_at/markdown/content_sha256/from_*``

    失败隔离：任一步骤抛错 → 单 Send 写入 ``extract_results: [{url, error,
    whether_in: False}]``，不阻断其他 Send。这是 fan-out 必要的失败隔离，
    不是吞错——错误透传到 barrier_extract 的 ``extract_errors`` 字段。
    """
    cfg = config or GetInfoConfig()

    async def fetch_extract_one(sub_state: ExtractState) -> dict[str, Any]:
        url = sub_state.get("url", "") or ""
        title = sub_state.get("title", "") or ""
        snippet = sub_state.get("snippet", "") or ""
        from_engines = sub_state.get("from_engines", []) or []
        from_queries = sub_state.get("from_queries", []) or []
        question = sub_state.get("question", "") or ""
        sub_questions = sub_state.get("sub_questions", []) or []

        sem = _get_semaphore(cfg.fetch_extract_concurrency)
        async with sem:
            try:
                # Step 1: fetch HTML（T29: web_fetcher 已迁移为 async，直接 await）
                fetched = await fetch_page(url)
                html = (fetched.get("html") or "") if isinstance(fetched, dict) else ""
                if not html.strip():
                    raise RuntimeError("empty html")

                # Step 2: HTML → markdown (Readability 主, MinerU 兜底)
                try:
                    markdown = await asyncio.to_thread(
                        convert_html_to_markdown_readability, html
                    )
                except Exception:
                    markdown = await asyncio.to_thread(convert_html_to_markdown, html)

                if not markdown or not markdown.strip():
                    raise RuntimeError("empty markdown")

                # Step 3: 算内容指纹 + raw 目录去重查询（T24 回补）
                content_sha256 = await asyncio.to_thread(
                    compute_body_sha256, markdown
                )
                lookup_result = await asyncio.to_thread(
                    hash_lookup, content_sha256
                )
                resolved_title = title or (
                    fetched.get("title", "") if isinstance(fetched, dict) else ""
                )

                # Step 4: 命中分支 → short-circuit 丢弃，不写 extract_results
                # 原因：命中 = 内容已在 Milvus 里，QA 主流程后续 fanout_search 阶段天然召回，
                # 让 candidate 继续走下游流水只能产生“更新 fetched_at”这种没人消费的副作用。
                if lookup_result.get("status") == "hit":
                    matches = lookup_result.get("matches") or []
                    existing_doc_id = matches[0].get("doc_id", "") if matches else ""
                    logger.info(
                        "fetch_extract_one: hash hit, skipped. sha256=%s url=%s existing_doc_id=%s",
                        content_sha256, url, existing_doc_id,
                    )
                    return {"extract_results": []}

                # Step 5: 未命中 → LLM 评估。T27 fail-fast：invoke_structured
                # 不再传 fallback；LLM 异常走外层 try/except（fan-out 隔离）。
                # T27：修复 T25 遗留签名错配——_fetch_extract_user_prompt 接受
                # from_engines/from_queries 而不是 url（之前被 fallback 吞掉异常没暴露）。
                user_prompt = _fetch_extract_user_prompt(
                    question=question,
                    sub_questions=sub_questions,
                    title=title,
                    snippet=snippet,
                    from_engines=list(from_engines),
                    from_queries=list(from_queries),
                    markdown=markdown,
                )
                result: FetchExtractResult = await asyncio.to_thread(
                    invoke_structured,
                    llm,
                    FetchExtractResult,
                    FETCH_EXTRACT_SYSTEM_PROMPT,
                    user_prompt,
                )

                # Step 6: 组装候选 dict
                candidate = {
                    "url": url,
                    "title": resolved_title,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "markdown": markdown,
                    "content_sha256": content_sha256,
                    "from_engines": list(from_engines),
                    "from_queries": list(from_queries),
                    "score": int(result.score),
                    "type": result.type,
                    "summary": result.summary,
                    "keywords": list(result.keywords),
                    "whether_in": bool(result.whether_in),
                    "reason": result.reason,
                }
                return {"extract_results": [candidate]}
            except Exception as e:
                # fan-out 失败隔离：错误结构化透传到下游 barrier_extract。
                # CLAUDE.md 规则 25 补丁：保留 try-except 必须 log。
                logger.warning(
                    "fetch_extract_one fan-out fail: url=%s exc=%s: %s",
                    url, type(e).__name__, str(e)[:200],
                )
                return {
                    "extract_results": [
                        {
                            "url": url,
                            "error": str(e)[:200],
                            "whether_in": False,
                        }
                    ]
                }

    return fetch_extract_one


# ---------------------------------------------------------------------------
# Node 5: barrier_extract (sync, fan-in 合并)
# ---------------------------------------------------------------------------


def barrier_extract_node(state: dict[str, Any]) -> dict[str, Any]:
    """fan-in 合并：过滤 whether_in=False/error，按 score 降序，错误聚合。

    入：``extract_results: list[dict]``（reducer 累加结果）
    出：
        - ``get_info_candidates``：whether_in=True 且无 error 的候选，按 score 降序
        - ``extract_errors``：错误聚合 ``["url: error", ...]``
        - ``get_info_attempted=True``：防 dispatcher 重复触发
    """
    results = list(state.get("extract_results", []) or [])

    candidates: list[dict] = []
    errors: list[str] = []

    for r in results:
        url = r.get("url", "") or ""
        err = r.get("error", "")
        if err:
            errors.append(f"{url}: {err}")
            continue
        if not r.get("whether_in", False):
            continue
        candidates.append(r)

    # 同分稳定保序（Python sort 是稳定排序）
    candidates.sort(key=lambda c: int(c.get("score", 0) or 0), reverse=True)

    return {
        "get_info_candidates": candidates,
        "extract_errors": errors,
        "get_info_attempted": True,
    }
