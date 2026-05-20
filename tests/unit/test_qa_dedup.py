# -*- coding: utf-8 -*-
"""qa.py:_dedup_evidence_by_chunk_id helper 单元测试。

T54 迁移：原测试在 tests/unit/test_qa_get_info_loop.py（GetInfoGraph 链路），随
GetInfoGraph 整条链路删除时迁出本 helper 测试。`_dedup_evidence_by_chunk_id`
是 QA 主图（barrier2 后跨子问题去重）仍在用的纯逻辑 helper，与 GetInfoGraph 无关。

历史出处：T19-B2「跨组 evidence 污染修复」（见 md/archive/ToDo-Phase-15-21.md）。

运行方式：

    pytest tests/unit/test_qa_dedup.py -v
"""
from __future__ import annotations

import sys

import pytest

from brain_base.nodes.qa import _dedup_evidence_by_chunk_id


def test_dedup_helper_keeps_highest_score_per_chunk_id():
    """helper 级别：同 chunk_id 保留最高 score 的副本，顺序按首次出现。"""
    evidence = [
        {"chunk_id": "c1", "sub_idx": 0, "score": 0.5, "text": "a"},
        {"chunk_id": "c2", "sub_idx": 0, "score": 0.7, "text": "b"},
        {"chunk_id": "c1", "sub_idx": 1, "score": 0.9, "text": "c"},  # c1 的更高 score
        {"chunk_id": "c3", "sub_idx": 2, "score": 0.3, "text": "d"},
        {"chunk_id": "c1", "sub_idx": 2, "score": 0.1, "text": "e"},  # 不应覆盖 0.9
    ]
    out = _dedup_evidence_by_chunk_id(evidence)

    assert len(out) == 3, f"3 个独立 chunk_id，实际 {len(out)}"
    # 按首次出现顺序：c1, c2, c3
    assert [ev["chunk_id"] for ev in out] == ["c1", "c2", "c3"]
    # c1 保留 sub_idx=1 score=0.9 这条
    c1 = next(ev for ev in out if ev["chunk_id"] == "c1")
    assert c1["sub_idx"] == 1 and c1["score"] == 0.9 and c1["text"] == "c"


def test_dedup_helper_preserves_no_chunk_id_evidence():
    """helper：无 chunk_id 且无 id 的 evidence 原样保留，追加在末尾。"""
    evidence = [
        {"chunk_id": "c1", "score": 0.5},
        {"source": "fs_grep", "text": "match"},  # 无 chunk_id / id
        {"chunk_id": "c1", "score": 0.9},  # dedup
        {"source": "fs_grep", "text": "match2"},  # 无 chunk_id
    ]
    out = _dedup_evidence_by_chunk_id(evidence)

    assert len(out) == 3  # 1 个 c1 + 2 个无 cid
    # c1 先，然后 2 个 no_cid 按原顺序
    assert out[0]["chunk_id"] == "c1" and out[0]["score"] == 0.9
    assert out[1]["text"] == "match"
    assert out[2]["text"] == "match2"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
