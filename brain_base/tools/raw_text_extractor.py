"""通用 raw text 提取路径（T20；T48.0 抓取层迁 Playwright）。

针对 GitHub / GitLab / arxiv abs / RFC 等"能直接拿到结构化纯文本"的站点，
做一次"短链路"拉取——绕开 MinerU-HTML 全流程（后者对长文档 >16K token 会被
``BB_MINERU_HTML_MAX_INPUT`` 硬截断，实测 GitHub README 1304 行被切到 617 行
丢 53%）。

T48.0 后所有网页抓取必须走 ``brain_base/tools/web_fetcher.py`` 的 Playwright
单例 + stealth + auto-scroll 反爬方案——不再用 ``urllib.request`` 直 GET。
本模块只负责 URL 路由 / README 探测矩阵 / arxiv meta 解析，HTTP 拉取一律
委托给 ``fetch_page_sync``。

raw.githubusercontent.com / rfc-editor.org 返回 text/plain 时，chromium 会渲染
为 ``<html><body><pre>...原文...</pre></body></html>``；通过
``page.evaluate(() => document.body.innerText)`` 即可拿到纯文本，不会被 MinerU
截断（fetch_page 不走 MinerU，直接 ``page.content()`` / ``innerText``）。

设计：
- ``try_raw_text(url)`` 返回 ``dict | None``，命中即返回带 markdown 的 dict
- 不命中 / 抓取失败 → 返回 ``None``，调用方走原 playwright + MinerU 流程
- 不抛异常：raw text 是优化路径，失败必须静默降级到主路径
- 仍保持 sync 签名，调用方零改动；内部 ``fetch_page_sync`` 用 ``asyncio.run``
  包一层 playwright async 入口；调用方在 async 上下文需经 ``asyncio.to_thread``
  包装（``qa_url_pre_fetch`` / ``qa_tools.execute_raw_text`` 已如此）
"""

from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 通用配置
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = 30.0
"""单次 fetch_page 的超时秒数；playwright 启 chromium 比 urllib 慢得多，
默认 30s 给 SPA 渲染 + auto-scroll 留出空间。"""

# GitHub / GitLab 仓库根 URL 时，按顺序尝试这些 README 文件名
_README_VARIANTS = ("README.md", "README_zh.md", "README_en.md", "readme.md")
_BRANCH_VARIANTS = ("main", "master")


# ---------------------------------------------------------------------------
# HTTP GET 封装（T48.0：底层走 web_fetcher.fetch_page_sync）
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str]:
    """通过 Playwright 拉取 URL，返回 ``(status_code, body_text)``。

    T48.0 改造：以前用 ``urllib.request.urlopen`` 裸 HTTP GET，新规则要求所有
    网页信息获取统一走 ``web_fetcher.fetch_page_sync``（带 stealth JS / 真实 UA /
    auto-scroll / 默认有头）。

    返回语义保持与旧实现一致：
    - 抓取成功且正文非空 → ``(200, body)``
    - fetch_page 报错（``status="spa_failed"``、``error`` 字段非空）→ ``(500, "")``
      让上层 handler 像旧 ``HTTPError`` 一样视为失败但不抛
    - 仅在意外异常（fetch_page 自身抛错而非 status 字段）才抛 ``RuntimeError``，
      由 ``try_raw_text`` 统一捕获返回 None

    body 取自 ``page.evaluate(() => document.body.innerText)``——chromium 对
    text/plain 响应会渲染为 ``<pre>`` 包裹，innerText 即原始文本。html 字段不直接用，
    避免拿到带 ``<pre>`` 标签包裹的字符串污染下游 markdown 解析。
    """
    try:
        from brain_base.tools.web_fetcher import fetch_page_sync
    except ImportError as exc:  # pragma: no cover - 缺 playwright 时早就失败
        raise RuntimeError(
            f"raw_text_extractor 需要 playwright 但导入失败: {exc}"
        ) from exc

    try:
        result = fetch_page_sync(url, timeout=timeout)
    except Exception as exc:
        # 规则 25：保留 try-except 必须打日志；transient 异常视为可重试失败
        logger.warning(
            "raw_text fetch_page_sync raised | url=%s err=%s: %s",
            url, type(exc).__name__, str(exc)[:200],
        )
        raise RuntimeError(
            f"HTTP GET 失败: {url} → {type(exc).__name__}: {exc}"
        ) from exc

    status = result.get("status", "spa_failed")
    error = result.get("error", "")
    text = result.get("text", "") or ""
    title = result.get("title", "")

    if status != "ok" or error:
        # fetch_page 内部已 log；这里以 (500, "") 形式回报，handler 视为失败 + 不抛
        logger.debug(
            "raw_text fetch_page non-ok | url=%s status=%s error=%s title=%r",
            url, status, error[:200], title[:80],
        )
        return 500, ""

    return 200, text


async def _http_get_async(url: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str]:
    """async 版 ``_http_get``：直接 await ``fetch_page``，不走 sync 包装层。

    T48.2 D3 修订：sync ``fetch_page_sync`` 在 worker-thread 内起新 ``asyncio.run``，
    会反复触发 web_fetcher 单例 ``_LOOP`` affinity 检查导致 chromium 重启。
    async 调用方（``qa_url_pre_fetch._fetch_one`` / ``qa_tools.execute_raw_text``）
    应走本函数走纯 async 路径，与主 graph 共享同一 loop，单例不再切换 → 不重启。

    返回语义与 ``_http_get`` 一致 ``(status_code, body_text)``。
    """
    try:
        from brain_base.tools.web_fetcher import fetch_page
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            f"raw_text_extractor 需要 playwright 但导入失败: {exc}"
        ) from exc

    try:
        result = await fetch_page(url, timeout=timeout)
    except Exception as exc:
        logger.warning(
            "raw_text fetch_page (async) raised | url=%s err=%s: %s",
            url, type(exc).__name__, str(exc)[:200],
        )
        raise RuntimeError(
            f"HTTP GET 失败: {url} → {type(exc).__name__}: {exc}"
        ) from exc

    status = result.get("status", "spa_failed")
    error = result.get("error", "")
    text = result.get("text", "") or ""
    title = result.get("title", "")

    if status != "ok" or error:
        logger.debug(
            "raw_text fetch_page (async) non-ok | url=%s status=%s error=%s title=%r",
            url, status, error[:200], title[:80],
        )
        return 500, ""

    return 200, text


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def try_raw_text(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any] | None:
    """检测 URL 能否直接拿到纯 Markdown / 纯文本，命中时返回。

    返回字段：
    - ``markdown``: 内容文本（Markdown 或 plain text）
    - ``title``: 从内容首 H1 / 首行提取，供 ``title_hint`` 使用
    - ``source_url``: 实际 GET 的 URL（可能与输入不同）

    URL 模式优先级：
    1. github.com → raw.githubusercontent.com
    2. gitlab.com → gitlab.com/.../-/raw/...
    3. arxiv.org/abs → 解析 abs 页 meta
    4. RFC（datatracker / rfc-editor / ietf）→ rfc-editor.org/rfc/rfcXXXX.txt

    任何环节失败 → 返回 None（静默降级，不抛异常）。

    **sync 入口**：内部用 ``_http_get`` 走 ``fetch_page_sync``。供 sync 调用方
    （``bin/ingest_url.fetch_node``）使用。

    **async 调用方请用 ``try_raw_text_async``**——避免 ``asyncio.to_thread`` 路径
    每次重启 chromium（T48.2 D3 修复）。
    """
    if not url:
        return None
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return None

    handlers = [
        (("github.com",), _try_github),
        (("gitlab.com",), _try_gitlab),
        (("arxiv.org",), _try_arxiv),
        (("datatracker.ietf.org", "ietf.org", "www.rfc-editor.org", "rfc-editor.org"), _try_rfc),
    ]
    for hosts, handler in handlers:
        if any(host == h or host.endswith("." + h) for h in hosts):
            try:
                return handler(url, timeout)
            except Exception:
                # 任何 handler 内部异常都静默降级，不阻断 fetch_node
                return None
    return None


async def try_raw_text_async(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any] | None:
    """async 版 ``try_raw_text``：内部全程 await ``fetch_page``，不走 sync 包装层。

    T48.2 D3 修复：sync ``try_raw_text`` 在 worker-thread 内 ``asyncio.to_thread``
    包装时，每次起新 loop 触发 web_fetcher 单例 affinity 检查 → chromium 反复重启。
    async 调用方（``qa_url_pre_fetch._fetch_one`` / ``qa_tools.execute_raw_text``）
    应走本函数走纯 async 路径，与主 graph 共享同一 loop，单例稳定不切换。

    URL 路由 / 探测矩阵 / 返回结构与 sync 版完全一致——仅 IO 通道改 ``await``。
    """
    if not url:
        return None
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return None

    async_handlers = [
        (("github.com",), _try_github_async),
        (("gitlab.com",), _try_gitlab_async),
        (("arxiv.org",), _try_arxiv_async),
        (("datatracker.ietf.org", "ietf.org", "www.rfc-editor.org", "rfc-editor.org"), _try_rfc_async),
    ]
    for hosts, handler in async_handlers:
        if any(host == h or host.endswith("." + h) for h in hosts):
            try:
                return await handler(url, timeout)
            except Exception:
                # 任何 handler 内部异常都静默降级，不阻断主图节点
                return None
    return None


# ---------------------------------------------------------------------------
# 各站点 handler
# ---------------------------------------------------------------------------


_GITHUB_BLOB_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?:blob|tree|raw)/(?P<branch>[^/]+)/(?P<path>.+)$"
)
_GITHUB_REPO_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/?$")


def _match_github_url(url: str) -> dict[str, Any] | None:
    """T48.4 共享纯函数 helper：解析 GitHub URL 模式，返路由信息无 IO。

    返回字典格式：
    - 文件页：``{"kind": "blob"|"tree"|"raw", "owner", "repo", "branch", "path"}``
    - 仓库根：``{"kind": "repo_root", "owner", "repo"}``

    None 表示不是支持的 GitHub URL（issue / PR / wiki / search / gist 等不在覆盖范围）。

    供 sync ``_try_github`` 与 async ``try_github_raw`` 共享，避免双份正则维护。
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = (parsed.netloc or "").lower()
    if host != "github.com" and not host.endswith(".github.com"):
        return None
    # gist.github.com 是独立 host，不属于本工具覆盖范围
    if host.startswith("gist."):
        return None

    path = parsed.path.rstrip("/")

    # 文件页（blob / tree / raw）
    m = _GITHUB_BLOB_RE.match(path + "/" if not path.endswith("/") else path)
    if m:
        # 判断 kind：blob / tree / raw
        # 重新正则提取 kind（避免共享正则改动）
        kind_m = re.match(
            r"^/[^/]+/[^/]+/(?P<kind>blob|tree|raw)/", path,
        )
        kind = kind_m.group("kind") if kind_m else "blob"
        return {
            "kind": kind,
            "owner": m.group("owner"),
            "repo": m.group("repo"),
            "branch": m.group("branch"),
            "path": m.group("path").rstrip("/"),
        }

    # 仓库根
    m = _GITHUB_REPO_RE.match(path)
    if m:
        return {
            "kind": "repo_root",
            "owner": m.group("owner"),
            "repo": m.group("repo"),
            "branch": None,
            "path": None,
        }
    # 其他（issues / pulls / wiki / search 等）不支持
    return None


def _try_github(url: str, timeout: float) -> dict[str, Any] | None:
    """GitHub URL → raw.githubusercontent.com（sync 版，T48.4 D4：内部用 ``_match_github_url``）。

    - 仓库根（``github.com/X/Y``）：按 ``main → master`` × ``README{,_zh,_en}.md`` 矩阵探测
    - blob/tree/raw 文件页：直接转换为对应 raw URL（不带探测）
    - tree 目录页：raw.githubusercontent.com 对目录返 404 → 实际返 None

    ``bin/ingest_url.fetch_node`` 走 sync 路径（IngestUrlGraph 同步图），通过
    ``try_raw_text`` 内部 handlers 表分发到本函数。性能 ~1s 拉 README。
    """
    match = _match_github_url(url)
    if match is None:
        return None

    if match["kind"] in ("blob", "tree", "raw"):
        owner, repo, branch, file_path = (
            match["owner"], match["repo"], match["branch"], match["path"],
        )
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
        status, body = _http_get(raw_url, timeout)
        if status == 200 and body.strip():
            return _build_result(body, raw_url)
        return None

    if match["kind"] == "repo_root":
        owner, repo = match["owner"], match["repo"]
        for branch in _BRANCH_VARIANTS:
            for fname in _README_VARIANTS:
                raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{fname}"
                try:
                    status, body = _http_get(raw_url, timeout)
                except RuntimeError:
                    continue
                if status == 200 and body.strip():
                    return _build_result(body, raw_url)
    return None


_GITLAB_BLOB_RE = re.compile(
    r"^/(?P<full_path>.+?)/-/(?:blob|raw)/(?P<branch>[^/]+)/(?P<path>.+)$"
)
_GITLAB_REPO_RE = re.compile(r"^/(?P<full_path>[^/].*?)/?$")


def _try_gitlab(url: str, timeout: float) -> dict[str, Any] | None:
    """GitLab URL → gitlab.com/.../-/raw/...

    - 文件页（``-/blob/branch/path``）：转换为 ``-/raw/branch/path``
    - 仓库根：按 ``main → master`` × ``README*`` 矩阵探测
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")

    m = _GITLAB_BLOB_RE.match(path + ("/" if not path.endswith("/") else ""))
    if m:
        full_path, branch, file_path = m.group("full_path"), m.group("branch"), m.group("path").rstrip("/")
        raw_url = f"https://gitlab.com/{full_path}/-/raw/{branch}/{file_path}?inline=false"
        status, body = _http_get(raw_url, timeout)
        if status == 200 and body.strip():
            return _build_result(body, raw_url)
        return None

    m = _GITLAB_REPO_RE.match(path)
    if m:
        full_path = m.group("full_path")
        # 排除显然不是仓库的路径（如 explore / users / -）
        if full_path.startswith(("explore", "users", "-", "help")):
            return None
        for branch in _BRANCH_VARIANTS:
            for fname in _README_VARIANTS:
                raw_url = f"https://gitlab.com/{full_path}/-/raw/{branch}/{fname}?inline=false"
                try:
                    status, body = _http_get(raw_url, timeout)
                except RuntimeError:
                    continue
                if status == 200 and body.strip():
                    return _build_result(body, raw_url)
    return None


_ARXIV_ABS_RE = re.compile(r"^/abs/(?P<id>[^/]+?)(?:v\d+)?/?$")
_ARXIV_PDF_RE = re.compile(r"^/pdf/")

# T48.3：保留 v 后缀的 arxiv id 正则（用于 arxiv_pdf 工具，v 决定 PDF binary）
# 锚点 ^/(abs|pdf)/...$ + (?:\.pdf)?/?$ 双锁 + 字类 [^/] 唯一匹配
_ARXIV_ID_RE_KEEP_V = re.compile(r"^/(?:abs|pdf)/(?P<id>[^/]+?)(?:\.pdf)?/?$")


def normalize_arxiv_pdf_url(url: str) -> str | None:
    """T48.3：把 arxiv URL 规范化为 PDF 直链 ``https://arxiv.org/pdf/{id}.pdf``。

    支持的输入模式：
    - ``https://arxiv.org/abs/2501.12345`` → ``https://arxiv.org/pdf/2501.12345.pdf``
    - ``https://arxiv.org/abs/2501.12345v2`` → ``https://arxiv.org/pdf/2501.12345v2.pdf``
    - ``https://arxiv.org/pdf/2501.12345`` → ``https://arxiv.org/pdf/2501.12345.pdf``
    - ``https://arxiv.org/pdf/2501.12345v2.pdf`` → ``https://arxiv.org/pdf/2501.12345v2.pdf``

    与 ``_ARXIV_ABS_RE`` 的差异：本正则**保留 v 后缀**——arxiv 不同版本是不同的 PDF
    binary（sha256 必不同），arxiv_pdf 工具需保留版本信息进入 dedup key。

    返回 None 表示不是 arxiv URL（host 不匹配 / 路径不是 abs|pdf）。
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = (parsed.netloc or "").lower()
    if host != "arxiv.org" and not host.endswith(".arxiv.org"):
        return None
    m = _ARXIV_ID_RE_KEEP_V.match(parsed.path.rstrip("/"))
    if not m:
        return None
    arxiv_id = m.group("id")
    return f"https://arxiv.org/pdf/{arxiv_id}.pdf"


def _try_arxiv(url: str, timeout: float) -> dict[str, Any] | None:
    """arxiv.org/abs/XXXX → 解析 abs 页 meta 标签生成 markdown。

    PDF 页跳过（让 MinerU PDF 路径处理）。
    """
    parsed = urlparse(url)
    path = parsed.path

    if _ARXIV_PDF_RE.match(path):
        return None  # PDF 走原 MinerU 路径

    m = _ARXIV_ABS_RE.match(path)
    if not m:
        return None

    arxiv_id = m.group("id")
    abs_url = f"https://arxiv.org/abs/{arxiv_id}"
    try:
        status, html = _http_get(abs_url, timeout)
    except RuntimeError:
        return None
    if status != 200 or not html:
        return None

    title = _extract_meta(html, "citation_title") or _extract_meta(html, "og:title") or arxiv_id
    authors_raw = _extract_all_meta(html, "citation_author")
    abstract = _extract_arxiv_abstract(html) or ""

    md_parts = [f"# {title.strip()}", ""]
    if authors_raw:
        md_parts.append("**Authors**: " + ", ".join(a.strip() for a in authors_raw if a.strip()))
        md_parts.append("")
    md_parts.append("## Abstract")
    md_parts.append("")
    md_parts.append(abstract.strip())
    md_parts.append("")
    md_parts.append(f"---")
    md_parts.append(f"arXiv: [{arxiv_id}]({abs_url})")
    markdown = "\n".join(md_parts)
    return _build_result(markdown, abs_url, title_override=title.strip())


_RFC_NUM_RE = re.compile(r"rfc[-_]?(\d+)", re.IGNORECASE)


def _try_rfc(url: str, timeout: float) -> dict[str, Any] | None:
    """RFC URL → rfc-editor.org/rfc/rfcXXXX.txt 纯文本。"""
    m = _RFC_NUM_RE.search(url)
    if not m:
        return None
    rfc_num = m.group(1)
    raw_url = f"https://www.rfc-editor.org/rfc/rfc{rfc_num}.txt"
    status, body = _http_get(raw_url, timeout)
    if status != 200 or not body.strip():
        return None
    return _build_result(body, raw_url, title_override=f"RFC {rfc_num}")


# ---------------------------------------------------------------------------
# 各站点 async handler（T48.2 D3：全程 await，不走 sync 包装层）
# ---------------------------------------------------------------------------
#
# 共享所有 sync 版 handler 的 helper：_GITHUB_BLOB_RE / _GITHUB_REPO_RE /
# _README_VARIANTS / _BRANCH_VARIANTS / _GITLAB_BLOB_RE / _GITLAB_REPO_RE /
# _ARXIV_ABS_RE / _ARXIV_PDF_RE / _RFC_NUM_RE / _build_result / _extract_meta /
# _extract_all_meta / _extract_arxiv_abstract。
# 仅 ``_http_get`` → ``_http_get_async`` 替换。

async def try_github_raw(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any] | None:
    """async 版 GitHub raw 提取（T48.4 公开 API，供 ``execute_github_raw`` 工具使用）。

    与 sync ``_try_github`` 行为完全一致——共享 ``_match_github_url`` 纯函数 helper，
    仅 IO 通道走 ``_http_get_async`` 直接 ``await fetch_page`` 不重启 chromium
    （T48.2 D3 修复后业务路径同 loop 复用）。

    支持：仓库根（自动 README 探测）/ blob 文件页 / raw 文件页。
    不支持：issue / PR / wiki / search / gist（请用 fetch_url）/ tree 目录页（raw 形不存在）。
    """
    match = _match_github_url(url)
    if match is None:
        return None

    if match["kind"] in ("blob", "tree", "raw"):
        owner, repo, branch, file_path = (
            match["owner"], match["repo"], match["branch"], match["path"],
        )
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
        try:
            status, body = await _http_get_async(raw_url, timeout)
        except RuntimeError:
            return None
        if status == 200 and body.strip():
            return _build_result(body, raw_url)
        return None

    if match["kind"] == "repo_root":
        owner, repo = match["owner"], match["repo"]
        for branch in _BRANCH_VARIANTS:
            for fname in _README_VARIANTS:
                raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{fname}"
                try:
                    status, body = await _http_get_async(raw_url, timeout)
                except RuntimeError:
                    continue
                if status == 200 and body.strip():
                    return _build_result(body, raw_url)
    return None


# T48.4：保留别名 ``_try_github_async`` 让 ``try_raw_text_async`` 内部 dispatch 仍能用
# （raw_text 工具还会兜底处理 GitHub URL，仅 description 不再宣传该能力）
_try_github_async = try_github_raw


async def _try_gitlab_async(url: str, timeout: float) -> dict[str, Any] | None:
    """async 版 ``_try_gitlab``——逻辑与 sync 版完全一致，IO 走 ``_http_get_async``。"""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")

    m = _GITLAB_BLOB_RE.match(path + ("/" if not path.endswith("/") else ""))
    if m:
        full_path, branch, file_path = (
            m.group("full_path"), m.group("branch"),
            m.group("path").rstrip("/"),
        )
        raw_url = f"https://gitlab.com/{full_path}/-/raw/{branch}/{file_path}?inline=false"
        status, body = await _http_get_async(raw_url, timeout)
        if status == 200 and body.strip():
            return _build_result(body, raw_url)
        return None

    m = _GITLAB_REPO_RE.match(path)
    if m:
        full_path = m.group("full_path")
        if full_path.startswith(("explore", "users", "-", "help")):
            return None
        for branch in _BRANCH_VARIANTS:
            for fname in _README_VARIANTS:
                raw_url = f"https://gitlab.com/{full_path}/-/raw/{branch}/{fname}?inline=false"
                try:
                    status, body = await _http_get_async(raw_url, timeout)
                except RuntimeError:
                    continue
                if status == 200 and body.strip():
                    return _build_result(body, raw_url)
    return None


async def _try_arxiv_async(url: str, timeout: float) -> dict[str, Any] | None:
    """async 版 ``_try_arxiv``——逻辑与 sync 版完全一致，IO 走 ``_http_get_async``。"""
    parsed = urlparse(url)
    path = parsed.path

    if _ARXIV_PDF_RE.match(path):
        return None  # PDF 走原 MinerU 路径

    m = _ARXIV_ABS_RE.match(path)
    if not m:
        return None

    arxiv_id = m.group("id")
    abs_url = f"https://arxiv.org/abs/{arxiv_id}"
    try:
        status, html = await _http_get_async(abs_url, timeout)
    except RuntimeError:
        return None
    if status != 200 or not html:
        return None

    title = _extract_meta(html, "citation_title") or _extract_meta(html, "og:title") or arxiv_id
    authors_raw = _extract_all_meta(html, "citation_author")
    abstract = _extract_arxiv_abstract(html) or ""

    md_parts = [f"# {title.strip()}", ""]
    if authors_raw:
        md_parts.append("**Authors**: " + ", ".join(a.strip() for a in authors_raw if a.strip()))
        md_parts.append("")
    md_parts.append("## Abstract")
    md_parts.append("")
    md_parts.append(abstract.strip())
    md_parts.append("")
    md_parts.append("---")
    md_parts.append(f"arXiv: [{arxiv_id}]({abs_url})")
    markdown = "\n".join(md_parts)
    return _build_result(markdown, abs_url, title_override=title.strip())


async def _try_rfc_async(url: str, timeout: float) -> dict[str, Any] | None:
    """async 版 ``_try_rfc``——逻辑与 sync 版完全一致，IO 走 ``_http_get_async``。"""
    m = _RFC_NUM_RE.search(url)
    if not m:
        return None
    rfc_num = m.group(1)
    raw_url = f"https://www.rfc-editor.org/rfc/rfc{rfc_num}.txt"
    status, body = await _http_get_async(raw_url, timeout)
    if status != 200 or not body.strip():
        return None
    return _build_result(body, raw_url, title_override=f"RFC {rfc_num}")


# ---------------------------------------------------------------------------
# 内部 helper
# ---------------------------------------------------------------------------


_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _build_result(body: str, source_url: str, title_override: str = "") -> dict[str, Any]:
    """统一打包返回结构。title 优先用 override，否则从 body 首 H1 / 首行抽取。"""
    if title_override:
        title = title_override
    else:
        m = _H1_RE.search(body)
        if m:
            title = m.group(1).strip()
        else:
            # 取首个非空行作为 title 兜底
            for line in body.splitlines():
                line = line.strip()
                if line:
                    title = line[:200]
                    break
            else:
                title = ""
    return {
        "markdown": body,
        "title": title,
        "source_url": source_url,
    }


_META_RE_TPL = r'<meta\s+[^>]*name=["\']{name}["\'][^>]*content=["\'](?P<content>[^"\']*)["\']'
_META_RE_TPL_OG = r'<meta\s+[^>]*property=["\']{name}["\'][^>]*content=["\'](?P<content>[^"\']*)["\']'


def _extract_meta(html: str, name: str) -> str:
    """提取首个匹配的 <meta name="X" content="Y"> 内容。og:title 走 property=。"""
    pattern = _META_RE_TPL_OG if name.startswith("og:") else _META_RE_TPL
    m = re.search(pattern.format(name=re.escape(name)), html, re.IGNORECASE)
    if m:
        return unescape(m.group("content"))
    return ""


def _extract_all_meta(html: str, name: str) -> list[str]:
    """提取所有匹配的 <meta name="X" content="Y"> 内容（如多 author）。"""
    pattern = _META_RE_TPL.format(name=re.escape(name))
    return [unescape(m.group("content")) for m in re.finditer(pattern, html, re.IGNORECASE)]


_ARXIV_ABSTRACT_RE = re.compile(
    r'<blockquote\s+class=["\']abstract[^"\']*["\'][^>]*>(?P<body>.*?)</blockquote>',
    re.DOTALL | re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ABSTRACT_DESCRIPTOR_RE = re.compile(r"^\s*Abstract:\s*", re.IGNORECASE)


def _extract_arxiv_abstract(html: str) -> str:
    """从 arxiv abs 页提取摘要正文（剥 HTML 标签 + 去 ``Abstract:`` 描述符前缀）。"""
    m = _ARXIV_ABSTRACT_RE.search(html)
    if not m:
        return ""
    raw = m.group("body")
    text = _HTML_TAG_RE.sub("", raw)
    text = unescape(text)
    text = _ABSTRACT_DESCRIPTOR_RE.sub("", text.strip())
    # 折叠多余空白
    return re.sub(r"\s+", " ", text).strip()
