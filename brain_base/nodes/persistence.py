"""
KnowledgePersistence 子图节点函数。

流程：chunk → enrich → ingest

物理切分由 `bin/chunker.py` 确定性完成；LLM 只做 enrichment（写回
chunk frontmatter 的 summary / keywords / questions）。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from pathlib import Path as _P
from typing import Any, Callable

from brain_base.agents.schemas import ChunkEnrichment
from brain_base.agents.utils.structured import invoke_structured
from brain_base.nodes._frontmatter import (
    inject_enrichment,
    reassemble,
    split_frontmatter,
)
from brain_base.prompts.persistence_prompts import (
    ENRICH_SYSTEM_PROMPT,
    ENRICH_USER_PROMPT_TEMPLATE,
)

# bin/ 下的模块名带连字符，用 importlib 动态导入
# milvus-cli.py 内部 from milvus_config import ...，需要 bin/ 在 sys.path 上
_BIN_DIR = str(_P(__file__).resolve().parent.parent.parent / "bin")
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

_chunker = importlib.import_module("bin.chunker")
write_chunks = _chunker.write_chunks

_milvus_cli = importlib.import_module("bin.milvus-cli")
milvus_ingest_chunks = _milvus_cli.ingest_chunks


# ---------------------------------------------------------------------------
# chunk：物理切分（确定性）
# ---------------------------------------------------------------------------


def chunk_node(state: dict[str, Any]) -> dict[str, Any]:
    """调用 bin.chunker.write_chunks 生成 chunk Markdown。"""
    raw_md_path = state.get("raw_md_path", "")
    if not raw_md_path:
        return {"error": "chunk_node: raw_md_path 为空"}

    doc_id = state.get("doc_id", "")
    chunk_dir = state.get("chunk_dir", "data/docs/chunks")

    written = write_chunks(
        raw_path=Path(raw_md_path),
        output_dir=Path(chunk_dir),
    )

    chunk_files = [str(p) for p in written]
    if not chunk_files:
        return {"error": f"chunker 未生成 chunk 文件: {doc_id}-*.md"}

    return {"chunk_files": chunk_files, "chunk_dir": chunk_dir}


# ---------------------------------------------------------------------------
# enrich：LLM 富化（summary / keywords / questions）
# ---------------------------------------------------------------------------


_ENRICH_FIELDS = ("summary:", "keywords:", "questions:")


def _chunk_needs_enrich(text: str) -> bool:
    """判断 chunk 是否缺富化字段。"""
    return any(field not in text for field in _ENRICH_FIELDS)


def create_enrich_node(llm: Any = None) -> Callable:
    """chunk 富化节点工厂。

    Args:
        llm: 可选 LLM 实例
            - None：仅做前置检查，标记需 enrich 的 chunk，不真正写入
              （等外部 LLM 流程接管，CLAUDE.md 硬约束 14：软依赖降级）。
            - 非 None：调 `with_structured_output(ChunkEnrichment)` 富化并
              写回 chunk frontmatter。
    """

    def enrich_node(state: dict[str, Any]) -> dict[str, Any]:
        chunk_files = state.get("chunk_files", [])
        if not chunk_files:
            return {"error": "enrich_node: chunk_files 为空", "enriched": False}

        # 收集需要富化的 chunk
        to_enrich: list[Path] = []
        for cf in chunk_files:
            path = Path(cf)
            if not path.exists():
                continue
            if _chunk_needs_enrich(path.read_text(encoding="utf-8")):
                to_enrich.append(path)

        if not to_enrich:
            return {"enriched": True, "enriched_count": 0}

        # 降级路径：仅返回待处理列表，由外部流程接管
        if llm is None:
            return {
                "enriched": False,
                "chunks_to_enrich": [str(p) for p in to_enrich],
            }

        # LLM 路径：with_structured_output 写回
        enriched_count = 0
        skipped_count = 0
        for path in to_enrich:
            text = path.read_text(encoding="utf-8")
            fm, body = split_frontmatter(text)
            if not fm:
                # 没 frontmatter 的 chunk 不富化（chunker 应当总是写 frontmatter）
                skipped_count += 1
                continue

            try:
                result = invoke_structured(
                    llm,
                    ChunkEnrichment,
                    ENRICH_SYSTEM_PROMPT,
                    ENRICH_USER_PROMPT_TEMPLATE.format(chunk_text=body),
                )
            except Exception:
                # 单个 chunk 失败不阻断整批；fail 仅跳过该 chunk
                skipped_count += 1
                continue

            new_fm = inject_enrichment(
                fm,
                summary=result.summary,
                keywords=list(result.keywords),
                questions=list(result.questions),
            )
            path.write_text(reassemble(new_fm, body), encoding="utf-8")
            enriched_count += 1

        return {
            "enriched": True,
            "enriched_count": enriched_count,
            "skipped_count": skipped_count,
        }

    return enrich_node


# 向后兼容：导出无 LLM 版本（当前图未注入 LLM 时仍可工作）
enrich_node = create_enrich_node(llm=None)


# ---------------------------------------------------------------------------
# ingest：写入 Milvus
# ---------------------------------------------------------------------------


def ingest_node(state: dict[str, Any]) -> dict[str, Any]:
    """调用 bin.milvus_cli.ingest_chunks 完成 hybrid 入库。"""
    chunk_files = state.get("chunk_files", [])
    if not chunk_files:
        return {"error": "ingest_node: chunk_files 为空", "milvus_inserted": 0}

    report = milvus_ingest_chunks(
        chunk_files=[Path(cf) for cf in chunk_files],
    )
    inserted = report.get("inserted", 0)

    return {"milvus_inserted": inserted, "error": ""}
