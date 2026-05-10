# -*- coding: utf-8 -*-
"""T26.1-b 单元测试：write_raw_one + barrier_raw + fanout_persist_dispatcher。

覆盖：
- ``_url_to_slug`` 边界（含端口 / 无路径 / unicode / 空 URL）
- ``_build_raw_frontmatter`` 字段完整性 + json 转义
- ``write_raw_one`` 成功路径（mock chunker.write_chunks）+ 字段映射
- ``write_raw_one`` 失败隔离（empty url / empty markdown / chunker 抛错 / chunker 返回空列表）
- ``write_raw_one`` title 含 `:` / `"` 字符的 frontmatter 转义
- ``barrier_raw_node`` 聚合（success flatten + 失败归 persist_errors + 上游 errors 累加）
- ``fanout_persist_dispatcher`` gate（candidates 空 → "ingest"；非空 → list[Send]）
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from brain_base.nodes.qa_persist import (
    DEFAULT_CHUNK_DIR,
    DEFAULT_RAW_DIR,
    _build_raw_frontmatter,
    _resolve_fetched_at_date,
    _url_to_slug,
    barrier_raw_node,
    fanout_persist_dispatcher,
    write_raw_one,
)


# ===========================================================================
# 1. _url_to_slug 边界
# ===========================================================================


@pytest.mark.parametrize(
    "url, expected",
    [
        # path 内 `/` 转 `_`，`.` 转 `-`；host 内 `.` 转 `-`
        ("https://demo.ragflow.io/docs/quickstart", "demo-ragflow-io_docs_quickstart"),
        ("https://github.com/hkuds/rag-anything", "github-com_hkuds_rag-anything"),
        ("https://x.io/", "x-io"),
        ("https://x.io", "x-io"),
        # 含端口 → host 里 `:` 转 `-`
        ("http://localhost:8080/api", "localhost-8080_api"),
        # 大写 host 应小写化
        ("https://Foo.Example.COM/Bar", "foo-example-com_bar"),
        # path 内多层级
        ("https://x.io/a/b/c", "x-io_a_b_c"),
        # path 内 `.` → `-`
        ("https://x.io/api/v1.0/foo", "x-io_api_v1-0_foo"),
    ],
)
def test_url_to_slug_basic(url: str, expected: str):
    assert _url_to_slug(url) == expected


def test_url_to_slug_empty_returns_unknown():
    assert _url_to_slug("") == "unknown"


def test_url_to_slug_no_path():
    """纯 host 无 path → 仅 host slug。"""
    assert _url_to_slug("https://demo.ragflow.io") == "demo-ragflow-io"


def test_url_to_slug_chinese_path_replaced():
    """非 [a-z0-9_-] 的路径字符（含 unicode）→ 替换为 `-` 并合并连续 `-`。"""
    s = _url_to_slug("https://x.io/中文路径/page")
    # 中文被 urlparse 保留为原字符，转 lowercase 后被 invalid 正则替换为 `-`
    assert s.startswith("x-io_")
    assert "中" not in s and "文" not in s


def test_url_to_slug_caps_length_at_100():
    """超长 path 应截断到 100 字符以内。"""
    long_path = "a" * 200
    s = _url_to_slug(f"https://x.io/{long_path}")
    assert len(s) <= 100


# ===========================================================================
# 2. _build_raw_frontmatter 字段完整性 + json 转义
# ===========================================================================


def test_build_raw_frontmatter_all_fields_present():
    fm = _build_raw_frontmatter(
        doc_id="x-io-2026-05-09-abc",
        title="Hello World",
        source_type="official-doc",
        source="x.io",
        url="https://x.io/docs",
        fetched_at_date="2026-05-09",
        content_sha256="0" * 64,
        keywords=["a", "b", "c"],
    )
    # 8 字段全在
    for line in [
        "doc_id: x-io-2026-05-09-abc",
        'title: "Hello World"',
        "source_type: official-doc",
        "source: x.io",
        "url: https://x.io/docs",
        "fetched_at: 2026-05-09",
        f"content_sha256: {'0' * 64}",
        'keywords: ["a", "b", "c"]',
    ]:
        assert line in fm
    # frontmatter 包围
    assert fm.startswith("---\n")
    assert fm.endswith("\n---")


def test_build_raw_frontmatter_title_with_colon_escapes():
    """title 含 `:` → json 转义后用双引号包裹，YAML 解析不会被冒号截断。"""
    fm = _build_raw_frontmatter(
        doc_id="x",
        title="Hello: World",
        source_type="community",
        source="x.io",
        url="https://x.io",
        fetched_at_date="2026-05-09",
        content_sha256="abc",
        keywords=[],
    )
    assert 'title: "Hello: World"' in fm


def test_build_raw_frontmatter_title_with_quote_escapes():
    """title 含 `"` → json.dumps 转义为 `\\"`，frontmatter 不破。"""
    fm = _build_raw_frontmatter(
        doc_id="x",
        title='He said "yes"',
        source_type="community",
        source="x.io",
        url="https://x.io",
        fetched_at_date="2026-05-09",
        content_sha256="abc",
        keywords=[],
    )
    assert 'title: "He said \\"yes\\""' in fm


def test_resolve_fetched_at_date_iso_to_date():
    assert _resolve_fetched_at_date("2026-05-09T10:30:45+00:00") == "2026-05-09"


def test_resolve_fetched_at_date_empty_falls_back_to_today():
    """空值 fallback 到 datetime.now() 当天日期。"""
    out = _resolve_fetched_at_date("")
    # 格式 YYYY-MM-DD
    assert len(out) == 10 and out.count("-") == 2


# ===========================================================================
# 3. write_raw_one 成功路径
# ===========================================================================


def _make_candidate(**overrides: Any) -> dict:
    """构造一个最小可工作的 candidate dict（默认含所有必要字段）。"""
    base = {
        "url": "https://demo.ragflow.io/docs/quickstart",
        "title": "RAGFlow 快速启动",
        "markdown": "# RAGFlow\n\n## 启动\n\n先 clone 仓库...\n",
        "content_sha256": "a" * 64,
        "type": "official-doc",
        "keywords": ["ragflow", "docker", "quickstart"],
        "fetched_at": "2026-05-09T10:30:45+00:00",
        "from_engines": ["google"],
        "from_queries": [0],
        "score": 90,
        "summary": "RAGFlow 快速启动指南",
        "whether_in": True,
        "reason": "ok",
    }
    base.update(overrides)
    return base


def test_write_raw_one_success(tmp_path, monkeypatch):
    """成功路径：写 raw .md + 调 chunker → 返回 success=True 单元素 list。"""
    fake_chunks = [tmp_path / "chunks" / "fake-001.md", tmp_path / "chunks" / "fake-002.md"]

    def fake_write_chunks(raw_path: Path, output_dir: Path) -> list[Path]:
        # 模拟 chunker 行为：只返回路径列表，不真的切分
        return fake_chunks

    monkeypatch.setattr("brain_base.nodes.qa_persist.write_chunks", fake_write_chunks)

    raw_dir = tmp_path / "raw"
    chunk_dir = tmp_path / "chunks"

    out = asyncio.run(
        write_raw_one(
            {
                "candidate": _make_candidate(),
                "raw_dir": str(raw_dir),
                "chunk_dir": str(chunk_dir),
            }
        )
    )
    results = out["persist_results"]
    assert len(results) == 1
    r = results[0]
    assert r["success"] is True
    assert r["url"] == "https://demo.ragflow.io/docs/quickstart"
    # doc_id = {slug}-{YYYY-MM-DD}-{8hex}；slug = demo-ragflow-io_docs_quickstart
    assert r["doc_id"].startswith("demo-ragflow-io_docs_quickstart-")
    assert r["doc_id"].count("-") >= 4  # slug 内 `-` + 3 个日期 `-` + 1 个 hash `-`
    assert len(r["chunk_files"]) == 2

    # raw 文件真的写入了
    raw_path = Path(r["raw_path"])
    assert raw_path.exists()
    raw_text = raw_path.read_text(encoding="utf-8")
    assert "doc_id:" in raw_text
    assert 'title: "RAGFlow 快速启动"' in raw_text
    assert "source_type: official-doc" in raw_text
    assert "source: demo.ragflow.io" in raw_text
    assert "url: https://demo.ragflow.io/docs/quickstart" in raw_text
    assert "fetched_at: 2026-05-09" in raw_text
    assert f"content_sha256: {'a' * 64}" in raw_text
    assert 'keywords: ["ragflow", "docker", "quickstart"]' in raw_text
    # 正文写入
    assert "# RAGFlow" in raw_text
    assert "先 clone 仓库" in raw_text


def test_write_raw_one_doc_id_uses_url_slug_prefix(tmp_path, monkeypatch):
    """doc_id 前缀应等于 _url_to_slug(url)，不是固定 `web-`。"""
    monkeypatch.setattr(
        "brain_base.nodes.qa_persist.write_chunks",
        lambda raw_path, out_dir: [out_dir / "x.md"],
    )

    out = asyncio.run(
        write_raw_one(
            {
                "candidate": _make_candidate(url="https://github.com/hkuds/rag-anything"),
                "raw_dir": str(tmp_path / "raw"),
                "chunk_dir": str(tmp_path / "chunks"),
            }
        )
    )
    doc_id = out["persist_results"][0]["doc_id"]
    assert doc_id.startswith("github-com_hkuds_rag-anything-")
    # 不走旧 `web-` 前缀约定
    assert not doc_id.startswith("web-")


def test_write_raw_one_default_dirs_when_not_given(tmp_path, monkeypatch):
    """sub_state 不给 raw_dir/chunk_dir → 走 DEFAULT_RAW_DIR / DEFAULT_CHUNK_DIR 默认值。"""
    captured = {}

    def fake_write_chunks(raw_path: Path, output_dir: Path) -> list[Path]:
        captured["raw_path"] = raw_path
        captured["chunk_dir"] = output_dir
        return [output_dir / "x.md"]

    monkeypatch.setattr("brain_base.nodes.qa_persist.write_chunks", fake_write_chunks)
    monkeypatch.chdir(tmp_path)

    out = asyncio.run(write_raw_one({"candidate": _make_candidate()}))
    assert out["persist_results"][0]["success"] is True
    # chunk_dir 默认值（跨平台用 Path 对比，避免 Windows 反斜杠不一致）
    assert captured["chunk_dir"] == Path(DEFAULT_CHUNK_DIR)
    # raw 写到默认 raw 目录（跨平台）
    assert Path(DEFAULT_RAW_DIR) in captured["raw_path"].parents


# ===========================================================================
# 4. write_raw_one 失败隔离
# ===========================================================================


def test_write_raw_one_empty_url_returns_failure():
    """url 为空 → success=False，error 透传。"""
    out = asyncio.run(write_raw_one({"candidate": _make_candidate(url="")}))
    r = out["persist_results"][0]
    assert r["success"] is False
    assert "empty url" in r["error"]


def test_write_raw_one_empty_markdown_returns_failure():
    """markdown 为空 → success=False。"""
    out = asyncio.run(write_raw_one({"candidate": _make_candidate(markdown="   ")}))
    r = out["persist_results"][0]
    assert r["success"] is False
    assert "empty markdown" in r["error"]


def test_write_raw_one_chunker_raises_returns_failure(tmp_path, monkeypatch):
    """chunker.write_chunks 抛错 → 错误透传到 persist_results，不向上抛。"""
    def boom(raw_path, out_dir):
        raise RuntimeError("chunker explode")

    monkeypatch.setattr("brain_base.nodes.qa_persist.write_chunks", boom)

    out = asyncio.run(
        write_raw_one(
            {
                "candidate": _make_candidate(),
                "raw_dir": str(tmp_path / "raw"),
                "chunk_dir": str(tmp_path / "chunks"),
            }
        )
    )
    r = out["persist_results"][0]
    assert r["success"] is False
    assert "chunker explode" in r["error"]
    # raw 文件应已写入（chunker 失败发生在 raw 写入之后）
    # 但 success=False 时 raw_path 字段留空，便于下游 barrier 不再处理这个 doc
    assert r["raw_path"] == ""


def test_write_raw_one_chunker_returns_empty_list_returns_failure(tmp_path, monkeypatch):
    """chunker 返回 [] → success=False（防止 0 chunks 进入下游 enrich）。"""
    monkeypatch.setattr(
        "brain_base.nodes.qa_persist.write_chunks", lambda raw_path, out_dir: []
    )

    out = asyncio.run(
        write_raw_one(
            {
                "candidate": _make_candidate(),
                "raw_dir": str(tmp_path / "raw"),
                "chunk_dir": str(tmp_path / "chunks"),
            }
        )
    )
    r = out["persist_results"][0]
    assert r["success"] is False
    assert "未生成 chunks" in r["error"]


# ===========================================================================
# 5. barrier_raw_node 聚合
# ===========================================================================


def test_barrier_raw_flattens_success_chunk_files():
    """多 doc success → chunk_files flatten。"""
    state = {
        "persist_results": [
            {
                "doc_id": "d1",
                "raw_path": "raw/d1.md",
                "chunk_files": ["chunks/d1-001.md", "chunks/d1-002.md"],
                "url": "https://x.io/a",
                "success": True,
            },
            {
                "doc_id": "d2",
                "raw_path": "raw/d2.md",
                "chunk_files": ["chunks/d2-001.md"],
                "url": "https://x.io/b",
                "success": True,
            },
        ]
    }
    out = barrier_raw_node(state)
    assert out["chunk_files"] == [
        "chunks/d1-001.md",
        "chunks/d1-002.md",
        "chunks/d2-001.md",
    ]
    assert out["persist_errors"] == []


def test_barrier_raw_collects_failures_into_persist_errors():
    """失败 doc → 不进 chunk_files，错误归 persist_errors。"""
    state = {
        "persist_results": [
            {"chunk_files": ["chunks/d1-001.md"], "url": "https://x.io/a", "success": True, "doc_id": "d1", "raw_path": "p"},
            {"chunk_files": [], "url": "https://x.io/b", "success": False, "error": "chunker boom", "doc_id": "", "raw_path": ""},
        ]
    }
    out = barrier_raw_node(state)
    assert out["chunk_files"] == ["chunks/d1-001.md"]
    assert len(out["persist_errors"]) == 1
    assert "https://x.io/b" in out["persist_errors"][0]
    assert "chunker boom" in out["persist_errors"][0]


def test_barrier_raw_preserves_upstream_persist_errors():
    """上游已有 persist_errors → barrier 累加（不覆盖）。"""
    state = {
        "persist_results": [
            {"chunk_files": [], "url": "https://x.io/c", "success": False, "error": "down", "doc_id": "", "raw_path": ""},
        ],
        "persist_errors": ["upstream_error_1"],
    }
    out = barrier_raw_node(state)
    assert "upstream_error_1" in out["persist_errors"]
    assert any("down" in e for e in out["persist_errors"])


# ===========================================================================
# 6. fanout_persist_dispatcher gate
# ===========================================================================


def test_dispatcher_empty_candidates_short_circuits():
    """get_info_candidates 空 → 短路返回字符串（T28：ingest）。"""
    assert fanout_persist_dispatcher({"get_info_candidates": []}) == "ingest"
    assert fanout_persist_dispatcher({}) == "ingest"


def test_dispatcher_dispatches_one_send_per_candidate():
    """N 个 candidate → N 个 Send 实例。"""
    from langgraph.types import Send

    state = {
        "get_info_candidates": [
            {"url": "https://x.io/a", "markdown": "x"},
            {"url": "https://x.io/b", "markdown": "y"},
            {"url": "https://x.io/c", "markdown": "z"},
        ]
    }
    out = fanout_persist_dispatcher(state)
    assert isinstance(out, list)
    assert len(out) == 3
    for s in out:
        assert isinstance(s, Send)
        assert s.node == "write_raw_one"
        # arg 含 candidate / raw_dir / chunk_dir
        assert "candidate" in s.arg
        assert s.arg["raw_dir"] == DEFAULT_RAW_DIR
        assert s.arg["chunk_dir"] == DEFAULT_CHUNK_DIR
