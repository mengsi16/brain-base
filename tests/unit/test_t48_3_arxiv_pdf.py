# -*- coding: utf-8 -*-
"""T48.3 单元测试：arxiv_pdf 工具（PDF + MinerU + dedup + raw 落盘）。

覆盖（10 用例）：

URL 规范化（2 用例）：
- test_normalize_arxiv_pdf_url_all_patterns：4 类 URL 规范化
- test_normalize_arxiv_pdf_url_rejects_non_arxiv：非 arxiv 返 None

execute_arxiv_pdf 主流程（5 用例，全 mock）：
- test_execute_arxiv_pdf_invalid_url
- test_execute_arxiv_pdf_dedup_hit_skips_mineru
- test_execute_arxiv_pdf_dedup_miss_calls_mineru
- test_execute_arxiv_pdf_fetch_failure
- test_execute_arxiv_pdf_mineru_failure

ToolSpec 注册 + 调度（2 用例）：
- test_arxiv_pdf_tool_spec_registered
- test_two_arxiv_pdf_serial_via_executor（依赖 T48.1）

evidence + persist 集成（1 用例）：
- test_evidence_passes_raw_path_to_candidate

mock 策略：
- fetch_binary mock 返 fake bytes
- _lookup_by_frontmatter_sha256 mock
- convert_one mock 返 raw_path 指向 tempfile

CLAUDE.md 规则 14 豁免：本测试验证调度拓扑 / 数据流 / dedup 逻辑，不验证 LLM 语义。
真 MinerU 调用留 e2e（耗时 5-10 min/篇）。

契约：md/research/2026-05-19-t48.3-arxiv-pdf-tool-contract.md
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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


def _empty_cfg():
    from brain_base.config import GetInfoConfig
    return GetInfoConfig()


# ---------------------------------------------------------------------------
# A. URL 规范化（2 用例）
# ---------------------------------------------------------------------------


class TestNormalizeArxivPdfUrl:
    """T48.3 D1：normalize_arxiv_pdf_url helper 测试。"""

    def test_normalize_arxiv_pdf_url_all_patterns(self):
        """abs / abs+v / pdf / pdf+v 4 类 URL 全转为统一 PDF 直链。"""
        from brain_base.tools.raw_text_extractor import normalize_arxiv_pdf_url

        cases = [
            ("https://arxiv.org/abs/2501.12345", "https://arxiv.org/pdf/2501.12345.pdf"),
            ("https://arxiv.org/abs/2501.12345v2", "https://arxiv.org/pdf/2501.12345v2.pdf"),
            ("https://arxiv.org/pdf/2501.12345", "https://arxiv.org/pdf/2501.12345.pdf"),
            ("https://arxiv.org/pdf/2501.12345v2.pdf", "https://arxiv.org/pdf/2501.12345v2.pdf"),
            # 末尾 / 容忍
            ("https://arxiv.org/abs/2501.12345/", "https://arxiv.org/pdf/2501.12345.pdf"),
            # 注：arxiv 2007 后新格式 YYMM.NNNNN 是主流；老格式 cs.LG/0501001
            # （含斜杠的 path）当前不支持——LLM 一般也只会拿到新格式 URL，
            # 落到老格式时返 None 让 fallback 到 raw_text 兜底。
        ]
        for input_url, expected in cases:
            got = normalize_arxiv_pdf_url(input_url)
            assert got == expected, f"input={input_url!r}: expected {expected!r}, got {got!r}"

    def test_normalize_arxiv_pdf_url_rejects_non_arxiv(self):
        """非 arxiv URL 返 None。"""
        from brain_base.tools.raw_text_extractor import normalize_arxiv_pdf_url

        rejected = [
            "",
            "https://github.com/torvalds/linux",
            "https://google.com/search?q=arxiv",
            "https://arxiv.com/abs/2501.12345",  # 错 host (.com 不是 .org)
            "https://arxiv.org/list/cs.LG",  # 不是 abs/pdf
            "https://arxiv.org",  # 无路径
            "not-a-url",
        ]
        for url in rejected:
            got = normalize_arxiv_pdf_url(url)
            assert got is None, f"input={url!r}: expected None, got {got!r}"


# ---------------------------------------------------------------------------
# B. execute_arxiv_pdf 主流程（5 用例）
# ---------------------------------------------------------------------------


class _LLMSentinel:
    def with_structured_output(self, schema, **kwargs):
        raise AssertionError("execute_arxiv_pdf 测试不应触发 LLM 调用")

    def invoke(self, messages):
        raise AssertionError("execute_arxiv_pdf 测试不应触发 LLM 调用")


class TestExecuteArxivPdfMainFlow:
    """T48.3：execute_arxiv_pdf 主流程 mock 单测。"""

    def test_execute_arxiv_pdf_invalid_url(self):
        """非 arxiv URL → 直接返 error，不调任何 IO。"""
        from brain_base.nodes.qa_tools import execute_arxiv_pdf

        out = _run(execute_arxiv_pdf(
            {"url": "https://github.com/x/y"},
            _LLMSentinel(),
            _empty_cfg(),
        ))
        assert out["error"]
        assert "not an arxiv URL" in out["error"]
        assert out["score"] == 0
        assert out["markdown"] == ""

    def test_execute_arxiv_pdf_empty_url(self):
        """空 URL → error: empty url。"""
        from brain_base.nodes.qa_tools import execute_arxiv_pdf

        out = _run(execute_arxiv_pdf(
            {"url": ""},
            _LLMSentinel(),
            _empty_cfg(),
        ))
        assert out["error"] == "empty url"

    def test_execute_arxiv_pdf_dedup_hit_skips_mineru(self, monkeypatch, tmp_path):
        """SHA-256 命中 → 直接读 existing raw md，不调 convert_one。

        关键断言：
        - convert_one 不被调用（spy 计数 = 0）
        - score=60（dedup hit 标记）
        - raw_path 指向 existing 路径
        - sha256_hash 透传
        """
        from brain_base.nodes import qa_tools

        fake_pdf_bytes = b"%PDF-1.4 fake test pdf binary content"
        expected_sha = hashlib.sha256(fake_pdf_bytes).hexdigest()

        # 准备 existing raw md
        existing_raw = tmp_path / "arxiv-2501_12345-20260101.md"
        existing_raw.write_text(
            "---\ndoc_id: arxiv-2501_12345-20260101\n"
            f"content_sha256: {expected_sha}\n---\n\n"
            "# Existing Paper Title\n\n"
            "This is an existing arxiv paper. " * 200,
            encoding="utf-8",
        )

        # mock fetch_binary
        async def fake_fetch_binary(url, timeout=None, extra_headers=None):
            return fake_pdf_bytes

        # mock _lookup_by_frontmatter_sha256
        def fake_lookup(sha256, raw_dir=None):
            if sha256 == expected_sha:
                return {"doc_id": existing_raw.stem, "raw_path": str(existing_raw)}
            return None

        # spy convert_one：不应被调用
        convert_called = {"n": 0}
        def spy_convert_one(*args, **kwargs):
            convert_called["n"] += 1
            raise AssertionError("convert_one should not be called on dedup hit")

        monkeypatch.setattr(
            "brain_base.tools.web_fetcher.fetch_binary", fake_fetch_binary,
        )
        monkeypatch.setattr(
            "brain_base.nodes.ingest_file._lookup_by_frontmatter_sha256", fake_lookup,
        )
        # 这里需要 patch importlib.import_module("bin.doc-converter").convert_one
        import importlib
        doc_conv_mod = importlib.import_module("bin.doc-converter")
        monkeypatch.setattr(doc_conv_mod, "convert_one", spy_convert_one)

        out = _run(qa_tools.execute_arxiv_pdf(
            {"url": "https://arxiv.org/abs/2501.12345"},
            _LLMSentinel(),
            _empty_cfg(),
        ))

        assert convert_called["n"] == 0, "convert_one should not be called on dedup hit"
        assert not out.get("error"), f"expected no error, got {out.get('error')!r}"
        assert out["score"] == 60
        assert out["raw_path"] == str(existing_raw)
        assert out["sha256_hash"] == expected_sha
        assert out["doc_id"] == existing_raw.stem
        assert "Existing Paper Title" in out["title"]
        assert "Existing Paper Title" in out["markdown"]
        assert len(out["markdown"]) <= 3000
        assert out["source_url"] == "https://arxiv.org/pdf/2501.12345.pdf"

    def test_execute_arxiv_pdf_dedup_miss_calls_mineru(self, monkeypatch, tmp_path):
        """SHA-256 未命中 → 调 convert_one → 落 raw md → 返 ToolResult。"""
        from brain_base.nodes import qa_tools

        fake_pdf_bytes = b"%PDF-1.4 unique pdf for miss test"
        expected_sha = hashlib.sha256(fake_pdf_bytes).hexdigest()

        # mineru 输出目录（mock convert_one 在 tmp_path 下创建 raw md）
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        fake_doc_id = "arxiv-fake-1234-20260519"
        fake_raw_path = raw_dir / f"{fake_doc_id}.md"

        async def fake_fetch_binary(url, timeout=None, extra_headers=None):
            return fake_pdf_bytes

        # dedup miss
        def fake_lookup(sha256, raw_dir=None):
            return None

        # mock convert_one：写一个测试 raw md
        convert_called = {"n": 0}
        def fake_convert_one(input_path, output_dir, uploads_dir, **kwargs):
            convert_called["n"] += 1
            # 模拟 convert_one 写 raw markdown body（无 frontmatter）
            output_dir.mkdir(parents=True, exist_ok=True)
            fake_raw_path_in_output = output_dir / f"{fake_doc_id}.md"
            fake_raw_path_in_output.write_text(
                "# Attention Is All You Need\n\n## Abstract\n\n"
                "The dominant sequence transduction models are based on complex "
                "recurrent or convolutional neural networks. " * 100,
                encoding="utf-8",
            )
            return {
                "doc_id": fake_doc_id,
                "raw_path": str(fake_raw_path_in_output),
                "archive_dir": str(uploads_dir / fake_doc_id),
                "original_file": str(uploads_dir / fake_doc_id / "in.pdf"),
                "images_dir": None,
                "has_images": False,
                "char_count": 5000,
                "format": "pdf",
                "backend": "mineru",
            }

        monkeypatch.setattr(
            "brain_base.tools.web_fetcher.fetch_binary", fake_fetch_binary,
        )
        monkeypatch.setattr(
            "brain_base.nodes.ingest_file._lookup_by_frontmatter_sha256", fake_lookup,
        )
        import importlib
        doc_conv_mod = importlib.import_module("bin.doc-converter")
        monkeypatch.setattr(doc_conv_mod, "convert_one", fake_convert_one)

        # 关键：execute_arxiv_pdf 内部硬编码 raw_dir = Path("data/docs/raw")
        # mock convert_one 接收 output_dir 参数 → 实际会写到 data/docs/raw/
        # 测试用 monkeypatch.chdir 让相对路径写到 tmp_path
        monkeypatch.chdir(tmp_path)

        out = _run(qa_tools.execute_arxiv_pdf(
            {"url": "https://arxiv.org/abs/1706.03762v7"},
            _LLMSentinel(),
            _empty_cfg(),
        ))

        assert convert_called["n"] == 1, "convert_one should be called once on dedup miss"
        assert not out.get("error"), f"expected no error, got {out.get('error')!r}"
        assert out["score"] == 70
        assert out["sha256_hash"] == expected_sha
        assert out["doc_id"] == fake_doc_id
        assert "Attention Is All You Need" in out["title"]
        assert "Attention Is All You Need" in out["markdown"]
        assert len(out["markdown"]) <= 3000
        assert out["source_url"] == "https://arxiv.org/pdf/1706.03762v7.pdf"
        # raw_path 指向 convert_one 写入的实际位置
        assert out["raw_path"]
        assert Path(out["raw_path"]).is_file()
        # 写盘后 frontmatter 应含 sha256（用于后续 dedup）
        rewritten = Path(out["raw_path"]).read_text(encoding="utf-8")
        assert "content_sha256: " + expected_sha in rewritten
        assert "arxiv_id: 1706.03762v7" in rewritten

    def test_execute_arxiv_pdf_fetch_failure(self, monkeypatch):
        """fetch_binary 抛 RuntimeError → ToolResult.error 含 'pdf download failed'。"""
        from brain_base.nodes import qa_tools

        async def failing_fetch(url, timeout=None, extra_headers=None):
            raise RuntimeError("simulated network failure")

        monkeypatch.setattr(
            "brain_base.tools.web_fetcher.fetch_binary", failing_fetch,
        )

        out = _run(qa_tools.execute_arxiv_pdf(
            {"url": "https://arxiv.org/abs/2501.12345"},
            _LLMSentinel(),
            _empty_cfg(),
        ))

        assert out.get("error")
        assert "pdf download failed" in out["error"]
        assert "RuntimeError" in out["error"]
        assert out["score"] == 0
        assert out["markdown"] == ""

    def test_execute_arxiv_pdf_mineru_failure(self, monkeypatch, tmp_path):
        """convert_one 抛错 → ToolResult.error 含 'mineru convert failed'。"""
        from brain_base.nodes import qa_tools

        fake_pdf_bytes = b"%PDF-1.4 invalid pdf"

        async def fake_fetch_binary(url, timeout=None, extra_headers=None):
            return fake_pdf_bytes

        def fake_lookup(sha256, raw_dir=None):
            return None

        def failing_convert_one(*args, **kwargs):
            raise RuntimeError("MinerU OOM during prefill")

        monkeypatch.setattr(
            "brain_base.tools.web_fetcher.fetch_binary", fake_fetch_binary,
        )
        monkeypatch.setattr(
            "brain_base.nodes.ingest_file._lookup_by_frontmatter_sha256", fake_lookup,
        )
        import importlib
        doc_conv_mod = importlib.import_module("bin.doc-converter")
        monkeypatch.setattr(doc_conv_mod, "convert_one", failing_convert_one)

        monkeypatch.chdir(tmp_path)

        out = _run(qa_tools.execute_arxiv_pdf(
            {"url": "https://arxiv.org/abs/2501.12345"},
            _LLMSentinel(),
            _empty_cfg(),
        ))

        assert out.get("error")
        assert "mineru convert failed" in out["error"]
        assert "MinerU OOM" in out["error"]
        assert out["score"] == 0


# ---------------------------------------------------------------------------
# C. ToolSpec 注册 + 调度（2 用例）
# ---------------------------------------------------------------------------


class TestArxivPdfToolSpec:
    """T48.3：ToolSpec 注册到 TOOL_REGISTRY + 调度行为。"""

    def test_arxiv_pdf_tool_spec_registered(self):
        """TOOL_REGISTRY['arxiv_pdf'] 存在 + 关键字段正确。"""
        from brain_base.nodes.qa_tools import TOOL_REGISTRY, execute_arxiv_pdf

        assert "arxiv_pdf" in TOOL_REGISTRY, "arxiv_pdf 未注册"
        spec = TOOL_REGISTRY["arxiv_pdf"]
        assert spec.name == "arxiv_pdf"
        assert spec.gpu is True, "arxiv_pdf 应标 gpu=True（MinerU 占 14GB VRAM）"
        assert spec.parallel_ok is False, (
            "arxiv_pdf 必须 parallel_ok=False（T48.1 串行化生效防 OOM）"
        )
        assert spec.is_async is True, (
            "arxiv_pdf 应 is_async=True（T48.2 D5 验证 async 路径不重启 chromium）"
        )
        assert "mineru" in spec.requires
        assert "playwright" in spec.requires
        assert spec.fn is execute_arxiv_pdf
        # description 包含关键提示
        assert "≤2" in spec.description or "GPU" in spec.description, (
            "description 应包含 ≤2 arxiv_pdf 提示（避免 LLM 过量并发）"
        )

    def test_two_arxiv_pdf_serial_via_executor(self, monkeypatch):
        """fan-out [arxiv_pdf(A), arxiv_pdf(B)] → T48.1 双队列串行（B.start ≥ A.end）。

        模拟 LLM 同跳吐两个 arxiv_pdf 的关键场景，验证不会并发触发 OOM。
        """
        from brain_base.nodes.qa_intent import create_intent_executor

        timing: list[tuple[str, float, float]] = []

        async def slow_arxiv_fn(tool_args, llm, cfg):
            url = tool_args.get("url", "?")
            start = time.perf_counter()
            await asyncio.sleep(0.1)  # 模拟 MinerU
            end = time.perf_counter()
            timing.append((url, start, end))
            return {
                "markdown": f"# Mock paper for {url}",
                "source_url": url,
                "title": f"Mock {url}",
                "score": 70,
                "raw_path": f"/tmp/mock_{url[-10:]}.md",
                "sha256_hash": "fake" + url[-12:],
                "doc_id": f"mock-{url[-10:]}",
            }

        # patch ToolSpec.fn 为 slow_arxiv_fn（保留 parallel_ok=False / is_async=True）
        from brain_base.nodes import qa_tools
        from brain_base.nodes.qa_tools import ToolSpec
        original_spec = qa_tools.TOOL_REGISTRY["arxiv_pdf"]
        spy_spec = ToolSpec(
            name="arxiv_pdf",
            description=original_spec.description,
            requires=original_spec.requires,
            gpu=True,
            parallel_ok=False,  # 关键：保 False 触发 T48.1 串行队列
            is_async=True,
            fn=slow_arxiv_fn,
        )
        monkeypatch.setitem(qa_tools.TOOL_REGISTRY, "arxiv_pdf", spy_spec)

        node = create_intent_executor(_LLMSentinel(), _empty_cfg())
        state = {
            "current_intent_plan": {
                "next_actions": [
                    {
                        "tool_name": "arxiv_pdf",
                        "tool_args": {"url": "https://arxiv.org/abs/2501.A_paper"},
                        "purpose": "test paper A",
                    },
                    {
                        "tool_name": "arxiv_pdf",
                        "tool_args": {"url": "https://arxiv.org/abs/2501.B_paper"},
                        "purpose": "test paper B",
                    },
                ],
                "reasoning": "test serial arxiv_pdf",
                "early_exit": False,
            },
        }
        out = _run(node(state))
        results = out["current_action_results"]

        assert len(results) == 2
        # idx 对齐
        assert "A_paper" in results[0]["source_url"]
        assert "B_paper" in results[1]["source_url"]

        # 关键：两个 arxiv_pdf 严格顺序（A.end ≤ B.start + 0.001s 容差）
        assert len(timing) == 2
        a, b = timing[0], timing[1]
        assert "A_paper" in a[0] and "B_paper" in b[0], (
            f"timing 顺序错位 a={a} b={b}"
        )
        assert a[2] <= b[1] + 0.001, (
            f"arxiv_pdf 应串行，但 A.end={a[2]:.3f} > B.start={b[1]:.3f}"
        )


# ---------------------------------------------------------------------------
# D. evidence + persist 集成（1 用例）
# ---------------------------------------------------------------------------


class TestArxivPdfEvidenceIntegration:
    """T48.3：evidence 透传 + write_raw_one fast-path 集成。"""

    def test_evidence_passes_raw_path_to_candidate(self):
        """ToolResult.raw_path → Evidence.raw_path → candidate.raw_path 透传链。

        验证 _tool_result_to_evidence 与 merge_evidence_node 的 raw_path 透传。
        """
        from brain_base.nodes.qa_intent import (
            _tool_result_to_evidence,
            merge_evidence_node,
        )

        # 模拟 arxiv_pdf 工具返的 ToolResult
        tool_result = {
            "tool_name": "arxiv_pdf",
            "tool_args": {"url": "https://arxiv.org/abs/2501.12345v2"},
            "purpose": "test purpose",
            "markdown": "# Paper Title\n\nFull content here.",
            "source_url": "https://arxiv.org/pdf/2501.12345v2.pdf",
            "title": "Paper Title",
            "summary": "Short summary",
            "score": 70,
            "error": "",
            # T48.3 新字段
            "raw_path": "/tmp/data/docs/raw/arxiv-2501.12345v2-20260519.md",
            "sha256_hash": "abc123sha256deadbeef",
            "doc_id": "arxiv-2501.12345v2-20260519",
        }

        # _tool_result_to_evidence 透传
        ev = _tool_result_to_evidence(tool_result, purpose="fallback")
        assert ev is not None
        assert ev["raw_path"] == "/tmp/data/docs/raw/arxiv-2501.12345v2-20260519.md"
        # sha256_hash 优先工具自报（PDF binary sha256），不是 markdown 重算
        assert ev["sha256_hash"] == "abc123sha256deadbeef"

        # merge_evidence_node 透传到 candidate
        state = {
            "evidence_pool": [ev],
        }
        out = merge_evidence_node(state)
        candidates = out["get_info_candidates"]
        assert len(candidates) == 1
        cand = candidates[0]
        assert cand["raw_path"] == "/tmp/data/docs/raw/arxiv-2501.12345v2-20260519.md"
        assert cand["content_sha256"] == "abc123sha256deadbeef"
