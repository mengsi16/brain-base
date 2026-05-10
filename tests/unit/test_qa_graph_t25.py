# -*- coding: utf-8 -*-
"""T25-C 主图集成验证。

不真跑 e2e（涉及 playwright / Milvus / LLM 太多 mock），只做编译 + 拓扑验证：
- 主图 compile() 不抛错
- T25 新节点在拓扑里：merge_search_keywords / search_web_dual / fetch_extract_one / barrier_extract
- T25 删的老节点不在拓扑里：get_info_trigger / web_research / select_candidates / ingest_candidates / re_search
- run() 初始化字段含 extract_results=[]
- get_info_config 在 state 里
"""
from __future__ import annotations

from brain_base.config import GetInfoConfig
from brain_base.graphs.qa_graph import QaGraph


def test_qa_graph_compiles_with_default_config(mock_llm):
    """QaGraph(llm=mock_llm) 默认参数构造 + compile 不抛错。"""
    g = QaGraph(llm=mock_llm)
    assert g.graph is not None


def test_qa_graph_compiles_with_custom_config(mock_llm):
    """传入自定义 GetInfoConfig（fetch_extract_concurrency=2）也能编译。"""
    g = QaGraph(llm=mock_llm, get_info_config=GetInfoConfig(fetch_extract_concurrency=2))
    assert g.graph is not None
    assert g.config.fetch_extract_concurrency == 2


def test_t25_new_nodes_registered(mock_llm):
    """T25 新增 4 个节点都注册到主图。"""
    g = QaGraph(llm=mock_llm)
    nodes = set(g.graph.nodes.keys())
    assert "merge_search_keywords" in nodes
    assert "search_web_dual" in nodes
    assert "fetch_extract_one" in nodes
    assert "barrier_extract" in nodes


def test_t25_old_nodes_removed(mock_llm):
    """T25 删除的 5 个老节点不在主图（judge 后外检回路）。"""
    g = QaGraph(llm=mock_llm)
    nodes = set(g.graph.nodes.keys())
    assert "get_info_trigger" not in nodes
    assert "web_research" not in nodes
    assert "select_candidates" not in nodes
    assert "ingest_candidates" not in nodes
    assert "re_search" not in nodes


def test_t23_nodes_still_present(mock_llm):
    """T23 第一段 fanout_prep 节点不被破坏。"""
    g = QaGraph(llm=mock_llm)
    nodes = set(g.graph.nodes.keys())
    assert "subquery_prep" in nodes
    assert "barrier1" in nodes
    # T28：legacy_dense_search 已删（详见 test_qa_graph_t28.py::test_t28_legacy_dense_search_removed）


def test_run_initial_state_includes_extract_results(mock_llm, monkeypatch):
    """run() 初始化 state 含 extract_results=[] 防 reducer 首次 add 抛错。

    只验证 initial dict 构造，不真跑 graph（那需要 mock LLM/Milvus/playwright）。
    """
    g = QaGraph(llm=mock_llm)
    captured: dict = {}

    async def fake_ainvoke(initial, config=None):
        captured.update(initial)
        return initial

    g.graph.ainvoke = fake_ainvoke
    g.run("RAGFlow 怎么启动？")

    assert captured["question"] == "RAGFlow 怎么启动？"
    assert captured["sub_prep_results"] == []
    assert captured["extract_results"] == []
    assert captured["get_info_attempted"] is False
    assert captured["get_info_config"] is g.config
