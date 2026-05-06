"""
状态初始化与图调用参数。

参考 TradingAgents 的 graph/propagation.py 模式。
"""

from typing import Any

from brain_base.agents.utils.agent_states import BrainBaseState


class Propagator:
    """处理状态初始化和图调用参数"""

    def __init__(self, max_recur_limit: int = 50):
        self.max_recur_limit = max_recur_limit

    def create_initial_state(self, **kwargs) -> dict[str, Any]:
        """创建顶层初始状态"""
        state: dict[str, Any] = {
            "mode": kwargs.get("mode", "ask"),
            "question": kwargs.get("question", ""),
            "input_files": kwargs.get("input_files", []),
            "url": kwargs.get("url", ""),
            "source_type": kwargs.get("source_type", "community"),
            "topic": kwargs.get("topic", "untitled"),
            "doc_ids": kwargs.get("doc_ids", []),
            "confirm": kwargs.get("confirm", False),
            "reason": kwargs.get("reason", ""),
        }
        return state

    def get_graph_args(self) -> dict[str, Any]:
        """获取图调用参数"""
        return {
            "config": {"recursion_limit": self.max_recur_limit},
        }
