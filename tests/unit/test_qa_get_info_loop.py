# -*- coding: utf-8 -*-
"""QA 自动外检闭环（T10）单元测试：路由 + 配额 + 防死循环。

只验图编译、节点工厂行为与条件路由，不真调外检 / 入库 / Milvus。

运行方式：

    pytest tests/unit/test_qa_get_info_loop.py -v

也可作为脚本独立跑（用于调试）：

    python tests/unit/test_qa_get_info_loop.py
"""
from __future__ import annotations

import sys

import pytest

from brain_base.config import GetInfoConfig
from brain_base.graph.conditional_logic import ConditionalLogic
from brain_base.graphs.qa_graph import QaGraph
from brain_base.nodes.qa import (
    create_get_info_trigger_node,
    create_select_candidates_node,
)


def test_qa_graph_compiles_with_loop_nodes():
    """QaGraph 必须包含外检闭环的 5 个新节点（编译期约束）。"""
    g = QaGraph(llm=None)
    nodes = set(g.graph.nodes.keys())
    expected_new = {
        "get_info_trigger",
        "web_research",
        "select_candidates",
        "ingest_candidates",
        "re_search",
    }
    missing = expected_new - nodes
    assert not missing, f"QaGraph 缺少外检闭环节点：{missing}"


def test_select_candidates_quota_filtering():
    """select_candidates 必须按 official/community 配额截断、丢弃 discard 与空 URL。"""
    cfg = GetInfoConfig(max_official=3, max_community=2, max_total=4)
    select = create_select_candidates_node(cfg)
    state = {
        "get_info_candidates": [
            {"url": "https://docs.a.com", "source_type": "official-doc", "title_hint": "A docs"},
            {"url": "https://docs.b.com", "source_type": "official-doc", "title_hint": "B docs"},
            {"url": "https://docs.c.com", "source_type": "official-doc", "title_hint": "C docs"},
            # max_official=3，第 4 个 official 被截
            {"url": "https://docs.d.com", "source_type": "official-doc", "title_hint": "D docs"},
            {"url": "https://blog.e.com", "source_type": "community", "title_hint": "E blog"},
            {"url": "https://blog.f.com", "source_type": "community", "title_hint": "F blog"},
            # max_total=4 总额截断
            {"url": "https://blog.g.com", "source_type": "community", "title_hint": "G blog"},
            # 必丢
            {"url": "https://spam.com", "source_type": "discard", "title_hint": "spam"},
            {"url": "", "source_type": "official-doc", "title_hint": "no url"},
        ]
    }
    out = select(state)
    targets = out.get("ingest_targets", [])
    urls = [t["url"] for t in targets]

    assert len(targets) == 4, f"max_total=4 应只剩 4 条，实际 {len(targets)}"
    assert urls[:3] == [
        "https://docs.a.com",
        "https://docs.b.com",
        "https://docs.c.com",
    ], "official-doc 必须排在前面（前 3 条）"
    assert "https://spam.com" not in urls, "discard 必须被丢弃"
    assert "https://docs.d.com" not in urls, "max_official=3 后第 4 个 official 必须被截"


def test_get_info_trigger_heuristic_paths():
    """启发式触发器（llm=None）：四种典型组合的预期返回值。"""
    trigger = create_get_info_trigger_node(llm=None, config=GetInfoConfig())

    # case1: evidence=[] + playwright=True + 未 attempted → 触发
    out = trigger(
        {"question": "x", "evidence": [], "infra_status": {"playwright_available": True}}
    )
    assert out["trigger_get_info"] is True, "无证据 + playwright 可用 + 未尝试 → 必须触发"

    # case2: playwright 不可用 → 软依赖降级，不触发
    out = trigger(
        {"question": "x", "evidence": [], "infra_status": {"playwright_available": False}}
    )
    assert out["trigger_get_info"] is False, "playwright 不可用时禁止触发"

    # case3: 已 attempted → 防死循环
    out = trigger(
        {
            "question": "x",
            "evidence": [],
            "infra_status": {"playwright_available": True},
            "get_info_attempted": True,
        }
    )
    assert out["trigger_get_info"] is False, "attempted=True 后不再触发"

    # case4: 配置 enable=False → 顶层关闭
    cfg_disabled = GetInfoConfig(enable=False)
    trigger_disabled = create_get_info_trigger_node(llm=None, config=cfg_disabled)
    out = trigger_disabled(
        {"question": "x", "evidence": [], "infra_status": {"playwright_available": True}}
    )
    assert out["trigger_get_info"] is False, "config.enable=False 时必须忽略所有信号"


def test_routing_anti_infinite_loop():
    """conditional_logic 路由必须保证：第二轮 judge 强制 answer，避免外检无限循环。"""
    r = ConditionalLogic()

    # 首轮 judge：证据不足且未尝试 → get_info_trigger
    assert r.after_judge({"evidence_sufficient": False}) == "get_info_trigger"

    # 第二轮 judge：已 attempted → 即使证据仍不足也走 answer（防死循环）
    assert (
        r.after_judge({"evidence_sufficient": False, "get_info_attempted": True}) == "answer"
    )

    # 证据充足 → answer
    assert r.after_judge({"evidence_sufficient": True}) == "answer"

    # trigger 路由
    assert r.after_get_info_trigger({"trigger_get_info": True}) == "web_research"
    assert r.after_get_info_trigger({"trigger_get_info": False}) == "answer"


# -----------------------------------------------------------------------------
# 脚本入口（保留以便不装 pytest 时也能跑）
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
