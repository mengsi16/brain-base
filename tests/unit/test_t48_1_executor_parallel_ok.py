# -*- coding: utf-8 -*-
"""T48.1 单元测试：intent_executor 双队列调度（parallel_ok 串行化）。

覆盖（6 条，契约 §5）：
- test_all_parallel_concurrent：全 parallel_ok=True → asyncio.gather 真并发（时间窗重叠）
- test_all_serial_strict_order：全 parallel_ok=False → for-loop 真串行（时间窗不重叠）
- test_mixed_serial_parallel_idx_alignment：混合 [S, P, S, P] → results idx 严格对齐 input
- test_single_action_does_not_split：单 action 不进双队列分支（_is_serial_action 不被调）
- test_unknown_tool_goes_to_parallel：未注册工具归 parallel + 返 error 不抛 KeyError
- test_two_serial_tools_strict_sequence：2 个 serial 工具 → 第二个 start ≥ 第一个 end

mock 策略：纯执行层调度测试，不真调 LLM（CLAUDE.md 规则 14 豁免——本测试验证
*调度拓扑* 而非 LLM 语义）。用 _LLMSentinel + monkeypatch TOOL_REGISTRY 注入
带 sleep 的 fake fn 控制时间窗口断言。

契约：md/research/2026-05-19-t48.1-intent-executor-parallel-serialization-contract.md
"""

from __future__ import annotations

import asyncio
import time
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
# Helpers
# ---------------------------------------------------------------------------


class _LLMSentinel:
    """不被调用的 LLM 占位（executor 自身不调 LLM）。"""

    def with_structured_output(self, schema, **kwargs):
        raise AssertionError("executor 测试不应触发 LLM 调用")

    def invoke(self, messages):
        raise AssertionError("executor 测试不应触发 LLM 调用")


def _run(coro):
    return asyncio.run(coro)


def _empty_cfg():
    from brain_base.config import GetInfoConfig
    return GetInfoConfig()


def _make_timed_async_tool(
    name: str,
    duration: float,
    timing_log: list[tuple[str, float, float]],
):
    """构造一个 async fake tool：记录 (name, start_ts, end_ts) 到 timing_log。"""

    async def _fn(tool_args, llm, cfg):
        start = time.perf_counter()
        await asyncio.sleep(duration)
        end = time.perf_counter()
        timing_log.append((name, start, end))
        return {
            "markdown": f"# {name} done",
            "source_url": f"https://mock.example.com/{name}",
            "title": name,
            "score": 80.0,
        }

    return _fn


def _register_tool(
    monkeypatch,
    name: str,
    fn: Any,
    *,
    parallel_ok: bool,
    is_async: bool = True,
):
    """把 fake ToolSpec 注入 TOOL_REGISTRY（test 自动 undo）。"""
    from brain_base.nodes import qa_tools
    from brain_base.nodes.qa_tools import ToolSpec

    spec = ToolSpec(
        name=name,
        description=f"test tool {name}",
        requires=[],
        gpu=False,
        parallel_ok=parallel_ok,
        is_async=is_async,
        fn=fn,
    )
    monkeypatch.setitem(qa_tools.TOOL_REGISTRY, name, spec)


def _ts_overlap(a: tuple[str, float, float], b: tuple[str, float, float]) -> bool:
    """两个时间窗 [start, end] 是否有交集。"""
    return not (a[2] <= b[1] or b[2] <= a[1])


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestT48_1_ExecutorDoubleQueue:
    """T48.1 双队列调度测试（按契约 §5 6 用例）。"""

    # ------------------------------------------------------------------
    # 1. 全 parallel：真并发
    # ------------------------------------------------------------------

    def test_all_parallel_concurrent(self, monkeypatch):
        """3 actions 全 parallel_ok=True → 时间窗口重叠 = 真并发。

        results 顺序断言：与 actions 顺序严格对齐（gather 保序）。
        """
        from brain_base.nodes.qa_intent import create_intent_executor

        timing: list[tuple[str, float, float]] = []
        for nm in ("p1", "p2", "p3"):
            _register_tool(
                monkeypatch, nm,
                _make_timed_async_tool(nm, 0.1, timing),
                parallel_ok=True,
            )

        node = create_intent_executor(_LLMSentinel(), _empty_cfg())
        state = {
            "current_intent_plan": {
                "next_actions": [
                    {"tool_name": "p1", "tool_args": {}, "purpose": "P1"},
                    {"tool_name": "p2", "tool_args": {}, "purpose": "P2"},
                    {"tool_name": "p3", "tool_args": {}, "purpose": "P3"},
                ],
                "reasoning": "test",
                "early_exit": False,
            },
        }

        wall_start = time.perf_counter()
        out = _run(node(state))
        wall_end = time.perf_counter()
        wall_dur = wall_end - wall_start

        results = out["current_action_results"]
        # 顺序对齐
        assert [r["tool_name"] for r in results] == ["p1", "p2", "p3"]
        assert [r["purpose"] for r in results] == ["P1", "P2", "P3"]

        # 真并发：总耗时应 < 单工具 duration × 2（理想 ~0.1s，<0.2s 留 100% 容差）
        assert wall_dur < 0.2, f"expected concurrent (<0.2s), got {wall_dur:.3f}s"

        # 时间窗口至少存在两两重叠
        assert len(timing) == 3
        # 至少 p1 与 p2 应重叠
        assert _ts_overlap(timing[0], timing[1]), \
            f"expected overlap, got {timing}"

    # ------------------------------------------------------------------
    # 2. 全 serial：真串行
    # ------------------------------------------------------------------

    def test_all_serial_strict_order(self, monkeypatch):
        """2 actions 全 parallel_ok=False → 时间窗口无重叠 = 真串行。"""
        from brain_base.nodes.qa_intent import create_intent_executor

        timing: list[tuple[str, float, float]] = []
        for nm in ("s1", "s2"):
            _register_tool(
                monkeypatch, nm,
                _make_timed_async_tool(nm, 0.1, timing),
                parallel_ok=False,
            )

        node = create_intent_executor(_LLMSentinel(), _empty_cfg())
        state = {
            "current_intent_plan": {
                "next_actions": [
                    {"tool_name": "s1", "tool_args": {}, "purpose": "S1"},
                    {"tool_name": "s2", "tool_args": {}, "purpose": "S2"},
                ],
                "reasoning": "test",
                "early_exit": False,
            },
        }

        wall_start = time.perf_counter()
        out = _run(node(state))
        wall_end = time.perf_counter()
        wall_dur = wall_end - wall_start

        results = out["current_action_results"]
        # 顺序对齐
        assert [r["tool_name"] for r in results] == ["s1", "s2"]

        # 真串行：总耗时 ≥ 2 × 单工具 duration × 0.9（双 0.1s = 0.2s，下限 0.18s）
        assert wall_dur >= 0.18, f"expected serial (>=0.18s), got {wall_dur:.3f}s"

        # 时间窗口无重叠
        assert len(timing) == 2
        # timing 顺序保证（serial for-loop 顺序记录）
        s1, s2 = timing[0], timing[1]
        assert s1[0] == "s1" and s2[0] == "s2"
        # s1.end ≤ s2.start（容差 1ms）
        assert s1[2] <= s2[1] + 0.001, \
            f"expected no overlap, got s1={s1}, s2={s2}"

    # ------------------------------------------------------------------
    # 3. 混合 serial + parallel：idx 严格对齐
    # ------------------------------------------------------------------

    def test_mixed_serial_parallel_idx_alignment(self, monkeypatch):
        """混合 [S_a, P_b, S_c, P_d] → 4 results idx 与 input 严格对齐。

        - serial 队列内部串行（S_a 与 S_c 时间窗不重叠）
        - parallel 队列内部并发（P_b 与 P_d 时间窗重叠）
        - 跨队列时间窗可重叠（serial 与 parallel 流水线 asyncio.gather 同时跑）
        """
        from brain_base.nodes.qa_intent import create_intent_executor

        timing: list[tuple[str, float, float]] = []
        _register_tool(monkeypatch, "S_a",
                       _make_timed_async_tool("S_a", 0.1, timing),
                       parallel_ok=False)
        _register_tool(monkeypatch, "P_b",
                       _make_timed_async_tool("P_b", 0.1, timing),
                       parallel_ok=True)
        _register_tool(monkeypatch, "S_c",
                       _make_timed_async_tool("S_c", 0.1, timing),
                       parallel_ok=False)
        _register_tool(monkeypatch, "P_d",
                       _make_timed_async_tool("P_d", 0.1, timing),
                       parallel_ok=True)

        node = create_intent_executor(_LLMSentinel(), _empty_cfg())
        state = {
            "current_intent_plan": {
                "next_actions": [
                    {"tool_name": "S_a", "tool_args": {}, "purpose": "p0"},
                    {"tool_name": "P_b", "tool_args": {}, "purpose": "p1"},
                    {"tool_name": "S_c", "tool_args": {}, "purpose": "p2"},
                    {"tool_name": "P_d", "tool_args": {}, "purpose": "p3"},
                ],
                "reasoning": "test",
                "early_exit": False,
            },
        }
        out = _run(node(state))
        results = out["current_action_results"]

        # 关键断言：idx 严格对齐
        assert len(results) == 4
        assert [r["tool_name"] for r in results] == ["S_a", "P_b", "S_c", "P_d"]
        assert [r["purpose"] for r in results] == ["p0", "p1", "p2", "p3"]

        # 找到 S_a / S_c 时间窗 → 不重叠
        timing_by_name = {t[0]: t for t in timing}
        s_a = timing_by_name["S_a"]
        s_c = timing_by_name["S_c"]
        p_b = timing_by_name["P_b"]
        p_d = timing_by_name["P_d"]

        assert not _ts_overlap(s_a, s_c), \
            f"S_a 和 S_c 应串行不重叠：{s_a} vs {s_c}"
        # P_b 与 P_d 应重叠（同 parallel 流水线）
        assert _ts_overlap(p_b, p_d), \
            f"P_b 和 P_d 应并发重叠：{p_b} vs {p_d}"

    # ------------------------------------------------------------------
    # 4. 单 action 不进双队列分支
    # ------------------------------------------------------------------

    def test_single_action_does_not_split(self, monkeypatch):
        """len(actions)=1 → 不调 _is_serial_action（不进双队列分支）。

        通过 spy _is_serial_action 调用计数验证：单 action 直接 await
        _execute_one，零调用；fan-out 路径才会调用。
        """
        from brain_base.nodes import qa_intent
        from brain_base.nodes.qa_intent import create_intent_executor

        call_count = {"n": 0}
        original_is_serial = qa_intent._is_serial_action

        def _spy_is_serial(action):
            call_count["n"] += 1
            return original_is_serial(action)

        monkeypatch.setattr(qa_intent, "_is_serial_action", _spy_is_serial)

        timing: list[tuple[str, float, float]] = []
        _register_tool(monkeypatch, "single_t",
                       _make_timed_async_tool("single_t", 0.05, timing),
                       parallel_ok=False)  # 即使 serial，单 action 也不应分队列

        node = create_intent_executor(_LLMSentinel(), _empty_cfg())
        state = {
            "current_intent_plan": {
                "next_actions": [
                    {"tool_name": "single_t", "tool_args": {}, "purpose": "p0"},
                ],
                "reasoning": "test",
                "early_exit": False,
            },
        }
        out = _run(node(state))
        results = out["current_action_results"]

        assert len(results) == 1
        assert results[0]["tool_name"] == "single_t"
        # 关键：单 action 路径未触发 _is_serial_action
        assert call_count["n"] == 0, \
            f"single action should not call _is_serial_action, got {call_count['n']} calls"

    # ------------------------------------------------------------------
    # 5. 未注册工具归 parallel + 不抛 KeyError
    # ------------------------------------------------------------------

    def test_unknown_tool_goes_to_parallel(self, monkeypatch):
        """混合 [unknown, registered_parallel] → 不抛 KeyError，未注册返 error。

        D2 修订关键：_is_serial_action 用 dict.get(...) 安全访问。
        """
        from brain_base.nodes.qa_intent import create_intent_executor

        timing: list[tuple[str, float, float]] = []
        _register_tool(monkeypatch, "good_p",
                       _make_timed_async_tool("good_p", 0.05, timing),
                       parallel_ok=True)

        node = create_intent_executor(_LLMSentinel(), _empty_cfg())
        state = {
            "current_intent_plan": {
                "next_actions": [
                    {"tool_name": "_unregistered_xxx", "tool_args": {}, "purpose": "u0"},
                    {"tool_name": "good_p", "tool_args": {}, "purpose": "u1"},
                ],
                "reasoning": "test",
                "early_exit": False,
            },
        }
        # 关键：不应抛 KeyError
        out = _run(node(state))
        results = out["current_action_results"]
        assert len(results) == 2

        # 顺序对齐
        assert results[0]["tool_name"] == "_unregistered_xxx"
        assert results[1]["tool_name"] == "good_p"

        # 未注册返 error
        assert results[0]["error"]
        assert "unknown tool_name" in results[0]["error"]

        # 已注册正常返
        assert not results[1]["error"]
        assert results[1]["markdown"] == "# good_p done"

    # ------------------------------------------------------------------
    # 6. 2 个 serial 工具严格顺序
    # ------------------------------------------------------------------

    def test_two_serial_tools_strict_sequence(self, monkeypatch):
        """fan-out 含 2 个 parallel_ok=False 工具 → 第二个 start ≥ 第一个 end。

        模拟 T48.3 上线后 LLM 同跳吐 [arxiv_pdf(A), arxiv_pdf(B)] 的关键场景：
        必须串行排队不并发触发 OOM。
        """
        from brain_base.nodes.qa_intent import create_intent_executor

        timing: list[tuple[str, float, float]] = []
        # 两个 serial 工具 + 一个 parallel 干扰项，确认串行流水线内部严格顺序
        _register_tool(monkeypatch, "gpu_a",
                       _make_timed_async_tool("gpu_a", 0.1, timing),
                       parallel_ok=False)
        _register_tool(monkeypatch, "gpu_b",
                       _make_timed_async_tool("gpu_b", 0.1, timing),
                       parallel_ok=False)
        _register_tool(monkeypatch, "ws_c",
                       _make_timed_async_tool("ws_c", 0.05, timing),
                       parallel_ok=True)

        node = create_intent_executor(_LLMSentinel(), _empty_cfg())
        state = {
            "current_intent_plan": {
                "next_actions": [
                    {"tool_name": "gpu_a", "tool_args": {}, "purpose": "A"},
                    {"tool_name": "gpu_b", "tool_args": {}, "purpose": "B"},
                    {"tool_name": "ws_c", "tool_args": {}, "purpose": "C"},
                ],
                "reasoning": "test",
                "early_exit": False,
            },
        }
        out = _run(node(state))
        results = out["current_action_results"]

        # idx 对齐
        assert [r["tool_name"] for r in results] == ["gpu_a", "gpu_b", "ws_c"]

        # gpu_a / gpu_b 严格顺序
        timing_by_name = {t[0]: t for t in timing}
        gpu_a = timing_by_name["gpu_a"]
        gpu_b = timing_by_name["gpu_b"]
        assert gpu_a[2] <= gpu_b[1] + 0.001, \
            f"gpu_a 应在 gpu_b 之前结束：a={gpu_a}, b={gpu_b}"
        # 总耗时 ≥ 2 × 0.1s × 0.9 = 0.18s（serial 主导）
        gpu_total = max(gpu_b[2], gpu_a[2]) - gpu_a[1]
        assert gpu_total >= 0.18, \
            f"两个 serial 应至少耗 0.18s，实际 {gpu_total:.3f}s"


class TestT48_1_LoggingAndIntegration:
    """T48.1 日志埋点 + 与 T47 已有行为的回归保护。"""

    def test_fanout_log_includes_serial_and_parallel_lists(self, monkeypatch, caplog):
        """fan-out INFO 日志应包含 serial=[...] 和 parallel=[...] 字段（D3）。"""
        import logging

        from brain_base.nodes.qa_intent import create_intent_executor

        timing: list[tuple[str, float, float]] = []
        _register_tool(monkeypatch, "log_s",
                       _make_timed_async_tool("log_s", 0.02, timing),
                       parallel_ok=False)
        _register_tool(monkeypatch, "log_p",
                       _make_timed_async_tool("log_p", 0.02, timing),
                       parallel_ok=True)

        node = create_intent_executor(_LLMSentinel(), _empty_cfg())
        state = {
            "current_intent_plan": {
                "next_actions": [
                    {"tool_name": "log_s", "tool_args": {}, "purpose": "s"},
                    {"tool_name": "log_p", "tool_args": {}, "purpose": "p"},
                ],
                "reasoning": "test",
                "early_exit": False,
            },
        }

        with caplog.at_level(logging.INFO, logger="brain_base.nodes.qa_intent"):
            _run(node(state))

        # 找 fan-out 日志
        fanout_msgs = [
            rec.getMessage() for rec in caplog.records
            if "intent_executor fan-out" in rec.getMessage()
        ]
        assert fanout_msgs, "fan-out INFO log not found"
        msg = fanout_msgs[0]
        assert "serial=" in msg and "parallel=" in msg
        assert "log_s" in msg
        assert "log_p" in msg
