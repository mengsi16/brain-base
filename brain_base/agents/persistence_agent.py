"""
Persistence Agent 工厂函数。

参考 TradingAgents 的 create_xxx(llm) 模式。
"""

from typing import Any, Callable

from brain_base.graphs.persistence_graph import PersistenceGraph


def create_persistence_agent(llm: Any = None) -> Callable:
    """创建 persistence agent 节点函数

    Args:
        llm: 可选 LLM 实例；None 时 enrich 节点只标记待处理
    """

    def persistence_agent_node(state: dict[str, Any]) -> dict[str, Any]:
        pg = PersistenceGraph(llm=llm)
        raw_md_path = state.get("raw_md_path", "")
        doc_id = state.get("doc_id", "")
        if not raw_md_path:
            return {"error": "persistence_agent: raw_md_path 为空"}
        try:
            result = pg.run(raw_md_path=raw_md_path, doc_id=doc_id)
            return {"persistence_result": result}
        except Exception as exc:
            return {"error": f"persistence_agent 失败: {exc}"}

    return persistence_agent_node
