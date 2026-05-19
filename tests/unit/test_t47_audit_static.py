# -*- coding: utf-8 -*-
"""T47 静态审核测试（不依赖之前测试与文档，只比对 ToDo.md 声明）。

只验证：
1. T47.6 文件删除是否到位
2. T47.1 schemas / config / QaState 字段
3. T47.2-T47.4 节点 / 路由 / wiring 是否齐全
4. T47.6 旧 import / 旧函数引用零残留
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# T47.6：文件级删除审核
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel_path",
    [
        "brain_base/nodes/qa_hop.py",
        "brain_base/nodes/qa_prep.py",
        "brain_base/prompts/classify_plan_prompts.py",
        "brain_base/prompts/hop_planner_prompts.py",
        "tests/e2e/test_qa_multihop.py",
        "bin/demo_prompts_trace.py",
    ],
)
def test_t47_6_files_deleted(rel_path: str) -> None:
    assert not (REPO_ROOT / rel_path).exists(), f"{rel_path} 应已被 T47.6 删除"


@pytest.mark.parametrize(
    "rel_path",
    [
        "brain_base/nodes/qa_extract_urls.py",
        "brain_base/nodes/qa_url_pre_fetch.py",
        "brain_base/nodes/qa_intent.py",
        "brain_base/prompts/intent_prompts.py",
    ],
)
def test_t47_new_files_exist(rel_path: str) -> None:
    assert (REPO_ROOT / rel_path).exists(), f"{rel_path} 应由 T47.2/T47.3 落地"


# ---------------------------------------------------------------------------
# T47.1：schemas + config + QaState 审核
# ---------------------------------------------------------------------------


def test_t47_1_new_schemas_present() -> None:
    schemas = importlib.import_module("brain_base.agents.schemas")
    for name in ("Evidence", "IntentAction", "IntentPlan", "IntentObservation"):
        assert hasattr(schemas, name), f"schemas 缺 {name}（T47.1 应新增）"


def test_t47_6_dead_schemas_removed() -> None:
    schemas = importlib.import_module("brain_base.agents.schemas")
    for name in ("RetrievalPlan", "HopPlan", "HopObservation",
                 "SearchStrategy", "SearchStrategyBatch"):
        assert not hasattr(schemas, name), f"schemas 残留死类 {name}（T47.6 应删）"


def test_t47_1_normalized_question_has_history_summary() -> None:
    schemas = importlib.import_module("brain_base.agents.schemas")
    fields = schemas.NormalizedQuestion.model_fields
    assert "conversation_history_summary" in fields, "T47.2 D4 应给 NormalizedQuestion 加摘要字段"


def test_t47_1_config_max_intent_iterations() -> None:
    cfg_mod = importlib.import_module("brain_base.config")
    cfg = cfg_mod.GetInfoConfig()
    assert getattr(cfg, "max_intent_iterations", None) == 5, "T47.1 D5 应设 5"
    assert not hasattr(cfg, "enable_search_strategy"), "T47.6 应删 enable_search_strategy"


def test_t47_qa_state_fields() -> None:
    qg = importlib.import_module("brain_base.graphs.qa_graph")
    annotations = set(qg.QaState.__annotations__.keys())
    # 11 新字段（含 user_urls）
    expected_new = {
        "url_pre_fetch_content", "evidence_pool", "visited_urls",
        "iteration_count", "max_iterations", "intent_sufficient",
        "consecutive_intent_errors", "current_intent_plan",
        "current_action_results", "last_intent_observation",
        "conversation_history_summary", "user_urls",
    }
    missing = expected_new - annotations
    assert not missing, f"QaState 缺 T47 字段: {missing}"

    # T47.6 删除的 T46/T23 死字段
    expected_gone = {
        "plan_type", "max_hops", "hops", "hop_count",
        "consecutive_tool_errors", "current_tool_selection",
        "current_tool_result", "sub_prep_results", "extract_results",
        "sub_question_evidence",
    }
    leaked = expected_gone & annotations
    assert not leaked, f"QaState 残留 T46/T23 死字段（T47.6 应删）: {leaked}"


# ---------------------------------------------------------------------------
# T47.2-T47.3：节点工厂签名 + 公开 API 审核
# ---------------------------------------------------------------------------


def test_extract_urls_factory_signature() -> None:
    mod = importlib.import_module("brain_base.nodes.qa_extract_urls")
    assert hasattr(mod, "create_extract_urls")
    factory = mod.create_extract_urls
    # 不接受 llm 参数
    params = inspect.signature(factory).parameters
    assert len(params) == 0, f"create_extract_urls 不应有参数: {params}"
    node = factory()
    out = node({"question": "看 https://example.com 和 https://x.com/y"})
    assert out == {"user_urls": ["https://example.com", "https://x.com/y"]}


def test_url_pre_fetch_factory_signature() -> None:
    mod = importlib.import_module("brain_base.nodes.qa_url_pre_fetch")
    assert hasattr(mod, "create_url_pre_fetch")
    sig = inspect.signature(mod.create_url_pre_fetch)
    assert "cfg" in sig.parameters
    assert "excerpt_chars" in sig.parameters
    assert "fetch_timeout" in sig.parameters


def test_qa_intent_exports() -> None:
    mod = importlib.import_module("brain_base.nodes.qa_intent")
    for name in ("create_intent_planner", "create_intent_executor",
                 "create_intent_observer", "merge_evidence_node"):
        assert hasattr(mod, name), f"qa_intent 缺 {name}（T47.3a/T47.3b 应落地）"


def test_intent_factories_fail_fast_on_none_llm() -> None:
    """T27 fail-fast：3 个 LLM 节点工厂对 llm=None 必须 raise。"""
    mod = importlib.import_module("brain_base.nodes.qa_intent")
    for factory_name in ("create_intent_planner",
                         "create_intent_executor",
                         "create_intent_observer"):
        factory = getattr(mod, factory_name)
        with pytest.raises(ValueError, match="None"):
            if factory_name == "create_intent_executor":
                factory(None, None)
            else:
                factory(None)


def test_merge_evidence_pure_format_conversion() -> None:
    """merge_evidence 不调 LLM、空 pool 退化、含 pool 输出 13 字段。"""
    mod = importlib.import_module("brain_base.nodes.qa_intent")

    # 空 pool
    out = mod.merge_evidence_node({})
    assert out["get_info_candidates"] == []
    assert out["get_info_attempted"] is True

    # 单条 evidence
    pool = [{
        "url": "https://x.com",
        "title": "T",
        "content": "正文",
        "score": 80.0,
        "sha256_hash": "deadbeef",
        "from_queries": ["q1"],
        "snippet": "摘要",
        "source_type": "official-doc",
        "tool_name": "fetch_url",
    }]
    out = mod.merge_evidence_node({"evidence_pool": pool})
    assert out["get_info_attempted"] is True
    cands = out["get_info_candidates"]
    assert len(cands) == 1
    expected_keys = {"url", "title", "fetched_at", "markdown", "content_sha256",
                     "from_engines", "from_queries", "score", "type", "summary",
                     "keywords", "whether_in", "reason"}
    assert set(cands[0].keys()) == expected_keys
    assert cands[0]["markdown"] == "正文"
    assert cands[0]["content_sha256"] == "deadbeef"
    assert cands[0]["score"] == 80
    assert cands[0]["type"] == "official-doc"
    assert cands[0]["whether_in"] is True


def test_intent_prompts_present() -> None:
    mod = importlib.import_module("brain_base.prompts.intent_prompts")
    assert hasattr(mod, "INTENT_PLANNER_SYSTEM_PROMPT")
    assert hasattr(mod, "INTENT_OBSERVER_SYSTEM_PROMPT")
    # planner prompt 应留 {tools_desc} 占位（运行时 _format_tools_desc 注入）
    assert "{tools_desc}" in mod.INTENT_PLANNER_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# T47.4：conditional_logic 路由审核
# ---------------------------------------------------------------------------


def test_conditional_logic_routes() -> None:
    mod = importlib.import_module("brain_base.graph.conditional_logic")
    cl = mod.ConditionalLogic()

    # 新增路由必须存在
    assert hasattr(cl, "route_after_extract_urls")
    assert hasattr(cl, "should_continue_intent")

    # 删除路由必须不存在
    for dead in ("after_classify_plan", "should_continue_hopping",
                 "after_barrier1", "after_judge", "after_get_info_trigger"):
        assert not hasattr(cl, dead), f"conditional_logic 残留死路由 {dead}"

    # after_crystallized_check：miss/stale/observed/degraded → extract_urls；
    # hit_fresh/cold_promoted → answer
    assert cl.after_crystallized_check({"crystallized_status": "hit_fresh"}) == "answer"
    assert cl.after_crystallized_check({"crystallized_status": "cold_promoted"}) == "answer"
    for s in ("hit_stale", "cold_observed", "miss", "degraded", ""):
        assert cl.after_crystallized_check({"crystallized_status": s}) == "extract_urls"

    # route_after_extract_urls：user_urls 非空 → url_pre_fetch；空/缺字段 → normalize
    assert cl.route_after_extract_urls({"user_urls": ["https://x"]}) == "url_pre_fetch"
    assert cl.route_after_extract_urls({"user_urls": []}) == "normalize"
    assert cl.route_after_extract_urls({}) == "normalize"


def test_should_continue_intent_5_levels() -> None:
    mod = importlib.import_module("brain_base.graph.conditional_logic")
    cl = mod.ConditionalLogic()

    # 1. consecutive_intent_errors >= 2 → merge（最高优先）
    assert cl.should_continue_intent({"consecutive_intent_errors": 2}) == "merge_evidence"
    assert cl.should_continue_intent({"consecutive_intent_errors": 5,
                                       "intent_sufficient": False}) == "merge_evidence"

    # 2. intent_sufficient → merge
    assert cl.should_continue_intent({"intent_sufficient": True}) == "merge_evidence"

    # 3. iteration_count >= max_iterations → merge
    assert cl.should_continue_intent(
        {"iteration_count": 5, "max_iterations": 5}
    ) == "merge_evidence"
    assert cl.should_continue_intent(
        {"iteration_count": 10, "max_iterations": 5}
    ) == "merge_evidence"

    # 4. next_actions 空 → merge
    assert cl.should_continue_intent(
        {"current_intent_plan": {"next_actions": []}}
    ) == "merge_evidence"
    assert cl.should_continue_intent({}) == "merge_evidence"

    # 5. 正常继续 → intent_planner
    state = {
        "consecutive_intent_errors": 0,
        "intent_sufficient": False,
        "iteration_count": 1,
        "max_iterations": 5,
        "current_intent_plan": {"next_actions": [{"tool_name": "web_search"}]},
    }
    assert cl.should_continue_intent(state) == "intent_planner"

    # 优先级：连错 > 充分 > 上限
    assert cl.should_continue_intent({
        "consecutive_intent_errors": 3,
        "intent_sufficient": False,
        "iteration_count": 1,
        "max_iterations": 5,
        "current_intent_plan": {"next_actions": [{"tool_name": "x"}]},
    }) == "merge_evidence"


# ---------------------------------------------------------------------------
# T47.4：QaGraph 节点注册审核
# ---------------------------------------------------------------------------


def test_qa_graph_registered_nodes() -> None:
    """QaGraph 编译后的节点拓扑：6 新节点存在 + 14 老节点不存在。"""
    qg_mod = importlib.import_module("brain_base.graphs.qa_graph")

    class _Sentinel:
        """假 LLM，QaGraph T27 fail-fast 只校验 is None，非 None 即可通过。"""
        def __getattr__(self, _):
            raise RuntimeError("sentinel must not be invoked in static audit")

    g = qg_mod.QaGraph(llm=_Sentinel())
    nodes = set(g.graph.nodes.keys())

    # T47 新增 6 节点
    expected = {"extract_urls", "url_pre_fetch", "intent_planner",
                "intent_executor", "intent_observer", "merge_evidence"}
    missing = expected - nodes
    assert not missing, f"QaGraph 缺 T47 新节点: {missing}"

    # T46/T23 应删除的 14 节点
    dead = {"classify_plan", "subquery_prep", "barrier1",
            "merge_search_keywords", "search_web_dual", "fetch_extract_one",
            "barrier_extract", "search_strategy", "hop_planner",
            "tool_selector", "tool_executor", "hop_observer",
            "merge_hop_evidence", "fetch_user_urls"}
    leaked = dead & nodes
    assert not leaked, f"QaGraph 残留 T46 死节点: {leaked}"


def test_qa_graph_init_state_has_t47_defaults() -> None:
    """QaGraph.run() 不能被实际跑（需要真实环境），但 initial state 是 dict 字面量构造，
    审核办法：通过 inspect 拿源码反向断言 11 字段都给了默认值。"""
    qg_mod = importlib.import_module("brain_base.graphs.qa_graph")
    src = inspect.getsource(qg_mod.QaGraph.run)
    for key in ("url_pre_fetch_content", "evidence_pool", "visited_urls",
                "iteration_count", "max_iterations", "intent_sufficient",
                "consecutive_intent_errors", "current_intent_plan",
                "current_action_results", "last_intent_observation",
                "conversation_history_summary", "user_urls"):
        assert f'"{key}"' in src, f"QaGraph.run init state 缺 {key} 默认值"


# ---------------------------------------------------------------------------
# T47.6：源代码 import / 调用残留扫描
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "needle",
    [
        "from brain_base.nodes.qa_hop",
        "import brain_base.nodes.qa_hop",
        "from brain_base.nodes.qa_prep",
        "import brain_base.nodes.qa_prep",
        "from brain_base.prompts.classify_plan_prompts",
        "from brain_base.prompts.hop_planner_prompts",
        "create_classify_plan_node(",
        "re_search_node(",
    ],
)
def test_no_dead_imports_in_source(needle: str) -> None:
    src_root = REPO_ROOT / "brain_base"
    hits: list[str] = []
    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        # 仅扫非注释行（粗略：去掉 # 之后的内容）
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # 把行内 # 注释切掉再判断
            head = stripped.split("#", 1)[0]
            if needle in head:
                hits.append(f"{py.relative_to(REPO_ROOT)}:{i}: {line}")
    assert not hits, f"残留死代码引用 {needle!r}:\n" + "\n".join(hits)
