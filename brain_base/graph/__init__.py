"""
brain_base graph 包。

T55 后仅保留 conditional_logic.py（QaGraph / CrystallizeGraph / LifecycleGraph
共享的条件边路由集合 `ConditionalLogic` 类）。原 BrainBaseGraph 顶层编排
+ GraphSetup 组装类 + Propagator 状态初始化已随 T55 整层拔除，CLI 直接
实例化各子图（fail-fast LLM 注入）。
"""

from .conditional_logic import ConditionalLogic

__all__ = ["ConditionalLogic"]
