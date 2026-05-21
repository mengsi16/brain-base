# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except Exception:
    pass

QA_CASES = [
    {
        "id": "qa_langgraph",
        "question": "LangGraph 的 StateGraph 和条件边分别解决什么问题？",
    },
    {
        "id": "qa_milvus_hybrid",
        "question": "Milvus hybrid search 中 dense 和 sparse 检索各自适合什么场景？",
    },
    {
        "id": "qa_langgraph_repeat",
        "question": "LangGraph 的 StateGraph 和条件边分别解决什么问题？",
    },
]

UPLOAD_CASES = [
    {
        "id": "upload_skillrouter",
        "path": REPO_ROOT / "papers" / "2026_SkillRouter_Skill_Routing_for_LLM_Agents_at_Scale.pdf",
        "question": "SkillRouter 这篇论文如何为 LLM agents 做 skill routing？",
        "expected_terms": ["skillrouter", "skill", "routing"],
    },
    {
        "id": "upload_mambaout",
        "path": REPO_ROOT / "papers" / "MambaOut Do WeReally Need Mamba for Vision.pdf",
        "question": "MambaOut 论文的核心结论是什么，为什么说视觉任务未必需要 Mamba？",
        "expected_terms": ["mambaout", "mamba", "vision"],
    },
]


def _now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _short_error(exc: BaseException) -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }


def _file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _frontmatter_value(text: str, key: str) -> str:
    if not text.startswith("---"):
        return ""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return ""
    prefix = f"{key}:"
    for line in parts[1].splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


def _find_raw_doc_ids_by_sha(sha256: str) -> list[str]:
    raw_dir = REPO_ROOT / "data" / "docs" / "raw"
    if not raw_dir.is_dir():
        return []
    doc_ids: list[str] = []
    for raw_file in sorted(raw_dir.glob("*.md")):
        try:
            text = raw_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _frontmatter_value(text, "content_sha256") == sha256:
            doc_ids.append(raw_file.stem)
    return doc_ids


def _expected_doc_id(path: Path) -> str:
    import importlib

    doc_converter = importlib.import_module("bin.doc-converter")
    return doc_converter.make_doc_id(path.stem, upload_date=date.today())


def _all_evidence(state: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = [e for e in state.get("evidence", []) or [] if isinstance(e, dict)]
    for group in state.get("sub_evidence", []) or []:
        if not isinstance(group, dict):
            continue
        for chunk in group.get("chunks", []) or []:
            if isinstance(chunk, dict):
                evidence.append(chunk)
    return evidence


def _evidence_matches_doc(state: dict[str, Any], doc_id: str, terms: list[str]) -> bool:
    evidence = _all_evidence(state)
    lowered_terms = [t.lower() for t in terms]
    for item in evidence:
        haystack = " ".join(
            str(item.get(k, ""))
            for k in ("doc_id", "chunk_id", "title", "summary", "chunk_text", "source")
        ).lower()
        if doc_id.lower() in haystack:
            return True
        if lowered_terms and all(term in haystack for term in lowered_terms[:2]):
            return True
    return False


def _count_milvus_rows(doc_id: str) -> int:
    import importlib

    from pymilvus import Collection, connections, utility

    milvus_cli = importlib.import_module("bin.milvus-cli")
    settings = milvus_cli.load_runtime_settings()
    connections.connect(
        alias="default",
        uri=settings["milvus_uri"],
        token=settings["milvus_token"],
        db_name=settings["milvus_db"],
    )
    collection_name = settings["milvus_collection"]
    if not utility.has_collection(collection_name):
        return 0
    collection = Collection(collection_name)
    collection.load()
    rows = collection.query(
        expr=f'doc_id == "{doc_id}"',
        output_fields=["doc_id"],
        limit=16384,
    )
    return len(rows)


def _direct_search_has_doc(query: str, doc_id: str) -> tuple[bool, dict[str, Any]]:
    import importlib

    milvus_cli = importlib.import_module("bin.milvus-cli")
    result = milvus_cli.multi_query_search(
        queries=[query],
        top_k_per_query=20,
        final_k=10,
        use_rerank=False,
    )
    hits = result.get("results", []) or []
    return any(hit.get("doc_id") == doc_id for hit in hits), result


def _build_llm_or_fail():
    from brain_base.cli import _build_llm_from_env

    llm = _build_llm_from_env()
    if llm is None:
        raise RuntimeError(
            "未配置 LLM API key；T58 E2E 必须真实调用 LLM，缺 key 不允许 skip。"
        )
    return llm


def _gpu_free_mb() -> int | None:
    """读 nvidia-smi 拿单卡 free 显存（MiB）。nvidia-smi 不可用时返回 None。"""
    import subprocess

    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        line = (proc.stdout or "").strip().split("\n")[0]
        return int(line.strip())
    except Exception:
        return None


def _wait_for_gpu_release(target_mb: int, max_wait_sec: int = 120, poll_sec: int = 3) -> dict[str, Any]:
    """等 GPU 空闲显存恢复到 target_mb。

    解决 Windows WDDM 调度下 MinerU 子进程退出后驱动延迟回收显存，导致下一个
    upload case 的显存预检（KB_MINERU_VRAM_LIMIT_MB）fail。详见
    `md/research/2026-05-21-t58-e2e-baseline.md` 「根因定位」。
    """
    started = time.time()
    while True:
        free = _gpu_free_mb()
        elapsed = time.time() - started
        if free is None:
            return {"ok": True, "skipped": True, "reason": "nvidia-smi 不可用", "elapsed_sec": round(elapsed, 1)}
        if free >= target_mb:
            return {"ok": True, "free_mb": free, "target_mb": target_mb, "elapsed_sec": round(elapsed, 1)}
        if elapsed >= max_wait_sec:
            return {
                "ok": False, "free_mb": free, "target_mb": target_mb,
                "elapsed_sec": round(elapsed, 1), "reason": "timeout",
            }
        time.sleep(poll_sec)


def _check_infra() -> dict[str, Any]:
    report: dict[str, Any] = {"ok": True, "checks": {}}
    try:
        from pymilvus import MilvusClient

        client = MilvusClient(uri=os.environ.get("KB_MILVUS_URI") or "http://localhost:19530")
        report["checks"]["milvus"] = {"ok": True, "collections": client.list_collections()}
    except Exception as exc:
        report["ok"] = False
        report["checks"]["milvus"] = {"ok": False, "error": _short_error(exc)}
    try:
        llm = _build_llm_or_fail()
        report["checks"]["llm"] = {"ok": True, "type": type(llm).__name__}
    except Exception as exc:
        report["ok"] = False
        report["checks"]["llm"] = {"ok": False, "error": _short_error(exc)}
    try:
        import importlib

        doc_converter = importlib.import_module("bin.doc-converter")
        report["checks"]["doc_converter"] = {"ok": True, "runtime": doc_converter.check_runtime()}
    except Exception as exc:
        report["ok"] = False
        report["checks"]["doc_converter"] = {"ok": False, "error": _short_error(exc)}
    return report


def _cleanup_doc_ids(doc_ids: list[str], reason: str) -> dict[str, Any]:
    if not doc_ids:
        return {"doc_ids": [], "skipped": True}
    from brain_base.graphs.lifecycle_graph import LifecycleGraph

    graph = LifecycleGraph()
    return graph.run(
        doc_ids=sorted(set(doc_ids)),
        confirm=True,
        force_recent=True,
        reason=reason,
    )


def _run_qa_case(qa: Any, case: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    started = time.time()
    result: dict[str, Any] = {
        "id": case["id"],
        "question": case["question"],
        "started_at": datetime.now().isoformat(),
    }
    try:
        state = qa.run(question=case["question"])
        elapsed = time.time() - started
        evidence = _all_evidence(state)
        result.update(
            {
                "passed": bool((state.get("answer") or "").strip()),
                "elapsed_sec": round(elapsed, 3),
                "answer_len": len(state.get("answer") or ""),
                "evidence_count": len(evidence),
                "crystallized_status": state.get("crystallized_status"),
                "self_check_passed": state.get("self_check_passed"),
                "state_path": str(out_dir / f"{case['id']}_state.json"),
            }
        )
        _write_json(out_dir / f"{case['id']}_state.json", state)
    except Exception as exc:
        result.update({"passed": False, "error": _short_error(exc)})
    _write_json(out_dir / f"{case['id']}_summary.json", result)
    return result


def _run_upload_case(llm: Any, qa: Any, case: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    from brain_base.graphs.ingest_file_graph import IngestFileGraph

    path = Path(case["path"])
    doc_id = _expected_doc_id(path)
    sha256 = _file_sha256(path)
    existing_doc_ids = sorted(set([doc_id, *_find_raw_doc_ids_by_sha(sha256)]))
    cleanup_before = _cleanup_doc_ids(existing_doc_ids, f"T58 baseline pre-clean {case['id']}")

    result: dict[str, Any] = {
        "id": case["id"],
        "input_path": str(path),
        "expected_doc_id": doc_id,
        "sha256": sha256,
        "preclean_doc_ids": existing_doc_ids,
        "preclean_result": cleanup_before,
        "started_at": datetime.now().isoformat(),
    }

    started = time.time()
    try:
        ingest_result = IngestFileGraph(llm=llm).run(input_files=[str(path)])
        result["ingest_result"] = ingest_result
        result["ingest_elapsed_sec"] = round(time.time() - started, 3)
        result["ingest_passed"] = (
            not ingest_result.get("conversion_errors")
            and not ingest_result.get("doc_enrich_errors")
            and not ingest_result.get("dedup_skipped")
            and all(not r.get("error") for r in ingest_result.get("persistence_results", []) or [])
        )
        doc_ids = []
        for item in ingest_result.get("converted", []) or []:
            if item.get("doc_id"):
                doc_ids.append(item["doc_id"])
        result["uploaded_doc_ids"] = doc_ids
        result["milvus_rows_after_ingest"] = {did: _count_milvus_rows(did) for did in doc_ids}
        if not result["ingest_passed"] or not doc_ids:
            result["passed"] = False
            _write_json(out_dir / f"{case['id']}_summary.json", result)
            return result
        direct_hit, direct_search = _direct_search_has_doc(case["question"], doc_id)
        result["direct_search_has_doc"] = direct_hit
        result["direct_search_path"] = str(out_dir / f"{case['id']}_direct_search.json")
        _write_json(out_dir / f"{case['id']}_direct_search.json", direct_search)
    except Exception as exc:
        result.update({"ingest_passed": False, "error": _short_error(exc)})
        _write_json(out_dir / f"{case['id']}_summary.json", result)
        return result

    qa_started = time.time()
    try:
        state = qa.run(question=case["question"])
        evidence = _all_evidence(state)
        matched = _evidence_matches_doc(state, doc_id, case.get("expected_terms", []))
        result.update(
            {
                "upload_qa_passed": bool((state.get("answer") or "").strip()) and matched,
                "upload_qa_elapsed_sec": round(time.time() - qa_started, 3),
                "upload_qa_answer_len": len(state.get("answer") or ""),
                "upload_qa_evidence_count": len(evidence),
                "upload_qa_matched_uploaded_doc": matched,
                "upload_qa_state_path": str(out_dir / f"{case['id']}_qa_state.json"),
            }
        )
        _write_json(out_dir / f"{case['id']}_qa_state.json", state)
    except Exception as exc:
        result.update({"upload_qa_passed": False, "upload_qa_error": _short_error(exc)})

    result["passed"] = bool(
        result.get("ingest_passed")
        and result.get("direct_search_has_doc")
        and result.get("upload_qa_passed")
    )
    _write_json(out_dir / f"{case['id']}_summary.json", result)
    return result


def _run_delete_validation(upload_results: list[dict[str, Any]], out_dir: Path) -> dict[str, Any]:
    from brain_base.graphs.lifecycle_graph import LifecycleGraph

    doc_ids = sorted({
        did
        for result in upload_results
        for did in result.get("uploaded_doc_ids", []) or []
    })
    report: dict[str, Any] = {"id": "delete_uploaded_docs", "doc_ids": doc_ids}
    if not doc_ids:
        report.update({"passed": False, "error": "没有可删除的 uploaded_doc_ids"})
        _write_json(out_dir / "delete_summary.json", report)
        return report

    graph = LifecycleGraph()
    try:
        dry_run = graph.run(doc_ids=doc_ids, confirm=False, force_recent=True, reason="T58 baseline dry-run")
        report["dry_run"] = dry_run
        targets = dry_run.get("targets", []) or []
        report["dry_run_passed"] = len(targets) == len(doc_ids)
        report["paths_before_delete"] = {
            target.get("doc_id", ""): {
                "raw_path": target.get("raw_path", ""),
                "chunks_paths": target.get("chunks_paths", []),
                "uploads_dir": str(REPO_ROOT / "data" / "docs" / "uploads" / target.get("doc_id", "")),
            }
            for target in targets
        }
        confirm = graph.run(
            doc_ids=doc_ids,
            confirm=True,
            force_recent=True,
            reason="T58 baseline cleanup uploaded docs",
        )
        report["confirm"] = confirm
        path_checks: dict[str, Any] = {}
        for doc_id in doc_ids:
            raw_path = REPO_ROOT / "data" / "docs" / "raw" / f"{doc_id}.md"
            chunks = sorted((REPO_ROOT / "data" / "docs" / "chunks").glob(f"{doc_id}-*.md"))
            uploads_dir = REPO_ROOT / "data" / "docs" / "uploads" / doc_id
            path_checks[doc_id] = {
                "raw_exists": raw_path.exists(),
                "chunks_remaining": [str(p) for p in chunks],
                "uploads_exists": uploads_dir.exists(),
                "milvus_rows_remaining": _count_milvus_rows(doc_id),
            }
        report["path_checks"] = path_checks
        report["confirm_passed"] = (
            not confirm.get("milvus_delete_failed")
            and not (confirm.get("file_delete_errors") or [])
            and all(
                not item["raw_exists"]
                and not item["chunks_remaining"]
                and not item["uploads_exists"]
                and item["milvus_rows_remaining"] == 0
                for item in path_checks.values()
            )
        )
        report["passed"] = bool(report.get("dry_run_passed") and report.get("confirm_passed"))
    except Exception as exc:
        report.update({"passed": False, "error": _short_error(exc)})
    _write_json(out_dir / "delete_summary.json", report)
    return report


def run_t58_baseline() -> dict[str, Any]:
    os.chdir(REPO_ROOT)
    run_id = _now_id()
    out_dir = REPO_ROOT / "data" / "logs" / "t58_e2e_baseline" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "run_id": run_id,
        "started_at": datetime.now().isoformat(),
        "out_dir": str(out_dir),
        "qa_cases": [],
        "upload_cases": [],
    }

    infra = _check_infra()
    summary["infra"] = infra
    _write_json(out_dir / "infra.json", infra)
    if not infra.get("ok"):
        summary["overall_passed"] = False
        summary["failed_stage"] = "infra"
        _write_json(out_dir / "summary.json", summary)
        return summary

    from brain_base.graphs.qa_graph import QaGraph

    llm = _build_llm_or_fail()
    qa = QaGraph(llm=llm)

    qa_dir = out_dir / "qa"
    for case in QA_CASES:
        summary["qa_cases"].append(_run_qa_case(qa, case, qa_dir))
        _write_json(out_dir / "summary.json", summary)

    upload_dir = out_dir / "upload"
    # 显存预检阈值与 .env / doc-converter 默认一致；case 间用它做等待目标，
    # 防止 MinerU 子进程退出后 Windows WDDM 驱动延迟回收显存导致预检 fail。
    gpu_wait_target_mb = int(os.environ.get("KB_MINERU_VRAM_LIMIT_MB", "13000"))
    for idx, case in enumerate(UPLOAD_CASES):
        if idx > 0:
            wait_report = _wait_for_gpu_release(target_mb=gpu_wait_target_mb)
            print(
                f"[T58] upload case 间等待 GPU 显存释放：{wait_report}",
                file=sys.stderr, flush=True,
            )
        case_result = _run_upload_case(llm, qa, case, upload_dir)
        if idx > 0:
            case_result["gpu_release_wait"] = wait_report
        summary["upload_cases"].append(case_result)
        _write_json(out_dir / "summary.json", summary)

    summary["delete_validation"] = _run_delete_validation(summary["upload_cases"], out_dir / "delete")
    summary["finished_at"] = datetime.now().isoformat()
    summary["overall_passed"] = bool(
        all(item.get("passed") for item in summary["qa_cases"])
        and all(item.get("passed") for item in summary["upload_cases"])
        and summary["delete_validation"].get("passed")
    )
    _write_json(out_dir / "summary.json", summary)
    return summary


@pytest.mark.requires_llm
@pytest.mark.requires_milvus
@pytest.mark.slow
def test_t58_e2e_baseline_real_qa_upload_delete() -> None:
    summary = run_t58_baseline()
    assert summary.get("overall_passed"), json.dumps(summary, ensure_ascii=False, indent=2, default=str)


def main() -> int:
    summary = run_t58_baseline()
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary.get("overall_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
