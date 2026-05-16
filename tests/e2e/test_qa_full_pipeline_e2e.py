# -*- coding: utf-8 -*-
"""完整 QA 链路端到端测试：query → QaGraph.run() → 答案。

4 组多轮对话场景，每组 2 轮：
- Turn 1：建立上下文实体（普通问题）
- Turn 2：含指代词追问（验证 T37 消解 → 全链路真实执行）

链路覆盖：
  probe → crystallized_check → normalize → decompose → fanout_prep × N
  → barrier1 → [merge_search_keywords → search_strategy → search_web_dual
                → fetch_extract × N → barrier_extract → persist → ingest]
  → subquery_search × N → barrier2 → judge → answer → self_check → crystallize

依赖：
- Milvus（http://localhost:19530，已 docker-compose up）
- Playwright Chromium
- LLM（Minimax，从 .env 读 BB_LLM_PROVIDER + BB_API_KEY）
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 项目根入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from brain_base.cli import _build_llm_from_env
from brain_base.graphs.qa_graph import QaGraph


# ---------------------------------------------------------------------------
# 4 组多轮对话用例
# ---------------------------------------------------------------------------
CASES = [
    {
        "id": "ragflow",
        "name": "Case 1: RAGFlow 跨轮代词消解（「它」→ RAGFlow）",
        "turn1": "RAGFlow 是什么？它解决什么问题？",
        "turn2": "它支持哪些文档格式？",
        "expect_entity": "RAGFlow",
    },
    {
        "id": "langgraph",
        "name": "Case 2: LangGraph 跨轮省略主语消解（「怎么安装」→ LangGraph）",
        "turn1": "LangGraph 是用来做什么的框架？",
        "turn2": "怎么安装？",
        "expect_entity": "LangGraph",
    },
    {
        "id": "milvus_timesens",
        "name": "Case 3: Milvus 跨轮指示代词 + 时效触发（「那个最近有什么更新」→ T38 强制外检）",
        "turn1": "Milvus 是什么向量数据库？",
        "turn2": "它最近有什么更新？",
        "expect_entity": "Milvus",
        "expect_time_sensitive": True,
    },
    {
        "id": "fastapi",
        "name": "Case 4: FastAPI 跨轮属性追问（「它的异步性能」→ FastAPI）",
        "turn1": "FastAPI 的核心特性是什么？",
        "turn2": "它的异步性能怎么样？",
        "expect_entity": "FastAPI",
    },
]


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _truncate(s: str, n: int = 200) -> str:
    if not isinstance(s, str):
        return str(s)
    s = s.strip()
    return s[:n] + ("..." if len(s) > n else "")


def _print_state_summary(turn_label: str, state: dict, elapsed: float) -> None:
    """打印关键 state 字段 + 完整 answer（不截断）。"""
    print(f"\n  ┌─[{turn_label} 状态摘要] ({elapsed:.1f}s)")
    print(f"  │ normalized_query     : {state.get('normalized_query', '')!r}")
    print(f"  │ contextualized_query : {state.get('contextualized_query')!r}")
    print(f"  │ rewrite_reason       : {state.get('rewrite_reason', '')!r}")
    print(f"  │ crystallized_status  : {state.get('crystallized_status')}")
    print(f"  │ crystallized_skill_id: {state.get('crystallized_skill_id')}")
    cz = state.get('crystallize_result') or {}
    if cz:
        print(f"  │ crystallize_result   : status={cz.get('status')} skill_id={cz.get('skill_id')} layer={cz.get('layer')}")
    print(f"  │ time_sensitive       : {state.get('time_sensitive', False)}")
    print(f"  │ time_range           : {state.get('time_range')}")
    sub_qs = state.get("sub_questions", [])
    print(f"  │ sub_questions ({len(sub_qs)})    : {sub_qs}")
    print(f"  │ sub_lexical_scores   : {state.get('sub_lexical_scores', [])}")
    print(f"  │ gi_trigger_reasons   : {state.get('gi_trigger_reasons', [])}")
    decisions = state.get("gi_decisions", [])
    if decisions:
        print(f"  │ gi_decisions         :")
        for d in decisions:
            score = d.get('sparse_score') or 0.0
            print(f"  │   - sub#{d.get('sub_idx')} triggered={d.get('triggered')} "
                  f"reason={d.get('reason')!r} score={score:.3f}")
    strategies = state.get("search_strategies", [])
    if strategies:
        print(f"  │ search_strategies    :")
        for s in strategies:
            print(f"  │   - scenario={s.get('scenario')!r} "
                  f"sites={s.get('suggested_sites')} "
                  f"rewritten={s.get('rewritten_query', '')!r}")
    print(f"  │ search_keywords      : {state.get('search_keywords', [])}")
    print(f"  │ ingested_count       : {state.get('ingested_count', 0)}")
    sub_evidence = state.get("sub_evidence", [])
    print(f"  │ sub_evidence         : {len(sub_evidence)} 个子问题，"
          f"共 {sum(len(s.get('evidence', [])) for s in sub_evidence)} 条证据")
    print(f"  └─ answer (完整，不截断):")
    print("  " + "─" * 76)
    ans = state.get('answer', '') or '(空)'
    for line in ans.splitlines() or [ans]:
        print(f"  {line}")
    print("  " + "─" * 76)


def _verify_t37(turn2_state: dict, expect_entity: str) -> tuple[bool, str]:
    """验证 T37 指代消解：normalized_query 应包含 expect_entity。"""
    norm = (turn2_state.get("normalized_query") or "").lower()
    ctx = (turn2_state.get("contextualized_query") or "").lower()
    entity = expect_entity.lower()
    if entity in norm:
        return True, f"normalized_query 含 {expect_entity!r}"
    if entity in ctx:
        return True, f"contextualized_query 含 {expect_entity!r}（但未替换 normalized）"
    return False, f"未消解（norm={norm!r}, ctx={ctx!r}）"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _dump_state(state: dict, path: Path) -> None:
    """按 turn 立即落盘 state JSON（防中断丢数据）。"""
    try:
        # 兜底序列化，过滤不可 JSON 化字段
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"  💾 state 已落盘: {path}")
    except Exception as e:
        print(f"  ⚠️  state 落盘失败: {type(e).__name__}: {e}")


def run_case(qa: QaGraph, case: dict, dump_dir: Path) -> dict:
    """跑一个用例的完整 2 轮对话；每 turn 立即落盘 state JSON。"""
    print(f"\n\n{'═' * 78}")
    print(f"  {case['name']}")
    print(f"{'═' * 78}")

    history: list[dict] = []
    case_result = {"id": case["id"], "name": case["name"], "turns": []}

    # ---- Turn 1 ----
    print(f"\n[Turn 1] 用户: {case['turn1']}")
    t0 = time.time()
    try:
        s1 = qa.run(question=case["turn1"], conversation_history=None)
        elapsed = time.time() - t0
        _print_state_summary("Turn 1", s1, elapsed)
        _dump_state(s1, dump_dir / f"dump_{case['id']}_t1.json")
        ans1 = s1.get("answer", "")
        ts = datetime.now(timezone.utc).isoformat()
        history.append({"role": "user", "text": case["turn1"], "ts": ts})
        history.append({"role": "ai", "text": ans1, "ts": ts})
        case_result["turns"].append({"role": "user", "text": case["turn1"]})
        case_result["turns"].append({"role": "ai", "text": ans1, "elapsed": elapsed})
    except Exception as e:
        print(f"\n  ❌ Turn 1 失败: {type(e).__name__}: {e}")
        case_result["turn1_error"] = f"{type(e).__name__}: {e}"
        return case_result

    # ---- Turn 2（带指代词，注入 history） ----
    print(f"\n[Turn 2] 用户: {case['turn2']}  (history len={len(history)})")
    t0 = time.time()
    try:
        s2 = qa.run(question=case["turn2"], conversation_history=history)
        elapsed = time.time() - t0
        _print_state_summary("Turn 2", s2, elapsed)
        _dump_state(s2, dump_dir / f"dump_{case['id']}_t2.json")
        ans2 = s2.get("answer", "")
        case_result["turns"].append({"role": "user", "text": case["turn2"]})
        case_result["turns"].append({"role": "ai", "text": ans2, "elapsed": elapsed})

        # T37 验证
        passed, reason = _verify_t37(s2, case["expect_entity"])
        case_result["t37_passed"] = passed
        case_result["t37_reason"] = reason
        verdict_icon = "✅" if passed else "❌"
        print(f"\n  {verdict_icon} T37 指代消解: {reason}")

        # T38 时效验证（仅 case 3）
        if case.get("expect_time_sensitive"):
            ts_actual = s2.get("time_sensitive", False)
            triggers = s2.get("gi_trigger_reasons", [])
            passed_t38 = ts_actual and "time_sensitive" in triggers
            case_result["t38_passed"] = passed_t38
            print(f"  {'✅' if passed_t38 else '❌'} T38 时效强制外检: "
                  f"time_sensitive={ts_actual}, triggers={triggers}")

    except Exception as e:
        print(f"\n  ❌ Turn 2 失败: {type(e).__name__}: {e}")
        case_result["turn2_error"] = f"{type(e).__name__}: {e}"

    # 每 case 完成后立即落盘汇总（防中断）
    try:
        (dump_dir / f"case_{case['id']}.json").write_text(
            json.dumps(case_result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"  ⚠️  case 落盘失败: {type(e).__name__}: {e}")

    return case_result


def main() -> int:
    print("=" * 78)
    print("  完整 QA 链路 E2E 测试：4 组多轮对话（每组 2 轮，含指代词消解）")
    print("=" * 78)

    print("\n[初始化] 构建 LLM + QaGraph...")
    llm = _build_llm_from_env()
    qa = QaGraph(llm=llm)
    print(f"[初始化] LLM={type(llm).__name__}, "
          f"enable_search_strategy={qa.config.enable_search_strategy}")

    # 落盘目录
    run_ts = int(time.time())
    dump_dir = Path(__file__).parent / f"e2e_run_{run_ts}"
    dump_dir.mkdir(parents=True, exist_ok=True)
    print(f"[初始化] 落盘目录: {dump_dir}")

    results = []
    overall_t0 = time.time()
    for case in CASES:
        results.append(run_case(qa, case, dump_dir))
    total = time.time() - overall_t0

    # ---- 汇总 ----
    print(f"\n\n{'═' * 78}")
    print("  汇总")
    print(f"{'═' * 78}")
    t37_pass = sum(1 for r in results if r.get("t37_passed"))
    t38_pass = sum(1 for r in results if r.get("t38_passed"))
    t38_total = sum(1 for c in CASES if c.get("expect_time_sensitive"))
    fail_runs = sum(1 for r in results if "turn1_error" in r or "turn2_error" in r)

    print(f"\n  完整链路成功跑通: {len(results) - fail_runs}/{len(results)}")
    print(f"  T37 指代消解通过: {t37_pass}/{len(results)}")
    print(f"  T38 时效强制外检通过: {t38_pass}/{t38_total}" if t38_total else "")
    print(f"  总耗时: {total:.1f}s")

    print(f"\n  详细结果:")
    for r in results:
        icon = "✅" if r.get("t37_passed") and "turn2_error" not in r else "❌"
        msg = r.get("t37_reason", r.get("turn2_error") or r.get("turn1_error") or "未知")
        print(f"    {icon} {r['name']}")
        print(f"       → {msg}")

    # 落盘完整结果（含答案）
    out_path = dump_dir / "e2e_results.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  完整结果已落盘: {out_path}")

    return 0 if (t37_pass == len(results) and fail_runs == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
