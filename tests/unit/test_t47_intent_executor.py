# -*- coding: utf-8 -*-
"""T47.3a 单元测试：intent_executor 节点。

覆盖（4 条）：
- test_executor_factory_rejects_none_llm（非 LLM）：T27 fail-fast
- test_executor_serial_single_action（mock TOOL）：len=1 串行调用
- test_executor_fanout_concurrent_with_isolation（mock TOOL）：fan-out 并发 + 单失败隔离
- test_executor_early_exit_returns_empty_results（无 mock）：early_exit 短路
- test_executor_unknown_tool_name_returns_error_result（无 mock）：未注册工具不抛

mock 策略：通过 monkeypatch 替换 TOOL_REGISTRY 字典内单条目（添加测试专用工具），
不动真实工具实现，确保隔离。无需真 LLM——executor 本身不调 LLM（决策已在 planner 完成）。

CLAUDE.md 规则 14 豁免：executor 是确定性 dispatcher（TOOL_REGISTRY → asyncio.gather），
不涉及 LLM 语义判断；mock TOOL_REGISTRY 是验证 *节点 dispatch 逻辑* 而非 LLM 行为。

契约引用：md/research/2026-05-17-t47-unified-intent-agent-contract.md §5
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

# 加载 .env（CLAUDE.md 规则 12）
try:
    from dotenv import load_dotenv
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    load_dotenv(_PROJECT_ROOT / ".env")
except Exception:
    pass


# ---------------------------------------------------------------------------
# Helper：构造 mock LLM sentinel（executor 不调 LLM，但工厂入参不能为 None）
# ---------------------------------------------------------------------------


class _LLMSentinel:
    """不被调用的 LLM 占位——executor 自身不调 LLM，但工具内部可能调。

    由于本测试 mock 了 TOOL_REGISTRY 内的 fn，不会调到 spec.fn(args, llm, cfg)
    再走 LLM 路径——sentinel 只用于通过 `llm is None` 校验。
    """

    def with_structured_output(self, schema, **kwargs):
        raise AssertionError("executor 测试不应触发 LLM 调用")

    def invoke(self, messages):
        raise AssertionError("executor 测试不应触发 LLM 调用")


def _run(coro):
    """同步运行 async coroutine（pytest-asyncio 不强依赖）。"""
    return asyncio.run(coro)


def _empty_cfg():
    """构造一个最小的 GetInfoConfig 实例（带 max_intent_iterations 默认值即可）。"""
    from brain_base.config import GetInfoConfig
    return GetInfoConfig()


# ---------------------------------------------------------------------------
# 测试组
# ---------------------------------------------------------------------------


class TestExecutorFactoryAndDispatch:
    """工厂入参校验 + 工具 dispatch 行为（mock TOOL_REGISTRY，无 LLM）。"""

    def test_executor_factory_rejects_none_llm(self):
        """T27 fail-fast：llm=None 必须 raise（CLAUDE.md 规则 14）。"""
        from brain_base.nodes.qa_intent import create_intent_executor
        with pytest.raises(ValueError, match="non-None llm"):
            create_intent_executor(None, _empty_cfg())

    def test_executor_serial_single_action(self, monkeypatch):
        """len(next_actions)=1 → 串行调用单工具，返回 1 条 result。"""
        from brain_base.nodes import qa_tools
        from brain_base.nodes.qa_intent import create_intent_executor

        call_count = {"n": 0}

        async def _mock_tool(tool_args, llm, cfg):
            call_count["n"] += 1
            return {
                "markdown": f"# Mock result for {tool_args.get('query', '')}",
                "source_url": "https://mock.example.com/page1",
                "title": "Mock Page",
                "score": 75.0,
            }

        # 注入测试专用工具到 TOOL_REGISTRY（test 结束 monkeypatch 自动 undo）
        from brain_base.nodes.qa_tools import ToolSpec
        spec = ToolSpec(
            name="mock_tool_a",
            description="测试工具 A",
            requires=[],
            gpu=False,
            parallel_ok=True,
            is_async=True,
            fn=_mock_tool,
        )
        monkeypatch.setitem(qa_tools.TOOL_REGISTRY, "mock_tool_a", spec)

        node = create_intent_executor(_LLMSentinel(), _empty_cfg())
        state = {
            "current_intent_plan": {
                "next_actions": [
                    {
                        "tool_name": "mock_tool_a",
                        "tool_args": {"query": "hello"},
                        "purpose": "test serial",
                    },
                ],
                "reasoning": "test",
                "early_exit": False,
            },
        }
        out = _run(node(state))
        results = out["current_action_results"]
        assert len(results) == 1
        assert results[0]["tool_name"] == "mock_tool_a"
        assert results[0]["error"] == ""
        assert results[0]["markdown"].startswith("# Mock result for hello")
        assert results[0]["score"] == 75.0
        assert call_count["n"] == 1

    def test_executor_fanout_concurrent_with_isolation(self, monkeypatch):
        """len>1 → asyncio.gather 并发，单失败隔离（其他 actions 仍正常返回）。"""
        from brain_base.nodes import qa_tools
        from brain_base.nodes.qa_intent import create_intent_executor
        from brain_base.nodes.qa_tools import ToolSpec

        async def _ok_tool(tool_args, llm, cfg):
            await asyncio.sleep(0.01)  # 模拟 IO，验证并发不串行
            return {
                "markdown": f"ok-{tool_args.get('id', '?')}",
                "source_url": f"https://ok.example.com/{tool_args.get('id', '?')}",
                "title": f"OK {tool_args.get('id', '?')}",
                "score": 80.0,
            }

        async def _fail_tool(tool_args, llm, cfg):
            raise RuntimeError("simulated tool crash")

        monkeypatch.setitem(
            qa_tools.TOOL_REGISTRY, "mock_ok",
            ToolSpec(name="mock_ok", description="ok tool", requires=[], gpu=False,
                    parallel_ok=True, is_async=True, fn=_ok_tool),
        )
        monkeypatch.setitem(
            qa_tools.TOOL_REGISTRY, "mock_fail",
            ToolSpec(name="mock_fail", description="failing tool", requires=[], gpu=False,
                    parallel_ok=True, is_async=True, fn=_fail_tool),
        )

        node = create_intent_executor(_LLMSentinel(), _empty_cfg())
        state = {
            "current_intent_plan": {
                "next_actions": [
                    {"tool_name": "mock_ok", "tool_args": {"id": "1"}, "purpose": "p1"},
                    {"tool_name": "mock_fail", "tool_args": {}, "purpose": "p2"},
                    {"tool_name": "mock_ok", "tool_args": {"id": "3"}, "purpose": "p3"},
                ],
                "reasoning": "fan-out test",
                "early_exit": False,
            },
        }
        out = _run(node(state))
        results = out["current_action_results"]
        assert len(results) == 3, f"expected 3 results (1 fail isolated), got {len(results)}"

        # 按 purpose 索引（顺序应保持）
        by_purpose = {r["purpose"]: r for r in results}
        assert by_purpose["p1"]["error"] == ""
        assert by_purpose["p1"]["markdown"] == "ok-1"
        assert by_purpose["p3"]["error"] == ""
        assert by_purpose["p3"]["markdown"] == "ok-3"
        # 失败的那条 error 非空，markdown 仍为空（不污染正常结果）
        assert by_purpose["p2"]["error"]
        assert "RuntimeError" in by_purpose["p2"]["error"]
        assert "simulated tool crash" in by_purpose["p2"]["error"]
        assert by_purpose["p2"]["markdown"] == ""

    def test_executor_early_exit_returns_empty_results(self):
        """early_exit=True → 跳过执行，返回空 results。"""
        from brain_base.nodes.qa_intent import create_intent_executor

        node = create_intent_executor(_LLMSentinel(), _empty_cfg())
        state = {
            "current_intent_plan": {
                "next_actions": [],  # 即便有 actions 也会被忽略，但通常 planner 会清空
                "reasoning": "all evidence collected",
                "early_exit": True,
            },
        }
        out = _run(node(state))
        assert out == {"current_action_results": []}

    def test_executor_unknown_tool_name_returns_error_result(self):
        """next_actions 含未注册工具名 → result.error 非空，不抛。

        planner 已经过滤一道，但 executor 是第二道防线（防 plan dict 来自其他路径）。
        """
        from brain_base.nodes.qa_intent import create_intent_executor

        node = create_intent_executor(_LLMSentinel(), _empty_cfg())
        state = {
            "current_intent_plan": {
                "next_actions": [
                    {
                        "tool_name": "_definitely_not_a_real_tool_xxxx",
                        "tool_args": {"foo": "bar"},
                        "purpose": "unknown tool test",
                    },
                ],
                "reasoning": "test unknown",
                "early_exit": False,
            },
        }
        out = _run(node(state))
        results = out["current_action_results"]
        assert len(results) == 1
        r = results[0]
        assert r["error"]
        assert "unknown tool_name" in r["error"]
        assert r["markdown"] == ""
        assert r["tool_name"] == "_definitely_not_a_real_tool_xxxx"
        assert r["purpose"] == "unknown tool test"
