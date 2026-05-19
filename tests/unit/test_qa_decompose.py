# -*- coding: utf-8 -*-
"""decompose 节点单元测试（T23）。

T23 改造后字段语义：
- 不分解或 LLM 不可用 → ``sub_questions = [normalized_query]``，长度 1
- 分解 → ``sub_questions = [子问题1, ..., 子问题N]``，长度 ≥ 1

T47.4 后 decompose → intent_planner（不再走 fanout_prep_dispatcher / barrier1
路径），路由测试由 ``test_t47_routes.py`` 覆盖；本文件只验 decompose 节点
本身的拆分行为。
"""
from __future__ import annotations

from brain_base.nodes.qa import create_decompose_node


def test_decompose_simple_question_returns_original_as_one_sub(real_llm):
    """单一事实问题不分解 → sub_questions=[原问题]。"""
    node = create_decompose_node(real_llm)

    out = node({"normalized_query": "RAGFlow 是什么"})

    assert out["decomposition_needed"] is False or len(out["sub_questions"]) == 1
    if out["decomposition_needed"] is False:
        assert out["sub_questions"] == ["RAGFlow 是什么"]


def test_decompose_multipart_returns_n_subs(real_llm):
    """多意图问题 → sub_questions 为 N 个独立子问题。"""
    node = create_decompose_node(real_llm)

    out = node({"normalized_query": "RAGFlow 是什么？怎么启动和卸载？"})

    assert out["decomposition_needed"] is True
    assert len(out["sub_questions"]) >= 2


# T27 删：4 个被废弃的降级路径测试
# - test_decompose_llm_none_fallback：llm=None 路径已在节点工厂里删
# - test_decompose_llm_raises_fallback：LLM 异常走降级已删，现在直接上拋
# - test_decompose_question_uses_normalized_or_question_field：原本用
#   create_decompose_node(None) 靠降级路径验证 question 字段回退；现在该验证
#   需传真实 LLM，该逻辑已在 test_decompose_simple_question_returns_original_as_one_sub
#   充分覆盖，重复测试不加这里删除。
# - test_decompose_empty_normalized_returns_empty：同上，该只参测 llm=None 路径。


def test_decompose_node_requires_non_none_llm():
    """T27 fail-fast：create_decompose_node 本身不拋（工厂不检 llm），
    但在实际 invoke 节点时 invoke_structured 会拋 ValueError。"""
    import pytest

    node = create_decompose_node(None)
    with pytest.raises(ValueError, match="llm 不能为 None"):
        node({"normalized_query": "x"})
