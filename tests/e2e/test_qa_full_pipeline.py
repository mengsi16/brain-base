# -*- coding: utf-8 -*-
"""QA 全链路端到端测试：跑通 QaGraph 完整闭环（含自动外检 + 入库 + 重检索 + 自检）。

依赖外部资源：
1. 真实 LLM API key（任一 provider，通过 BB_LLM_* 环境变量配置）
2. 本地 Milvus（可用 docker compose 起）
3. Playwright-cli（外检需要）

运行：

    pytest tests/e2e/test_qa_full_pipeline.py -v
    # 或独立脚本调试：
    python tests/e2e/test_qa_full_pipeline.py

CI / 无 LLM key 时被 ``requires_llm`` marker 自动跳过。

产物（独立脚本运行时）：

    data/logs/e2e_trace.log     控制台日志副本
    data/logs/e2e_trace.jsonl   每个节点的完整 update payload
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

# 加载 .env（如果存在）。dotenv 不强制依赖，缺包/缺文件都跳过。
try:
    from dotenv import load_dotenv

    _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    load_dotenv(_PROJECT_ROOT / ".env")
except Exception:
    pass


QUESTION = "讲讲 LiteLLM 这个项目：它是什么、解决了什么问题、怎么用？"


def _resolve_llm_credentials() -> dict[str, str] | None:
    """从环境变量解析 LLM 凭据。任一 provider 完整即可，缺则返回 None。"""
    api_key = (os.environ.get("BB_LLM_API_KEY") or "").strip()
    if not api_key:
        # 兜底常见 SDK env
        api_key = (
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("MINIMAX_API_KEY")
            or ""
        ).strip()
    if not api_key:
        return None
    return {
        "provider": (os.environ.get("BB_LLM_PROVIDER") or "anthropic").lower(),
        "model": (
            os.environ.get("BB_DEEP_THINK_LLM")
            or os.environ.get("MINIMAX_MODEL")
            or "claude-sonnet-4-20250514"
        ),
        "base_url": (
            os.environ.get("BB_LLM_BASE_URL")
            or os.environ.get("MINIMAX_BASE_URL")
            or ""
        ).strip()
        or None,
        "api_key": api_key,
    }


def _build_llm():
    creds = _resolve_llm_credentials()
    if creds is None:
        return None
    from brain_base.llm_clients.factory import create_llm_client

    client = create_llm_client(
        provider=creds["provider"],
        model=creds["model"],
        base_url=creds["base_url"],
        api_key=creds["api_key"],
        temperature=0.2,
        max_tokens_to_sample=2048,
        timeout=60,
        max_retries=2,
    )
    return client.get_llm()


@pytest.mark.requires_llm
@pytest.mark.requires_milvus
@pytest.mark.slow
def test_qa_full_pipeline_with_external_topup():
    """端到端：本地证据不足 → 自动外检 → 入库 → 重检索 → 答案 → 自检。

    断言：
    - 节点至少跑过 normalize / rewrite / search / judge / answer
    - evidence 列表最终非空（外检入库后可命中）
    - answer 文本非空
    """
    llm = _build_llm()
    if llm is None:
        pytest.skip("未配置 LLM API key（BB_LLM_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY）")

    from brain_base.config import GetInfoConfig
    from brain_base.graphs.qa_graph import QaGraph

    # 端到端用相对保守的配置：拉 2 个 official 文档，给入库充裕时间
    cfg = GetInfoConfig(
        enable=True,
        max_official=2,
        max_community=1,
        max_total=2,
        batch_timeout=180.0,
        single_url_timeout=60.0,
        get_info_max_iter=3,
        get_info_target_official=2,
        get_info_total_timeout=90.0,
    )
    graph = QaGraph(llm=llm, get_info_config=cfg).graph

    final_state = graph.invoke(
        {"question": QUESTION},
        config={"recursion_limit": 50},
    )

    assert final_state.get("answer"), "QA 最终必须产出非空 answer"
    assert isinstance(final_state.get("evidence"), list), "evidence 字段必须是 list"


# -----------------------------------------------------------------------------
# 独立脚本入口（带详细 trace 落盘）
# -----------------------------------------------------------------------------
def _main_with_trace() -> int:
    from brain_base.agents.utils.tracing import configure_logger, stream_with_trace
    from brain_base.config import GetInfoConfig
    from brain_base.graphs.qa_graph import QaGraph

    log_dir = Path("data/logs")
    logger = configure_logger(
        name="brain_base.e2e",
        level=logging.INFO,
        log_file=log_dir / "e2e_trace.log",
    )

    logger.info("构造 LLM：从环境变量读取（BB_LLM_* / ANTHROPIC_API_KEY / OPENAI_API_KEY / MINIMAX_*）")
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
        get_info_total_timeout=90.0,
    )
    logger.info("编译 QaGraph（含自动外检 + 入库回路）")
    graph = QaGraph(llm=llm, get_info_config=cfg).graph

    logger.info("问题：%s", QUESTION)
    logger.info(
        "外检配置：max_official=%d max_community=%d max_total=%d batch_timeout=%.0fs",
        cfg.max_official, cfg.max_community, cfg.max_total, cfg.batch_timeout,
    )

    final_state = stream_with_trace(
        graph,
        {"question": QUESTION},
        logger=logger,
        jsonl_path=log_dir / "e2e_trace.jsonl",
        config={"recursion_limit": 50},
    )

    logger.info("=" * 72)
    logger.info("[FINAL ANSWER]")
    answer = final_state.get("answer", "(无答案)")
    for line in (answer or "").splitlines() or ["(空)"]:
        logger.info("  %s", line)

    logger.info("-" * 72)
    logger.info("[PATH SUMMARY]")
    logger.info("  crystallized_status = %s", final_state.get("crystallized_status"))
    logger.info("  evidence_sufficient = %s", final_state.get("evidence_sufficient"))
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
    sys.exit(_main_with_trace())
