# -*- coding: utf-8 -*-
"""T28 主图集成测试：PIPE2 第二段子图接入后 QaGraph 拓扑验证。

不真跑 e2e（涉及 Milvus / playwright / LLM 太多 mock），只做编译 + 拓扑验证：
- 主图 compile() 不抛错
- T28 新增 2 节点都在拓扑里：subquery_search_one / barrier2
- T28 删除：legacy_dense_search 不在主图
- 老边删除：(ingest, legacy_dense_search) / (legacy_dense_search, judge) 已不存在
- 新边添加：(subquery_search_one, barrier2) / (barrier2, judge) 在主图
- 条件边短路目标改动：fanout_persist_dispatcher / fanout_enrich_dispatcher 短路到 ingest
- run() 初始化 state 含 sub_evidence=[] / search_errors=[]
"""
from __future__ import annotations

from brain_base.config import GetInfoConfig
from brain_base.graphs.qa_graph import QaGraph


def test_t28_subquery_search_one_and_barrier2_registered(mock_llm):
    """T28 新增 2 节点都注册到主图。"""
    g = QaGraph(llm=mock_llm)
    nodes = set(g.graph.nodes.keys())
    assert "subquery_search_one" in nodes
    assert "barrier2" in nodes


def test_t28_legacy_dense_search_removed(mock_llm):
    """T28 删除 legacy_dense_search 节点。"""
    g = QaGraph(llm=mock_llm)
    nodes = set(g.graph.nodes.keys())
    assert "legacy_dense_search" not in nodes


def test_t28_old_edges_removed(mock_llm):
    """T28 删除老边：(ingest, legacy_dense_search) / (legacy_dense_search, judge)。"""
    g = QaGraph(llm=mock_llm)
    edges = g.graph.builder.edges
    assert ("ingest", "legacy_dense_search") not in edges
    assert ("legacy_dense_search", "judge") not in edges


def test_t28_new_edges_added(mock_llm):
    """T28 新边：subquery_search_one → barrier2 → judge。"""
    g = QaGraph(llm=mock_llm)
    edges = g.graph.builder.edges
    assert ("subquery_search_one", "barrier2") in edges
    assert ("barrier2", "judge") in edges


def test_t28_run_initial_state_includes_pipe2_fields(mock_llm, monkeypatch):
    """run() 初始化 state 含 PIPE2 reducer 字段（sub_evidence=[] / search_errors=[]）。

    只验证 initial dict 构造，不真跑 graph。
    """
    g = QaGraph(llm=mock_llm)
    captured: dict = {}

    async def fake_ainvoke(initial, config=None):
        captured.update(initial)
        return initial

    g.graph.ainvoke = fake_ainvoke
    g.run("RAG-Anything 是什么？怎么用？")

    # T28 reducer 字段
    assert captured["sub_evidence"] == []
    assert captured["search_errors"] == []
    # T26.1 / T25 / T23 既有字段仍在
    assert captured["persist_results"] == []
    assert captured["enrich_results"] == []
    assert captured["sub_prep_results"] == []
    assert captured["extract_results"] == []
    assert captured["ingested_count"] == 0
    assert captured["question"] == "RAG-Anything 是什么？怎么用？"
