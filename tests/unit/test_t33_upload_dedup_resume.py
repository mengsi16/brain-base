"""T33 测试：upload 路径 dedup short-circuit + batch resume + replace_docs。

覆盖：
- C1 convert_node dedup hit → short-circuit 不调 convert_one
- C2 convert_node dedup miss → 正常调 convert_one + 透传 binary sha256
- C3 _convert_pdf_in_batches resume → 复用已有合法 batch 产物
- C4 _convert_pdf_in_batches 损坏 batch → rmtree 重跑
- C5 ingest_node 传 replace_docs=True → milvus_cli 收到 True
"""
from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# C1/C2: convert_node dedup short-circuit
# ---------------------------------------------------------------------------


def test_convert_node_dedup_hit_skips_convert(monkeypatch, tmp_path):
    """C1：_lookup_by_frontmatter_sha256 命中 → convert_one 不被调用 + dedup_skipped 记录该文件。"""
    from brain_base.nodes import ingest_file

    pdf_path = tmp_path / "test.pdf"
    pdf_bytes = b"fake pdf content for dedup test"
    pdf_path.write_bytes(pdf_bytes)
    expected_sha = hashlib.sha256(pdf_bytes).hexdigest()

    # mock _lookup_by_frontmatter_sha256 返回命中
    monkeypatch.setattr(
        ingest_file, "_lookup_by_frontmatter_sha256",
        lambda sha256, raw_dir=None: {"doc_id": "existing-doc-001", "raw_path": "existing.md"},
    )

    convert_called: list = []

    def fake_convert(**kwargs):
        convert_called.append(kwargs)
        return {"doc_id": "should-not-happen", "raw_path": "x.md"}

    monkeypatch.setattr(ingest_file, "convert_one", fake_convert)

    result = ingest_file.convert_node({"input_files": [str(pdf_path)]})

    assert result["converted"] == [], "convert_one 不应被调用"
    assert convert_called == [], "convert_one 不应被调用（双重断言）"
    assert len(result["dedup_skipped"]) == 1
    assert result["dedup_skipped"][0]["existing_doc_id"] == "existing-doc-001"
    assert result["dedup_skipped"][0]["sha256"] == expected_sha
    assert result["dedup_skipped"][0]["input"] == str(pdf_path)


def test_convert_node_dedup_miss_calls_convert_and_passes_sha(monkeypatch, tmp_path):
    """C2：_lookup_by_frontmatter_sha256 miss → convert_one 被调用 + binary sha256 透传给 result。"""
    from brain_base.nodes import ingest_file

    pdf_path = tmp_path / "unique.pdf"
    pdf_bytes = b"unique pdf content"
    pdf_path.write_bytes(pdf_bytes)
    expected_sha = hashlib.sha256(pdf_bytes).hexdigest()

    # mock _lookup_by_frontmatter_sha256 返回 None（miss）
    monkeypatch.setattr(
        ingest_file, "_lookup_by_frontmatter_sha256",
        lambda sha256, raw_dir=None: None,
    )

    def fake_convert(*, input_path, output_dir, uploads_dir, upload_date):
        return {
            "doc_id": "new-doc-002",
            "raw_path": str(tmp_path / "new-doc-002.md"),
            "original_file": str(input_path),
        }

    monkeypatch.setattr(ingest_file, "convert_one", fake_convert)

    result = ingest_file.convert_node({"input_files": [str(pdf_path)]})

    assert len(result["converted"]) == 1
    assert result["dedup_skipped"] == []
    # 关键断言：binary sha256 透传给 frontmatter_node
    assert result["converted"][0]["content_sha256"] == expected_sha


# ---------------------------------------------------------------------------
# C6: _lookup_by_frontmatter_sha256 集成测试（真实文件，不 mock）
# ---------------------------------------------------------------------------


def test_lookup_by_frontmatter_sha256_hit(tmp_path):
    """C6a：raw 目录里有 content_sha256 匹配的 md → 返回 {doc_id, raw_path}。"""
    from brain_base.nodes.ingest_file import _lookup_by_frontmatter_sha256

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    target_sha = "abcd1234" * 8  # 64 hex chars
    (raw_dir / "existing-doc.md").write_text(
        f"---\ndoc_id: existing-doc\ncontent_sha256: {target_sha}\n---\n\n# Body\n",
        encoding="utf-8",
    )
    # 另一个不匹配的文件
    (raw_dir / "other-doc.md").write_text(
        "---\ndoc_id: other-doc\ncontent_sha256: 00001111222233334444555566667777\n---\n\n# Other\n",
        encoding="utf-8",
    )

    result = _lookup_by_frontmatter_sha256(target_sha, raw_dir=raw_dir)
    assert result is not None
    assert result["doc_id"] == "existing-doc"


def test_lookup_by_frontmatter_sha256_miss(tmp_path):
    """C6b：raw 目录里没有匹配的 content_sha256 → 返回 None。"""
    from brain_base.nodes.ingest_file import _lookup_by_frontmatter_sha256

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "some-doc.md").write_text(
        "---\ndoc_id: some-doc\ncontent_sha256: ffffffff\n---\n\n# Body\n",
        encoding="utf-8",
    )

    result = _lookup_by_frontmatter_sha256("00000000", raw_dir=raw_dir)
    assert result is None


def test_lookup_by_frontmatter_sha256_no_frontmatter(tmp_path):
    """C6c：raw 目录里有无 frontmatter 的 md → 跳过不崩溃。"""
    from brain_base.nodes.ingest_file import _lookup_by_frontmatter_sha256

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "bare.md").write_text("# No frontmatter here\n", encoding="utf-8")

    result = _lookup_by_frontmatter_sha256("anything", raw_dir=raw_dir)
    assert result is None


def test_lookup_by_frontmatter_sha256_empty_dir(tmp_path):
    """C6d：raw 目录为空 → 返回 None。"""
    from brain_base.nodes.ingest_file import _lookup_by_frontmatter_sha256

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    assert _lookup_by_frontmatter_sha256("anything", raw_dir=raw_dir) is None


def test_lookup_by_frontmatter_sha256_dir_not_exist(tmp_path):
    """C6e：raw 目录不存在 → 返回 None（不崩溃）。"""
    from brain_base.nodes.ingest_file import _lookup_by_frontmatter_sha256

    assert _lookup_by_frontmatter_sha256("anything", raw_dir=tmp_path / "nonexist") is None


# ---------------------------------------------------------------------------
# C3/C4: _convert_pdf_in_batches resume
# ---------------------------------------------------------------------------


def _make_fake_batch_dir(work_dir: Path, batch_idx: int, start: int, end: int, stem: str, body_size: int = 500) -> Path:
    """构造一个看起来合法的已完成 batch 目录（MinerU 输出结构）。"""
    batch_work = work_dir / f"_batch_{batch_idx:03d}_p{start}-{end}"
    # MinerU 输出结构: <batch_work>/<stem>/auto/<stem>.md
    mineru_out = batch_work / stem / "auto"
    mineru_out.mkdir(parents=True, exist_ok=True)
    md_file = mineru_out / f"{stem}.md"
    md_file.write_text("# fake content\n" + "x" * body_size, encoding="utf-8")
    return batch_work


def test_convert_pdf_in_batches_resumes_completed_batches(monkeypatch, tmp_path):
    """C3：所有 batch 都已有合法产物 → MinerU 不被调用，直接 merge 复用。"""
    doc_converter = importlib.import_module("bin.doc-converter")

    stem = "testpdf"
    pdf_path = tmp_path / f"{stem}.pdf"
    pdf_path.write_bytes(b"fake pdf")
    work_dir = tmp_path / "_mineru_work" / stem
    work_dir.mkdir(parents=True)

    # 预先放 3 个完整 batch（模拟之前跑过 30 页 / 每批 10 页）
    for i, (s, e) in enumerate([(1, 10), (11, 20), (21, 30)]):
        _make_fake_batch_dir(work_dir, i, s, e, stem)

    mineru_call_count = [0]

    def fake_run_mineru(*args, **kwargs):
        mineru_call_count[0] += 1

    monkeypatch.setattr(doc_converter, "_run_mineru_via_python_api", fake_run_mineru)

    body, md_path = doc_converter._convert_pdf_in_batches(
        input_path=pdf_path,
        work_dir=work_dir,
        page_count=30,
        batch_size=10,
    )

    assert mineru_call_count[0] == 0, "所有 batch 已完成应不调 MinerU"
    assert md_path.exists()
    assert "fake content" in body


def test_convert_pdf_in_batches_reruns_corrupted_batch(monkeypatch, tmp_path):
    """C4：batch 目录存在但产物损坏（size < 100B）→ rmtree 重跑该 batch。"""
    doc_converter = importlib.import_module("bin.doc-converter")

    stem = "testpdf2"
    pdf_path = tmp_path / f"{stem}.pdf"
    pdf_path.write_bytes(b"fake pdf 2")
    work_dir = tmp_path / "_mineru_work" / stem
    work_dir.mkdir(parents=True)

    # batch_000 合法
    _make_fake_batch_dir(work_dir, 0, 1, 10, stem, body_size=500)
    # batch_001 损坏（极小 .md 文件）
    _make_fake_batch_dir(work_dir, 1, 11, 20, stem, body_size=5)

    mineru_calls = []

    def fake_run_mineru(input_path, work, mineru_bin=None, page_range=None):
        mineru_calls.append(page_range)
        # 模拟 MinerU 写出产物
        out = work / stem / "auto"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{stem}.md").write_text("# rerun\n" + "y" * 500, encoding="utf-8")

    monkeypatch.setattr(doc_converter, "_run_mineru_via_python_api", fake_run_mineru)

    body, md_path = doc_converter._convert_pdf_in_batches(
        input_path=pdf_path,
        work_dir=work_dir,
        page_count=20,
        batch_size=10,
    )

    # 关键断言：只有 batch_001 重跑（不是 batch_000，不是全部）
    assert mineru_calls == ["11-20"], f"应只重跑 batch_001 (11-20)，实得：{mineru_calls}"
    assert md_path.exists()


# ---------------------------------------------------------------------------
# C5: ingest_node 传 replace_docs=True
# ---------------------------------------------------------------------------


def test_ingest_node_passes_replace_docs_true(monkeypatch):
    """C5：persist_node 调 milvus_ingest_chunks 必须传 replace_docs=True，
    否则 upload 同 doc 重跑会追加 milvus 重复行。"""
    from brain_base.nodes import persistence

    captured: dict = {}

    def fake_ingest(*, chunk_files, replace_docs=False):
        captured["chunk_files"] = chunk_files
        captured["replace_docs"] = replace_docs
        return {"inserted": len(chunk_files), "doc_ids": ["test-doc"]}

    monkeypatch.setattr(persistence, "milvus_ingest_chunks", fake_ingest)

    state = {
        "chunk_files": ["fake-chunk.md"],
        "enriched": True,
        "enriched_count": 1,
    }
    result = persistence.ingest_node(state)

    assert captured["replace_docs"] is True, "ingest_node 必须传 replace_docs=True"
    assert result["milvus_inserted"] == 1
