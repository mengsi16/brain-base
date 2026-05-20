# -*- coding: utf-8 -*-
"""T48.4 单元测试：github_raw 工具 + _match_github_url 共享 helper。

覆盖（13 用例，契约 §5）：

`_match_github_url` 纯函数（4 用例）：
- repo root → kind=repo_root
- blob/raw/tree → kind 正确 + owner/repo/branch/path
- issue/PR/wiki/gist/search → 返 None
- 非 github host → 返 None

async `try_github_raw`（5 用例）：
- 仓库根 main/README.md 命中
- 仓库根 master fallback
- blob 转 raw
- README_zh fallback
- IO 异常静默 → 返 None

`execute_github_raw` 工具（2 用例）：
- 成功 → markdown / source_url / title
- 不支持 URL → error 含 "unsupported"

ToolSpec 注册 + sync 路径保留（2 用例）：
- TOOL_REGISTRY['github_raw'] 关键字段
- sync `_try_github` 行为零变化（T50.1 前供 ingest_url.fetch_node 保护；T50 后
  保留路径供 try_raw_text 内部 dispatch + sync test 用例）

mock 策略：monkeypatch ``_http_get_async``（async 版）/ ``_http_get``（sync 版）
完全避免真实网络。

CLAUDE.md 规则 14 豁免：本测试验证 URL 路由 / 正则 / 调度，不涉及 LLM 语义。
真 GitHub 拉取 + LLM 选工具留 e2e（用户自己跑或 T50 baseline）。

契约：md/research/2026-05-19-t48.4-github-raw-tool-contract.md
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
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _make_async_http_get_mock(routes: dict, default_status: int = 404):
    """返回 mock _http_get_async：按 URL 精确匹配返回 (status, body)。"""
    call_log: list[str] = []

    async def mock(url: str, timeout: float = 30.0):
        call_log.append(url)
        if url in routes:
            v = routes[url]
            if callable(v):
                return v(url)
            return v
        return default_status, ""

    mock.call_log = call_log  # type: ignore[attr-defined]
    return mock


class _LLMSentinel:
    def with_structured_output(self, schema, **kwargs):
        raise AssertionError("github_raw 测试不应触发 LLM 调用")

    def invoke(self, messages):
        raise AssertionError("github_raw 测试不应触发 LLM 调用")


def _empty_cfg():
    from brain_base.config import GetInfoConfig
    return GetInfoConfig()


# ---------------------------------------------------------------------------
# A. _match_github_url 纯函数（4 用例）
# ---------------------------------------------------------------------------


class TestMatchGithubUrl:
    """T48.4 D4：_match_github_url 共享 helper 验证。"""

    def test_repo_root(self):
        from brain_base.tools.raw_text_extractor import _match_github_url
        out = _match_github_url("https://github.com/torvalds/linux")
        assert out is not None
        assert out["kind"] == "repo_root"
        assert out["owner"] == "torvalds"
        assert out["repo"] == "linux"
        assert out["branch"] is None
        assert out["path"] is None

    def test_blob_raw_tree_kinds(self):
        from brain_base.tools.raw_text_extractor import _match_github_url

        cases = [
            ("https://github.com/X/Y/blob/main/README.md", "blob", "main", "README.md"),
            ("https://github.com/X/Y/raw/master/docs/api.md", "raw", "master", "docs/api.md"),
            ("https://github.com/X/Y/tree/dev/src", "tree", "dev", "src"),
            ("https://github.com/X/Y/blob/v1.2.3/CHANGELOG.md", "blob", "v1.2.3", "CHANGELOG.md"),
        ]
        for url, expected_kind, expected_branch, expected_path in cases:
            out = _match_github_url(url)
            assert out is not None, f"{url!r} should match"
            assert out["kind"] == expected_kind
            assert out["owner"] == "X"
            assert out["repo"] == "Y"
            assert out["branch"] == expected_branch
            assert out["path"] == expected_path

    def test_unsupported_pages_return_none(self):
        from brain_base.tools.raw_text_extractor import _match_github_url

        unsupported = [
            "https://github.com/X/Y/issues/123",
            "https://github.com/X/Y/pulls",
            "https://github.com/X/Y/wiki/Page",
            "https://github.com/X/Y/search?q=foo",
            "https://gist.github.com/somebody/abc123",
            "https://github.com/X/Y/releases",
            "https://github.com/orgs/X/teams",
        ]
        for url in unsupported:
            out = _match_github_url(url)
            # repo_root / blob / tree / raw 之外的所有路径——某些仍可能被 _GITHUB_REPO_RE
            # 匹配（如 /X/Y/issues 被当 repo_root），但这是已知行为；本测试关注的是
            # _match_github_url 的核心 4 类 + None 行为
            # 实际上 /X/Y/issues 路径不会被 repo_root 正则 ^/[^/]+/[^/]+/?$ 匹配（多了 /issues 段）
            if "issues" in url or "pulls" in url or "wiki" in url or "search" in url or "releases" in url or "teams" in url:
                assert out is None, f"{url!r} should not match (got {out!r})"
            elif "gist." in url:
                assert out is None, f"gist host should not match"

    def test_non_github_host_returns_none(self):
        from brain_base.tools.raw_text_extractor import _match_github_url

        for url in [
            "",
            "https://gitlab.com/X/Y",
            "https://example.com/github.com",
            "https://raw.githubusercontent.com/X/Y/main/README.md",  # raw 域不算 github.com
            "not-a-url",
        ]:
            assert _match_github_url(url) is None, f"{url!r} should not match"


# ---------------------------------------------------------------------------
# B. async try_github_raw（5 用例）
# ---------------------------------------------------------------------------


class TestTryGithubRawAsync:
    """T48.4：async try_github_raw mock IO 验证。"""

    def test_repo_root_main_readme_hit(self, monkeypatch):
        """github.com/X/Y → main/README.md 命中后停止探测。"""
        from brain_base.tools import raw_text_extractor as rte

        body = "# RAG-Anything\n\n核心特性\n\n- 多模态检索"
        routes = {
            "https://raw.githubusercontent.com/HKUDS/RAG-Anything/main/README.md": (200, body),
        }
        mock = _make_async_http_get_mock(routes)
        monkeypatch.setattr(rte, "_http_get_async", mock)

        out = _run(rte.try_github_raw("https://github.com/HKUDS/RAG-Anything"))
        assert out is not None
        assert out["markdown"] == body
        assert out["title"] == "RAG-Anything"
        assert out["source_url"].endswith("/main/README.md")
        # 命中后停止
        assert mock.call_log == [
            "https://raw.githubusercontent.com/HKUDS/RAG-Anything/main/README.md",
        ]

    def test_repo_root_master_fallback(self, monkeypatch):
        """main 三个 README 全 404 → master/README.md 兜底。"""
        from brain_base.tools import raw_text_extractor as rte

        body = "# Old Repo\nlegacy"
        routes = {
            "https://raw.githubusercontent.com/old/repo/master/README.md": (200, body),
        }
        mock = _make_async_http_get_mock(routes)
        monkeypatch.setattr(rte, "_http_get_async", mock)

        out = _run(rte.try_github_raw("https://github.com/old/repo"))
        assert out is not None
        assert out["source_url"].endswith("/master/README.md")
        # 主 README 三个 + master README 至少前 1 个
        assert any("main/README.md" in u for u in mock.call_log)
        assert any("master/README.md" in u for u in mock.call_log)

    def test_blob_url_converts_to_raw(self, monkeypatch):
        """blob 文件页 → raw URL 直接转换。"""
        from brain_base.tools import raw_text_extractor as rte

        body = "# API\ncontent"
        routes = {
            "https://raw.githubusercontent.com/HKUDS/RAG-Anything/dev/docs/api.md": (200, body),
        }
        mock = _make_async_http_get_mock(routes)
        monkeypatch.setattr(rte, "_http_get_async", mock)

        out = _run(rte.try_github_raw(
            "https://github.com/HKUDS/RAG-Anything/blob/dev/docs/api.md"
        ))
        assert out is not None
        assert out["source_url"].endswith("/dev/docs/api.md")
        # 单次拉取，不走探测矩阵
        assert mock.call_log == [
            "https://raw.githubusercontent.com/HKUDS/RAG-Anything/dev/docs/api.md",
        ]

    def test_readme_zh_fallback(self, monkeypatch):
        """main/README.md 不存在但 main/README_zh.md 存在 → 命中 README_zh。"""
        from brain_base.tools import raw_text_extractor as rte

        body = "# 项目名\n中文 README"
        routes = {
            "https://raw.githubusercontent.com/zh/repo/main/README_zh.md": (200, body),
        }
        mock = _make_async_http_get_mock(routes)
        monkeypatch.setattr(rte, "_http_get_async", mock)

        out = _run(rte.try_github_raw("https://github.com/zh/repo"))
        assert out is not None
        assert out["source_url"].endswith("/main/README_zh.md")
        assert out["title"] == "项目名"

    def test_io_failure_silently_returns_none(self, monkeypatch):
        """_http_get_async 抛 RuntimeError → 静默返 None 不传播。"""
        from brain_base.tools import raw_text_extractor as rte

        async def boom(url, timeout=30.0):
            raise RuntimeError("network down")

        monkeypatch.setattr(rte, "_http_get_async", boom)

        out = _run(rte.try_github_raw("https://github.com/x/y"))
        assert out is None


# ---------------------------------------------------------------------------
# C. execute_github_raw 工具（2 用例）
# ---------------------------------------------------------------------------


class TestExecuteGithubRawTool:
    """T48.4：execute_github_raw 工具入口验证。"""

    def test_success_returns_markdown(self, monkeypatch):
        """成功命中 → markdown / source_url / title 正确返回。"""
        from brain_base.nodes.qa_tools import execute_github_raw
        from brain_base.tools import raw_text_extractor as rte

        body = "# Test Repo\nbody content"
        routes = {
            "https://raw.githubusercontent.com/X/Y/main/README.md": (200, body),
        }
        mock = _make_async_http_get_mock(routes)
        monkeypatch.setattr(rte, "_http_get_async", mock)

        out = _run(execute_github_raw(
            {"url": "https://github.com/X/Y"},
            _LLMSentinel(),
            _empty_cfg(),
        ))
        assert not out.get("error"), f"unexpected error: {out.get('error')}"
        assert out["markdown"] == body
        assert out["title"] == "Test Repo"
        assert out["source_url"].endswith("/main/README.md")

    def test_unsupported_url_returns_error(self, monkeypatch):
        """非 GitHub URL 或不支持的页面（issue 等）→ error 含 'unsupported'。"""
        from brain_base.nodes.qa_tools import execute_github_raw

        # 1. 非 github host
        out = _run(execute_github_raw(
            {"url": "https://gitlab.com/X/Y"},
            _LLMSentinel(),
            _empty_cfg(),
        ))
        assert out.get("error")
        assert "unsupported" in out["error"].lower()

        # 2. issue 页（_match_github_url 返 None）
        out = _run(execute_github_raw(
            {"url": "https://github.com/X/Y/issues/123"},
            _LLMSentinel(),
            _empty_cfg(),
        ))
        assert out.get("error")
        assert "unsupported" in out["error"].lower()

        # 3. 空 URL
        out = _run(execute_github_raw(
            {"url": ""},
            _LLMSentinel(),
            _empty_cfg(),
        ))
        assert out.get("error") == "empty url"


# ---------------------------------------------------------------------------
# D. ToolSpec 注册 + sync 路径保留（2 用例）
# ---------------------------------------------------------------------------


class TestGithubRawToolSpec:
    """T48.4：ToolSpec 注册 + sync `_try_github` 保留。"""

    def test_github_raw_tool_spec_registered(self):
        """TOOL_REGISTRY['github_raw'] 关键字段正确（D5）。"""
        from brain_base.nodes.qa_tools import TOOL_REGISTRY, execute_github_raw

        assert "github_raw" in TOOL_REGISTRY
        spec = TOOL_REGISTRY["github_raw"]
        assert spec.name == "github_raw"
        assert spec.gpu is False
        assert spec.parallel_ok is True, "github_raw 是纯 IO，应允许并发"
        assert spec.is_async is True, (
            "github_raw 默认 is_async=True（T48.2 D5 验证 async 路径不重启 chromium）"
        )
        assert "playwright" in spec.requires
        assert spec.fn is execute_github_raw
        # description 包含关键提示
        assert "issue" in spec.description or "PR" in spec.description, (
            "description 必须列不支持的 URL 类型，避免 LLM 误用"
        )
        assert "tree" in spec.description, (
            "description 必须列 tree 目录页不支持（#22 修订）"
        )

    def test_raw_text_description_no_longer_advertises_github(self):
        """raw_text ToolSpec.description 不再宣传 GitHub（避免与 github_raw 重叠）。"""
        from brain_base.nodes.qa_tools import TOOL_REGISTRY

        raw_text_spec = TOOL_REGISTRY["raw_text"]
        # description 应明确只列 GitLab / arXiv / RFC
        # GitHub 字样可能仍在（"GitHub 改用 github_raw"），但不应在主描述列入支持列表
        assert "GitLab" in raw_text_spec.description
        assert "arXiv" in raw_text_spec.description
        assert "RFC" in raw_text_spec.description
        # 鼓励用户用 github_raw（明确指引）
        assert "github_raw" in raw_text_spec.description.lower()

    def test_sync_try_github_still_works_for_ingest(self, monkeypatch):
        """sync `_try_github` 行为零变化（T50.1 前供 ingest_url.fetch_node 保护）。

        即使新增 async try_github_raw + helper 抽出后，sync `_try_github` 通过
        ``try_raw_text`` 的 dispatch 表仍能命中——T50.1 前是 ingest_url 快路径，
        T50 后保留供未来潜在 sync 调用方与回归测试覆盖，
        必须保持。
        """
        from brain_base.tools import raw_text_extractor as rte

        body = "# Ingest Path Repo\nbody"
        routes = {
            "https://raw.githubusercontent.com/I/Repo/main/README.md": (200, body),
        }

        def mock_sync_http_get(url, timeout=30.0):
            if url in routes:
                return routes[url]
            return 404, ""

        monkeypatch.setattr(rte, "_http_get", mock_sync_http_get)

        # 通过 try_raw_text（sync 路径）调
        out = rte.try_raw_text("https://github.com/I/Repo")
        assert out is not None
        assert out["markdown"] == body
        assert out["title"] == "Ingest Path Repo"

    def test_async_try_raw_text_async_still_dispatches_github(self, monkeypatch):
        """async `try_raw_text_async` 内部 dispatch 表仍含 github（兜底）。

        即使 raw_text ToolSpec.description 不再宣传 GitHub，旧调用方
        （qa_url_pre_fetch / 误用 raw_text(github URL) 的 LLM）仍能 work。
        """
        from brain_base.tools import raw_text_extractor as rte

        body = "# Fallback Repo\nbody"
        routes = {
            "https://raw.githubusercontent.com/F/Repo/main/README.md": (200, body),
        }
        mock = _make_async_http_get_mock(routes)
        monkeypatch.setattr(rte, "_http_get_async", mock)

        out = _run(rte.try_raw_text_async("https://github.com/F/Repo"))
        assert out is not None
        assert out["markdown"] == body
