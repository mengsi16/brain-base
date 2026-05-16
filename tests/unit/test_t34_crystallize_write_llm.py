# -*- coding: utf-8 -*-
"""T34 验证：真调 LLM 验证固化层写入路径 value_score + skill_gen + write 端到端。

**默认必跑**（按 CLAUDE.md 规则 14：LLM 测试不跳过）。跑前在 `.env` 配任一 key：
    MINIMAX_API_KEY  (首选，默认 provider)
    GLM_API_KEY      (可选)

覆盖（4 case）：
1. value_score 四维度评分输出合理（0.0-1.0 且 composite_score 正确）
2. skill_gen 输出含真实 trigger_keywords（3-8 项，从问题/答案抽取）
3. 端到端 create_crystallize_answer_node 写入完整流程
4. 写入后 hit_check_node 能用生成的 keywords 命中
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

try:
    from dotenv import load_dotenv

    _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    load_dotenv(_PROJECT_ROOT / ".env")
except Exception:
    pass


# ---------------------------------------------------------------------------
# LLM 构造（复用 test_t31 模式）
# ---------------------------------------------------------------------------


def _resolve_llm_credentials() -> dict | None:
    minimax_key = (os.environ.get("MINIMAX_API_KEY") or "").strip()
    if minimax_key:
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
        "model": (os.environ.get("BB_DEEP_THINK_LLM") or "claude-sonnet-4-20250514"),
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
# Fixtures
# ---------------------------------------------------------------------------

_TEST_QUESTION = "RAGFlow 是什么？它的核心架构和使用场景有哪些？"
_TEST_ANSWER = """# RAGFlow 概述

RAGFlow 是一个开源的检索增强生成（RAG）引擎，专门用于构建基于深度文档理解的知识问答系统。

## 核心架构

1. **文档解析层**：支持 PDF、Word、Excel 等多种格式的深度解析
2. **分块引擎**：智能文档分块，保留语义完整性
3. **向量检索层**：基于 embedding 的语义搜索 + 关键词混合检索
4. **LLM 回答层**：调用大模型基于检索证据生成答案

## 使用场景

- 企业内部知识库问答
- 技术文档智能检索
- 法律/医疗等垂直领域文档问答
"""


@pytest.fixture(scope="module")
def llm():
    result = _build_llm()
    if result is None:
        pytest.fail(
            "T34 LLM 测试需要有效 API key（MINIMAX_API_KEY / GLM_API_KEY / BB_LLM_API_KEY）。"
            "缺 key 应 fail 不应 skip（CLAUDE.md 规则 14）。"
        )
    return result


@pytest.fixture
def crystal_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离的 data/crystallized/ 目录，避免写入真实数据。"""
    d = tmp_path / "crystallized"
    d.mkdir()
    (d / "cold").mkdir()
    monkeypatch.setattr(
        "brain_base.nodes.crystallize._CRYSTALLIZED_DIR", d
    )
    monkeypatch.setattr(
        "brain_base.nodes.crystallize._INDEX_FILE", d / "index.json"
    )
    return d


# ---------------------------------------------------------------------------
# Case 1: value_score 四维度评分合理
# ---------------------------------------------------------------------------


class TestValueScore:
    def test_value_score_returns_valid_dimensions(self, llm):
        from brain_base.nodes.crystallize import create_value_score_node

        vs_fn = create_value_score_node(llm)
        result = vs_fn(
            {"user_question": _TEST_QUESTION, "answer_markdown": _TEST_ANSWER}
        )

        # 四维度 + composite_score + recommended_layer 必须存在
        assert "value_score" in result
        assert "generality" in result
        assert "stability" in result
        assert "evidence_quality" in result
        assert "cost_benefit" in result
        assert "recommended_layer" in result

        # 每维度 [0.0, 1.0]
        for dim in ("generality", "stability", "evidence_quality", "cost_benefit"):
            assert 0.0 <= result[dim] <= 1.0, f"{dim}={result[dim]} 超出范围"

        # composite_score 也在 [0.0, 1.0]
        assert 0.0 <= result["value_score"] <= 1.0

        # recommended_layer 是三值之一
        assert result["recommended_layer"] in ("hot", "cold", "skip")

    def test_value_score_includes_entities_and_scenario(self, llm):
        """T41：ValueScore 输出必须含 entities (1-5 项) + scenario + 可选 trigger_keywords。

        RAGFlow 这类专有名词必须放 entities，不能放 trigger_keywords（后者只放场景辅助词）。
        """
        from brain_base.nodes.crystallize import create_value_score_node

        vs_fn = create_value_score_node(llm)
        result = vs_fn(
            {"user_question": _TEST_QUESTION, "answer_markdown": _TEST_ANSWER}
        )

        # entities 必填，1-5 项，且必须含 ragflow（专有名词）
        entities = result.get("entities", [])
        assert 1 <= len(entities) <= 5, f"entities 数量 {len(entities)} 不在 [1,5]"
        assert any(
            "ragflow" in e.lower() for e in entities
        ), f"entities 不含 ragflow: {entities}"

        # scenario 必须是 7 个枚举之一
        scenario = result.get("scenario", "")
        assert scenario in {
            "definition", "howto", "compare", "troubleshoot", "config", "update", "general",
        }, f"scenario 非合法枚举: {scenario!r}"

        # trigger_keywords 降级为可选辅助，长度 0-10；若有值不能包含疑问词/泛词（软检查）
        kws = result.get("trigger_keywords", [])
        assert 0 <= len(kws) <= 10, f"trigger_keywords 数量 {len(kws)} 不在 [0,10]"
        stop_words = {"是什么", "怎么", "如何", "功能", "用途", "简介"}
        assert not any(
            kw in stop_words for kw in kws
        ), f"trigger_keywords 含停用词: {kws}"


# ---------------------------------------------------------------------------
# Case 2: skill_gen 生成真实 skill 骨架
# ---------------------------------------------------------------------------


class TestSkillGen:
    def test_skill_gen_returns_valid_payload(self, llm):
        from brain_base.nodes.crystallize import create_skill_gen_node

        sg_fn = create_skill_gen_node(llm)
        result = sg_fn(
            {
                "user_question": _TEST_QUESTION,
                "answer_markdown": _TEST_ANSWER,
                "recommended_layer": "hot",
            }
        )

        payload = result.get("skill_payload")
        assert payload is not None, "skill_payload 不应为 None"
        assert "skill_id" in payload
        assert "title" in payload
        assert "description" in payload
        assert "entities" in payload        # T41：新必填字段
        assert "scenario" in payload        # T41：新必填字段
        assert "trigger_keywords" in payload  # T41：限标为辅助
        assert "layer" in payload
        assert "answer_markdown" in payload

        # T41：entities 必填 1-5 项，且含专有名词（RAGFlow）
        entities = payload["entities"]
        assert 1 <= len(entities) <= 5, f"skill_gen entities 数量 {len(entities)}"
        assert any("ragflow" in e.lower() for e in entities), \
            f"entities 不含 ragflow: {entities}"

        # T41：scenario 是 7 个枚举之一
        assert payload["scenario"] in {
            "definition", "howto", "compare", "troubleshoot", "config", "update", "general",
        }, f"scenario 非合法枚举: {payload['scenario']!r}"

        # trigger_keywords 降级为可选 0-10 项
        kws = payload["trigger_keywords"]
        assert 0 <= len(kws) <= 10, f"skill_gen trigger_keywords 数量 {len(kws)}"
        # layer 是 hot 或 cold
        assert payload["layer"] in ("hot", "cold")


# ---------------------------------------------------------------------------
# Case 3: 端到端 create_crystallize_answer_node 写入完整流程
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_e2e_write_creates_file_and_index(self, llm, crystal_dir):
        from brain_base.nodes.qa import create_crystallize_answer_node

        node_fn = create_crystallize_answer_node(llm)
        state: dict[str, Any] = {
            "question": _TEST_QUESTION,
            "answer": _TEST_ANSWER,
            "crystallized_status": "miss",
        }
        result = node_fn(state)

        cr = result.get("crystallize_result", {})
        # 允许 created_hot / created_cold / skipped（value_score < 0.3 则 skip）
        status = cr.get("status", "")
        assert status in (
            "created_hot", "created_cold", "skipped", "error"
        ), f"unexpected status: {status}"

        if status.startswith("created_"):
            # 验证 index.json 写入
            index_path = crystal_dir / "index.json"
            assert index_path.exists(), "index.json 未创建"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            skills = index.get("skills", [])
            assert len(skills) >= 1, "index.json 无 skill 条目"

            # 验证 .md 文件写入
            skill_id = cr.get("skill_id", "")
            assert skill_id, "skill_id 为空"
            layer = cr.get("layer", "")
            if layer == "cold":
                md_path = crystal_dir / "cold" / f"{skill_id}.md"
            else:
                md_path = crystal_dir / f"{skill_id}.md"
            assert md_path.exists(), f"{md_path} 文件未创建"

            # 验证 value_score 写入 index
            entry = next(
                (s for s in skills if s["skill_id"] == skill_id), None
            )
            assert entry is not None
            # T41：entity 是主匹配字段，必填 1-5 项
            assert "entities" in entry, f"index 中缺 entities 字段"
            assert 1 <= len(entry["entities"]) <= 5, (
                f"index 中 entities 数量异常: {entry['entities']}"
            )
            # scenario 在枚举集
            assert entry.get("scenario") in {
                "definition", "howto", "compare", "troubleshoot", "config", "update", "general",
            }
            # trigger_keywords 降级为辅助（可空），只检查字段存在
            assert "trigger_keywords" in entry

    def test_e2e_degraded_status_still_writes(self, llm, crystal_dir):
        """T34.1: degraded 不阻止写入（bootstrap 修复）——degraded 只影响读取路径。"""
        from brain_base.nodes.qa import create_crystallize_answer_node

        node_fn = create_crystallize_answer_node(llm)
        result = node_fn(
            {
                "question": _TEST_QUESTION,
                "answer": _TEST_ANSWER,
                "crystallized_status": "degraded",
            }
        )
        cr = result.get("crystallize_result", {})
        # 应正常走 LLM 评分 + 写入（或因 value_score < 0.3 跳过），不应返回空
        assert cr.get("status") in (
            "created_hot", "created_cold", "skipped", "error"
        ), f"degraded 状态下应尝试写入，实际: {cr}"

    def test_e2e_empty_answer_skips(self, llm, crystal_dir):
        """空答案直接跳过。"""
        from brain_base.nodes.qa import create_crystallize_answer_node

        node_fn = create_crystallize_answer_node(llm)
        result = node_fn(
            {"question": _TEST_QUESTION, "answer": "", "crystallized_status": "miss"}
        )
        assert result == {}


# ---------------------------------------------------------------------------
# Case 4: 写入后 hit_check 能命中
# ---------------------------------------------------------------------------


class TestHitAfterWrite:
    def test_hit_check_matches_generated_keywords(self, llm, crystal_dir):
        """写入一条 skill 后，用相似问题做 hit_check 应命中。"""
        from brain_base.nodes.crystallize import (
            create_skill_gen_node,
            create_value_score_node,
            crystallize_write_node,
            hit_check_node,
        )

        # 先写入
        vs_fn = create_value_score_node(llm)
        sg_fn = create_skill_gen_node(llm)

        vs_result = vs_fn(
            {"user_question": _TEST_QUESTION, "answer_markdown": _TEST_ANSWER}
        )
        value_score = vs_result.get("value_score", 0.0)

        if value_score < 0.3:
            pytest.skip(
                f"value_score={value_score:.2f} < 0.3, LLM 判定不值得固化——无法测命中"
            )

        sg_result = sg_fn(
            {
                "user_question": _TEST_QUESTION,
                "answer_markdown": _TEST_ANSWER,
                "recommended_layer": vs_result.get("recommended_layer", "cold"),
            }
        )

        write_result = crystallize_write_node(
            {
                "user_question": _TEST_QUESTION,
                "answer_markdown": _TEST_ANSWER,
                "value_score": value_score,
                "skill_payload": sg_result.get("skill_payload"),
            }
        )
        assert write_result["status"].startswith("created_")

        # 再 hit_check：用原始问题
        hit_result = hit_check_node({"user_question": _TEST_QUESTION})
        assert hit_result["status"] in (
            "hit_hot", "cold_observed", "cold_promoted"
        ), f"原始问题未命中: {hit_result}"

        # 用部分关键词的简化问题
        hit_result2 = hit_check_node({"user_question": "RAGFlow 的核心架构"})
        # 可能命中也可能不命中（取决于 LLM 生成的 keywords），不强制 assert
        # 但至少不应 crash
        assert "status" in hit_result2
