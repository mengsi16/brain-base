# -*- coding: utf-8 -*-
"""T50 单元测试：source_priority 计算 + frontmatter 写入 + write_raw_one 集成。

覆盖（契约 §7 验收点 4-5）：

1. ``_compute_source_priority`` 6 分级路径（P0-P3 全 cover）
2. ``_build_raw_frontmatter`` 含 / 不含 source_priority 双路径（向后兼容验证）
3. ``write_raw_one`` 端到端：candidate (type, fetched_at) → raw frontmatter
   含 ``source_priority`` 字段
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from brain_base.nodes.qa_persist import (
    _build_raw_frontmatter,
    _compute_source_priority,
    write_raw_one,
)


# ===========================================================================
# 1. _compute_source_priority 6 分级路径
# ===========================================================================


def _date_str(days_ago: int) -> str:
    """生成 N 天前的 YYYY-MM-DD 字符串。"""
    return (date.today() - timedelta(days=days_ago)).isoformat()


def test_compute_priority_official_doc_fresh_to_p0():
    """official-doc + ≤90 天 → P0。"""
    assert _compute_source_priority("official-doc", _date_str(0)) == "P0"
    assert _compute_source_priority("official-doc", _date_str(30)) == "P0"
    assert _compute_source_priority("official-doc", _date_str(90)) == "P0"


def test_compute_priority_official_doc_old_to_p1():
    """official-doc + >90 天 → P1。"""
    assert _compute_source_priority("official-doc", _date_str(91)) == "P1"
    assert _compute_source_priority("official-doc", _date_str(365)) == "P1"


def test_compute_priority_user_upload_always_to_p1():
    """user-upload 任意时效 → P1（用户精选高信任，与 official-old 同级）。"""
    assert _compute_source_priority("user-upload", _date_str(0)) == "P1"
    assert _compute_source_priority("user-upload", _date_str(180)) == "P1"
    assert _compute_source_priority("user-upload", _date_str(1000)) == "P1"


def test_compute_priority_community_fresh_to_p2():
    """community + ≤90 天 → P2。"""
    assert _compute_source_priority("community", _date_str(0)) == "P2"
    assert _compute_source_priority("community", _date_str(30)) == "P2"
    assert _compute_source_priority("community", _date_str(90)) == "P2"


def test_compute_priority_community_old_to_p3():
    """community + >90 天 → P3。"""
    assert _compute_source_priority("community", _date_str(91)) == "P3"
    assert _compute_source_priority("community", _date_str(365)) == "P3"


def test_compute_priority_unknown_type_to_p3():
    """未知 / 兜底 type → P3。"""
    assert _compute_source_priority("blog", _date_str(0)) == "P3"
    assert _compute_source_priority("", _date_str(0)) == "P3"
    assert _compute_source_priority("unknown", _date_str(30)) == "P3"


def test_compute_priority_invalid_date_falls_back_to_p3():
    """空 / 非法 fetched_at_date → 按 999 天处理 → P3 兜底（除 user-upload 仍 P1）。"""
    # 缺日期：community 无法判时效 → 默认陈旧 → P3
    assert _compute_source_priority("community", "") == "P3"
    # 非法格式
    assert _compute_source_priority("community", "not-a-date") == "P3"
    # official-doc 缺日期 → 按陈旧处理 → P1
    assert _compute_source_priority("official-doc", "") == "P1"
    # user-upload 缺日期 → 仍 P1（与时效无关）
    assert _compute_source_priority("user-upload", "") == "P1"


# ===========================================================================
# 2. _build_raw_frontmatter 含 / 不含 source_priority 双路径
# ===========================================================================


def test_build_frontmatter_with_source_priority_writes_field():
    """传 source_priority="P0" → frontmatter 含 ``source_priority: P0`` 行。"""
    fm = _build_raw_frontmatter(
        doc_id="x",
        title="t",
        source_type="official-doc",
        source="x.io",
        url="https://x.io",
        fetched_at_date="2026-05-20",
        content_sha256="abc",
        keywords=[],
        source_priority="P0",
    )
    assert "source_priority: P0" in fm
    # 字段位置在 keywords 后、闭合 --- 前
    lines = fm.splitlines()
    keywords_idx = next(i for i, ln in enumerate(lines) if ln.startswith("keywords:"))
    sp_idx = next(i for i, ln in enumerate(lines) if ln.startswith("source_priority:"))
    closing_idx = next(i for i, ln in enumerate(lines[1:], start=1) if ln == "---")
    assert keywords_idx < sp_idx < closing_idx


def test_build_frontmatter_without_source_priority_omits_field():
    """不传 source_priority → frontmatter 不含 ``source_priority`` 行（向后兼容）。"""
    fm = _build_raw_frontmatter(
        doc_id="x",
        title="t",
        source_type="community",
        source="x.io",
        url="https://x.io",
        fetched_at_date="2026-05-20",
        content_sha256="abc",
        keywords=[],
    )
    assert "source_priority" not in fm


def test_build_frontmatter_empty_source_priority_omits_field():
    """传 source_priority="" → 等同未传 → frontmatter 不含字段。"""
    fm = _build_raw_frontmatter(
        doc_id="x",
        title="t",
        source_type="community",
        source="x.io",
        url="https://x.io",
        fetched_at_date="2026-05-20",
        content_sha256="abc",
        keywords=[],
        source_priority="",
    )
    assert "source_priority" not in fm


@pytest.mark.parametrize("priority", ["P0", "P1", "P2", "P3"])
def test_build_frontmatter_all_priority_values_written_correctly(priority: str):
    """4 档 P0-P3 全部能正确写入。"""
    fm = _build_raw_frontmatter(
        doc_id="x",
        title="t",
        source_type="official-doc",
        source="x.io",
        url="https://x.io",
        fetched_at_date="2026-05-20",
        content_sha256="abc",
        keywords=[],
        source_priority=priority,
    )
    assert f"source_priority: {priority}" in fm


# ===========================================================================
# 3. write_raw_one 端到端：candidate → raw frontmatter 含 source_priority
# ===========================================================================


def _make_candidate(**overrides: Any) -> dict:
    """最小 candidate dict。"""
    base = {
        "url": "https://docs.litellm.ai/docs/",
        "title": "LiteLLM Docs",
        "markdown": "# LiteLLM\n\n核心文档...\n",
        "content_sha256": "a" * 64,
        "type": "official-doc",
        "keywords": ["litellm"],
        "fetched_at": _date_str(0) + "T10:00:00+00:00",  # 当天 → P0
        "from_engines": [],
        "from_queries": [],
        "score": 90,
        "summary": "",
        "whether_in": True,
        "reason": "ok",
    }
    base.update(overrides)
    return base


def _patch_chunker(monkeypatch, tmp_path: Path) -> None:
    """mock chunker 不真切分，避免依赖 bin/chunker。"""
    monkeypatch.setattr(
        "brain_base.nodes.qa_persist.write_chunks",
        lambda raw_path, output_dir: [tmp_path / "chunks" / "fake.md"],
    )


def test_write_raw_one_official_doc_fresh_writes_p0(tmp_path, monkeypatch):
    """candidate type=official-doc + 当天 → raw frontmatter 含 source_priority: P0。"""
    _patch_chunker(monkeypatch, tmp_path)

    out = asyncio.run(
        write_raw_one({
            "candidate": _make_candidate(type="official-doc"),
            "raw_dir": str(tmp_path / "raw"),
            "chunk_dir": str(tmp_path / "chunks"),
        })
    )
    raw_path = Path(out["persist_results"][0]["raw_path"])
    raw_text = raw_path.read_text(encoding="utf-8")
    assert "source_priority: P0" in raw_text


def test_write_raw_one_community_old_writes_p3(tmp_path, monkeypatch):
    """candidate type=community + 1 年前 → raw frontmatter 含 source_priority: P3。"""
    _patch_chunker(monkeypatch, tmp_path)

    cand = _make_candidate(
        type="community",
        fetched_at=_date_str(365) + "T10:00:00+00:00",
    )

    out = asyncio.run(
        write_raw_one({
            "candidate": cand,
            "raw_dir": str(tmp_path / "raw"),
            "chunk_dir": str(tmp_path / "chunks"),
        })
    )
    raw_path = Path(out["persist_results"][0]["raw_path"])
    raw_text = raw_path.read_text(encoding="utf-8")
    assert "source_priority: P3" in raw_text


def test_write_raw_one_user_upload_writes_p1(tmp_path, monkeypatch):
    """candidate type=user-upload → raw frontmatter 含 source_priority: P1（不论时效）。"""
    _patch_chunker(monkeypatch, tmp_path)

    cand = _make_candidate(
        type="user-upload",
        fetched_at=_date_str(500) + "T10:00:00+00:00",  # 1 年多前 → 仍 P1
    )

    out = asyncio.run(
        write_raw_one({
            "candidate": cand,
            "raw_dir": str(tmp_path / "raw"),
            "chunk_dir": str(tmp_path / "chunks"),
        })
    )
    raw_path = Path(out["persist_results"][0]["raw_path"])
    raw_text = raw_path.read_text(encoding="utf-8")
    assert "source_priority: P1" in raw_text


def test_write_raw_one_unknown_type_writes_p3(tmp_path, monkeypatch):
    """candidate type 缺失 → 默认 community + 当天 → P2（验证默认 type 兜底正常）。"""
    _patch_chunker(monkeypatch, tmp_path)

    # type 留空 → write_raw_one 内部 fallback 到 "community"
    cand = _make_candidate(type="")

    out = asyncio.run(
        write_raw_one({
            "candidate": cand,
            "raw_dir": str(tmp_path / "raw"),
            "chunk_dir": str(tmp_path / "chunks"),
        })
    )
    raw_path = Path(out["persist_results"][0]["raw_path"])
    raw_text = raw_path.read_text(encoding="utf-8")
    # type="" → fallback "community"，当天 → P2
    assert "source_priority: P2" in raw_text


def test_write_raw_one_frontmatter_has_priority_after_keywords(tmp_path, monkeypatch):
    """frontmatter 字段顺序：keywords 之后才有 source_priority。"""
    _patch_chunker(monkeypatch, tmp_path)

    out = asyncio.run(
        write_raw_one({
            "candidate": _make_candidate(),
            "raw_dir": str(tmp_path / "raw"),
            "chunk_dir": str(tmp_path / "chunks"),
        })
    )
    raw_text = Path(out["persist_results"][0]["raw_path"]).read_text(encoding="utf-8")
    keywords_pos = raw_text.find("keywords:")
    priority_pos = raw_text.find("source_priority:")
    assert keywords_pos != -1 and priority_pos != -1
    assert keywords_pos < priority_pos
