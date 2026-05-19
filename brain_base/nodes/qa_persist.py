"""
QA 主图持久化流水节点（T26.1）。

T26.1-b：write_raw_one + barrier_raw + fanout_persist_dispatcher
T26.1-c：enrich_one + barrier_enrich + fanout_enrich_dispatcher + ingest_node

架构（详见 CLAUDE.md `get_info_block` 内部展开 + 契约
`md/research/2026-05-09-t26-1-persist-pipeline-contract.md`）：

    merge_evidence → fanout_persist_dispatcher
        Send × N → write_raw_one → barrier_raw
        或短路 → ingest（T28: legacy_dense_search 被 PIPE2 替代）
    barrier_raw → fanout_enrich_dispatcher
        Send × M → enrich_one → barrier_enrich
        或短路 → ingest
    barrier_enrich → ingest_node → fanout_search_dispatcher → ... → judge → answer

并发：
- write_raw_one：IO 操作不限流（D5）
- enrich_one：LLM Semaphore 限流（D6 决策：独立于 fetch_extract）
- ingest_node：单批 Milvus，fail-fast（CLAUDE.md 规则 25）
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from pathlib import Path as _P
from typing import Any, Callable, TypedDict
from urllib.parse import urlparse

from brain_base.agents.schemas import ChunkEnrichment
from brain_base.agents.utils.agent_utils import generate_doc_id
from brain_base.agents.utils.structured import invoke_structured
from brain_base.config import GetInfoConfig
from brain_base.nodes._frontmatter import (
    inject_enrich_error,
    inject_enrichment,
    reassemble,
    split_frontmatter,
)
from brain_base.prompts.persistence_prompts import (
    ENRICH_SYSTEM_PROMPT,
    ENRICH_USER_PROMPT_TEMPLATE,
)

# bin/chunker + bin/milvus-cli 动态导入（同 nodes/persistence.py 模式）
_BIN_DIR = str(_P(__file__).resolve().parent.parent.parent / "bin")
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

_chunker = importlib.import_module("bin.chunker")
write_chunks = _chunker.write_chunks

_milvus_cli = importlib.import_module("bin.milvus-cli")
milvus_ingest_chunks = _milvus_cli.ingest_chunks

logger = logging.getLogger(__name__)

DEFAULT_RAW_DIR = "data/docs/raw"
DEFAULT_CHUNK_DIR = "data/docs/chunks"

# T26.1-c：复用 nodes/persistence.py 的重试常量（同步语义）
ENRICH_RETRY_COUNT = 1
ENRICH_RETRY_BACKOFF_SEC = 1.0


# ---------------------------------------------------------------------------
# 子状态 TypedDict（write_raw_one 的 Send 输入）
# ---------------------------------------------------------------------------


class PersistState(TypedDict, total=False):
    """write_raw_one 节点的 Send 子状态。"""
    candidate: dict[str, Any]   # 单个 get_info_candidates 元素
    raw_dir: str                # 默认 data/docs/raw
    chunk_dir: str              # 默认 data/docs/chunks


# ---------------------------------------------------------------------------
# URL → doc_id slug 化（契约 §5）
# ---------------------------------------------------------------------------


_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9_-]")
_SLUG_DASH_REPEAT = re.compile(r"-+")


def _url_to_slug(url: str) -> str:
    """URL → doc_id 前缀 slug。

    示例：
        https://demo.ragflow.io/docs/quickstart → demo-ragflow-io_docs-quickstart
        https://github.com/hkuds/rag-anything   → github-com_hkuds_rag-anything
        https://x.io/                            → x-io
        ""                                       → unknown

    规则：
        - host 用 `-` 连接（`.` / `:` → `-`）
        - path 与 host 之间用 `_` 分隔，路径内 `/` → `_`，`.` → `-`
        - 全部小写
        - 非 [a-z0-9_-] 字符替换为 `-`
        - 多个 `-` 合并为一个，首尾 `-_` 去掉
        - 限长 100 字符（防超长 path）
    """
    if not url:
        return "unknown"
    p = urlparse(url)
    host = (p.netloc or "").lower().replace(".", "-").replace(":", "-")
    path = (p.path or "").strip("/").replace("/", "_").replace(".", "-")
    raw = f"{host}_{path}" if path else host
    if not raw:
        return "unknown"
    cleaned = _SLUG_INVALID_CHARS.sub("-", raw.lower())
    cleaned = _SLUG_DASH_REPEAT.sub("-", cleaned).strip("-_")
    return cleaned[:100] if cleaned else "unknown"


# ---------------------------------------------------------------------------
# raw frontmatter 拼装（手动，str 字段 json.dumps 安全转义）
# ---------------------------------------------------------------------------


def _build_raw_frontmatter(
    *,
    doc_id: str,
    title: str,
    source_type: str,
    source: str,
    url: str,
    fetched_at_date: str,
    content_sha256: str,
    keywords: list[str],
) -> str:
    """组装 raw markdown frontmatter（与 backup §2.2 模板对齐）。

    title 用 json.dumps 包裹，避免值里含 `:` / `\\n` 破坏 YAML 解析；
    keywords JSON inline 数组（与 _frontmatter.inject_enrichment 风格一致）。
    """
    return "\n".join(
        [
            "---",
            f"doc_id: {doc_id}",
            f"title: {json.dumps(title, ensure_ascii=False)}",
            f"source_type: {source_type}",
            f"source: {source}",
            f"url: {url}",
            f"fetched_at: {fetched_at_date}",
            f"content_sha256: {content_sha256}",
            f"keywords: {json.dumps(keywords, ensure_ascii=False)}",
            "---",
        ]
    )


def _resolve_fetched_at_date(fetched_at_iso: str) -> str:
    """ISO timestamp `2026-05-09T10:30:45+00:00` → `2026-05-09`；空值 fallback now()。"""
    if fetched_at_iso:
        return fetched_at_iso.split("T", 1)[0]
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Node 1: write_raw_one (async, fan-out 单元)
# ---------------------------------------------------------------------------


async def write_raw_one(sub_state: PersistState) -> dict[str, Any]:
    """单 candidate 落盘：写 raw .md → 调 chunker.write_chunks 切分。

    入：``candidate`` dict（含 url / title / markdown / content_sha256 / type /
       keywords / fetched_at），来自 merge_evidence.get_info_candidates（T47.4 后
       由统一意图识别 Agent-Loop 的 evidence_pool 转换而来；T46 三路汇聚已删）。
    出：``{"persist_results": [{...}]}``，单元素列表，让 reducer add 累加。

    **T48.3 fast-path**：当 ``candidate.raw_path`` 非空且文件存在（如 arxiv_pdf 工具
    已经把 PDF 转 markdown 并落盘到 ``data/docs/raw/{doc_id}.md``），跳过 frontmatter
    拼装 + 写 raw md 步骤，直接调 chunker。doc_id 从 raw_path stem 提取。这样避免
    重复落盘 + frontmatter 不一致（fast-path 工具的 frontmatter 由其自己负责拼装）。

    失败隔离：任一步骤抛错 → success=False，不阻断其他 Send。
    """
    candidate = sub_state.get("candidate") or {}
    raw_dir = sub_state.get("raw_dir") or DEFAULT_RAW_DIR
    chunk_dir = sub_state.get("chunk_dir") or DEFAULT_CHUNK_DIR

    url = (candidate.get("url") or "").strip()
    title = candidate.get("title") or ""
    markdown = candidate.get("markdown") or ""
    content_sha256 = candidate.get("content_sha256") or ""
    source_type = candidate.get("type") or "community"
    keywords = list(candidate.get("keywords") or [])
    fetched_at_iso = candidate.get("fetched_at") or ""
    fast_raw_path = (candidate.get("raw_path") or "").strip()  # T48.3

    try:
        if not url:
            raise ValueError("empty url")

        # T48.3 fast-path：fast_raw_path 已存在 → 跳 fetch+write 直接 chunker
        if fast_raw_path and Path(fast_raw_path).is_file():
            raw_path = Path(fast_raw_path)
            doc_id = raw_path.stem  # arxiv-2501.12345v2-20260519 等
            logger.info(
                "write_raw_one fast-path | doc_id=%s raw_path=%s url=%s",
                doc_id, raw_path, url,
            )
        else:
            # 标准路径：拼 frontmatter + 写 raw md
            if not markdown.strip():
                raise ValueError(f"empty markdown for url={url}")

            # Step 1: doc_id（URL slug + 日期 + url hash 前 8 位）
            prefix = _url_to_slug(url)
            doc_id = generate_doc_id(prefix, url=url)
            host = (urlparse(url).netloc or "unknown").lower()

            # Step 2: fetched_at ISO timestamp → date 部分（YYYY-MM-DD）
            fetched_at_date = _resolve_fetched_at_date(fetched_at_iso)

            # Step 3: 组 frontmatter + 写 raw markdown
            fm = _build_raw_frontmatter(
                doc_id=doc_id,
                title=title,
                source_type=source_type,
                source=host,
                url=url,
                fetched_at_date=fetched_at_date,
                content_sha256=content_sha256,
                keywords=keywords,
            )
            raw_text = fm + "\n\n" + markdown
            raw_path = Path(raw_dir) / f"{doc_id}.md"

            def _write_raw_file() -> None:
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_text(raw_text, encoding="utf-8")

            await asyncio.to_thread(_write_raw_file)

        # Step 4: 调 chunker.write_chunks（同步函数，扔线程池）
        written = await asyncio.to_thread(write_chunks, raw_path, Path(chunk_dir))
        chunk_files = [str(p) for p in written]
        if not chunk_files:
            raise RuntimeError(f"chunker 未生成 chunks: {doc_id}")

        return {
            "persist_results": [
                {
                    "doc_id": doc_id,
                    "raw_path": str(raw_path),
                    "chunk_files": chunk_files,
                    "url": url,
                    "success": True,
                }
            ]
        }
    except Exception as e:
        logger.warning(
            "write_raw_one failed url=%s err=%s", url, str(e)[:200]
        )
        return {
            "persist_results": [
                {
                    "doc_id": "",
                    "raw_path": "",
                    "chunk_files": [],
                    "url": url,
                    "success": False,
                    "error": str(e)[:200],
                }
            ]
        }


# ---------------------------------------------------------------------------
# Node 2: barrier_raw (sync, fan-in 聚合)
# ---------------------------------------------------------------------------


def barrier_raw_node(state: dict[str, Any]) -> dict[str, Any]:
    """fan-in：聚合 ``persist_results`` → flatten ``chunk_files`` + 错误归集。

    入：``persist_results: list[dict]``（reducer add 累加）
    出：
        - ``chunk_files``：所有 success doc 的 chunk 文件路径 flatten 列表
        - ``persist_errors``：累加上游 errors + 当前阶段 write_raw 失败聚合
    """
    results = list(state.get("persist_results", []) or [])
    chunk_files: list[str] = []
    errors: list[str] = list(state.get("persist_errors", []) or [])

    for r in results:
        if r.get("success", False):
            chunk_files.extend(r.get("chunk_files", []) or [])
        else:
            errors.append(
                f"write_raw {r.get('url', '?')}: {r.get('error', '')}"
            )

    return {
        "chunk_files": chunk_files,
        "persist_errors": errors,
    }


# ---------------------------------------------------------------------------
# Conditional Edge: fanout_persist_dispatcher
# ---------------------------------------------------------------------------


def fanout_persist_dispatcher(state: dict[str, Any]) -> Any:
    """1 重 gate：``get_info_candidates`` 空 → 短路 ingest（T28：PIPE2 之后不再走 legacy_dense_search）。

    merge_evidence 已过滤无效 evidence（T47.4 后由统一意图识别 Agent-Loop 输出），
    candidates 都已是有效候选；无需检查 ``persist_attempted``（主图无回路）。
    """
    from langgraph.types import Send  # 局部 import 避免顶层强依赖

    candidates = list(state.get("get_info_candidates", []) or [])
    if not candidates:
        return "ingest"

    return [
        Send(
            "write_raw_one",
            {
                "candidate": c,
                "raw_dir": DEFAULT_RAW_DIR,
                "chunk_dir": DEFAULT_CHUNK_DIR,
            },
        )
        for c in candidates
    ]


# ===========================================================================
# T26.1-c: enrich_one + barrier_enrich + fanout_enrich_dispatcher + ingest_node
# ===========================================================================


# ---------------------------------------------------------------------------
# 模块级 Semaphore（enrich_one 限流，独立于 fetch_extract 的 _sem）
# ---------------------------------------------------------------------------


_enrich_sem: asyncio.Semaphore | None = None
_enrich_sem_concurrency: int = 0
_enrich_sem_loop_id: int | None = None


def _get_enrich_semaphore(concurrency: int) -> asyncio.Semaphore:
    """惰性创建 enrich Semaphore；concurrency 改变或 loop 切换时重建。

    与 fetch_extract 的 _sem 完全独立（D6 决策），避免两阶段串行执行时
    fetch_extract 阶段的剩余 acquire 计数影响 enrich 阶段。

    loop id 检查同 ``qa_get_info._get_semaphore``：多次 ``asyncio.run()``
    会创建新 loop，复用旧 loop 的 sem 会报 ``bound to different event loop``。
    """
    global _enrich_sem, _enrich_sem_concurrency, _enrich_sem_loop_id
    try:
        current_loop_id: int | None = id(asyncio.get_running_loop())
    except RuntimeError:
        current_loop_id = None
    if (_enrich_sem is None
            or _enrich_sem_concurrency != concurrency
            or _enrich_sem_loop_id != current_loop_id):
        _enrich_sem = asyncio.Semaphore(concurrency)
        _enrich_sem_concurrency = concurrency
        _enrich_sem_loop_id = current_loop_id
    return _enrich_sem


def _reset_enrich_semaphore_for_test() -> None:
    """测试用：重置 Semaphore（每个测试 / 每个 event loop 独立）。"""
    global _enrich_sem, _enrich_sem_concurrency
    _enrich_sem = None
    _enrich_sem_concurrency = 0


# ---------------------------------------------------------------------------
# 子状态 TypedDict（enrich_one 的 Send 输入）
# ---------------------------------------------------------------------------


class EnrichState(TypedDict, total=False):
    """enrich_one 节点的 Send 子状态。"""
    chunk_file: str             # chunk 文件绝对路径


# ---------------------------------------------------------------------------
# Conditional Edge: fanout_enrich_dispatcher
# ---------------------------------------------------------------------------


def fanout_enrich_dispatcher(state: dict[str, Any]) -> Any:
    """1 重 gate：``chunk_files`` 空（全部 doc 失败 chunk 阶段）→ T28：短路 ingest。"""
    from langgraph.types import Send  # 局部 import 避免顶层强依赖

    chunk_files = list(state.get("chunk_files", []) or [])
    if not chunk_files:
        return "ingest"

    return [Send("enrich_one", {"chunk_file": cf}) for cf in chunk_files]


# ---------------------------------------------------------------------------
# Node: enrich_one (async, Semaphore 限流)
# ---------------------------------------------------------------------------


def create_enrich_one(
    llm: Any,
    config: GetInfoConfig | None = None,
) -> Callable:
    """enrich_one async 节点工厂。

    每个 Send 实例独立 acquire enrich Semaphore（默认 cfg.enrich_concurrency=3），
    防 LLM API 限流。

    步骤：
        1. 读 chunk file → split_frontmatter → fm + body
        2. invoke_structured(llm, ChunkEnrichment, ENRICH_SYSTEM_PROMPT, ...)
        3. LLM 失败重试 1 次（沿用 ENRICH_RETRY_COUNT，与 nodes/persistence.py 同语义）
        4. 重试仍失败 → inject_enrich_error 写错误标记到 frontmatter，success=False
        5. 成功 → inject_enrichment 写 4 字段到 frontmatter，success=True
        6. 返回 ``enrich_results: [{chunk_file, success, error?}]``

    失败隔离：任一步骤抛错 → 单 Send 写 success=False，不阻断其他 Send。
    """
    cfg = config or GetInfoConfig()

    async def enrich_one(sub_state: EnrichState) -> dict[str, Any]:
        chunk_file = sub_state.get("chunk_file") or ""
        sem = _get_enrich_semaphore(cfg.enrich_concurrency)
        async with sem:
            try:
                if not chunk_file:
                    raise ValueError("empty chunk_file")
                path = Path(chunk_file)
                if not path.exists():
                    raise FileNotFoundError(f"chunk file not found: {chunk_file}")

                text = await asyncio.to_thread(path.read_text, encoding="utf-8")
                fm, body = split_frontmatter(text)
                if not fm:
                    raise ValueError(f"chunk has no frontmatter: {chunk_file}")

                # LLM 重试 1 次（沿用 nodes/persistence.py 的语义）
                result: ChunkEnrichment | None = None
                last_exc: Exception | None = None
                for attempt in range(ENRICH_RETRY_COUNT + 1):
                    try:
                        result = await asyncio.to_thread(
                            invoke_structured,
                            llm,
                            ChunkEnrichment,
                            ENRICH_SYSTEM_PROMPT,
                            ENRICH_USER_PROMPT_TEMPLATE.format(chunk_text=body),
                        )
                        break
                    except Exception as exc:  # noqa: BLE001 — 需要重试
                        last_exc = exc
                        if attempt < ENRICH_RETRY_COUNT:
                            logger.warning(
                                "enrich_one: %s 第 %d 次失败，%.1fs 后重试: %s",
                                path.name,
                                attempt + 1,
                                ENRICH_RETRY_BACKOFF_SEC,
                                str(exc)[:120],
                            )
                            await asyncio.sleep(ENRICH_RETRY_BACKOFF_SEC)

                if result is None:
                    # 所有重试失败：写 enrich_error 到 frontmatter，success=False
                    err_msg = (
                        f"{type(last_exc).__name__}: {str(last_exc)[:160]}"
                        if last_exc
                        else "unknown"
                    )
                    new_fm = inject_enrich_error(fm, err_msg)
                    await asyncio.to_thread(
                        path.write_text,
                        reassemble(new_fm, body),
                        encoding="utf-8",
                    )
                    return {
                        "enrich_results": [
                            {
                                "chunk_file": chunk_file,
                                "success": False,
                                "error": err_msg,
                            }
                        ]
                    }

                # 成功：写 4 字段 enrichment 回 frontmatter
                new_fm = inject_enrichment(
                    fm,
                    title=result.title,
                    summary=result.summary,
                    keywords=list(result.keywords),
                    questions=list(result.questions),
                )
                await asyncio.to_thread(
                    path.write_text,
                    reassemble(new_fm, body),
                    encoding="utf-8",
                )
                return {
                    "enrich_results": [
                        {
                            "chunk_file": chunk_file,
                            "success": True,
                        }
                    ]
                }
            except Exception as e:  # noqa: BLE001 — fan-out 失败隔离
                logger.warning(
                    "enrich_one outer fail %s: %s", chunk_file, str(e)[:200]
                )
                return {
                    "enrich_results": [
                        {
                            "chunk_file": chunk_file,
                            "success": False,
                            "error": str(e)[:200],
                        }
                    ]
                }

    return enrich_one


# ---------------------------------------------------------------------------
# Node: barrier_enrich (sync, fan-in 聚合)
# ---------------------------------------------------------------------------


def barrier_enrich_node(state: dict[str, Any]) -> dict[str, Any]:
    """fan-in：聚合 ``enrich_results`` → ``enriched_chunks``（仅 success）+ 错误归集。

    入：``enrich_results: list[dict]``（reducer add 累加）
    出：
        - ``enriched_chunks``：success=True 的 chunk 文件路径（失败的不进 ingest）
        - ``persist_errors``：累加上游 errors + 当前阶段 enrich 失败聚合
    """
    results = list(state.get("enrich_results", []) or [])
    enriched: list[str] = []
    errors: list[str] = list(state.get("persist_errors", []) or [])

    for r in results:
        if r.get("success", False):
            enriched.append(r.get("chunk_file", ""))
        else:
            errors.append(
                f"enrich {r.get('chunk_file', '?')}: {r.get('error', '')}"
            )

    return {
        "enriched_chunks": enriched,
        "persist_errors": errors,
    }


# ---------------------------------------------------------------------------
# Node: ingest_node (sync, fail-fast)
# ---------------------------------------------------------------------------


def ingest_node(state: dict[str, Any]) -> dict[str, Any]:
    """单批入库 ``enriched_chunks`` 到 Milvus。

    fail-fast（CLAUDE.md 规则 25）：``milvus_ingest_chunks`` 抛错直接透传，不
    try/except。批失败说明 Milvus 异常，让整个 QA 报错而非吞掉（更易排障）。

    ``enriched_chunks`` 空（barrier_enrich 全部 success=False，或上游全部失败）
    → 不调 Milvus，返回 ``ingested_count=0``，让主图继续走 legacy_dense_search。
    """
    chunk_files = list(state.get("enriched_chunks", []) or [])
    if not chunk_files:
        return {"ingested_count": 0}

    report = milvus_ingest_chunks([Path(cf) for cf in chunk_files])
    return {"ingested_count": int(report.get("inserted", 0))}
