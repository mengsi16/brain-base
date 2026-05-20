"""
brain_base：个人知识库 LangGraph 重构版。

参考 TradingAgents 的包结构（T55 后已扁平化为 2 层 indirection）：
- agents/：agent schemas + utils（agent 工厂随 BrainBaseGraph 一并拔除）
- graph/：条件逻辑（ConditionalLogic）
- llm_clients/：多 provider LLM 客户端
- nodes/：节点函数（纯业务逻辑）
- graphs/：子图定义（CLI 直接实例化，无顶层编排层）

T55 删除：BrainBaseGraph 顶层编排层 + GraphSetup + Propagator + 4 个 agent
工厂（qa / ingest_file / lifecycle / lint）+ agents.utils.agent_states 整文件。
CLI 8 个子命令现统一直接 `XxGraph(llm=...)` 实例化（fail-fast LLM 注入）。
"""

__all__: list[str] = []
