"""
BrainBaseGraph：顶层编排类。

参考 TradingAgents 的 TradingAgentsGraph 模式。
统一入口，按 mode 路由到对应子图。
"""

import logging
from typing import Any

from brain_base.graph.setup import GraphSetup
from brain_base.graph.conditional_logic import ConditionalLogic
from brain_base.graph.propagation import Propagator

logger = logging.getLogger(__name__)


class BrainBaseGraph:
    """brain_base 顶层编排图

    Args:
        llm: 可选 LLM 实例（LangChain BaseChatModel）；
             None 时所有 LLM 节点走降级路径（规则实现），图仍可完整运行
        debug: 是否启用调试模式
    """

    def __init__(self, llm: Any = None, debug: bool = False):
        self.llm = llm
        self.debug = debug
        self.conditional_logic = ConditionalLogic()
        self.graph_setup = GraphSetup(self.conditional_logic, llm=llm)
        self.propagator = Propagator()

        # 组装并编译图
        self.workflow = self.graph_setup.setup_graph()
        self.graph = self.workflow.compile()

    def run(self, **kwargs) -> dict[str, Any]:
        """执行图

        按模式调用：
        - mode="ask", question="..."
        - mode="ingest-file", input_files=[...]
        - mode="ingest-url", url="..."
        - mode="remove-doc", doc_ids=[...], confirm=True
        - mode="lint"
        """
        init_state = self.propagator.create_initial_state(**kwargs)
        args = self.propagator.get_graph_args()

        if self.debug:
            for chunk in self.graph.stream(init_state, **args):
                logger.info("chunk: %s", list(chunk.keys()))
            # stream 不返回最终状态，用 invoke 补
            result = self.graph.invoke(init_state, **args)
        else:
            result = self.graph.invoke(init_state, **args)

        return dict(result)

    def ask(self, question: str) -> dict[str, Any]:
        """QA 问答"""
        return self.run(mode="ask", question=question)

    def ingest_file(self, input_files: list[str]) -> dict[str, Any]:
        """文件入库"""
        return self.run(mode="ingest-file", input_files=input_files)

    def ingest_url(self, url: str, source_type: str = "community", topic: str = "untitled") -> dict[str, Any]:
        """URL 入库"""
        return self.run(mode="ingest-url", url=url, source_type=source_type, topic=topic)

    def remove_doc(self, doc_ids: list[str], confirm: bool = False, reason: str = "") -> dict[str, Any]:
        """文档删除"""
        return self.run(mode="remove-doc", doc_ids=doc_ids, confirm=confirm, reason=reason)

    def lint(self) -> dict[str, Any]:
        """固化层清理"""
        return self.run(mode="lint")
