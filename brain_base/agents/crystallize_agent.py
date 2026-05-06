"""
Crystallize Agent 工厂函数。
"""

from typing import Any, Callable

from brain_base.graphs.crystallize_graph import CrystallizeGraph


def create_crystallize_agent() -> Callable:
    """创建 crystallize agent 节点函数"""

    def crystallize_agent_node(state: dict[str, Any]) -> dict[str, Any]:
        cg = CrystallizeGraph()
        mode = state.get("mode", "hit_check")

        if mode == "hit_check":
            question = state.get("user_question", "")
            entities = state.get("extracted_entities", [])
            try:
                result = cg.hit_check(user_question=question, extracted_entities=entities)
                return {"crystallize_result": result}
            except Exception as exc:
                return {"error": f"crystallize_agent hit_check 失败: {exc}"}

        if mode == "crystallize":
            try:
                result = CrystallizeGraph.crystallize(
                    user_question=state.get("user_question", ""),
                    answer_markdown=state.get("answer_markdown", ""),
                    value_score=state.get("value_score", 0.0),
                    trigger_keywords=state.get("trigger_keywords", []),
                    description=state.get("description", ""),
                )
                return {"crystallize_result": result}
            except Exception as exc:
                return {"error": f"crystallize_agent crystallize 失败: {exc}"}

        return {"error": f"crystallize_agent: 未知 mode={mode}"}

    return crystallize_agent_node
