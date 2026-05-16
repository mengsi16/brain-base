# -*- coding: utf-8 -*-
"""T46: 迭代多跳循环 4 节点。

hop_planner → tool_selector → tool_executor → hop_observer → (should_continue_hopping)

契约引用 §6.2–6.5。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from brain_base.agents.schemas import HopObservation, HopPlan
from brain_base.agents.utils.structured import invoke_structured
from brain_base.nodes.qa_tools import TOOL_REGISTRY
from brain_base.prompts.hop_planner_prompts import HOP_PLANNER_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Node 1: hop_planner (sync, LLM)
# ---------------------------------------------------------------------------


def create_hop_planner(llm: Any) -> Callable:
    """hop_planner 节点工厂：LLM 结构化输出 HopPlan（§6.2）。

    从 state 读 pending_goals[0] 作为当前目标，结合 resolved_entities +
    hops 历史 + 可用工具列表，让 LLM 规划本跳。
    """

    def hop_planner_node(state: dict[str, Any]) -> dict[str, Any]:
        pending_goals = state.get("pending_goals", []) or []
        if not pending_goals:
            # 无目标 → 空选择，should_continue_hopping 会判断退出
            return {"current_tool_selection": {}}

        current_goal = pending_goals[0]
        normalized_query = state.get("normalized_query", state.get("question", ""))
        hops = state.get("hops", []) or []
        resolved_entities = state.get("resolved_entities", {}) or {}

        # 构建 hops 摘要
        hops_summary = ""
        if hops:
            lines = []
            for i, h in enumerate(hops):
                lines.append(
                    f"  跳 {i+1}: 目标={h.get('goal','')} → "
                    f"工具={h.get('tool_name','')} → "
                    f"实体={h.get('resolved_entity','')} "
                    f"(confidence={h.get('confidence',0)})"
                )
            hops_summary = "\n".join(lines)

        # 构建可用工具列表
        tools_desc = "\n".join(
            f"  - {spec.name}: {spec.description}"
            for spec in TOOL_REGISTRY.values()
        )

        # 替换 next_goals 模板中的 {resolved_entity}
        goal_display = current_goal
        for entity_key, entity_val in resolved_entities.items():
            goal_display = goal_display.replace(f"{{{entity_key}}}", entity_val)

        user_prompt = (
            f"用户原始问题：{normalized_query}\n\n"
            f"当前目标：{goal_display}\n\n"
            f"已解决实体：{resolved_entities}\n\n"
            f"已完成跳步：\n{hops_summary or '  （无）'}\n\n"
            f"可用工具：\n{tools_desc}"
        )

        result: HopPlan = invoke_structured(
            llm, HopPlan, HOP_PLANNER_SYSTEM_PROMPT, user_prompt,
        )

        return {
            "current_tool_selection": {
                "goal": result.goal,
                "tool_name": result.tool_name,
                "tool_args": result.tool_args,
                "stop_entity": result.stop_entity,
                "next_goals": list(result.next_goals),
                "reason": result.reason,
            },
        }

    return hop_planner_node


# ---------------------------------------------------------------------------
# Node 2: tool_selector (sync, 确定性校验)
# ---------------------------------------------------------------------------


def tool_selector_node(state: dict[str, Any]) -> dict[str, Any]:
    """tool_selector：确定性白名单校验 + fallback（§6.3）。

    校验规则：
    1. tool_name ∈ TOOL_REGISTRY
    2. 工具 requires 的基础设施可用
    3. GPU 工具检查并发约束（本版不强制）
    4. hop_count < max_hops

    fallback：校验失败 → 降级到 web_search（最通用），保留 goal / stop_entity /
    next_goals / reason 不变。
    """
    selection = dict(state.get("current_tool_selection", {}) or {})
    if not selection:
        return {"current_tool_selection": selection}

    tool_name = selection.get("tool_name", "")
    infra = state.get("infra_status", state.get("infra", {})) or {}

    needs_fallback = False
    fallback_reason = ""

    # 校验 1：工具名在注册表中
    if tool_name not in TOOL_REGISTRY:
        needs_fallback = True
        fallback_reason = f"tool_name '{tool_name}' not in TOOL_REGISTRY"
    else:
        spec = TOOL_REGISTRY[tool_name]
        # 校验 2：基础设施可用
        for req in spec.requires:
            avail_key = f"{req}_available"
            if not infra.get(avail_key, True):
                needs_fallback = True
                fallback_reason = f"infra '{req}' not available"
                break

    if needs_fallback:
        logger.warning(
            "tool_selector fallback to web_search: %s (original=%s)",
            fallback_reason, tool_name,
        )
        # fallback：只替换 tool_name + tool_args，保留其余字段
        selection["tool_name"] = "web_search"
        # 用 goal 作为搜索 query
        selection["tool_args"] = {"query": selection.get("goal", "")}

    return {"current_tool_selection": selection}


# ---------------------------------------------------------------------------
# Node 3: tool_executor (async)
# ---------------------------------------------------------------------------


def create_tool_executor(llm: Any, cfg: Any) -> Callable:
    """tool_executor 节点工厂：按 tool_name dispatch 到 TOOL_REGISTRY（§6.4）。

    async 节点。sync 工具用 asyncio.to_thread 包装。
    工具返回原始内容后调 LLM 提取 HopObservation。
    """
    from brain_base.prompts.get_info_prompts import FETCH_EXTRACT_SYSTEM_PROMPT

    async def tool_executor_node(state: dict[str, Any]) -> dict[str, Any]:
        selection = state.get("current_tool_selection", {}) or {}
        tool_name = selection.get("tool_name", "")
        tool_args = selection.get("tool_args", {}) or {}
        stop_entity = selection.get("stop_entity", "")

        if not tool_name or tool_name not in TOOL_REGISTRY:
            return {
                "current_tool_result": {
                    "error": f"invalid tool_name: {tool_name}",
                    "evidence": "",
                    "resolved_entity": "",
                },
            }

        spec = TOOL_REGISTRY[tool_name]

        # dispatch 执行
        try:
            if spec.is_async:
                raw_result = await spec.fn(tool_args, llm, cfg)
            else:
                raw_result = await asyncio.to_thread(spec.fn, tool_args, llm, cfg)
        except Exception as exc:
            logger.warning(
                "tool_executor %s failed: %s: %s",
                tool_name, type(exc).__name__, str(exc)[:200],
            )
            return {
                "current_tool_result": {
                    "error": str(exc)[:200],
                    "evidence": "",
                    "resolved_entity": "",
                },
            }

        # 工具返回错误
        if raw_result.get("error"):
            return {
                "current_tool_result": {
                    "error": raw_result["error"],
                    "evidence": "",
                    "resolved_entity": "",
                    "markdown": raw_result.get("markdown", ""),
                    "source_url": raw_result.get("source_url", ""),
                    "title": raw_result.get("title", ""),
                },
            }

        markdown = raw_result.get("markdown", "")

        # LLM 提取 HopObservation（限职：只提取 resolved_entity + evidence_summary）
        if markdown.strip():
            try:
                obs_prompt = (
                    f"从以下内容中提取信息。\n\n"
                    f"需要提取的实体：{stop_entity}\n\n"
                    f"内容：\n{markdown[:8000]}"
                )
                obs: HopObservation = await asyncio.to_thread(
                    invoke_structured,
                    llm,
                    HopObservation,
                    "你是信息提取器。从给定内容中提取目标实体和关键证据摘要。",
                    obs_prompt,
                )
                resolved_entity = obs.resolved_entity
                evidence_summary = obs.evidence_summary
                confidence = obs.confidence
            except Exception as exc:
                logger.warning(
                    "tool_executor LLM extraction failed: %s", str(exc)[:200],
                )
                resolved_entity = ""
                evidence_summary = raw_result.get("summary", markdown[:500])
                confidence = 0.3
        else:
            resolved_entity = ""
            evidence_summary = ""
            confidence = 0.0

        return {
            "current_tool_result": {
                "evidence": evidence_summary,
                "markdown": markdown,
                "source_url": raw_result.get("source_url", ""),
                "title": raw_result.get("title", ""),
                "resolved_entity": resolved_entity,
                "confidence": confidence,
            },
        }

    return tool_executor_node


# ---------------------------------------------------------------------------
# Node 4: hop_observer (sync, 纯状态更新)
# ---------------------------------------------------------------------------


def hop_observer_node(state: dict[str, Any]) -> dict[str, Any]:
    """hop_observer：纯状态更新，无 LLM（§6.5）。

    1. 从 current_tool_result + current_tool_selection 读取本跳结果
    2. 追加 hops 记录
    3. 更新 resolved_entities
    4. hop_count + 1
    5. pop pending_goals[0] + append next_goals
    6. 更新 consecutive_tool_errors
    """
    result = state.get("current_tool_result", {}) or {}
    selection = state.get("current_tool_selection", {}) or {}

    # 复制可变字段（避免修改 state 原引用）
    hops = list(state.get("hops", []) or [])
    resolved_entities = dict(state.get("resolved_entities", {}) or {})
    pending_goals = list(state.get("pending_goals", []) or [])
    hop_count = state.get("hop_count", 0)
    consecutive_errors = state.get("consecutive_tool_errors", 0)

    # 追加 hop 记录
    hop_record = {
        "goal": selection.get("goal", ""),
        "tool_name": selection.get("tool_name", ""),
        "tool_args": selection.get("tool_args", {}),
        "stop_entity": selection.get("stop_entity", ""),
        "resolved_entity": result.get("resolved_entity", ""),
        "evidence": result.get("evidence", ""),
        "confidence": result.get("confidence", 0.0),
        "source_url": result.get("source_url", ""),
        "title": result.get("title", ""),
        "markdown": result.get("markdown", ""),
        "error": result.get("error", ""),
    }
    hops.append(hop_record)

    # 更新 resolved_entities
    re_key = selection.get("stop_entity", "")
    re_val = result.get("resolved_entity", "")
    if re_key and re_val:
        resolved_entities[re_key] = re_val

    # hop_count + 1
    hop_count += 1

    # pop 当前目标 + append next_goals
    if pending_goals:
        pending_goals.pop(0)
    next_goals = selection.get("next_goals", []) or []
    # 替换 next_goals 中的 {resolved_entity} 模板
    for ng in next_goals:
        resolved_ng = ng
        for ek, ev in resolved_entities.items():
            resolved_ng = resolved_ng.replace(f"{{{ek}}}", ev)
        pending_goals.append(resolved_ng)

    # 更新 consecutive_tool_errors
    if result.get("error"):
        consecutive_errors += 1
    else:
        consecutive_errors = 0

    return {
        "hops": hops,
        "resolved_entities": resolved_entities,
        "pending_goals": pending_goals,
        "hop_count": hop_count,
        "consecutive_tool_errors": consecutive_errors,
    }


# ---------------------------------------------------------------------------
# Node 5: merge_hop_evidence (sync, 纯格式转换)
# ---------------------------------------------------------------------------


def merge_hop_evidence_node(state: dict[str, Any]) -> dict[str, Any]:
    """merge_hop_evidence：将 hops → get_info_candidates 格式（§6.7）。

    纯格式转换，无 LLM。输出对齐 barrier_extract 的 candidate dict 格式，
    供下游 fanout_persist_dispatcher 消费。

    只保留有 evidence 且无 error 的 hop（与 barrier_extract 过滤 whether_in=False 对齐）。
    """
    from datetime import datetime, timezone

    hops = state.get("hops", []) or []

    candidates: list[dict[str, Any]] = []
    for hop in hops:
        if hop.get("error"):
            continue
        evidence = hop.get("evidence", "")
        markdown = hop.get("markdown", "")
        if not evidence and not markdown:
            continue

        candidates.append({
            "url": hop.get("source_url", ""),
            "title": hop.get("title", ""),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "markdown": markdown,
            "content_sha256": "",
            "from_engines": [],
            "from_queries": [],
            "score": int(hop.get("confidence", 0.8) * 100),
            "type": "hop_evidence",
            "summary": evidence,
            "keywords": [],
            "whether_in": True,
            "reason": f"hop goal: {hop.get('goal', '')}",
        })

    # 按 score 降序（同 barrier_extract 行为）
    candidates.sort(key=lambda c: int(c.get("score", 0)), reverse=True)

    return {
        "get_info_candidates": candidates,
        "get_info_attempted": True,
    }
