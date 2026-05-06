"""
Lint Agent 工厂函数。
"""

from typing import Any, Callable

from brain_base.graphs.lint_graph import LintGraph


def create_lint_agent() -> Callable:
    """创建 lint agent 节点函数"""

    def lint_agent_node(state: dict[str, Any]) -> dict[str, Any]:
        graph = LintGraph()
        try:
            result = graph.run()
            return {"lint_result": result}
        except Exception as exc:
            return {"error": f"lint_agent 失败: {exc}"}

    return lint_agent_node
