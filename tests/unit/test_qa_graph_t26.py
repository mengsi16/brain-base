# -*- coding: utf-8 -*-
"""T26.1-d 主图集成验证。

不真跑 e2e（涉及 playwright / Milvus / LLM 太多 mock），只做编译 + 拓扑验证：
- 主图 compile() 不抛错
- T26.1 新增 5 节点都在拓扑里：write_raw_one / barrier_raw / enrich_one / barrier_enrich / ingest
- T25 老节点（barrier_extract 等）仍存在不被破坏
- T28：legacy_dense_search 已删除，被 PIPE2 第二段子图（fanout_search × N + barrier2）替代
- 老边 barrier_extract → legacy_dense_search 不再是 add_edge（被条件边替代）
- run() 初始化 state 含 4 个新字段（persist_results=[] / enrich_results=[] / persist_errors=[] / ingested_count=0）
"""
from __future__ import annotations

import pytest

from brain_base.config import GetInfoConfig
from brain_base.graphs.qa_graph import QaGraph


def test_qa_graph_init_raises_on_none_llm():
    """T27 fail-fast：QaGraph(llm=None) 直接 raise ValueError。"""
    with pytest.raises(ValueError, match="non-None llm"):
        QaGraph(llm=None)


def test_qa_graph_compiles_with_persist_pipeline(mock_llm):
    """T26.1 全节点接入后 QaGraph 仍能编译。"""
    g = QaGraph(llm=mock_llm)
    assert g.graph is not None


def test_t26_1_new_nodes_registered(mock_llm):
    """T26.1 新增的 5 个节点都注册到主图。"""
    g = QaGraph(llm=mock_llm)
    nodes = set(g.graph.nodes.keys())
    assert "write_raw_one" in nodes
    assert "barrier_raw" in nodes
    assert "enrich_one" in nodes
    assert "barrier_enrich" in nodes
    assert "ingest" in nodes


def test_t25_and_t23_nodes_still_present(mock_llm):
    """T26.1 不破坏 T25 / T23 上游节点。"""
    g = QaGraph(llm=mock_llm)
    nodes = set(g.graph.nodes.keys())
    # T25 上游
    assert "merge_search_keywords" in nodes
    assert "search_web_dual" in nodes
    assert "fetch_extract_one" in nodes
    assert "barrier_extract" in nodes
    # T23 第一段
    assert "subquery_prep" in nodes
    assert "barrier1" in nodes
    # T28：legacy_dense_search 已删（详见 test_qa_graph_t28.py::test_t28_legacy_dense_search_removed）


def test_run_initial_state_includes_persist_fields(mock_llm, monkeypatch):
    """run() 初始化 state 含 T26.1 新加的 4 个字段，防 reducer 首次 add 抛错。

    只验证 initial dict 构造，不真跑 graph（那需要 mock LLM/Milvus/playwright）。
    """
    g = QaGraph(llm=mock_llm)
    captured: dict = {}

    async def fake_ainvoke(initial, config=None):
        captured.update(initial)
        return initial

    g.graph.ainvoke = fake_ainvoke
    g.run("RAGFlow 怎么启动？")

    # T26.1 新字段
    assert captured["persist_results"] == []
    assert captured["enrich_results"] == []
    assert captured["persist_errors"] == []
    assert captured["ingested_count"] == 0
    # T25 / T23 既有字段仍在
    assert captured["sub_prep_results"] == []
    assert captured["extract_results"] == []
    assert captured["get_info_attempted"] is False
    assert captured["get_info_config"] is g.config


def test_qa_graph_uses_enrich_concurrency_from_config(mock_llm):
    """传入 enrich_concurrency=2 → enrich_one 节点 Semaphore 用 2。"""
    g = QaGraph(llm=mock_llm, get_info_config=GetInfoConfig(enrich_concurrency=2))
    assert g.config.enrich_concurrency == 2
    # 节点工厂只关心 cfg.enrich_concurrency，不直接 expose Semaphore；
    # 此处只验证 config 正确穿透（详细 Semaphore 行为已在 test_qa_persist_enrich.py 覆盖）


def test_barrier_extract_no_longer_directly_connects_legacy_dense_search(mock_llm):
    """老 add_edge(barrier_extract → legacy_dense_search) 已被条件边替代。

    LangGraph compile 后边集合用 g.graph.builder.edges 检查（StateGraph 内部存边的列表）；
    无条件 add_edge 写入这个 set，条件边写入 branches。

    T28：legacy_dense_search 已整体删除，ingest 后接 fanout_search_dispatcher (条件边)；
    原老边 (ingest, legacy_dense_search) 不再存在。
    """
    g = QaGraph(llm=mock_llm)
    # StateGraph.compile 后内部的 edges 是 set[tuple[str, str]]
    edges = g.graph.builder.edges
    # 老边删除
    assert ("barrier_extract", "legacy_dense_search") not in edges
    # T28：ingest → legacy_dense_search 老边也删
    assert ("ingest", "legacy_dense_search") not in edges
    # 新链路若干无条件边在
    assert ("write_raw_one", "barrier_raw") in edges
    assert ("enrich_one", "barrier_enrich") in edges
    assert ("barrier_enrich", "ingest") in edges
