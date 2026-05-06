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
    summary: str,
    keywords: list[str],
    questions: list[str],
) -> str:
    """把 enrichment 三字段注入 frontmatter，已存在替换、不存在追加。

    fm 必须包含首尾 `---`（即 split_frontmatter 的第一个返回值）。
    """
    if not fm:
        return fm
    summary_line = f"summary: {json.dumps(summary, ensure_ascii=False)}"
    keywords_line = f"keywords: {json.dumps(keywords, ensure_ascii=False)}"
    questions_line = f"questions: {json.dumps(questions, ensure_ascii=False)}"

    seen = {"summary": False, "keywords": False, "questions": False}
    out: list[str] = []
    for line in fm.split("\n"):
        if line.startswith("summary:"):
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
        if not seen["summary"]:
            extras.append(summary_line)
        if not seen["keywords"]:
            extras.append(keywords_line)
        if not seen["questions"]:
            extras.append(questions_line)
        out[insert_at:insert_at] = extras

    return "\n".join(out)


def reassemble(fm: str, body: str) -> str:
    """把（修改过的）frontmatter 与正文重新拼成完整文本。"""
    if not fm:
        return body
    if not fm.endswith("---"):
        fm = fm + "\n---"
    return f"{fm}\n{body}"
