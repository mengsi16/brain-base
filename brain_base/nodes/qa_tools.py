# -*- coding: utf-8 -*-
"""T46/T47: 工具注册表 + 工具规范。

统一意图识别 Agent-Loop（intent_planner → intent_executor → intent_observer）
通过 TOOL_REGISTRY 按名称 dispatch 到具体工具函数。（T46 迭代多跳
拓扑 hop_planner → tool_selector → tool_executor 已于 T47 拔除。）

设计权衡见契约文档 §7：
- web_search / fetch_url 为 async（内部走 playwright fetch）
- raw_text / local_search 为 sync（intent_executor 用 asyncio.to_thread 包装）
- arxiv_pdf 待 T48 实现后注册

工具函数统一返回 ToolResult dict（evidence + markdown + source_url + title），
供 intent_executor / intent_observer 内 LLM 提取 IntentObservation 使用。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ToolSpec 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """单个工具的注册规范。

    Attributes:
        name: 工具名，与 TOOL_REGISTRY key 一致。
        description: 自然语言描述，注入 intent_planner prompt 供 LLM 选择。
        requires: 该工具依赖的基础设施标记（与 infra_status key 对齐）。
        gpu: 是否需要 GPU（影响并发约束判断，仅元信息，不参与 executor 调度决策）。
        parallel_ok: 是否允许并行调用。**T48.1 起被 intent_executor 读取**——
                     ``parallel_ok=False`` 的 actions 走 serial 队列 for-loop 串行，
                     其余进 parallel 队列 ``asyncio.gather`` 并发，避免单跳 LLM
                     吐出多个 GPU 工具同时跑触发 OOM。
        is_async: fn 是否为 async 函数。True → intent_executor 直接 await；
                  False → intent_executor 用 asyncio.to_thread 包装。
        fn: 工具函数。签名见各函数 docstring。

    设计规范（T48.1 拍板）：
        ``gpu=True`` **强烈建议**配 ``parallel_ok=False``——GPU 是稀缺资源
        （单 4060Ti 16GB 同一时刻只能跑一个 MinerU，14GB 阈值见
        ``CLAUDE.md`` 规则 6）。如工具自身支持多并发（多 stream / 多 GPU
        批处理），可声明 ``gpu=True + parallel_ok=True``，作者自行确保不 OOM。
        ``gpu`` 字段仅作元信息（infra 检查 / 文档），不参与 executor 调度决策；
        executor 唯一读取的是 ``parallel_ok``。
    """
    name: str
    description: str
    requires: list[str] = field(default_factory=list)
    gpu: bool = False
    parallel_ok: bool = True
    is_async: bool = True
    fn: Callable[..., Any] | None = None


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


async def execute_web_search(
    tool_args: dict[str, Any],
    llm: Any,
    cfg: Any,
) -> dict[str, Any]:
    """Google + Bing 搜索 → 取 top URL → fetch + evaluate → 返回最佳证据。

    简化版 SERP 流水（不经过 search_strategy 节点，见契约 §10.1）：
    1. 用 tool_args["query"] 单 query 搜索
    2. 取 top N URL（N = min(3, serp_results)）
    3. 对每个 URL 调 _fetch_and_evaluate
    4. 返回 score 最高且 whether_in=True 的候选

    tool_args:
        query: str — 搜索关键词
    """
    from brain_base.nodes.qa_get_info import _fetch_and_evaluate
    from brain_base.tools.web_fetcher import search_bing, search_google

    query = tool_args.get("query", "")
    if not query:
        return {"error": "empty query", "markdown": "", "source_url": "", "title": ""}

    # 并行搜 Google + Bing 各 1 页
    google_task = search_google(query, num_results=5, page=1)
    bing_task = search_bing(query, num_results=5, page=1)
    results = await asyncio.gather(google_task, bing_task, return_exceptions=True)

    # 合并去重
    seen: set[str] = set()
    urls: list[dict[str, str]] = []
    for res in results:
        if isinstance(res, Exception) or not isinstance(res, list):
            continue
        for item in res:
            u = item.get("url", "")
            if u and u not in seen:
                seen.add(u)
                urls.append(item)

    if not urls:
        return {"error": "no serp results", "markdown": "", "source_url": "", "title": ""}

    # 取 top 3 尝试 fetch + evaluate
    best: dict[str, Any] | None = None
    for item in urls[:3]:
        try:
            candidate = await _fetch_and_evaluate(
                item.get("url", ""), query, llm, cfg,
                title_hint=item.get("title", ""),
                snippet=item.get("snippet", ""),
            )
            if candidate is None:
                continue
            if not candidate.get("whether_in", False):
                continue
            if best is None or candidate.get("score", 0) > best.get("score", 0):
                best = candidate
        except Exception as exc:
            logger.warning(
                "web_search fetch fail: url=%s err=%s",
                item.get("url", ""), str(exc)[:200],
            )

    if best:
        return {
            "markdown": best.get("markdown", ""),
            "source_url": best.get("url", ""),
            "title": best.get("title", ""),
            "summary": best.get("summary", ""),
        }
    return {"error": "all fetches failed or irrelevant", "markdown": "", "source_url": "", "title": ""}


async def execute_fetch_url(
    tool_args: dict[str, Any],
    llm: Any,
    cfg: Any,
) -> dict[str, Any]:
    """抓取指定 URL → HTML → Markdown → LLM 评估 → 返回证据。

    与 fetch_user_urls 共享 _fetch_and_evaluate helper。

    tool_args:
        url: str — 目标 URL
        question: str — 用户原始问题（用于 LLM 评估上下文）
    """
    from brain_base.nodes.qa_get_info import _fetch_and_evaluate

    url = tool_args.get("url", "")
    question = tool_args.get("question", "")
    if not url:
        return {"error": "empty url", "markdown": "", "source_url": "", "title": ""}

    candidate = await _fetch_and_evaluate(url, question, llm, cfg)
    if candidate is None:
        return {"markdown": "", "source_url": url, "title": "", "note": "hash_hit_skip"}
    return {
        "markdown": candidate.get("markdown", ""),
        "source_url": candidate.get("url", ""),
        "title": candidate.get("title", ""),
        "summary": candidate.get("summary", ""),
    }


async def execute_raw_text(
    tool_args: dict[str, Any],
    llm: Any,
    cfg: Any,
) -> dict[str, Any]:
    """GitHub / GitLab / arXiv abs / RFC 直取纯文本。

    内部走 ``raw_text_extractor.try_raw_text_async(url)``，按 URL host 自动路由。
    T48.2 D3 修订：原 sync 版 + ``asyncio.to_thread`` 包装会让 ``fetch_page_sync``
    每次起新 loop 触发 chromium 重启；改 async 直调后与主图共享同一 loop 单例
    稳定复用。

    tool_args:
        url: str — 目标 URL
    """
    from brain_base.tools.raw_text_extractor import try_raw_text_async

    url = tool_args.get("url", "")
    if not url:
        return {"error": "empty url", "markdown": "", "source_url": "", "title": ""}

    result = await try_raw_text_async(url)
    if result is None:
        return {"error": "unsupported url or fetch failed", "markdown": "", "source_url": url, "title": ""}
    return {
        "markdown": result.get("markdown", ""),
        "source_url": result.get("source_url", url),
        "title": result.get("title", ""),
    }


async def execute_arxiv_pdf(
    tool_args: dict[str, Any],
    llm: Any,
    cfg: Any,
) -> dict[str, Any]:
    """T48.3：arXiv PDF 全文工具——下载 PDF + MinerU 解析 + 落盘 raw md。

    适用场景：LLM 在 evidence_pool 看到 ``arxiv.org/abs/...`` URL 且需要论文
    完整内容（公式 / 方法论 / 实验细节）；只需摘要时用 raw_text。

    流程（同步串行，单调用 ~10-15 min/篇 首调，命中 sha256 ~5s）：

    1. URL 规范化（abs / pdf 含/不含 v 后缀 → ``arxiv.org/pdf/{id}.pdf``）
    2. ``fetch_binary`` 拉 PDF bytes（T48.2 交付，async）
    3. 写 tempfile.pdf，算 SHA-256
    4. ``_lookup_by_frontmatter_sha256`` 查 raw 目录是否已有同 PDF
       - hit → 读 existing raw md → 截前 3000 字 → 返 ToolResult（score=60）
       - miss → 继续
    5. ``convert_one(input_path=temp_pdf, ...)`` MinerU 解析
       - GPU 5-10 min/篇（T48.1 已串行化，不会并发 OOM）
    6. 读 markdown 全文 → 截前 3000 字（observer 评分 + UI 预览）
    7. 提取 title（首 H1 或 doc_id）
    8. 返 ToolResult，含 ``markdown / source_url / title / summary / score / raw_path / sha256_hash / doc_id``

    失败 fallback：抛错 → ``ToolResult.error`` 含失败原因 + score=0；
    上层 observer 看到 error 不会写 evidence_pool，planner 下跳可选 raw_text 拿摘要兜底。

    tool_args:
        url: str — arxiv URL（abs 或 pdf 形式均可）
    """
    import hashlib
    import shutil
    import tempfile
    from pathlib import Path

    from brain_base.tools.raw_text_extractor import normalize_arxiv_pdf_url
    from brain_base.tools.web_fetcher import fetch_binary

    url = (tool_args.get("url") or "").strip()
    if not url:
        return {
            "error": "empty url",
            "markdown": "", "source_url": "", "title": "",
            "score": 0,
        }

    # Step 1: URL 规范化
    pdf_url = normalize_arxiv_pdf_url(url)
    if pdf_url is None:
        return {
            "error": f"not an arxiv URL: {url[:200]}",
            "markdown": "", "source_url": url, "title": "",
            "score": 0,
        }

    # Step 2: fetch_binary 拉 PDF
    try:
        pdf_bytes = await fetch_binary(pdf_url, timeout=120.0)
    except Exception as exc:
        logger.warning(
            "execute_arxiv_pdf: fetch_binary failed | url=%s err=%s: %s",
            pdf_url, type(exc).__name__, str(exc)[:200],
        )
        return {
            "error": f"pdf download failed: {type(exc).__name__}: {str(exc)[:200]}",
            "markdown": "", "source_url": pdf_url, "title": "",
            "score": 0,
        }

    if not pdf_bytes:
        return {
            "error": "empty pdf bytes",
            "markdown": "", "source_url": pdf_url, "title": "",
            "score": 0,
        }

    # Step 3: 写 tempfile + 算 SHA-256
    binary_sha256 = hashlib.sha256(pdf_bytes).hexdigest()

    # Step 4: SHA-256 dedup 查询
    try:
        from brain_base.nodes.ingest_file import _lookup_by_frontmatter_sha256
        existing = await asyncio.to_thread(_lookup_by_frontmatter_sha256, binary_sha256)
    except Exception as exc:
        logger.warning(
            "execute_arxiv_pdf: dedup lookup failed (continuing without dedup) | err=%s: %s",
            type(exc).__name__, str(exc)[:200],
        )
        existing = None

    if existing is not None:
        # Hit：读 existing raw md
        existing_raw_path = Path(existing["raw_path"])
        try:
            full_md = await asyncio.to_thread(
                existing_raw_path.read_text, encoding="utf-8"
            )
        except Exception as exc:
            logger.warning(
                "execute_arxiv_pdf: failed to read existing raw md | path=%s err=%s",
                existing_raw_path, str(exc)[:200],
            )
            full_md = ""

        # 剥 frontmatter 拿 body（首 H1 → title）
        body = full_md
        if full_md.startswith("---"):
            parts = full_md.split("---", 2)
            if len(parts) >= 3:
                body = parts[2].lstrip()
        title = _extract_first_h1(body) or existing["doc_id"]
        md_preview = body[:3000]

        logger.info(
            "execute_arxiv_pdf dedup hit | sha256=%s existing_doc_id=%s url=%s",
            binary_sha256[:12], existing["doc_id"], pdf_url,
        )
        return {
            "markdown": md_preview,
            "source_url": pdf_url,
            "title": title,
            "summary": md_preview[:400],
            "score": 60,  # dedup 命中：未做新分析，分数稍低
            "raw_path": str(existing_raw_path),
            "sha256_hash": binary_sha256,
            "doc_id": existing["doc_id"],
        }

    # Step 5: miss → MinerU 转 markdown
    # 创建 tempfile 写 PDF binary，调 convert_one 转 raw markdown
    import importlib
    _doc_converter = importlib.import_module("bin.doc-converter")
    convert_one = _doc_converter.convert_one

    raw_dir = Path("data/docs/raw")
    uploads_dir = Path("data/docs/uploads")

    # arxiv_id 从 pdf_url 末段提取（已经过 normalize 是 .pdf 结尾）
    arxiv_id = pdf_url.rsplit("/", 1)[-1].removesuffix(".pdf")
    safe_id = arxiv_id.replace(".", "_").replace("/", "_")

    with tempfile.NamedTemporaryFile(
        suffix=f"_{safe_id}.pdf", delete=False,
    ) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)

    try:
        try:
            convert_result = await asyncio.to_thread(
                convert_one,
                input_path=tmp_path,
                output_dir=raw_dir,
                uploads_dir=uploads_dir,
            )
        except Exception as exc:
            logger.warning(
                "execute_arxiv_pdf: mineru convert failed | url=%s err=%s: %s",
                pdf_url, type(exc).__name__, str(exc)[:200],
            )
            return {
                "error": (
                    f"mineru convert failed: {type(exc).__name__}: {str(exc)[:200]}"
                ),
                "markdown": "", "source_url": pdf_url, "title": "",
                "score": 0,
            }

        doc_id = convert_result.get("doc_id", "")
        raw_path = Path(convert_result.get("raw_path", ""))
        if not doc_id or not raw_path.is_file():
            return {
                "error": f"mineru convert returned invalid result: {convert_result}",
                "markdown": "", "source_url": pdf_url, "title": "",
                "score": 0,
            }

        # Step 6: 读 markdown 全文 + 写入 sha256 frontmatter（让后续查重命中）
        try:
            full_md = await asyncio.to_thread(raw_path.read_text, encoding="utf-8")
        except Exception as exc:
            logger.warning(
                "execute_arxiv_pdf: read raw md failed | path=%s err=%s",
                raw_path, str(exc)[:200],
            )
            full_md = ""

        # convert_one 不写 frontmatter（只写 body），arxiv_pdf 自己拼一份带 sha256 的
        # frontmatter 让 _lookup_by_frontmatter_sha256 后续命中。
        body = full_md
        title = _extract_first_h1(body) or doc_id
        md_preview = body[:3000]

        from datetime import date
        fetched_at_date = date.today().isoformat()
        fm = _build_arxiv_frontmatter(
            doc_id=doc_id,
            title=title,
            url=pdf_url,
            arxiv_id=arxiv_id,
            fetched_at_date=fetched_at_date,
            content_sha256=binary_sha256,
        )
        new_raw_text = fm + "\n\n" + body.lstrip()

        try:
            await asyncio.to_thread(
                raw_path.write_text, new_raw_text, encoding="utf-8"
            )
        except Exception as exc:
            logger.warning(
                "execute_arxiv_pdf: rewrite frontmatter failed | path=%s err=%s",
                raw_path, str(exc)[:200],
            )
            # 不抛——body 已落盘，frontmatter 缺失只影响后续 dedup 不影响本次返回

        logger.info(
            "execute_arxiv_pdf converted | doc_id=%s sha256=%s raw_len=%d",
            doc_id, binary_sha256[:12], len(body),
        )

        return {
            "markdown": md_preview,
            "source_url": pdf_url,
            "title": title,
            "summary": md_preview[:400],
            "score": 70,  # 成功转换 + 持久化
            "raw_path": str(raw_path),
            "sha256_hash": binary_sha256,
            "doc_id": doc_id,
        }
    finally:
        # 清理 tempfile（convert_one 内部已 archive 到 uploads_dir/{doc_id}/）
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


async def execute_github_raw(
    tool_args: dict[str, Any],
    llm: Any,
    cfg: Any,
) -> dict[str, Any]:
    """T48.4：GitHub raw 工具——直取 GitHub 仓库 / 文件原文。

    适用场景：LLM 在 evidence_pool 看到 ``github.com/X/Y[/blob|/tree|/raw/branch/path]``
    URL 且需要源码 / README / 文档原文（绕过 GitHub HTML 渲染的导航 / 侧栏 / 评论
    等噪音）；issue / PR / wiki / search / gist 请用 fetch_url 渲染动态页面。

    内部走 ``raw_text_extractor.try_github_raw(url)`` async 直 await ``fetch_page``
    （T48.2 D3 修复后同 loop 复用 chromium，~1s 拉 README）。

    支持：
    - 仓库根 ``github.com/X/Y`` → 自动探测 ``main → master`` × ``README{,_zh,_en}.md``
    - blob 文件页 ``github.com/X/Y/blob/{branch}/{path}`` → 转 raw URL
    - raw 文件页 ``github.com/X/Y/raw/{branch}/{path}`` → 直接拉

    不支持（返 error 让 planner 改调 fetch_url）：
    - tree 目录页 → raw.githubusercontent.com 对目录返 404
    - issues / pulls / wiki / search 等动态页面（需 chromium 渲染）
    - gist.github.com（独立 host）

    比 fetch_url 快 5-10×（~1s vs 5-8s），description 让 LLM 优先选。

    tool_args:
        url: str — GitHub URL
    """
    from brain_base.tools.raw_text_extractor import try_github_raw

    url = (tool_args.get("url") or "").strip()
    if not url:
        return {
            "error": "empty url",
            "markdown": "", "source_url": "", "title": "",
        }

    try:
        result = await try_github_raw(url)
    except Exception as exc:
        logger.warning(
            "execute_github_raw failed | url=%s err=%s: %s",
            url, type(exc).__name__, str(exc)[:200],
        )
        return {
            "error": f"github raw fetch failed: {type(exc).__name__}: {str(exc)[:200]}",
            "markdown": "", "source_url": url, "title": "",
        }

    if result is None:
        return {
            "error": (
                "unsupported github url (issue/PR/wiki/gist/tree directory or "
                "non-github host) — please use fetch_url or provide a blob/raw URL"
            ),
            "markdown": "", "source_url": url, "title": "",
        }
    return {
        "markdown": result.get("markdown", ""),
        "source_url": result.get("source_url", url),
        "title": result.get("title", ""),
    }


def _extract_first_h1(text: str) -> str:
    """从 markdown 提取第一个 H1 作为 title。"""
    for line in text.split("\n", 50):
        line = line.strip()
        if line.startswith("# ") and not line.startswith("##"):
            return line[2:].strip()[:200]
    return ""


def _build_arxiv_frontmatter(
    doc_id: str,
    title: str,
    url: str,
    arxiv_id: str,
    fetched_at_date: str,
    content_sha256: str,
) -> str:
    """T48.3 arxiv_pdf 工具专用 frontmatter（含 content_sha256 让 dedup 后续可命中）。

    格式与 ingest_file.frontmatter_node 兼容（同字段名 + 同值约定）。
    """
    safe_title = title.replace('"', "'")[:200]
    return (
        "---\n"
        f"doc_id: {doc_id}\n"
        f'title: "{safe_title}"\n'
        "source: arxiv.org\n"
        "source_type: official-doc\n"
        f"url: {url}\n"
        f"arxiv_id: {arxiv_id}\n"
        f"fetched_at: {fetched_at_date}\n"
        f"content_sha256: {content_sha256}\n"
        "keywords: []\n"
        "---"
    )


def execute_local_search(
    tool_args: dict[str, Any],
    llm: Any,
    cfg: Any,
) -> dict[str, Any]:
    """Milvus 本地知识库混合检索。

    sync 函数——intent_executor 用 asyncio.to_thread 包装。

    tool_args:
        query: str — 检索关键词
        top_k: int — 返回条数（默认 8）
    """
    from brain_base.tools.milvus_client import multi_query_search

    query = tool_args.get("query", "")
    top_k = int(tool_args.get("top_k", 8))
    if not query:
        return {"error": "empty query", "markdown": "", "source_url": "", "title": ""}

    result = multi_query_search(
        queries=[query],
        top_k_per_query=max(top_k, 12),
        final_k=top_k,
    )
    candidates = result.get("candidates", []) or []
    if not candidates:
        return {"markdown": "", "source_url": "local_milvus", "title": ""}

    # 拼接 top candidates 的 text 作为 markdown evidence
    parts: list[str] = []
    for c in candidates[:top_k]:
        text = c.get("text", "") or c.get("content", "")
        doc_id = c.get("doc_id", "")
        if text:
            parts.append(f"[{doc_id}] {text}")

    return {
        "markdown": "\n\n---\n\n".join(parts),
        "source_url": "local_milvus",
        "title": f"Milvus search: {query[:50]}",
    }


# ---------------------------------------------------------------------------
# TOOL_REGISTRY
# ---------------------------------------------------------------------------


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "web_search": ToolSpec(
        name="web_search",
        description="Google + Bing 搜索，适用于需要最新网络信息的场景",
        requires=["playwright"],
        gpu=False,
        parallel_ok=True,
        is_async=True,
        fn=execute_web_search,
    ),
    "fetch_url": ToolSpec(
        name="fetch_url",
        description="抓取指定 URL 内容（HTML → Markdown → LLM 评估）",
        requires=["playwright"],
        gpu=False,
        parallel_ok=True,
        is_async=True,
        fn=execute_fetch_url,
    ),
    "raw_text": ToolSpec(
        name="raw_text",
        description=(
            "GitLab raw / arXiv 摘要页 / RFC 纯文本直取（按 URL host 自动路由）。"
            "GitHub URL 请优先用 github_raw（更明确 + 同等性能）。"
        ),
        requires=["playwright"],
        gpu=False,
        parallel_ok=True,
        is_async=True,  # T48.2 D3：async 直调避免 fetch_page_sync 重启 chromium
        fn=execute_raw_text,
    ),
    "local_search": ToolSpec(
        name="local_search",
        description="Milvus 本地知识库混合检索，适用于已入库文档的精确查找",
        requires=["milvus"],
        gpu=False,
        parallel_ok=True,
        is_async=False,
        fn=execute_local_search,
    ),
    "arxiv_pdf": ToolSpec(
        name="arxiv_pdf",
        description=(
            "下载 arXiv 论文 PDF 并用 MinerU 解析为 Markdown，"
            "适用于需要论文完整内容（公式 / 方法论 / 实验细节）的场景。"
            "仅需摘要或快速判定相关性时请用 raw_text。"
            "单跳建议 ≤2 个 arxiv_pdf——会串行排队，"
            "每篇 GPU MinerU 5-10 分钟 + 持久化 ~5 分钟，"
            "总耗时 10-15 分钟/篇（命中 SHA-256 dedup ~5s）。"
        ),
        requires=["mineru", "playwright"],
        gpu=True,
        parallel_ok=False,  # MinerU 14GB VRAM 硬约束（CLAUDE.md 规则 6 + T48.1 串行化）
        is_async=True,
        fn=execute_arxiv_pdf,
    ),
    "github_raw": ToolSpec(
        name="github_raw",
        description=(
            "获取 GitHub 仓库文件的 raw 纯文本（绕过 GitHub HTML 页面的导航 / 侧栏 / 评论噪音）。"
            "支持：仓库根 URL（自动探测 README）、blob 文件页、raw 文件页。"
            "不支持：issue / PR / wiki / search / gist（请用 fetch_url）、"
            "tree 目录页（raw 形不存在，请提供具体 blob URL）。"
            "对代码文件 / README / 文档的精确提取比 fetch_url 快 5-10×（~1s vs 5-8s）。"
        ),
        requires=["playwright"],
        gpu=False,
        parallel_ok=True,
        is_async=True,  # T48.2 D5 验证 async 路径同 loop 复用 chromium 不重启
        fn=execute_github_raw,
    ),
}
