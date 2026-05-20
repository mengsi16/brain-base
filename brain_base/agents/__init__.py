"""
brain_base agents 包。

T55 后仅保留 schemas（Pydantic 模型）+ utils（agent_utils / structured /
tracing）；T55 删除：4 个 agent 工厂（qa / ingest_file / lifecycle / lint）
随 BrainBaseGraph 顶层编排一并拔除。CLI 直接实例化各子图（fail-fast LLM
注入），不再走"agent 包装节点 → 子图 invoke"的双层 indirection。

T56 已删 crystallize_agent / persistence_agent 双孤儿；T55 继续清理另 4 个。
"""

__all__: list[str] = []
