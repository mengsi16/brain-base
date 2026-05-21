"""
T57 守门测试：ingest-file 路径 ``frontmatter_node`` 写出的 raw frontmatter
必含 ``source_priority: P1`` 字段，与 ask 路径
``qa_persist:_compute_source_priority`` 真值表对齐
（``source_type == "user-upload"`` → 固定 ``"P1"``）。

执行计划：``md/research/2026-05-21-t57-ingest-file-link-cleanup.md``
契约真值表：``brain_base/nodes/qa_persist.py:202-208``。

Maker-Checker 风格：
- ``test_frontmatter_writes_source_priority_p1`` — 单点断言 P1 字段写入
- ``test_frontmatter_field_set_alignment`` — 字段集合断言（共 10 个字段）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brain_base.nodes.ingest_file import frontmatter_node


@pytest.fixture
def converted_item(tmp_path: Path) -> dict:
    """构造一个 user-upload converted item（模拟 convert_node 输出）。

    raw_path 已存在并写入纯 body markdown（不含 frontmatter），
    模拟 MinerU 转换出来的初始状态；frontmatter_node 负责给它添加 fm 头。
    """
    doc_id = "doc-test-t57"
    raw_path = tmp_path / f"{doc_id}.md"
    body = "# 测试文档标题\n\n正文内容第一段。\n\n正文内容第二段。\n"
    raw_path.write_text(body, encoding="utf-8")

    return {
        "raw_path": str(raw_path),
        "doc_id": doc_id,
        "original_file": "test_t57.pdf",
        "content_sha256": "a" * 64,  # 64 字符 sha256 字面量，绕过 fallback 算 body sha
    }


def _read_frontmatter_lines(raw_path: str) -> list[str]:
    """从 raw markdown 文件读取 frontmatter 区块的字段行（不含 ``---`` 边界）。"""
    text = Path(raw_path).read_text(encoding="utf-8")
    lines = text.split("\n")
    assert lines[0] == "---", f"raw FM 缺起始 ---，实际首行: {lines[0]!r}"

    # 找到结束 ---
    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line == "---":
            end_idx = i
            break
    assert end_idx is not None, f"raw FM 缺结束 ---，文本: {text[:200]!r}"

    return lines[1:end_idx]


def test_frontmatter_writes_source_priority_p1(converted_item: dict) -> None:
    """断言 frontmatter_node 写出的 raw FM 含 ``source_priority: P1`` 一行。

    user-upload 不依赖 fetched_at 时效，按 ``_compute_source_priority:204-205``
    设计应固定 P1（与 official-old 同级，高于 community P2/P3）。
    """
    state = {"converted": [converted_item], "upload_date": "2026-05-21"}

    result = frontmatter_node(state)

    assert result["raw_paths"] == [converted_item["raw_path"]]

    fm_lines = _read_frontmatter_lines(converted_item["raw_path"])

    # 断言点 1：含 source_priority 字段
    priority_lines = [line for line in fm_lines if line.startswith("source_priority:")]
    assert len(priority_lines) == 1, (
        f"raw FM 必须恰好包含 1 行 source_priority 字段，实际 {len(priority_lines)} 行；"
        f"完整 fm 字段: {fm_lines}"
    )

    # 断言点 2：值为 P1（user-upload 固定 P1）
    assert priority_lines[0].strip() == "source_priority: P1", (
        f"user-upload 的 source_priority 必须是 P1，实际: {priority_lines[0]!r}"
    )


def test_frontmatter_field_set_alignment(converted_item: dict) -> None:
    """断言 raw FM 字段集合：9 个共有字段（含 source_priority）+ original_file = 共 10 个字段。

    与 ask 路径 ``qa_persist:_build_raw_frontmatter`` 的 9 字段集合相比，
    ingest-file 路径多 1 个 ``original_file`` 独有字段（用于 user-upload 原始文件名追溯）。
    """
    state = {"converted": [converted_item], "upload_date": "2026-05-21"}
    frontmatter_node(state)

    fm_lines = _read_frontmatter_lines(converted_item["raw_path"])

    # 抽出所有 "key: value" 形式字段的 key 集合
    field_keys = set()
    for line in fm_lines:
        if ":" in line:
            key = line.split(":", 1)[0].strip()
            if key:
                field_keys.add(key)

    expected_keys = {
        # 9 个共有字段（与 ask 路径 _build_raw_frontmatter 对齐）
        "doc_id",
        "title",
        "source",
        "source_type",
        "url",
        "fetched_at",
        "content_sha256",
        "keywords",
        "source_priority",
        # 1 个 ingest-file 独有字段
        "original_file",
    }

    assert field_keys == expected_keys, (
        f"raw FM 字段集合不匹配 T57 契约。\n"
        f"  期望（10 个）: {sorted(expected_keys)}\n"
        f"  实际（{len(field_keys)} 个）: {sorted(field_keys)}\n"
        f"  缺失: {sorted(expected_keys - field_keys)}\n"
        f"  多余: {sorted(field_keys - expected_keys)}"
    )
