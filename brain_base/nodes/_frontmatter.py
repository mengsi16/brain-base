"""
YAML frontmatter 解析与注入工具。

约束（CLAUDE.md 硬约束 19）：`url:` 字段不写 `""`，冒号后留空即可。
约束（CLAUDE.md 硬约束 13）：写入字段前 schema 里必须已存在。

格式约定：
- frontmatter 用 `---` 包围，第一行必须是 `---\n`。
- 标量字段 `key: value`（不去引号），列表字段 `key: ["a", "b"]`（JSON inline）。
- summary / keywords / questions 三个 enrichment 字段写入时统一走
  `inject_enrichment`，已存在替换、不存在追加。
"""

from __future__ import annotations

import json
from typing import Any


def split_frontmatter(text: str) -> tuple[str, str]:
    """拆分 YAML frontmatter 与正文。

    无 frontmatter 时返回 `("", text)`；有则返回 `("---\\nfm\\n---", body)`。
    保留首尾 `---` 在 fm 里，方便 inject 后整体替换。
    """
    if not text.startswith("---\n"):
        return "", text
    parts = text.split("\n---\n", 1)
    if len(parts) != 2:
        return "", text
    return parts[0] + "\n---", parts[1]


def parse_frontmatter(text: str) -> dict[str, Any]:
    """把 frontmatter 块解析成字典。

    单行 `key: value` → str；JSON 数组形式 → list。
    解析失败的字段保留原始字符串。
    """
    fm, _ = split_frontmatter(text)
    if not fm:
        return {}
    meta: dict[str, Any] = {}
    for line in fm.splitlines():
        if line.startswith("---"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value.startswith("[") and value.endswith("]"):
            try:
                meta[key] = json.loads(value)
                continue
            except json.JSONDecodeError:
                pass
        meta[key] = value
    return meta


def dump_frontmatter(meta: dict[str, Any]) -> str:
    """把字典 dump 成 frontmatter 块（含首尾 `---`）。

    list / dict 值 → JSON inline；其他 → str。
    None 或空字符串值仍写出（保持 `key: ` 形式，便于人工填充）。
    """
    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, (list, dict)):
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        elif value is None:
            lines.append(f"{key}:")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def inject_enrichment(
    fm: str,
    *,
    title: str,
    summary: str,
    keywords: list[str],
    questions: list[str],
) -> str:
    """把 enrichment 四字段注入 frontmatter，已存在替换、不存在追加。

    fm 必须包含首尾 `---`（即 split_frontmatter 的第一个返回值）。

    T26.1-a：升级到 4 字段（加 title）。title 行覆盖 chunker 透传的 doc 级 title
    （chunk 级 title 优先于 doc 级 title）。强制使用 keyword-only 参数，避免
    位置参数顺序歧义。
    """
    if not fm:
        return fm
    title_line = f"title: {json.dumps(title, ensure_ascii=False)}"
    summary_line = f"summary: {json.dumps(summary, ensure_ascii=False)}"
    keywords_line = f"keywords: {json.dumps(keywords, ensure_ascii=False)}"
    questions_line = f"questions: {json.dumps(questions, ensure_ascii=False)}"

    seen = {"title": False, "summary": False, "keywords": False, "questions": False}
    out: list[str] = []
    for line in fm.split("\n"):
        if line.startswith("title:"):
            out.append(title_line)
            seen["title"] = True
        elif line.startswith("summary:"):
            out.append(summary_line)
            seen["summary"] = True
        elif line.startswith("keywords:"):
            out.append(keywords_line)
            seen["keywords"] = True
        elif line.startswith("questions:"):
            out.append(questions_line)
            seen["questions"] = True
        else:
            out.append(line)

    if not all(seen.values()) and out and out[-1] == "---":
        insert_at = len(out) - 1
        extras: list[str] = []
        if not seen["title"]:
            extras.append(title_line)
        if not seen["summary"]:
            extras.append(summary_line)
        if not seen["keywords"]:
            extras.append(keywords_line)
        if not seen["questions"]:
            extras.append(questions_line)
        out[insert_at:insert_at] = extras

    return "\n".join(out)


def inject_doc_enrichment(
    fm: str,
    *,
    summary: str,
    keywords: list[str],
) -> str:
    """T32 新增：把 doc 级 2 字段注入 raw md frontmatter。

    与 ``inject_enrichment`` 区别（设计决策见 md/research/2026-05-10-t32-upload-path-execution-plan.md D5）：
    - 只写 ``summary`` + ``keywords``，不动 ``title``（``frontmatter_node`` H1 提取已稳定）
    - 不写 ``questions``（``DocEnrichment`` 没这字段）

    fm 必须包含首尾 ``---``。已存在的字段被替换、不存在的追加到末尾 ``---`` 前。
    """
    if not fm:
        return fm
    summary_line = f"summary: {json.dumps(summary, ensure_ascii=False)}"
    keywords_line = f"keywords: {json.dumps(keywords, ensure_ascii=False)}"

    seen = {"summary": False, "keywords": False}
    out: list[str] = []
    for line in fm.split("\n"):
        if line.startswith("summary:"):
            out.append(summary_line)
            seen["summary"] = True
        elif line.startswith("keywords:"):
            out.append(keywords_line)
            seen["keywords"] = True
        else:
            out.append(line)

    if not all(seen.values()) and out and out[-1] == "---":
        insert_at = len(out) - 1
        extras: list[str] = []
        if not seen["summary"]:
            extras.append(summary_line)
        if not seen["keywords"]:
            extras.append(keywords_line)
        out[insert_at:insert_at] = extras

    return "\n".join(out)


def inject_enrich_error(fm: str, err: str) -> str:
    """把 enrich 失败信息写入 frontmatter ``enrich_error:`` 字段。

    - 字段已存在 → 替换
    - 字段不存在 → 在末尾 ``---`` 之前追加
    - err 字符串自动截断 200 字符 + 转 JSON 字符串（避免 YAML 解析问题）

    用途（CLAUDE.md 规则 29 错误信息端到端透传）：单个 chunk LLM 富化失败时，
    持久化失败原因到 chunk frontmatter，便于事后排查 / 重试 / 用户审计。
    """
    if not fm:
        return fm
    err_text = (err or "").strip()[:200]
    err_line = f"enrich_error: {json.dumps(err_text, ensure_ascii=False)}"

    seen = False
    out: list[str] = []
    for line in fm.split("\n"):
        if line.startswith("enrich_error:"):
            out.append(err_line)
            seen = True
        else:
            out.append(line)

    if not seen and out and out[-1] == "---":
        out.insert(len(out) - 1, err_line)
    return "\n".join(out)


def reassemble(fm: str, body: str) -> str:
    """把（修改过的）frontmatter 与正文重新拼成完整文本。"""
    if not fm:
        return body
    if not fm.endswith("---"):
        fm = fm + "\n---"
    return f"{fm}\n{body}"
