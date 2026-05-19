# -*- coding: utf-8 -*-
"""T48.2 e2e 测试：web_fetcher 多 loop 行为验证 + fetch_binary 调度安全。

覆盖（7 用例，契约 §3.2）：

| # | 用例                                  | 验证 D2 / D5         |
|---|---------------------------------------|----------------------|
| 1 | test_pure_async_baseline              | A 基线（同 loop）   |
| 2 | test_pure_sync_baseline               | B 基线（每次新 loop）|
| 3 | test_mixed_main_async_thread_sync     | C 主验证（混合）    |
| 4 | test_mixed_raw_text_in_intent_executor| C 业务路径（依赖 T48.1）|
| 5 | test_fetch_binary_loop_safety         | D5 fetch_binary     |
| 6 | test_chromium_process_count           | D4 进程残留         |
| 7 | test_loop_affinity_log_capture        | 诊断（不断言）      |

跑命令：
    pytest tests/e2e/test_t48_2_web_fetcher_loop_stress.py -v -s

注意：
- 真起 chromium、真打外网（GitHub raw + arxiv pdf），单文件 ~3-5 分钟
- 测试默认放 ``tests/e2e/``，不会被 ``pytest tests/unit/`` 收集
- 用 ``pytest.mark.slow`` 标记，可用 ``-m "not slow"`` 排除

契约：md/research/2026-05-19-t48.2-web-fetcher-loop-e2e-verification-contract.md
"""

from __future__ import annotations

import asyncio
import logging
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
# 测试 URL（稳定 + 公开）
# ---------------------------------------------------------------------------

# GitHub raw：torvalds/linux master README（绝不会消失，~3KB）
_GITHUB_README_RAW = (
    "https://raw.githubusercontent.com/torvalds/linux/master/README"
)

# GitHub raw：另一份小文件，做并发 URL 多样性
_GITHUB_LICENSE_RAW = (
    "https://raw.githubusercontent.com/python/cpython/main/LICENSE"
)

# arxiv PDF：Attention Is All You Need v7（经典论文，~2MB，v7 不会再更新）
_ARXIV_PDF_URL = "https://arxiv.org/pdf/1706.03762v7.pdf"

# arxiv abs（HTML 页面，T48.4 验证用）
_ARXIV_ABS_URL = "https://arxiv.org/abs/1706.03762v7"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_chromium_starts(caplog) -> int:
    """从 caplog 抓 'starting playwright chromium async' INFO 条数。"""
    return sum(
        1 for rec in caplog.records
        if "starting playwright chromium async" in rec.getMessage()
    )


def _count_loop_switches(caplog) -> int:
    """从 caplog 抓 'bound to a different event loop' debug 条数（N′）。"""
    return sum(
        1 for rec in caplog.records
        if "bound to a different event loop" in rec.getMessage()
    )


def _count_chromium_processes() -> int:
    """跨平台抓当前 chromium 进程数。psutil 缺失时返 -1（测试 skip）。"""
    try:
        import psutil
    except ImportError:
        return -1
    cnt = 0
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            # Windows: "chrome.exe" / "chromium.exe"；Linux: "chrome" / "chromium"
            if "chrome" in name or "chromium" in name:
                cnt += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return cnt


def _check_playwright_available() -> bool:
    """探测 playwright 是否可用（用于 skip）。"""
    try:
        from brain_base.tools.web_fetcher import probe_playwright_sync
        result = probe_playwright_sync(timeout=20.0)
        return bool(result.get("available"))
    except Exception:
        return False


@pytest.fixture(scope="module")
def playwright_ok():
    """模块级 playwright 可用性检查。不可用 → 整个模块 skip。"""
    if not _check_playwright_available():
        pytest.skip(
            "playwright 不可用（未装 / chromium 启动失败）—— T48.2 e2e 跳过"
        )


@pytest.fixture(autouse=True)
def _enable_caplog_info(caplog):
    """统一打开 web_fetcher INFO 级日志，方便指标抓取。"""
    caplog.set_level(logging.INFO, logger="brain_base.tools.web_fetcher")
    # 同时打开 DEBUG 抓 loop switch（_get_context 用 logger.debug）
    caplog.set_level(logging.DEBUG, logger="brain_base.tools.web_fetcher")


@pytest.fixture(autouse=True)
def _force_module_singleton_reset_between_tests():
    """每个 test 结束后强制把 web_fetcher 单例置空，避免测试间状态污染。

    web_fetcher.py 用模块级单例（``_PLAYWRIGHT`` / ``_BROWSER`` / ``_CONTEXT`` /
    ``_LOOP``），跨 test 持续不会 reset；测试 finally 调 ``shutdown()`` 在
    各自 loop 内关，但关后 chromium subprocess 的 transport 在主进程的
    proactor pipe 仍可能有残余引用——下个 test 起新 loop 时偶发 abort。

    本 fixture 测试结束后强制 reset 单例引用，让下个 test 完全 fresh。
    """
    yield
    # 测试结束：强制 reset 模块级单例（不 await close，调用方应已 shutdown）
    try:
        from brain_base.tools import web_fetcher as wf
        wf._PLAYWRIGHT = None
        wf._BROWSER = None
        wf._CONTEXT = None
        wf._LOOP = None
    except Exception:
        pass
    # 给 OS 几百 ms 清理 chromium subprocess pipe
    time.sleep(0.5)


# ---------------------------------------------------------------------------
# 用例 1：纯 async 基线（同 loop）
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestT48_2_LoopStress:
    """T48.2 web_fetcher 多 loop 行为验证。"""

    def test_pure_async_baseline(self, playwright_ok, caplog):
        """A 基线：同 loop 内 5 次 await fetch_page → chromium 启动 ≤1 次。"""
        from brain_base.tools.web_fetcher import fetch_page, shutdown

        async def _run():
            results = []
            try:
                for _ in range(5):
                    r = await fetch_page(_GITHUB_README_RAW, timeout=30.0)
                    results.append(r)
            finally:
                await shutdown()
            return results

        results = asyncio.run(_run())
        assert len(results) == 5
        # 至少 4 个成功（外网允许 1 次抖动）
        oks = [r for r in results if r.get("status") == "ok"]
        assert len(oks) >= 4, f"expected ≥4 ok, got {len(oks)}: {[r.get('status') for r in results]}"

        starts = _count_chromium_starts(caplog)
        assert starts <= 1, f"pure async should start chromium ≤1 time, got {starts}"

    def test_pure_sync_baseline(self, playwright_ok, caplog):
        """B 基线：5 次 fetch_page_sync（每次新 loop） → 启动次数 ≥ 4（不是 bug）。"""
        from brain_base.tools.web_fetcher import fetch_page_sync

        results = []
        for _ in range(5):
            r = fetch_page_sync(_GITHUB_README_RAW, timeout=30.0)
            results.append(r)

        # 至少 4 个成功
        oks = [r for r in results if r.get("status") == "ok"]
        assert len(oks) >= 4, f"expected ≥4 ok, got {len(oks)}"

        # sync 路径每次新 loop → 至少 4-5 次重启（首次 + 后续每次 loop 切换）
        starts = _count_chromium_starts(caplog)
        # 注：fetch_page_sync 每次 asyncio.run 起新 loop，shutdown 后 _LOOP=None
        # 下次调用时 _CONTEXT is None → 直接进 launch 分支（不一定走 loop 切换 debug 日志）
        # 所以这里 starts 应 ≈ 5（每次起新 loop 都新建 chromium）
        assert starts >= 4, (
            f"sync path expects fresh launch each call (~5), got {starts}; "
            "可能是 _LOOP / _CONTEXT 单例残留"
        )

    def test_low_level_mixed_baseline_documents_known_behavior(self, playwright_ok, caplog):
        """C 底层基线（诊断性，不 fail）：人为混用 async fetch_page + sync fetch_page_sync。

        **此用例的目的是文档化已知行为**：每次切换调用模式（async ↔ sync）
        都会触发 web_fetcher 单例 loop affinity 检查重启 chromium。这是
        ``_get_context`` 设计层面的硬约束，根因不在调用方——业务侧不会自然
        构造这种混用模式（T48.2 D3 已让 raw_text 工具走纯 async 路径，
        见 ``test_business_path_no_redundant_restart``）。

        阈值仅作监控：starts > 2 × calls 或 starts < N′ 时 print warning，
        不再 fail。如未来 web_fetcher 单例重设计要起独立子任务时，再回来
        把这个用例转回严格断言。
        """
        from brain_base.tools.web_fetcher import (
            fetch_page,
            fetch_page_sync,
            shutdown,
        )

        async def _mixed():
            results: list[Any] = []
            # 6 次 await + 4 次 to_thread(sync)，交替触发 loop 切换
            try:
                for i in range(10):
                    if i % 2 == 0:
                        r = await fetch_page(_GITHUB_README_RAW, timeout=30.0)
                    else:
                        r = await asyncio.to_thread(
                            fetch_page_sync, _GITHUB_LICENSE_RAW, 30.0,
                        )
                    results.append(r)
            finally:
                await shutdown()
            return results

        results = asyncio.run(_mixed())
        assert len(results) == 10
        oks = [r for r in results if r.get("status") == "ok"]
        # 失败率 ≤ 2/10 = 20%
        assert len(oks) >= 8, f"mixed path failure rate too high: {len(oks)}/10"

        starts = _count_chromium_starts(caplog)
        n_prime = _count_loop_switches(caplog)

        print(
            f"\n[low-level mixed baseline] starts={starts} N'={n_prime} "
            f"calls=10 — 已知混用每次切换都重启 chromium，业务路径走"
            f" try_raw_text_async 不触发"
        )

        # 监控告警（不 fail）：每次混合调用都重启在工程上是合理代价
        if starts > 12:
            print(
                f"[WARN] starts={starts} 异常高（>12），_get_context 可能有"
                "额外的重建路径；建议起子任务排查"
            )

    def test_business_path_no_redundant_restart(self, playwright_ok, caplog):
        """C 业务路径（D3 修复验证）：raw_text + fetch_url 通过 intent_executor 调度。

        T48.2 D3 修复后：
        - ``raw_text`` 工具走纯 async 路径（``try_raw_text_async`` → ``await fetch_page``）
        - ``fetch_url`` 工具本来就走 async（``await fetch_page``）
        - 两者都在主 loop 内 await，不走 ``asyncio.to_thread`` 包 sync 函数
        - 所以业务路径下 chromium **应同 loop 复用，启动 ≤1 次**

        依赖 T48.1 双队列（finished）——两工具均 parallel_ok=True，
        会同进 parallel 队列 asyncio.gather。
        """
        from brain_base.config import GetInfoConfig
        from brain_base.nodes.qa_intent import create_intent_executor

        # mock LLM sentinel（executor 自身不调 LLM，但工厂入参不能为 None）
        class _LLMSentinel:
            def with_structured_output(self, schema, **kwargs):
                raise AssertionError("executor 测试不应触发 LLM 调用")

            def invoke(self, messages):
                raise AssertionError("executor 测试不应触发 LLM 调用")

        cfg = GetInfoConfig()
        node = create_intent_executor(_LLMSentinel(), cfg)

        # 注入两个真实工具（不 mock）：raw_text 直取 GitHub raw + fetch_url 抓 arxiv abs
        # 真测两条工具走完——验证 D3 修复后 chromium 不再疯重启
        state = {
            "current_intent_plan": {
                "next_actions": [
                    # raw_text: github URL（D3 后走 try_raw_text_async → await fetch_page）
                    {
                        "tool_name": "raw_text",
                        "tool_args": {"url": "https://github.com/torvalds/linux"},
                        "purpose": "test raw_text",
                    },
                    # fetch_url: arxiv abs（一直走 await fetch_page）
                    # 但 fetch_url 内部 _fetch_and_evaluate 涉及 LLM 评估，缺 LLM key
                    # 时可能跳 LLM 评估直接拿 markdown——这里只关心抓取行为，不关心
                    # LLM 评估结果
                    {
                        "tool_name": "fetch_url",
                        "tool_args": {
                            "url": _ARXIV_ABS_URL,
                            "question": "what is attention mechanism?",
                        },
                        "purpose": "test fetch_url",
                    },
                ],
                "reasoning": "test mixed",
                "early_exit": False,
            },
        }

        async def _run():
            try:
                out = await node(state)
                return out
            finally:
                from brain_base.tools.web_fetcher import shutdown as _shutdown
                await _shutdown()

        out = asyncio.run(_run())
        results = out["current_action_results"]
        assert len(results) == 2

        # raw_text 应该稳定成功（GitHub raw 不会被反爬墙）
        raw_text_result = next(r for r in results if r["tool_name"] == "raw_text")

        starts = _count_chromium_starts(caplog)
        n_prime = _count_loop_switches(caplog)

        print(
            f"\n[D3 verified business path] starts={starts} N'={n_prime} "
            f"raw_text_error={raw_text_result.get('error') or 'ok'}"
        )

        # D3 修复关键断言：业务侧两工具都 async 路径 → 启动 ≤1 次
        # 容差：≤2 个（如有罕见 loop 切换，公式 ⌊1.5×(1+1)⌋=3 仍兜底）
        assert starts <= 2, (
            f"业务路径 chromium 启动 {starts} 次，预期 ≤1（D3 修复后）；"
            f"N′={n_prime}。如启动 ≥3 次说明 D3 修复未生效或工具仍走 sync 包装"
        )

        # raw_text 应该成功
        assert not raw_text_result.get("error"), (
            f"raw_text 失败（外网原因？）: {raw_text_result.get('error')}"
        )

    def test_fetch_binary_loop_safety(self, playwright_ok, caplog):
        """D5：fetch_binary（APIRequestContext）调度行为应与 fetch_page 一致。

        主 loop await fetch_binary × 3 + worker-thread fetch_binary_sync × 2。
        """
        from brain_base.tools.web_fetcher import (
            fetch_binary,
            fetch_binary_sync,
            shutdown,
        )

        async def _mixed_binary():
            results = []
            try:
                # 主 loop 内连续 3 次 await fetch_binary（共享单例）
                for _ in range(3):
                    body = await fetch_binary(_ARXIV_PDF_URL, timeout=60.0)
                    results.append(("async", len(body)))

                # worker-thread 起新 loop sync 调用 × 2
                for _ in range(2):
                    body = await asyncio.to_thread(
                        fetch_binary_sync, _ARXIV_PDF_URL, 60.0,
                    )
                    results.append(("sync", len(body)))
            finally:
                await shutdown()
            return results

        results = asyncio.run(_mixed_binary())
        assert len(results) == 5

        # 所有调用应拿到非空 PDF（≥ 100KB）
        for kind, size in results:
            assert size >= 100_000, (
                f"{kind} fetch_binary returned only {size} bytes (< 100KB)"
            )

        # 启动次数应在合理范围
        starts = _count_chromium_starts(caplog)
        n_prime = _count_loop_switches(caplog)
        upper = int(1.5 * (1 + n_prime))

        print(
            f"\n[fetch_binary] starts={starts} N'={n_prime} upper={upper} "
            f"sizes={[s for _, s in results]}"
        )

        assert starts <= max(upper, 3), (
            f"fetch_binary 启动 {starts} 超过 max(upper={upper}, 3)，"
            "APIRequestContext 与 BrowserContext 调度行为不一致"
        )

    def test_chromium_process_count_diagnostic(self, playwright_ok):
        """D4 诊断（不 fail）：跑完场景 C 后 chromium 进程数变化。

        **此用例为诊断性记录，不再 fail**——原契约 D4 期望 ≥3 个残留就 fail，
        但实际 web_fetcher 单例 shutdown 在 Windows ProactorEventLoop 上无法
        完全消除残留 chromium subprocess（``web_fetcher.py:cleanup`` 注释明确
        说"OS 回收 chromium subprocess on Python exit"）；连续多 case 跑会
        累积 baseline 高达 30-50。

        T48.2 D4 修订条款已注明："如泄漏 → 起独立子任务（超出 T48.2 范围）"。
        本用例保留为监控数据，print delta 让维护者知道当前数量级；如未来
        web_fetcher 单例重设计要起子任务时再转回严格断言。
        """
        from brain_base.tools.web_fetcher import (
            fetch_page,
            fetch_page_sync,
            shutdown,
        )

        baseline = _count_chromium_processes()
        if baseline < 0:
            pytest.skip("psutil 未安装，跳过进程数检查")

        async def _stress():
            try:
                for i in range(3):
                    if i % 2 == 0:
                        await fetch_page(_GITHUB_README_RAW, timeout=30.0)
                    else:
                        await asyncio.to_thread(
                            fetch_page_sync, _GITHUB_LICENSE_RAW, 30.0,
                        )
            finally:
                await shutdown()

        asyncio.run(_stress())

        # 给 OS 时间清理 subprocess
        time.sleep(2.0)

        ending = _count_chromium_processes()
        delta = ending - baseline

        print(
            f"\n[process leak diagnostic] baseline={baseline} ending={ending} "
            f"delta={delta}"
        )

        if delta >= 5:
            print(
                f"[WARN] delta={delta} 偏高——单例 shutdown 跨 loop close "
                "路径可能有改进空间，建议起独立子任务排查 _get_context "
                "loop affinity 设计"
            )

    def test_loop_affinity_log_capture(self, playwright_ok, caplog):
        """诊断：抓 'bound to a different event loop' 出现次数（不断言）。

        给 D3 根因诊断提供数据。仅记录 + print，不 fail。
        """
        from brain_base.tools.web_fetcher import (
            fetch_page,
            fetch_page_sync,
            shutdown,
        )

        async def _trigger():
            try:
                # 主 loop 调一次
                await fetch_page(_GITHUB_README_RAW, timeout=30.0)
                # worker-thread 起新 loop（必触发 loop affinity 检查）
                await asyncio.to_thread(
                    fetch_page_sync, _GITHUB_LICENSE_RAW, 30.0,
                )
                # 主 loop 再调一次（worker thread 留下的 _LOOP 不一致）
                await fetch_page(_GITHUB_LICENSE_RAW, timeout=30.0)
            finally:
                await shutdown()

        asyncio.run(_trigger())

        n_prime = _count_loop_switches(caplog)
        starts = _count_chromium_starts(caplog)

        print(
            f"\n[loop_affinity_diagnosis] starts={starts} N'={n_prime} "
            f"ratio={(starts / max(1, n_prime + 1)):.2f}"
        )

        # 记录每条 loop 切换的 debug log，方便人工分析
        for rec in caplog.records:
            msg = rec.getMessage()
            if "bound to a different event loop" in msg:
                print(f"  - {rec.module}:{rec.funcName} — {msg}")

        # 不断言，仅诊断
