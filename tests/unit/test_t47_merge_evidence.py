# -*- coding: utf-8 -*-
"""T47.3b 单元测试：merge_evidence_node。

覆盖（2 条，无 LLM）：
- test_merge_evidence_empty_pool：空 pool → empty candidates，get_info_attempted=True
- test_merge_evidence_format_and_sort：3 条 evidence → 13 字段全齐 + score 降序 + 类型正确

契约引用：md/research/2026-05-17-t47-unified-intent-agent-contract.md §7
"""

from __future__ import annotations

import pytest


# get_info_candidates 13 字段（fanout_persist_dispatcher 期望，与 barrier_extract 对齐）
_REQUIRED_CANDIDATE_FIELDS = {
    "url", "title", "fetched_at", "markdown", "content_sha256",
    "from_engines", "from_queries", "score", "type", "summary",
    "keywords", "whether_in", "reason",
}


class TestMergeEvidence:
    """merge_evidence_node：纯格式转换 + 排序，无 LLM。"""

    def test_merge_evidence_empty_pool(self):
        """空 pool → candidates=[]；get_info_attempted=True 防 dispatcher 重触发。"""
        from brain_base.nodes.qa_intent import merge_evidence_node

        out = merge_evidence_node({"evidence_pool": []})
        assert out == {"get_info_candidates": [], "get_info_attempted": True}

        # 缺字段也兜底 → 空 list
        out2 = merge_evidence_node({})
        assert out2 == {"get_info_candidates": [], "get_info_attempted": True}

    def test_merge_evidence_format_and_sort(self):
        """3 条 evidence（score 70/85/55）→ candidates 按 score 降序 + 13 字段齐 + 类型对。

        混合 source_type（official-doc / community）+ 不同 tool_name 验证字段透传。
        """
        from brain_base.nodes.qa_intent import merge_evidence_node

        evidence_pool = [
            {
                "url": "https://example.com/a",
                "title": "Title A",
                "content": "# markdown A",
                "score": 70.4,  # float，merge 后转 int
                "sha256_hash": "sha_aaa",
                "from_queries": ["q1"],
                "snippet": "snippet a",
                "source_type": "community",
                "tool_name": "web_search",
            },
            {
                "url": "https://github.com/x/y",
                "title": "Title B",
                "content": "# markdown B",
                "score": 85.0,
                "sha256_hash": "sha_bbb",
                "from_queries": ["q2", "q2_alt"],
                "snippet": "snippet b",
                "source_type": "official-doc",
                "tool_name": "raw_text",
            },
            {
                "url": "https://example.com/c",
                "title": "Title C",
                "content": "# markdown C",
                "score": 55.5,
                "sha256_hash": "sha_ccc",
                "from_queries": [],
                "snippet": "snippet c",
                "source_type": "community",
                "tool_name": "fetch_url",
            },
        ]
        out = merge_evidence_node({"evidence_pool": evidence_pool})
        candidates = out["get_info_candidates"]

        # 数量 + 排序（85 > 70 > 56）
        assert len(candidates) == 3
        scores = [c["score"] for c in candidates]
        assert scores == sorted(scores, reverse=True)
        # 排序后 url 顺序
        assert candidates[0]["url"] == "https://github.com/x/y"
        assert candidates[1]["url"] == "https://example.com/a"
        assert candidates[2]["url"] == "https://example.com/c"

        # 13 字段全齐
        for c in candidates:
            missing = _REQUIRED_CANDIDATE_FIELDS - set(c.keys())
            assert not missing, f"candidate 缺字段 {missing}: {c}"

        # 类型正确
        for c in candidates:
            assert isinstance(c["score"], int), f"score 应为 int，得 {type(c['score'])}"
            assert isinstance(c["from_queries"], list)
            assert isinstance(c["from_engines"], list)
            assert isinstance(c["keywords"], list)
            assert isinstance(c["whether_in"], bool)
            assert c["whether_in"] is True
            assert c["from_engines"] == []  # intent agent 不用 SERP 引擎源
            assert c["keywords"] == []      # 持久化 enrich 阶段才算

        # 字段映射验证
        cand_b = candidates[0]  # 排序后第一名 url=github.com/x/y
        assert cand_b["markdown"] == "# markdown B"
        assert cand_b["content_sha256"] == "sha_bbb"
        assert cand_b["type"] == "official-doc"
        assert cand_b["summary"] == "snippet b"
        assert cand_b["from_queries"] == ["q2", "q2_alt"]
        assert "raw_text" in cand_b["reason"]  # tool_name 透传到 reason
        # score 70.4 应 round → 70
        cand_a = next(c for c in candidates if c["url"] == "https://example.com/a")
        assert cand_a["score"] == 70

        # fetched_at 是 ISO 字符串
        from datetime import datetime
        for c in candidates:
            # 应能用 fromisoformat 解析（不抛 ValueError）
            datetime.fromisoformat(c["fetched_at"].replace("Z", "+00:00"))
