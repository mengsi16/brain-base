# -*- coding: utf-8 -*-
"""T25-C 主图集成验证（T47.4 改写版）。

不真跑 e2e（涉及 playwright / Milvus / LLM 太多 mock），只做编译 + 拓扑验证：
- 主图 compile() 不抛错
- T25 新节点（merge_search_keywords / search_web_dual / fetch_extract_one /
  barrier_extract）T47.4 后均已从主图拔除——原 T25 期间验证需求转为 T47.4 反向断言
- T25 删的老节点仍不在拓扑里：get_info_trigger / web_research / select_candidates / ingest_candidates / re_search
- T23 fanout_prep 节点（subquery_prep / barrier1）T47.4 后也已拔除
- run() 初始化字段含 extract_results=[]（字段保留防 reducer 报错，T47.6 才删）
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


def test_t25_nodes_removed_in_t47_4(mock_llm):
    """T47.4 后 T25 节点均已从主图拔除（节点函数文件 T47.6 才删）。

    原测试 ``test_t25_new_nodes_registered`` 断言 4 个节点在主图，T47.4 重组后
    parallel 通道 4 个节点均被拔除——转为反向断言验证 T47.4 wiring 干净。
    """
    g = QaGraph(llm=mock_llm)
    nodes = set(g.graph.nodes.keys())
    assert "merge_search_keywords" not in nodes
    assert "search_web_dual" not in nodes
    assert "fetch_extract_one" not in nodes
    assert "barrier_extract" not in nodes


def test_t25_old_nodes_removed(mock_llm):
    """T25 删除的 5 个老节点不在主图（judge 后外检回路）。"""
    g = QaGraph(llm=mock_llm)
    nodes = set(g.graph.nodes.keys())
    assert "get_info_trigger" not in nodes
    assert "web_research" not in nodes
    assert "select_candidates" not in nodes
    assert "ingest_candidates" not in nodes
    assert "re_search" not in nodes


def test_t23_nodes_removed_in_t47_4(mock_llm):
    """T47.4 后 T23 fanout_prep 节点均已从主图拔除。

    原测试 ``test_t23_nodes_still_present`` 断言 subquery_prep / barrier1 仍在主图，
    T47.4 重组后 parallel 通道整体被拔除——转为反向断言验证 wiring 干净。
    """
    g = QaGraph(llm=mock_llm)
    nodes = set(g.graph.nodes.keys())
    assert "subquery_prep" not in nodes
    assert "barrier1" not in nodes
    # T28：legacy_dense_search 已删（详见 test_qa_graph_t28.py::test_t28_legacy_dense_search_removed）


def test_run_initial_state_includes_t47_reducer_fields(mock_llm, monkeypatch):
    """run() 初始化 state 含 T26.1 + T28 reducer 字段防 reducer 首次 add 抛错。

    T47.6 后已删除 T23 sub_prep_results / T25 extract_results 初始化（D1 决策，
    对应节点 qa_prep / qa_get_info 部分函数已删，主图无写入节点）。
    剩余 reducer 字段：persist_results / enrich_results（T26.1）+ sub_evidence（T28）。
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
    # T26.1 reducer 字段初始化（write_raw_one × N / enrich_one × M）
    assert captured["persist_results"] == []
    assert captured["enrich_results"] == []
    # T28 PIPE2 reducer 字段（subquery_search_one × N）
    assert captured["sub_evidence"] == []
    assert captured["get_info_attempted"] is False
    assert captured["get_info_config"] is g.config
    # T47.6 反向断言：已删字段不应出现
    assert "sub_prep_results" not in captured
    assert "extract_results" not in captured
