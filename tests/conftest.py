"""
Shared pytest fixtures for brain-base smoke tests (P0-2).

Design goals:
1. No external dependencies — tests must pass without Milvus, Playwright, network.
2. Isolated temp dirs — no test ever reads/writes real data/crystallized/ or data/docs/.
3. Fast — full suite completes in <5s on a cold cache.
4. CLI-level — invoke bin/*.py via subprocess, parse JSON from stdout, assert structure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
CRYSTALLIZE_CLI = BIN_DIR / "crystallize-cli.py"
MILVUS_CLI = BIN_DIR / "milvus-cli.py"
EVAL_RECALL_CLI = BIN_DIR / "eval-recall.py"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def crystallize_cli_path() -> Path:
    assert CRYSTALLIZE_CLI.exists(), f"crystallize-cli.py missing at {CRYSTALLIZE_CLI}"
    return CRYSTALLIZE_CLI


@pytest.fixture(scope="session")
def milvus_cli_path() -> Path:
    assert MILVUS_CLI.exists(), f"milvus-cli.py missing at {MILVUS_CLI}"
    return MILVUS_CLI


@pytest.fixture(scope="session")
def eval_recall_cli_path() -> Path:
    assert EVAL_RECALL_CLI.exists(), f"eval-recall.py missing at {EVAL_RECALL_CLI}"
    return EVAL_RECALL_CLI


# ---------------------------------------------------------------------------
# Subprocess runner with JSON stdout parsing
# ---------------------------------------------------------------------------


def _run_cli(cli_path: Path, args: list[str]) -> tuple[int, str, str]:
    """Invoke a CLI script and return (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(cli_path), *args]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return proc.returncode, proc.stdout, proc.stderr


@pytest.fixture
def run_crystallize(crystallize_cli_path: Path):
    def _runner(*args: str, crystal_dir: Path | None = None) -> dict[str, Any]:
        full_args: list[str] = []
        if crystal_dir is not None:
            full_args.extend(["--crystal-dir", str(crystal_dir)])
        full_args.extend(args)
        rc, out, err = _run_cli(crystallize_cli_path, full_args)
        assert rc == 0, f"crystallize-cli exited {rc}; stderr={err}; stdout={out}"
        assert out.strip(), f"crystallize-cli produced empty stdout; stderr={err}"
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            pytest.fail(f"crystallize-cli output is not valid JSON: {exc}\nstdout:\n{out}")

    return _runner


@pytest.fixture
def run_milvus(milvus_cli_path: Path):
    """Run milvus-cli and return parsed JSON.

    Raises on non-zero exit unless ``allow_fail=True`` is passed (for negative
    tests that assert error handling)."""

    def _runner(
        *args: str,
        allow_fail: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any] | str, str]:
        cmd = [sys.executable, str(milvus_cli_path), *args]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=REPO_ROOT,
            env=env,
        )
        if not allow_fail:
            assert proc.returncode == 0, (
                f"milvus-cli exited {proc.returncode}; "
                f"stderr={proc.stderr}; stdout={proc.stdout}"
            )
        # Try to parse JSON but tolerate non-JSON output for error cases
        try:
            payload: dict[str, Any] | str = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = proc.stdout
        return proc.returncode, payload, proc.stderr

    return _runner


@pytest.fixture
def run_eval_recall(eval_recall_cli_path: Path):
    """Run eval-recall and return parsed JSON."""

    def _runner(
        *args: str,
        allow_fail: bool = False,
    ) -> tuple[int, dict[str, Any] | str, str]:
        cmd = [sys.executable, str(eval_recall_cli_path), *args]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        if not allow_fail:
            assert proc.returncode == 0, (
                f"eval-recall exited {proc.returncode}; "
                f"stderr={proc.stderr}; stdout={proc.stdout}"
            )
        try:
            payload: dict[str, Any] | str = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = proc.stdout
        return proc.returncode, payload, proc.stderr

    return _runner


# ---------------------------------------------------------------------------
# Temp fixtures for crystallize layer
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_crystal_dir(tmp_path: Path) -> Path:
    """Empty crystallized directory — index.json does not exist yet."""
    d = tmp_path / "crystallized"
    d.mkdir()
    return d


@pytest.fixture
def seeded_crystal_dir(tmp_path: Path) -> Path:
    """Pre-populated crystallized dir with one hot and one cold entry."""
    d = tmp_path / "crystallized"
    d.mkdir()
    cold_dir = d / "cold"
    cold_dir.mkdir()

    index = {
        "version": "1.1.0",
        "updated_at": "2026-04-24T00:00:00+08:00",
        "skills": [
            {
                "skill_id": "smoke-hot-example",
                "description": "Hot-layer smoke test entry",
                "trigger_keywords": ["smoke", "hot"],
                "last_confirmed_at": "2026-04-24T00:00:00+08:00",
                "freshness_ttl_days": 90,
                "revision": 1,
                "user_feedback": "pending",
                "layer": "hot",
                "value_score": 0.75,
                "value_breakdown": {
                    "generality": 0.8,
                    "stability": 0.8,
                    "evidence_quality": 0.7,
                    "cost_benefit": 0.5,
                },
                "hit_count": 0,
                "last_hit_at": None,
                "promoted_from_cold_at": None,
            },
            {
                "skill_id": "smoke-cold-example",
                "description": "Cold-layer smoke test entry",
                "trigger_keywords": ["smoke", "cold"],
                "last_confirmed_at": "2026-04-24T00:00:00+08:00",
                "freshness_ttl_days": 90,
                "revision": 1,
                "user_feedback": "pending",
                "layer": "cold",
                "value_score": 0.45,
                "value_breakdown": {
                    "generality": 0.5,
                    "stability": 0.5,
                    "evidence_quality": 0.5,
                    "cost_benefit": 0.3,
                },
                "hit_count": 0,
                "last_hit_at": None,
                "promoted_from_cold_at": None,
            },
        ],
    }
    (d / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Minimal markdown fixtures so show-cold / show-doc queries don't report missing files
    _write_fixture_md(d / "smoke-hot-example.md", "smoke-hot-example")
    _write_fixture_md(cold_dir / "smoke-cold-example.md", "smoke-cold-example")
    return d


def _write_fixture_md(path: Path, skill_id: str) -> None:
    frontmatter = "\n".join(
        [
            "---",
            f"skill_id: {skill_id}",
            f"description: Fixture markdown for {skill_id}",
            'trigger_keywords: ["smoke"]',
            "created_at: 2026-04-24T00:00:00+08:00",
            "last_confirmed_at: 2026-04-24T00:00:00+08:00",
            "freshness_ttl_days: 90",
            "revision: 1",
            "user_feedback: pending",
            "---",
        ]
    )
    body = f"\n# 固化答案：{skill_id}\n\n这是一条测试用的 crystallized skill。\n\n## 执行路径\n\n1. fixture\n\n## 遇到的坑\n\n1. none\n"
    path.write_text(frontmatter + body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Temp fixtures for docs (raw + chunks) — for milvus-cli list-docs / stats
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_docs_dirs(tmp_path: Path) -> tuple[Path, Path]:
    raw = tmp_path / "raw"
    chunks = tmp_path / "chunks"
    raw.mkdir()
    chunks.mkdir()
    return raw, chunks


@pytest.fixture
def seeded_docs_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Two fake docs: one recent (tier-1 green), one old (tier-3 red).

    Each doc has one chunk file with full P1-4 frontmatter (fetched_at, source_type,
    evidence_date). milvus-cli list-docs / stats / stale-check should all work."""
    raw = tmp_path / "raw"
    chunks = tmp_path / "chunks"
    raw.mkdir()
    chunks.mkdir()

    # Doc A — recent, official-doc, tier-1
    doc_a_id = "smoke-docs-recent-2026-04-20"
    (raw / f"{doc_a_id}.md").write_text(
        "# Smoke Recent Doc\n\nRecent authoritative doc.\n", encoding="utf-8"
    )
    _write_chunk_md(
        chunks / f"{doc_a_id}-001.md",
        doc_id=doc_a_id,
        chunk_id=f"{doc_a_id}-001",
        title="Smoke Recent Doc",
        source_type="official-doc",
        fetched_at="2026-04-20",
        body="Recent authoritative content about smoke testing.",
    )

    # Doc B — old, community, tier-3
    doc_b_id = "smoke-docs-old-2025-01-15"
    (raw / f"{doc_b_id}.md").write_text(
        "# Smoke Old Doc\n\nOld community post.\n", encoding="utf-8"
    )
    _write_chunk_md(
        chunks / f"{doc_b_id}-001.md",
        doc_id=doc_b_id,
        chunk_id=f"{doc_b_id}-001",
        title="Smoke Old Doc",
        source_type="community",
        fetched_at="2025-01-15",
        body="Aged community content for stale-check exercise.",
    )

    return raw, chunks


def _write_chunk_md(
    path: Path,
    *,
    doc_id: str,
    chunk_id: str,
    title: str,
    source_type: str,
    fetched_at: str,
    body: str,
) -> None:
    frontmatter = "\n".join(
        [
            "---",
            f"doc_id: {doc_id}",
            f"chunk_id: {chunk_id}",
            f"title: {title}",
            f"source_type: {source_type}",
            f"fetched_at: {fetched_at}",
            'questions: ["What is this doc about?", "Why use it?"]',
            "summary: Minimal fixture for smoke testing.",
            "---",
        ]
    )
    path.write_text(frontmatter + "\n\n" + body + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# P2-1 fixtures: raw dirs with content-hash scenarios
# ---------------------------------------------------------------------------


def _write_raw_md(
    path: Path,
    *,
    doc_id: str,
    body: str,
    source_type: str = "official-doc",
    fetched_at: str = "2026-04-24",
    declared_sha256: str | None = None,
    skip_frontmatter: bool = False,
) -> None:
    """Write a raw Markdown fixture with optional frontmatter.

    When ``skip_frontmatter=True``, writes only the body (simulates a freshly
    converted doc where upload-ingest hasn't yet assembled frontmatter)."""
    if skip_frontmatter:
        path.write_text(body + "\n" if not body.endswith("\n") else body, encoding="utf-8")
        return
    lines = [
        "---",
        f"doc_id: {doc_id}",
        f"title: {doc_id}",
        f"source_type: {source_type}",
        f"fetched_at: {fetched_at}",
    ]
    if declared_sha256 is not None:
        lines.append(f"content_sha256: {declared_sha256}")
    lines.append("---")
    frontmatter = "\n".join(lines)
    content = frontmatter + "\n\n" + body
    if not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")


def _body_sha256(body: str) -> str:
    """Test helper mirroring milvus-cli's _compute_body_sha256.

    Must stay in lockstep with bin/milvus-cli.py:_compute_body_sha256."""
    import hashlib

    normalised = body.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


@pytest.fixture
def hash_helper():
    """Expose the body SHA-256 helper to tests."""
    return _body_sha256


# ---------------------------------------------------------------------------
# T27: mock LLM sentinel for QaGraph compile / topology tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_llm():
    """Sentinel LLM 实例：仅用于 QaGraph 编译/拓扑测试，不真跑节点。

    QaGraph(llm=mock_llm) 通过 __init__ 的 None 检查后，调用图本身不被触发，
    内部 invoke_structured / llm.invoke 也不会被调用——除非测试真跑 graph，
    那时这个 sentinel 会主动抛错，提示测试方应该用真实 mock 而不是 sentinel
    （详见 tests/unit/test_qa_decompose.py::_FakeLLM）。
    """

    class _SentinelLLM:
        def with_structured_output(self, schema, **kwargs):
            raise RuntimeError(
                "mock_llm sentinel.with_structured_output called——编译/拓扑测试不应触发 LLM 路径，"
                "或测试需要的是真实 _FakeLLM（见 tests/unit/test_qa_decompose.py / test_qa_prep.py）"
            )

        def invoke(self, messages):
            raise RuntimeError(
                "mock_llm sentinel.invoke called——见 with_structured_output 注释"
            )

    return _SentinelLLM()


@pytest.fixture
def raw_dir_for_hash(tmp_path: Path) -> Path:
    """Raw directory seeded with three scenarios for P2-1 hash testing:

      1. ``doc-with-correct-hash`` — frontmatter declares the matching SHA.
      2. ``doc-with-stale-hash`` — declared SHA is wrong (body edited after
         frontmatter was written).
      3. ``doc-missing-hash`` — frontmatter exists but no ``content_sha256``.
      4. ``doc-no-frontmatter`` — raw body only (simulates pre-ingest state).
      5. ``doc-duplicate-of-1`` — identical body to doc 1 but different doc_id
         (exercises find-duplicates grouping).
    """
    raw = tmp_path / "raw"
    raw.mkdir()

    body_a = "# Doc A\n\nThe quick brown fox jumps over the lazy dog."
    body_b = "# Doc B\n\nCompletely different content about Milvus."

    _write_raw_md(
        raw / "doc-with-correct-hash.md",
        doc_id="doc-with-correct-hash",
        body=body_a,
        declared_sha256=_body_sha256(body_a),
    )
    _write_raw_md(
        raw / "doc-with-stale-hash.md",
        doc_id="doc-with-stale-hash",
        body=body_b,
        declared_sha256="0" * 64,  # deliberately wrong
    )
    _write_raw_md(
        raw / "doc-missing-hash.md",
        doc_id="doc-missing-hash",
        body=body_b + "\n\nBut this version has an appendix.",
        declared_sha256=None,
    )
    _write_raw_md(
        raw / "doc-no-frontmatter.md",
        doc_id="doc-no-frontmatter",
        body="# Pre-ingest Doc\n\nJust the body, no frontmatter yet.",
        skip_frontmatter=True,
    )
    # Duplicate of doc-with-correct-hash (same body, different doc_id)
    _write_raw_md(
        raw / "doc-duplicate-of-1.md",
        doc_id="doc-duplicate-of-1",
        body=body_a,
        declared_sha256=_body_sha256(body_a),
    )
    return raw
