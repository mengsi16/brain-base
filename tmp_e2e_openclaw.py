# -*- coding: utf-8 -*-
"""一次性 e2e 验证脚本：跑 openclaw 多跳问题，看 T12 fan-out 在真实 LLM + Milvus 下端到端表现。

不属于回归套件——跑完用户可删。复用 tests/e2e/test_qa_full_pipeline.py 的
LLM 凭据解析 / trace 落盘逻辑。

产物：
    data/logs/openclaw_e2e.log
    data/logs/openclaw_e2e.jsonl

运行：
    python tmp_e2e_openclaw.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

# 复用 test_qa_full_pipeline 的 _build_llm（避免重复实现凭据解析）
sys.path.insert(0, str(Path(__file__).resolve().parent / "tests" / "e2e"))
from test_qa_full_pipeline import _build_llm  # noqa: E402

DEFAULT_QUESTION = "openclaw 是什么，怎么启动的，怎么卸载的，怎么配置的"


def main() -> int:
    from brain_base.agents.utils.tracing import configure_logger, stream_with_trace
    from brain_base.config import GetInfoConfig
    from brain_base.graphs.qa_graph import QaGraph

    # 允许命令行参数覆盖：python tmp_e2e_openclaw.py "问题文本" [log名前缀]
    question = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUESTION
    log_prefix = sys.argv[2] if len(sys.argv) > 2 else "openclaw"

    log_dir = Path("data/logs")
    logger = configure_logger(
        name=f"brain_base.{log_prefix}_e2e",
        level=logging.INFO,
        log_file=log_dir / f"{log_prefix}_e2e.log",
    )

    logger.info("构造 LLM（从 .env 读凭据）")
    llm = _build_llm()
    if llm is None:
        logger.error("未配置 LLM API key，终止")
        return 1

    cfg = GetInfoConfig(
        enable=True,
        max_official=2,
        max_community=1,
        max_total=2,
        batch_timeout=180.0,
        single_url_timeout=60.0,
        get_info_max_iter=3,
        get_info_target_official=2,
        get_info_total_timeout=120.0,
    )
    logger.info("编译 QaGraph（含 T12 多跳分解 + T10 自动外检）")
    graph = QaGraph(llm=llm, get_info_config=cfg).graph

    logger.info("问题：%s", question)

    final_state = stream_with_trace(
        graph,
        {"question": question},
        logger=logger,
        jsonl_path=log_dir / f"{log_prefix}_e2e.jsonl",
        config={"recursion_limit": 50},
    )

    logger.info("=" * 72)
    logger.info("[FINAL ANSWER]")
    answer = final_state.get("answer", "(无答案)")
    for line in (answer or "").splitlines() or ["(空)"]:
        logger.info("  %s", line)

    logger.info("-" * 72)
    logger.info("[T12 PATH SUMMARY]")
    logger.info("  decomposition_needed = %s", final_state.get("decomposition_needed"))
    sub_groups = final_state.get("sub_question_evidence") or []
    logger.info("  sub_question_evidence: %d 组", len(sub_groups))
    for g in sub_groups:
        logger.info(
            "    [%d] %s | queries=%d | evidence=%d",
            g.get("idx", -1),
            g.get("sub_question", ""),
            len(g.get("queries", []) or []),
            g.get("evidence_count", 0),
        )

    logger.info("-" * 72)
    logger.info("[GENERAL PATH SUMMARY]")
    logger.info("  crystallized_status = %s", final_state.get("crystallized_status"))
    logger.info("  evidence_sufficient = %s", final_state.get("evidence_sufficient"))
    logger.info("  judge_reason        = %s", final_state.get("judge_reason"))
    logger.info(
        "  self_check_passed   = %s (skipped=%s)",
        final_state.get("self_check_passed"),
        final_state.get("self_check_skipped"),
    )
    logger.info("  evidence count      = %d", len(final_state.get("evidence", []) or []))
    logger.info(
        "  trigger_get_info    = %s | reason=%s",
        final_state.get("trigger_get_info"),
        final_state.get("get_info_reason", ""),
    )
    logger.info("  get_info_attempted  = %s", final_state.get("get_info_attempted"))
    logger.info("  candidates count    = %d", len(final_state.get("get_info_candidates", []) or []))
    logger.info("  ingest_targets      = %d", len(final_state.get("ingest_targets", []) or []))
    logger.info("  get_info_ingested   = %s", final_state.get("get_info_ingested", []))
    errors = final_state.get("ingest_errors", []) or []
    if errors:
        logger.info("  ingest_errors:")
        for e in errors:
            logger.info("    - %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
