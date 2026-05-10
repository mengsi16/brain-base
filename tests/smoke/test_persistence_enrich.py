# -*- coding: utf-8 -*-
"""持久化 enrich 节点 smoke 测试。

聚焦 T13 修复目标：``_chunk_needs_enrich`` 必须把"字段存在但值为空占位符"
正确识别为「需要富化」。

历史 bug：chunker 在 frontmatter 写空占位符 ``summary: ""`` / ``keywords: []`` /
``questions: []``（注释明确说"留空由后续 LLM 填充"），但 ``_chunk_needs_enrich``
只用 ``"summary:" not in text`` 判定缺字段，把空占位符当作"已富化"，
导致 enrich 节点对所有新 chunk 都跳过 → 全库 chunk 的 summary / keywords /
questions 永远空 → 规则 38 reranker 拿不到 summary / sparse 通道丢 keyword
加权 / doc2query 反向问题缺失，hybrid 召回严重退化。

本测试套件守护：
1. 修复后空占位符 → ``_chunk_needs_enrich`` 返回 True。
2. 真已富化的 chunk → 返回 False（不重复跑 LLM 浪费 token）。
3. 完全缺字段（老格式）→ 返回 True（保留原行为）。
4. 真实数据：拿 ``data/docs/chunks/openclaw-*.md`` 直接跑，应返回 True
   （T12 e2e 暴露的真实 bug 数据）。
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 1. 空占位符必须被识别为需要富化（核心修复目标）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frontmatter",
    [
        # summary 空占位符（双引号）
        '---\nsummary: ""\nkeywords: ["a", "b"]\nquestions: ["q1"]\n---\n正文\n',
        # summary 空占位符（单引号）
        "---\nsummary: ''\nkeywords: ['a']\nquestions: ['q']\n---\n正文\n",
        # keywords 空数组
        '---\nsummary: "已写"\nkeywords: []\nquestions: ["q1"]\n---\n正文\n',
        # questions 空数组（带空格）
        '---\nsummary: "已写"\nkeywords: ["a"]\nquestions: [ ]\n---\n正文\n',
        # 三项全空（chunker 默认输出）
        '---\nsummary: ""\nkeywords: []\nquestions: []\n---\n正文\n',
    ],
    ids=[
        "summary_empty_double_quote",
        "summary_empty_single_quote",
        "keywords_empty_array",
        "questions_empty_array_with_space",
        "all_three_empty_chunker_default",
    ],
)
def test_empty_placeholder_needs_enrich(frontmatter: str):
    from brain_base.nodes.persistence import _chunk_needs_enrich

    assert _chunk_needs_enrich(frontmatter) is True, (
        f"空占位符必须被识别为需要富化，但 _chunk_needs_enrich 返回 False。\n"
        f"frontmatter:\n{frontmatter}"
    )


# ---------------------------------------------------------------------------
# 2. 真已富化的 chunk 不应被误判为需要重新富化
# ---------------------------------------------------------------------------


def test_real_enriched_chunk_skips():
    """有真实 summary / keywords / questions 的 chunk 不应再次富化（避免浪费 LLM）。"""
    from brain_base.nodes.persistence import _chunk_needs_enrich

    text = (
        "---\n"
        'summary: "这段讲 LiteLLM 的快速开始。"\n'
        'keywords: ["litellm", "quickstart", "openai"]\n'
        'questions: ["怎么安装 LiteLLM", "LiteLLM 支持哪些模型"]\n'
        "---\n"
        "正文内容...\n"
    )
    assert _chunk_needs_enrich(text) is False


# ---------------------------------------------------------------------------
# 3. 完全缺字段（老格式向前兼容）
# ---------------------------------------------------------------------------


def test_missing_field_needs_enrich():
    """老格式的 chunk frontmatter 完全没有这三个字段 → 仍应返回 True。"""
    from brain_base.nodes.persistence import _chunk_needs_enrich

    text = (
        "---\n"
        "doc_id: example\n"
        "chunk_id: example-001\n"
        "---\n"
        "正文\n"
    )
    assert _chunk_needs_enrich(text) is True


# ---------------------------------------------------------------------------
# 4. 部分字段空、部分有值
# ---------------------------------------------------------------------------


def test_partial_empty_needs_enrich():
    """summary 已写但 keywords 还是空 → 仍需富化（任意一项空都补）。"""
    from brain_base.nodes.persistence import _chunk_needs_enrich

    text = (
        "---\n"
        'summary: "这是真的摘要"\n'
        "keywords: []\n"
        'questions: ["q1", "q2", "q3"]\n'
        "---\n"
        "正文\n"
    )
    assert _chunk_needs_enrich(text) is True


# ---------------------------------------------------------------------------
# 5. 真实数据：T12 e2e 抓的 openclaw chunks
# ---------------------------------------------------------------------------


def test_real_openclaw_chunks_need_enrich():
    """对真实数据验证 _chunk_needs_enrich 的修复。

    测试策略：扫描 openclaw-* chunks，**对仍含空占位符的 chunk** 必须返回 True；
    对已 enrich 过的 chunk 必须返回 False（避免重复 enrich 浪费 token）。

    若环境无 openclaw chunks（CI 等清洁环境）或所有 chunks 都已 enrich 过
    （T17 后跑过 e2e），跳过——不阻断流程。
    """
    import re

    from brain_base.nodes.persistence import _chunk_needs_enrich

    chunks_dir = Path("data/docs/chunks")
    openclaw_chunks = sorted(chunks_dir.glob("openclaw-*.md"))
    if not openclaw_chunks:
        pytest.skip("未找到 openclaw-* chunks（清洁环境，跳过真实数据验证）")

    empty_summary = re.compile(r'^summary\s*:\s*(""|\'\')\s*$', re.MULTILINE)
    empty_keywords = re.compile(r'^keywords\s*:\s*\[\s*\]\s*$', re.MULTILINE)

    chunks_with_empty_placeholder = 0
    for chunk_path in openclaw_chunks:
        text = chunk_path.read_text(encoding="utf-8")
        is_empty = empty_summary.search(text) or empty_keywords.search(text)
        if is_empty:
            chunks_with_empty_placeholder += 1
            assert _chunk_needs_enrich(text) is True, (
                f"{chunk_path.name} 含空占位符 frontmatter 但 _chunk_needs_enrich "
                f"返回 False——T13 修复未生效"
            )
        else:
            assert _chunk_needs_enrich(text) is False, (
                f"{chunk_path.name} 已 enrich（含真实 summary/keywords/questions）但 "
                f"_chunk_needs_enrich 返回 True——会触发重复 enrich 浪费 token"
            )

    if chunks_with_empty_placeholder == 0:
        pytest.skip("所有 openclaw-* chunks 都已 enrich 过，跳过空占位符断言")


# ---------------------------------------------------------------------------
# T17：enrich_node 错误透传 + 重试 + enrich_error 字段
# ---------------------------------------------------------------------------


def _make_chunk_file(tmp_path: Path, name: str = "c-001.md") -> Path:
    """生成一个带空占位符 frontmatter 的 chunk 文件，模拟 chunker 输出。"""
    content = (
        '---\n'
        'doc_id: test-doc\n'
        'chunk_id: test-doc-001\n'
        'source: official-doc\n'
        'summary: ""\n'
        'keywords: []\n'
        'questions: []\n'
        '---\n'
        '# Title\n\n这是用于测试的 chunk 正文。'
    )
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_inject_enrich_error_appends_field():
    """enrich_error 字段不存在时追加到末尾 ---  之前。"""
    from brain_base.nodes._frontmatter import inject_enrich_error

    fm = '---\ndoc_id: x\nsummary: ""\n---'
    out = inject_enrich_error(fm, "ValidationError: keywords too short")
    assert 'enrich_error: "ValidationError: keywords too short"' in out
    # 顺序：原字段在前，enrich_error 追加在闭合 --- 之前
    lines = out.split("\n")
    assert lines[0] == "---"
    assert lines[-1] == "---"
    assert any(l.startswith("enrich_error:") for l in lines[1:-1])


def test_inject_enrich_error_replaces_existing():
    """enrich_error 字段已存在时替换，不重复追加。"""
    from brain_base.nodes._frontmatter import inject_enrich_error

    fm = '---\ndoc_id: x\nenrich_error: "old error"\n---'
    out = inject_enrich_error(fm, "new error")
    assert out.count("enrich_error:") == 1, "字段应替换而非重复追加"
    assert '"new error"' in out
    assert '"old error"' not in out


def test_enrich_node_retries_on_failure_then_succeeds(tmp_path, monkeypatch):
    """LLM 第一次失败、第二次成功 → enriched_count=1，frontmatter 写入正常字段。"""
    from brain_base.agents.schemas import ChunkEnrichment
    from brain_base.nodes import persistence as persistence_module
    from brain_base.nodes.persistence import create_enrich_node

    chunk_path = _make_chunk_file(tmp_path)

    call_count = {"n": 0}

    def fake_invoke_structured(llm, schema, sys_prompt, user_prompt):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient: 429 rate limit")
        return ChunkEnrichment(
            title="测试章节标题",
            summary="测试摘要" * 3,
            keywords=["k1", "k2", "k3", "k4", "k5"],
            questions=["q1?", "q2?", "q3?"],
        )

    monkeypatch.setattr(persistence_module, "invoke_structured", fake_invoke_structured)
    # 把退避缩到 0 让测试快速跑完
    monkeypatch.setattr(persistence_module, "ENRICH_RETRY_BACKOFF_SEC", 0.0)

    node = create_enrich_node(llm=object())
    out = node({"chunk_files": [str(chunk_path)]})
    assert out["enriched"] is True
    assert out["enriched_count"] == 1
    assert out["skipped_count"] == 0
    assert call_count["n"] == 2, f"应该重试 1 次（共调 2 次），实际 {call_count['n']}"

    # 重试成功后的 frontmatter 不应有 enrich_error 字段
    final = chunk_path.read_text(encoding="utf-8")
    assert "enrich_error:" not in final
    # T26.1-a：4 字段全部写回 frontmatter
    assert '"测试章节标题"' in final
    assert '"测试摘要测试摘要测试摘要"' in final
    assert "k1" in final and "k5" in final


def test_enrich_node_writes_error_field_when_all_retries_fail(tmp_path, monkeypatch, caplog):
    """所有重试都失败 → frontmatter 写 enrich_error 字段 + log warning。"""
    import logging

    from brain_base.nodes import persistence as persistence_module
    from brain_base.nodes.persistence import create_enrich_node

    chunk_path = _make_chunk_file(tmp_path)

    def always_fail(llm, schema, sys_prompt, user_prompt):
        raise ValueError("schema mismatch: keywords len < 5")

    monkeypatch.setattr(persistence_module, "invoke_structured", always_fail)
    monkeypatch.setattr(persistence_module, "ENRICH_RETRY_BACKOFF_SEC", 0.0)

    node = create_enrich_node(llm=object())
    with caplog.at_level(logging.WARNING, logger="brain_base.nodes.persistence"):
        out = node({"chunk_files": [str(chunk_path)]})

    # 所有重试失败 → skipped_count=1，enriched_count=0
    assert out["enriched"] is True  # 节点本身不抛错
    assert out["enriched_count"] == 0
    assert out["skipped_count"] == 1

    # frontmatter 写入了 enrich_error 字段，含异常类名 + 信息
    final = chunk_path.read_text(encoding="utf-8")
    assert "enrich_error:" in final, f"frontmatter 必须含 enrich_error 字段；实际:\n{final}"
    assert "ValueError" in final
    assert "schema mismatch" in final

    # log 包含 chunk 名 + 错误信息
    log_text = caplog.text
    assert chunk_path.name in log_text, f"log 应含 chunk 文件名；实际:\n{log_text}"
    assert "LLM 富化失败" in log_text or "重试" in log_text
