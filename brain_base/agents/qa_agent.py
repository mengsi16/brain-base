"""
QA Agent 工厂函数。
"""

from typing import Any, Callable

from brain_base.graphs.qa_graph import QaGraph


def create_qa_agent(llm: Any = None) -> Callable:
    """创建 QA agent 节点函数

    Args:
        llm: 可选 LLM 实例；None 时 QaGraph 内部节点走降级路径
    """

    def qa_agent_node(state: dict[str, Any]) -> dict[str, Any]:
        graph = QaGraph(llm=llm)
        question = state.get("question", "")
        if not question:
            return {"error": "qa_agent: question 为空"}
        try:
            result = graph.run(question=question)
            return {"qa_result": result}
        except Exception as exc:
            return {"error": f"qa_agent 失败: {exc}"}

    return qa_agent_node
