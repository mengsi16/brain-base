# -*- coding: utf-8 -*-
"""T50 单元测试：bin/chunker.py 透传 source_priority 到 chunk frontmatter。

覆盖（契约 §7 验收点 5）：

1. raw 含 ``source_priority: P0`` → 所有 chunk frontmatter 都含同字段
2. raw 不含 ``source_priority``（历史无字段文档）→ chunk 也不含，自动跳过
3. 多 chunk 输出一致性：所有 chunk 都继承同一个 source_priority 值
4. 4 档 P0-P3 全部能透传
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from pathlib import Path as _P

import pytest

# bin/ 在 sys.path 上才能 import chunker（同 nodes/qa_persist.py 模式）
_BIN_DIR = str(_P(__file__).resolve().parent.parent.parent / "bin")
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

chunker = importlib.import_module("bin.chunker")
write_chunks = chunker.write_chunks


# ===========================================================================
# fixture：构造 raw markdown 文件
# ===========================================================================


def _make_raw_file(
    tmp_path: Path,
    doc_id: str = "test-doc",
    source_priority: str | None = "P0",
    extra_fm: str = "",
    body: str = "# Title\n\n## Section\n\n" + ("正文" * 200),
) -> Path:
    """生成一份合法 raw markdown 到 tmp_path/raw/<doc_id>.md。

    Args:
        source_priority: None 不写字段；其他字符串写入 ``source_priority: <val>``。
    """
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{doc_id}.md"

    fm_lines = [
        "---",
        f"doc_id: {doc_id}",
        'title: "Test Doc"',
        "source_type: official-doc",
        "source: x.io",
        "url: https://x.io/docs",
        "fetched_at: 2026-05-20",
    ]
    if source_priority is not None:
        fm_lines.append(f"source_priority: {source_priority}")
    if extra_fm:
        fm_lines.append(extra_fm)
    fm_lines.append("---")

    raw_path.write_text("\n".join(fm_lines) + "\n\n" + body, encoding="utf-8")
    return raw_path


# ===========================================================================
# 1. raw 含 source_priority → chunk 透传
# ===========================================================================


def test_chunker_propagates_source_priority_to_chunks(tmp_path):
    """raw frontmatter 含 source_priority: P0 → chunk frontmatter 也有同字段。"""
    raw_path = _make_raw_file(tmp_path, source_priority="P0")
    chunk_dir = tmp_path / "chunks"

    written = write_chunks(raw_path=raw_path, output_dir=chunk_dir)

    assert len(written) >= 1, "应至少生成 1 个 chunk"
    for chunk_path in written:
        chunk_text = chunk_path.read_text(encoding="utf-8")
        assert "source_priority: P0" in chunk_text, (
            f"chunk {chunk_path.name} frontmatter 缺 source_priority: P0\n"
            f"实际内容前 500 字符:\n{chunk_text[:500]}"
        )


@pytest.mark.parametrize("priority", ["P0", "P1", "P2", "P3"])
def test_chunker_propagates_all_priority_tiers(tmp_path, priority: str):
    """4 档 P0-P3 全部能从 raw 透传到 chunk。"""
    raw_path = _make_raw_file(
        tmp_path,
        doc_id=f"doc-{priority.lower()}",
        source_priority=priority,
    )
    chunk_dir = tmp_path / "chunks"

    written = write_chunks(raw_path=raw_path, output_dir=chunk_dir)

    assert len(written) >= 1
    for chunk_path in written:
        text = chunk_path.read_text(encoding="utf-8")
        assert f"source_priority: {priority}" in text


# ===========================================================================
# 2. raw 无 source_priority → chunk 也无（历史文档自动跳过）
# ===========================================================================


def test_chunker_skips_when_raw_lacks_source_priority(tmp_path):
    """历史 raw 文档无 source_priority → chunk frontmatter 也不含此字段。"""
    raw_path = _make_raw_file(tmp_path, source_priority=None)  # 不写字段
    chunk_dir = tmp_path / "chunks"

    written = write_chunks(raw_path=raw_path, output_dir=chunk_dir)

    assert len(written) >= 1
    for chunk_path in written:
        text = chunk_path.read_text(encoding="utf-8")
        # 不含 source_priority key
        assert "source_priority" not in text, (
            f"raw 无字段时 chunk 不应有 source_priority\n"
            f"chunk 前 400 字符:\n{text[:400]}"
        )


def test_chunker_skips_when_raw_has_empty_source_priority(tmp_path):
    """raw 含 ``source_priority:`` 但值为空字符串 → chunk 也不写（``if val`` 守卫）。"""
    raw_path = _make_raw_file(tmp_path, source_priority="")  # 空字符串值
    chunk_dir = tmp_path / "chunks"

    written = write_chunks(raw_path=raw_path, output_dir=chunk_dir)

    assert len(written) >= 1
    for chunk_path in written:
        text = chunk_path.read_text(encoding="utf-8")
        assert "source_priority" not in text


# ===========================================================================
# 3. 多 chunk 一致性
# ===========================================================================


def test_chunker_all_chunks_have_same_priority(tmp_path):
    """长文档生成多个 chunk 时，所有 chunk 都继承同一 source_priority 值。"""
    # 大正文：3 万字符，至少切出 6 个 chunk（每 chunk ≤5000 字符）
    big_body = "# H1\n\n" + ("## H2\n\n" + "中文正文 " * 1000 + "\n\n") * 6
    raw_path = _make_raw_file(
        tmp_path,
        doc_id="multi-chunk-doc",
        source_priority="P2",
        body=big_body,
    )
    chunk_dir = tmp_path / "chunks"

    written = write_chunks(raw_path=raw_path, output_dir=chunk_dir)

    assert len(written) >= 2, f"应切出 ≥2 个 chunk（实际 {len(written)}）"
    for chunk_path in written:
        text = chunk_path.read_text(encoding="utf-8")
        assert "source_priority: P2" in text, (
            f"chunk {chunk_path.name} 缺 source_priority: P2"
        )


# ===========================================================================
# 4. 字段位置在 frontmatter 中正确（与其他继承字段同区域）
# ===========================================================================


def test_chunker_priority_field_in_frontmatter_block(tmp_path):
    """source_priority 应在 frontmatter 的 inherit_block 内（在 chunk_id 后、enrichment 字段前）。"""
    raw_path = _make_raw_file(tmp_path, source_priority="P1")
    chunk_dir = tmp_path / "chunks"

    written = write_chunks(raw_path=raw_path, output_dir=chunk_dir)
    text = written[0].read_text(encoding="utf-8")

    # source_priority 必须在 chunk_id 后
    chunk_id_pos = text.find("chunk_id:")
    priority_pos = text.find("source_priority:")
    summary_pos = text.find("summary:")

    assert chunk_id_pos != -1
    assert priority_pos != -1
    assert summary_pos != -1
    # 顺序：chunk_id < source_priority < summary（在 frontmatter 内 inherit_block 区域）
    assert chunk_id_pos < priority_pos < summary_pos
