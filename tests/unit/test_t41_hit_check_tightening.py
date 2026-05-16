# -*- coding: utf-8 -*-
"""T41 hit_check 收紧验证：entity-first + scenario 二次过滤 + 老数据兜底。

纯规则测试，不调 LLM，快速覆盖：
1. 新数据（含 entities）：entity substring 命中 → 返回 hit_hot
2. 新数据：entity 不命中 → miss
3. 新数据：entity 命中但 scenario 不一致 → miss
4. 新数据：entity 命中 + scenario 都为 general → 宽松命中
5. 老数据兜底：无 entities 字段 + 只有弱 keywords（纯停用词）→ miss（不误命中）
6. 老数据兜底：无 entities 字段 + 有强 keyword（如实体名）→ 命中
7. _infer_scenario 规则正确性
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from brain_base.nodes.crystallize import _infer_scenario, _matches_skill, hit_check_node


# ---------------------------------------------------------------------------
# _infer_scenario：纯规则单元测试
# ---------------------------------------------------------------------------


class TestInferScenario:
    @pytest.mark.parametrize("question,expected", [
        ("RAGFlow 是什么？", "definition"),
        ("Milvus 的用途", "definition"),
        ("如何安装 LangGraph？", "howto"),
        ("FastAPI 怎么部署", "howto"),
        ("LangGraph vs LangChain", "compare"),
        ("Milvus 和 Pinecone 的区别", "compare"),
        ("Milvus 报错怎么办", "troubleshoot"),
        ("FastAPI 启动失败", "troubleshoot"),
        ("Playwright 的配置参数", "config"),
        ("RAGFlow 最近有什么更新", "update"),
        ("LangGraph 最新版本", "update"),
        ("天气如何", "howto"),  # 包含“如何” → howto（规则采词级匹配，无上下文）
        ("hello world", "general"),  # 纯英文无中文规则命中
    ])
    def test_scenario_inference(self, question, expected):
        assert _infer_scenario(question) == expected


# ---------------------------------------------------------------------------
# _matches_skill：核心匹配逻辑单元测试
# ---------------------------------------------------------------------------


class TestMatchesSkill:
    def test_new_data_entity_hit_same_scenario(self):
        """T41：entities 命中 + scenario 一致 → 命中"""
        skill = {"entities": ["RAGFlow"], "scenario": "definition"}
        assert _matches_skill(skill, "ragflow 是什么？", "definition")

    def test_new_data_entity_miss(self):
        """T41：entities 不命中 → miss"""
        skill = {"entities": ["LangGraph"], "scenario": "definition"}
        assert not _matches_skill(skill, "milvus 是什么？", "definition")

    def test_new_data_entity_hit_scenario_mismatch(self):
        """T41：entities 命中但 scenario 不一致 → miss"""
        skill = {"entities": ["RAGFlow"], "scenario": "definition"}
        # skill 是 definition，question 是 howto → miss
        assert not _matches_skill(skill, "ragflow 怎么安装", "howto")

    def test_new_data_scenario_general_lenient(self):
        """T41：skill.scenario=general → 只看 entity，不强制 scenario"""
        skill = {"entities": ["RAGFlow"], "scenario": "general"}
        assert _matches_skill(skill, "ragflow 怎么安装", "howto")

    def test_question_scenario_general_lenient(self):
        """T41：question_scenario=general → 不强制 scenario"""
        skill = {"entities": ["RAGFlow"], "scenario": "definition"}
        assert _matches_skill(skill, "说说 ragflow", "general")

    def test_case_insensitive_entity_match(self):
        """T41：entity 大小写不敏感（注意契约：_matches_skill 要求传入已 lower 的 question）"""
        skill = {"entities": ["RAGFlow"], "scenario": "definition"}
        # 模拟 hit_check_node 调用时传入的 question.lower()
        assert _matches_skill(skill, "RAGFLOW 是什么？".lower(), "definition")
        assert _matches_skill(skill, "ragflow 是什么？", "definition")

    # ---- 老数据兜底（没有 entities 字段）----

    def test_legacy_stopwords_only_no_hit(self):
        """T41 关键：老数据只有停用词 keywords → 不误命中"""
        legacy_skill = {
            "skill_id": "langgraph_purpose",
            "trigger_keywords": ["是什么", "用途", "框架", "功能", "简介"],
            # 无 entities 字段
        }
        # question 含"是什么"但无真实实体——不应命中（停用词被过滤）
        assert not _matches_skill(legacy_skill, "fastapi 是什么？", "definition")

    def test_legacy_strong_keyword_hits(self):
        """老数据含强 keyword（实体名）→ 命中"""
        legacy_skill = {
            "skill_id": "langgraph_purpose",
            "trigger_keywords": ["LangGraph", "是什么", "用途"],
            # 无 entities 字段
        }
        # question 含 LangGraph → 命中（LangGraph 非停用词）
        assert _matches_skill(legacy_skill, "langgraph 是什么？", "definition")
        # question 无 LangGraph 但含"是什么" → 不命中（停用词被过滤）
        assert not _matches_skill(legacy_skill, "fastapi 是什么？", "definition")


# ---------------------------------------------------------------------------
# hit_check_node：端到端集成（临时 index.json）
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_crystallized(tmp_path, monkeypatch):
    """临时 crystallized 目录 + index."""
    from brain_base.nodes import crystallize as cry_mod

    cry_dir = tmp_path / "crystallized"
    cry_dir.mkdir()
    idx = cry_dir / "index.json"
    monkeypatch.setattr(cry_mod, "_CRYSTALLIZED_DIR", cry_dir)
    monkeypatch.setattr(cry_mod, "_INDEX_FILE", idx)
    return cry_dir


def _write_skill(cry_dir: Path, skill: dict):
    """写入一条 skill 到临时 index.json + 对应 .md 文件。"""
    idx_path = cry_dir / "index.json"
    idx = json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.exists() else {"skills": [], "version": 1}
    idx["skills"].append(skill)
    idx_path.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    layer = skill.get("layer", "hot")
    sub = "cold/" if layer == "cold" else ""
    md_dir = cry_dir / sub.rstrip("/")
    md_dir.mkdir(exist_ok=True)
    (cry_dir / f"{sub}{skill['skill_id']}.md").write_text(
        f"# {skill['skill_id']}\n", encoding="utf-8",
    )


class TestHitCheckNode:
    def test_legacy_skill_no_entities_stopwords_only_miss(self, tmp_crystallized):
        """**再现 E2E 触发的真实 bug**：老 skill 只有泛词 keywords → 不应误命中 FastAPI 问题。"""
        _write_skill(tmp_crystallized, {
            "skill_id": "langgraph_purpose",
            "layer": "hot",
            "trigger_keywords": ["LangGraph", "是什么", "用途", "框架", "功能", "做什么"],
            # 无 entities 字段（老数据）
            "last_confirmed_at": "2026-05-12",
        })
        # 关键：问 FastAPI，命中停用词"是什么"——T41 前会误命中，T41 后应 miss
        result = hit_check_node({"user_question": "FastAPI 的核心特性是什么？"})
        assert result["status"] == "miss", \
            f"T41 收紧失败：老数据弱 keywords 误命中！实际 {result}"

    def test_legacy_skill_entity_in_keywords_hits(self, tmp_crystallized):
        """老 skill 的 keywords 含实体名（LangGraph）+ 问题含该实体 → 命中。"""
        _write_skill(tmp_crystallized, {
            "skill_id": "langgraph_purpose",
            "layer": "hot",
            "trigger_keywords": ["LangGraph", "是什么"],
            "last_confirmed_at": "2026-05-12",
        })
        result = hit_check_node({"user_question": "LangGraph 是什么？"})
        assert result["status"] == "hit_hot"
        assert result["skill_id"] == "langgraph_purpose"

    def test_new_skill_entity_hit(self, tmp_crystallized):
        """新 skill 含 entities + scenario → entity 命中即可。"""
        _write_skill(tmp_crystallized, {
            "skill_id": "ragflow_intro",
            "layer": "hot",
            "entities": ["RAGFlow"],
            "scenario": "definition",
            "trigger_keywords": [],
            "last_confirmed_at": "2026-05-12",
        })
        result = hit_check_node({"user_question": "RAGFlow 是什么？"})
        assert result["status"] == "hit_hot"
        assert result["skill_id"] == "ragflow_intro"

    def test_new_skill_cross_entity_miss(self, tmp_crystallized):
        """新 skill 含 entities=[LangGraph] → 问 FastAPI 绝不命中。"""
        _write_skill(tmp_crystallized, {
            "skill_id": "langgraph_purpose",
            "layer": "hot",
            "entities": ["LangGraph"],
            "scenario": "definition",
            "trigger_keywords": [],
            "last_confirmed_at": "2026-05-12",
        })
        result = hit_check_node({"user_question": "FastAPI 的核心特性是什么？"})
        assert result["status"] == "miss"

    def test_scenario_mismatch_miss(self, tmp_crystallized):
        """新 skill scenario=definition + question 是 howto → miss（避免用"是什么"的答案回答"怎么装"）。"""
        _write_skill(tmp_crystallized, {
            "skill_id": "ragflow_intro",
            "layer": "hot",
            "entities": ["RAGFlow"],
            "scenario": "definition",
            "trigger_keywords": [],
            "last_confirmed_at": "2026-05-12",
        })
        # 问"如何安装 RAGFlow"是 howto，但 skill 是 definition
        result = hit_check_node({"user_question": "如何安装 RAGFlow？"})
        assert result["status"] == "miss"
