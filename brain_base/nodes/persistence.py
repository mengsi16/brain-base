"""
KnowledgePersistence 子图节点函数。

流程：chunk → enrich → ingest

物理切分由 `bin/chunker.py` 确定性完成；LLM 只做 enrichment（写回
chunk frontmatter 的 summary / keywords / questions）。
"""

from __future__ import annotations

import importlib
import logging
import re
import sys
import time
import traceback
from pathlib import Path
from pathlib import Path as _P
from typing import Any, Callable

from brain_base.agents.schemas import ChunkEnrichment
from brain_base.agents.utils.structured import invoke_structured
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

logger = logging.getLogger(__name__)

# T17 重试参数：单 chunk LLM 调用失败后重试 1 次，间隔 1s。
# 该重试只针对瞬时性故障（网络抖动 / 429 / 偏差输出）生效；
# 如果是系统性问题（schema 太严 / chunk 内容不适合）重试也会失败，
# 这时候 enrich_error 字段就派上用场。
ENRICH_RETRY_COUNT = 1
ENRICH_RETRY_BACKOFF_SEC = 1.0

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

# 空占位符模式：``chunker`` 写出的 frontmatter 形如：
#   summary: ""
#   keywords: []
#   questions: []
# 这些字段名都在文本里（``"summary:" in text`` 为 True），但值是空占位符。
# 老逻辑只判 key 名 → 把"空字段"当作"已富化"导致 enrich 永远跳过。
# 这里按字段名分别匹配它们的"空值"形式，命中任意一项即视为待富化。
_EMPTY_PLACEHOLDER_PATTERNS = (
    # summary: "" 或 summary: ''（允许前后空白；YAML 字符串型空值）
    re.compile(r'^summary\s*:\s*(""|\'\')\s*$', re.MULTILINE),
    # keywords: [] 或 keywords: [ ]（YAML 数组型空值）
    re.compile(r'^keywords\s*:\s*\[\s*\]\s*$', re.MULTILINE),
    # questions: [] 或 questions: [ ]
    re.compile(r'^questions\s*:\s*\[\s*\]\s*$', re.MULTILINE),
)


def _chunk_needs_enrich(text: str) -> bool:
    """判断 chunk 是否缺富化字段。

    返回 True 的两种情况（任一即可）：
    1. 字段缺失：``summary:`` / ``keywords:`` / ``questions:`` 任一 key 不在 frontmatter。
    2. 字段空占位符：key 存在但值是 ``""`` / ``''`` / ``[]`` 这类 chunker 写的占位符。
       这是 T13 修复的核心——老逻辑只看 key 名漏掉了占位符场景。
    """
    if any(field not in text for field in _ENRICH_FIELDS):
        return True
    return any(p.search(text) for p in _EMPTY_PLACEHOLDER_PATTERNS)


def create_enrich_node(llm: Any) -> Callable:
    """chunk 富化节点工厂。

    T32 重构：删 ``llm is None`` 降级分支。LLM 缺失时 fail-fast（CLAUDE.md 规则 14）。
    upload / get-info 路径都是核心 Agent 节点，不接受“软降级到不富化”路径——那会让残缺
    chunk 进 milvus、检索质量严重退化（T32 F2/F5 初现正是这个问题）。

    Args:
        llm: LLM 实例。必须传；llm=None 时 raise RuntimeError。
    """
    if llm is None:
        raise RuntimeError(
            "create_enrich_node: llm 必须提供。chunk 富化是 core Agent 节点，"
            "LLM 缺失不能走降级（CLAUDE.md 规则 14）。上游 PersistenceGraph / cli 负责加载 LLM。"
        )

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

        # LLM 路径：with_structured_output 写回
        enriched_count = 0
        skipped_count = 0
        for path in to_enrich:
            text = path.read_text(encoding="utf-8")
            fm, body = split_frontmatter(text)
            if not fm:
                # 没 frontmatter 的 chunk 不富化（chunker 应当总是写 frontmatter）
                logger.warning("enrich_node: chunk 无 frontmatter，跳过 %s", path)
                skipped_count += 1
                continue

            # T17：重试 1 次（总调用 ≤ ENRICH_RETRY_COUNT + 1）。
            # 仅 “瞬时性故障” 重试（网络 / 429 / LLM 输出偏差）；
            # 系统性问题重试依然失败。
            result = None
            last_exc: Exception | None = None
            for attempt in range(ENRICH_RETRY_COUNT + 1):
                try:
                    result = invoke_structured(
                        llm,
                        ChunkEnrichment,
                        ENRICH_SYSTEM_PROMPT,
                        ENRICH_USER_PROMPT_TEMPLATE.format(chunk_text=body),
                    )
                    break
                except Exception as exc:  # noqa: BLE001 — 需要在重试后透传错误
                    last_exc = exc
                    if attempt < ENRICH_RETRY_COUNT:
                        logger.warning(
                            "enrich_node: chunk %s 第 %d 次调 LLM 失败，%.1fs 后重试: %s",
                            path.name, attempt + 1, ENRICH_RETRY_BACKOFF_SEC, str(exc)[:120],
                        )
                        time.sleep(ENRICH_RETRY_BACKOFF_SEC)

            if result is None:
                # 所有重试都失败：log warning + traceback，并将错误写入 frontmatter
                # （CLAUDE.md 规则 25：fail-fast 不吞错；规则 29：错误信息透传）
                err_msg = f"{type(last_exc).__name__}: {str(last_exc)[:160]}" if last_exc else "unknown"
                logger.warning(
                    "enrich_node: chunk %s LLM 富化失败（重试 %d 次仍失败）: %s\n%s",
                    path.name,
                    ENRICH_RETRY_COUNT,
                    err_msg,
                    "".join(traceback.format_exception_only(type(last_exc), last_exc)) if last_exc else "",
                )
                new_fm = inject_enrich_error(fm, err_msg)
                path.write_text(reassemble(new_fm, body), encoding="utf-8")
                skipped_count += 1
                continue

            new_fm = inject_enrichment(
                fm,
                title=result.title,
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


# T32 重构：删除模块级 enrich_node = create_enrich_node(llm=None) 导出。
# 原代码作为“向后兼容”使用，但 grep 全仓无人 import（T32 诊断报告 F2 验证）；llm=None
# 调用现在 raise RuntimeError，该导出会在 import 时报错，顺势删除。


# ---------------------------------------------------------------------------
# ingest：写入 Milvus
# ---------------------------------------------------------------------------


def ingest_node(state: dict[str, Any]) -> dict[str, Any]:
    """调用 bin.milvus_cli.ingest_chunks 完成 hybrid 入库。

    T32 F5：加 enrich 状态前置检查。``enriched=False`` 表示 enrich 阶段未运行或失败，
    入库会让残缺 chunk 进 milvus 造成检索质量退化 → fail-fast。
    """
    chunk_files = state.get("chunk_files", [])
    if not chunk_files:
        return {"error": "ingest_node: chunk_files 为空", "milvus_inserted": 0}

    # T32 F5 guard：enrich 未完成（或 chunk_files 空造成 enriched=False）时 raise
    # 不用 enriched_count > 0 是因为“tmp 复跑”场景（所有 chunk 已富化）enriched=True / enriched_count=0 仍应入库
    if not state.get("enriched", False):
        raise RuntimeError(
            "ingest_node: enrich 阶段未完成或失败（enriched=False），拒绝入库。避免残缺 chunk "
            "进 milvus 造成检索质量退化（T32 F5 / CLAUDE.md 规则 14）。"
        )

    # T33：upload 路径重跑同 doc 必须先按 doc_id 删旧 milvus 行再插新行，
    # 否则追加模式会产生重复 chunk（同 doc_id × 多份），污染检索召回 + 扭曲 sparse 词频。
    # milvus_cli.ingest_chunks 已支持 replace_docs=True（delete by doc_id + insert）。
    # QA 路径 qa_persist.ingest_node 不加此参数——QA 处理的是新 candidate，doc_id 不会撞车。
    report = milvus_ingest_chunks(
        chunk_files=[Path(cf) for cf in chunk_files],
        replace_docs=True,
    )
    inserted = report.get("inserted", 0)

    return {"milvus_inserted": inserted, "error": ""}
