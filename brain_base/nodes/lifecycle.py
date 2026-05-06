"""
Lifecycle 节点函数：文档生命周期管理（删除/归档）。

流程：resolve → scan → dry_run → [confirm] → delete_milvus → delete_files → clean_index → audit
参考 ../brain-base-backup/skills/lifecycle-workflow/SKILL.md

核心逻辑直接 import bin/ 下的 Python 模块，不通过 subprocess。
"""

import importlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path, Path as _P
from typing import Any

# 延迟导入 milvus-cli
def _get_milvus_cli():
    _BIN_DIR = str(_P(__file__).resolve().parent.parent.parent.parent / "bin")
    if _BIN_DIR not in sys.path:
        sys.path.insert(0, _BIN_DIR)
    return importlib.import_module("bin.milvus-cli")


def resolve_doc_ids_node(state: dict[str, Any]) -> dict[str, Any]:
    """解析输入，得到权威 doc_id 列表"""
    doc_ids = state.get("doc_ids", [])
    urls = state.get("urls", [])
    sha256 = state.get("sha256", "")

    resolved = list(doc_ids)

    # 通过 url 解析 doc_id
    if urls:
        raw_dir = Path("data/docs/raw")
        for url in urls:
            for raw_file in raw_dir.glob("*.md"):
                text = raw_file.read_text(encoding="utf-8", errors="ignore")
                if f"url: {url}" in text:
                    resolved.append(raw_file.stem)

    # 通过 sha256 解析 doc_id
    if sha256:
        try:
            milvus_cli = _get_milvus_cli()
            result = milvus_cli.hash_lookup(sha256)
            for match in result.get("matches", []):
                doc_id = match.get("doc_id", "")
                if doc_id and doc_id not in resolved:
                    resolved.append(doc_id)
        except Exception:
            pass

    if not resolved:
        return {"error": "未找到匹配的文档", "resolved_doc_ids": []}

    return {"resolved_doc_ids": resolved}


def scan_impact_node(state: dict[str, Any]) -> dict[str, Any]:
    """对每个 doc_id 扫描影响面"""
    doc_ids = state.get("resolved_doc_ids", [])
    force_recent = state.get("force_recent", False)
    targets: list[dict] = []

    for doc_id in doc_ids:
        target: dict[str, Any] = {
            "doc_id": doc_id,
            "raw_path": "",
            "chunks_paths": [],
            "milvus_chunk_ids": [],
            "doc2query_chunk_ids": [],
            "crystallized_skills_referencing": [],
            "recent_protection": False,
        }

        # raw 路径
        raw_path = Path(f"data/docs/raw/{doc_id}.md")
        if raw_path.exists():
            target["raw_path"] = str(raw_path)
            # 新文件保护：mtime < 10 分钟
            mtime = raw_path.stat().st_mtime
            if (time.time() - mtime) < 600 and not force_recent:
                target["recent_protection"] = True

        # chunks 路径
        chunks_dir = Path("data/docs/chunks")
        if chunks_dir.is_dir():
            target["chunks_paths"] = [
                str(p) for p in chunks_dir.glob(f"{doc_id}-*.md")
            ]

        # Milvus chunk_ids（从文件系统推断）
        target["milvus_chunk_ids"] = [
            Path(p).stem for p in target["chunks_paths"]
        ]

        # doc2query-index 条目
        d2q_path = Path("data/eval/doc2query-index.json")
        if d2q_path.exists():
            try:
                d2q = json.loads(d2q_path.read_text(encoding="utf-8"))
                for chunk_id in d2q:
                    if chunk_id.startswith(f"{doc_id}-"):
                        target["doc2query_chunk_ids"].append(chunk_id)
            except (json.JSONDecodeError, TypeError):
                pass

        # crystallized 引用
        for index_path in [
            Path("data/crystallized/index.json"),
            Path("data/crystallized/cold/index.json"),
        ]:
            if not index_path.exists():
                continue
            try:
                idx = json.loads(index_path.read_text(encoding="utf-8"))
                for skill in idx.get("skills", []):
                    source_chunks = skill.get("source_chunks", [])
                    source_docs = skill.get("source_docs", [])
                    if any(doc_id in sc or doc_id in sd for sc, sd in zip(source_chunks, source_docs)):
                        target["crystallized_skills_referencing"].append(skill.get("skill_id", ""))
            except (json.JSONDecodeError, TypeError):
                pass

        targets.append(target)

    return {"targets": targets}


def dry_run_report_node(state: dict[str, Any]) -> dict[str, Any]:
    """输出 dry-run 清单"""
    targets = state.get("targets", [])
    confirm = state.get("confirm", False)

    total_chunks = sum(len(t["chunks_paths"]) for t in targets)
    total_skills = sum(len(t["crystallized_skills_referencing"]) for t in targets)

    report = {
        "mode": "remove_doc",
        "confirm": confirm,
        "targets": targets,
        "dry_run_summary": {
            "docs_to_remove": len(targets),
            "chunks_to_remove": total_chunks,
            "skills_to_mark_rejected": total_skills,
        },
    }
    return {"dry_run_report": report}


def delete_milvus_node(state: dict[str, Any]) -> dict[str, Any]:
    """执行 Milvus 行删除（confirm=true 时）"""
    if not state.get("confirm", False):
        return {}

    doc_ids = state.get("resolved_doc_ids", [])
    try:
        milvus_cli = _get_milvus_cli()
        result = milvus_cli.delete_by_doc_ids(doc_ids=doc_ids, confirm=True)
        return {"milvus_delete_result": result}
    except Exception as exc:
        return {"error": f"Milvus 删除失败: {exc}", "milvus_delete_failed": True}


def delete_files_node(state: dict[str, Any]) -> dict[str, Any]:
    """执行文件系统删除"""
    if not state.get("confirm", False):
        return {}
    if state.get("milvus_delete_failed", False):
        return {}

    targets = state.get("targets", [])
    errors: list[str] = []

    for target in targets:
        # 删除 raw
        raw = Path(target.get("raw_path", ""))
        if raw.exists():
            try:
                raw.unlink()
            except OSError as exc:
                errors.append(f"删除 raw 失败 {raw}: {exc}")

        # 删除 chunks
        for cp in target.get("chunks_paths", []):
            chunk = Path(cp)
            if chunk.exists():
                try:
                    chunk.unlink()
                except OSError as exc:
                    errors.append(f"删除 chunk 失败 {chunk}: {exc}")

        # 删除 uploads 目录
        uploads_dir = Path(f"data/docs/uploads/{target['doc_id']}")
        if uploads_dir.is_dir():
            import shutil
            try:
                shutil.rmtree(uploads_dir)
            except OSError as exc:
                errors.append(f"删除 uploads 失败 {uploads_dir}: {exc}")

    return {"file_delete_errors": errors}


def clean_index_node(state: dict[str, Any]) -> dict[str, Any]:
    """清理 doc2query-index 和 crystallized index"""
    if not state.get("confirm", False):
        return {}

    doc_ids = state.get("resolved_doc_ids", [])
    targets = state.get("targets", [])
    errors: list[str] = []

    # doc2query-index 清理
    d2q_path = Path("data/eval/doc2query-index.json")
    if d2q_path.exists():
        try:
            d2q = json.loads(d2q_path.read_text(encoding="utf-8"))
            for doc_id in doc_ids:
                keys_to_remove = [k for k in d2q if k.startswith(f"{doc_id}-")]
                for k in keys_to_remove:
                    del d2q[k]
            tmp = d2q_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d2q, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.rename(d2q_path)
        except Exception as exc:
            errors.append(f"doc2query-index 清理失败: {exc}")

    # crystallized index 标记 rejected
    for index_path in [
        Path("data/crystallized/index.json"),
        Path("data/crystallized/cold/index.json"),
    ]:
        if not index_path.exists():
            continue
        try:
            idx = json.loads(index_path.read_text(encoding="utf-8"))
            for skill in idx.get("skills", []):
                source_chunks = skill.get("source_chunks", [])
                source_docs = skill.get("source_docs", [])
                if any(did in sc or did in sd for did in doc_ids for sc, sd in zip(source_chunks, source_docs)):
                    skill["user_feedback"] = "rejected"
                    skill["lifecycle_rejected_reason"] = f"source doc {doc_ids} removed"
            tmp = index_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.rename(index_path)
        except Exception as exc:
            errors.append(f"crystallized index 标记失败 {index_path}: {exc}")

    return {"index_clean_errors": errors}


def audit_log_node(state: dict[str, Any]) -> dict[str, Any]:
    """写审计日志"""
    if not state.get("confirm", False):
        return {}

    doc_ids = state.get("resolved_doc_ids", [])
    reason = state.get("reason", "")
    audit_path = Path("data/lifecycle-audit.jsonl")
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "ts": datetime.now().isoformat(),
        "mode": "remove_doc",
        "doc_ids": doc_ids,
        "reason": reason,
        "summary": state.get("dry_run_report", {}).get("dry_run_summary", {}),
    }

    with open(audit_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return {"audit_log_path": str(audit_path)}
