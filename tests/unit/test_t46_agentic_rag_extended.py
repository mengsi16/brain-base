# -*- coding: utf-8 -*-
"""T46 Agentic-RAG 补充单元测试。

覆盖静态检查发现的缺失项：
- classify_plan LLM fallback 路径
- hop_planner LLM 路径 + 模板替换
- tool_executor dispatch / async sync 包装 / 失败隔离
- fetch_user_urls raw_text 短路 + _fetch_and_evaluate fallback
- QaState 12 个新字段默认值
- Graph 三路分流 dispatch
- merge_hop_evidence score 排序
"""

from __future__ import annotations

import asyncio
import pytest


# ---------------------------------------------------------------------------
# classify_plan LLM fallback 路径
# ---------------------------------------------------------------------------


class TestClassifyPlanLLMPath:
    """classify_plan 节点 LLM fallback 路径。"""

    def test_iterative_plan_from_llm(self, monkeypatch):
        from brain_base.nodes.qa import create_classify_plan_node
        from brain_base.agents.schemas import RetrievalPlan

        def fake_invoke(llm, schema, sys_prompt, user_prompt):
            assert schema is RetrievalPlan
            return RetrievalPlan(
                plan_type="iterative",
                max_hops=2,
                initial_goal="找出郎朗的导师",
                chain_reasoning="链式问题需顺序查找",
            )

        monkeypatch.setattr(
            "brain_base.nodes.qa.invoke_structured", fake_invoke
        )

        node = create_classify_plan_node(object())
        out = node({
            "user_urls": [],
            "sub_questions": ["郎朗的导师的导师是谁？"],
            "decomposition_needed": False,
            "normalized_query": "郎朗的导师的导师是谁？",
            "entities": ["郎朗"],
            "time_sensitive": False,
        })

        assert out["plan_type"] == "iterative"
        assert out["max_hops"] == 2
        assert out["initial_goal"] == "找出郎朗的导师"
        assert out["pending_goals"] == ["找出郎朗的导师"]

    def test_iterative_fallback_to_parallel_when_empty_goal(self, monkeypatch):
        """iterative 但 initial_goal 为空 → fallback parallel。"""
        from brain_base.nodes.qa import create_classify_plan_node
        from brain_base.agents.schemas import RetrievalPlan

        def fake_invoke(llm, schema, sys_prompt, user_prompt):
            return RetrievalPlan(
                plan_type="iterative",
                max_hops=3,
                initial_goal="",
                chain_reasoning="",
            )

        monkeypatch.setattr(
            "brain_base.nodes.qa.invoke_structured", fake_invoke
        )

        node = create_classify_plan_node(object())
        out = node({
            "user_urls": [],
            "sub_questions": ["test"],
            "decomposition_needed": False,
            "normalized_query": "test",
            "entities": [],
            "time_sensitive": False,
        })

        assert out["plan_type"] == "parallel"
        assert out["pending_goals"] == []


# ---------------------------------------------------------------------------
# hop_planner LLM 路径
# ---------------------------------------------------------------------------


class TestHopPlanner:
    """hop_planner 节点 LLM 路径。"""

    def test_basic_hop_plan(self, monkeypatch):
        from brain_base.nodes.qa_hop import create_hop_planner
        from brain_base.agents.schemas import HopPlan

        def fake_invoke(llm, schema, sys_prompt, user_prompt):
            assert schema is HopPlan
            return HopPlan(
                goal="找出郎朗的导师",
                tool_name="web_search",
                tool_args={"query": "郎朗 导师"},
                stop_entity="郎朗的导师",
                next_goals=["查找{郎朗的导师}的导师"],
                reason="需要先知道郎朗的导师",
            )

        monkeypatch.setattr(
            "brain_base.nodes.qa_hop.invoke_structured", fake_invoke
        )

        node = create_hop_planner(object())
        out = node({
            "pending_goals": ["找出郎朗的导师"],
            "hops": [],
            "resolved_entities": {},
            "normalized_query": "郎朗的导师的导师是谁？",
        })

        sel = out["current_tool_selection"]
        assert sel["goal"] == "找出郎朗的导师"
        assert sel["tool_name"] == "web_search"
        assert sel["tool_args"] == {"query": "郎朗 导师"}
        assert sel["stop_entity"] == "郎朗的导师"
        assert sel["next_goals"] == ["查找{郎朗的导师}的导师"]
        assert sel["reason"] == "需要先知道郎朗的导师"

    def test_resolved_entity_substitution(self, monkeypatch):
        """goal 中的 {resolved_entity} 模板应在 user_prompt 中替换。"""
        from brain_base.nodes.qa_hop import create_hop_planner
        from brain_base.agents.schemas import HopPlan

        captured_prompt = {"user": ""}

        def fake_invoke(llm, schema, sys_prompt, user_prompt):
            captured_prompt["user"] = user_prompt
            return HopPlan(
                goal="查找{郎朗的导师}的导师",
                tool_name="web_search",
                tool_args={"query": "但昭义 导师"},
                stop_entity="但昭义的导师",
                next_goals=[],
                reason="第二跳",
            )

        monkeypatch.setattr(
            "brain_base.nodes.qa_hop.invoke_structured", fake_invoke
        )

        node = create_hop_planner(object())
        node({
            "pending_goals": ["查找{郎朗的导师}的导师"],
            "hops": [],
            "resolved_entities": {"郎朗的导师": "但昭义"},
            "normalized_query": "郎朗的导师的导师是谁？",
        })

        # user_prompt 中 goal 应替换为 "查找但昭义的导师"
        assert "查找但昭义的导师" in captured_prompt["user"]

    def test_empty_pending_goals_returns_empty(self):
        """pending_goals 为空时返回空 selection，should_continue_hopping 会退出。"""
        from brain_base.nodes.qa_hop import create_hop_planner

        node = create_hop_planner(object())
        out = node({
            "pending_goals": [],
            "hops": [],
            "resolved_entities": {},
            "normalized_query": "test",
        })

        assert out["current_tool_selection"] == {}


# ---------------------------------------------------------------------------
# tool_executor dispatch / async sync 包装 / 失败隔离
# ---------------------------------------------------------------------------


class TestToolExecutor:
    """tool_executor 节点 dispatch + LLM 提取 + 失败隔离。"""

    @staticmethod
    def _fake_registry(**overrides):
        """构造一个非 frozen 的 fake TOOL_REGISTRY dict，避免修改原 dataclass。

        override 值可以是 callable（自动包成 SimpleNamespace，推断 is_async）
        或任意对象（直接替换）。
        """
        import asyncio
        from types import SimpleNamespace
        base = {
            "web_search": SimpleNamespace(is_async=True, fn=lambda *a, **kw: {}),
            "fetch_url": SimpleNamespace(is_async=True, fn=lambda *a, **kw: {}),
            "raw_text": SimpleNamespace(is_async=False, fn=lambda *a, **kw: {}),
            "local_search": SimpleNamespace(is_async=False, fn=lambda *a, **kw: {}),
        }
        for k, v in overrides.items():
            if callable(v):
                is_async = asyncio.iscoroutinefunction(v)
                base[k] = SimpleNamespace(is_async=is_async, fn=v)
            else:
                base[k] = v
        return base

    def test_async_tool_dispatch(self, monkeypatch):
        """web_search（async 工具）应被直接 await，不走 to_thread。"""
        from brain_base.nodes.qa_hop import create_tool_executor
        from brain_base.agents.schemas import HopObservation

        call_log = {"direct": 0}

        async def fake_web_search(tool_args, llm, cfg):
            call_log["direct"] += 1
            return {
                "markdown": "# result\n郎朗的导师是但昭义",
                "source_url": "https://example.com",
                "title": "Example",
            }

        monkeypatch.setattr(
            "brain_base.nodes.qa_hop.TOOL_REGISTRY",
            self._fake_registry(web_search=fake_web_search),
        )

        def fake_invoke(llm, schema, sys_prompt, user_prompt):
            return HopObservation(
                resolved_entity="但昭义",
                evidence_summary="导师是但昭义",
                confidence=0.95,
            )

        monkeypatch.setattr(
            "brain_base.nodes.qa_hop.invoke_structured", fake_invoke
        )

        node = create_tool_executor(object(), object())
        out = asyncio.run(node({
            "current_tool_selection": {
                "tool_name": "web_search",
                "tool_args": {"query": "郎朗 导师"},
                "stop_entity": "郎朗的导师",
            },
        }))

        assert call_log["direct"] == 1
        assert out["current_tool_result"]["resolved_entity"] == "但昭义"
        assert out["current_tool_result"]["evidence"] == "导师是但昭义"

    def test_sync_tool_via_to_thread(self, monkeypatch):
        """raw_text（sync 工具）应被 asyncio.to_thread 包装。"""
        from brain_base.nodes.qa_hop import create_tool_executor
        from brain_base.agents.schemas import HopObservation

        call_log = {"in_thread": False}

        def fake_raw_text(tool_args, llm, cfg):
            call_log["in_thread"] = True
            return {
                "markdown": "# README\n...",
                "source_url": "https://github.com/test",
                "title": "Test",
            }

        monkeypatch.setattr(
            "brain_base.nodes.qa_hop.TOOL_REGISTRY",
            self._fake_registry(raw_text=fake_raw_text),
        )

        def fake_invoke(llm, schema, sys_prompt, user_prompt):
            return HopObservation(
                resolved_entity="",
                evidence_summary="GitHub README 内容",
                confidence=0.9,
            )

        monkeypatch.setattr(
            "brain_base.nodes.qa_hop.invoke_structured", fake_invoke
        )

        node = create_tool_executor(object(), object())
        out = asyncio.run(node({
            "current_tool_selection": {
                "tool_name": "raw_text",
                "tool_args": {"url": "https://github.com/test"},
                "stop_entity": "",
            },
        }))

        assert out["current_tool_result"]["evidence"] == "GitHub README 内容"

    def test_tool_failure_isolation(self, monkeypatch):
        """工具执行异常应返回 error 字段，不抛错。"""
        from brain_base.nodes.qa_hop import create_tool_executor

        async def fake_failing_tool(tool_args, llm, cfg):
            raise RuntimeError("network timeout")

        monkeypatch.setattr(
            "brain_base.nodes.qa_hop.TOOL_REGISTRY",
            self._fake_registry(web_search=fake_failing_tool),
        )

        node = create_tool_executor(object(), object())
        out = asyncio.run(node({
            "current_tool_selection": {
                "tool_name": "web_search",
                "tool_args": {"query": "test"},
                "stop_entity": "",
            },
        }))

        result = out["current_tool_result"]
        assert "error" in result
        assert "network timeout" in result["error"]
        assert result["evidence"] == ""
        assert result["resolved_entity"] == ""

    def test_raw_result_with_error_field(self, monkeypatch):
        """raw_result 含 error 字段时，应透传到 current_tool_result。"""
        from brain_base.nodes.qa_hop import create_tool_executor

        async def fake_tool_with_error(tool_args, llm, cfg):
            return {"error": "404 not found", "markdown": "", "source_url": "", "title": ""}

        monkeypatch.setattr(
            "brain_base.nodes.qa_hop.TOOL_REGISTRY",
            self._fake_registry(web_search=fake_tool_with_error),
        )

        node = create_tool_executor(object(), object())
        out = asyncio.run(node({
            "current_tool_selection": {
                "tool_name": "web_search",
                "tool_args": {"query": "test"},
                "stop_entity": "",
            },
        }))

        result = out["current_tool_result"]
        assert result["error"] == "404 not found"
        assert result["evidence"] == ""
        assert result["markdown"] == ""

    def test_invalid_tool_name(self, monkeypatch):
        """tool_name 不在 TOOL_REGISTRY 中时应返回 error。"""
        from brain_base.nodes.qa_hop import create_tool_executor

        # 替换为只有单个工具的 registry
        from types import SimpleNamespace
        monkeypatch.setattr(
            "brain_base.nodes.qa_hop.TOOL_REGISTRY",
            {"other": SimpleNamespace(is_async=True, fn=lambda *a, **kw: {})},
        )

        node = create_tool_executor(object(), object())
        out = asyncio.run(node({
            "current_tool_selection": {
                "tool_name": "nonexistent",
                "tool_args": {},
                "stop_entity": "",
            },
        }))

        result = out["current_tool_result"]
        assert "nonexistent" in result["error"]
        assert result["evidence"] == ""

    def test_llm_extraction_failure_fallback(self, monkeypatch):
        """LLM 提取 HopObservation 失败时，应 fallback 到 markdown 前 500 字符。"""
        from brain_base.nodes.qa_hop import create_tool_executor

        async def fake_tool(tool_args, llm, cfg):
            return {
                "markdown": "A" * 1000,
                "source_url": "https://test.com",
                "title": "Test",
            }

        monkeypatch.setattr(
            "brain_base.nodes.qa_hop.TOOL_REGISTRY",
            self._fake_registry(web_search=fake_tool),
        )

        def fake_invoke_raises(*a, **kw):
            raise RuntimeError("LLM extraction failed")

        monkeypatch.setattr(
            "brain_base.nodes.qa_hop.invoke_structured", fake_invoke_raises
        )

        node = create_tool_executor(object(), object())
        out = asyncio.run(node({
            "current_tool_selection": {
                "tool_name": "web_search",
                "tool_args": {"query": "test"},
                "stop_entity": "",
            },
        }))

        result = out["current_tool_result"]
        assert result["evidence"] == "A" * 500  # fallback 到 markdown[:500]
        assert result["confidence"] == 0.3


# ---------------------------------------------------------------------------
# fetch_user_urls
# ---------------------------------------------------------------------------


class TestFetchUserUrls:
    """fetch_user_urls 节点：raw_text 短路 + _fetch_and_evaluate fallback。"""

    def test_raw_text_shortcut(self, monkeypatch):
        """try_raw_text 命中时直接返回简化 candidate，不调 _fetch_and_evaluate。"""
        from brain_base.nodes.qa_get_info import create_fetch_user_urls

        def fake_try_raw_text(url):
            return {
                "markdown": "# raw text\ncontent",
                "title": "Raw",
            }

        monkeypatch.setattr(
            "brain_base.tools.raw_text_extractor.try_raw_text", fake_try_raw_text
        )

        # _fetch_and_evaluate 应不被调用
        called = {"n": 0}
        async def fake_fetch_eval(*a, **kw):
            called["n"] += 1
            return None

        monkeypatch.setattr(
            "brain_base.nodes.qa_get_info._fetch_and_evaluate", fake_fetch_eval
        )

        node = create_fetch_user_urls(object())
        out = asyncio.run(node({
            "user_urls": ["https://github.com/test/repo"],
            "normalized_query": "test question",
        }))

        candidates = out["get_info_candidates"]
        assert len(candidates) == 1
        assert candidates[0]["type"] == "raw_text"
        assert candidates[0]["score"] == 80
        assert candidates[0]["whether_in"] is True
        assert called["n"] == 0  # _fetch_and_evaluate 未被调用

    def test_fetch_evaluate_fallback(self, monkeypatch):
        """try_raw_text 未命中时 fallback 到 _fetch_and_evaluate。"""
        from brain_base.nodes.qa_get_info import create_fetch_user_urls

        def fake_try_raw_text(url):
            return None  # 未命中

        monkeypatch.setattr(
            "brain_base.tools.raw_text_extractor.try_raw_text", fake_try_raw_text
        )

        async def fake_fetch_eval(*a, **kw):
            return {
                "url": "https://example.com",
                "title": "Example",
                "markdown": "# content",
                "score": 75,
                "whether_in": True,
            }

        monkeypatch.setattr(
            "brain_base.nodes.qa_get_info._fetch_and_evaluate", fake_fetch_eval
        )

        node = create_fetch_user_urls(object())
        out = asyncio.run(node({
            "user_urls": ["https://example.com"],
            "normalized_query": "test",
        }))

        candidates = out["get_info_candidates"]
        assert len(candidates) == 1
        assert candidates[0]["score"] == 75

    def test_empty_user_urls(self):
        """user_urls 为空时返回空 candidates + get_info_attempted=True。"""
        from brain_base.nodes.qa_get_info import create_fetch_user_urls

        node = create_fetch_user_urls(object())
        out = asyncio.run(node({
            "user_urls": [],
            "normalized_query": "test",
        }))

        assert out["get_info_candidates"] == []
        assert out["get_info_attempted"] is True

    def test_whether_in_filtering(self, monkeypatch):
        """whether_in=False 的 candidate 应被过滤掉。"""
        from brain_base.nodes.qa_get_info import create_fetch_user_urls

        def fake_try_raw_text(url):
            return None

        monkeypatch.setattr(
            "brain_base.tools.raw_text_extractor.try_raw_text", fake_try_raw_text
        )

        async def fake_fetch_eval(*a, **kw):
            return {
                "url": "https://example.com",
                "title": "Example",
                "markdown": "# content",
                "score": 60,
                "whether_in": False,
            }

        monkeypatch.setattr(
            "brain_base.nodes.qa_get_info._fetch_and_evaluate", fake_fetch_eval
        )

        node = create_fetch_user_urls(object())
        out = asyncio.run(node({
            "user_urls": ["https://example.com"],
            "normalized_query": "test",
        }))

        assert out["get_info_candidates"] == []


# ---------------------------------------------------------------------------
# QaState 12 个新字段默认值
# ---------------------------------------------------------------------------


class TestQaStateInit:
    """QaGraph.run 初始化的 T46 字段值（严格检查：实际执行第一个节点拦截）。"""

    def test_qastate_schema_has_all_t46_fields(self):
        """QaState TypedDict 必须含所有 12 个 T46 字段，且类型正确。"""
        from brain_base.graphs.qa_graph import QaState

        ann = QaState.__annotations__
        expected = {
            "user_urls": list,
            "plan_type": str,
            "max_hops": int,
            "initial_goal": str,
            "chain_reasoning": str,
            "pending_goals": list,
            "resolved_entities": dict,
            "hops": list,
            "hop_count": int,
            "current_tool_selection": dict,
            "current_tool_result": dict,
            "consecutive_tool_errors": int,
        }
        for field, expected_origin in expected.items():
            assert field in ann, f"QaState 缺失 T46 字段: {field}"
            # TypedDict 注解可能是 list[str] / dict[str,str] / int 等
            anno = ann[field]
            origin = getattr(anno, "__origin__", anno)
            assert origin is expected_origin, (
                f"QaState[{field}] 类型不符：期望 {expected_origin}，实际 origin={origin}"
            )

    def test_initial_dict_via_first_node_intercept(self, mock_llm, monkeypatch):
        """真验证 run 的 initial dict：patch probe_node 拦截入参，断言 T46 字段默认值。

        这是比 inspect.getsource 字符串匹配严格得多的验证——只要 initial dict 写法
        与源码不符（含注释里的伪字面量），mock probe 拿到的 state 会暴露。
        """
        from brain_base.graphs import qa_graph as qa_graph_module

        captured = {}

        def fake_probe(state):
            # 直接抛错短路图执行——把 state 留在异常里
            captured.update(state)
            raise RuntimeError("__STOP_AFTER_PROBE__")

        monkeypatch.setattr(qa_graph_module, "probe_node", fake_probe)

        # patch 之后再实例化 graph（否则 add_node 时绑定的还是旧 probe_node）
        g = qa_graph_module.QaGraph(llm=mock_llm)

        with pytest.raises(Exception):
            g.run(question="test question")

        # 验证所有 T46 字段都被初始化且值正确
        assert captured.get("user_urls") == []
        assert captured.get("plan_type") == "parallel"
        assert captured.get("max_hops") == 3
        assert captured.get("initial_goal") == ""
        assert captured.get("chain_reasoning") == ""
        assert captured.get("pending_goals") == []
        assert captured.get("resolved_entities") == {}
        assert captured.get("hops") == []
        assert captured.get("hop_count") == 0
        assert captured.get("current_tool_selection") == {}
        assert captured.get("current_tool_result") == {}
        assert captured.get("consecutive_tool_errors") == 0


# ---------------------------------------------------------------------------
# Graph 三路分流 dispatch
# ---------------------------------------------------------------------------


class TestGraphDispatch:
    """_after_classify_plan_dispatch 三种返回值（直接对比 compiled graph 边集）。"""

    def _drawable_edges(self, g):
        """提取 compiled graph 所有边（含 conditional），返回 (source, target) 集合。

        LangGraph 的 Drawable Graph.edges 含 conditional 展开后的所有路径，
        每条边是 namedtuple Edge(source, target, data?, conditional?)。
        """
        drawable = g.graph.get_graph()
        result = set()
        for e in drawable.edges:
            # 支持 namedtuple 或 tuple 两种返回
            src = getattr(e, "source", None) or e[0]
            tgt = getattr(e, "target", None) or e[1]
            result.add((src, tgt))
        return result

    def test_classify_plan_three_branches(self, mock_llm):
        """classify_plan 必须连到 barrier1（parallel）+ hop_planner（iterative）+ fetch_user_urls（direct_url）。

        严格断言：每条目标都在 drawable edges 中存在（不允许 or 兜底）。
        """
        from brain_base.graphs.qa_graph import QaGraph

        g = QaGraph(llm=mock_llm)
        edges = self._drawable_edges(g)

        assert ("classify_plan", "barrier1") in edges, (
            f"classify_plan → barrier1 边缺失，实际 edges from classify_plan: "
            f"{[e for e in edges if e[0] == 'classify_plan']}"
        )
        assert ("classify_plan", "hop_planner") in edges
        assert ("classify_plan", "fetch_user_urls") in edges

    def test_iterative_hop_loop_topology(self, mock_llm):
        """迭代循环：hop_planner → tool_selector → tool_executor → hop_observer → {hop_planner | merge_hop_evidence}。"""
        from brain_base.graphs.qa_graph import QaGraph

        g = QaGraph(llm=mock_llm)
        edges = self._drawable_edges(g)

        assert ("hop_planner", "tool_selector") in edges
        assert ("tool_selector", "tool_executor") in edges
        assert ("tool_executor", "hop_observer") in edges
        # conditional edges 应该展开成两条
        assert ("hop_observer", "hop_planner") in edges
        assert ("hop_observer", "merge_hop_evidence") in edges

    def test_three_way_converge_to_ingest(self, mock_llm):
        """三路（barrier_extract / merge_hop_evidence / fetch_user_urls）都通向 ingest。"""
        from brain_base.graphs.qa_graph import QaGraph

        g = QaGraph(llm=mock_llm)
        edges = self._drawable_edges(g)

        # 严格断言（不再用 or any 兜底）
        assert ("barrier_extract", "ingest") in edges
        assert ("merge_hop_evidence", "ingest") in edges
        assert ("fetch_user_urls", "ingest") in edges

    def test_after_classify_plan_dispatch_function_behavior(self, mock_llm, monkeypatch):
        """直接验证 _after_classify_plan_dispatch 闭包函数在三种 plan_type 下的返回值。

        通过 patch fanout_prep_dispatcher 返回固定值，runtime 行为可被观察。
        """
        from brain_base.graphs import qa_graph as qa_graph_module

        # patch fanout_prep_dispatcher 返回固定 sentinel
        monkeypatch.setattr(
            qa_graph_module, "fanout_prep_dispatcher",
            lambda state: "__PARALLEL_SENTINEL__",
        )

        # 拦截 conditional_edges 调用获取 dispatch 闭包
        captured_dispatch = {}
        from langgraph.graph import StateGraph
        orig_add = StateGraph.add_conditional_edges

        def spy_add(self, source, path, *args, **kwargs):
            if source == "classify_plan":
                captured_dispatch["fn"] = path
            return orig_add(self, source, path, *args, **kwargs)

        monkeypatch.setattr(StateGraph, "add_conditional_edges", spy_add)

        qa_graph_module.QaGraph(llm=mock_llm)
        dispatch = captured_dispatch["fn"]

        # parallel 委托 fanout_prep_dispatcher
        assert dispatch({"plan_type": "parallel"}) == "__PARALLEL_SENTINEL__"
        # iterative 返回节点名
        assert dispatch({"plan_type": "iterative"}) == "hop_planner"
        # direct_url 返回节点名
        assert dispatch({"plan_type": "direct_url"}) == "fetch_user_urls"
        # 缺省 plan_type → parallel
        assert dispatch({}) == "__PARALLEL_SENTINEL__"


# ---------------------------------------------------------------------------
# merge_hop_evidence score 排序
# ---------------------------------------------------------------------------


class TestMergeHopEvidenceSorting:
    """merge_hop_evidence_node 多个 hop 时的 score 排序。"""

    def test_multiple_hops_sorted_by_score(self):
        from brain_base.nodes.qa_hop import merge_hop_evidence_node

        state = {
            "hops": [
                {
                    "evidence": "low confidence",
                    "markdown": "# low",
                    "confidence": 0.5,
                    "source_url": "http://low.com",
                    "title": "Low",
                    "goal": "G1",
                },
                {
                    "evidence": "high confidence",
                    "markdown": "# high",
                    "confidence": 0.95,
                    "source_url": "http://high.com",
                    "title": "High",
                    "goal": "G2",
                },
                {
                    "evidence": "mid confidence",
                    "markdown": "# mid",
                    "confidence": 0.7,
                    "source_url": "http://mid.com",
                    "title": "Mid",
                    "goal": "G3",
                },
            ],
        }

        out = merge_hop_evidence_node(state)
        candidates = out["get_info_candidates"]
        assert len(candidates) == 3
        scores = [c["score"] for c in candidates]
        assert scores == [95, 70, 50]  # confidence * 100 降序

    def test_confidence_zero_skipped(self):
        """confidence=0 且 evidence/markdown 为空时应跳过。"""
        from brain_base.nodes.qa_hop import merge_hop_evidence_node

        state = {
            "hops": [
                {
                    "evidence": "",
                    "markdown": "",
                    "confidence": 0,
                    "source_url": "",
                    "title": "",
                    "goal": "G1",
                },
            ],
        }

        out = merge_hop_evidence_node(state)
        assert out["get_info_candidates"] == []

    def test_error_hop_skipped(self):
        """含 error 字段的 hop 应被跳过。"""
        from brain_base.nodes.qa_hop import merge_hop_evidence_node

        state = {
            "hops": [
                {
                    "evidence": "good",
                    "markdown": "# good",
                    "confidence": 0.9,
                    "source_url": "u1",
                    "title": "T1",
                    "goal": "G1",
                },
                {
                    "error": "timeout",
                    "evidence": "bad",
                    "markdown": "# bad",
                    "confidence": 0.9,
                    "source_url": "u2",
                    "title": "T2",
                    "goal": "G2",
                },
            ],
        }

        out = merge_hop_evidence_node(state)
        candidates = out["get_info_candidates"]
        assert len(candidates) == 1
        assert candidates[0]["url"] == "u1"


# ---------------------------------------------------------------------------
# tool_selector infra fallback（先前未覆盖分支）
# ---------------------------------------------------------------------------


class TestToolSelectorInfraFallback:
    """tool_selector 基础设施不可用 → fallback 到 web_search 分支。"""

    def test_milvus_unavailable_falls_back_to_web_search(self):
        """local_search 需要 milvus，infra_status.milvus_available=False → 降级。"""
        from brain_base.nodes.qa_hop import tool_selector_node

        out = tool_selector_node({
            "current_tool_selection": {
                "goal": "查找 RAGFlow",
                "tool_name": "local_search",
                "tool_args": {"query": "RAGFlow"},
                "stop_entity": "RAGFlow 介绍",
                "next_goals": [],
                "reason": "本地有相关文档",
            },
            "infra_status": {"milvus_available": False},
        })

        sel = out["current_tool_selection"]
        assert sel["tool_name"] == "web_search"  # fallback 完成
        # 其他字段保留
        assert sel["goal"] == "查找 RAGFlow"
        assert sel["stop_entity"] == "RAGFlow 介绍"
        # tool_args 被替换为 query=goal
        assert sel["tool_args"] == {"query": "查找 RAGFlow"}

    def test_playwright_unavailable_falls_back(self):
        """fetch_url 需要 playwright，不可用时 → web_search（虽然 web_search 也要 playwright，但代码不递归校验）。"""
        from brain_base.nodes.qa_hop import tool_selector_node

        out = tool_selector_node({
            "current_tool_selection": {
                "goal": "抓 https://example.com",
                "tool_name": "fetch_url",
                "tool_args": {"url": "https://example.com", "question": "test"},
                "stop_entity": "",
                "next_goals": [],
                "reason": "用户给 URL",
            },
            "infra_status": {"playwright_available": False},
        })

        sel = out["current_tool_selection"]
        assert sel["tool_name"] == "web_search"

    def test_empty_selection_passes_through(self):
        """current_tool_selection 为空（hop_planner pending_goals=[] 时）→ 透传不变。"""
        from brain_base.nodes.qa_hop import tool_selector_node

        out = tool_selector_node({"current_tool_selection": {}})
        assert out["current_tool_selection"] == {}

    def test_no_infra_status_defaults_available(self):
        """state 缺 infra_status → 默认所有 infra 可用，工具不降级。"""
        from brain_base.nodes.qa_hop import tool_selector_node

        out = tool_selector_node({
            "current_tool_selection": {
                "goal": "G",
                "tool_name": "local_search",
                "tool_args": {"query": "q"},
                "stop_entity": "",
                "next_goals": [],
                "reason": "",
            },
        })

        # 无 infra_status → 默认 True → local_search 不降级
        assert out["current_tool_selection"]["tool_name"] == "local_search"
