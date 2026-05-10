# -*- coding: utf-8 -*-
"""lexical_grep AND 语义单元测试（T23）。

T23 重构后：
- 函数 ``grep_keywords_and(keywords) -> int``，AND 语义；
- 同一文件必须含**所有** keywords 才算命中 1 次；
- 一个文件即使含全部关键词多次也只计 1。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain_base.tools.lexical_grep import grep_keywords_and


def _write(p: Path, content: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


@pytest.fixture
def tmp_corpus(tmp_path):
    chunks = tmp_path / "chunks"
    raw = tmp_path / "raw"
    chunks.mkdir(parents=True, exist_ok=True)
    raw.mkdir(parents=True, exist_ok=True)
    return chunks, raw


def test_and_single_keyword_hit(tmp_corpus):
    """单关键词 → 任何含该词的文件都算 1 次命中。"""
    chunks, raw = tmp_corpus
    _write(chunks / "a.md", "RAGFlow 是一个 RAG 框架。")
    _write(chunks / "b.md", "无关内容。")

    assert grep_keywords_and(["ragflow"], chunks, raw) == 1


def test_and_two_keywords_same_file(tmp_corpus):
    """两个关键词同时出现在同一文件 → 命中 1。"""
    chunks, raw = tmp_corpus
    _write(chunks / "a.md", "如何启动 ragflow：先 docker compose up...")
    _write(chunks / "b.md", "ragflow 项目介绍：基于 LLM 的 RAG。")

    # a.md 含 'ragflow' + '启动'；b.md 只含 'ragflow' 不含 '启动'
    assert grep_keywords_and(["ragflow", "启动"], chunks, raw) == 1


def test_and_keywords_in_different_files_no_hit(tmp_corpus):
    """两词分别在不同文件 → 0 命中（AND 语义关键测试）。"""
    chunks, raw = tmp_corpus
    _write(chunks / "a.md", "openclaw 项目介绍。")
    _write(chunks / "b.md", "怎么卸载工具：执行 uninstall...")

    # a.md 含 openclaw 不含 卸载；b.md 含 卸载 不含 openclaw → 0
    assert grep_keywords_and(["openclaw", "卸载"], chunks, raw) == 0


def test_and_three_keywords_strict(tmp_corpus):
    """三个关键词 → 同文件必须三个都有才算命中。"""
    chunks, raw = tmp_corpus
    _write(chunks / "a.md", "ragflow 启动 docker 步骤说明")  # 都有
    _write(chunks / "b.md", "ragflow 启动指南，不涉及容器环境")  # 缺 docker
    _write(chunks / "c.md", "无关")

    assert grep_keywords_and(["ragflow", "启动", "docker"], chunks, raw) == 1


def test_and_multiple_files_match(tmp_corpus):
    """多个文件都同时含全部关键词 → 命中数 = 文件数。"""
    chunks, raw = tmp_corpus
    _write(chunks / "a.md", "ragflow 启动方法 1")
    _write(chunks / "b.md", "ragflow 的启动注意事项")
    _write(chunks / "c.md", "ragflow 架构介绍，仅讲原理")

    assert grep_keywords_and(["ragflow", "启动"], chunks, raw) == 2


def test_repeat_in_file_counts_once(tmp_corpus):
    """同文件同关键词出现 N 次 → 仍只计 1。"""
    chunks, raw = tmp_corpus
    _write(chunks / "a.md", "ragflow ragflow ragflow ragflow ragflow")

    assert grep_keywords_and(["ragflow"], chunks, raw) == 1


def test_case_insensitive(tmp_corpus):
    """大小写不敏感：RAGFlow / RAGFLOW / ragflow 都视为同一词。"""
    chunks, raw = tmp_corpus
    _write(chunks / "a.md", "RAGFlow 是 RAG 框架")
    _write(chunks / "b.md", "RAGFLOW 大写写法")

    assert grep_keywords_and(["ragflow"], chunks, raw) == 2


def test_raw_dir_also_scanned(tmp_corpus):
    """raw 目录的命中也要统计。"""
    chunks, raw = tmp_corpus
    _write(chunks / "a.md", "无关")
    _write(raw / "raw_a.md", "ragflow 启动文档")

    assert grep_keywords_and(["ragflow", "启动"], chunks, raw) == 1


def test_chunks_and_raw_summed(tmp_corpus):
    """chunks 和 raw 同时命中 → 计数累加。"""
    chunks, raw = tmp_corpus
    _write(chunks / "a.md", "ragflow 启动方法")
    _write(raw / "raw_a.md", "ragflow 启动详细文档")

    assert grep_keywords_and(["ragflow", "启动"], chunks, raw) == 2


def test_no_keywords_returns_zero(tmp_corpus):
    """空 keywords → 直接返回 0（不报错）。"""
    chunks, raw = tmp_corpus
    _write(chunks / "a.md", "任意内容")

    assert grep_keywords_and([], chunks, raw) == 0


def test_only_empty_strings_returns_zero(tmp_corpus):
    """全空字符串 keywords → 归一化后空列表 → 返回 0。"""
    chunks, raw = tmp_corpus
    _write(chunks / "a.md", "任意内容")

    assert grep_keywords_and(["", "  "], chunks, raw) == 0


def test_directory_missing(tmp_path):
    """两个目录都不存在 → 返回 0，不抛错。"""
    chunks = tmp_path / "no_chunks"
    raw = tmp_path / "no_raw"

    assert grep_keywords_and(["ragflow"], chunks, raw) == 0


def test_unreadable_file_skipped(tmp_corpus):
    """单文件读取异常时跳过，不影响其他文件统计。"""
    chunks, raw = tmp_corpus
    _write(chunks / "good.md", "ragflow 启动")
    bad = chunks / "bad.md"
    bad.write_bytes(b"\xff\xfe invalid binary")

    # bad.md 用 errors='replace' 不会真抛错，只是字符乱掉；good.md 该命中仍命中
    assert grep_keywords_and(["ragflow", "启动"], chunks, raw) == 1


def test_max_files_scanned_limit(tmp_corpus):
    """超过 max_files_scanned 的硬保护：只扫前 N 个文件。"""
    chunks, raw = tmp_corpus
    for i in range(10):
        _write(chunks / f"doc{i:02d}.md", "ragflow 启动")

    hits = grep_keywords_and(
        ["ragflow", "启动"], chunks, raw, max_files_scanned=3
    )
    # 只扫 3 个 → 命中 ≤ 3
    assert hits <= 3
