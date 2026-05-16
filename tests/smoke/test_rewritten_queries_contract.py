# -*- coding: utf-8 -*-
"""Smoke test：真实 rewrite 返回若把 queries 写成字符串数组，应触发 schema 失败。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from brain_base.agents.schemas import RewrittenQueries


def test_rewritten_queries_rejects_string_items_from_real_llm_output() -> None:
    payload = {
        "queries": [
            "Continuous Thought Machine 连续思维机是什么",
            "Continuous Thought Machine CTM architecture 连续思维机",
            "Continuous Thought Machine 原理 架构",
            "连续思维机 工作机制 AI架构",
            "Continuous Thought Machine 是一种新型神经网络架构，它通过持续的状态更新机制实现...",
        ],
        "lexical_query": "Continuous Thought Machine 原理",
    }

    with pytest.raises(ValidationError) as exc_info:
        RewrittenQueries.model_validate(payload)

    error_locs = [tuple(err["loc"]) for err in exc_info.value.errors()]
    assert error_locs == [
        ("queries", 0),
        ("queries", 1),
        ("queries", 2),
        ("queries", 3),
        ("queries", 4),
    ]
