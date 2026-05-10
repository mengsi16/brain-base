"""T20 单元测试：raw text 提取路径。

覆盖 GitHub / GitLab / arxiv abs / RFC 四类站点的命中与降级路径，
以及 fetch_node / clean_node 两个集成短路点。

mock 策略：用 monkeypatch 替换 ``raw_text_extractor._http_get``，
完全避免真实网络请求。
"""
from __future__ import annotations

import sys

import pytest

from brain_base.tools import raw_text_extractor as rte


# ---------------------------------------------------------------------------
# helper: 构造可控的 _http_get mock
# ---------------------------------------------------------------------------


def _make_http_get_mock(routes: dict, default_status: int = 404):
    """返回 mock _http_get：按 URL 精确匹配返回 (status, body)，未命中走 default_status。

    routes: dict[url, (status, body)] 或 dict[url, callable(url)->(status, body)]
    """
    call_log: list[str] = []

    def mock(url: str, timeout: float = 10.0):
        call_log.append(url)
        if url in routes:
            v = routes[url]
            if callable(v):
                return v(url)
            return v
        return default_status, ""

    mock.call_log = call_log  # type: ignore[attr-defined]
    return mock


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


def test_try_raw_text_github_repo_root_hits_main_readme(monkeypatch):
    """github.com/X/Y 仓库根：命中 main/README.md 即返回，不再尝试 master。"""
    body = "# RAG-Anything\n\n核心特性\n\n- 多模态检索\n"
    routes = {
        "https://raw.githubusercontent.com/HKUDS/RAG-Anything/main/README.md": (200, body),
    }
    mock = _make_http_get_mock(routes)
    monkeypatch.setattr(rte, "_http_get", mock)

    out = rte.try_raw_text("https://github.com/HKUDS/RAG-Anything")

    assert out is not None
    assert out["markdown"] == body
    assert out["title"] == "RAG-Anything"
    assert out["source_url"].endswith("/main/README.md")
    # 命中 main/README.md 后必须停止探测，不应再尝试 master 或 README_zh
    assert mock.call_log == [
        "https://raw.githubusercontent.com/HKUDS/RAG-Anything/main/README.md",
    ]


def test_try_raw_text_github_falls_back_to_master_when_main_404(monkeypatch):
    """main 分支三个 README 全 404 时，必须回退到 master 分支。"""
    body = "# Old Repo\n\nlegacy content"
    routes = {
        "https://raw.githubusercontent.com/old/repo/master/README.md": (200, body),
    }
    mock = _make_http_get_mock(routes)
    monkeypatch.setattr(rte, "_http_get", mock)

    out = rte.try_raw_text("https://github.com/old/repo")

    assert out is not None
    assert out["source_url"].endswith("/master/README.md")
    # main 全部尝试过且未命中后，才轮到 master
    assert "main/README.md" in mock.call_log[0]
    assert any("master/README.md" in u for u in mock.call_log)


def test_try_raw_text_github_blob_url_converts_to_raw(monkeypatch):
    """github.com/X/Y/blob/BRANCH/PATH → 直接转 raw URL，不走探测矩阵。"""
    body = "# API doc\n\ncontent"
    routes = {
        "https://raw.githubusercontent.com/HKUDS/RAG-Anything/dev/docs/api.md": (200, body),
    }
    mock = _make_http_get_mock(routes)
    monkeypatch.setattr(rte, "_http_get", mock)

    out = rte.try_raw_text("https://github.com/HKUDS/RAG-Anything/blob/dev/docs/api.md")

    assert out is not None
    assert out["source_url"].endswith("/dev/docs/api.md")
    assert mock.call_log == [
        "https://raw.githubusercontent.com/HKUDS/RAG-Anything/dev/docs/api.md",
    ]


def test_try_raw_text_github_zh_readme_fallback(monkeypatch):
    """main/README.md 不存在但 main/README_zh.md 存在时命中 README_zh。"""
    body = "# 项目名\n\n中文 README"
    routes = {
        "https://raw.githubusercontent.com/zh/repo/main/README_zh.md": (200, body),
    }
    mock = _make_http_get_mock(routes)
    monkeypatch.setattr(rte, "_http_get", mock)

    out = rte.try_raw_text("https://github.com/zh/repo")

    assert out is not None
    assert out["source_url"].endswith("/main/README_zh.md")
    assert out["title"] == "项目名"


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------


def test_try_raw_text_gitlab_blob_url_converts_to_raw(monkeypatch):
    """gitlab.com/X/Y/-/blob/BRANCH/PATH → /-/raw/BRANCH/PATH?inline=false。"""
    body = "# GitLab Project\n\nbody"
    routes = {
        "https://gitlab.com/group/proj/-/raw/main/README.md?inline=false": (200, body),
    }
    mock = _make_http_get_mock(routes)
    monkeypatch.setattr(rte, "_http_get", mock)

    out = rte.try_raw_text("https://gitlab.com/group/proj/-/blob/main/README.md")

    assert out is not None
    assert "/-/raw/main/README.md" in out["source_url"]
    assert "inline=false" in out["source_url"]


# ---------------------------------------------------------------------------
# arxiv
# ---------------------------------------------------------------------------


_ARXIV_ABS_HTML_FIXTURE = """
<html>
<head>
<meta name="citation_title" content="A Study on Multimodal RAG">
<meta name="citation_author" content="Doe, Jane">
<meta name="citation_author" content="Smith, John">
</head>
<body>
<blockquote class="abstract mathjax">
<span class="descriptor">Abstract:</span>
We propose a novel approach for multimodal retrieval-augmented generation
that improves recall on long-form documents.
</blockquote>
</body>
</html>
"""


def test_try_raw_text_arxiv_abs_extracts_title_authors_abstract(monkeypatch):
    """arxiv.org/abs/XXXX → 解析 abs 页 meta 标签生成结构化 markdown。"""
    routes = {
        "https://arxiv.org/abs/2510.12323": (200, _ARXIV_ABS_HTML_FIXTURE),
    }
    mock = _make_http_get_mock(routes)
    monkeypatch.setattr(rte, "_http_get", mock)

    out = rte.try_raw_text("https://arxiv.org/abs/2510.12323")

    assert out is not None
    md = out["markdown"]
    assert "# A Study on Multimodal RAG" in md
    assert "Doe, Jane" in md and "Smith, John" in md
    assert "## Abstract" in md
    assert "multimodal retrieval-augmented generation" in md
    # 描述符前缀 "Abstract:" 必须被剥掉
    assert "Abstract: We propose" not in md
    # title_override 应直接使用 citation_title
    assert out["title"] == "A Study on Multimodal RAG"


def test_try_raw_text_arxiv_pdf_returns_none(monkeypatch):
    """arxiv.org/pdf/XXXX → None（PDF 走 MinerU 路径，不走 raw text）。"""
    # 即使 mock 命中也应被 PDF 规则提前拦下
    mock = _make_http_get_mock({})
    monkeypatch.setattr(rte, "_http_get", mock)

    out = rte.try_raw_text("https://arxiv.org/pdf/2510.12323.pdf")

    assert out is None
    assert mock.call_log == [], "PDF URL 不应触发任何 HTTP GET"


def test_try_raw_text_arxiv_versioned_id_strips_version(monkeypatch):
    """arxiv 带版本号（abs/2510.12323v2）：识别后剥掉版本号 GET 主 abs URL。

    设计意图：arxiv 服务端对 ``abs/{id}``（不带版本）会返回最新版，避免为每个
    历史版本各存一份；用户提供 v2 时仍然取最新内容。
    """
    routes = {
        # 注意：handler 剥掉 v2 后 GET 的是不带版本号的 URL
        "https://arxiv.org/abs/2510.12323": (200, _ARXIV_ABS_HTML_FIXTURE),
    }
    mock = _make_http_get_mock(routes)
    monkeypatch.setattr(rte, "_http_get", mock)

    out = rte.try_raw_text("https://arxiv.org/abs/2510.12323v2")

    assert out is not None
    assert "# A Study on Multimodal RAG" in out["markdown"]
    assert mock.call_log == ["https://arxiv.org/abs/2510.12323"]


# ---------------------------------------------------------------------------
# RFC
# ---------------------------------------------------------------------------


def test_try_raw_text_rfc_extracts_number_and_fetches_txt(monkeypatch):
    """datatracker.ietf.org/doc/html/rfc9110 → 提取数字后 GET rfc-editor 的 .txt 版本。"""
    body = "Internet Engineering Task Force (IETF)\nRFC 9110\n\nHTTP Semantics"
    routes = {
        "https://www.rfc-editor.org/rfc/rfc9110.txt": (200, body),
    }
    mock = _make_http_get_mock(routes)
    monkeypatch.setattr(rte, "_http_get", mock)

    out = rte.try_raw_text("https://datatracker.ietf.org/doc/html/rfc9110")

    assert out is not None
    assert out["markdown"] == body
    assert out["title"] == "RFC 9110"  # title_override
    assert out["source_url"].endswith("/rfc9110.txt")


def test_try_raw_text_rfc_editor_url_direct(monkeypatch):
    """www.rfc-editor.org/rfc/rfcXXXX.txt 直接命中。"""
    body = "RFC 8446 - TLS 1.3"
    routes = {
        "https://www.rfc-editor.org/rfc/rfc8446.txt": (200, body),
    }
    mock = _make_http_get_mock(routes)
    monkeypatch.setattr(rte, "_http_get", mock)

    out = rte.try_raw_text("https://www.rfc-editor.org/rfc/rfc8446.txt")

    assert out is not None
    assert out["title"] == "RFC 8446"


# ---------------------------------------------------------------------------
# 通用降级
# ---------------------------------------------------------------------------


def test_try_raw_text_unrelated_url_returns_none(monkeypatch):
    """不在覆盖范围的 URL 直接返回 None，不触发任何 HTTP GET。"""
    mock = _make_http_get_mock({})
    monkeypatch.setattr(rte, "_http_get", mock)

    out = rte.try_raw_text("https://example.com/blog/article")

    assert out is None
    assert mock.call_log == []


def test_try_raw_text_http_failure_returns_none_silently(monkeypatch):
    """_http_get 抛 RuntimeError → handler 内部 catch 返回 None，不向上传播异常。"""
    def boom(url: str, timeout: float = 10.0):
        raise RuntimeError("network down")

    monkeypatch.setattr(rte, "_http_get", boom)

    # GitHub repo root: 第一次 main/README.md 抛错应被 catch（continue），后续探测也都抛错，
    # 最终返回 None 而非抛异常
    out = rte.try_raw_text("https://github.com/x/y")
    assert out is None

    # arxiv 单次 GET 抛错也应静默
    out = rte.try_raw_text("https://arxiv.org/abs/2510.12323")
    assert out is None


def test_try_raw_text_empty_url_returns_none():
    """空 URL → None，不抛错。"""
    assert rte.try_raw_text("") is None
    assert rte.try_raw_text(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# fetch_node / clean_node 集成短路
# ---------------------------------------------------------------------------


def test_fetch_node_uses_raw_text_when_available(monkeypatch):
    """fetch_node 命中 raw text → 写 cleaned_md + raw_html='' + extraction_status=ok，不调 playwright。"""
    from brain_base.nodes import ingest_url as ingest_mod

    def fake_try_raw_text(url, timeout=10.0):
        return {
            "markdown": "# title\n\nbody content",
            "title": "title",
            "source_url": "https://raw.githubusercontent.com/x/y/main/README.md",
        }

    def boom_fetch_page(url):
        raise AssertionError("raw text 命中时不应调用 playwright fetch_page")

    monkeypatch.setattr(ingest_mod, "try_raw_text", fake_try_raw_text)
    monkeypatch.setattr(ingest_mod, "fetch_page", boom_fetch_page)

    out = ingest_mod.fetch_node({"url": "https://github.com/x/y"})

    assert out["extraction_status"] == "ok"
    assert out["cleaned_md"] == "# title\n\nbody content"
    assert out["raw_content"] == "# title\n\nbody content"
    assert out["raw_html"] == ""  # 明确置空
    assert out["title_hint"] == "title"


def test_fetch_node_falls_back_to_playwright_when_raw_text_misses(monkeypatch):
    """raw text 返回 None → 走原 playwright 路径。"""
    from brain_base.nodes import ingest_url as ingest_mod

    monkeypatch.setattr(ingest_mod, "try_raw_text", lambda url, timeout=10.0: None)

    called = {"flag": False}

    def fake_fetch_page(url):
        called["flag"] = True
        return {"html": "<html><body>hi</body></html>", "text": "hi", "title": "T"}

    monkeypatch.setattr(ingest_mod, "fetch_page", fake_fetch_page)

    out = ingest_mod.fetch_node({"url": "https://example.com/blog"})

    assert called["flag"] is True
    assert out["extraction_status"] == "ok"
    assert out["raw_html"] == "<html><body>hi</body></html>"


def test_clean_node_short_circuits_when_cleaned_md_already_set(monkeypatch):
    """clean_node 检测到 cleaned_md 已存在 → 直接返回 ok，不调 MinerU-HTML 转换。"""
    from brain_base.nodes import ingest_url as ingest_mod

    def boom(html):
        raise AssertionError("cleaned_md 已存在时不应调用 convert_html_to_markdown")

    # convert_html_to_markdown 是函数内 import 的，需要 patch 其源模块
    import brain_base.tools.doc_converter_tool as dc_mod
    monkeypatch.setattr(dc_mod, "convert_html_to_markdown", boom)

    out = ingest_mod.clean_node({
        "cleaned_md": "# pre-filled\n\nbody",
        "raw_html": "",  # raw text 路径下置空
    })

    assert out == {"extraction_status": "ok"}


# -----------------------------------------------------------------------------
# 脚本入口
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
