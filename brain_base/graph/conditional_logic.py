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
    # 顶层编排（BrainBaseGraph）
    # ------------------------------------------------------------------

    def route_by_mode(self, state: dict[str, Any]) -> str:
        """按 mode 路由到对应子图。"""
        mode = state.get("mode", "")
        return {
            "ask": "qa_agent",
            "ingest-file": "ingest_file_agent",
            "ingest-url": "ingest_url_agent",
            "remove-doc": "lifecycle_agent",
            "lint": "lint_agent",
        }.get(mode, "end")

    # ------------------------------------------------------------------
    # QA 主图
    # ------------------------------------------------------------------

    def after_crystallized_check(self, state: dict[str, Any]) -> str:
        """固化层命中 → 直接 answer；其余 → 走完整 RAG。"""
        status = state.get("crystallized_status", "miss")
        if status in ("hit_fresh", "cold_promoted"):
            return "answer"
        return "normalize"

    def after_judge(self, state: dict[str, Any]) -> str:
        """证据判断后路由：

        - evidence_sufficient=True → answer
        - 已尝试过外检（get_info_attempted=True）→ answer（防死循环）
        - 否则 → get_info_trigger（让 trigger 节点决定要不要真去外检）
        """
        if state.get("evidence_sufficient", False):
            return "answer"
        if state.get("get_info_attempted", False):
            # 第二次到 judge：不管 evidence 充不充分都直接 answer，避免死循环
            return "answer"
        return "get_info_trigger"

    def after_get_info_trigger(self, state: dict[str, Any]) -> str:
        """get_info_trigger 后路由：

        - trigger_get_info=False（不需 / 不可用 / 已禁用）→ answer
        - trigger_get_info=True → web_research
        """
        if state.get("trigger_get_info", False):
            return "web_research"
        return "answer"

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
    # IngestUrl 子图
    # ------------------------------------------------------------------

    def after_completeness_check(self, state: dict[str, Any]) -> str:
        """completeness 不通过的不写文件，直接 END 携带 completeness_status 给上层。"""
        status = state.get("completeness_status", "ok")
        if status == "ok":
            return "frontmatter"
        return "end"

    # ------------------------------------------------------------------
    # GetInfo 子图（多步循环）
    # ------------------------------------------------------------------

    def route_get_info_continue(self, state: dict[str, Any]) -> str:
        """读 check_continue 节点写入的 _route 字段。"""
        return state.get("_route", "end")
