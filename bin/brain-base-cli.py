#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT_DIR / "bin"
CONVERSATIONS_DIR = ROOT_DIR / "data" / "conversations"
DEFAULT_CLAUDE_BIN = os.environ.get("BRAIN_BASE_CLAUDE_BIN", "claude").strip() or "claude"

# HuggingFace Hub offline mode strategy:
# - If model is already cached locally → force offline (avoids spurious API calls)
# - If model is NOT cached → allow download (with mirror auto-switch in milvus_config)
# - User can force offline via HF_HUB_OFFLINE=1 env var
if not os.environ.get("HF_HUB_OFFLINE"):
    _bge_m3_cache = Path(os.environ.get(
        "HF_CACHE_DIR",
        Path.home() / ".cache" / "huggingface" / "hub",
    )) / "models--BAAI--bge-m3"
    if _bge_m3_cache.is_dir():
        os.environ["HF_HUB_OFFLINE"] = "1"
    # else: allow download, milvus_config._ensure_hf_endpoint() will handle mirror


def _print_json(payload: dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


_UUID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # uuid.DNS


def _ensure_uuid(value: str | None) -> str:
    """Return a valid UUID string.  If *value* is already a UUID, return it
    unchanged; otherwise derive a deterministic UUID5 so the caller's
    intent is preserved but ``claude --session-id`` never rejects it."""
    if not value:
        return str(uuid.uuid4())
    try:
        uuid.UUID(value)
        return value
    except ValueError:
        return str(uuid.uuid5(_UUID_NAMESPACE, value))


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _conversation_path(session_id: str) -> Path:
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    return CONVERSATIONS_DIR / f"{session_id}.jsonl"


def _append_conversation_event(session_id: str, event: dict[str, Any]) -> Path:
    """Append a JSONL event to ``data/conversations/<session_id>.jsonl``.

    The file is treated as an append-only log.  Each line records one CLI
    interaction (ask / resume / feedback) with timestamp + prompt + result
    summary so callers can replay the conversation by reading the file.
    """
    path = _conversation_path(session_id)
    line = json.dumps(event, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return path


def _summarize_result_text(text: str | None, limit: int = 280) -> str:
    if not text:
        return ""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", (value or "").strip().lower())
    slug = slug.strip("-")
    return slug or "upload"


def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_milvus_cli_module():
    return _load_module("brain_base_milvus_cli", BIN_DIR / "milvus-cli.py")


def _resolve_claude_bin(explicit: str | None = None) -> str:
    candidate = (explicit or DEFAULT_CLAUDE_BIN).strip()
    path = Path(candidate)
    if path.is_file():
        return str(path)
    resolved = shutil.which(candidate)
    if resolved:
        return resolved
    raise FileNotFoundError(f"未找到 claude 可执行文件: {candidate}")


def _probe_claude_bin(explicit: str | None = None) -> str:
    candidate = (explicit or DEFAULT_CLAUDE_BIN).strip()
    path = Path(candidate)
    if path.is_file():
        return str(path)
    resolved = shutil.which(candidate)
    return resolved or ""


def _run_process(argv: list[str], cwd: Path | None = None) -> dict[str, Any]:
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd or ROOT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stdout, stderr = proc.communicate()
    return {
        "command": argv,
        "cwd": str(cwd or ROOT_DIR),
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "ok": proc.returncode == 0,
    }


def _run_claude_agent_stream(
    argv: list[str],
    cwd: Path,
) -> dict[str, Any]:
    """Run claude -p with --output-format stream-json.

    Each JSON line from stdout is forwarded to stderr so the caller can
    observe intermediate progress in real time.  The final ``result``
    text is extracted from the stream and returned as ``stdout`` in the
    standard result dict.
    """
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    result_text_parts: list[str] = []
    all_lines: list[str] = []
    while True:
        line = proc.stdout.readline()
        if not line and proc.poll() is not None:
            break
        if not line:
            continue
        line = line.rstrip("\n")
        all_lines.append(line)
        # Forward every stream-json line to stderr for real-time visibility
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
        # Try to extract result text from stream-json lines
        try:
            obj = json.loads(line)
            if obj.get("type") == "result":
                result_text_parts.append(obj.get("result", ""))
        except (json.JSONDecodeError, KeyError):
            pass
    remaining_stderr = proc.stderr.read()
    return {
        "command": argv,
        "cwd": str(cwd),
        "exit_code": proc.returncode,
        "stdout": "".join(result_text_parts) if result_text_parts else "\n".join(all_lines),
        "stderr": remaining_stderr,
        "ok": proc.returncode == 0,
    }


def _run_claude_agent(
    *,
    agent: str,
    prompt: str,
    session_id: str | None,
    resume_session_id: str | None,
    plugin_dir: Path,
    claude_bin: str,
    output_format: str = "stream-json",
    model: str | None = None,
) -> dict[str, Any]:
    argv = [
        claude_bin,
        "-p",
        "--output-format",
        output_format,
        "--plugin-dir",
        str(plugin_dir),
        "--agent",
        agent,
        "--dangerously-skip-permissions",
    ]
    if model:
        argv.extend(["--model", model])
    if session_id:
        argv.extend(["--session-id", session_id])
    if resume_session_id:
        argv.extend(["--resume", resume_session_id])
    if output_format == "stream-json":
        argv.append("--verbose")
    argv.append(prompt)
    if output_format == "stream-json":
        return _run_claude_agent_stream(argv, cwd=plugin_dir)
    return _run_process(argv, cwd=plugin_dir)


def _parse_raw_frontmatter(raw_file: Path) -> dict[str, str]:
    text = raw_file.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    metadata: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    return metadata


def _build_ask_prompt(raw_prompt: str, no_supplement: bool) -> str:
    prompt = raw_prompt.strip()
    if not no_supplement:
        return prompt
    return (
        f"{prompt}\n\n"
        "## 额外约束\n"
        "仅检索本地已有资料，不需要联网补库。如果证据不足，请明确说明本地证据不足。"
    )


def _build_ingest_url_prompt(urls: list[str], topic: str, latest: bool) -> str:
    lines = [
        "## 任务",
        "把以下 URL 补充进 brain-base 知识库，不需要输出最终问答，只返回入库摘要。",
        "",
        "## URL 列表",
    ]
    lines.extend(f"- {url}" for url in urls)
    if topic:
        lines.extend(["", "## 主题", topic])
    if latest:
        lines.extend(["", "## 时效要求", "优先抓取最新版本内容。"])
    lines.extend(
        [
            "",
            "## 硬约束",
            "一个 URL = 一个 raw 文档。禁止将多个 URL 的内容合并为一篇文档，即使主题相同也必须逐 URL 独立落盘到 data/docs/raw/。每个 raw 的 frontmatter 中 url 字段只写该文档对应的单个 URL。",
            "",
            "## 返回要求",
            "返回新增文档的 doc_id、raw/chunks 路径、chunk_rows、question_rows、关键证据摘要、失败阶段。",
        ]
    )
    return "\n".join(lines)


def _build_ingest_file_prompt(paths: list[str], section_path: str) -> str:
    lines = [
        "## 任务",
        "把以下本地文档入库到 brain-base。",
        "",
        "## 文件路径",
    ]
    lines.extend(f"- {path}" for path in paths)
    if section_path:
        lines.extend(["", "## 可选元信息", f"- section_path: {section_path}"])
    return "\n".join(lines)


def _build_enrich_chunks_prompt(doc_ids: list[str]) -> str:
    lines = [
        "## 任务",
        "对以下 doc_id 的 chunk 文件执行 enrichment 补填（title/summary/keywords/questions），然后重新入库。",
        "",
        "## doc_id 列表",
    ]
    lines.extend(f"- {d}" for d in doc_ids)
    lines.extend(
        [
            "",
            "## 执行要求",
            "1. 扫描 chunk 文件，检测哪些缺少 enrichment 字段。",
            "2. 对缺失的 chunk 逐个生成 enrichment，写回 frontmatter。",
            "3. 删除 Milvus 旧行，重新 ingest。",
            "4. 返回 JSON 摘要（doc_id / chunks_scanned / chunks_enriched / milvus_rows_deleted / milvus_rows_inserted / failed_chunks）。",
        ]
    )
    return "\n".join(lines)


def _build_remove_doc_prompt(
    doc_ids: list[str],
    urls: list[str],
    sha256: str,
    confirm: bool,
    force_recent: bool,
    reason: str,
) -> str:
    lines = [
        "## 任务",
        "remove_doc",
        "",
        "## 目标",
    ]
    if doc_ids:
        lines.append("doc_ids:")
        lines.extend(f"  - {d}" for d in doc_ids)
    if urls:
        lines.append("urls:")
        lines.extend(f"  - {u}" for u in urls)
    if sha256:
        lines.append(f"sha256: {sha256}")
    lines.extend(
        [
            "",
            "## 选项",
            f"confirm: {'true' if confirm else 'false'}",
            f"force_recent: {'true' if force_recent else 'false'}",
            f"reason: {reason or '(未提供)'}",
            "",
            "## 返回要求",
            "严格按 lifecycle-workflow 步骤 9 的 JSON 结构返回。",
            "如果 confirm=false，只输出 dry-run 清单，不动任何存储。",
        ]
    )
    return "\n".join(lines)


def _build_feedback_prompt(status: str, note: str) -> str:
    if status == "confirmed":
        base = "用户未否定，确认固化上一轮答案"
    elif status == "rejected":
        base = "用户明确否定上一轮答案，拒绝固化"
    else:
        base = f"用户补充：{note or '有补充信息'}，更新固化答案"
    if not note:
        return base
    return f"{base}\n\n补充说明：{note}"


def cmd_health(args: argparse.Namespace) -> int:
    claude_bin = _probe_claude_bin(args.claude_bin)
    claude_version = (
        _run_process([claude_bin, "-v"], cwd=ROOT_DIR)
        if claude_bin
        else {"ok": False, "stdout": "", "stderr": "未找到 claude 可执行文件", "exit_code": 127}
    )
    milvus_argv = [sys.executable, str(BIN_DIR / "milvus-cli.py"), "check-runtime"]
    if args.require_local_model:
        milvus_argv.append("--require-local-model")
    if args.smoke_test:
        milvus_argv.append("--smoke-test")
    milvus_runtime_proc = _run_process(milvus_argv, cwd=ROOT_DIR)
    milvus_runtime = (
        json.loads(milvus_runtime_proc["stdout"])
        if milvus_runtime_proc["ok"] and (milvus_runtime_proc["stdout"] or "").strip()
        else {
            "ok": False,
            "exit_code": milvus_runtime_proc["exit_code"],
            "stdout": milvus_runtime_proc["stdout"],
            "stderr": milvus_runtime_proc["stderr"],
        }
    )
    doc_runtime = _run_process(
        [sys.executable, str(BIN_DIR / "doc-converter.py"), "check-runtime"],
        cwd=ROOT_DIR,
    )
    doc_runtime_json = (
        json.loads(doc_runtime["stdout"])
        if (doc_runtime["stdout"] or "").strip()
        else {}
    )
    payload = {
        "plugin_dir": str(ROOT_DIR),
        "claude": {
            "available": claude_version["ok"],
            "bin": claude_bin or (args.claude_bin or DEFAULT_CLAUDE_BIN),
            "version": claude_version["stdout"].strip(),
            "stderr": claude_version["stderr"].strip(),
        },
        "milvus": milvus_runtime,
        "doc_converter": {
            **doc_runtime_json,
            "exit_code": doc_runtime["exit_code"],
            "ok": doc_runtime["ok"],
            "stderr": doc_runtime["stderr"],
        },
    }
    return _print_json(payload)


def cmd_search(args: argparse.Namespace) -> int:
    milvus = _load_milvus_cli_module()
    result = milvus.multi_query_search(
        queries=args.query,
        top_k_per_query=args.top_k_per_query,
        final_k=args.final_k,
        rrf_k=args.rrf_k,
        use_rerank=not args.no_rerank,
    )
    return _print_json(result)


def cmd_exists(args: argparse.Namespace) -> int:
    milvus = _load_milvus_cli_module()
    if args.doc_id:
        result = milvus.show_doc(args.doc_id)
        payload = {
            "mode": "doc_id",
            "exists": bool(result.get("raw_exists") or result.get("chunks_count")),
            "doc": result,
        }
        return _print_json(payload)
    if args.sha256:
        result = milvus.hash_lookup(args.sha256)
        payload = {"mode": "sha256", **result}
        return _print_json(payload)
    target_url = args.url.strip()
    matches: list[dict[str, Any]] = []
    raw_dir = Path(args.raw_dir)
    if raw_dir.exists():
        for raw_file in sorted(raw_dir.glob("*.md")):
            meta = _parse_raw_frontmatter(raw_file)
            if meta.get("url", "").strip() != target_url:
                continue
            doc_id = raw_file.stem
            doc = milvus.show_doc(doc_id)
            matches.append(doc)
    payload = {
        "mode": "url",
        "url": target_url,
        "exists": bool(matches),
        "count": len(matches),
        "matches": matches,
    }
    return _print_json(payload)


def cmd_ask(args: argparse.Namespace) -> int:
    claude_bin = _resolve_claude_bin(args.claude_bin)
    session_id = _ensure_uuid(args.session_id)
    prompt = _build_ask_prompt(args.prompt, args.no_supplement)
    result = _run_claude_agent(
        agent="brain-base:qa-agent",
        prompt=prompt,
        session_id=session_id,
        resume_session_id=None,
        plugin_dir=ROOT_DIR,
        claude_bin=claude_bin,
        output_format=args.output_format,
        model=getattr(args, 'model', None),
    )
    conv_path = _append_conversation_event(
        session_id,
        {
            "ts": _now_iso(),
            "event": "ask",
            "session_id": session_id,
            "prompt": args.prompt,
            "no_supplement": bool(args.no_supplement),
            "ok": result["ok"],
            "exit_code": result["exit_code"],
            "answer_summary": _summarize_result_text(result.get("stdout")),
        },
    )
    payload = {
        "command": "ask",
        "session_id": session_id,
        "feedback_recommended": result["ok"],
        "conversation_log": str(conv_path),
        "result": result,
    }
    return _print_json(payload, 0 if result["ok"] else result["exit_code"] or 1)


def cmd_resume(args: argparse.Namespace) -> int:
    claude_bin = _resolve_claude_bin(args.claude_bin)
    resume_id = _ensure_uuid(args.session_id)
    prompt = _build_ask_prompt(args.prompt, args.no_supplement)
    result = _run_claude_agent(
        agent="brain-base:qa-agent",
        prompt=prompt,
        session_id=None,
        resume_session_id=resume_id,
        plugin_dir=ROOT_DIR,
        claude_bin=claude_bin,
        output_format=args.output_format,
        model=getattr(args, 'model', None),
    )
    conv_path = _append_conversation_event(
        resume_id,
        {
            "ts": _now_iso(),
            "event": "resume",
            "session_id": resume_id,
            "prompt": args.prompt,
            "no_supplement": bool(args.no_supplement),
            "ok": result["ok"],
            "exit_code": result["exit_code"],
            "answer_summary": _summarize_result_text(result.get("stdout")),
        },
    )
    payload = {
        "command": "resume",
        "session_id": resume_id,
        "feedback_recommended": result["ok"],
        "conversation_log": str(conv_path),
        "result": result,
    }
    return _print_json(payload, 0 if result["ok"] else result["exit_code"] or 1)


def _read_conversation_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"raw": line, "parse_error": True})
    return events


def cmd_history(args: argparse.Namespace) -> int:
    """List recorded conversations or replay a single session."""
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    if args.session_id:
        sid = _ensure_uuid(args.session_id)
        path = _conversation_path(sid)
        events = _read_conversation_events(path)
        payload = {
            "mode": "session",
            "session_id": sid,
            "log_path": str(path),
            "exists": path.exists(),
            "event_count": len(events),
            "events": events,
        }
        return _print_json(payload)

    files = sorted(
        CONVERSATIONS_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    sessions: list[dict[str, Any]] = []
    for path in files[: args.limit]:
        events = _read_conversation_events(path)
        first = events[0] if events else {}
        last = events[-1] if events else {}
        sessions.append(
            {
                "session_id": path.stem,
                "log_path": str(path),
                "event_count": len(events),
                "first_ts": first.get("ts", ""),
                "last_ts": last.get("ts", ""),
                "first_prompt": first.get("prompt", ""),
                "last_event": last.get("event", ""),
                "last_answer_summary": last.get("answer_summary", ""),
            }
        )
    payload = {
        "mode": "list",
        "conversations_dir": str(CONVERSATIONS_DIR),
        "limit": args.limit,
        "total": len(files),
        "sessions": sessions,
    }
    return _print_json(payload)


def cmd_ingest_url(args: argparse.Namespace) -> int:
    claude_bin = _resolve_claude_bin(args.claude_bin)
    session_id = _ensure_uuid(args.session_id)
    prompt = _build_ingest_url_prompt(args.url, args.topic, args.latest)
    result = _run_claude_agent(
        agent="brain-base:get-info-agent",
        prompt=prompt,
        session_id=session_id,
        resume_session_id=None,
        plugin_dir=ROOT_DIR,
        claude_bin=claude_bin,
        output_format=args.output_format,
        model=getattr(args, 'model', None),
    )
    payload = {
        "command": "ingest-url",
        "session_id": session_id,
        "result": result,
    }
    return _print_json(payload, 0 if result["ok"] else result["exit_code"] or 1)


def _build_clean_url_prompt(url: str, source_type: str, topic: str) -> str:
    lines = [
        "## 任务",
        "抓取以下 URL 的页面内容，清洗为 Markdown，写入 data/docs/raw/，分块入库。",
        "",
        "## 输入",
        f"url: {url}",
        f"source_type: {source_type}",
        f"topic: {topic}",
        "",
        "## 硬约束",
        "一个 URL = 一个 raw 文档。禁止引入其他 URL 的内容。",
        "official-doc 原始页面的每个章节都必须存在，不得删除、合并或概括。翻译允许但不得遗漏章节。",
        "",
        "## 返回要求",
        "返回 JSON 摘要：url / doc_id / source_type / raw_path / chunk_paths / chunk_rows / question_rows / content_sha256 / extraction_status / errors。",
    ]
    return "\n".join(lines)


def cmd_clean_url(args: argparse.Namespace) -> int:
    claude_bin = _resolve_claude_bin(args.claude_bin)
    session_id = _ensure_uuid(args.session_id)
    prompt = _build_clean_url_prompt(args.url, args.source_type, args.topic)
    result = _run_claude_agent(
        agent="brain-base:content-cleaner-agent",
        prompt=prompt,
        session_id=session_id,
        resume_session_id=None,
        plugin_dir=ROOT_DIR,
        claude_bin=claude_bin,
        output_format=args.output_format,
        model=getattr(args, "model", None),
    )
    payload = {
        "command": "clean-url",
        "session_id": session_id,
        "result": result,
    }
    return _print_json(payload, 0 if result["ok"] else result["exit_code"] or 1)


def cmd_ingest_file(args: argparse.Namespace) -> int:
    claude_bin = _resolve_claude_bin(args.claude_bin)
    session_id = _ensure_uuid(args.session_id)
    prompt = _build_ingest_file_prompt(args.path, args.section_path)
    result = _run_claude_agent(
        agent="brain-base:upload-agent",
        prompt=prompt,
        session_id=session_id,
        resume_session_id=None,
        plugin_dir=ROOT_DIR,
        claude_bin=claude_bin,
        output_format=args.output_format,
        model=getattr(args, 'model', None),
    )
    payload = {
        "command": "ingest-file",
        "session_id": session_id,
        "result": result,
    }
    return _print_json(payload, 0 if result["ok"] else result["exit_code"] or 1)


def cmd_ingest_text(args: argparse.Namespace) -> int:
    content = args.content
    if args.content_file:
        content = Path(args.content_file).read_text(encoding="utf-8")
    if not content.strip():
        return _print_json({"command": "ingest-text", "ok": False, "error": "content 不能为空"}, 2)
    temp_dir = Path(tempfile.mkdtemp(prefix="brain-base-cli-"))
    temp_file = temp_dir / f"{_slugify(args.title or 'upload')}.md"
    temp_file.write_text(content, encoding="utf-8")
    fake_args = argparse.Namespace(
        path=[str(temp_file)],
        section_path=args.section_path,
        session_id=args.session_id,
        claude_bin=args.claude_bin,
        output_format=args.output_format,
    )
    exit_code = cmd_ingest_file(fake_args)
    if args.keep_temp:
        return exit_code
    if temp_file.exists():
        temp_file.unlink()
    if temp_dir.exists():
        temp_dir.rmdir()
    return exit_code


def cmd_feedback(args: argparse.Namespace) -> int:
    claude_bin = _resolve_claude_bin(args.claude_bin)
    resume_id = _ensure_uuid(args.session_id)
    prompt = _build_feedback_prompt(args.status, args.note)
    result = _run_claude_agent(
        agent="brain-base:qa-agent",
        prompt=prompt,
        session_id=None,
        resume_session_id=resume_id,
        plugin_dir=ROOT_DIR,
        claude_bin=claude_bin,
        output_format=args.output_format,
        model=getattr(args, 'model', None),
    )
    conv_path = _append_conversation_event(
        resume_id,
        {
            "ts": _now_iso(),
            "event": "feedback",
            "session_id": resume_id,
            "status": args.status,
            "note": args.note,
            "ok": result["ok"],
            "exit_code": result["exit_code"],
            "answer_summary": _summarize_result_text(result.get("stdout")),
        },
    )
    payload = {
        "command": "feedback",
        "session_id": resume_id,
        "status": args.status,
        "conversation_log": str(conv_path),
        "result": result,
    }
    return _print_json(payload, 0 if result["ok"] else result["exit_code"] or 1)


def cmd_enrich_chunks(args: argparse.Namespace) -> int:
    claude_bin = _resolve_claude_bin(args.claude_bin)
    session_id = _ensure_uuid(args.session_id)
    prompt = _build_enrich_chunks_prompt(args.doc_id)
    result = _run_claude_agent(
        agent="brain-base:chunk-enrichment-agent",
        prompt=prompt,
        session_id=session_id,
        resume_session_id=None,
        plugin_dir=ROOT_DIR,
        claude_bin=claude_bin,
        output_format=args.output_format,
        model=getattr(args, 'model', None),
    )
    payload = {
        "command": "enrich-chunks",
        "session_id": session_id,
        "result": result,
    }
    return _print_json(payload, 0 if result["ok"] else result["exit_code"] or 1)


def cmd_remove_doc(args: argparse.Namespace) -> int:
    if not (args.doc_id or args.url or args.sha256):
        return _print_json(
            {
                "command": "remove-doc",
                "ok": False,
                "error": "必须提供 --doc-id / --url / --sha256 之一",
            },
            2,
        )
    claude_bin = _resolve_claude_bin(args.claude_bin)
    session_id = _ensure_uuid(args.session_id)
    prompt = _build_remove_doc_prompt(
        doc_ids=args.doc_id or [],
        urls=args.url or [],
        sha256=args.sha256 or "",
        confirm=bool(args.confirm),
        force_recent=bool(args.force_recent),
        reason=args.reason or "",
    )
    result = _run_claude_agent(
        agent="brain-base:lifecycle-agent",
        prompt=prompt,
        session_id=session_id,
        resume_session_id=None,
        plugin_dir=ROOT_DIR,
        claude_bin=claude_bin,
        output_format=args.output_format,
        model=getattr(args, 'model', None),
    )
    payload = {
        "command": "remove-doc",
        "session_id": session_id,
        "confirm": bool(args.confirm),
        "result": result,
    }
    return _print_json(payload, 0 if result["ok"] else result["exit_code"] or 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brain-base-cli", description="brain-base 外部 Agent 调用 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_health = sub.add_parser("health", help="检查 claude / milvus / doc-converter 可用性")
    p_health.add_argument("--claude-bin", default=None)
    p_health.add_argument("--require-local-model", action="store_true")
    p_health.add_argument("--smoke-test", action="store_true")
    p_health.set_defaults(func=cmd_health)

    p_search = sub.add_parser("search", help="执行多查询检索并返回 JSON")
    p_search.add_argument("--query", action="append", required=True)
    p_search.add_argument("--top-k-per-query", type=int, default=20)
    p_search.add_argument("--final-k", type=int, default=10)
    p_search.add_argument("--rrf-k", type=int, default=60)
    p_search.add_argument("--no-rerank", action="store_true")
    p_search.set_defaults(func=cmd_search)

    p_exists = sub.add_parser("exists", help="检查 doc_id / url / sha256 是否已存在")
    exists_group = p_exists.add_mutually_exclusive_group(required=True)
    exists_group.add_argument("--doc-id")
    exists_group.add_argument("--url")
    exists_group.add_argument("--sha256")
    p_exists.add_argument("--raw-dir", default=str(ROOT_DIR / "data/docs/raw"))
    p_exists.set_defaults(func=cmd_exists)

    p_ask = sub.add_parser("ask", help="调用 qa-agent 完整问答链路")
    p_ask.add_argument("prompt")
    p_ask.add_argument("--session-id", default=None)
    p_ask.add_argument("--claude-bin", default=None)
    p_ask.add_argument("--model", default=None, help="覆盖 claude-code 默认模型（如 sonnet / haiku）")
    p_ask.add_argument("--no-supplement", action="store_true")
    p_ask.add_argument("--output-format", default="stream-json", choices=["stream-json", "text", "json"])
    p_ask.set_defaults(func=cmd_ask)

    p_resume = sub.add_parser(
        "resume",
        help="基于已有 session_id 继续多轮对话（底层走 claude --resume，由 qa-agent 复用上下文）",
    )
    p_resume.add_argument("--session-id", required=True)
    p_resume.add_argument("prompt")
    p_resume.add_argument("--claude-bin", default=None)
    p_resume.add_argument("--model", default=None, help="覆盖 claude-code 默认模型")
    p_resume.add_argument("--no-supplement", action="store_true")
    p_resume.add_argument("--output-format", default="stream-json", choices=["stream-json", "text", "json"])
    p_resume.set_defaults(func=cmd_resume)

    p_history = sub.add_parser(
        "history",
        help="列出会话历史；不传 --session-id 列出最近的会话摘要，传 --session-id 回放该 session 的所有事件",
    )
    p_history.add_argument("--session-id", default=None)
    p_history.add_argument("--limit", type=int, default=20)
    p_history.set_defaults(func=cmd_history)

    p_ingest_url = sub.add_parser("ingest-url", help="调用 get-info-agent 把 URL 补充入库")
    p_ingest_url.add_argument("--url", action="append", required=True)
    p_ingest_url.add_argument("--topic", default="")
    p_ingest_url.add_argument("--latest", action="store_true")
    p_ingest_url.add_argument("--session-id", default=None)
    p_ingest_url.add_argument("--claude-bin", default=None)
    p_ingest_url.add_argument("--model", default=None, help="覆盖 claude-code 默认模型")
    p_ingest_url.add_argument("--output-format", default="stream-json", choices=["stream-json", "text", "json"])
    p_ingest_url.set_defaults(func=cmd_ingest_url)

    p_clean_url = sub.add_parser("clean-url", help="调用 content-cleaner-agent 抓取单个 URL、清洗并入库")
    p_clean_url.add_argument("--url", required=True, help="要抓取的页面 URL（单个）")
    p_clean_url.add_argument("--source-type", default="official-doc", choices=["official-doc", "community"], help="URL 的来源类型")
    p_clean_url.add_argument("--topic", default="", help="主题关键词，用于 doc_id 命名")
    p_clean_url.add_argument("--session-id", default=None)
    p_clean_url.add_argument("--claude-bin", default=None)
    p_clean_url.add_argument("--model", default=None, help="覆盖 claude-code 默认模型")
    p_clean_url.add_argument("--output-format", default="stream-json", choices=["stream-json", "text", "json"])
    p_clean_url.set_defaults(func=cmd_clean_url)

    p_ingest_file = sub.add_parser("ingest-file", help="调用 upload-agent 导入本地文件")
    p_ingest_file.add_argument("--path", action="append", required=True)
    p_ingest_file.add_argument("--section-path", default="")
    p_ingest_file.add_argument("--session-id", default=None)
    p_ingest_file.add_argument("--claude-bin", default=None)
    p_ingest_file.add_argument("--model", default=None, help="覆盖 claude-code 默认模型")
    p_ingest_file.add_argument("--output-format", default="stream-json", choices=["stream-json", "text", "json"])
    p_ingest_file.set_defaults(func=cmd_ingest_file)

    p_ingest_text = sub.add_parser("ingest-text", help="把文本暂存为 Markdown 后走 upload-agent 入库")
    text_group = p_ingest_text.add_mutually_exclusive_group(required=True)
    text_group.add_argument("--content")
    text_group.add_argument("--content-file")
    p_ingest_text.add_argument("--title", default="upload")
    p_ingest_text.add_argument("--section-path", default="")
    p_ingest_text.add_argument("--session-id", default=None)
    p_ingest_text.add_argument("--claude-bin", default=None)
    p_ingest_text.add_argument("--keep-temp", action="store_true")
    p_ingest_text.add_argument("--output-format", default="stream-json", choices=["stream-json", "text", "json"])
    p_ingest_text.set_defaults(func=cmd_ingest_text)

    p_feedback = sub.add_parser("feedback", help="基于 session_id 给 qa-agent 发送固化反馈")
    p_feedback.add_argument("--session-id", required=True)
    p_feedback.add_argument("--status", choices=["confirmed", "rejected", "supplement"], required=True)
    p_feedback.add_argument("--note", default="")
    p_feedback.add_argument("--claude-bin", default=None)
    p_feedback.add_argument("--model", default=None, help="覆盖 claude-code 默认模型")
    p_feedback.add_argument("--output-format", default="stream-json", choices=["stream-json", "text", "json"])
    p_feedback.set_defaults(func=cmd_feedback)

    p_enrich = sub.add_parser("enrich-chunks", help="对已有 chunk 文件补填 enrichment（title/summary/keywords/questions）并重新入库")
    p_enrich.add_argument("--doc-id", action="append", required=True, help="要 enrichment 的 doc_id；可重复")
    p_enrich.add_argument("--session-id", default=None)
    p_enrich.add_argument("--claude-bin", default=None)
    p_enrich.add_argument("--model", default=None, help="覆盖 claude-code 默认模型")
    p_enrich.add_argument("--output-format", default="stream-json", choices=["stream-json", "text", "json"])
    p_enrich.set_defaults(func=cmd_enrich_chunks)

    p_remove_doc = sub.add_parser(
        "remove-doc",
        help="(危险) 通过 lifecycle-agent 编排删除文档（默认 dry-run，需 --confirm 才真删）",
    )
    p_remove_doc.add_argument("--doc-id", action="append", help="要删除的 doc_id；可重复")
    p_remove_doc.add_argument("--url", action="append", help="要删除的 raw 文档对应 url；可重复")
    p_remove_doc.add_argument("--sha256", help="按 body SHA-256 解析 doc_id")
    p_remove_doc.add_argument("--confirm", action="store_true", help="必须显式加上才会真删；默认 dry-run")
    p_remove_doc.add_argument("--force-recent", action="store_true", help="允许删除 10 分钟内创建的新文档")
    p_remove_doc.add_argument("--reason", default="", help="审计日志中记录的删除原因（建议必填）")
    p_remove_doc.add_argument("--session-id", default=None)
    p_remove_doc.add_argument("--claude-bin", default=None)
    p_remove_doc.add_argument("--model", default=None, help="覆盖 claude-code 默认模型")
    p_remove_doc.add_argument("--output-format", default="stream-json", choices=["stream-json", "text", "json"])
    p_remove_doc.set_defaults(func=cmd_remove_doc)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
