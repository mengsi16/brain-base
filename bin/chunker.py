#!/usr/bin/env python3
"""chunker.py — 确定性 Markdown 分块脚本

职责：把 raw Markdown 按 H2/H3 标题切成语义块，
      小块合并到 ≥ MIN_CHUNK_CHARS，大块用递归切分兜底，
      保证代码块/表格不被切断。

大模型不再负责物理切分，只负责 enrichment（摘要/关键词/QA）。

用法：
  python bin/chunker.py <raw_md_path> [--output-dir data/docs/chunks] [--min 3500] [--max 5000] [--overlap 200]

输出：一组 chunk .md 文件（无 frontmatter 的 enrichment 字段，
      那些由后续 LLM enrichment 步骤填充）。
"""
import re
import sys
import argparse
from pathlib import Path

# ── 参数 ──
MIN_CHUNK_CHARS = 3500
MAX_CHUNK_CHARS = 5000
OVERLAP_CHARS = 200

# ── Markdown 标题切分 ──

HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

def _split_by_headers(text: str, max_level: int = 3) -> list[dict]:
    """按 H1~H(max_level) 标题切分，返回 [{header, level, content}, ...]"""
    blocks = []
    current = {"header": None, "level": 0, "content": ""}
    
    lines = text.split("\n")
    in_code_fence = False
    
    for line in lines:
        # 跟踪 code fence 状态，fence 内不识别标题
        if line.strip().startswith("```"):
            in_code_fence = not in_code_fence
        
        if not in_code_fence:
            m = HEADER_RE.match(line)
            if m and len(m.group(1)) <= max_level:
                # 遇到新标题，保存当前块
                if current["content"].strip() or current["header"] is not None:
                    blocks.append(current)
                current = {
                    "header": m.group(2).strip(),
                    "level": len(m.group(1)),
                    "content": line + "\n",
                }
                continue
        
        current["content"] += line + "\n"
    
    # 最后一块
    if current["content"].strip() or current["header"] is not None:
        blocks.append(current)
    
    return blocks


def _contains_table(text: str) -> bool:
    """判断文本是否包含 Markdown 表格（3 行以上 | 开头），忽略代码块内的行"""
    lines = text.strip().split("\n")
    in_code = False
    table_lines = 0
    for l in lines:
        if l.strip().startswith("```"):
            in_code = not in_code
            continue
        if not in_code and l.strip().startswith("|"):
            table_lines += 1
    return table_lines >= 3


def _split_table_rows(text: str, max_chars: int) -> list[str]:
    """将含表格的文本按行切分，每组保留表头+分隔行+若干数据行。

    策略：
    1. 提取表格前的非表格文本（前言），与第一组表头合并。
    2. 表头行 + 分隔行（---|---）始终复制到每个分组顶部。
    3. 数据行按字符数累积，接近 max_chars 时截断开新组。
    """
    lines = text.split("\n")
    # 分离：表格前文本 / 表头 / 分隔行 / 数据行 / 表格后文本
    pre_lines = []
    header_line = None
    sep_line = None
    data_lines = []
    post_lines = []
    state = "pre"
    in_code = False
    for line in lines:
        stripped = line.strip()
        # 跟踪代码块状态
        if stripped.startswith("```"):
            in_code = not in_code
            # 代码块边界行归入当前状态对应的列表
            if state == "pre":
                pre_lines.append(line)
            elif state == "post":
                post_lines.append(line)
            else:
                data_lines.append(line)
            continue
        # 代码块内的行不参与表格检测
        if in_code:
            if state == "pre":
                pre_lines.append(line)
            elif state == "post":
                post_lines.append(line)
            else:
                data_lines.append(line)
            continue
        if state == "pre":
            if stripped.startswith("|"):
                header_line = line
                state = "header"
            else:
                pre_lines.append(line)
        elif state == "header":
            # 分隔行：|---|---| 或 | --- | --- |
            if stripped.startswith("|") and re.match(r"^[|\s\-:]+$", stripped):
                sep_line = line
                state = "data"
            else:
                # 可能多行表头（罕见），当作数据行
                data_lines.append(line)
                state = "data"
        elif state == "data":
            if stripped.startswith("|"):
                data_lines.append(line)
            else:
                state = "post"
                post_lines.append(line)
        else:
            post_lines.append(line)

    if header_line is None or sep_line is None:
        # 不像标准表格，回退为不切
        return [text]

    # 构建每组：表头 + 分隔行 + 若干数据行
    # 第一组包含 pre_lines（表格前文本/代码），后续组只复制表头+分隔行
    first_header = "\n".join(pre_lines + [header_line, sep_line])
    rest_header = "\n".join([header_line, sep_line])
    first_header_len = len(first_header) + 1
    rest_header_len = len(rest_header) + 1

    groups = []
    current = first_header
    current_len = first_header_len
    is_first = True

    for dl in data_lines:
        line_len = len(dl) + 1
        cur_header_len = first_header_len if is_first else rest_header_len
        if current_len + line_len > max_chars and current_len > cur_header_len:
            # 当前组已超限，开新组（只复制表头+分隔行，不含 pre_lines）
            groups.append(current)
            current = rest_header
            current_len = rest_header_len
            is_first = False
        current += "\n" + dl
        current_len += line_len

    if current.strip():
        groups.append(current)

    # 表格后文本追加到最后一组
    if post_lines:
        post_text = "\n".join(post_lines).strip()
        if post_text and groups:
            groups[-1] += "\n\n" + post_text
        elif post_text:
            groups.append(post_text)

    return groups if groups else [text]


def _split_oversized(text: str, max_chars: int, overlap: int) -> list[str]:
    """递归字符切分：按 \n\n → \n → 空格 逐级切。

    表格按行切分：保留表头+分隔行，每组复制到顶部，数据行按字符数分组。
    """
    if len(text) <= max_chars:
        return [text]
    
    # 如果包含表格 → 按行切分（保留表头+分隔行，每组复制到顶部）
    if _contains_table(text):
        return _split_table_rows(text, max_chars)
    
    # 1. 先按段落切（\n\n）
    paragraphs = re.split(r"(\n\n+)", text)
    parts = [p for p in paragraphs if p.strip()]
    
    if len(parts) <= 1:
        # 2. 按行切
        parts = [l + "\n" for l in text.split("\n") if l.strip()]
    
    if len(parts) <= 1:
        # 3. 按句子切
        parts = re.split(r"(?<=[.。！？])\s+", text)
    
    chunks = []
    current = ""
    for part in parts:
        # 包含表格的 part → 不可切，独立 chunk
        if _contains_table(part):
            if current.strip():
                chunks.append(current.strip())
                current = ""
            chunks.append(part.strip())
            continue
        
        # 非 table 超大 part → 硬切
        if len(part) > max_chars:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            step = max_chars - overlap
            for start in range(0, len(part), step):
                end = start + max_chars
                chunk = part[start:end]
                if chunk.strip():
                    chunks.append(chunk.strip())
            continue
        
        if len(current) + len(part) <= max_chars:
            current += part
        else:
            if current.strip():
                chunks.append(current.strip())
            # overlap
            if chunks and overlap > 0:
                prev = chunks[-1]
                tail = prev[-overlap:] if len(prev) > overlap else prev
                current = tail + part
            else:
                current = part
    
    
    if current.strip():
        chunks.append(current.strip())
    
    return chunks


def _merge_small_blocks(blocks: list[dict], min_chars: int) -> list[dict]:
    """合并相邻小块，直到 ≥ min_chars。只有最后一个 chunk 允许 < min_chars。"""
    if not blocks:
        return blocks
    
    merged = []
    current = blocks[0].copy()
    
    for i in range(1, len(blocks)):
        cur_len = len(current["content"].strip())
        next_len = len(blocks[i]["content"].strip())
        
        # 当前块不够大，合并
        if cur_len < min_chars:
            current["content"] += "\n\n" + blocks[i]["content"]
            # 如果原来没有 header，用合并进来的 header
            if current["header"] is None and blocks[i]["header"] is not None:
                current["header"] = blocks[i]["header"]
                current["level"] = blocks[i]["level"]
        else:
            merged.append(current)
            current = blocks[i].copy()
    
    merged.append(current)
    return merged


def chunk_markdown(
    text: str,
    min_chars: int = MIN_CHUNK_CHARS,
    max_chars: int = MAX_CHUNK_CHARS,
    overlap: int = OVERLAP_CHARS,
) -> list[str]:
    """主入口：Markdown → 确定性分块列表。
    
    返回 list[str]，每个元素是一个 chunk 的 Markdown 正文。
    """
    # 1. 去掉 frontmatter（如果有）
    fm_match = re.match(r"^---\s*\n.*?\n---\s*\n?(.*)$", text, re.DOTALL)
    body = fm_match.group(1) if fm_match else text
    
    # 2. 整篇 ≤ max_chars → 不切
    if len(body.strip()) <= max_chars:
        return [body.strip()]
    
    # 3. 按 H2/H3 切
    blocks = _split_by_headers(body, max_level=3)
    
    # 4. 合并小块
    blocks = _merge_small_blocks(blocks, min_chars)
    
    # 5. 对超大块递归切分
    chunks = []
    for block in blocks:
        content = block["content"].strip()
        if len(content) > max_chars:
            sub_chunks = _split_oversized(content, max_chars, overlap)
            chunks.extend(sub_chunks)
        else:
            chunks.append(content)
    
    # 6. 最终审计：合并小 chunk（< min_chars 的合并到前一个）
    i = len(chunks) - 1
    while i >= 1:
        if len(chunks[i]) < min_chars:
            chunks[i - 1] = chunks[i - 1] + "\n\n" + chunks[i]
            chunks.pop(i)
        i -= 1
    
    return chunks


# ── 文件输出 ──

def _parse_raw_frontmatter(text: str) -> dict[str, str]:
    """从 raw Markdown 文本提取 frontmatter 字段为 dict。"""
    fm_match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not fm_match:
        return {}
    meta: dict[str, str] = {}
    for line in fm_match.group(1).split("\n"):
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    return meta


def write_chunks(
    raw_path: Path,
    output_dir: Path,
    min_chars: int = MIN_CHUNK_CHARS,
    max_chars: int = MAX_CHUNK_CHARS,
    overlap: int = OVERLAP_CHARS,
) -> list[Path]:
    """读取 raw markdown，分块，写入 chunk 文件。
    
    chunk 文件名格式：<doc_id>-<NNN>.md
    基础 frontmatter 从 raw 继承确定性字段（source_type/url/fetched_at/source/title），
    enrichment 字段（summary/keywords/questions）留空由后续 LLM 步骤填充。
    """
    text = raw_path.read_text(encoding="utf-8")
    
    # 提取 raw frontmatter
    raw_fm = _parse_raw_frontmatter(text)
    doc_id = raw_fm.get("doc_id", "").strip().strip('"').strip("'")
    if not doc_id:
        doc_id = raw_path.stem
    
    chunks = chunk_markdown(text, min_chars, max_chars, overlap)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    
    for i, chunk_body in enumerate(chunks, 1):
        chunk_id = f"{doc_id}-{i:03d}"
        filename = f"{chunk_id}.md"
        
        # 从 raw 继承的确定性字段
        inherit_fields = []
        for key in ("source_type", "url", "fetched_at", "source", "title"):
            val = raw_fm.get(key, "").strip()
            if val:
                inherit_fields.append(f"{key}: {val}")
        
        inherit_block = "\n".join(inherit_fields) + "\n" if inherit_fields else ""
        
        frontmatter = f"""---
doc_id: {doc_id}
chunk_id: {chunk_id}
{inherit_block}summary: ""
keywords: []
questions: []
---

"""
        out_path = output_dir / filename
        out_path.write_text(frontmatter + chunk_body, encoding="utf-8")
        written.append(out_path)
    
    return written


# ── CLI ──

def main():
    parser = argparse.ArgumentParser(description="确定性 Markdown 分块")
    parser.add_argument("raw_path", type=Path, help="raw markdown 文件路径")
    parser.add_argument("--output-dir", type=Path, default=ROOT_DIR / "data" / "docs" / "chunks",
                        help="chunk 输出目录")
    parser.add_argument("--min", type=int, default=MIN_CHUNK_CHARS, help="最小字符数")
    parser.add_argument("--max", type=int, default=MAX_CHUNK_CHARS, help="最大字符数")
    parser.add_argument("--overlap", type=int, default=OVERLAP_CHARS, help="重叠字符数")
    parser.add_argument("--dry-run", action="store_true", help="只输出分块信息，不写文件")
    args = parser.parse_args()
    
    raw_path: Path = args.raw_path
    if not raw_path.exists():
        print(f"文件不存在: {raw_path}", file=sys.stderr)
        sys.exit(1)
    
    text = raw_path.read_text(encoding="utf-8")
    chunks = chunk_markdown(text, args.min, args.max, args.overlap)
    
    print(f"raw: {raw_path.name} ({len(text)} chars)")
    print(f"chunks: {len(chunks)}")
    for i, chunk in enumerate(chunks, 1):
        print(f"  #{i:03d}: {len(chunk)} chars")
    
    if args.dry_run:
        return
    
    written = write_chunks(raw_path, args.output_dir, args.min, args.max, args.overlap)
    print(f"\n写入 {len(written)} 个 chunk 文件:")
    for p in written:
        print(f"  {p}")


ROOT_DIR = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    main()
