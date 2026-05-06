"""
Checkpointer 管理。

参考 TradingAgents/graph/checkpointer.py 模式。
"""

from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from langgraph.checkpoint.memory import MemorySaver


@contextmanager
def get_checkpointer(
    data_dir: str | Path,
    session_id: str,
) -> Generator[MemorySaver, None, None]:
    """获取 MemorySaver context manager"""
    yield MemorySaver()
