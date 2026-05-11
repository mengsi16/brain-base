# -*- coding: utf-8 -*-
"""T31 验证：真调 LLM 验证 NORMALIZE_SYSTEM_PROMPT 4 类改写规则有效性。

**默认必跑**（按 CLAUDE.md 规则 14：LLM 测试不跳过）。跑前在 `.env` 配任一 key：
    MINIMAX_API_KEY  (首选，默认 provider)
    GLM_API_KEY      (可选)
    BB_LLM_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY

缺 key 应 fail 而非 skip——mock LLM 只验字段透传，无法验 prompt 是否让 LLM 输出预期改写。

覆盖（6 个 case，不要求字面精确匹配，因 LLM 输出有微小不确定性）：
1. 反问→陈述（rule #2）
2. 缩写歧义消解（rule #3）含真实歧义场景
3. 缩写无歧义场景（rule #3 反例）
4. 时间归一化（rule #4）以 user_prompt 注入的【今天日期】为锦点
5. 拼写纠错保留实体大小写（rule #5）
6. NormalizedQuestion schema time_range 长度约束（纯 pydantic 验证，不调 LLM）
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from pydantic import ValidationError

# 加载 .env（按规则 12：测试脚本用 load_dotenv 而非 $env:）。
# 不强制依赖 dotenv 包，缺包/缺文件都安静跳过——env 仍可被外部注入。
try:
    from dotenv import load_dotenv

    _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    load_dotenv(_PROJECT_ROOT / ".env")
except Exception:
    pass

from brain_base.agents.schemas import NormalizedQuestion
from brain_base.nodes.qa import create_normalize_node


# ---------------------------------------------------------------------------
# LLM 构造（参考 tests/e2e/test_qa_full_pipeline.py 风格）
# ---------------------------------------------------------------------------


def _resolve_llm_credentials() -> dict | None:
    """从 env 找 LLM 凭证。

    按 CLAUDE.md 规则 14必须先 Minimax（默认 provider）：MINIMAX_API_KEY 优先；
    GLM 互为备选；BB_LLM_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY 兑底。
    """
    minimax_key = (os.environ.get("MINIMAX_API_KEY") or "").strip()
    if minimax_key:
        # Minimax 走 Anthropic-compatible API（factory 不识别 "minimax" provider）。
        return {
            "provider": "anthropic",
            "model": (os.environ.get("MINIMAX_MODEL") or "MiniMax-M2"),
            "base_url": (os.environ.get("MINIMAX_BASE_URL") or "").strip() or None,
            "api_key": minimax_key,
        }
    glm_key = (os.environ.get("GLM_API_KEY") or "").strip()
    if glm_key:
        return {
            "provider": "glm",
            "model": (os.environ.get("GLM_MODEL") or "glm-4.6"),
            "base_url": (os.environ.get("GLM_BASE_URL") or "").strip() or None,
            "api_key": glm_key,
        }
    api_key = (
        os.environ.get("BB_LLM_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()
    if not api_key:
        return None
    return {
        "provider": (os.environ.get("BB_LLM_PROVIDER") or "anthropic").lower(),
        "model": (
            os.environ.get("BB_DEEP_THINK_LLM")
            or "claude-sonnet-4-20250514"
        ),
        "base_url": (os.environ.get("BB_LLM_BASE_URL") or "").strip() or None,
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
        max_tokens_to_sample=1024,
        timeout=60,
        max_retries=2,
    )
    return client.get_llm()


# ---------------------------------------------------------------------------
# Fixture：session 级 LLM + normalize_node（一次构造，5 case 共享）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def normalize():
    llm = _build_llm()
    if llm is None:
        # 按 CLAUDE.md 规则 14：LLM 测试是核心必跑，缺 key 应 fail 不应 skip。
        pytest.fail(
            "未配置 LLM API key：请在 .env 加 MINIMAX_API_KEY（首选） / GLM_API_KEY / "
            "BB_LLM_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY 中任一。"
            "LLM 测试是核心必跑（CLAUDE.md 规则 14）不允许跳过。"
        )
    return create_normalize_node(llm)


def _run(normalize_fn, question: str) -> dict:
    """跑 normalize_node 拿 state 输出。"""
    return normalize_fn({"question": question})


# ---------------------------------------------------------------------------
# Case 1: 反问 → 陈述
# ---------------------------------------------------------------------------


@pytest.mark.requires_llm
def test_rhetorical_to_declarative(normalize):  # 保留 mark 仅作分类标签，addopts 不再 auto-skip
    """反问句"不会..吧？" 应改写为陈述疑问，去掉反诘语气词。"""
    out = _run(normalize, "openclaw 不会要排队两小时吧？")

    normalized = out["normalized_query"]
    assert isinstance(normalized, str) and normalized.strip(), (
        f"normalized 应非空：{normalized!r}"
    )

    # 反问语气词应被改写：至少一个标志性反诘词消失
    has_rhetorical_marker = any(m in normalized for m in ("不会", "吧"))
    assert not has_rhetorical_marker, (
        f"反问应改写为陈述，但 normalized 仍含反诘词：{normalized!r}"
    )

    # 应保留实体名 openclaw
    assert "openclaw" in normalized.lower(), (
        f"实体名 openclaw 应被保留：{normalized!r}"
    )


# ---------------------------------------------------------------------------
# Case 2: 缩写歧义消解（"RAG" 有 ≥2 解读）
# ---------------------------------------------------------------------------


@pytest.mark.requires_llm
def test_abbreviation_ambiguous_rag(normalize):
    """RAG 在 RAG-Anything / RAGFlow 等多框架场景有 ≥2 解读，应输出 hints。"""
    out = _run(normalize, "RAG 怎么部署？")

    hints = out["abbreviation_hints"]
    # 允许 LLM 给出 None 或 list，但有歧义场景下应该非 None
    # 若实测发现 LLM 不稳定可放宽为 (hints is None or len(hints) >= 2)
    assert hints is not None, (
        f"RAG 有真实歧义（RAGFlow / RAG-Anything 等），应输出 hints；"
        f"实际 None。normalized={out.get('normalized_query')!r}"
    )
    assert isinstance(hints, list), f"hints 应为 list：{type(hints)}"
    assert len(hints) >= 2, f"歧义场景应输出 ≥2 候选：{hints}"

    # 缩写本身应保留在 normalized 里（rule #3 不展开）
    normalized = out["normalized_query"]
    assert "RAG" in normalized.upper(), (
        f"原拼写 RAG 应保留：normalized={normalized!r}"
    )


# ---------------------------------------------------------------------------
# Case 3: 缩写无歧义（"YOLOv8" 单义）
# ---------------------------------------------------------------------------


@pytest.mark.requires_llm
def test_abbreviation_unambiguous_yolo(normalize):
    """YOLOv8 是单一产品名（指定版本），不应触发 hints。"""
    out = _run(normalize, "YOLOv8 训练步骤")

    hints = out["abbreviation_hints"]
    assert hints is None, (
        f"YOLOv8 单义不应输出 hints，实际 hints={hints}；"
        f"normalized={out.get('normalized_query')!r}"
    )


# ---------------------------------------------------------------------------
# Case 4: 时间归一化（"最近" → time_range，以今天为锚点）
# ---------------------------------------------------------------------------


@pytest.mark.requires_llm
def test_time_range_recent(normalize):
    """time_sensitive=True + 含"最近" → time_range 应以今天为锚点的 ISO 日期范围。"""
    out = _run(normalize, "最近 RAGFlow 有什么更新？")

    assert out["time_sensitive"] is True, (
        f"含「最近」应触发 time_sensitive=True，实际 {out['time_sensitive']}；"
        f"normalized={out.get('normalized_query')!r}"
    )

    tr = out["time_range"]
    assert tr is not None, (
        f"含「最近」+ time_sensitive=True 应输出 time_range，实际 None"
    )
    assert isinstance(tr, list) and len(tr) == 2, (
        f"time_range 应为长度 2 的 list：{tr}"
    )

    # 日期格式 YYYY-MM-DD
    start, end = tr
    assert len(start) == 10 and start[4] == "-" and start[7] == "-", (
        f"start 应为 YYYY-MM-DD：{start!r}"
    )
    assert len(end) == 10 and end[4] == "-" and end[7] == "-", (
        f"end 应为 YYYY-MM-DD：{end!r}"
    )

    # end 不晚于今天（LLM 必须以注入的【今天日期】为锚点）
    today = datetime.now(timezone.utc).astimezone().date()
    end_date = date.fromisoformat(end)
    start_date = date.fromisoformat(start)
    assert end_date <= today, (
        f"time_range end={end} 应不晚于今天 {today}（LLM 必须用注入日期作锚点）"
    )
    assert start_date <= end_date, (
        f"time_range start={start} 应不晚于 end={end}"
    )


# ---------------------------------------------------------------------------
# Case 5: 拼写纠错保留实体大小写
# ---------------------------------------------------------------------------


@pytest.mark.requires_llm
def test_typo_keeps_entity_case(normalize):
    """常见动词 typo（"安转"→"安装"）应纠正，但实体名 "RAGFlow" 大小写必须保留。"""
    out = _run(normalize, "RAGFlow 安转步骤")

    normalized = out["normalized_query"]
    # typo "安转" 应被纠为 "安装"
    assert "安装" in normalized, (
        f"typo 「安转」应纠为「安装」：normalized={normalized!r}"
    )
    assert "安转" not in normalized, (
        f"原 typo「安转」不应保留：normalized={normalized!r}"
    )

    # 实体 "RAGFlow" 必须原拼写保留（不小写化为 "ragflow"）
    assert "RAGFlow" in normalized, (
        f"实体 RAGFlow 大小写应保留：normalized={normalized!r}"
    )


# ---------------------------------------------------------------------------
# Case 6: NormalizedQuestion schema time_range 长度约束（不调 LLM）
# ---------------------------------------------------------------------------


def test_normalized_question_schema_validates_time_range_length():
    """time_range 必须长度恰好为 2；过短或过长应 raise ValidationError。

    纯 pydantic schema 验证，不调 LLM——以避免后续有人误以为 tuple/list 限制可以软。
    """
    # 长度 = 2 应通过
    ok = NormalizedQuestion(
        normalized="x",
        expected_type="fact",
        time_range=["2026-04-10", "2026-05-10"],
    )
    assert ok.time_range == ["2026-04-10", "2026-05-10"]

    # None 应通过（default）
    ok2 = NormalizedQuestion(normalized="x", expected_type="fact", time_range=None)
    assert ok2.time_range is None

    # 长度 = 1 应报错
    with pytest.raises(ValidationError):
        NormalizedQuestion(
            normalized="x", expected_type="fact", time_range=["2026-04-10"]
        )

    # 长度 = 3 应报错
    with pytest.raises(ValidationError):
        NormalizedQuestion(
            normalized="x",
            expected_type="fact",
            time_range=["2026-04-10", "2026-05-10", "2026-06-10"],
        )

    # 空列表应报错
    with pytest.raises(ValidationError):
        NormalizedQuestion(normalized="x", expected_type="fact", time_range=[])
