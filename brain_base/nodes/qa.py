"""
QA 主图节点函数。

流程：probe → crystallized_check → normalize → decompose → rewrite →
       search → judge → answer → self_check → crystallize_answer

设计原则：
- 纯逻辑节点（probe / crystallized_check / search / crystallize_answer）：模块级函数。
- LLM 节点：`create_xxx_node(llm)` 工厂，内部用 `invoke_structured(...)` 拿到
  Pydantic schema 实例，避免在 prompt 里塞 JSON 格式段。
- T27 fail-fast：节点工厂不再接受 `llm=None` 降级；llm=None / LLM 异常都让
  入口（QaGraph.__init__）/ invoke_structured 直接 raise，不在节点内吞异常。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path as _P
from typing import Any, Callable
from urllib.parse import urlparse

import time

from brain_base.agents.schemas import (
    DecomposedQuestion,
    EvidenceJudgment,
    NormalizedQuestion,
    RewrittenQueries,
    SelfCheckResult,
)
from brain_base.agents.utils.structured import invoke_structured
from brain_base.config import GetInfoConfig
from brain_base.nodes._probe import probe_milvus, probe_playwright
from brain_base.prompts.qa_prompts import (
    ANSWER_MULTI_SUB_USER_PROMPT_TEMPLATE,
    ANSWER_SYSTEM_PROMPT,
    ANSWER_USER_PROMPT_TEMPLATE,
    DECOMPOSE_SYSTEM_PROMPT,
    JUDGE_EVIDENCE_SYSTEM_PROMPT,
    NORMALIZE_SYSTEM_PROMPT,
    REWRITE_SYSTEM_PROMPT,
    SELF_CHECK_SYSTEM_PROMPT,
)
from brain_base.tools.milvus_client import list_docs, multi_query_search


# ---------------------------------------------------------------------------
# T12 多跳问题分解 fan-out 常量
# ---------------------------------------------------------------------------

# 单子问题最多 8 条 evidence；4 子问题合计 ≤ 32
_FANOUT_FINAL_K_PER_SUB = 8
_FANOUT_TOP_K_PER_QUERY = 20
_FANOUT_RRF_K = 60
# 总改写数上限（4 子问题 × 6 改写 = 24，传给 re_search 兜底）
_FANOUT_REWRITTEN_QUERIES_CAP = 24
# 多子问题模式下，单条 evidence 渲染字符上限（防 prompt token 超限）
_FANOUT_EVIDENCE_CHAR_LIMIT = 400


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


# T28 删：legacy_dense_search_node——T23 引入的临时桥接节点（扁平搜索被
# 强子问题挤掉弱子问题），由 PIPE2 第二段子图（fanout_search × N + 每子问题独立
# milvus + rerank + barrier2）替换。新实现见 brain_base/nodes/qa_search.py。


def create_crystallize_answer_node(llm: Any) -> Callable:
    """固化层写入节点工厂（T34：接入 LLM 真实评分 + 生成）。

    内部调用链：value_score(LLM) → skill_gen(LLM) → write。
    固化层是 QA 软依赖（CLAUDE.md 规则 14），LLM 失败不阻断 QA 返回答案。
    """
    import logging

    from brain_base.nodes.crystallize import (
        create_skill_gen_node,
        create_value_score_node,
        crystallize_write_node,
    )

    _logger = logging.getLogger(__name__)
    _value_score_fn = create_value_score_node(llm)
    _skill_gen_fn = create_skill_gen_node(llm)

    def crystallize_answer_node(state: dict[str, Any]) -> dict[str, Any]:
        """委托固化层写入答案（LLM 真实评分 + skill 生成）。"""
        answer = state.get("answer", "")
        question = state.get("question", "")
        if not answer:
            return {}

        try:
            # 第一步：LLM 价值评分（四维度）
            vs_result = _value_score_fn(
                {"user_question": question, "answer_markdown": answer}
            )
            value_score = vs_result.get("value_score", 0.0)
            if value_score < 0.3:
                _logger.info(
                    "crystallize 跳过：value_score=%.2f < 0.3 | question=%r",
                    value_score, question[:80],
                )
                return {
                    "crystallize_result": {
                        "status": "skipped",
                        "skip_reason": f"value_score={value_score:.2f} < 0.3",
                    }
                }

            # 第二步：LLM 生成 skill 骨架（trigger_keywords / description / answer_markdown）
            sg_result = _skill_gen_fn(
                {
                    "user_question": question,
                    "answer_markdown": answer,
                    "recommended_layer": vs_result.get("recommended_layer", "cold"),
                }
            )

            # 第三步：写入 data/crystallized/
            write_state: dict[str, Any] = {
                "user_question": question,
                "answer_markdown": answer,
                "value_score": value_score,
                "skill_payload": sg_result.get("skill_payload"),
            }
            result = crystallize_write_node(write_state)
            _logger.info(
                "crystallize 写入完成：skill_id=%s layer=%s value_score=%.2f",
                result.get("skill_id", "?"),
                result.get("layer", "?"),
                value_score,
            )
            return {"crystallize_result": result}

        except Exception as exc:
            # 软依赖：固化失败不阻断 QA（CLAUDE.md 规则 14）
            _logger.warning(
                "crystallize_answer_node 失败（不阻断 QA）: %s: %s | question=%r",
                type(exc).__name__, str(exc)[:200], question[:80],
            )
            return {
                "crystallize_result": {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            }

    return crystallize_answer_node


# ---------------------------------------------------------------------------
# LLM 节点工厂（schema 强制结构化输出）
# ---------------------------------------------------------------------------


def create_normalize_node(llm: Any) -> Callable:
    """规范化用户问题节点工厂（T27 fail-fast：llm 必须为非空）。"""

    def normalize_node(state: dict[str, Any]) -> dict[str, Any]:
        question = state.get("question", "")

        # T31：注入今天日期作错点——让 LLM 不依赖训练截止日期算 time_range。
        # 使用 UTC 转本地时区后取 ISO 日期（与 prompt 中「今天」语义一致）。
        today_iso = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        user_prompt = f"今天日期：{today_iso}\n用户问题：{question}"

        result = invoke_structured(
            llm,
            NormalizedQuestion,
            NORMALIZE_SYSTEM_PROMPT,
            user_prompt,
        )

        return {
            "normalized_query": result.normalized,
            "expected_type": result.expected_type,
            "time_sensitive": result.time_sensitive,
            "language": result.language,
            # T31 新增字段：time_range / abbreviation_hints
            "time_range": result.time_range,
            "abbreviation_hints": result.abbreviation_hints,
        }

    return normalize_node


def create_decompose_node(llm: Any) -> Callable:
    """复杂问题分解节点工厂（T27 fail-fast：llm 必须为非空）。

    T23：统一走 fanout_prep 路径——"不分解" = 1 个子问题（原题），
    输出 ``sub_questions: list[str]`` 长度 ≥ 1。字段重命名自旧的 ``sub_queries``
    （旧字段被 barrier1 输出的 ``sub_queries: list[list[dict]]`` 占用）。
    """

    def decompose_node(state: dict[str, Any]) -> dict[str, Any]:
        question = state.get("normalized_query", state.get("question", ""))

        # T24：补 expected_type / time_sensitive 上下文——让 LLM 能据此判定第 5 类
        # 时序变化优先级（time_sensitive=True 时优先拆取当前状态 + 历史演进）
        expected_type = state.get("expected_type", "") or "未指定"
        time_sensitive = bool(state.get("time_sensitive", False))
        user_prompt = (
            f"用户问题：{question}\n"
            f"期望答案类型：{expected_type}\n"
            f"时效敏感：{time_sensitive}"
        )
        result = invoke_structured(
            llm,
            DecomposedQuestion,
            DECOMPOSE_SYSTEM_PROMPT,
            user_prompt,
        )

        if result.needs_decompose and result.sub_questions:
            return {
                "sub_questions": [sq.text for sq in result.sub_questions],
                "decomposition_needed": True,
            }
        # 不分解 → 原题作为唯一子问题
        return {
            "sub_questions": [question] if question else [],
            "decomposition_needed": False,
        }

    return decompose_node


def _dedup_evidence_by_chunk_id(evidence: list[dict]) -> list[dict]:
    """跨子问题对 evidence 按 chunk_id 去重，保留最高 score 的副本。

    T19 修复：rag-anything e2e 暴露的问题——同一 chunk 被多个子问题同时命中，
    合并 evidence 后同 chunk 重复出现 N 次（实测 "VLM 增强查询模式" / "arXiv 论文"
    各重复 3 次），浪费 30%+ answer prompt token。

    策略：
    - 有 chunk_id 的 evidence：按 chunk_id 聚合，保留 score 最高的一条；
      score 相同时保留第一次出现的（保持 RRF 输入顺序稳定）。
    - 无 chunk_id 也无 id 的 evidence：原样保留追加在末尾（向后兼容，
      例如 FS grep 结果 / 未来其他来源）。
    """
    best: dict[str, dict] = {}
    no_cid: list[dict] = []
    first_order: dict[str, int] = {}
    for i, ev in enumerate(evidence):
        cid = ev.get("chunk_id") or str(ev.get("id") or "")
        if not cid:
            no_cid.append(ev)
            continue
        if cid not in first_order:
            first_order[cid] = i
        prev = best.get(cid)
        if prev is None or float(ev.get("score", 0.0)) > float(prev.get("score", 0.0)):
            best[cid] = ev
    # 按首次出现顺序输出 dedup 后的 evidence，保持与原 merged_evidence 的相对顺序
    ordered_cids = sorted(first_order.keys(), key=lambda c: first_order[c])
    return [best[cid] for cid in ordered_cids] + no_cid


# T23 删除：create_subquery_fanout_node / _lexical_block_judgment
# 职责迁移到：
# - rewrite + sparse gate：brain_base/nodes/qa_prep.py::create_prep_one_subquery（fanout_prep 子节点）
# - 子问题分发：brain_base/nodes/qa_prep.py::fanout_prep_dispatcher
# - barrier 聚合：brain_base/nodes/qa_prep.py::barrier1_node
# - Milvus 检索：T28 PIPE2 的 subquery_search_one × N + barrier2 接管
# - lexical 强约束门槛：T23 grep AND gate (grep_hits=0 → needs_get_info=true) →
#   T30 sparse gate (milvus text_search top-3 avg < 0.20 → needs_get_info=true) 替代


def create_judge_node(llm: Any) -> Callable:
    """证据充分性判断节点工厂（T27 fail-fast：llm 必须为非空）。

    多子问题模式（``sub_question_evidence`` 非空）：
    - 任一子问题 ``evidence_count == 0`` → 整体 ``evidence_sufficient=False``，
      触发 T10 已交付的 get_info 回路（"分组缺证据 → 自动外检"）。
    - 所有子问题都有证据 → 走 LLM 综合 sufficiency 判断（evidence 按 sub_idx 分组渲染）。

    单链路模式（无 sub_question_evidence）：保持原行为不变。

    T27：删 LLM 调用的 try/except 兜底 + 删 if llm is None 降级两处。
    LLM 异常直接上拋到 LangGraph runtime，不在节点内吞掉返回伪 sufficiency。
    """

    def judge_node(state: dict[str, Any]) -> dict[str, Any]:
        evidence = state.get("evidence", [])
        question = state.get("question", "")
        # T24：多跳模式判断改用 sub_questions 长度 > 1（sub_question_evidence T23 之后不再写入，
        # 其分支事实上已死代码。user_prompt 拼装里补 sub_questions 让 LLM 能逐子问题评覆盖度）
        sub_questions = state.get("sub_questions", []) or []
        sub_groups: list[dict] = state.get("sub_question_evidence", []) or []

        # ---- 多子问题模式：先做组级别缺证据检测 ----
        if sub_groups:
            missing = [g for g in sub_groups if g.get("evidence_count", 0) == 0]
            if missing:
                missing_qs = [g.get("sub_question", f"sub-{g.get('idx', '?')}")
                              for g in missing]
                coverage = 1.0 - len(missing) / max(1, len(sub_groups))
                return {
                    "evidence_sufficient": False,
                    "evidence_recommendation": "trigger_get_info",
                    "coverage_score": coverage,
                    "judge_reason": f"子问题缺证据：{missing_qs}",
                }
            # 全部子问题都有证据 → 走 LLM 综合判断（evidence_summary 按子问题分组）
            evidence_summary = _render_grouped_evidence_for_judge(
                sub_groups, evidence
            )
        else:
            # ---- 单链路模式：原行为 ----
            evidence_summary = "\n".join(
                f"- [{e.get('source', '?')}] {_evidence_body(e, max_chars=200)}"
                for e in evidence[:10]
            )

        # T24：多跳模式拼进 sub_questions 列表交给 LLM（逐子问题评覆盖度）
        if len(sub_questions) > 1:
            sub_q_list = "\n".join(
                f"  [s{i}] {sq}" for i, sq in enumerate(sub_questions)
            )
            judge_user_prompt = (
                f"用户原始问题：{question}\n\n"
                f"子问题列表（按 sub_idx 索引）：\n{sub_q_list}\n\n"
                f"证据列表：\n{evidence_summary}"
            )
        else:
            judge_user_prompt = (
                f"用户问题：{question}\n\n证据列表：\n{evidence_summary}"
            )

        # T27 fail-fast：LLM 异常直接上拋到 LangGraph runtime。
        result = invoke_structured(
            llm,
            EvidenceJudgment,
            JUDGE_EVIDENCE_SYSTEM_PROMPT,
            judge_user_prompt,
        )

        return {
            "evidence_sufficient": result.sufficient,
            "evidence_recommendation": result.recommendation,
            "coverage_score": result.coverage,
            "judge_reason": result.reason,
        }

    return judge_node


def _render_grouped_evidence_for_judge(
    sub_groups: list[dict],
    evidence: list[dict],
) -> str:
    """按 sub_idx 分组渲染 evidence 给 judge 的 LLM 看。

    每子问题单 5 条预览 + 用 ``[s{idx}-{n}]`` 编号，便于 LLM 看清覆盖情况。
    """
    by_sub: dict[int, list[dict]] = {}
    for e in evidence:
        idx = e.get("sub_idx")
        if idx is None:
            continue
        by_sub.setdefault(int(idx), []).append(e)

    blocks: list[str] = []
    for g in sub_groups:
        idx = int(g.get("idx", 0))
        sub_q = g.get("sub_question", f"sub-{idx}")
        items = by_sub.get(idx, [])[:5]
        if not items:
            blocks.append(f"## 子问题 {idx + 1}：{sub_q}\n（无证据）")
            continue
        lines = [f"## 子问题 {idx + 1}：{sub_q}"]
        for n, e in enumerate(items, 1):
            lines.append(
                f"[s{idx + 1}-{n}] {_evidence_body(e, max_chars=200)}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _render_evidence_for_answer(
    evidence: list[dict],
    sub_groups: list[dict],
) -> tuple[str, bool]:
    """为 answer 节点渲染 evidence 文本。

    返回 ``(evidence_text, multi_sub_mode)``。
    - 多子问题模式（sub_groups 非空）：按 sub_idx 分组，每条用 ``[s{idx}-{n}]``
      编号，单条字符上限 ``_FANOUT_EVIDENCE_CHAR_LIMIT``（防 prompt token 超限）。
    - 单链路模式：保持原 ``[i] source | body`` 格式，单条 800 字符。
    """
    if sub_groups:
        by_sub: dict[int, list[dict]] = {}
        for e in evidence:
            idx = e.get("sub_idx")
            if idx is None:
                continue
            by_sub.setdefault(int(idx), []).append(e)

        blocks: list[str] = []
        for g in sub_groups:
            idx = int(g.get("idx", 0))
            sub_q = g.get("sub_question", f"sub-{idx}")
            items = by_sub.get(idx, [])
            if not items:
                blocks.append(
                    f"### 子问题 {idx + 1}：{sub_q}\n（无证据）"
                )
                continue
            lines = [f"### 子问题 {idx + 1}：{sub_q}"]
            for n, e in enumerate(items, 1):
                lines.append(
                    f"[s{idx + 1}-{n}] {e.get('source', '?')} | "
                    f"{_evidence_body(e, max_chars=_FANOUT_EVIDENCE_CHAR_LIMIT)}"
                )
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks), True

    # 单链路：原行为
    text = "\n".join(
        f"[{i+1}] {e.get('source', '?')} | {_evidence_body(e, max_chars=800)}"
        for i, e in enumerate(evidence[:10])
    )
    return text, False


def create_answer_node(llm: Any) -> Callable:
    """基于证据生成答案节点工厂（T27 fail-fast：llm 必须为非空）。

    answer 节点是自由文本输出（含 markdown 格式与证据表），不走
    `with_structured_output`——结构由 prompt 模板与渲染规则保证。

    多子问题模式（``sub_question_evidence`` 非空）：
    - evidence 按 sub_idx 分组渲染，每条编号 ``[s{idx}-{n}]``；
    - 给 LLM 的 user prompt 切换到多子问题模板，强制按子问题分段输出。

    T27：删原有 ``llm=None`` 降级分支 + LLM 调用 try/except 兜底；
    同时删除只被降级路径使用的 ``_render_degraded_multi_answer`` 辅助函数。
    """

    def answer_node(state: dict[str, Any]) -> dict[str, Any]:
        crystallized_status = state.get("crystallized_status", "miss")
        if crystallized_status in ("hit_fresh", "cold_promoted"):
            return {"answer": state.get("crystallized_answer", "")}

        evidence = state.get("evidence", [])
        question = state.get("question", "")
        sub_groups: list[dict] = state.get("sub_question_evidence", []) or []

        if not evidence:
            return {
                "answer": f"未能找到关于「{question}」的本地证据。",
                "evidence_sufficient": False,
            }

        evidence_text, multi_sub_mode = _render_evidence_for_answer(
            evidence, sub_groups
        )

        from langchain_core.messages import HumanMessage, SystemMessage

        if multi_sub_mode:
            sub_q_list = "\n".join(
                f"- 子问题 {int(g.get('idx', 0)) + 1}：{g.get('sub_question', '')}"
                for g in sub_groups
            )
            user_prompt = ANSWER_MULTI_SUB_USER_PROMPT_TEMPLATE.format(
                question=question,
                sub_questions=sub_q_list,
                evidence=evidence_text,
            )
        else:
            user_prompt = ANSWER_USER_PROMPT_TEMPLATE.format(
                question=question,
                evidence=evidence_text,
            )

        # T27 fail-fast：LLM 异常直接上拋到 LangGraph runtime，不吞掉返回伪答案。
        response = llm.invoke([
            SystemMessage(content=ANSWER_SYSTEM_PROMPT),
            HumanMessage(content=user_prompt),
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

    return answer_node


def create_self_check_node(llm: Any) -> Callable:
    """答案自检节点工厂（Maker-Checker，T27 fail-fast：llm 必须为非空）。

    - 降级模式跳过自检（CLAUDE.md 规则 35）。
    - 自检只能删除或标注，不能凭空添加（CLAUDE.md 规则 34）。
    - T27：删原有 ``or llm is None`` 跳过条件与 LLM 调用 try/except 兜底。
    """

    def self_check_node(state: dict[str, Any]) -> dict[str, Any]:
        answer = state.get("answer", "")
        question = state.get("question", "")
        evidence = state.get("evidence", [])
        # T24：多跳模式下 completeness 需要逐子问题检查小节是否都存在
        sub_questions = state.get("sub_questions", []) or []
        crystallized_status = state.get("crystallized_status", "miss")

        if crystallized_status in ("hit_fresh", "cold_promoted", "degraded"):
            return {"self_check_passed": True, "self_check_skipped": True}

        if not answer:
            return {"self_check_passed": True, "self_check_skipped": True}

        evidence_text = "\n".join(
            f"[{i+1}] {_evidence_body(e, max_chars=400)}"
            for i, e in enumerate(evidence[:10])
        )
        # T24：多跳模式拼进 sub_questions，让 LLM 能逐小节评 completeness
        if len(sub_questions) > 1:
            sub_q_list = "\n".join(
                f"  {i + 1}. {sq}" for i, sq in enumerate(sub_questions)
            )
            user_prompt = (
                f"用户原始问题：{question}\n\n"
                f"子问题列表：\n{sub_q_list}\n\n"
                f"已生成答案：\n{answer}\n\n"
                f"可用证据：\n{evidence_text}"
            )
        else:
            user_prompt = (
                f"用户问题：{question}\n\n"
                f"已生成答案：\n{answer}\n\n"
                f"可用证据：\n{evidence_text}"
            )
        # T27 fail-fast：LLM 异常直接上拋到 LangGraph runtime。
        result = invoke_structured(
            llm,
            SelfCheckResult,
            SELF_CHECK_SYSTEM_PROMPT,
            user_prompt,
        )

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


# T25 删：create_get_info_trigger_node / create_web_research_node 两个老节点工厂。
# 外检从 judge 后送底改为 search 前预检，dispatcher 的 5 重 gate（基于
# sub_needs_get_info）取代 trigger；SERP + fetch + Readability + LLM 的 fan-out
# 路径取代 web_research 调 GetInfoGraph。原 trigger 依赖的 GetInfoTrigger schema
# 仍保留在 schemas.py（以防老代码 / e2e 测试引用）。


def _url_priority_score(url: str) -> int:
    """根据 URL 模式给候选打分（T14：select_candidates 同 source_type 内重排）。

    分档（值越大越优先）：

    - **100**：GitHub README / wiki / blob 中的 docs 类文件
      （`github.com/*/blob/*/(README|readme|docs/*)`、`github.com/*/wiki/*`）
    - **80**：专门文档站
      （`*.readthedocs.io`、`*.gitbook.io`、`docs.*` 子域、URL path
      含 `/docs/` 或 `/documentation/`）
    - **60**：GitHub 仓库根（`github.com/<owner>/<repo>`，path 段数 ≤ 2）
    - **40**：默认（普通技术文章 / 博客）
    - **20**：landing page（host 后无路径，仅 ``""`` 或 ``"/"``）

    设计原则：纯字符串模式匹配，零外部依赖；同分稳定保序由调用方 ``sort`` 保证。
    解析失败 / 非 http / 空 URL 一律给默认 40，不抛异常（fail-safe，因为
    select_candidates 已在前置 step 过滤了空 URL，这里只兜底）。
    """
    if not url or not isinstance(url, str):
        return 40
    try:
        parsed = urlparse(url)
    except Exception:
        return 40
    if parsed.scheme not in ("http", "https"):
        return 40

    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    path_lower = path.lower()
    # 去掉首尾斜杠后的段数（landing page 判定用）
    segments = [s for s in path.split("/") if s]

    # ---- 100：GitHub README / wiki / blob docs ----
    if host == "github.com" and len(segments) >= 3:
        # owner / repo / (wiki | blob | tree) / ...
        third = segments[2].lower()
        if third == "wiki":
            return 100
        if third in ("blob", "tree") and len(segments) >= 5:
            # blob/<branch>/<path>...
            tail = "/".join(segments[4:]).lower()
            if tail.startswith("readme") or "/readme" in f"/{tail}":
                return 100
            if tail.startswith("docs/") or "/docs/" in f"/{tail}":
                return 100

    # ---- 80：专门文档站 ----
    if host.endswith(".readthedocs.io") or host.endswith(".gitbook.io"):
        return 80
    if host.startswith("docs."):
        return 80
    if "/docs/" in path_lower or "/documentation/" in path_lower:
        return 80

    # ---- 60：GitHub 仓库根 ----
    if host == "github.com" and 1 <= len(segments) <= 2:
        # 仅 github.com 或 github.com/owner 不算（至少要 owner/repo）
        if len(segments) == 2:
            return 60

    # ---- 20：landing page（host 后无路径） ----
    if not segments:
        return 20

    # ---- 40：默认 ----
    return 40


def _candidate_priority(c: dict[str, Any]) -> int:
    """T16 + T14 综合优先级。

    LLM ``priority_score`` 字段（GetInfoGraph preview_score_one 节点写入）优先；
    缺失时降级到 T14 静态 URL 模式分。两者都是 0–100 量纲，可直接比较。

    什么时候 ``priority_score`` 会缺失：
    - GetInfoGraph 的 ``llm=None`` 路径（fan-out 跳过 preview_score_one）
    - 单候选 LLM 评分抛错（preview_score_one 写入 ``score_error`` 不写 priority_score）
    - 老路径调用 select_candidates 时（向后兼容）

    返回 int 0–100，调用方按降序排序。
    """
    score = c.get("priority_score")
    if isinstance(score, int):
        return score
    return _url_priority_score(c.get("url", ""))


def create_select_candidates_node(
    config: GetInfoConfig | None = None,
) -> Callable:
    """select_candidates 节点工厂：按配额筛选 + 去重，输出最终入库目标。

    顺序：
    1. 过滤 discard
    2. 按已入库 URL 去重
    3. **T14：按 URL 优先级稳定降序排序**（GitHub README/docs > 文档站 >
       GitHub 仓库根 > 默认 > landing page），让高信息密度候选自动占据每个
       分类的前列。
    4. official-doc 取前 max_official 个
    5. community 取前 max_community 个
    6. 合并后取前 max_total 个（official 优先）
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

        # 3 T16/T14 排序：LLM priority_score 优先；缺失时降级到 T14 静态 URL 模式分
        # （Python sort 保稳定，同分保留原顺序）
        survived.sort(key=lambda c: -_candidate_priority(c))

        # 4+5 分类配额
        official = [c for c in survived if c.get("source_type") == "official-doc"][:cfg.max_official]
        community = [c for c in survived if c.get("source_type") == "community"][:cfg.max_community]

        # 6 合并 + 总数上限（official 优先）
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

    两种模式：
    - **单链路**（``sub_question_evidence`` 为空）：用 ``rewritten_queries``
      重跑 Milvus，覆盖 ``state["evidence"]``。
    - **多跳**（``sub_question_evidence`` 非空，T12 分解路径）：
      复用每个 sub_group 的 ``queries`` 字段重跑 Milvus（**不重跑 LLM rewrite，省 token**），
      evidence 打 ``sub_idx`` / ``sub_question`` 标签合并，
      同步更新 ``sub_question_evidence`` 各组的 ``evidence_count``。
      否则 ``answer_node`` 走多跳渲染时仍用旧的 evidence_count=0，输出"证据不足"。
    """
    infra = state.get("infra_status", {}) or {}
    milvus_ok = bool(infra.get("milvus_available", False))
    sub_groups: list[dict] = state.get("sub_question_evidence", []) or []

    # ---- 多跳模式：按子问题分组重检索，更新 sub_question_evidence ----
    if sub_groups:
        if not milvus_ok:
            return {"evidence": state.get("evidence", []) or []}

        merged_evidence: list[dict] = []
        new_groups: list[dict] = []
        for grp in sub_groups:
            sub_queries = list(grp.get("queries") or [])
            sub_idx = grp.get("idx", 0)
            sub_question = grp.get("sub_question", "")
            sub_ev: list[dict] = []
            if sub_queries:
                try:
                    result = multi_query_search(
                        queries=sub_queries,
                        top_k_per_query=_FANOUT_TOP_K_PER_QUERY,
                        final_k=_FANOUT_FINAL_K_PER_SUB,
                        rrf_k=_FANOUT_RRF_K,
                        use_rerank=True,
                    )
                    for hit in result.get("results", []) or []:
                        sub_ev.append(
                            {
                                "source": "milvus",
                                "match_type": "vector",
                                "sub_idx": sub_idx,
                                "sub_question": sub_question,
                                **hit,
                            }
                        )
                except Exception:
                    # 单子问题检索失败不阻断；evidence_count=0 仍由 answer 处理
                    pass
            # evidence_count 等 dedup 后按 sub_idx 重新统计，此处先不写入
            new_groups.append({**grp})
            merged_evidence.extend(sub_ev)

        # T19 B2：跨子问题按 chunk_id 去重（保留最高 score），同步更新每组 evidence_count
        deduped_evidence = _dedup_evidence_by_chunk_id(merged_evidence)
        count_by_sub: dict[int, int] = {}
        for ev in deduped_evidence:
            si = ev.get("sub_idx")
            if si is not None:
                count_by_sub[int(si)] = count_by_sub.get(int(si), 0) + 1
        for grp in new_groups:
            grp["evidence_count"] = count_by_sub.get(int(grp.get("idx", -1)), 0)

        return {
            "sub_question_evidence": new_groups,
            "evidence": deduped_evidence,
        }

    # ---- 单链路模式：原逻辑 ----
    queries = state.get("rewritten_queries", []) or []
    if not milvus_ok or not queries:
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


# T27 删：末尾老 alias（normalize_node / decompose_node / judge_node /
# answer_node / self_check_node）——create_*_node 现不接受 llm=None；
# grep 全仓无人 import 这些 alias，是死代码。select_candidates_node /
# ingest_candidates_node 也是老 BB 节点 T25 后不在主图，同为死代码，
# T27 不动留待后续清债任务。
