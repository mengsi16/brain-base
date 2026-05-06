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
# ---------------------------------------------------------------------------


def hit_check_node(state: dict[str, Any]) -> dict[str, Any]:
    """固化层命中判断：先 hot 后 cold。"""
    user_question = state.get("user_question", "")
    if not _CRYSTALLIZED_DIR.is_dir():
        return {"status": "degraded"}

    try:
        index = _load_index()
    except Exception:
        return {"status": "degraded"}

    skills = index.get("skills", [])
    question_lower = user_question.lower()

    for skill in skills:
        if skill.get("layer", "hot") != "hot":
            continue
        keywords = skill.get("trigger_keywords", [])
        if not any(kw.lower() in question_lower for kw in keywords):
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
        keywords = skill.get("trigger_keywords", [])
        if not any(kw.lower() in question_lower for kw in keywords):
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
                "trigger_keywords": [user_question[:20]],
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
        except Exception:
            return {"value_score": 0.0, "recommended_layer": "skip"}

        return {
            "value_score": result.composite_score,
            "generality": result.generality,
            "stability": result.stability,
            "evidence_quality": result.evidence_quality,
            "cost_benefit": result.cost_benefit,
            "recommended_layer": result.recommended_layer,
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
            return {
                "skill_payload": {
                    "skill_id": _generate_skill_id(user_question),
                    "title": user_question[:80],
                    "description": user_question[:300],
                    "trigger_keywords": [user_question[:20], "skill", "default"],
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
        except Exception:
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
