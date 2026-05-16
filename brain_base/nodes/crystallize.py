"""
Crystallize 子图节点函数。

模式：
1. hit_check：固化层命中判断（hot → cold 两阶段）
2. value_score：四维度评分（LLM）
3. skill_gen：把 QA 对改写成固化条目（LLM）
4. crystallize_write：把固化条目写入 data/crystallized/
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from brain_base.agents.schemas import CrystallizedSkill, ValueScore
from brain_base.agents.utils.structured import invoke_structured
from brain_base.prompts.crystallize_prompts import (
    CRYSTALLIZE_SKILL_SYSTEM_PROMPT,
    HIT_CHECK_SYSTEM_PROMPT,
    VALUE_SCORE_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

_CRYSTALLIZED_DIR = Path("data/crystallized")
_INDEX_FILE = _CRYSTALLIZED_DIR / "index.json"
_FRESHNESS_TTL_DAYS = 30


def _load_index() -> dict[str, Any]:
    """读取 index.json，损坏时备份并重建。"""
    if not _INDEX_FILE.exists():
        _CRYSTALLIZED_DIR.mkdir(parents=True, exist_ok=True)
        empty = {"skills": [], "version": 1}
        _INDEX_FILE.write_text(
            json.dumps(empty, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return empty
    try:
        return json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = _INDEX_FILE.with_suffix(
            f".broken-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        )
        _INDEX_FILE.rename(backup)
        empty = {"skills": [], "version": 1}
        _INDEX_FILE.write_text(
            json.dumps(empty, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return empty


def _save_index(index: dict[str, Any]) -> None:
    _INDEX_FILE.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_skill_md(skill_id: str) -> str:
    """读取固化 skill 的 Markdown。"""
    if not skill_id:
        return ""
    for sub in ("", "cold/"):
        path = _CRYSTALLIZED_DIR / f"{sub}{skill_id}.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
    return ""


# ---------------------------------------------------------------------------
# 命中判断（纯规则）：先 hot 再 cold
# T41 收紧：entity-first 匹配 + scenario 二次过滤；老数据走停用词黑名单兜底
# ---------------------------------------------------------------------------


# T41：scenario 推断正则（hit_check 用，纯规则不调 LLM）
_SCENARIO_PATTERNS: dict[str, re.Pattern] = {
    "definition":   re.compile(r"(是什么|做什么|用途|用来|简介|介绍|概念|特性|what is)", re.I),
    "howto":        re.compile(r"(怎么|如何|使用方法|部署|安装|how to|install|setup)", re.I),
    "compare":      re.compile(r"( vs |对比|区别|差异|谁更|哪个好|compare)", re.I),
    "troubleshoot": re.compile(r"(报错|失败|错误|异常|为什么.*不|why.*fail|debug)", re.I),
    "config":       re.compile(r"(配置|参数|选项|configure|setting)", re.I),
    "update":       re.compile(r"(最近|最新|更新|版本|changelog|latest update)", re.I),
}


def _infer_scenario(question: str) -> str:
    """从问题推断 scenario；纯规则，无匹配返回 general。

    优先级：troubleshoot > update > compare > howto > config > definition > general。
    troubleshoot/update 语义更具体（问"最近怎么报错"应归 troubleshoot，不是 definition）。
    """
    for scenario in ("troubleshoot", "update", "compare", "howto", "config", "definition"):
        if _SCENARIO_PATTERNS[scenario].search(question):
            return scenario
    return "general"


# T41：老数据（没有 entities 字段）兜底——拦截这些弱关键词匹配，防止误命中
_STOP_KEYWORDS = frozenset({
    # 疑问词
    "是什么", "做什么", "怎么", "怎样", "如何", "哪些", "哪个", "为什么", "有什么",
    # 泛词 / 属性词
    "功能", "用途", "用来", "简介", "介绍", "概念", "特性", "内容",
    # 动词 / 辅助词
    "做", "用", "使用", "安装", "部署", "配置", "支持", "可以", "是",
    # 通用名词（通常不是独特实体）
    "框架", "工具", "系统", "方法", "方式", "文档",
})


def _matches_skill(skill: dict[str, Any], question_lower: str, question_scenario: str) -> bool:
    """判断 skill 是否命中 question（T41 entity-first 匹配）。

    命中规则：
    1. **主过滤（entity）**：skill.entities 必须 ≥1 项 substring 命中 question（case-insensitive）。
    2. **辅过滤（scenario）**：若 skill.scenario 与 question_scenario 都非 "general"，
       必须相等才算命中；任一为 "general" 走宽松（只看 entity）。
    3. **老数据兜底**：skill 无 entities 字段（pre-T41 数据）→ 退化为
       trigger_keywords 匹配 + 停用词黑名单过滤（至少 1 个非停用词的 keyword 命中）。
    """
    entities = skill.get("entities", [])
    if entities:
        if not any(str(e).lower() in question_lower for e in entities if e):
            return False
        skill_scenario = skill.get("scenario", "general")
        if skill_scenario != "general" and question_scenario != "general":
            if skill_scenario != question_scenario:
                return False
        return True

    # 兜底：老数据没有 entities，走严格的 trigger_keywords 匹配
    keywords = skill.get("trigger_keywords", [])
    strong_hit = any(
        str(kw).lower() in question_lower
        for kw in keywords
        if kw and str(kw) not in _STOP_KEYWORDS
    )
    return strong_hit


def hit_check_node(state: dict[str, Any]) -> dict[str, Any]:
    """固化层命中判断：先 hot 后 cold（T41：entity-first + scenario 二次过滤）。"""
    user_question = state.get("user_question", "")
    if not _CRYSTALLIZED_DIR.is_dir():
        return {"status": "degraded"}

    try:
        index = _load_index()
    except Exception:
        return {"status": "degraded"}

    skills = index.get("skills", [])
    question_lower = user_question.lower()
    question_scenario = _infer_scenario(user_question)

    for skill in skills:
        if skill.get("layer", "hot") != "hot":
            continue
        if not _matches_skill(skill, question_lower, question_scenario):
            continue
        return {
            "status": "hit_hot",
            "skill_id": skill.get("skill_id", ""),
            "answer_markdown": _read_skill_md(skill.get("skill_id", "")),
            "layer": "hot",
            "last_confirmed_at": skill.get("last_confirmed_at", ""),
        }

    for skill in skills:
        if skill.get("layer", "cold") != "cold":
            continue
        if not _matches_skill(skill, question_lower, question_scenario):
            continue
        skill["hit_count"] = skill.get("hit_count", 0) + 1
        skill["last_hit_at"] = date.today().isoformat()
        _save_index(index)

        if skill["hit_count"] >= 3:
            return {
                "status": "cold_promoted",
                "skill_id": skill.get("skill_id", ""),
                "answer_markdown": _read_skill_md(skill.get("skill_id", "")),
                "layer": "cold",
            }

        return {
            "status": "cold_observed",
            "skill_id": skill.get("skill_id", ""),
            "cold_evidence_summary": skill.get("description", ""),
        }

    return {"status": "miss"}


def freshness_check_node(state: dict[str, Any]) -> dict[str, Any]:
    """新鲜度判断：hit_hot 后判断是否过期（hit_fresh / hit_stale）。"""
    last_confirmed = state.get("last_confirmed_at", "")
    if not last_confirmed:
        return {"status": "hit_stale"}

    try:
        confirmed_date = date.fromisoformat(last_confirmed)
    except (ValueError, TypeError):
        return {"status": "hit_stale"}

    ttl_days = state.get("freshness_ttl_days", _FRESHNESS_TTL_DAYS)
    expires_at = confirmed_date + timedelta(days=ttl_days)
    if date.today() < expires_at:
        return {"status": "hit_fresh"}
    return {"status": "hit_stale"}


# ---------------------------------------------------------------------------
# LLM 节点：value_score / hit_check_llm / skill_gen
# ---------------------------------------------------------------------------


def create_value_score_node(llm: Any = None) -> Callable:
    """价值评分节点工厂（四维度）。

    llm=None：启发式中间分（每维 0.5），落到 cold 层。
    llm 非 None：用 `ValueScore` schema 让 LLM 直接返回类型化对象。
    """

    def value_score_node(state: dict[str, Any]) -> dict[str, Any]:
        user_question = state.get("user_question", "")
        answer_markdown = state.get("answer_markdown", "")

        if not user_question or not answer_markdown:
            return {"value_score": 0.0, "recommended_layer": "skip"}

        if llm is None:
            return {
                "value_score": 0.5,
                "generality": 0.5,
                "stability": 0.5,
                "evidence_quality": 0.5,
                "cost_benefit": 0.5,
                "recommended_layer": "cold",
                # T41：启发式分支无法抽 entity，给空列表 + general，crystallize_write 兜底
                "entities": [],
                "scenario": "general",
                "trigger_keywords": [],
                "description": user_question[:80],
            }

        prompt_body = (
            f"用户问题：\n{user_question}\n\n"
            f"答案 Markdown（节选 1500 字）：\n{answer_markdown[:1500]}"
        )
        try:
            result = invoke_structured(
                llm, ValueScore, VALUE_SCORE_SYSTEM_PROMPT, prompt_body
            )
        except Exception as exc:
            logger.warning(
                "value_score LLM 评分失败（降级为 skip）: %s: %s | question=%r",
                type(exc).__name__, str(exc)[:200], user_question[:80],
            )
            return {"value_score": 0.0, "recommended_layer": "skip"}

        return {
            "value_score": result.composite_score,
            "generality": result.generality,
            "stability": result.stability,
            "evidence_quality": result.evidence_quality,
            "cost_benefit": result.cost_benefit,
            "recommended_layer": result.recommended_layer,
            # T41 新增：entity/scenario 写入 state 顶层（skill_gen 可复用 / crystallize_write 兜底）
            "entities": list(result.entities),
            "scenario": result.scenario,
            "trigger_keywords": list(result.trigger_keywords),
            "description": result.reason or user_question[:80],
        }

    return value_score_node


def create_skill_gen_node(llm: Any = None) -> Callable:
    """把 QA 对改写成固化 skill 条目（CrystallizedSkill schema）。"""

    def skill_gen_node(state: dict[str, Any]) -> dict[str, Any]:
        user_question = state.get("user_question", "")
        answer_markdown = state.get("answer_markdown", "")
        if not user_question or not answer_markdown:
            return {"skill_payload": None}

        if llm is None:
            # 降级：用问题串生成最简骨架
            # T41：entities 来自 state（value_score 已抽过，降级通常是 heuristic，为空列表）
            return {
                "skill_payload": {
                    "skill_id": _generate_skill_id(user_question),
                    "title": user_question[:80],
                    "description": user_question[:300],
                    "entities": state.get("entities", []),
                    "scenario": state.get("scenario", "general"),
                    "trigger_keywords": state.get("trigger_keywords", []),
                    "layer": state.get("recommended_layer", "cold"),
                    "answer_markdown": answer_markdown,
                }
            }

        try:
            result = invoke_structured(
                llm,
                CrystallizedSkill,
                CRYSTALLIZE_SKILL_SYSTEM_PROMPT,
                f"问题：{user_question}\n\n答案：\n{answer_markdown[:2000]}",
            )
        except Exception as exc:
            logger.warning(
                "skill_gen LLM 生成失败（降级为 None）: %s: %s | question=%r",
                type(exc).__name__, str(exc)[:200], user_question[:80],
            )
            return {"skill_payload": None}

        return {"skill_payload": result.model_dump()}

    return skill_gen_node


# ---------------------------------------------------------------------------
# 写入（纯逻辑）
# ---------------------------------------------------------------------------


def crystallize_write_node(state: dict[str, Any]) -> dict[str, Any]:
    """写入新固化条目。

    优先使用 `skill_payload`（CrystallizedSkill 输出）；缺失时回退到
    `state` 顶层字段（向后兼容）。
    """
    user_question = state.get("user_question", "")
    answer_markdown = state.get("answer_markdown", "")
    if not answer_markdown:
        return {"status": "skipped", "skip_reason": "无答案内容"}

    value_score = state.get("value_score", 0.0)
    if value_score < 0.3:
        return {
            "status": "skipped",
            "skip_reason": f"value_score={value_score:.2f} < 0.3",
        }

    payload = state.get("skill_payload") or {}
    layer = payload.get("layer") or ("hot" if value_score >= 0.6 else "cold")
    skill_id = (
        payload.get("skill_id")
        or state.get("skill_id")
        or _generate_skill_id(user_question)
    )
    trigger_keywords = payload.get("trigger_keywords") or state.get(
        "trigger_keywords", []
    )
    # T41：entities 主匹配字段，从 skill_payload 优先；value_score 兜底；都没有用空列表
    entities = payload.get("entities") or state.get("entities") or []
    scenario = payload.get("scenario") or state.get("scenario") or "general"
    description = (
        payload.get("description")
        or state.get("description")
        or user_question[:80]
    )
    skill_md = payload.get("answer_markdown") or answer_markdown

    sub_dir = "cold" if layer == "cold" else ""
    target_dir = _CRYSTALLIZED_DIR / sub_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / f"{skill_id}.md"

    fm = (
        "---\n"
        f"skill_id: {skill_id}\n"
        f"layer: {layer}\n"
        f"value_score: {value_score:.2f}\n"
        f"created_at: {date.today().isoformat()}\n"
        f"last_confirmed_at: {date.today().isoformat()}\n"
        f"freshness_ttl_days: {_FRESHNESS_TTL_DAYS}\n"
        f"entities: {json.dumps(entities, ensure_ascii=False)}\n"
        f"scenario: {scenario}\n"
        f"trigger_keywords: {json.dumps(trigger_keywords, ensure_ascii=False)}\n"
        "---\n\n"
    )
    target_file.write_text(fm + skill_md, encoding="utf-8")

    index = _load_index()
    entry = {
        "skill_id": skill_id,
        "layer": layer,
        "value_score": value_score,
        "created_at": date.today().isoformat(),
        "last_confirmed_at": date.today().isoformat(),
        "freshness_ttl_days": _FRESHNESS_TTL_DAYS,
        "entities": entities,
        "scenario": scenario,
        "trigger_keywords": trigger_keywords,
        "hit_count": 0,
        "last_hit_at": None,
        "description": description,
    }
    skills = index.get("skills", [])
    for i, s in enumerate(skills):
        if s.get("skill_id") == skill_id:
            skills[i] = entry
            break
    else:
        skills.append(entry)
    index["skills"] = skills
    _save_index(index)

    return {
        "status": f"created_{layer}",
        "skill_id": skill_id,
        "layer": layer,
        "value_score": value_score,
    }


def _generate_skill_id(question: str) -> str:
    """从问题生成 skill_id。"""
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", question.lower())[:40].strip("-")
    return f"{slug}-{date.today().isoformat()}"


# 向后兼容
value_score_node = create_value_score_node(llm=None)
skill_gen_node = create_skill_gen_node(llm=None)
