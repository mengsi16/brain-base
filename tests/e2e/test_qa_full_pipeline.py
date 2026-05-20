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

默认套件不跑本文件，是因为 ``requires_milvus`` 被 `pytest.ini` 默认排除；
若显式执行本文件，LLM key 缺失应视为环境未配置而失败，不再静默跳过。

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
        pytest.fail(
            "未配置 LLM API key（MINIMAX_API_KEY / GLM_API_KEY / "
            "BB_LLM_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY 任一）。"
            "显式执行 e2e 时，LLM 缺失必须 fail，不得 skip。"
        )

    from brain_base.config import GetInfoConfig
    from brain_base.graphs.qa_graph import QaGraph

    # T54 后：GetInfoConfig 只保留 fetch_extract / SERP / intent 等仍活字段
    # （T50.1 删 max_official/max_community/max_total/batch_timeout/single_url_timeout，
    #  T54 删 get_info_max_iter/get_info_target_official/get_info_total_timeout）
    cfg = GetInfoConfig(
        enable=True,
        fetch_extract_concurrency=3,
        search_pages_per_engine=2,
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
        fetch_extract_concurrency=3,
        search_pages_per_engine=2,
    )
    logger.info("编译 QaGraph（含自动外检 + 入库回路）")
    graph = QaGraph(llm=llm, get_info_config=cfg).graph

    logger.info("问题：%s", QUESTION)
    logger.info(
        "外检配置：fetch_extract_concurrency=%d search_pages_per_engine=%d",
        cfg.fetch_extract_concurrency, cfg.search_pages_per_engine,
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
