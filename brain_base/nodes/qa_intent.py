# -*- coding: utf-8 -*-
"""T47 统一意图识别 Agent-Loop 节点（动作侧 + 评估侧）。

主图位置（T47.4 接线后）：
    decompose → intent_planner → intent_executor → intent_observer
                     ↑                                    │
                     └───── should_continue_intent ←──────┘
                                          ↓ 早退
                                     merge_evidence → fanout_persist_dispatcher

T47.3a 实现：动作侧 2 节点
- create_intent_planner: LLM 决策本跳要调哪些工具（IntentPlan，支持 fan-out D1）
- create_intent_executor: 串行 / 并发执行工具（asyncio.gather），错误隔离

T47.3b 实现：评估侧 2 节点
- create_intent_observer: 状态聚合 + LLM 评估充分性（IntentObservation），追加 evidence_pool
- merge_evidence_node: evidence_pool → get_info_candidates 格式适配（无 LLM）

设计原则（CLAUDE.md 规则 14 + T27 fail-fast）：
- llm=None 时节点工厂直接 raise（不允许 mock 测语义，生产必须真 LLM）
- executor 单工具失败不抛、不污染他人（fan-out 隔离）
- planner 自过滤 unknown tool_name（白名单第一道防线，executor 是第二道）
- observer 覆盖式写完整结构（QaState 不用 reducer，与 T46 hop_observer 模式一致）
- intent_sufficient = (confidence >= 0.85) AND (remaining_gaps == [])

契约引用：md/research/2026-05-17-t47-unified-intent-agent-contract.md §4 + §5 + §6 + §7
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from brain_base.agents.schemas import IntentObservation, IntentPlan
from brain_base.agents.utils.structured import invoke_structured
from brain_base.nodes._hash import compute_body_sha256
from brain_base.nodes.qa_tools import TOOL_REGISTRY
from brain_base.prompts.intent_prompts import (
    INTENT_OBSERVER_SYSTEM_PROMPT,
    INTENT_PLANNER_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: prompt 上下文渲染（intent_planner 内部用）
# ---------------------------------------------------------------------------


# evidence_pool 渲染总长度上限（防 prompt token 撑爆）
_EVIDENCE_POOL_RENDER_CHAR_LIMIT = 3000
# 单条 evidence 摘要截断长度
_EVIDENCE_SNIPPET_CHAR_LIMIT = 150
# url_pre_fetch excerpt 二次裁剪长度（与 normalize 节点对齐）
_URL_EXCERPT_CHAR_LIMIT = 1500


def _format_evidence_pool(evidence_pool: list[dict]) -> str:
    """把 evidence_pool 格式化成 prompt 友好的 markdown bullet。

    单条格式：
        - [score=85] 来源: <url>
          摘要: <snippet[:150]>
          服务于: <from_queries 前 2 个>

    总长度超 _EVIDENCE_POOL_RENDER_CHAR_LIMIT 时截断 + 加 "...省略 N 条"。
    空 pool 时返回 "（首跳，evidence_pool 为空）"。
    """
    if not evidence_pool:
        return "（首跳，evidence_pool 为空）"

    lines: list[str] = []
    total_len = 0
    rendered_count = 0
    for ev in evidence_pool:
        url = (ev.get("url") or "").strip()
        score = ev.get("score", 0.0)
        snippet = (ev.get("snippet") or ev.get("summary") or "").strip()
        from_queries = ev.get("from_queries") or []

        if not url and not snippet:
            continue

        block_lines = [f"- [score={score:.0f}] 来源: {url or '(无 URL)'}"]
        if snippet:
            block_lines.append(f"  摘要: {snippet[:_EVIDENCE_SNIPPET_CHAR_LIMIT]}")
        if from_queries:
            block_lines.append(f"  服务于: {', '.join(str(q) for q in from_queries[:2])}")
        block = "\n".join(block_lines)

        if total_len + len(block) > _EVIDENCE_POOL_RENDER_CHAR_LIMIT:
            remaining = len(evidence_pool) - rendered_count
            if remaining > 0:
                lines.append(f"... (剩余 {remaining} 条已省略，避免 prompt 撑爆)")
            break
        lines.append(block)
        total_len += len(block) + 1
        rendered_count += 1

    return "\n".join(lines) if lines else "（evidence_pool 全部为空条目）"


def _format_url_context(url_pre_fetch_content: list[dict]) -> str:
    """把 url_pre_fetch_content 渲染为 [URL 浅抓上下文] section（条件渲染）。

    与 normalize 节点（@/brain_base/nodes/qa.py:create_normalize_node）对齐：
    excerpt 截 1500 字符（二次裁剪防 prompt 撑爆——_fetch_one 已截 2000）。

    空时返回空串（让 user_prompt 不渲染该段）。
    """
    if not url_pre_fetch_content:
        return ""

    lines: list[str] = []
    for idx, item in enumerate(url_pre_fetch_content, 1):
        u = (item.get("url") or "").strip()
        t = (item.get("title") or "").strip()
        excerpt = (item.get("markdown_excerpt") or "").strip()
        if not u:
            continue
        lines.append(f"- URL {idx}: {u}")
        if t:
            lines.append(f"  Title: {t}")
        if excerpt:
            lines.append(f"  Excerpt: {excerpt[:_URL_EXCERPT_CHAR_LIMIT]}")

    if not lines:
        return ""
    return "## URL 浅抓上下文\n" + "\n".join(lines) + "\n"


def _format_tools_desc() -> str:
    """从 TOOL_REGISTRY 动态拼工具描述（避免硬编码工具名漂移）。

    格式：
        - tool_name: description
    """
    return "\n".join(
        f"- {spec.name}: {spec.description}"
        for spec in TOOL_REGISTRY.values()
    )


# ---------------------------------------------------------------------------
# Node 1: intent_planner（sync, LLM）
# ---------------------------------------------------------------------------


def create_intent_planner(llm: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """intent_planner 节点工厂：LLM 输出 IntentPlan（契约 §4）。

    Args:
        llm: LangChain BaseChatModel 实例。**T27 fail-fast**：不接受 None，
             llm=None 时本工厂直接 raise（CLAUDE.md 规则 14：LLM 是 Agent 核心
             依赖，缺失即 fail-fast，不允许降级）。

    Returns:
        intent_planner_node(state) -> {"current_intent_plan": dict}

    决策行为：
    - 工具白名单：next_actions 内 tool_name 不在 TOOL_REGISTRY → log warning + 过滤
    - early_exit + actions 互斥：early_exit=True 时 actions 强制清空（防 LLM 矛盾输出）
    - 序列化：输出 dict 而非 BaseModel（QaState 是 TypedDict）
    """
    if llm is None:
        raise ValueError(
            "create_intent_planner requires non-None llm. "
            "T27 fail-fast：LLM 是 Agent 核心依赖，缺失应在 cli 入口直接退出，"
            "不允许节点降级。"
        )

    def intent_planner_node(state: dict[str, Any]) -> dict[str, Any]:
        # 读 state 字段（默认值容忍首跳为空）
        normalized_query = state.get("normalized_query") or state.get("question", "")
        sub_questions = state.get("sub_questions", []) or []
        user_urls = state.get("user_urls", []) or []
        url_pre_fetch_content = state.get("url_pre_fetch_content", []) or []
        evidence_pool = state.get("evidence_pool", []) or []
        visited_urls = state.get("visited_urls", []) or []
        iteration_count = int(state.get("iteration_count", 0))
        history_summary = (state.get("conversation_history_summary") or "").strip()
        last_obs = state.get("last_intent_observation", {}) or {}

        # 渲染 prompt 上下文
        url_context = _format_url_context(url_pre_fetch_content)
        evidence_render = _format_evidence_pool(evidence_pool)

        sub_q_render = (
            "\n".join(f"  {i+1}. {q}" for i, q in enumerate(sub_questions))
            if sub_questions else "  (decompose 未拆分子问题)"
        )
        user_urls_render = (
            "\n".join(f"  - {u}" for u in user_urls)
            if user_urls else "  (无)"
        )
        visited_render = (
            "\n".join(f"  - {u}" for u in visited_urls)
            if visited_urls else "  (无，首跳)"
        )

        last_obs_render = ""
        if last_obs:
            last_obs_render = (
                f"\n## 上跳观察\n"
                f"- coverage_summary: {last_obs.get('coverage_summary', '')}\n"
                f"- remaining_gaps: {last_obs.get('remaining_gaps', [])}\n"
                f"- confidence: {last_obs.get('confidence', 0.0)}\n"
            )

        history_render = ""
        if history_summary:
            history_render = f"\n## 多轮历史摘要\n{history_summary}\n"

        # 工具描述运行时注入（TOOL_REGISTRY 可能扩展，T48 加 arxiv_pdf / github_raw）
        system_prompt = INTENT_PLANNER_SYSTEM_PROMPT.replace(
            "{tools_desc}", _format_tools_desc()
        )

        user_prompt = (
            f"## 用户原始问题\n{normalized_query}\n\n"
            f"## 子问题列表（sub_questions）\n{sub_q_render}\n\n"
            f"## 用户提供的 URL（user_urls）\n{user_urls_render}\n\n"
            f"{url_context}"
            f"## 已积累证据（evidence_pool，{len(evidence_pool)} 条）\n{evidence_render}\n\n"
            f"## 已访问 URL（visited_urls）\n{visited_render}\n\n"
            f"## 当前迭代次数\niteration_count = {iteration_count}\n"
            f"{last_obs_render}"
            f"{history_render}"
        )

        plan: IntentPlan = invoke_structured(
            llm, IntentPlan, system_prompt, user_prompt,
        )

        # 工具白名单过滤（第一道防线）
        valid_actions = []
        for action in plan.next_actions:
            if action.tool_name in TOOL_REGISTRY:
                valid_actions.append(action)
            else:
                logger.warning(
                    "intent_planner: 过滤未注册工具 %r（reasoning=%r）",
                    action.tool_name, plan.reasoning[:100],
                )

        # early_exit 与 actions 互斥保护（早退时强制清空 actions）
        if plan.early_exit and valid_actions:
            logger.info(
                "intent_planner: early_exit=True 且 actions 非空，按早退处理（清空 %d actions）",
                len(valid_actions),
            )
            valid_actions = []

        # 重建 plan dict（保证字段命名稳定）
        plan_dict = {
            "next_actions": [a.model_dump() for a in valid_actions],
            "reasoning": plan.reasoning,
            "early_exit": plan.early_exit,
        }

        return {"current_intent_plan": plan_dict}

    return intent_planner_node


# ---------------------------------------------------------------------------
# Node 2: intent_executor（async, fan-out + 错误隔离）
# ---------------------------------------------------------------------------


def _is_serial_action(action: dict) -> bool:
    """判断 action 是否归 serial 队列（T48.1 D1）。

    规则：
    - ``parallel_ok=False`` 的注册工具 → True（走 serial for-loop 串行）
    - ``parallel_ok=True`` 或工具未注册 → False（走 parallel asyncio.gather）

    未注册工具默认归 parallel——``_execute_one`` 内部立返
    ``error: "unknown tool_name"`` 不耗资源；归 serial 反而占用宝贵的
    串行窗口。
    """
    spec = TOOL_REGISTRY.get(action.get("tool_name", ""))
    return spec is not None and not spec.parallel_ok


def _make_base_result(action: dict, error: str = "") -> dict[str, Any]:
    """构造标准 ToolResult dict（contract §5 + §1.2）。"""
    return {
        "tool_name": action.get("tool_name", ""),
        "tool_args": dict(action.get("tool_args", {}) or {}),
        "purpose": action.get("purpose", ""),
        "markdown": "",
        "source_url": "",
        "title": "",
        "summary": "",
        "score": 0.0,
        "error": error,
    }


async def _execute_one(
    action: dict,
    llm: Any,
    cfg: Any,
) -> dict[str, Any]:
    """单 action 执行：tool dispatch + try/except 隔离 + 标准化返回。

    永远不抛异常——所有失败都翻译成 ``error`` 字段，让 fan-out gather
    不会因为单条失败污染其他并发动作。

    score 启发式：
    - 工具自带 score → 用之
    - 否则 markdown 非空 → 50.0（中等占位，T47.3b observer 会重评）
    - markdown 空 → 0.0
    """
    tool_name = action.get("tool_name", "")
    tool_args = dict(action.get("tool_args", {}) or {})
    base = _make_base_result(action)

    if tool_name not in TOOL_REGISTRY:
        base["error"] = f"unknown tool_name: {tool_name!r}"
        return base

    spec = TOOL_REGISTRY[tool_name]

    try:
        if spec.is_async:
            raw = await spec.fn(tool_args, llm, cfg)
        else:
            raw = await asyncio.to_thread(spec.fn, tool_args, llm, cfg)
    except Exception as exc:
        logger.warning(
            "intent_executor %s failed: %s: %s",
            tool_name, type(exc).__name__, str(exc)[:200],
        )
        base["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return base

    if not isinstance(raw, dict):
        base["error"] = f"tool returned non-dict: {type(raw).__name__}"
        return base

    # 工具自报 error → 写入 result（可能仍有 markdown 部分内容）
    if raw.get("error"):
        base["error"] = str(raw["error"])[:500]

    md = raw.get("markdown", "") or ""
    base["markdown"] = md
    base["source_url"] = raw.get("source_url", "") or ""
    base["title"] = raw.get("title", "") or ""
    # summary：工具优先；否则截 markdown 前 500
    base["summary"] = (
        raw.get("summary")
        or (md[:500] if md else "")
    )
    # score：工具优先；否则按 markdown 是否非空二档启发式
    if "score" in raw:
        try:
            base["score"] = float(raw["score"])
        except (TypeError, ValueError):
            base["score"] = 50.0 if md else 0.0
    else:
        base["score"] = 50.0 if md else 0.0

    return base


def create_intent_executor(
    llm: Any,
    cfg: Any,
) -> Callable[[dict[str, Any]], Any]:
    """intent_executor 节点工厂：执行 IntentPlan.next_actions（契约 §5）。

    Args:
        llm: 同 create_intent_planner，T27 fail-fast 不接受 None。
        cfg: GetInfoConfig 实例（工具调用如 fetch_url / web_search 内部用）。

    Returns:
        intent_executor_node(state) -> {"current_action_results": list[dict]}

    fan-out 语义（T48.1 D1 修订：双队列调度）：
    - early_exit=True → 跳过执行，return {"current_action_results": []}
    - len(next_actions) == 0 → 同上（observer 会发现无新增）
    - len(next_actions) == 1 → 串行调用单工具
    - len(next_actions) > 1 → 按 ``ToolSpec.parallel_ok`` 分组：
        * ``parallel_ok=False`` actions 进 serial 队列 for-loop 串行执行
          （避免 GPU 工具如 ``arxiv_pdf`` 同跳触发并发 OOM）
        * 其余 actions 进 parallel 队列 ``asyncio.gather`` 并发
        * 两条流水线再用 ``asyncio.gather(run_serial, run_parallel)`` 同时跑
        * 最后按原始 idx 排序合并 results（保 observer.purpose 配对正确）

    单 action 永不抛——_execute_one 内部已 try/except 兜底，错误翻译成
    result.error 字段。consecutive_intent_errors 由 T47.3b observer 综合
    判断写入（executor 不直接写）。
    """
    if llm is None:
        raise ValueError(
            "create_intent_executor requires non-None llm. "
            "T27 fail-fast：LLM 是 Agent 核心依赖，缺失应在 cli 入口直接退出。"
        )

    async def intent_executor_node(state: dict[str, Any]) -> dict[str, Any]:
        plan = state.get("current_intent_plan", {}) or {}

        if plan.get("early_exit", False):
            logger.info("intent_executor: early_exit=True，跳过执行")
            return {"current_action_results": []}

        actions = plan.get("next_actions", []) or []
        if not actions:
            logger.info("intent_executor: next_actions 为空，跳过执行")
            return {"current_action_results": []}

        if len(actions) == 1:
            results = [await _execute_one(actions[0], llm, cfg)]
            return {"current_action_results": list(results)}

        # fan-out（T48.1 D1）：按 parallel_ok 双队列调度
        indexed = list(enumerate(actions))
        serial_indexed = [(i, a) for i, a in indexed if _is_serial_action(a)]
        parallel_indexed = [(i, a) for i, a in indexed if not _is_serial_action(a)]

        logger.info(
            "intent_executor fan-out | total=%d serial=%s parallel=%s",
            len(actions),
            [a.get("tool_name", "?") for _, a in serial_indexed],
            [a.get("tool_name", "?") for _, a in parallel_indexed],
        )

        async def run_serial() -> list[tuple[int, dict[str, Any]]]:
            """serial 队列：for-loop 串行执行（GPU 工具防 OOM）。"""
            out: list[tuple[int, dict[str, Any]]] = []
            for idx, action in serial_indexed:
                out.append((idx, await _execute_one(action, llm, cfg)))
            return out

        async def run_parallel() -> list[tuple[int, dict[str, Any]]]:
            """parallel 队列：asyncio.gather 并发（gather 单流水线内保序）。"""
            if not parallel_indexed:
                return []
            coros = [_execute_one(a, llm, cfg) for _, a in parallel_indexed]
            res = await asyncio.gather(*coros)
            # gather 保序：res[k] 对应 parallel_indexed[k]
            return [(idx, r) for (idx, _), r in zip(parallel_indexed, res)]

        # 两条流水线同时跑，serial / parallel 之间互不阻塞
        serial_results, parallel_results = await asyncio.gather(
            run_serial(), run_parallel(),
        )
        # 按原始 idx 排序合并 → 与 actions 顺序严格对齐
        combined = sorted(serial_results + parallel_results, key=lambda x: x[0])
        results = [r for _, r in combined]

        return {"current_action_results": list(results)}

    return intent_executor_node


# ===========================================================================
# T47.3b：评估侧 — Helper + Observer + Merge
# ===========================================================================


# Evidence.snippet / title 字段长度上限（与 schemas.py:582,593 对齐）
_EVIDENCE_TITLE_LEN_LIMIT = 500
_EVIDENCE_SNIPPET_LEN_LIMIT = 500
# observer 喂给 LLM 的本跳 results 摘要单条 markdown 截断长度（防 prompt 撑爆）
_OBSERVER_RESULT_MD_PREVIEW_LIMIT = 800
# intent_sufficient 阈值（契约 §6 + §7 拍板）
_INTENT_SUFFICIENT_CONFIDENCE_THRESHOLD = 0.85


def _infer_source_type(url: str, tool_name: str) -> str:
    """启发式推断 Evidence.source_type（执行计划 §1.3 拍板）。

    规则：
    - 常见技术文档站 (/docs/, docs.*, .readthedocs.) / arxiv abs / RFC → "official-doc"
    - tool_name=raw_text 且 url 含 github.com → "official-doc"（GitHub README 当作官方）
    - 其他 → "community"
    """
    if not url:
        return "community"
    url_lower = url.lower()
    official_hints = (
        "/docs/", "docs.", ".readthedocs.", "arxiv.org/abs",
        "rfc-editor.org", "tools.ietf.org/rfc",
    )
    if any(h in url_lower for h in official_hints):
        return "official-doc"
    if tool_name == "raw_text" and "github.com" in url_lower:
        return "official-doc"
    return "community"


def _tool_result_to_evidence(
    result: dict[str, Any],
    purpose: str = "",
) -> dict[str, Any] | None:
    """ToolResult dict → Evidence dict（含 SHA-256 计算 + 字段截断）。

    返回 None 时表示 result 无效（observer 应跳过）：
    - error 非空 → 跳过
    - markdown / source_url 都为空 → 跳过

    purpose: from_queries 标签来源（一般是 plan action 的 purpose 或 result.purpose 透传）。
    """
    if (result.get("error") or "").strip():
        return None

    url = (result.get("source_url") or "").strip()
    markdown = (result.get("markdown") or "").strip()

    # url 和 markdown 都为空 → 无意义证据
    if not url and not markdown:
        return None

    tool_name = result.get("tool_name", "")
    title = (result.get("title") or "")[:_EVIDENCE_TITLE_LEN_LIMIT]
    snippet = (result.get("summary") or markdown[:_EVIDENCE_SNIPPET_LEN_LIMIT])
    snippet = snippet[:_EVIDENCE_SNIPPET_LEN_LIMIT] if snippet else ""

    # SHA-256（T48.3 修订）：工具自报的 sha256_hash 优先（如 arxiv_pdf 报 PDF
    # 二进制 sha256 与 markdown 重算结果完全不同），缺失时 fallback 到
    # markdown 重算；markdown 空且无工具自报则留空（避免空内容污染 dedup）。
    tool_sha = (result.get("sha256_hash") or "").strip()
    if tool_sha:
        sha256_hash = tool_sha
    elif markdown:
        sha256_hash = compute_body_sha256(markdown)
    else:
        sha256_hash = ""

    # T48.3：透传工具自报的 raw_path（如 arxiv_pdf 落盘的完整 markdown 路径）
    raw_path = (result.get("raw_path") or "").strip()

    # score 范围保护（schemas.py:584 ge=0 le=100）
    try:
        score = float(result.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(100.0, score))

    # purpose 优先用 result.purpose（T47.3a executor 已透传），否则用入参（plan-level）
    effective_purpose = (result.get("purpose") or purpose or "").strip()
    from_queries = [effective_purpose] if effective_purpose else []

    return {
        "url": url,
        "title": title,
        "content": markdown,
        "score": score,
        "sha256_hash": sha256_hash,
        "from_queries": from_queries,
        "snippet": snippet,
        "source_type": _infer_source_type(url, tool_name),
        "tool_name": tool_name,
        "raw_path": raw_path,
    }


def _format_results_for_observer_prompt(
    results: list[dict[str, Any]],
) -> str:
    """observer LLM user_prompt 用：把本跳 results 渲染成 bullet（含 error 标记）。"""
    if not results:
        return "（本跳无 results——early_exit 或 planner 输出空 actions）"

    lines: list[str] = []
    for idx, r in enumerate(results, 1):
        tool = r.get("tool_name", "?")
        purpose = r.get("purpose", "")
        err = r.get("error", "")
        url = r.get("source_url", "")
        md_preview = (r.get("markdown") or "")[:_OBSERVER_RESULT_MD_PREVIEW_LIMIT]

        lines.append(f"### Result {idx}（tool={tool}, purpose={purpose!r}）")
        if err:
            lines.append(f"- ❌ 失败：{err}")
        else:
            lines.append(f"- source_url: {url}")
            score = r.get("score", 0.0)
            lines.append(f"- score: {score}")
            if md_preview:
                lines.append(f"- markdown 摘要：\n```\n{md_preview}\n```")
            else:
                lines.append("- markdown 为空")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Node 3: intent_observer（sync, LLM）
# ---------------------------------------------------------------------------


def create_intent_observer(
    llm: Any,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """intent_observer 节点工厂：状态聚合 + LLM 评估充分性（契约 §6）。

    Args:
        llm: LangChain BaseChatModel 实例。**T27 fail-fast**：不接受 None。

    Returns:
        intent_observer_node(state) -> {
            "evidence_pool": list[dict],
            "visited_urls": list[str],
            "iteration_count": int,
            "consecutive_intent_errors": int,
            "last_intent_observation": dict,
            "intent_sufficient": bool,
        }

    状态聚合规则：
    - evidence_pool / visited_urls：覆盖式写完整结构（不用 reducer，QaGraph 设计）
    - iteration_count：进入 observer 即 +1（早退路径也 +1，防主图死循环）
    - consecutive_intent_errors：本跳所有 results 都 error → +1；任一成功 → 归 0；
      results 为空（early_exit）→ 保持原值
    - 去重：url 已在历史 visited_urls 中 → 跳过追加 evidence_pool（visited_urls 不重复加）
    - LLM 调用条件：current_action_results 非空才调；early_exit / 空 results 跳过 LLM，
      last_intent_observation 透传上跳值（避免无意义 LLM 调用）
    - intent_sufficient = (confidence >= 0.85) AND (remaining_gaps == [])
    """
    if llm is None:
        raise ValueError(
            "create_intent_observer requires non-None llm. "
            "T27 fail-fast：LLM 是 Agent 核心依赖（CLAUDE.md 规则 14）。"
        )

    def intent_observer_node(state: dict[str, Any]) -> dict[str, Any]:
        # 读 state
        results = list(state.get("current_action_results", []) or [])
        normalized_query = state.get("normalized_query") or state.get("question", "")
        sub_questions = list(state.get("sub_questions", []) or [])
        plan = state.get("current_intent_plan", {}) or {}
        plan_actions = plan.get("next_actions", []) or []

        # 复制可变字段（与 hop_observer 风格一致：避免修改 state 原引用）
        evidence_pool = list(state.get("evidence_pool", []) or [])
        visited_urls = list(state.get("visited_urls", []) or [])
        iteration_count = int(state.get("iteration_count", 0))
        consecutive_errors = int(state.get("consecutive_intent_errors", 0))
        last_obs_prev = state.get("last_intent_observation", {}) or {}

        # iteration +1（无论是否调 LLM，避免死循环）
        iteration_count += 1

        # 转 Evidence + 追加（按 results 顺序，purpose 优先取 result 自带 →
        # 其次按 index 配 plan_actions[i].purpose）
        existing_urls = {e.get("url") for e in evidence_pool if e.get("url")}
        existing_sha = {e.get("sha256_hash") for e in evidence_pool if e.get("sha256_hash")}

        new_evidences: list[dict] = []
        for idx, r in enumerate(results):
            fallback_purpose = ""
            if idx < len(plan_actions):
                fallback_purpose = plan_actions[idx].get("purpose", "")
            ev = _tool_result_to_evidence(r, purpose=fallback_purpose)
            if ev is None:
                continue
            url = ev["url"]
            sha = ev["sha256_hash"]
            # 去重：url 已存在 → 跳过；sha 二次去重（url 不同但内容相同）
            if url and url in existing_urls:
                continue
            if sha and sha in existing_sha:
                continue
            new_evidences.append(ev)
            if url:
                existing_urls.add(url)
                # local_search 这种伪 URL 不写 visited_urls（避免污染 planner 去重）
                if url not in visited_urls and url != "local_milvus":
                    visited_urls.append(url)
            if sha:
                existing_sha.add(sha)

        evidence_pool.extend(new_evidences)

        # consecutive_errors 计数
        if not results:
            # early_exit / 空 actions：保持原值（不归 0 也不 +1）
            pass
        else:
            any_success = any(
                (not (r.get("error") or "").strip()) and (r.get("markdown") or "").strip()
                for r in results
            )
            if any_success:
                consecutive_errors = 0
            else:
                consecutive_errors += 1

        # LLM 调用：early_exit / 空 results 跳过，透传上跳 observation（避免无意义 LLM）
        if not results:
            logger.info(
                "intent_observer: results 为空（early_exit），跳过 LLM 调用，"
                "iteration_count=%d, evidence_pool=%d",
                iteration_count, len(evidence_pool),
            )
            obs_dict = dict(last_obs_prev) if last_obs_prev else {
                "new_evidence_count": 0,
                "coverage_summary": "",
                "remaining_gaps": list(sub_questions),
                "confidence": 0.0,
            }
            # 透传时 new_evidence_count 一定是 0（本跳 observer 没新增 evidence）
            obs_dict["new_evidence_count"] = 0
        else:
            # LLM user_prompt 渲染
            sub_q_render = (
                "\n".join(f"  {i+1}. {q}" for i, q in enumerate(sub_questions))
                if sub_questions else "  (decompose 未拆分子问题)"
            )
            results_render = _format_results_for_observer_prompt(results)
            evidence_summary_render = _format_evidence_pool(evidence_pool)

            user_prompt = (
                f"## 用户原始问题\n{normalized_query}\n\n"
                f"## 子问题列表（sub_questions——remaining_gaps 必须从此精确摘录）\n"
                f"{sub_q_render}\n\n"
                f"## 本跳工具结果（current_action_results，{len(results)} 条）\n"
                f"{results_render}\n\n"
                f"## 当前 evidence_pool 状态（含本跳新增 {len(new_evidences)} 条，共 {len(evidence_pool)} 条）\n"
                f"{evidence_summary_render}\n\n"
                f"## 已访问 URL\n{visited_urls if visited_urls else '(无)'}\n"
            )

            obs: IntentObservation = invoke_structured(
                llm, IntentObservation, INTENT_OBSERVER_SYSTEM_PROMPT, user_prompt,
            )
            obs_dict = obs.model_dump()

        # 翻译 intent_sufficient（contract §6 拍板）
        confidence = float(obs_dict.get("confidence", 0.0))
        remaining_gaps = obs_dict.get("remaining_gaps", []) or []
        intent_sufficient = bool(
            confidence >= _INTENT_SUFFICIENT_CONFIDENCE_THRESHOLD
            and not remaining_gaps
        )

        return {
            "evidence_pool": evidence_pool,
            "visited_urls": visited_urls,
            "iteration_count": iteration_count,
            "consecutive_intent_errors": consecutive_errors,
            "last_intent_observation": obs_dict,
            "intent_sufficient": intent_sufficient,
        }

    return intent_observer_node


# ---------------------------------------------------------------------------
# Node 4: merge_evidence_node（sync, 纯格式转换，无 LLM）
# ---------------------------------------------------------------------------


def merge_evidence_node(state: dict[str, Any]) -> dict[str, Any]:
    """merge_evidence：evidence_pool → get_info_candidates 格式（契约 §7）。

    纯格式转换，无 LLM。输出对齐 fanout_persist_dispatcher 的 candidate dict
    13 字段格式（与 barrier_extract / fetch_user_urls 一致），让下游 persist
    pipeline 无感知接入。

    字段映射（执行计划 §1.4）：
    - Evidence.content → markdown
    - Evidence.sha256_hash → content_sha256
    - Evidence.score (float) → score (int, round)
    - Evidence.source_type → type
    - Evidence.snippet → summary
    - 推断字段：fetched_at=now, from_engines=[], keywords=[], whether_in=True

    排序：按 score 降序（与 barrier_extract 行为一致）。
    """
    pool = list(state.get("evidence_pool", []) or [])
    fetched_at = datetime.now(timezone.utc).isoformat()

    candidates: list[dict[str, Any]] = []
    for ev in pool:
        content = ev.get("content", "") or ""
        snippet = ev.get("snippet", "") or ""
        # 内容和摘要都为空 → 无意义证据，过滤（observer 已过滤但兜底防御）
        if not content and not snippet:
            continue

        score_float = float(ev.get("score", 0.0) or 0.0)
        tool_name = ev.get("tool_name", "")
        candidates.append({
            "url": ev.get("url", ""),
            "title": ev.get("title", ""),
            "fetched_at": fetched_at,
            "markdown": content,
            "content_sha256": ev.get("sha256_hash", ""),
            "from_engines": [],
            "from_queries": list(ev.get("from_queries", []) or []),
            "score": int(round(score_float)),
            "type": ev.get("source_type", "community"),
            "summary": snippet,
            "keywords": [],
            "whether_in": True,
            "reason": f"intent agent loop evidence (tool={tool_name}, score={score_float:.0f})",
            # T48.3：透传 raw_path —— write_raw_one fast-path 用（已落盘则跳 fetch+write）
            "raw_path": ev.get("raw_path", ""),
        })

    # 同分稳定保序（Python sort 是稳定排序，与 barrier_extract 行为一致）
    candidates.sort(key=lambda c: int(c.get("score", 0) or 0), reverse=True)

    return {
        "get_info_candidates": candidates,
        "get_info_attempted": True,
    }
