"""通用 raw text 提取路径（T20）。

针对 GitHub / GitLab / arxiv abs / RFC 等"能直接拿到结构化纯文本"的站点，
在 ingest_url 流程的 fetch 阶段做一次轻量 HTTP GET 短路，绕过 playwright
+ MinerU-HTML 全流程——后者对长文档（>16K token）会被 ``BB_MINERU_HTML_MAX_INPUT``
硬截断（16GB 卡 OOM 上限），实测 GitHub README 1304 行被切到 617 行（丢 53%）。

设计：
- ``try_raw_text(url)`` 返回 ``dict | None``，命中即返回带 markdown 的 dict
- 不命中 / 抓取失败 → 返回 ``None``，调用方走原 playwright 流程
- 不抛异常：raw text 是优化路径，失败必须静默降级到主路径
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# 通用配置
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = 10.0
_USER_AGENT = "brain-base-raw-text/1.0 (+https://github.com/mengsi16/brain-base)"

# GitHub / GitLab 仓库根 URL 时，按顺序尝试这些 README 文件名
_README_VARIANTS = ("README.md", "README_zh.md", "README_en.md", "readme.md")
_BRANCH_VARIANTS = ("main", "master")


# ---------------------------------------------------------------------------
# HTTP GET 封装
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str]:
    """简单 HTTP GET，返回 (status_code, body_text)。

    失败抛 ``RuntimeError``——上层 ``try_raw_text`` 统一捕获返回 None。
    Body 编码：response.headers 提供 charset 时按 charset 解码，否则按 UTF-8
    带 ``errors="replace"`` 兜底（适配 RFC 等旧文本可能为 ASCII / Latin-1 的场景）。
    """
    req = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"})
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - whitelisted hosts
            status = int(resp.status)
            charset = resp.headers.get_content_charset() or "utf-8"
            body = resp.read().decode(charset, errors="replace")
            return status, body
    except HTTPError as exc:
        return int(exc.code), ""
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"HTTP GET 失败: {url} → {type(exc).__name__}: {exc}") from exc


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


# ---------------------------------------------------------------------------
# 各站点 handler
# ---------------------------------------------------------------------------


_GITHUB_BLOB_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?:blob|tree|raw)/(?P<branch>[^/]+)/(?P<path>.+)$"
)
_GITHUB_REPO_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/?$")


def _try_github(url: str, timeout: float) -> dict[str, Any] | None:
    """GitHub URL → raw.githubusercontent.com。

    - 仓库根（``github.com/X/Y``）：按 ``main → master`` × ``README{,_zh,_en}.md`` 矩阵探测
    - blob/tree/raw 文件页：直接转换为对应 raw URL（不带探测）
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")

    # 文件页（blob / tree / raw）
    m = _GITHUB_BLOB_RE.match(path + "/" if not path.endswith("/") else path)
    if m:
        owner, repo, branch, file_path = m.group("owner"), m.group("repo"), m.group("branch"), m.group("path").rstrip("/")
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
        status, body = _http_get(raw_url, timeout)
        if status == 200 and body.strip():
            return _build_result(body, raw_url)
        return None

    # 仓库根
    m = _GITHUB_REPO_RE.match(path)
    if m:
        owner, repo = m.group("owner"), m.group("repo")
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
