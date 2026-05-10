"""原生 playwright async 单 URL preview 抓取（T16）。

设计要点：
- **每次 fetch_preview 独立启动 chromium**：asyncio.run() 每次新 event loop，
  跨 loop 共享 Browser/Lock 会报错；单候选 4-6s 启动开销可接受。
- **N 个 Send 并行 = N 个 chromium 进程并行**：约 200MB / 进程，10 候选 = 2GB，
  桌面机器可承受；后续若做服务化再考虑 browser pool。
- **fail-safe**：异常一律回填 CandidatePreview(fetched=False, error=...)，
  不抛错——让上游 preview_score_one 节点能给该候选打 0 分继续 fan-in。
  规则 25（fail-fast）适用于"不该被吞的内部错误"；这里 fetch 失败是预期外部
  故障，必须降级返回，否则一个 URL 的网络问题会拖垮整个 fan-out。

CLAUDE.md 规则 28（Windows asyncio subprocess 优先 subprocess.Popen）
**不适用**——原生 playwright 自带 Node 端事件循环 + 浏览器进程管理，
不依赖 ``asyncio.create_subprocess_exec``。
"""

from __future__ import annotations

from playwright.async_api import async_playwright

from brain_base.agents.schemas import CandidatePreview

# 取前 1000 字 innerText（schema preview_text max=1200，留 200 字裕量给 schema 校验）
_PREVIEW_TEXT_LIMIT = 1000


async def fetch_preview(url: str, timeout: float = 15.0) -> CandidatePreview:
    """单 URL 抓取精简 snapshot：title + 首个 h1/h2 + 前 1000 字 innerText。

    Args:
        url: 候选 URL，必须以 http:// 或 https:// 开头
        timeout: 单页 goto 超时秒数（默认 15s）

    Returns:
        CandidatePreview。抓取失败时 fetched=False，error 字段记录原因。
    """
    if not url or not url.startswith(("http://", "https://")):
        return CandidatePreview(url=url, fetched=False, error="invalid url")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(timeout * 1000),
                )
                # 给 SPA 一点时间渲染
                await page.wait_for_timeout(800)
                title = (await page.title()) or ""
                heading = (
                    await page.evaluate(
                        "() => { const h = document.querySelector('h1, h2');"
                        "  return h ? h.innerText : ''; }"
                    )
                ) or ""
                inner_text = (
                    await page.evaluate(
                        "() => document.body ? document.body.innerText.slice(0, "
                        + str(_PREVIEW_TEXT_LIMIT)
                        + ") : ''"
                    )
                ) or ""
            finally:
                await browser.close()
    except Exception as exc:  # noqa: BLE001 — 见模块 docstring，需要兜底降级
        return CandidatePreview(url=url, fetched=False, error=str(exc)[:200])

    return CandidatePreview(
        url=url,
        fetched=True,
        title=title[:300],
        heading=heading[:300],
        preview_text=inner_text[: _PREVIEW_TEXT_LIMIT + 200],
    )
