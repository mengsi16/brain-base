"""
brain_base graph 包。

参考 TradingAgents 的 graph/ 模式：
- setup.py：GraphSetup 组装类
- conditional_logic.py：条件边路由
- propagation.py：状态初始化
- brain_base_graph.py：顶层编排类
"""

from .setup import GraphSetup
from .brain_base_graph import BrainBaseGraph
from .conditional_logic import ConditionalLogic
from .propagation import Propagator

__all__ = [
    "GraphSetup",
    "BrainBaseGraph",
    "ConditionalLogic",
    "Propagator",
]
