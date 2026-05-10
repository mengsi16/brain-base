# -*- coding: utf-8 -*-
"""T26.1-c 单元测试：enrich_one + barrier_enrich + ingest_node + fanout_enrich_dispatcher。

覆盖：
- ``fanout_enrich_dispatcher`` gate（chunk_files 空 → "ingest"；非空 → list[Send]）
- ``enrich_one`` 成功路径（4 字段写回 frontmatter）
- ``enrich_one`` 第一次失败 + 重试成功 → success=True
- ``enrich_one`` 所有重试失败 → frontmatter 写 enrich_error，success=False
- ``enrich_one`` 文件不存在 / chunk 无 frontmatter / 空 chunk_file → success=False
- ``enrich_one`` Semaphore 限流（>concurrency 个 Send 时排队，不超过上限）
- ``barrier_enrich_node`` 聚合（success → enriched_chunks；失败 → persist_errors，上游 persist_errors 累加）
- ``ingest_node`` 空 enriched_chunks → ingested_count=0，不调 Milvus
- ``ingest_node`` fail-fast：Milvus 抛错时透传，不 try/except
- ``ingest_node`` 成功路径：调 milvus_ingest_chunks 并返回 inserted 计数
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from brain_base.agents.schemas import ChunkEnrichment
from brain_base.config import GetInfoConfig
from brain_base.nodes import qa_persist as qa_persist_module
from brain_base.nodes.qa_persist import (
    _reset_enrich_semaphore_for_test,
    barrier_enrich_node,
    create_enrich_one,
    fanout_enrich_dispatcher,
    ingest_node,
)


@pytest.fixture(autouse=True)
def _reset_enrich_sem():
    """每个测试独立 Semaphore（不同 event loop 不共享）。"""
    _reset_enrich_semaphore_for_test()
    yield
    _reset_enrich_semaphore_for_test()


# ===========================================================================
# 1. fanout_enrich_dispatcher gate
# ===========================================================================


def test_enrich_dispatcher_empty_chunk_files_short_circuits():
    """chunk_files 空（全部 doc 失败）→ T28：短路 ingest。"""
    assert fanout_enrich_dispatcher({"chunk_files": []}) == "ingest"
    assert fanout_enrich_dispatcher({}) == "ingest"


def test_enrich_dispatcher_dispatches_one_send_per_chunk():
    """N 个 chunk_files → N 个 Send 实例。"""
    from langgraph.types import Send

    state = {"chunk_files": ["a.md", "b.md", "c.md"]}
    out = fanout_enrich_dispatcher(state)
    assert isinstance(out, list)
    assert len(out) == 3
    for s, cf in zip(out, ["a.md", "b.md", "c.md"]):
        assert isinstance(s, Send)
        assert s.node == "enrich_one"
        assert s.arg == {"chunk_file": cf}


# ===========================================================================
# 2. enrich_one helper：构造 chunk file + 默认成功 LLM
# ===========================================================================


def _make_chunk_file(tmp_path: Path, name: str = "c-001.md") -> Path:
    """生成一个带空 enrichment 占位符的 chunk 文件（模拟 chunker 输出）。"""
    text = (
        "---\n"
        "doc_id: test-doc\n"
        "chunk_id: test-doc-001\n"
        "source: official-doc\n"
        'summary: ""\n'
        "keywords: []\n"
        "questions: []\n"
        "---\n"
        "# Title\n\n这是用于测试的 chunk 正文。"
    )
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _good_enrichment() -> ChunkEnrichment:
    return ChunkEnrichment(
        title="测试章节标题",
        # summary 最小 10 字符（schema 约束 min_length=10）
        summary="测试摘要测试摘要测试摘要测试摘要",
        keywords=["k1", "k2", "k3", "k4", "k5"],
        questions=["如何测试？", "What is test?", "测试和验证的区别？"],
    )


# ===========================================================================
# 3. enrich_one 成功路径
# ===========================================================================


def test_enrich_one_success_writes_4_fields_to_frontmatter(tmp_path, monkeypatch):
    """成功路径：4 字段全部写回 frontmatter，success=True。"""
    chunk_path = _make_chunk_file(tmp_path)

    def fake_invoke(llm, schema, sys_prompt, user_prompt):
        assert schema is ChunkEnrichment
        return _good_enrichment()

    monkeypatch.setattr(qa_persist_module, "invoke_structured", fake_invoke)

    node = create_enrich_one(llm=object(), config=GetInfoConfig())
    out = asyncio.run(node({"chunk_file": str(chunk_path)}))
    results = out["enrich_results"]
    assert len(results) == 1
    assert results[0]["success"] is True
    assert results[0]["chunk_file"] == str(chunk_path)

    # frontmatter 4 字段全部写回
    final = chunk_path.read_text(encoding="utf-8")
    assert '"测试章节标题"' in final
    assert "测试摘要" in final
    assert "k1" in final and "k5" in final
    assert "如何测试" in final
    assert "What is test" in final
    # 不留 enrich_error 字段
    assert "enrich_error:" not in final


# ===========================================================================
# 4. enrich_one 重试 1 次成功
# ===========================================================================


def test_enrich_one_first_fail_retry_succeeds(tmp_path, monkeypatch):
    """第一次抛 RuntimeError → 重试成功 → success=True。"""
    chunk_path = _make_chunk_file(tmp_path)
    call_count = {"n": 0}

    def fake_invoke(llm, schema, sys_prompt, user_prompt):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient: 429 rate limit")
        return _good_enrichment()

    monkeypatch.setattr(qa_persist_module, "invoke_structured", fake_invoke)
    monkeypatch.setattr(qa_persist_module, "ENRICH_RETRY_BACKOFF_SEC", 0.0)

    node = create_enrich_one(llm=object(), config=GetInfoConfig())
    out = asyncio.run(node({"chunk_file": str(chunk_path)}))
    assert out["enrich_results"][0]["success"] is True
    assert call_count["n"] == 2  # 1 次失败 + 1 次重试

    # 重试成功后 frontmatter 不留 enrich_error
    final = chunk_path.read_text(encoding="utf-8")
    assert "enrich_error:" not in final
    assert '"测试章节标题"' in final


# ===========================================================================
# 5. enrich_one 所有重试失败
# ===========================================================================


def test_enrich_one_all_retries_fail_writes_enrich_error(tmp_path, monkeypatch):
    """所有重试都失败 → frontmatter 写 enrich_error，success=False。"""
    chunk_path = _make_chunk_file(tmp_path)

    def always_fail(llm, schema, sys_prompt, user_prompt):
        raise ValueError("schema mismatch: keywords < 5")

    monkeypatch.setattr(qa_persist_module, "invoke_structured", always_fail)
    monkeypatch.setattr(qa_persist_module, "ENRICH_RETRY_BACKOFF_SEC", 0.0)

    node = create_enrich_one(llm=object(), config=GetInfoConfig())
    out = asyncio.run(node({"chunk_file": str(chunk_path)}))
    r = out["enrich_results"][0]
    assert r["success"] is False
    assert "ValueError" in r["error"]
    assert "schema mismatch" in r["error"]

    # frontmatter 写入 enrich_error 字段
    final = chunk_path.read_text(encoding="utf-8")
    assert "enrich_error:" in final
    assert "ValueError" in final


# ===========================================================================
# 6. enrich_one 失败隔离（外层异常）
# ===========================================================================


def test_enrich_one_empty_chunk_file_returns_failure():
    """chunk_file 空字符串 → success=False，error='empty chunk_file'。"""
    node = create_enrich_one(llm=object(), config=GetInfoConfig())
    out = asyncio.run(node({"chunk_file": ""}))
    r = out["enrich_results"][0]
    assert r["success"] is False
    assert "empty chunk_file" in r["error"]


def test_enrich_one_missing_file_returns_failure(tmp_path):
    """chunk 文件不存在 → success=False，FileNotFoundError 透传。"""
    node = create_enrich_one(llm=object(), config=GetInfoConfig())
    out = asyncio.run(node({"chunk_file": str(tmp_path / "nonexistent.md")}))
    r = out["enrich_results"][0]
    assert r["success"] is False
    assert "not found" in r["error"]


def test_enrich_one_no_frontmatter_returns_failure(tmp_path):
    """chunk 文件无 frontmatter → success=False。"""
    bad = tmp_path / "bad.md"
    bad.write_text("# 只有正文，没有 frontmatter\n\n内容", encoding="utf-8")

    node = create_enrich_one(llm=object(), config=GetInfoConfig())
    out = asyncio.run(node({"chunk_file": str(bad)}))
    r = out["enrich_results"][0]
    assert r["success"] is False
    assert "frontmatter" in r["error"]


# ===========================================================================
# 7. enrich_one Semaphore 限流
# ===========================================================================


def test_enrich_one_semaphore_caps_concurrency(tmp_path, monkeypatch):
    """5 个 Send 同时跑 + Semaphore=2 → 任一时刻 in_flight ≤ 2。"""
    chunk_paths = [_make_chunk_file(tmp_path, f"c-{i:03d}.md") for i in range(5)]

    in_flight = {"n": 0, "peak": 0}
    lock = asyncio.Lock()

    async def fake_invoke_async(llm, schema, sys_prompt, user_prompt):
        # 注意：invoke_structured 是 sync，会被 asyncio.to_thread 包到线程；
        # 这里用 sync 函数模拟，借助上下文计数验证并发上限
        return _good_enrichment()

    def fake_invoke_sync(llm, schema, sys_prompt, user_prompt):
        # sync 但触发 in_flight 计数（基于 GIL 的 thread safety）
        in_flight["n"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["n"])
        # 模拟 LLM 处理耗时让其他 Send 有机会并发触达
        import time
        time.sleep(0.05)
        in_flight["n"] -= 1
        return _good_enrichment()

    monkeypatch.setattr(qa_persist_module, "invoke_structured", fake_invoke_sync)

    node = create_enrich_one(
        llm=object(), config=GetInfoConfig(enrich_concurrency=2)
    )

    async def run_all():
        return await asyncio.gather(
            *[node({"chunk_file": str(p)}) for p in chunk_paths]
        )

    outs = asyncio.run(run_all())
    assert len(outs) == 5
    for o in outs:
        assert o["enrich_results"][0]["success"] is True
    # Semaphore=2 → 峰值并发 ≤ 2
    assert in_flight["peak"] <= 2, (
        f"Semaphore=2 但峰值并发达到 {in_flight['peak']}，限流失败"
    )


# ===========================================================================
# 8. barrier_enrich_node 聚合
# ===========================================================================


def test_barrier_enrich_collects_only_success_to_enriched_chunks():
    """success=True → 进 enriched_chunks；False → 归 persist_errors。"""
    state = {
        "enrich_results": [
            {"chunk_file": "a.md", "success": True},
            {"chunk_file": "b.md", "success": False, "error": "ValueError: bad"},
            {"chunk_file": "c.md", "success": True},
        ]
    }
    out = barrier_enrich_node(state)
    assert out["enriched_chunks"] == ["a.md", "c.md"]
    assert len(out["persist_errors"]) == 1
    assert "b.md" in out["persist_errors"][0]
    assert "ValueError: bad" in out["persist_errors"][0]


def test_barrier_enrich_preserves_upstream_persist_errors():
    """上游 persist_errors（来自 barrier_raw 的 write_raw 失败）应累加。"""
    state = {
        "enrich_results": [
            {"chunk_file": "x.md", "success": False, "error": "down"},
        ],
        "persist_errors": ["write_raw https://x.io/a: chunker boom"],
    }
    out = barrier_enrich_node(state)
    assert any("chunker boom" in e for e in out["persist_errors"])
    assert any("down" in e for e in out["persist_errors"])
    assert out["enriched_chunks"] == []


# ===========================================================================
# 9. ingest_node 空 + 成功 + fail-fast
# ===========================================================================


def test_ingest_node_empty_returns_zero(monkeypatch):
    """enriched_chunks 空 → ingested_count=0，不调 Milvus。"""
    called = {"n": 0}

    def should_not_be_called(chunk_files):
        called["n"] += 1
        return {"inserted": 999}

    monkeypatch.setattr(qa_persist_module, "milvus_ingest_chunks", should_not_be_called)

    out = ingest_node({"enriched_chunks": []})
    assert out == {"ingested_count": 0}
    assert called["n"] == 0  # 真没调


def test_ingest_node_calls_milvus_and_returns_inserted_count(monkeypatch):
    """非空 → 调 milvus_ingest_chunks，返回 inserted 计数。"""
    captured = {}

    def fake_ingest(chunk_files):
        captured["chunk_files"] = chunk_files
        return {"inserted": 42}

    monkeypatch.setattr(qa_persist_module, "milvus_ingest_chunks", fake_ingest)

    out = ingest_node({"enriched_chunks": ["a.md", "b.md"]})
    assert out == {"ingested_count": 42}
    # chunk_files 转换成 Path 对象传递
    assert all(isinstance(p, Path) for p in captured["chunk_files"])
    assert [p.name for p in captured["chunk_files"]] == ["a.md", "b.md"]


def test_ingest_node_fail_fast_propagates_milvus_error(monkeypatch):
    """Milvus 抛错 → fail-fast 透传，不 try/except 吞掉。"""
    def boom(chunk_files):
        raise RuntimeError("Milvus connection refused")

    monkeypatch.setattr(qa_persist_module, "milvus_ingest_chunks", boom)

    with pytest.raises(RuntimeError, match="Milvus connection refused"):
        ingest_node({"enriched_chunks": ["a.md"]})
