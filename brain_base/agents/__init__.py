"""
brain_base agents 包。

参考 TradingAgents 的 agents/ 模式：
每个 agent 是一个 create_xxx() 工厂函数，返回一个 xxx_node(state) 节点函数。
"""

from .persistence_agent import create_persistence_agent
from .ingest_file_agent import create_ingest_file_agent
from .ingest_url_agent import create_ingest_url_agent
from .qa_agent import create_qa_agent
from .crystallize_agent import create_crystallize_agent
from .lifecycle_agent import create_lifecycle_agent
from .lint_agent import create_lint_agent

__all__ = [
    "create_persistence_agent",
    "create_ingest_file_agent",
    "create_ingest_url_agent",
    "create_qa_agent",
    "create_crystallize_agent",
    "create_lifecycle_agent",
    "create_lint_agent",
]
