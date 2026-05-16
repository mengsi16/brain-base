# -*- coding: utf-8 -*-
"""T25-B qa_get_info 5 节点测试。

mock 三层（playwright / subprocess Readability / LLM），不真起任何外部进程。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from brain_base.config import GetInfoConfig
from brain_base.nodes.qa_get_info import (
    _fetch_extract_user_prompt,
    barrier_extract_node,
    create_fetch_extract_one,
    fanout_extract_dispatcher,
    merge_search_keywords_node,
    search_web_dual_node,
)


# T29: web_fetcher.fetch_page 迁移为 async coroutine。测试原本用 sync lambda mock
# (lambda url, **_: {...})，被 await 会报 ``object dict can't be used in 'await' expression``。
# 用 ``_as_async`` helper 把 sync 返值包成 async coroutine。
def _as_async(value_or_fn):
    """把同步返值 / 同步函数转成 async coroutine，匹配被 await 的 mock target。

    用法：
        monkeypatch.setattr("...fetch_page", _as_async({"html": "..."}))
        monkeypatch.setattr("...fetch_page", _as_async(lambda url, **_: {"html": "..."}))
    """
    if callable(value_or_fn):
        async def _fn(*args, **kwargs):
            return value_or_fn(*args, **kwargs)
        return _fn

    async def _fn(*args, **kwargs):
        return value_or_fn
    return _fn


# ===========================================================================
# Node 1: merge_search_keywords
# ===========================================================================


def test_merge_basic_lexical_queries_pass_through():
    """每个子问题的 lexical_query 直接作 SERP query（T30 改造）。

    原 sub_grep_keywords 是 list of keywords list 需 join；新 sub_lexical_queries
    已是 LLM 输出的短自然语言串（SERP 友好），无需再 join。
    """
    state = {"sub_lexical_queries": ["RAGFlow 启动 步骤", "openclaw 卸载"]}
    out = merge_search_keywords_node(state)
    assert out == {"search_keywords": ["RAGFlow 启动 步骤", "openclaw 卸载"]}


def test_merge_dedup_at_query_level():
    """两个子问题 lexical_query 完全相同 → 去重后 1 条 query（保序）。"""
    state = {"sub_lexical_queries": ["a b", "a b"]}
    out = merge_search_keywords_node(state)
    assert out == {"search_keywords": ["a b"]}


def test_merge_skips_empty_subquery():
    """空 / 纯空白 lexical_query 跳过，不影响其他子问题。"""
    state = {"sub_lexical_queries": ["", "a b", "   "]}
    out = merge_search_keywords_node(state)
    assert out == {"search_keywords": ["a b"]}


def test_merge_no_input_field():
    """sub_lexical_queries 缺失 → 空列表。"""
    out = merge_search_keywords_node({})
    assert out == {"search_keywords": []}


# ===========================================================================
# Node 2: search_web_dual (async)
# ===========================================================================


def test_search_web_dual_aggregates_engines_and_queries():
    """2 query × 2 page × 2 engine 全并行 → URL 去重 + from_engines/from_queries 累加。"""

    async def fake_google(q, num=10, page=1):
        return [{"url": "https://x.io", "title": "X", "snippet": "x"}]

    async def fake_bing(q, num=10, page=1):
        return [{"url": "https://x.io", "title": "X", "snippet": "x"}]

    with patch(
        "brain_base.nodes.qa_get_info.search_google", side_effect=fake_google
    ), patch("brain_base.nodes.qa_get_info.search_bing", side_effect=fake_bing):
        state = {
            "search_keywords": ["q0", "q1"],
            # T29 节流：单测不该真 sleep 10-20s，间隔设 0 关闭节流质量外拖试讯
            "get_info_config": GetInfoConfig(
                search_pages_per_engine=2,
                serp_min_interval_sec=0.0,
                serp_max_interval_sec=0.0,
            ),
        }
        out = asyncio.run(search_web_dual_node(state))

    urls = out["serp_urls"]
    assert len(urls) == 1
    entry = urls[0]
    assert entry["url"] == "https://x.io"
    assert set(entry["from_engines"]) == {"google", "bing"}
    assert set(entry["from_queries"]) == {0, 1}


def test_search_web_dual_one_engine_fail_does_not_block_other():
    """google 抛错 → bing 结果继续；不阻断。"""

    async def fake_google_fail(q, num=10, page=1):
        raise RuntimeError("google blocked")

    async def fake_bing_ok(q, num=10, page=1):
        return [{"url": "https://b.io", "title": "B", "snippet": "b"}]

    with patch(
        "brain_base.nodes.qa_get_info.search_google", side_effect=fake_google_fail
    ), patch("brain_base.nodes.qa_get_info.search_bing", side_effect=fake_bing_ok):
        state = {
            "search_keywords": ["q"],
            "get_info_config": GetInfoConfig(
                search_pages_per_engine=1,
                serp_min_interval_sec=0.0,
                serp_max_interval_sec=0.0,
            ),
        }
        out = asyncio.run(search_web_dual_node(state))

    assert len(out["serp_urls"]) == 1
    assert out["serp_urls"][0]["url"] == "https://b.io"
    assert out["serp_urls"][0]["from_engines"] == ["bing"]


def test_search_web_dual_empty_keywords_short_circuits():
    """search_keywords=[] → 直接返 serp_urls=[]，不调任何引擎。"""
    out = asyncio.run(search_web_dual_node({"search_keywords": []}))
    assert out == {"serp_urls": []}


def test_search_web_dual_all_engines_fail_returns_empty():
    """全部抛错 → serp_urls=[]，让 dispatcher 短路。"""

    async def fail(q, num=10, page=1):
        raise RuntimeError("blocked")

    with patch(
        "brain_base.nodes.qa_get_info.search_google", side_effect=fail
    ), patch("brain_base.nodes.qa_get_info.search_bing", side_effect=fail):
        state = {
            "search_keywords": ["q"],
            "get_info_config": GetInfoConfig(
                search_pages_per_engine=1,
                serp_min_interval_sec=0.0,
                serp_max_interval_sec=0.0,
            ),
        }
        out = asyncio.run(search_web_dual_node(state))
    assert out == {"serp_urls": []}


# ===========================================================================
# Node 3: fanout_extract_dispatcher（5 重 gate）
# ===========================================================================


def _base_dispatcher_state() -> dict:
    """正常 state：能派发 Send 的最小集。"""
    return {
        "get_info_config": GetInfoConfig(),
        "sub_needs_get_info": [True],
        "get_info_attempted": False,
        "infra": {"playwright_available": True},
        "serp_urls": [
            {
                "url": "https://x.io",
                "title": "X",
                "snippet": "x",
                "from_engines": ["google"],
                "from_queries": [0],
            }
        ],
        "question": "Q",
        "sub_questions": ["Q"],
    }


def test_dispatcher_normal_sends_n_with_t24_context():
    """正常 state → list[Send]，每个含 question + sub_questions（T24 上下文继承）。"""
    from langgraph.types import Send

    s = _base_dispatcher_state()
    s["serp_urls"] = [
        {
            "url": "https://a.io",
            "title": "A",
            "snippet": "a",
            "from_engines": ["google"],
            "from_queries": [0],
        },
        {
            "url": "https://b.io",
            "title": "B",
            "snippet": "b",
            "from_engines": ["bing"],
            "from_queries": [1],
        },
    ]
    s["question"] = "RAGFlow 怎么启动？openclaw 怎么卸载？"
    s["sub_questions"] = ["RAGFlow 启动", "openclaw 卸载"]
    out = fanout_extract_dispatcher(s)
    assert isinstance(out, list)
    assert len(out) == 2
    for send in out:
        assert isinstance(send, Send)
        assert send.node == "fetch_extract_one"
        assert send.arg["question"] == "RAGFlow 怎么启动？openclaw 怎么卸载？"
        assert send.arg["sub_questions"] == ["RAGFlow 启动", "openclaw 卸载"]


def test_dispatcher_gate_disabled():
    s = _base_dispatcher_state()
    s["get_info_config"] = GetInfoConfig(enable=False)
    assert fanout_extract_dispatcher(s) == "barrier_extract"


def test_dispatcher_gate_no_subneeds():
    """T23 grep 全命中 sub_needs_get_info 全 False → 短路（替代旧 trigger 节点）。"""
    s = _base_dispatcher_state()
    s["sub_needs_get_info"] = [False, False]
    assert fanout_extract_dispatcher(s) == "barrier_extract"


def test_dispatcher_gate_attempted_prevents_loop():
    """get_info_attempted=True → 防死循环。"""
    s = _base_dispatcher_state()
    s["get_info_attempted"] = True
    assert fanout_extract_dispatcher(s) == "barrier_extract"


def test_dispatcher_gate_no_playwright():
    s = _base_dispatcher_state()
    s["infra"] = {"playwright_available": False}
    assert fanout_extract_dispatcher(s) == "barrier_extract"


def test_dispatcher_gate_empty_serp():
    s = _base_dispatcher_state()
    s["serp_urls"] = []
    assert fanout_extract_dispatcher(s) == "barrier_extract"


# ===========================================================================
# Node 4: fetch_extract_one (async)
# ===========================================================================


HIGH_QUALITY_MD = """# RAGFlow 部署文档

RAGFlow 是一个开源 RAG 引擎。完整启动步骤：
1. docker-compose up -d 启动服务
2. 访问 http://localhost:9380 进入 Web UI
3. 上传 PDF 触发解析

## 卸载

执行 docker-compose down -v 清理容器与卷。
"""

LOW_QUALITY_MD = """# 顶级广告促销页

立即购买 50% off！点击查看更多优惠！只剩最后 3 个名额！
"""


def _send_arg(url: str = "https://x.io", *, question: str = "Q", sub_questions=None):
    return {
        "url": url,
        "title": "X title",
        "snippet": "x snippet",
        "from_engines": ["google"],
        "from_queries": [0],
        "question": question,
        "sub_questions": sub_questions or [question],
    }


def _hash_miss(*_a, **_kw):
    """hash_lookup mock：默认 miss，走 LLM 评估主路径。"""
    return {"status": "miss", "matches": [], "query_sha256": "" * 64, "raw_dir": ""}


def _hash_hit(doc_id: str = "existing-doc-001"):
    """hash_lookup mock 生成器：hit，含 1 个匹配 doc。"""

    def _mock(sha256_hex, **_kw):
        return {
            "status": "hit",
            "query_sha256": sha256_hex,
            "match_count": 1,
            "matches": [
                {
                    "doc_id": doc_id,
                    "raw_path": f"data/raw/{doc_id}.md",
                    "declared_sha256": sha256_hex,
                    "actual_sha256": sha256_hex,
                }
            ],
            "raw_dir": "data/raw",
        }

    return _mock


def test_fetch_extract_one_whether_in_true(real_llm, monkeypatch):
    """真调 LLM：高质 markdown → whether_in=True + candidate 完整。"""
    monkeypatch.setattr(
        "brain_base.nodes.qa_get_info.fetch_page",
        _as_async(lambda url, **_: {"html": "<html>x</html>", "title": "T", "text": "t"}),
    )
    monkeypatch.setattr(
        "brain_base.nodes.qa_get_info.convert_html_to_markdown_readability",
        lambda html, **_: HIGH_QUALITY_MD,
    )
    monkeypatch.setattr("brain_base.nodes.qa_get_info.hash_lookup", _hash_miss)

    node = create_fetch_extract_one(real_llm, GetInfoConfig())
    out = asyncio.run(node(_send_arg()))

    er = out["extract_results"]
    assert len(er) == 1
    c = er[0]
    assert c["url"] == "https://x.io"
    assert c["whether_in"] is True
    assert c["score"] >= 40
    assert c["type"] in {"official-doc", "community"}
    assert c["markdown"] == HIGH_QUALITY_MD
    assert c["from_engines"] == ["google"]
    assert "fetched_at" in c
    # miss 路径：candidate 带 content_sha256
    assert len(c["content_sha256"]) == 64  # SHA-256 hex


def test_fetch_extract_one_whether_in_false(real_llm, monkeypatch):
    """真调 LLM：低质 markdown → whether_in=False。"""
    monkeypatch.setattr(
        "brain_base.nodes.qa_get_info.fetch_page",
        _as_async(lambda url, **_: {"html": "<html>x</html>", "title": "T"}),
    )
    monkeypatch.setattr(
        "brain_base.nodes.qa_get_info.convert_html_to_markdown_readability",
        lambda html, **_: LOW_QUALITY_MD,
    )
    monkeypatch.setattr("brain_base.nodes.qa_get_info.hash_lookup", _hash_miss)
    node = create_fetch_extract_one(real_llm, GetInfoConfig())
    out = asyncio.run(node(_send_arg()))
    c = out["extract_results"][0]
    assert c["whether_in"] is False
    assert c["type"] == "discard"


def test_fetch_extract_one_fetch_failure_writes_error(monkeypatch):
    """playwright fetch 抛错 → extract_results 写 error，不阻断其他 Send。"""

    async def fail_fetch(url, **_):
        raise RuntimeError("playwright down")

    monkeypatch.setattr("brain_base.nodes.qa_get_info.fetch_page", fail_fetch)

    node = create_fetch_extract_one(None, GetInfoConfig())
    out = asyncio.run(node(_send_arg()))
    c = out["extract_results"][0]
    assert "error" in c
    assert "playwright" in c["error"]
    assert c["whether_in"] is False


def test_fetch_extract_one_readability_falls_back_to_mineru(real_llm, monkeypatch):
    """readability 抛错 → fallback MinerU 成功 → 真 LLM 继续评估。"""
    monkeypatch.setattr(
        "brain_base.nodes.qa_get_info.fetch_page",
        _as_async(lambda url, **_: {"html": "<html>x</html>"}),
    )

    def readability_fail(html, **_):
        raise RuntimeError("readability fail")

    monkeypatch.setattr(
        "brain_base.nodes.qa_get_info.convert_html_to_markdown_readability",
        readability_fail,
    )
    monkeypatch.setattr(
        "brain_base.nodes.qa_get_info.convert_html_to_markdown",
        lambda html, **_: HIGH_QUALITY_MD,
    )
    monkeypatch.setattr("brain_base.nodes.qa_get_info.hash_lookup", _hash_miss)
    node = create_fetch_extract_one(real_llm, GetInfoConfig())
    out = asyncio.run(node(_send_arg()))
    c = out["extract_results"][0]
    assert c["markdown"] == HIGH_QUALITY_MD


def test_fetch_extract_one_empty_html_writes_error(monkeypatch):
    """fetch 返回空 html → 写 empty html 错误，不调 LLM。"""
    monkeypatch.setattr(
        "brain_base.nodes.qa_get_info.fetch_page",
        _as_async(lambda url, **_: {"html": "", "title": ""}),
    )
    node = create_fetch_extract_one(None, GetInfoConfig())
    out = asyncio.run(node(_send_arg()))
    c = out["extract_results"][0]
    assert "error" in c
    assert "empty html" in c["error"]


# ===========================================================================
# T24 回补：SHA-256 内容指纹 + raw 目录 dedup
# ===========================================================================


def test_fetch_extract_one_hash_hit_short_circuits(monkeypatch):
    """hash_lookup hit → short-circuit：不调 LLM，不写 extract_results（candidate 直接丢弃）。

    设计依据：命中表示内容已在 Milvus 里，QA 后续 fanout_search 阶段天然召回；
    让 candidate 继续走下游只能产生“更新 fetched_at”这种没人消费的副作用。
    """
    monkeypatch.setattr(
        "brain_base.nodes.qa_get_info.fetch_page",
        _as_async(lambda url, **_: {"html": "<html>existing</html>", "title": "E"}),
    )
    monkeypatch.setattr(
        "brain_base.nodes.qa_get_info.convert_html_to_markdown_readability",
        lambda html, **_: "# E\n\nexisting body",
    )
    monkeypatch.setattr(
        "brain_base.nodes.qa_get_info.hash_lookup",
        _hash_hit(doc_id="old-doc-42"),
    )

    # LLM 一旦被调用就报错，以验证“跳过 LLM”严格成立
    class _ExplodingLLM:
        def with_structured_output(self, *a, **kw):
            raise AssertionError("hit 路径不应调 LLM")

        def invoke(self, *_):
            raise AssertionError("hit 路径不应调 LLM")

    node = create_fetch_extract_one(_ExplodingLLM(), GetInfoConfig())
    out = asyncio.run(node(_send_arg()))
    # short-circuit：不写任何 candidate，extract_results 为空列表
    assert out == {"extract_results": []}


def test_fetch_extract_one_content_sha256_is_stable(real_llm, monkeypatch):
    """同一 markdown 两次调用 → content_sha256 一致（验证 hash 调用参数对齐）。"""
    monkeypatch.setattr(
        "brain_base.nodes.qa_get_info.fetch_page",
        _as_async(lambda url, **_: {"html": "<html>x</html>", "title": "T"}),
    )
    monkeypatch.setattr(
        "brain_base.nodes.qa_get_info.convert_html_to_markdown_readability",
        lambda html, **_: HIGH_QUALITY_MD,
    )
    monkeypatch.setattr("brain_base.nodes.qa_get_info.hash_lookup", _hash_miss)

    node = create_fetch_extract_one(real_llm, GetInfoConfig())
    out1 = asyncio.run(node(_send_arg()))
    out2 = asyncio.run(node(_send_arg(url="https://other.io")))
    h1 = out1["extract_results"][0]["content_sha256"]
    h2 = out2["extract_results"][0]["content_sha256"]
    # 同样的 markdown 产生同样的 hash（与 url 无关，只与内容有关）
    assert h1 == h2 != ""


# ===========================================================================
# user_prompt 拼装（T24 上下文继承）
# ===========================================================================


def test_user_prompt_multi_hop_includes_subquestion_list():
    """多跳模式：含原 question + 子问题列表 [s0]/[s1] + SERP 元数据。"""
    p = _fetch_extract_user_prompt(
        question="RAGFlow 怎么启动？openclaw 怎么卸载？",
        sub_questions=["RAGFlow 服务启动的步骤", "openclaw 彻底卸载的方法"],
        title="T",
        snippet="S",
        from_engines=["google", "bing"],
        from_queries=[0, 1],
        markdown="markdown body",
    )
    assert "用户原始问题：RAGFlow 怎么启动？" in p
    assert "[s0] RAGFlow 服务启动的步骤" in p
    assert "[s1] openclaw 彻底卸载的方法" in p
    assert "google, bing" in p
    assert "完整 markdown 内容" in p
    assert "markdown body" in p


def test_user_prompt_single_hop_simplified():
    """单跳：用户问题简洁格式，不塞子问题列表段落。"""
    p = _fetch_extract_user_prompt(
        question="RAGFlow 是什么",
        sub_questions=["RAGFlow 是什么"],
        title="T",
        snippet="S",
        from_engines=["bing"],
        from_queries=[0],
        markdown="md",
    )
    assert "用户问题：RAGFlow 是什么" in p
    assert "子问题列表" not in p
    assert "[s0]" not in p


def test_user_prompt_no_sub_questions_uses_single_hop():
    """空 sub_questions → 走单跳格式（极端兜底）。"""
    p = _fetch_extract_user_prompt(
        question="x",
        sub_questions=[],
        title="",
        snippet="",
        from_engines=[],
        from_queries=[],
        markdown="m",
    )
    assert "子问题列表" not in p


# ===========================================================================
# Node 5: barrier_extract
# ===========================================================================


def test_barrier_filters_whether_in_false():
    state = {
        "extract_results": [
            {"url": "a", "whether_in": True, "score": 80},
            {"url": "b", "whether_in": False, "score": 20},
        ]
    }
    out = barrier_extract_node(state)
    assert len(out["get_info_candidates"]) == 1
    assert out["get_info_candidates"][0]["url"] == "a"
    assert out["get_info_attempted"] is True


def test_barrier_sorts_by_score_descending():
    state = {
        "extract_results": [
            {"url": "a", "whether_in": True, "score": 50},
            {"url": "b", "whether_in": True, "score": 90},
            {"url": "c", "whether_in": True, "score": 70},
        ]
    }
    out = barrier_extract_node(state)
    urls = [c["url"] for c in out["get_info_candidates"]]
    assert urls == ["b", "c", "a"]


def test_barrier_aggregates_errors():
    """error 字段非空的不进 candidates，但聚合到 extract_errors。"""
    state = {
        "extract_results": [
            {"url": "https://a.io", "error": "fetch fail", "whether_in": False},
            {"url": "https://b.io", "whether_in": True, "score": 80},
            {"url": "https://c.io", "error": "llm fail", "whether_in": False},
        ]
    }
    out = barrier_extract_node(state)
    assert len(out["get_info_candidates"]) == 1
    assert "fetch fail" in out["extract_errors"][0]
    assert "llm fail" in out["extract_errors"][1]


def test_barrier_empty_input():
    out = barrier_extract_node({"extract_results": []})
    assert out == {
        "get_info_candidates": [],
        "extract_errors": [],
        "get_info_attempted": True,
    }
