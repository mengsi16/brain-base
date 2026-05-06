"""
IngestUrl 图节点函数。

流程：fetch → clean → completeness_check → frontmatter → persist
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from brain_base.agents.schemas import CompletenessJudgment
from brain_base.agents.utils.structured import invoke_structured
from brain_base.prompts.ingest_url_prompts import COMPLETENESS_CHECK_SYSTEM_PROMPT
from brain_base.tools.web_fetcher import fetch_page


_SLUG_CHARS = re.compile(r"[^a-zA-Z0-9]+")


def _slugify(text: str, max_len: int = 60) -> str:
    """ASCII slug：非字母数字一律替换为 -，连续 - 折叠，截断到 max_len。"""
    s = _SLUG_CHARS.sub("-", text or "").strip("-").lower()
    return s[:max_len] or "untitled"


def _doc_id_from_url(url: str, title_hint: str = "") -> str:
    """从 URL 派生 doc_id：host_slug + path_slug。

    例：https://docs.litellm.ai/docs/  → docs-litellm-ai_docs
        https://github.com/BerriAI/litellm → github-com_berriai-litellm
    title_hint 不作为 doc_id 主体（标题可能很长且含特殊字符），仅在
    URL 解析失败时退化使用。
    """
    try:
        parsed = urlparse(url)
        host = _slugify(parsed.netloc, max_len=40)
        path = _slugify(parsed.path, max_len=60)
        if path:
            return f"{host}_{path}"
        return host
    except Exception:
        return _slugify(title_hint or url, max_len=80)


def fetch_node(state: dict[str, Any]) -> dict[str, Any]:
    """抓取页面到 state：raw_html（原始 HTML 给 MinerU 清洗）+ raw_content（plain text fallback）。

    策略：
    1. 调用方已在 state 里预填 raw_content（旧 Agent 调度路径）→ 跳过抓取
    2. 单次 playwright 抓取：page.content() 拿到渲染后完整 HTML（静态/SPA 通吃）
    """
    url = state.get("url", "")
    if not url:
        return {"error": "fetch_node: url 为空", "extraction_status": "missing-input"}

    # 调用方已预填 raw_content，跳过抓取（向后兼容）
    if state.get("raw_content") or state.get("raw_html"):
        return {"extraction_status": "ok"}

    # playwright 单次抓取：起浏览器贵但成功率高，避免静态抓拿到空骨架的伪成功
    result = fetch_page(url)
    html = result.get("html", "")
    text = result.get("text", "")
    title = result.get("title", "")

    if not html.strip() and not text.strip():
        return {
            "error": f"fetch_node 抓取失败: {result.get('error', '正文为空')}",
            "extraction_status": "spa-failed",
            "raw_content": "",
            "raw_html": "",
        }

    # raw_content 仍保留 plain text 作为降级路径
    md_parts: list[str] = []
    if title:
        md_parts.append(f"# {title.strip()}\n")
    md_parts.append(text.strip())
    raw_content = "\n\n".join(md_parts)

    return {
        "raw_html": html,
        "raw_content": raw_content,
        "title_hint": state.get("title_hint") or title,
        "extraction_status": "ok",
    }


def clean_node(state: dict[str, Any]) -> dict[str, Any]:
    """HTML → Markdown 清洗：用 MinerU-HTML 保留结构（标题/代码块/列表/表格）。

    fail-fast：MinerU-HTML 失败直接抛错，不降级到 plain text——降级会让下游
    误以为入库成功但实际质量极差。失败时由 `ingest_candidates_node` 接住记入
    ingest_errors 跳过这个 URL，处理下一个。
    """
    from brain_base.tools.doc_converter_tool import convert_html_to_markdown

    raw_html = state.get("raw_html", "")
    source_type = state.get("source_type", "community")

    if not raw_html.strip():
        return {"error": "clean_node: raw_html 为空", "extraction_status": "spa-failed"}

    cleaned_md = convert_html_to_markdown(raw_html)

    # community 类型最低内容门槛
    if source_type == "community" and len(cleaned_md.strip()) < 200:
        return {"extraction_status": "insufficient-content", "cleaned_md": ""}

    if not cleaned_md.strip():
        return {"extraction_status": "spa-failed", "cleaned_md": ""}

    return {"cleaned_md": cleaned_md, "extraction_status": "ok"}


def frontmatter_node(state: dict[str, Any]) -> dict[str, Any]:
    """组装 official-doc/community frontmatter 并写入 raw 文件"""
    cleaned_md = state.get("cleaned_md", "")
    if not cleaned_md:
        return {"raw_md_path": "", "error": state.get("error", "frontmatter_node: 无清洗结果")}

    url = state.get("url", "")
    source_type = state.get("source_type", "community")
    topic = state.get("topic", "untitled")
    title_hint = state.get("title_hint", "")
    fetched_at = date.today().isoformat()

    # doc_id 必须基于 URL（每个 URL 一个独立文档），不能基于 topic/question——
    # 否则一次问答的多个候选 URL 都会写到同一个文件互相覆盖。
    doc_id = f"{_doc_id_from_url(url, title_hint)}-{fetched_at}"
    raw_path = Path(f"data/docs/raw/{doc_id}.md")

    # 计算 content_sha256
    sha256 = hashlib.sha256(
        cleaned_md.replace("\r\n", "\n").strip("\n").encode("utf-8")
    ).hexdigest()

    # 从正文提取标题
    title = title_hint or doc_id
    for line in cleaned_md.split("\n"):
        if line.startswith("# "):
            title = line[2:].strip()
            break

    # 推导 source 标识
    source = "community-blog"
    if source_type == "official-doc":
        source = "official-doc"

    fm = (
        "---\n"
        f"doc_id: {doc_id}\n"
        f"title: {title}\n"
        f"source_type: {source_type}\n"
        f"source: {source}\n"
        f"url: {url}\n"
        f"fetched_at: {fetched_at}\n"
        f"content_sha256: {sha256}\n"
        "keywords: []\n"
        "---\n\n"
    )

    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(fm + cleaned_md, encoding="utf-8")

    return {"raw_md_path": str(raw_path), "doc_id": doc_id}


def create_completeness_check_node(llm: Any = None) -> Callable:
    """完整性校验节点工厂。

    判断 cleaned_md 是否可入库（ok / spa-failed / insufficient-content /
    over-cleaned），用 `CompletenessJudgment` schema。

    llm=None：用纯字符长度阈值降级（official-doc ≥ 500，community ≥ 200）。
    """

    def completeness_check_node(state: dict[str, Any]) -> dict[str, Any]:
        cleaned_md = state.get("cleaned_md", "")
        source_type = state.get("source_type", "community")
        chars = len(cleaned_md.strip())

        if not cleaned_md:
            return {"completeness_status": "spa-failed", "completeness_chars": 0}

        if llm is None:
            min_chars = 500 if source_type == "official-doc" else 200
            status = "ok" if chars >= min_chars else "insufficient-content"
            return {"completeness_status": status, "completeness_chars": chars}

        try:
            result = invoke_structured(
                llm,
                CompletenessJudgment,
                COMPLETENESS_CHECK_SYSTEM_PROMPT,
                f"source_type: {source_type}\n字符数: {chars}\n\n正文（节选 1500 字）：\n{cleaned_md[:1500]}",
            )
        except Exception:
            min_chars = 500 if source_type == "official-doc" else 200
            status = "ok" if chars >= min_chars else "insufficient-content"
            return {"completeness_status": status, "completeness_chars": chars}

        return {
            "completeness_status": result.status,
            "completeness_chars": result.chars or chars,
            "completeness_reason": result.reason,
        }

    return completeness_check_node


# 向后兼容
completeness_check_node = create_completeness_check_node(llm=None)


def create_persist_node(llm: Any = None):
    """持久化节点工厂：调用 PersistenceGraph 完成分块入库"""
    from brain_base.graphs.persistence_graph import PersistenceGraph

    def persist_node(state: dict[str, Any]) -> dict[str, Any]:
        raw_md_path = state.get("raw_md_path", "")
        doc_id = state.get("doc_id", "")
        if not raw_md_path:
            return {"persistence_result": {}, "error": state.get("error", "")}

        pg = PersistenceGraph(llm=llm)
        try:
            r = pg.run(raw_md_path=raw_md_path, doc_id=doc_id)
            return {"persistence_result": r}
        except Exception as exc:
            return {"persistence_result": {"doc_id": doc_id, "error": str(exc)}}

    return persist_node


# 向后兼容
persist_node = create_persist_node(llm=None)
