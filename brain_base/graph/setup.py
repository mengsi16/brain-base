"""
GraphSetup：组装顶层编排图。

参考 TradingAgents 的 graph/setup.py 模式。
将所有 agent 节点注册到 StateGraph，定义边和条件边。
"""

from langgraph.graph import END, START, StateGraph

from brain_base.agents.utils.agent_states import BrainBaseState
from brain_base.agents import (
    create_qa_agent,
    create_ingest_file_agent,
    create_ingest_url_agent,
    create_lifecycle_agent,
    create_lint_agent,
)
from brain_base.graph.conditional_logic import ConditionalLogic


class GraphSetup:
    """组装 brain_base 顶层编排图"""

    def __init__(self, conditional_logic: ConditionalLogic | None = None, llm=None):
        self.conditional_logic = conditional_logic or ConditionalLogic()
        self.llm = llm

    def setup_graph(self) -> StateGraph:
        """组装并返回 workflow（未 compile）"""
        workflow = StateGraph(BrainBaseState)

        # 注册 agent 节点（LLM 可选，None 时走降级路径）
        workflow.add_node("qa_agent", create_qa_agent(llm=self.llm))
        workflow.add_node("ingest_file_agent", create_ingest_file_agent(llm=self.llm))
        workflow.add_node("ingest_url_agent", create_ingest_url_agent(llm=self.llm))
        workflow.add_node("lifecycle_agent", create_lifecycle_agent())
        workflow.add_node("lint_agent", create_lint_agent())

        # 入口 → 按 mode 路由
        workflow.add_conditional_edges(START, self.conditional_logic.route_by_mode, {
            "qa_agent": "qa_agent",
            "ingest_file_agent": "ingest_file_agent",
            "ingest_url_agent": "ingest_url_agent",
            "lifecycle_agent": "lifecycle_agent",
            "lint_agent": "lint_agent",
            "end": END,
        })

        # 各 agent → END
        workflow.add_edge("qa_agent", END)
        workflow.add_edge("ingest_file_agent", END)
        workflow.add_edge("ingest_url_agent", END)
        workflow.add_edge("lifecycle_agent", END)
        workflow.add_edge("lint_agent", END)

        return workflow
