"""
web 抓取与搜索引擎封装（全走 playwright-cli，不依赖 trafilatura）。

调用约束（CLAUDE.md 项目硬约束）：
- 22：Amazon 不走 Cloudflare（playwright-cli 抓 amazon.com 不要带任何
  Cloudflare 解决参数，浪费时间）。
- 28：Windows 下外部子进程优先用 subprocess.Popen（asyncio 在 Windows
  上可能抛 NotImplementedError）。
- 25：fail-fast，不在本层吞错——subprocess 失败抛 RuntimeError，由
  graph 层接住并写 degraded_reason。

抓取策略：
- fetch_page(url)：playwright-cli run-code 加载页面，取 page.content() / innerText / title。
  SPA / 静态页统一一条路，起浏览器成本高但成功率高。
- search_google / search_bing：SERP 普遍需 JS 渲染，同样走 playwright-cli。
以前的 trafilatura 静态路径已下架：它在 SPA 页抓不到有效内容但会返回
"空骨架"伪成功，让下游误判 status=ok；提取接口 trafilatura.extract 质量低于
MinerU-HTML，也不适合作为降级。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from functools import lru_cache
from typing import Any

_PLAYWRIGHT_BIN = "playwright-cli"
_SPA_TIMEOUT = 60  # 秒，SPA 渲染允许更久


@lru_cache(maxsize=1)
def _resolve_playwright_path() -> str | None:
    """解析 playwright-cli 完整路径。

    Windows 下安装为 .CMD，subprocess 直接传 'playwright-cli' 会
    抛 FileNotFoundError，必须用 shutil.which 拿到带后缀的完整路径。
    """
    return shutil.which(_PLAYWRIGHT_BIN)


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------


def probe_playwright(timeout: float = 5.0) -> dict[str, Any]:
    """探测 playwright-cli 是否可用。"""
    bin_path = _resolve_playwright_path()
    if bin_path is None:
        return {"available": False, "error": f"{_PLAYWRIGHT_BIN} 不在 PATH 中"}
    try:
        proc = subprocess.run(
            [bin_path, "--help"],
            capture_output=True,
            timeout=timeout,
            text=True,
            encoding="utf-8",
        )
        return {
            "available": proc.returncode == 0,
            "path": bin_path,
            "stderr": proc.stderr[:200] if proc.returncode != 0 else "",
        }
    except subprocess.TimeoutExpired:
        return {"available": False, "error": f"playwright-cli --help 超时 {timeout}s"}


# ---------------------------------------------------------------------------
# 单页抓取
# ---------------------------------------------------------------------------


def fetch_page(
    url: str,
    timeout: float | None = None,
    **_legacy: Any,
) -> dict[str, Any]:
    """抓取单个 URL，返回 {url, html, text, title, status, error}。

    全走 playwright-cli run-code：加载页面 → page.content() / innerText / title。
    不再提供静态 trafilatura 路径，避免 SPA 页拿到空骨架伪成功。

    Args:
        url: 要抓的 URL。
        timeout: 超时秒数，默认 _SPA_TIMEOUT。
        **_legacy: 接住以前调用方传的 render_js / 其他参数以保证向后兼容，忑视。
    """
    return _fetch_dynamic(url, timeout=timeout or _SPA_TIMEOUT)


def _fetch_dynamic(url: str, timeout: float) -> dict[str, Any]:
    """playwright-cli 动态抓取：用 run-code 一次性加载并取 innerHTML / title。"""
    ms = int(timeout * 1000)
    code = (
        "async (page) => {"
        " await page.goto(" + repr(url) + ", { waitUntil: 'domcontentloaded', timeout: " + str(ms) + " });"
        " await page.waitForTimeout(2500);"
        " const title = await page.title();"
        " const html = await page.content();"
        " const text = await page.evaluate(() => document.body ? document.body.innerText : '');"
        " return JSON.stringify({title, html, text});"
        "}"
    )

    raw = _run_playwright(["run-code", code, "--raw"], timeout=timeout + 30)
    payload = _parse_json_or_empty(raw)
    if not payload:
        return {
            "url": url,
            "html": "",
            "text": "",
            "title": "",
            "status": "spa_failed",
            "error": f"playwright-cli run-code 输出无法解析: {raw[:200]}",
        }
    return {
        "url": url,
        "html": payload.get("html", ""),
        "text": payload.get("text", ""),
        "title": payload.get("title", ""),
        "status": "ok" if payload.get("text") else "empty",
        "error": "",
    }


# ---------------------------------------------------------------------------
# 搜索引擎
# ---------------------------------------------------------------------------


def search_google(query: str, num_results: int = 10) -> list[dict[str, Any]]:
    """Google 搜索结果。返回 [{url, title, snippet}, ...]。

    注：Google 对 playwright 自动化检测严格，常见 0 结果。优先用 search_bing。
    """
    return _serp_via_playwright(
        url=f"https://www.google.com/search?q={_quote(query)}&num={num_results}",
        result_selector="div.g, div[data-sokoban-container], div.MjjYud",
        link_selector="a[href]",
        title_selector="h3",
        snippet_selector="div[data-sncf], div.VwiC3b, span.aCOpRe",
    )


def search_bing(query: str, num_results: int = 10) -> list[dict[str, Any]]:
    """Bing 搜索结果（cn.bing.com + ensearch=1，国内可用）。返回 [{url, title, snippet}, ...]。"""
    return _serp_via_playwright(
        url=f"https://cn.bing.com/search?q={_quote(query)}&ensearch=1&count={num_results}",
        result_selector="li.b_algo",
        link_selector="h2 a",
        title_selector="h2",
        snippet_selector="div.b_caption p, p.b_lineclamp4",
    )


def _serp_via_playwright(
    url: str,
    result_selector: str,
    link_selector: str,
    title_selector: str,
    snippet_selector: str,
) -> list[dict[str, Any]]:
    """通用 SERP 解析：playwright-cli 渲染后用 querySelectorAll 抽取。

    等待策略：domcontentloaded + waitForSelector（有些站 networkidle 永远不触发）。
    """
    # 用字符串拼接避免 .format 与 JS 花括号冲突；selectors 已是字符串字面量直接拼。
    # 解码 Bing ck/a 跳转链接：u 参数前缀 a1 后是 base64 url-safe 编码的真实 URL。
    js = (
        "async (page) => {"
        " await page.goto(" + repr(url) + ", { waitUntil: 'domcontentloaded', timeout: 30000 });"
        " try { await page.waitForSelector(" + repr(result_selector) + ", { timeout: 15000 }); } catch (e) {}"
        " await page.waitForTimeout(1500);"
        " const items = await page.evaluate((sel) => {"
        "  function decodeBingCk(href) {"
        "   try {"
        "    const m = href.match(/[?&]u=a1([^&]+)/);"
        "    if (!m) return href;"
        "    let b = m[1].replace(/-/g, '+').replace(/_/g, '/');"
        "    while (b.length % 4) b += '=';"
        "    return atob(b);"
        "   } catch (e) { return href; }"
        "  }"
        "  const out = [];"
        "  const blocks = document.querySelectorAll(sel.result);"
        "  blocks.forEach(b => {"
        "    const a = b.querySelector(sel.link);"
        "    const t = b.querySelector(sel.title);"
        "    const s = b.querySelector(sel.snippet);"
        "    if (a && a.href) {"
        "      let realUrl = a.href;"
        "      if (realUrl.indexOf('bing.com/ck/a') !== -1) realUrl = decodeBingCk(realUrl);"
        "      out.push({"
        "        url: realUrl,"
        "        title: t ? t.innerText.trim() : '',"
        "        snippet: s ? s.innerText.trim() : ''"
        "      });"
        "    }"
        "  });"
        "  return out;"
        " }, {"
        "  result: " + repr(result_selector) + ","
        "  link: " + repr(link_selector) + ","
        "  title: " + repr(title_selector) + ","
        "  snippet: " + repr(snippet_selector)
        + " });"
        " return JSON.stringify(items);"
        "}"
    )
    raw = _run_playwright(["run-code", js, "--raw"], timeout=90)
    items = _parse_json_or_empty(raw)
    if not isinstance(items, list):
        return []
    return [
        {"url": it.get("url", ""), "title": it.get("title", ""), "snippet": it.get("snippet", "")}
        for it in items
        if it.get("url")
    ]


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


_BROWSER_OPENED = False


def _ensure_browser_open(bin_path: str) -> None:
    """playwright-cli 的 run-code/goto 等命令需要先 open，否则报
    "The browser 'default' is not open"。本进程内只 open 一次。
    """
    global _BROWSER_OPENED
    if _BROWSER_OPENED:
        return
    try:
        subprocess.run(
            [bin_path, "open"],
            capture_output=True,
            timeout=30,
            text=True,
            encoding="utf-8",
        )
    except Exception:
        # open 失败也不阻断；run-code 会再次报错由上层接住
        pass
    _BROWSER_OPENED = True


def _run_playwright(args: list[str], timeout: float) -> str:
    """统一封装 subprocess 调 playwright-cli。fail-fast。

    自动确保浏览器已 open；遇到 "not open" 错误自动重试一次。
    """
    bin_path = _resolve_playwright_path()
    if bin_path is None:
        raise RuntimeError("playwright-cli 不在 PATH，无法抓取")

    _ensure_browser_open(bin_path)

    def _call() -> subprocess.CompletedProcess:
        return subprocess.run(
            [bin_path, *args],
            capture_output=True,
            timeout=timeout,
            text=True,
            encoding="utf-8",
        )

    proc = _call()
    if proc.returncode != 0:
        # stderr 可能是空（playwright-cli 把错误打到 stdout）
        err = (proc.stderr or proc.stdout or "").strip()
        if "not open" in err.lower() or "please run open" in err.lower():
            # 强制再 open 一次后重试
            global _BROWSER_OPENED
            _BROWSER_OPENED = False
            _ensure_browser_open(bin_path)
            proc = _call()
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(
                f"playwright-cli {args[0]} 失败 rc={proc.returncode} err={err[:500]}"
            )
    return proc.stdout


def _parse_json_or_empty(raw: str) -> Any:
    """run-code --raw 输出可能就是 JSON 字符串本身，也可能被引号包裹。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    # run-code 的返回值是 JSON 字符串，外层 stdout 可能再包一层
    if isinstance(parsed, str):
        try:
            return json.loads(parsed)
        except json.JSONDecodeError:
            return None
    return parsed


def _quote(s: str) -> str:
    """简单 URL 编码（只处理空格和常见保留字符）。"""
    from urllib.parse import quote_plus

    return quote_plus(s)
