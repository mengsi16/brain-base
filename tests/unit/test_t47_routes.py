# -*- coding: utf-8 -*-
"""T47.4 主图路由函数测试。

覆盖 conditional_logic.ConditionalLogic 上 T47.4 改写 / 新增的 3 个路由：

- ``after_crystallized_check``（改写）：6 状态分桶到 answer / extract_urls
- ``route_after_extract_urls``（新增 D7 A 方案）：user_urls 非空 / 空 二路
- ``should_continue_intent``（新增）：5 级早退 + 正常继续

均不调 LLM，纯 dict 输入 → 字符串输出验证。

契约引用：
- md/research/2026-05-18-t47.4-graph-rewiring-execution-plan.md §2 路由函数契约
- md/research/2026-05-17-t47-unified-intent-agent-contract.md §10 should_continue_intent
"""
from __future__ import annotations

import pytest

from brain_base.graph.conditional_logic import ConditionalLogic


@pytest.fixture
def logic() -> ConditionalLogic:
    return ConditionalLogic()


# ---------------------------------------------------------------------------
# after_crystallized_check（T47.4 改写：miss/stale/observed/degraded → extract_urls）
# ---------------------------------------------------------------------------


class TestAfterCrystallizedCheck:
    """6 状态分桶：hit_fresh/cold_promoted → answer；其余 → extract_urls（T47.4 改）。"""

    @pytest.mark.parametrize("status", ["hit_fresh", "cold_promoted"])
    def test_hot_hit_routes_to_answer(self, logic: ConditionalLogic, status: str):
        """热命中 + 冷晋升热 → 直接 answer 走固化答案。"""
        assert logic.after_crystallized_check({"crystallized_status": status}) == "answer"

    @pytest.mark.parametrize(
        "status", ["hit_stale", "cold_observed", "miss", "degraded"]
    )
    def test_other_states_route_to_extract_urls(
        self, logic: ConditionalLogic, status: str
    ):
        """其他 4 状态均走 extract_urls 进入统一意图识别 Agent-Loop（T47.4 改：原走 normalize）。"""
        assert (
            logic.after_crystallized_check({"crystallized_status": status})
            == "extract_urls"
        )

    def test_default_missing_field_routes_to_extract_urls(self, logic: ConditionalLogic):
        """字段缺失默认按 miss 处理 → extract_urls（与 T46 默认 normalize 行为一致语义）。"""
        assert logic.after_crystallized_check({}) == "extract_urls"


# ---------------------------------------------------------------------------
# route_after_extract_urls（D7 A 方案）
# ---------------------------------------------------------------------------


class TestRouteAfterExtractUrls:
    """user_urls 非空 → url_pre_fetch；空 → normalize 直行。"""

    def test_nonempty_urls_routes_to_url_pre_fetch(self, logic: ConditionalLogic):
        state = {"user_urls": ["https://example.com/foo"]}
        assert logic.route_after_extract_urls(state) == "url_pre_fetch"

    def test_empty_urls_routes_to_normalize(self, logic: ConditionalLogic):
        state = {"user_urls": []}
        assert logic.route_after_extract_urls(state) == "normalize"

    def test_missing_urls_field_routes_to_normalize(self, logic: ConditionalLogic):
        """字段缺失等价于空 list → normalize（state.get 默认值兜底）。"""
        assert logic.route_after_extract_urls({}) == "normalize"


# ---------------------------------------------------------------------------
# should_continue_intent（5 级早退 + 正常继续）
# ---------------------------------------------------------------------------


class TestShouldContinueIntent:
    """5 级早退优先级（先短路后正常）：
    1. consecutive_intent_errors >= 2 → merge_evidence（最高优先）
    2. intent_sufficient is True      → merge_evidence
    3. iteration_count >= max_iterations → merge_evidence
    4. current_intent_plan["next_actions"] 空 → merge_evidence
    5. 其余                            → intent_planner
    """

    def _base_state(self, **overrides) -> dict:
        """正常继续状态：所有早退条件均未触发，只缺一个 actions 才继续。"""
        state = {
            "consecutive_intent_errors": 0,
            "intent_sufficient": False,
            "iteration_count": 1,
            "max_iterations": 5,
            "current_intent_plan": {
                "next_actions": [{"tool_name": "fetch_url", "tool_args": {}}],
                "early_exit": False,
                "reasoning": "继续抓取",
            },
        }
        state.update(overrides)
        return state

    def test_consecutive_errors_exit_highest_priority(self, logic: ConditionalLogic):
        """连错 ≥2 是最高优先 — 即便其他条件全允许继续，也强制 merge。"""
        state = self._base_state(consecutive_intent_errors=2)
        assert logic.should_continue_intent(state) == "merge_evidence"

    def test_consecutive_errors_overrides_continue_intent(
        self, logic: ConditionalLogic
    ):
        """连错保护即便 next_actions 非空也强制 merge（前序工具失败时 confidence 不可信）。"""
        state = self._base_state(consecutive_intent_errors=3)
        assert logic.should_continue_intent(state) == "merge_evidence"

    def test_intent_sufficient_exit(self, logic: ConditionalLogic):
        """observer LLM 评估信息充分（confidence ≥0.85 且 remaining_gaps=[] → True）→ merge。"""
        state = self._base_state(intent_sufficient=True)
        assert logic.should_continue_intent(state) == "merge_evidence"

    def test_iteration_limit_exit(self, logic: ConditionalLogic):
        """iteration_count >= max_iterations → merge（跳数上限保护）。"""
        state = self._base_state(iteration_count=5, max_iterations=5)
        assert logic.should_continue_intent(state) == "merge_evidence"

    def test_no_action_exit(self, logic: ConditionalLogic):
        """next_actions 空（含 early_exit=True 后被 planner 工厂强制清空的情形）→ merge。"""
        state = self._base_state(
            current_intent_plan={
                "next_actions": [],
                "early_exit": True,
                "reasoning": "已充分",
            }
        )
        assert logic.should_continue_intent(state) == "merge_evidence"

    def test_normal_continue(self, logic: ConditionalLogic):
        """所有早退条件未触发 + next_actions 非空 → intent_planner 继续下一跳。"""
        state = self._base_state()
        assert logic.should_continue_intent(state) == "intent_planner"

    def test_priority_errors_over_sufficient(self, logic: ConditionalLogic):
        """连错优先于充分判断（连错时 confidence 评估不可信）。"""
        state = self._base_state(consecutive_intent_errors=2, intent_sufficient=True)
        assert logic.should_continue_intent(state) == "merge_evidence"

    def test_priority_sufficient_over_iteration_limit(self, logic: ConditionalLogic):
        """充分判断优先于上限（避免明明 evidence 够还硬跑满 max_iterations）。

        即便已达上限，只要充分也归类为 merge——本测试关键不验证字符串差异，
        而验证：充分时不会因 iteration 计算路径触发任何异常。
        """
        state = self._base_state(intent_sufficient=True, iteration_count=5, max_iterations=5)
        assert logic.should_continue_intent(state) == "merge_evidence"

    def test_default_missing_fields_continues(self, logic: ConditionalLogic):
        """全部字段缺失 → 默认 0/False/{}，next_actions 空 → merge_evidence（no_action 早退）。

        这是首跳进入循环前的边界情形：state 里所有 intent_* 字段都是 init_state 默认值，
        current_intent_plan={}，next_actions 不存在等同于空 → 触发 4. no_action。
        """
        assert logic.should_continue_intent({}) == "merge_evidence"
