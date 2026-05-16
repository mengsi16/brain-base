"""web 抓取与搜索引擎封装（playwright **async** API，反检测）。

调用约束（CLAUDE.md 项目硬约束）：
- 22：Amazon 不走 Cloudflare（不要带任何 Cloudflare 解决参数，浪费时间）。
- 25：fail-fast，不在本层吞错——除显示声明 try-except 外异常上抛由 graph 层接住。
- 26：调试 HTML 解析先读 raw HTML（_save_snapshot 把每次抓取的 page.content() 落盘）。

抓取策略：
- 模块级 ``async_playwright`` 单例 + 反检测 chromium context（首次调用 lazy 启动）；
  反检测：navigator.webdriver=undefined / window.chrome / plugins / languages /
  hardware concurrency / 删 cdc_* / launch args 加 --disable-blink-features=AutomationControlled。
- async 单 event loop 单线程，同 context 并发开多 page 由 playwright 内部调度，不需 threading.Lock。
- 单例绑定创建时的 event loop；loop 变了 (多次 ``asyncio.run``) 需要重建。
- atexit 则 best-effort 释放；loop 可能已关，只置 None，chromium 子进程随 Python 进程退出被 OS 清理。
- BB_PLAYWRIGHT_HEADLESS 环境变量控制 headless（默认 **False / 有头**）；
  显式设 "1"/"true"/"yes"/"on" → True（无头，CI / 服务器场景）；
  其他值 / 缺失 → False（有头，开发调试默认）。
  原因：Google / Cloudflare 等对 headless 检测极严格，默认有头降低反爬命中率。
  也可调用方传 ``headless=`` 参数显式覆盖。

公共接口（T29 playwright sync→async 迁移：主接口全 async，包调用方另提供 sync 包装）：
- ``async fetch_page(url) -> {url, html, text, title, status, error}``
- ``async search_google(query, num_results, page) -> [{url, title, snippet}, ...]``
- ``async search_bing(query, num_results, page) -> [{url, title, snippet}, ...]``
- ``async probe_playwright() -> {available: bool, error?: str}``
- 同步包装（给 IngestUrlGraph / _probe.py 等 sync 调用方）：``fetch_page_sync`` / ``probe_playwright_sync``
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 反检测 stealth 脚本（来自 NanmiCoder/CrawlerTutorial 反检测教程）
# ---------------------------------------------------------------------------

_STEALTH_JS = r"""
// 隐藏 webdriver 标志
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined
});

// 模拟 Chrome 对象
window.chrome = {
    runtime: {},
    loadTimes: function() {},
    csi: function() {},
    app: {}
};

// 模拟正常的插件列表
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const plugins = [
            { name: 'Chrome PDF Plugin', description: 'Portable Document Format', filename: 'internal-pdf-viewer' },
            { name: 'Chrome PDF Viewer', description: '', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
            { name: 'Native Client', description: '', filename: 'internal-nacl-plugin' }
        ];
        plugins.item = (i) => plugins[i];
        plugins.namedItem = (name) => plugins.find(p => p.name === name);
        plugins.refresh = () => {};
        return plugins;
    }
});

// 模拟语言设置（中文优先 + 英文回退）
Object.defineProperty(navigator, 'languages', {
    get: () => ['zh-CN', 'zh', 'en-US', 'en']
});

// 修复 permissions API（自动化探针常用此接口判定 webdriver）
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);

// 模拟硬件并发数 / 设备内存
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

// 隐藏 ChromeDriver 探针属性
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
"""


# ---------------------------------------------------------------------------
# launch / context 反检测配置
# ---------------------------------------------------------------------------

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-infobars",
    "--window-size=1920,1080",
]

# Windows Chrome UA（与项目运行环境一致；不用 Mac UA 避免 sec-ch-ua 平台头不一致触发检测）
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_VIEWPORT = {"width": 1920, "height": 1080}
_LOCALE = "zh-CN"
_TIMEZONE = "Asia/Shanghai"
_EXTRA_HEADERS = {"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}

_DEFAULT_TIMEOUT_MS = 60_000  # 默认 page 操作超时 60s


# ---------------------------------------------------------------------------
# 全局单例（async event loop 级）
# ---------------------------------------------------------------------------

# 模块级 lazy 单例（首次调用 _get_context 时启动，atexit 时 best-effort 释放）
# async 架构下playwright 实例绑定创建它的 event loop；多次 ``asyncio.run`` 会起
# 不同 loop，跳 loop 复用旧实例会报 ``... is bound to a different event loop``。
# 解法：_get_context 内 ``_LOOP`` 一致性检查，不一致则重建。
_PLAYWRIGHT: Any = None
_BROWSER: Any = None
_CONTEXT: Any = None
_LOOP: asyncio.AbstractEventLoop | None = None

# 调试断点 once flag：BB_DEBUG_PAUSE_GOOGLE=1 时第一次 search_google 后断点，
# 之后置 True 不再触发（避免每次 google 都暂停）。async 单 loop 单线程无竞态。
_DEBUG_GOOGLE_PAUSED_ONCE = False


def _resolve_headless(override: bool | None = None) -> bool:
    """决定 headless 模式（默认有头，Google 无头检测严格）。

    优先级：override 参数 > BB_PLAYWRIGHT_HEADLESS 环境变量 > 默认 False。
    显式设 "1" / "true" / "yes" / "on" → True（无头，服务器 / CI）；
    其他值 / 缺失 → False（有头，默认）。
    """
    if override is not None:
        return bool(override)
    raw = os.environ.get("BB_PLAYWRIGHT_HEADLESS", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    return False


async def _get_context(headless: bool | None = None) -> Any:
    """获取（必要时启动）反检测 chromium context（async 版）。

    loop affinity 检查：playwright 实例绑定创建它的 event loop；如果当前
    running loop 与实例绑定的 ``_LOOP`` 不一致（典型场景：多次 ``asyncio.run``
    各起新 loop），旧实例不能跨 loop 复用 —— 置空重建。

    headless: 仅启动 / 重建时生效；后续调用同 loop 复用已有 context（playwright 不
    支持动态切 headless，要切必须重启进程）。
    """
    global _PLAYWRIGHT, _BROWSER, _CONTEXT, _LOOP
    current_loop = asyncio.get_running_loop()
    if _CONTEXT is not None and _LOOP is current_loop:
        return _CONTEXT
    if _CONTEXT is not None and _LOOP is not current_loop:
        # loop 变了，旧实例不可用；best-effort 取消引用 (不 await close，原 loop 可能已关)
        logger.debug(
            "playwright context bound to a different event loop, dropping and rebuilding"
        )
        _PLAYWRIGHT = _BROWSER = _CONTEXT = None
        _LOOP = None

    from playwright.async_api import async_playwright

    is_headless = _resolve_headless(headless)
    logger.info(
        "starting playwright chromium async | headless=%s ua=%s",
        is_headless, _USER_AGENT,
    )
    _PLAYWRIGHT = await async_playwright().start()
    _BROWSER = await _PLAYWRIGHT.chromium.launch(
        headless=is_headless,
        args=_LAUNCH_ARGS,
    )
    _CONTEXT = await _BROWSER.new_context(
        viewport=_VIEWPORT,
        locale=_LOCALE,
        timezone_id=_TIMEZONE,
        user_agent=_USER_AGENT,
        extra_http_headers=_EXTRA_HEADERS,
    )
    # 反检测脚本：每个新 page 都自动注入
    await _CONTEXT.add_init_script(_STEALTH_JS)
    _CONTEXT.set_default_timeout(_DEFAULT_TIMEOUT_MS)
    _LOOP = current_loop
    atexit.register(_cleanup)
    return _CONTEXT


async def shutdown() -> None:
    """主动关 playwright（在 event loop 还活着时调用，推荐用法）。

    Windows ProactorEventLoop 下，asyncio.run() 退出后让 GC 关 chromium subprocess
    transport 会触发 ``__del__`` → 已关 pipe → ``Exception ignored: I/O operation
    on closed pipe`` 满屏噪音。必须在 loop 关闭前主动 ``await`` 关所有 playwright
    对象，让 subprocess transport 在 loop 内正常关。

    loop affinity：只关绑定当前 running loop 的实例；不同 loop 的实例跳过（那
    loop 已关无法 await），留给 ``_cleanup()`` 置 None 由 OS 回收。

    调用方：QA / IngestUrl / GetInfo 等图在 asyncio.run 主协程的 finally 中调用。
    """
    global _PLAYWRIGHT, _BROWSER, _CONTEXT, _LOOP
    if _PLAYWRIGHT is None:
        return  # 没启动过，nop
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        # 不在 running loop 里调用（误用），让 _cleanup atexit 兜底
        return
    if _LOOP is not None and _LOOP is not current_loop:
        # 实例绑到其他 loop，跨 loop close 会抛；置空让 GC 自己处理
        logger.debug("shutdown: playwright bound to different loop, skipping close")
        _PLAYWRIGHT = _BROWSER = _CONTEXT = None
        _LOOP = None
        return
    try:
        if _CONTEXT is not None:
            await _CONTEXT.close()
        if _BROWSER is not None:
            await _BROWSER.close()
        if _PLAYWRIGHT is not None:
            await _PLAYWRIGHT.stop()
        logger.debug("playwright shutdown ok")
    except Exception as exc:
        # 规则 25：关闭失败只 warning，不抛——此时业务已出结果，shutdown 失败顶多
        # 留僵尸 subprocess 给 OS 回收，不应反过来污染业务返回值
        logger.warning(
            "playwright shutdown failed | err=%s: %s",
            type(exc).__name__, str(exc)[:200],
        )
    finally:
        _PLAYWRIGHT = _BROWSER = _CONTEXT = None
        _LOOP = None


async def _with_shutdown(coro: Any) -> Any:
    """sync 包装器用的 helper：``await coro`` + finally ``await shutdown()``。

    所有 ``XXX_sync`` / ``asyncio.run`` 短入口都要走这个 helper 包一层：

    ``asyncio.run(_with_shutdown(fetch_page(url)))``

    这样主协程 finally 会在 event loop 关闭前主动关 playwright subprocess，
    避免 Windows ProactorEventLoop GC 触发 ``Exception ignored: I/O operation
    on closed pipe`` 满屏噪音。
    """
    try:
        return await coro
    finally:
        await shutdown()


def _cleanup() -> None:
    """进程退出时释放 playwright（atexit 兜底，async 调用方应优先用 ``shutdown()``）。

    async 架构下 playwright 实例绑定创建它的 event loop；进程退出阶段原 loop
    大概率已关闭，无法调 ``await xxx.close()``。直接置 None 让 GC，chromium
    子进程随 Python 进程退出被 OS 清理（playwright 启的子进程主线程退出会跟着退）。

    Windows ProactorEventLoop 已知问题：此时 GC 触发 transport.__del__ 会试图读已关
    pipe fd 抛 ``ValueError: I/O operation on closed pipe`` 被 Python 标记为
    ``Exception ignored``——不影响进程 exit code 但会在 stderr 输出满屏红色。解法：
    调用方在 loop 内主动 ``await shutdown()`` 让实例变 None，本函数走到这里即 nop。
    """
    global _PLAYWRIGHT, _BROWSER, _CONTEXT, _LOOP
    try:
        if _PLAYWRIGHT is not None:
            logger.debug(
                "playwright cleanup: skip async close (event loop likely closed), "
                "OS will reclaim chromium subprocess on Python exit"
            )
    except Exception as exc:
        # 规则 25：保留 try-except 必须打日志；atexit 阶段失败 OS 会清理子进程，debug 级足够
        logger.debug(
            "playwright cleanup unexpected | err=%s: %s",
            type(exc).__name__, str(exc)[:160],
        )
    finally:
        _PLAYWRIGHT = _BROWSER = _CONTEXT = None
        _LOOP = None


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------


async def probe_playwright(timeout: float = 30.0) -> dict[str, Any]:
    """探测 playwright 是否可用：尝试启动 chromium 一次。

    复用模块级单例——探测成功后 context 已经准备好，后续 fetch_page /
    search_* 可以直接用，不必再次启动。

    timeout 仅用作语义参数，实际由 playwright 自身控制；浏览器启动通常 ≤5s。
    """
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError as exc:
        return {"available": False, "error": f"playwright 未安装: {exc}"}
    try:
        await _get_context()
        return {"available": True}
    except Exception as exc:
        # 规则 25：保留 try-except 必须打日志；探测失败要让上层知道具体原因
        logger.warning(
            "probe_playwright failed | err=%s: %s",
            type(exc).__name__, str(exc)[:200],
        )
        return {
            "available": False,
            "error": f"启动失败: {type(exc).__name__}: {str(exc)[:200]}",
        }


def probe_playwright_sync(timeout: float = 30.0) -> dict[str, Any]:
    """同步版 ``probe_playwright`` 包装，给同步调用方用（_probe.py / cli inspect-config）。

    内部 ``asyncio.run(_with_shutdown(probe_playwright(...)))``：主协程 finally 主动
    关 playwright，避免 Windows ProactorEventLoop GC 阶段触发 ``Exception ignored:
    I/O operation on closed pipe`` 满屏噪音。调测 / 探测 场景调用频次低，
    每次故重启 chromium 可接受。
    """
    try:
        return asyncio.run(_with_shutdown(probe_playwright(timeout=timeout)))
    except Exception as exc:
        # 规则 25：保留 try-except 必须打日志；asyncio.run 外层失败同样要提示上层
        logger.warning(
            "probe_playwright_sync failed | err=%s: %s",
            type(exc).__name__, str(exc)[:200],
        )
        return {
            "available": False,
            "error": f"启动失败: {type(exc).__name__}: {str(exc)[:200]}",
        }


# ---------------------------------------------------------------------------
# 抓取快照归档
# ---------------------------------------------------------------------------

# 抓取快照归档目录（每次 playwright 抓取都强制落盘留档，方便调试看反爬触发 / selector 失效）
# 这不是命中-跳过的 cache，业务照样每次重新爬；这里只做单向 dump。
# 文件按日期分目录，单文件命名 {ts}_{kind}_{slug}_{hash6}.json，避免同秒同 key 覆盖。
_SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / "data" / "cache" / "web_fetcher"


def _save_snapshot(kind: str, key: str, payload: dict[str, Any]) -> None:
    """把一次 playwright 抓取的输入 + raw + parsed 落盘，纯归档不读回。

    kind 取 ``'fetch_page'`` / ``'serp_google'`` / ``'serp_bing'``；
    key 用于文件名 slug（url 或 query），同时也写在文件内 ``key`` 字段。

    存盘失败只 warning 不抛——快照归档不应阻断业务（playwright 本体的成功/失败
    才是业务关心的事，归档侧出错不应让调用方崩）。
    """
    try:
        ts_full = time.strftime("%Y%m%d_%H%M%S")
        date_dir = time.strftime("%Y-%m-%d")
        # 文件名 slug：URL/query 中非字母数字一律换 -，截 60 字符；纯 ASCII 安全
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", key).strip("-")[:60] or "no-slug"
        # 同秒同 key 多次抓时用 hash6 + 微秒级唯一后缀避免覆盖
        h = hashlib.sha256(f"{ts_full}::{key}::{time.time_ns()}".encode("utf-8")).hexdigest()[:6]
        d = _SNAPSHOT_DIR / date_dir
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{ts_full}_{kind}_{slug}_{h}.json"
        f.write_text(
            json.dumps(
                {
                    "timestamp": ts_full,
                    "kind": kind,
                    "key": key,
                    **payload,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info(
            "web_fetcher snapshot saved | kind=%s key=%r file=%s",
            kind, key[:80], f.name,
        )
    except Exception as exc:
        # 规则 25：保留 try-except 必须打日志；归档失败不阻断业务
        logger.warning(
            "web_fetcher snapshot save FAILED | kind=%s key=%r err=%s: %s",
            kind, key[:80], type(exc).__name__, str(exc)[:160],
        )


# ---------------------------------------------------------------------------
# 单页抓取
# ---------------------------------------------------------------------------


async def fetch_page(
    url: str,
    timeout: float | None = None,
    **_legacy: Any,
) -> dict[str, Any]:
    """抓取单个 URL（async），返回 ``{url, html, text, title, status, error}``。

    走反检测 chromium context 起 page，goto + wait 2.5s 让 SPA 渲染稳定，
    取 title / page.content() / body.innerText。失败时 status="spa_failed"
    带 error 字段，html/text 为空字符串——保签名兼容老调用方对 status 的分支。

    **legacy: 接住以前调用方传的 render_js / 其他参数以保证向后兼容，忽视。
    """
    timeout_ms = int((timeout or 60.0) * 1000)
    title = ""
    html = ""
    text = ""
    status = "spa_failed"
    error = ""

    ctx = await _get_context()
    page = await ctx.new_page()
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            # SPA 首屏稳定 1.0s（domcontentloaded 后还要等 React/Vue mount）
            await page.wait_for_timeout(1000)

            # === auto-scroll 循环：触发 lazy-load / 无限滚动的 XHR ===
            # 现代 SPA 大量使用 IntersectionObserver 懒加载，不滚到底就根本不发 XHR；
            # 每轮滚一屏 + 等 800ms 让 XHR 触发并渲染，scrollHeight 不再增长视为到底。
            scroll_rounds_used = 0
            prev_height = 0
            for round_idx in range(8):  # 最多 8 轮 ≈ 6.4s 上限
                cur_height = await page.evaluate(
                    "() => document.body ? document.body.scrollHeight : 0"
                )
                if cur_height <= prev_height:
                    # 高度无变化 → 已到底或无懒加载，提前结束
                    break
                await page.evaluate(f"window.scrollTo(0, {cur_height})")
                prev_height = cur_height
                scroll_rounds_used = round_idx + 1
                await page.wait_for_timeout(800)

            # === networkidle 收尾：让最后一批 XHR 完成 ===
            # 部分站点（聊天/心跳/广告轮询）永远不 idle，这里 5s 超时降级容忍
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception as exc:
                # 规则 25：networkidle 超时是预期场景（不阻断后续抓取），仅 debug 记录
                logger.debug(
                    "fetch_page networkidle timeout (tolerated) | url=%s err=%s",
                    url, type(exc).__name__,
                )

            # 滚回顶部（保险：某些站 lazy-render 在 viewport 外的元素不挂 DOM）
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(300)

            title = await page.title()
            html = await page.content()
            text = await page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            )
            status = "ok" if text else "empty"
            error = ""
            logger.info(
                "fetch_page ok | url=%s html_len=%d text_len=%d scroll_rounds=%d",
                url, len(html), len(text), scroll_rounds_used,
            )
        except Exception as exc:
            # 规则 25：保留 try-except 必须打日志；单 URL 失败由调用方决定降级
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.warning("fetch_page failed | url=%s err=%s", url, error)

        # 归档：成败都落盘（reading raw html 是诊断反爬/selector 的最快路径）
        # 同步 IO 短阻塞 (<10ms json.dump)，async 函数内直接调可接受
        _save_snapshot(
            kind="fetch_page",
            key=url,
            payload={
                "url": url,
                "title": title,
                "html_len": len(html),
                "html_snippet": html[:8000],
                "text_len": len(text),
                "text_preview": text[:5000],
                "status": status,
                "error": error,
            },
        )
        return {
            "url": url,
            "html": html,
            "text": text,
            "title": title,
            "status": status,
            "error": error,
        }
    finally:
        try:
            await page.close()
        except Exception as exc:
            # 规则 25：page.close 失败不阻断（main result 已有），只记录便于排查泄漏
            logger.warning(
                "fetch_page page.close failed | url=%s err=%s: %s",
                url, type(exc).__name__, str(exc)[:160],
            )


def fetch_page_sync(
    url: str,
    timeout: float | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """同步版 ``fetch_page`` 包装，给 IngestUrlGraph 等同步图节点用。

    内部 ``asyncio.run(_with_shutdown(fetch_page(...)))``：每次调用起新 event loop，
    finally 主动关 playwright subprocess 避免 Windows GC 噪音。同步调用方多为单点
    (IngestUrlGraph.fetch_node) 调一次，不在主图 fan-out 内，~5s 启动开销可接受。
    **主图 (QaGraph) 全程 async，不要走这个 sync 包装**——主图 async 节点内调 sync
    包装会触发 nested loop 报错。
    """
    return asyncio.run(_with_shutdown(fetch_page(url, timeout=timeout, **kwargs)))


# ---------------------------------------------------------------------------
# 搜索引擎
# ---------------------------------------------------------------------------


async def search_google(query: str, num_results: int = 10, page: int = 1) -> list[dict[str, Any]]:
    """Google 搜索结果（async）。返回 ``[{url, title, snippet}, ...]``。

    Args:
        query: 搜索关键词。
        num_results: 单页期望结果数（Google ``num`` 参数；实际可能少于此值）。
        page: 页码（1-based，默认第 1 页）；page>1 时拼 ``&start=(page-1)*num_results`` 翻页。

    反检测 chromium 配合 stealth 脚本能绕过大多数 google 自动化检测；若仍被
    sorry/unusual_traffic 页面拦截，返回 [] 由调用方降级到 bing。
    """
    base = f"https://www.google.com/search?q={_quote(query)}&num={num_results}"
    start = (page - 1) * num_results
    if start > 0:
        base += f"&start={start}"
    return await _serp(
        url=base,
        result_selector="div.g, div[data-sokoban-container], div.MjjYud",
        link_selector="a[href]",
        title_selector="h3",
        snippet_selector="div[data-sncf], div.VwiC3b, span.aCOpRe",
        kind="serp_google",
        query=query,
    )


async def search_bing(query: str, num_results: int = 10, page: int = 1) -> list[dict[str, Any]]:
    """Bing 搜索结果（cn.bing.com + ensearch=1，国内可用，async）。返回 ``[{url, title, snippet}, ...]``。

    Args:
        query: 搜索关键词。
        num_results: 单页期望结果数（Bing ``count`` 参数）。
        page: 页码（1-based，默认第 1 页）；page>1 时拼 ``&first=(page-1)*num_results+1`` 翻页。
    """
    base = (
        f"https://cn.bing.com/search?q={_quote(query)}&ensearch=1&count={num_results}"
    )
    first = (page - 1) * num_results + 1
    if first > 1:
        base += f"&first={first}"
    return await _serp(
        url=base,
        result_selector="li.b_algo",
        link_selector="h2 a",
        title_selector="h2",
        snippet_selector="div.b_caption p, p.b_lineclamp4",
        kind="serp_bing",
        query=query,
    )


# 浏览器端 SERP 解析脚本：用 querySelectorAll 抽取 + Bing ck/a 跳转链解码
_SERP_EXTRACT_JS = r"""
(sel) => {
    function decodeBingCk(href) {
        try {
            const m = href.match(/[?&]u=a1([^&]+)/);
            if (!m) return href;
            let b = m[1].replace(/-/g, '+').replace(/_/g, '/');
            while (b.length % 4) b += '=';
            return atob(b);
        } catch (e) { return href; }
    }
    const out = [];
    const blocks = document.querySelectorAll(sel.result);
    blocks.forEach(b => {
        const a = b.querySelector(sel.link);
        const t = b.querySelector(sel.title);
        const s = b.querySelector(sel.snippet);
        if (a && a.href) {
            let realUrl = a.href;
            if (realUrl.indexOf('bing.com/ck/a') !== -1) realUrl = decodeBingCk(realUrl);
            out.push({
                url: realUrl,
                title: t ? t.innerText.trim() : '',
                snippet: s ? s.innerText.trim() : ''
            });
        }
    });
    return out;
}
"""


async def _serp(
    url: str,
    result_selector: str,
    link_selector: str,
    title_selector: str,
    snippet_selector: str,
    kind: str,
    query: str,
) -> list[dict[str, Any]]:
    """通用 SERP 解析（async）：playwright 渲染后用 querySelectorAll 抽取。

    等待策略：domcontentloaded + wait_for_selector（有些站 networkidle 永远不触发）。
    抓取过程包含 page_title / body_text / html_snippet 用于反爬调试归档。
    """
    page_title = ""
    body_text = ""
    html_snippet = ""
    items: list = []
    error = ""

    ctx = await _get_context()
    page = await ctx.new_page()
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_selector(result_selector, timeout=15_000)
            except Exception:
                # selector 等不到（反爬页 / 空结果 / 站点改版）—— 不抛，
                # 继续用 evaluate 拿 fallback 内容（page_title / body_text 帮助诊断）
                pass
            await page.wait_for_timeout(1500)
            page_title = await page.title()
            body_text = await page.evaluate(
                "() => document.body ? document.body.innerText.slice(0, 5000) : ''"
            )
            html_snippet = await page.evaluate(
                "() => document.body ? document.body.innerHTML.slice(0, 8000) : ''"
            )
            items = await page.evaluate(
                _SERP_EXTRACT_JS,
                {
                    "result": result_selector,
                    "link": link_selector,
                    "title": title_selector,
                    "snippet": snippet_selector,
                },
            )
            if not isinstance(items, list):
                items = []
        except Exception as exc:
            # 规则 25：保留 try-except 必须打日志；单 SERP 失败由 search_web_dual 聚合层决定
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.warning(
                "serp goto/extract failed | kind=%s query=%r err=%s",
                kind, query, error,
            )

        # 归档（每次 SERP 抓取都落盘，方便看反爬触发 / selector 失效）
        # 同步 IO 短阻塞 (<10ms json.dump)，async 函数内直接调可接受
        _save_snapshot(
            kind=kind,
            key=query or url,
            payload={
                "url": url,
                "query": query,
                "result_selector": result_selector,
                "page_title": page_title,
                "body_text_preview": body_text,
                "html_snippet": html_snippet,
                "items_count": len(items),
                "items": items,
                "error": error,
            },
        )
        result_list = [
            {
                "url": it.get("url", ""),
                "title": it.get("title", ""),
                "snippet": it.get("snippet", ""),
            }
            for it in items
            if isinstance(it, dict) and it.get("url")
        ]
        return result_list
    finally:
        # 调试断点：第一次 search_google 完成后保留 page + 阻塞等回车，
        # 让用户切到 chromium 窗口看 google 实际显示（验证反爬触发模式）。
        # async 架构下没有 _LOCK，其他 task 会继续跑；但 google 这个 page 未 close，用户可以在
        # chromium 窗口切到 google tab 看。input() 是阻塞 IO，包 ``await asyncio.to_thread``
        # 避免阻塞 event loop。
        global _DEBUG_GOOGLE_PAUSED_ONCE
        should_pause = (
            kind == "serp_google"
            and not _DEBUG_GOOGLE_PAUSED_ONCE
            and os.environ.get("BB_DEBUG_PAUSE_GOOGLE", "").strip().lower() in ("1", "true", "yes")
        )
        if should_pause:
            _DEBUG_GOOGLE_PAUSED_ONCE = True
            print(
                f"\n[DEBUG] search_google 完成 | query={query!r} items_count={len(items)}",
                file=sys.stderr, flush=True,
            )
            print(
                "[DEBUG] google page 已保留（未 close），其他 task 继续并发跑；"
                "可在 chromium 窗口切到 google tab 看实际显示。",
                file=sys.stderr, flush=True,
            )
            print(
                "[DEBUG] 看完后回到终端按 Enter 继续...",
                file=sys.stderr, flush=True,
            )
            try:
                # input() 是阻塞 IO：to_thread 派到独立 thread，event loop 继续转
                await asyncio.to_thread(input)
            except (EOFError, KeyboardInterrupt):
                # 规则 25：保留 try-except 必须打日志；用户 Ctrl+C / EOF 视为继续
                logger.warning(
                    "debug pause interrupted | kind=%s query=%r",
                    kind, query,
                )
        try:
            await page.close()
        except Exception as exc:
            # 规则 25：page.close 失败不阻断（main result 已有），只记录便于排查泄漏
            logger.warning(
                "serp page.close failed | kind=%s query=%r err=%s: %s",
                kind, query, type(exc).__name__, str(exc)[:160],
            )


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _quote(s: str) -> str:
    """简单 URL 编码（只处理空格和常见保留字符）。"""
    return quote_plus(s)
