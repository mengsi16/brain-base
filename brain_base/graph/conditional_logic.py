"""
条件边路由逻辑集中地（参考 TradingAgents 的 graph/conditional_logic.py）。

所有 `add_conditional_edges` 用的路由函数都集中在这里，按图分组。
单纯的 add_edge 直连不在此处出现——只有「需要根据 state 字段决定下一步」
才属于条件边。
"""

from __future__ import annotations

from typing import Any


class ConditionalLogic:
    """brain_base 全部图的条件边路由集合。"""

    # ------------------------------------------------------------------
    # T55 删除：route_by_mode（原顶层 BrainBaseGraph mode 分流）
    # 随 BrainBaseGraph + GraphSetup + Propagator 顶层编排层一并拔除，CLI 8
    # 个子命令现直接 `XxGraph(llm=...)` 实例化（fail-fast LLM 注入）。
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # QA 主图
    # ------------------------------------------------------------------

    def after_crystallized_check(self, state: dict[str, Any]) -> str:
        """固化层命中 → 直接 answer；其余 → 进入 extract_urls 走统一意图识别 Agent-Loop。

        6 状态路由（T34 显式化 + T47.4 改 miss/stale 出口）：
        - hit_fresh     → answer（热命中且新鲜，直接返回固化答案）
        - cold_promoted → answer（冷层刚晋升为热，视同 hit_fresh）
        - hit_stale     → extract_urls（过期，走完整 RAG 重新回答；刷新路径留后续版本）
        - cold_observed → extract_urls（仅观察 +1，走完整 RAG）
        - miss          → extract_urls（两层都未命中）
        - degraded      → extract_urls（固化层异常，静默降级）

        T47.4 变更：miss/stale/observed/degraded 出口从 "normalize" 改为 "extract_urls"——
        新拓扑先经 extract_urls 提取 user_urls + url_pre_fetch 浅抓改写上下文，再进 normalize。
        """
        status = state.get("crystallized_status", "miss")
        if status in ("hit_fresh", "cold_promoted"):
            return "answer"
        return "extract_urls"

    # T23 删除：after_decompose（旧的单链路 vs fanout 二选一路由）
    # 改用 brain_base/nodes/qa_prep.py::fanout_prep_dispatcher——所有问题统一
    # 走 fanout_prep（不分解 = 1 个子问题 = [normalized_query]），不再有单链路。

    # T47.4 删除：after_classify_plan / should_continue_hopping（T46 三路分流 +
    # 迭代多跳循环路由）。统一意图识别 Agent-Loop 已替代三路分流，相关代码归 T47.6
    # 整体清理。3 个新路由函数见下方 route_after_extract_urls / should_continue_intent。

    def route_after_extract_urls(self, state: dict[str, Any]) -> str:
        """T47.4 extract_urls 后路由（D7 A 方案）。

        - user_urls 非空 → url_pre_fetch（浅抓内容供 normalize 改写时作上下文）
        - user_urls 空   → normalize（直行，跳过 url_pre_fetch）

        user_urls 由 extract_urls 节点正则提取，**不是分流标志**——url_pre_fetch
        只是改写辅助节点，不影响后续意图识别 Agent 的决策权。
        """
        if state.get("user_urls", []) or []:
            return "url_pre_fetch"
        return "normalize"

    def should_continue_intent(self, state: dict[str, Any]) -> str:
        """T47.4 统一意图识别 Agent-Loop 终止判断（契约 §2.3 / T47.0 §10）。

        5 级判断（先短路后正常，详 T47.4 执行计划 §5 R4 优先级理由）：
        1. consecutive_intent_errors >= 2     → merge_evidence（连错保护，最高优先）
        2. intent_sufficient is True          → merge_evidence（信息充分，observer LLM 评估）
        3. iteration_count >= max_iterations  → merge_evidence（跳数上限保护）
        4. current_intent_plan["next_actions"] 空 → merge_evidence（no_action 早退；含
           early_exit=True 情形——T47.3a planner 工厂强制清空 actions 时）
        5. 其余                                → intent_planner（继续下一跳）

        优先级理由：连错时 LLM evaluated confidence 可能不可信（前序工具失败 →
        evidence_pool 缺失），强制走 merge 用现有 evidence 兜底；充分判断优于上限
        避免明明 evidence 够还硬跑满 max_iterations；上限是物理硬约束优于 no_action。
        """
        if state.get("consecutive_intent_errors", 0) >= 2:
            return "merge_evidence"
        if state.get("intent_sufficient", False):
            return "merge_evidence"
        if state.get("iteration_count", 0) >= state.get("max_iterations", 5):
            return "merge_evidence"
        plan = state.get("current_intent_plan", {}) or {}
        if not (plan.get("next_actions") or []):
            return "merge_evidence"
        return "intent_planner"

    # T47.6 删除：after_barrier1 / after_judge / after_get_info_trigger 三个 T46
    # 路由函数。barrier1 / get_info_trigger / web_research 节点 T47.4 已从主图
    # 拔除，T47.6 同步删除函数体；judge 后路由 T47.4 改为简化直连（judge → answer），
    # 不再走 trigger 回路；3 函数零引用（grep 验证 brain_base/ + tests/）。

    # ------------------------------------------------------------------
    # Crystallize 子图
    # ------------------------------------------------------------------

    def after_hit_check(self, state: dict[str, Any]) -> str:
        """hit_check 后：命中 hot → 走 freshness；其余 → END。"""
        status = state.get("status", "miss")
        if status == "hit_hot":
            return "freshness"
        return "end"

    def after_freshness(self, state: dict[str, Any]) -> str:
        """freshness 后：无论 fresh 还是 stale 都直接 END，由调用方决定下一步。"""
        return "end"

    def should_write_crystallize(self, state: dict[str, Any]) -> str:
        """value_score < 0.3 跳过；recommended_layer=skip 跳过；其余写入。"""
        if state.get("recommended_layer", "cold") == "skip":
            return "skip"
        if float(state.get("value_score", 0.0)) < 0.3:
            return "skip"
        return "write"

    # ------------------------------------------------------------------
    # Lifecycle 子图
    # ------------------------------------------------------------------

    def should_execute_lifecycle(self, state: dict[str, Any]) -> str:
        """confirm=False 短路；Milvus 删除失败立即停。"""
        if not state.get("confirm", False):
            return "end"
        if state.get("milvus_delete_failed", False):
            return "end"
        return "continue"

    # ------------------------------------------------------------------
    # T50 删除：原 IngestUrl 子图的 after_completeness_check 路由
    # 随 IngestUrlGraph 一并删除（ask 路径已全面覆盖 URL 入库语义）。
    # T54 删除：原 GetInfo 子图的 route_get_info_continue 路由
    # 随 GetInfoGraph 一并删除（外检改走 fetch_extract 链路，无多步循环）。
    # ------------------------------------------------------------------
