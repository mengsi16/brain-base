"""
Lint 节点函数：固化层周期清理。

流程：scan → check_freshness → degrade_expired → delete_rejected
参考 ../brain-base-backup/skills/crystallize-lint/SKILL.md
"""

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

_CRYSTALLIZED_DIR = Path("data/crystallized")


def scan_crystallized_node(state: dict[str, Any]) -> dict[str, Any]:
    """扫描固化层所有条目"""
    index_path = _CRYSTALLIZED_DIR / "index.json"
    if not index_path.exists():
        return {"entries": [], "scan_status": "no_index"}

    try:
        idx = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"entries": [], "scan_status": "corrupted"}

    entries = idx.get("skills", [])
    return {"entries": entries, "scan_status": "ok"}


def check_freshness_node(state: dict[str, Any]) -> dict[str, Any]:
    """检查每条固化条目的新鲜度，标记需要降级/删除的"""
    entries = state.get("entries", [])
    today = date.today()

    to_degrade: list[str] = []   # hot → cold
    to_delete: list[str] = []    # cold → 删除
    to_keep: list[str] = []

    for entry in entries:
        skill_id = entry.get("skill_id", "")
        layer = entry.get("layer", "hot")
        ttl_days = entry.get("freshness_ttl_days", 30)
        last_confirmed = entry.get("last_confirmed_at", "")
        last_hit = entry.get("last_hit_at", "")
        feedback = entry.get("user_feedback", "")

        # rejected 条目直接标记删除
        if feedback == "rejected":
            to_delete.append(skill_id)
            continue

        # confirmed 条目不降级
        if feedback == "confirmed":
            to_keep.append(skill_id)
            continue

        # 计算过期时间
        try:
            confirmed_date = date.fromisoformat(last_confirmed) if last_confirmed else today - timedelta(days=999)
        except ValueError:
            confirmed_date = today - timedelta(days=999)

        expires_at = confirmed_date + timedelta(days=ttl_days * 3)  # hot 降级阈值：3x TTL

        if layer == "hot":
            if today > expires_at:
                to_degrade.append(skill_id)
            else:
                to_keep.append(skill_id)
        elif layer == "cold":
            cold_expires = confirmed_date + timedelta(days=ttl_days * 6)  # cold 删除阈值：6x TTL
            if today > cold_expires:
                to_delete.append(skill_id)
            else:
                to_keep.append(skill_id)

    return {
        "to_degrade": to_degrade,
        "to_delete": to_delete,
        "to_keep": to_keep,
    }


def degrade_expired_node(state: dict[str, Any]) -> dict[str, Any]:
    """将过期的 hot 条目降级到 cold"""
    to_degrade = state.get("to_degrade", [])
    if not to_degrade:
        return {}

    index_path = _CRYSTALLIZED_DIR / "index.json"
    if not index_path.exists():
        return {}

    try:
        idx = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    cold_dir = _CRYSTALLIZED_DIR / "cold"
    cold_dir.mkdir(parents=True, exist_ok=True)

    degraded: list[str] = []
    for skill in idx.get("skills", []):
        skill_id = skill.get("skill_id", "")
        if skill_id not in to_degrade:
            continue
        # 移动文件
        hot_path = _CRYSTALLIZED_DIR / f"{skill_id}.md"
        cold_path = cold_dir / f"{skill_id}.md"
        if hot_path.exists():
            import shutil
            shutil.move(str(hot_path), str(cold_path))
        # 更新 index
        skill["layer"] = "cold"
        degraded.append(skill_id)

    # 写回 index
    tmp = index_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(index_path)

    return {"degraded": degraded}


def delete_rejected_node(state: dict[str, Any]) -> dict[str, Any]:
    """删除 rejected 和超期 cold 条目"""
    to_delete = state.get("to_delete", [])
    if not to_delete:
        return {}

    index_path = _CRYSTALLIZED_DIR / "index.json"
    if not index_path.exists():
        return {}

    try:
        idx = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    deleted: list[str] = []
    remaining = []
    for skill in idx.get("skills", []):
        skill_id = skill.get("skill_id", "")
        if skill_id in to_delete:
            # 删除文件
            for sub in ("", "cold/"):
                path = _CRYSTALLIZED_DIR / f"{sub}{skill_id}.md"
                if path.exists():
                    path.unlink()
            deleted.append(skill_id)
        else:
            remaining.append(skill)

    # 写回 index
    idx["skills"] = remaining
    tmp = index_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(index_path)

    return {"deleted": deleted}
