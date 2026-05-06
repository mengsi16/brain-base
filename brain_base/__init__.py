"""
brain_base：个人知识库 LangGraph 重构版。

参考 TradingAgents 的包结构：
- agents/：agent 工厂函数 + schemas + utils
- graph/：图组装、条件逻辑、传播、顶层编排
- llm_clients/：多 provider LLM 客户端
- nodes/：节点函数（纯业务逻辑）
- graphs/：子图定义
"""

from brain_base.graph.brain_base_graph import BrainBaseGraph

__all__ = ["BrainBaseGraph"]
