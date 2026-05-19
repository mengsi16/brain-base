# -*- coding: utf-8 -*-
"""GetInfo 上传子图（get_info_graph.py）选择器 / 评分节点单元测试。

T47.5 清退后，本文件仅覆盖与 QA 主图无关的 GetInfoGraph 子图节点（select_
candidates / fan_out_to_preview / preview_score_one / merge_scores）+ helper
（_url_priority_score / _candidate_priority / _dedup_evidence_by_chunk_id）。

T47.5 删除的 6 条（依赖 T47.4 已拔除的路由 / re_search_node）：
- test_routing_anti_infinite_loop：after_judge / after_get_info_trigger 路由在
  T47.4 主图中不再调用（留函数体，T47.6 才删）
- test_after_barrier1_routes_by_sub_needs_get_info：T47.4 拔除 barrier1 后
  该路由不被使用，断言返回值包含 T47.6 删除节点 "merge_search_keywords"
- test_re_search_multihop_*（3 条） + test_re_search_singlehop_*。
  re_search_node 自 T25 起已从主图拔除（函数体仍在 qa.py，T47.6 才删）
  这几条是孤儿测试，提前清退。
运行方式：

    pytest tests/unit/test_qa_get_info_loop.py -v
"""
from __future__ import annotations

import sys

import pytest

from brain_base.config import GetInfoConfig
from brain_base.nodes.qa import (
    _url_priority_score,
    create_select_candidates_node,
)


# T25 删：test_qa_graph_compiles_with_loop_nodes。原测试检查 get_info_trigger /
# web_research / select_candidates / ingest_candidates / re_search 5 个老节点都在
# QaGraph 主图中；T25 外检从 judge 后送底改为 search 前预检，这 5 个节点从
# 主图删除（函数本身作为略代码保留在 nodes/qa.py，供 chunk 阶段后续决策是否彻底删）。
# 新的主图拓扑验证在 tests/unit/test_qa_graph_t25.py。


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


# T25 删：test_get_info_trigger_heuristic_paths。原测试验证
# create_get_info_trigger_node 启发式路径；T25 删了该函数工厂（外检触发判定转
# 移到 fanout_extract_dispatcher 的 5 重 gate，由 sub_needs_get_info 判定），
# 原测试不再适用。新 dispatcher 的 gate 测试在 tests/unit/test_qa_get_info.py。


# T47.5 删除：test_routing_anti_infinite_loop / test_after_barrier1_routes_by_sub_needs_get_info
# 两条路由测试被 T47.4 拔除（主图不再调用该路由）；after_judge /
# after_get_info_trigger / after_barrier1 函数本身仍在 conditional_logic.py，
# T47.6 一并清。

# ---------------------------------------------------------------------------
# T14：URL 优先级打分 + select_candidates 同类内重排序
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected_score,note",
    [
        # 100：GitHub README / wiki / docs（信息密度最高）
        ("https://github.com/owner/repo/blob/main/README.md", 100, "github blob README"),
        ("https://github.com/owner/repo/blob/main/readme.rst", 100, "github blob readme 大小写"),
        ("https://github.com/owner/repo/wiki/Installation", 100, "github wiki"),
        ("https://github.com/owner/repo/blob/main/docs/setup.md", 100, "github blob docs"),
        # 80：专门文档站
        ("https://requests.readthedocs.io/en/latest/", 80, "readthedocs"),
        ("https://example.gitbook.io/manual/", 80, "gitbook"),
        ("https://docs.python.org/3/library/os.html", 80, "docs.* 子域"),
        ("https://example.com/docs/getting-started", 80, "path 含 /docs/"),
        ("https://example.com/documentation/api", 80, "path 含 /documentation/"),
        # 60：GitHub 仓库根（无 README/wiki/blob 后缀）
        ("https://github.com/owner/repo", 60, "github 仓库根"),
        ("https://github.com/owner/repo/", 60, "github 仓库根带斜杠"),
        # 40：默认（普通文章 / 博客）
        ("https://example.com/blog/post-1", 40, "普通博客"),
        ("https://medium.com/some-author/some-article", 40, "medium 文章"),
        # 20：landing page（host 后无路径）
        ("https://openclaw.ai/", 20, "openclaw landing"),
        ("https://openclaw.ai", 20, "无尾斜杠 landing"),
        ("https://example.com", 20, "纯 host landing"),
    ],
    ids=lambda x: x if isinstance(x, str) and len(x) < 80 else "case",
)
def test_url_priority_score_layers(url: str, expected_score: int, note: str):
    """每档位 URL 模式必须返回正确的分数。"""
    actual = _url_priority_score(url)
    assert actual == expected_score, (
        f"{note}: URL={url} 应得 {expected_score} 分，实际 {actual}"
    )


def test_url_priority_score_handles_edge_cases():
    """边界：空字符串、非 http、奇怪格式都不应崩溃，给默认 40。"""
    assert _url_priority_score("") == 40, "空 URL 给默认 40，不崩溃"
    assert _url_priority_score("not-a-url") == 40, "非 URL 字符串给默认 40"
    assert _url_priority_score("ftp://example.com/file") == 40, "非 http 协议给默认 40"


def test_select_candidates_url_priority_reorders_within_category():
    """同 source_type 内：GitHub README 必须排在 landing page 前面。

    构造：3 个 official-doc，LLM 把 landing page 排在最前面（模拟 T12 e2e 真实场景）；
    断言：select_candidates 重排序后 README 在第 1 位、landing page 被截。
    """
    cfg = GetInfoConfig(max_official=2, max_community=0, max_total=2)
    select = create_select_candidates_node(cfg)
    state = {
        "get_info_candidates": [
            # LLM 给的顺序：landing page 在前
            {"url": "https://openclaw.ai/", "source_type": "official-doc", "title_hint": "landing"},
            {"url": "https://github.com/owner/repo/blob/main/README.md", "source_type": "official-doc", "title_hint": "README"},
            {"url": "https://github.com/owner/repo/wiki", "source_type": "official-doc", "title_hint": "wiki"},
        ]
    }
    out = select(state)
    urls = [t["url"] for t in out["ingest_targets"]]

    # README + wiki 都得 100 分，应排在前 2；landing page (20) 被 max_official=2 截掉
    assert "https://github.com/owner/repo/blob/main/README.md" in urls, (
        f"README (100 分) 必须入选，实际 {urls}"
    )
    assert "https://github.com/owner/repo/wiki" in urls, (
        f"wiki (100 分) 必须入选，实际 {urls}"
    )
    assert "https://openclaw.ai/" not in urls, (
        f"landing page (20 分) 必须被截掉，实际 {urls}"
    )


def test_select_candidates_priority_stable_for_equal_scores():
    """同分候选必须保留原顺序（稳定排序），避免破坏既有测试预期。"""
    cfg = GetInfoConfig(max_official=3, max_community=0, max_total=3)
    select = create_select_candidates_node(cfg)
    state = {
        "get_info_candidates": [
            # 3 个都是 docs.* 子域 → 同 80 分；保序后 a → b → c
            {"url": "https://docs.a.com/api", "source_type": "official-doc", "title_hint": "A"},
            {"url": "https://docs.b.com/guide", "source_type": "official-doc", "title_hint": "B"},
            {"url": "https://docs.c.com/intro", "source_type": "official-doc", "title_hint": "C"},
        ]
    }
    out = select(state)
    urls = [t["url"] for t in out["ingest_targets"]]
    assert urls == [
        "https://docs.a.com/api",
        "https://docs.b.com/guide",
        "https://docs.c.com/intro",
    ], f"同分候选必须保留原顺序，实际 {urls}"


def test_select_candidates_priority_real_openclaw_scenario():
    """复现 T12 e2e 真实场景：GitHub repo 应优于 landing page，避免营销页占据入库配额。"""
    cfg = GetInfoConfig(max_official=1, max_community=1, max_total=2)
    select = create_select_candidates_node(cfg)
    state = {
        "get_info_candidates": [
            # 模拟 LLM 的真实输出：landing 在前、README 在后
            {"url": "https://openclaw.ai/", "source_type": "community", "title_hint": "landing"},
            {"url": "https://github.com/openclaw/openclaw", "source_type": "official-doc", "title_hint": "github repo"},
            {"url": "https://medium.com/some-blog", "source_type": "community", "title_hint": "blog"},
        ]
    }
    out = select(state)
    urls = [t["url"] for t in out["ingest_targets"]]
    # GitHub repo 是 official-doc 唯一候选 → 必入选
    assert "https://github.com/openclaw/openclaw" in urls, urls
    # community 类：medium 博客 (40) 应优于 landing page (20)
    assert "https://medium.com/some-blog" in urls, urls
    assert "https://openclaw.ai/" not in urls, (
        f"landing page 在 community 类应被 medium 博客挤掉，实际 {urls}"
    )


# -----------------------------------------------------------------------------
# T16：Agent 化候选选择（Send fan-out + 并发 preview + 并发 LLM 评分）
# -----------------------------------------------------------------------------

import asyncio  # noqa: E402

from brain_base.agents.schemas import CandidatePreview, CandidateScore  # noqa: E402
from brain_base.nodes.get_info import (  # noqa: E402
    create_fan_out_to_preview,
    create_preview_score_one,
    merge_scores_node,
)
from brain_base.nodes.qa import _candidate_priority  # noqa: E402


# ---- T16-1：fan_out 路由 -----------------------------------------------------


def test_fan_out_returns_sends_for_unscored_candidates():
    """有未评分候选 + LLM 时：返回 list[Send]，长度等于未评分候选数。"""
    from langgraph.types import Send

    fan_out = create_fan_out_to_preview(llm=object())
    state = {
        "candidates": [
            {"url": "https://a.com"},
            {"url": "https://b.com"},
        ],
        "scored_candidates": [],
        "user_question": "q",
    }
    result = fan_out(state)
    assert isinstance(result, list), f"未评分候选应返回 list[Send]，实际 {type(result).__name__}"
    assert len(result) == 2
    assert all(isinstance(s, Send) for s in result)
    assert {s.arg["candidate"]["url"] for s in result} == {"https://a.com", "https://b.com"}
    assert all(s.arg["user_question"] == "q" for s in result)


def test_fan_out_skips_when_all_already_scored():
    """所有候选 url 都在 scored_candidates 里 → 返回 'merge_scores' 字符串路由。"""
    fan_out = create_fan_out_to_preview(llm=object())
    state = {
        "candidates": [{"url": "https://a.com"}],
        "scored_candidates": [{"url": "https://a.com", "priority_score": 50}],
        "user_question": "q",
    }
    assert fan_out(state) == "merge_scores"


def test_fan_out_skips_when_llm_none():
    """llm=None → 不调 preview_score_one，直接路由到 merge_scores。"""
    fan_out = create_fan_out_to_preview(llm=None)
    state = {
        "candidates": [{"url": "https://a.com"}],
        "scored_candidates": [],
        "user_question": "q",
    }
    assert fan_out(state) == "merge_scores"


def test_fan_out_filters_already_scored_in_partial_state():
    """混合场景：部分已评分、部分未评分 → 只对未评分候选 fan-out。"""
    from langgraph.types import Send

    fan_out = create_fan_out_to_preview(llm=object())
    state = {
        "candidates": [
            {"url": "https://a.com"},
            {"url": "https://b.com"},
            {"url": "https://c.com"},
        ],
        "scored_candidates": [{"url": "https://a.com", "priority_score": 60}],
        "user_question": "q",
    }
    result = fan_out(state)
    assert isinstance(result, list)
    assert len(result) == 2
    assert {s.arg["candidate"]["url"] for s in result} == {"https://b.com", "https://c.com"}


# ---- T16-2：preview_score_one async 节点 -------------------------------------


def test_preview_score_one_records_llm_score(monkeypatch):
    """preview 成功 + LLM 成功 → scored_candidates 含 priority_score 等 4 字段。"""
    async def fake_fetch(url, timeout=15.0):
        return CandidatePreview(
            url=url, fetched=True, title="OpenClaw Docs",
            heading="What is OpenClaw", preview_text="OpenClaw is ...",
        )

    def fake_invoke_structured(llm, schema, sys_prompt, user_prompt):
        # 假 LLM：高质量 docs 给 88 分
        return CandidateScore(
            priority_score=88, relevance_reason="官方文档站",
            is_docs=True, is_landing=False,
        )

    monkeypatch.setattr("brain_base.nodes.get_info.fetch_preview", fake_fetch)
    monkeypatch.setattr("brain_base.nodes.get_info.invoke_structured", fake_invoke_structured)

    node = create_preview_score_one(llm=object())
    out = asyncio.run(node({
        "candidate": {"url": "https://docs.openclaw.ai/", "source_type": "official-doc"},
        "user_question": "OpenClaw 怎么用",
    }))
    assert "scored_candidates" in out
    assert len(out["scored_candidates"]) == 1
    sc = out["scored_candidates"][0]
    assert sc["url"] == "https://docs.openclaw.ai/"
    assert sc["priority_score"] == 88
    assert sc["is_docs"] is True
    assert sc["is_landing"] is False
    assert sc["relevance_reason"] == "官方文档站"
    assert sc["preview"]["fetched"] is True


def test_preview_score_one_handles_fetch_failure(monkeypatch):
    """preview 失败 → priority_score=0 + relevance_reason 标 '抓取失败'，不抛错。"""
    async def fake_fetch(url, timeout=15.0):
        return CandidatePreview(url=url, fetched=False, error="net timeout")

    monkeypatch.setattr("brain_base.nodes.get_info.fetch_preview", fake_fetch)

    node = create_preview_score_one(llm=object())
    out = asyncio.run(node({
        "candidate": {"url": "https://broken.example.com/"},
        "user_question": "q",
    }))
    sc = out["scored_candidates"][0]
    assert sc["priority_score"] == 0
    assert "抓取失败" in sc["relevance_reason"]
    assert sc["is_docs"] is False
    assert sc["is_landing"] is False


def test_preview_score_one_handles_llm_failure(monkeypatch):
    """preview 成功 + LLM 抛错 → 不写 priority_score，留 score_error 给 select fallback。"""
    async def fake_fetch(url, timeout=15.0):
        return CandidatePreview(url=url, fetched=True, title="t", heading="h", preview_text="p")

    def fake_invoke_structured(llm, schema, sys_prompt, user_prompt):
        raise RuntimeError("LLM provider down")

    monkeypatch.setattr("brain_base.nodes.get_info.fetch_preview", fake_fetch)
    monkeypatch.setattr("brain_base.nodes.get_info.invoke_structured", fake_invoke_structured)

    node = create_preview_score_one(llm=object())
    out = asyncio.run(node({
        "candidate": {"url": "https://x.com/"},
        "user_question": "q",
    }))
    sc = out["scored_candidates"][0]
    assert "priority_score" not in sc, f"LLM 失败时不应写 priority_score（让 select 走 T14 fallback）"
    assert "score_error" in sc
    assert "LLM provider down" in sc["score_error"]


# ---- T16-3：merge_scores ----------------------------------------------------


def test_merge_scores_writes_back_llm_fields():
    """scored_candidates 的 LLM 字段按 url 合并回 candidates 同位置。"""
    state = {
        "candidates": [
            {"url": "https://a.com", "title_hint": "A"},
            {"url": "https://b.com", "title_hint": "B"},
        ],
        "scored_candidates": [
            {
                "url": "https://a.com", "priority_score": 88,
                "relevance_reason": "docs", "is_docs": True, "is_landing": False,
                "preview": {"fetched": True},
            },
            # b.com 没评分
        ],
    }
    out = merge_scores_node(state)
    by_url = {c["url"]: c for c in out["candidates"]}
    assert by_url["https://a.com"]["priority_score"] == 88
    assert by_url["https://a.com"]["is_docs"] is True
    assert by_url["https://a.com"]["title_hint"] == "A"  # 原字段保留
    assert "priority_score" not in by_url["https://b.com"], "未评分候选不应被注入 priority_score"


# ---- T16-4：select_candidates 用 LLM priority_score 排序 ---------------------


def test_select_candidates_uses_llm_priority_score_when_available():
    """有 LLM priority_score 时按 LLM 分排序，**忽略 T14 静态分**。

    构造：landing page (LLM=85) vs GitHub README (LLM=30) → landing 应排前。
    （现实中不会这样，但用极端值证明排序键已切换）
    """
    cfg = GetInfoConfig(max_official=2, max_community=2, max_total=2)
    select = create_select_candidates_node(cfg)
    state = {
        "get_info_candidates": [
            {
                "url": "https://github.com/x/y/blob/main/README.md",
                "source_type": "official-doc",
                "priority_score": 30,
            },
            {
                "url": "https://example.com/",  # T14 给 20 分（landing）
                "source_type": "official-doc",
                "priority_score": 85,
            },
        ]
    }
    out = select(state)
    urls = [t["url"] for t in out["ingest_targets"]]
    assert urls[0] == "https://example.com/", (
        f"LLM=85 的 example.com 必须排在 LLM=30 的 github README 之前；实际 {urls}"
    )


def test_select_candidates_falls_back_to_t14_when_llm_score_missing():
    """priority_score 缺失时降级到 T14 静态分（向后兼容 + LLM 失败容错）。"""
    cfg = GetInfoConfig(max_official=2, max_community=2, max_total=2)
    select = create_select_candidates_node(cfg)
    state = {
        "get_info_candidates": [
            # 都不带 priority_score → 走 T14：README=100 > landing=20
            {"url": "https://github.com/x/y/blob/main/README.md", "source_type": "official-doc"},
            {"url": "https://example.com/", "source_type": "official-doc"},
        ]
    }
    out = select(state)
    urls = [t["url"] for t in out["ingest_targets"]]
    assert urls[0] == "https://github.com/x/y/blob/main/README.md", (
        f"无 LLM 分时走 T14 静态分 README(100) > landing(20)；实际 {urls}"
    )


def test_candidate_priority_treats_zero_as_valid_score():
    """priority_score=0（fetch 失败的候选）必须当 0 处理，不要被 truthy 当作缺失。"""
    c = {"url": "https://github.com/x/y/blob/main/README.md", "priority_score": 0}
    # 应严格返回 0，而不是 fallback 到 T14 的 100
    assert _candidate_priority(c) == 0, "priority_score=0 是有效分数（fetch 失败标记），不应被 fallback"


# T47.5 删除：T18 re_search_node 补丁测试（test_re_search_*）4 条。
# re_search_node 自 T25 起已从主图拔除（函数体仍在 qa.py，T47.6 才删）
# 这几条是孤儿测试，提前清退。

# =============================================================================
# T19：T12 多跳路径遗留 bug 修复（B1 judge_reason 泄漏 + B2 跨组 evidence 污染）
# =============================================================================


# ---- T19-B1：judge_node 降级路径必须写全 4 字段 ------------------------------


# T27 删：3 个被废弃的 judge 降级路径测试
# - test_judge_llm_none_multihop_all_sufficient_writes_4_fields：多跳 + llm=None 降级路径已删
# - test_judge_llm_none_singlehop_writes_4_fields：单链路 + llm=None 降级路径已删
# - test_judge_llm_exception_writes_4_fields_with_reason_containing_exc_type：
#   LLM 异常 try/except 兜底已删，现在直接上拋
# 多跳模式的 evidence_count=0 → 整体 unsufficient 业务规则保留，
# 由 test_judge_node_multi_subquery_missing_evidence 等其他测试覆盖。


# ---- T19-B2：_dedup_evidence_by_chunk_id helper -----------------------------


def test_dedup_helper_keeps_highest_score_per_chunk_id():
    """helper 级别：同 chunk_id 保留最高 score 的副本，顺序按首次出现。"""
    from brain_base.nodes.qa import _dedup_evidence_by_chunk_id

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
    from brain_base.nodes.qa import _dedup_evidence_by_chunk_id

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



# T23 删除起点：下面原有 T19-B2 跨组 dedup + T22 lexical 强约束共 11 个测试，
# 均依赖已删除的 create_subquery_fanout_node / search_node / lexical_* 字段。
# T24 完成 fanout_search 后补回跨子问题 dedup 测试；grep AND gate 现归
# tests/unit/test_lexical_grep.py 与 tests/unit/test_qa_prep.py 覆盖。

# -----------------------------------------------------------------------------
# 脚本入口（保留以便不装 pytest 时也能跑）
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
