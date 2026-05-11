"""
IngestFile 图节点函数。

流程（T32 重构后）：convert → frontmatter → doc_enrich → persist
参考 ../brain-base-backup/skills/upload-ingest/SKILL.md

核心逻辑直接 import bin/ 下的 Python 模块，不通过 subprocess。
"""

import hashlib
import logging
import time
import traceback
from datetime import date
from pathlib import Path
from typing import Any, Callable

import importlib

from brain_base.agents.schemas import DocEnrichment
from brain_base.agents.utils.structured import invoke_structured
from brain_base.nodes._frontmatter import (
    inject_doc_enrichment,
    reassemble,
    split_frontmatter,
)
from brain_base.prompts.doc_enrich_prompts import (
    DOC_ENRICH_SYSTEM_PROMPT,
    DOC_ENRICH_USER_PROMPT_TEMPLATE,
)

logger = logging.getLogger(__name__)

# T32 新增：doc 级 enrich 重试参数，与 nodes/persistence.py ENRICH_RETRY_* 同语义
_DOC_ENRICH_RETRY_COUNT = 1
_DOC_ENRICH_RETRY_BACKOFF_SEC = 1.0
# T32 新增：doc 级 LLM 输入前缀长度（趌进 2000 字符覆盖 abstract + intro）
_DOC_ENRICH_HEAD_CHARS = 2000

# bin/ 下的模块名带连字符，用 importlib 动态导入
_doc_converter = importlib.import_module("bin.doc-converter")
convert_one = _doc_converter.convert_one


def _compute_file_sha256(file_path: Path, chunk_size: int = 1 << 16) -> str:
    """T33: 计算任意文件二进制内容的 SHA-256。

    用于 upload 路径的 dedup short-circuit——convert 之前在 PDF 原始二进制上
    算指纹，避免重复 PDF 走完整 MinerU 转换（30+ min）。分块读取避免
    大文件一次性载入内存。
    """
    h = hashlib.sha256()
    with file_path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _lookup_by_frontmatter_sha256(
    sha256: str, raw_dir: Path = Path("data/docs/raw")
) -> dict[str, str] | None:
    """T33: 扫 raw 目录所有 md 的 frontmatter ``content_sha256`` 字段声明值，
    找到与传入 sha256 匹配的文档则返回 ``{doc_id, raw_path}``。

    **不用 hash_lookup**——hash_lookup 内部 ``_build_hash_index`` 按重算的
    body markdown sha256 建索引，而 upload 路径写入的 content_sha256 是 PDF
    二进制 sha256（两者不可能相等），导致 hash_lookup 永远 miss。
    本函数直接读 frontmatter 声明值比较，不重算 body。
    """
    if not raw_dir.exists():
        return None
    for raw_file in sorted(raw_dir.glob("*.md")):
        try:
            text = raw_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        for line in text.split("\n"):
            if line.startswith("content_sha256:"):
                declared = line.split(":", 1)[1].strip()
                if declared == sha256:
                    return {"doc_id": raw_file.stem, "raw_path": str(raw_file)}
                break  # content_sha256 字段只出现一次，找到就跳出内层
    return None


def convert_node(state: dict[str, Any]) -> dict[str, Any]:
    """调用 bin.doc_converter.convert_one 完成格式转换。

    T33 dedup short-circuit：convert 之前先算 PDF 二进制 sha256 查重，
    命中跳过整个转换。避免 MinerU 30+ min 重跑。字段名沿用
    ``content_sha256``（与外检路径一致），语义按 source 区分：
    user-upload 路径 = PDF 二进制 sha256；外检 = cleaned markdown sha256。
    """
    input_files = state.get("input_files", [])
    if not input_files:
        return {
            "error": "convert_node: input_files 为空",
            "converted": [],
            "conversion_errors": [],
            "dedup_skipped": [],
        }

    upload_date_str = state.get("upload_date", date.today().isoformat())
    upload_date = date.fromisoformat(upload_date_str) if upload_date_str else None

    converted: list[dict] = []
    conversion_errors: list[dict] = []
    dedup_skipped: list[dict] = []  # T33: dedup 命中跳过的文件清单

    for fp in input_files:
        input_path = Path(fp)

        # T33 dedup short-circuit：算 PDF 二进制 sha256 表查 raw 目录已有同内容文档。
        # 务必在 convert 之前做——MinerU 一跳就是 30+ min，跳完才发现重复于事无补。
        try:
            binary_sha256 = _compute_file_sha256(input_path)
        except OSError as exc:
            conversion_errors.append({"input": fp, "error": f"读取文件失败: {exc}"})
            continue

        existing = _lookup_by_frontmatter_sha256(binary_sha256)
        if existing is not None:
            existing_doc_id = existing["doc_id"]
            logger.info(
                "convert_node: hash hit, skipped convert. sha256=%s existing_doc_id=%s incoming=%s",
                binary_sha256, existing_doc_id, input_path.name,
            )
            dedup_skipped.append({
                "input": fp,
                "existing_doc_id": existing_doc_id,
                "sha256": binary_sha256,
            })
            continue

        try:
            result = convert_one(
                input_path=input_path,
                output_dir=Path("data/docs/raw"),
                uploads_dir=Path("data/docs/uploads"),
                upload_date=upload_date,
            )
            # T33: 透传 PDF 二进制 sha256 给 frontmatter_node 写入 content_sha256
            result["content_sha256"] = binary_sha256
            converted.append(result)
        except Exception as exc:
            conversion_errors.append({"input": fp, "error": str(exc)})

    return {
        "converted": converted,
        "conversion_errors": conversion_errors,
        "dedup_skipped": dedup_skipped,
    }


def frontmatter_node(state: dict[str, Any]) -> dict[str, Any]:
    """为每个 raw MD 组装 user-upload frontmatter。

    T33: dedup 已移到 convert_node 顶部（基于 PDF 二进制 sha256，事前检测），
    本节点不再算 body markdown sha256，直接用 convert_node 透传的 content_sha256。
    字段名沿用 ``content_sha256``不变，语义按 source 区分。
    """
    # T32 F7：上游 convert_node 可能将部分文件 try-except 到 conversion_errors，
    # 这里显式 log warning 避免错误被静默吞。
    conversion_errors = state.get("conversion_errors", []) or []
    if conversion_errors:
        logger.warning(
            "frontmatter_node: %d 个文件在 convert 阶段失败，跳过这些：%s",
            len(conversion_errors),
            "; ".join(f"{e.get('input', '?')} -> {e.get('error', '?')[:80]}" for e in conversion_errors),
        )

    converted = state.get("converted", [])
    if not converted:
        return {"raw_paths": [], "error": state.get("error", "frontmatter_node: 无转换结果")}

    upload_date = state.get("upload_date", date.today().isoformat())
    raw_paths: list[str] = []

    for item in converted:
        raw_path = Path(item["raw_path"])
        if not raw_path.exists():
            continue

        body = raw_path.read_text(encoding="utf-8")

        # T33: content_sha256 由 convert_node 透传（PDF 二进制 sha256），本节点不再算。
        # 后兼容：item 中缺字段时 fallback 算 body markdown sha256（老调用路径 / 测试）。
        content_sha256 = item.get("content_sha256") or hashlib.sha256(
            body.replace("\r\n", "\n").strip("\n").encode("utf-8")
        ).hexdigest()

        title = item.get("doc_id", "untitled")
        for line in body.split("\n"):
            if line.startswith("# "):
                title = line[2:].strip()
                break

        fm = (
            "---\n"
            f"doc_id: {item['doc_id']}\n"
            f"title: {title}\n"
            "source: user-upload\n"
            "source_type: user-upload\n"
            f"original_file: {item.get('original_file', '')}\n"
            "url:\n"
            f"fetched_at: {upload_date}\n"
            f"content_sha256: {content_sha256}\n"
            "keywords: []\n"
            "---\n\n"
        )
        raw_path.write_text(fm + body, encoding="utf-8")
        raw_paths.append(str(raw_path))

    return {"raw_paths": raw_paths}


def create_doc_enrich_node(llm: Any) -> Callable:
    """T32 新增：doc 级 LLM 富化节点工厂。

    流程（位于 frontmatter_node 与 persist_node 之间）：
        1. 逐 raw_path：split_frontmatter → fm + body
        2. body 取前 2000 字符作为 doc_head
        3. invoke_structured(llm, DocEnrichment, DOC_ENRICH_SYSTEM_PROMPT, ...)
        4. 失败重试 1 次（瞬时性故障：网络 / 429 / 输出偏差）
        5. 成功 → inject_doc_enrichment 写回 fm 的 summary + keywords
        6. 失败 → 该文件从 raw_paths 移除（不进 persist）、记到 doc_enrich_errors

    Args:
        llm: 必须传；llm=None 时 raise RuntimeError（CLAUDE.md 规则 14：upload 路径属核心
             Agent 节点，LLM 缺失 fail-fast）。
    """
    if llm is None:
        raise RuntimeError(
            "create_doc_enrich_node: llm 必须提供。upload 路径属核心 Agent 节点，"
            "LLM 缺失不能走降级（CLAUDE.md 规则 14）。请在 cli 加载 LLM 后传入。"
        )

    def doc_enrich_node(state: dict[str, Any]) -> dict[str, Any]:
        raw_paths: list[str] = state.get("raw_paths", []) or []
        if not raw_paths:
            return {
                "doc_enriched": False,
                "doc_enriched_count": 0,
                "doc_enrich_errors": [],
            }

        success_paths: list[str] = []
        errors: list[dict] = []

        for rp in raw_paths:
            path = Path(rp)
            if not path.exists():
                errors.append({"raw_path": rp, "error": "raw_path 不存在"})
                continue

            text = path.read_text(encoding="utf-8")
            fm, body = split_frontmatter(text)
            if not fm:
                logger.warning("doc_enrich_node: %s 无 frontmatter，跳过", path.name)
                errors.append({"raw_path": rp, "error": "无 frontmatter"})
                continue

            doc_head = body[:_DOC_ENRICH_HEAD_CHARS]

            result = None
            last_exc: Exception | None = None
            for attempt in range(_DOC_ENRICH_RETRY_COUNT + 1):
                try:
                    result = invoke_structured(
                        llm,
                        DocEnrichment,
                        DOC_ENRICH_SYSTEM_PROMPT,
                        DOC_ENRICH_USER_PROMPT_TEMPLATE.format(doc_head=doc_head),
                    )
                    break
                except Exception as exc:  # noqa: BLE001 — 需要重试后透传错误
                    last_exc = exc
                    if attempt < _DOC_ENRICH_RETRY_COUNT:
                        logger.warning(
                            "doc_enrich_node: %s 第 %d 次调 LLM 失败，%.1fs 后重试: %s",
                            path.name, attempt + 1, _DOC_ENRICH_RETRY_BACKOFF_SEC,
                            str(exc)[:120],
                        )
                        time.sleep(_DOC_ENRICH_RETRY_BACKOFF_SEC)

            if result is None:
                err_msg = (
                    f"{type(last_exc).__name__}: {str(last_exc)[:160]}"
                    if last_exc else "unknown"
                )
                logger.warning(
                    "doc_enrich_node: %s LLM 富化失败（重试 %d 次仍失败）: %s\n%s",
                    path.name,
                    _DOC_ENRICH_RETRY_COUNT,
                    err_msg,
                    "".join(traceback.format_exception_only(type(last_exc), last_exc)) if last_exc else "",
                )
                errors.append({"raw_path": rp, "error": err_msg})
                continue

            new_fm = inject_doc_enrichment(
                fm,
                summary=result.summary,
                keywords=list(result.keywords),
            )
            path.write_text(reassemble(new_fm, body), encoding="utf-8")
            success_paths.append(rp)

        return {
            "doc_enriched": len(success_paths) > 0,
            "doc_enriched_count": len(success_paths),
            "doc_enrich_errors": errors,
            # 覆写 raw_paths：只让 doc 级富化成功的文件进 persist（D4）
            "raw_paths": success_paths,
        }

    return doc_enrich_node


def create_persist_node(llm: Any = None):
    """持久化节点工厂：循环调用 PersistenceGraph 处理多个 raw 文件"""
    from brain_base.graphs.persistence_graph import PersistenceGraph

    def persist_node(state: dict[str, Any]) -> dict[str, Any]:
        raw_paths = state.get("raw_paths", [])
        if not raw_paths:
            return {"persistence_results": [], "error": state.get("error", "")}

        pg = PersistenceGraph(llm=llm)
        results: list[dict] = []

        for rp in raw_paths:
            path = Path(rp)
            doc_id = path.stem
            try:
                r = pg.run(raw_md_path=rp, doc_id=doc_id)
                results.append(r)
            except Exception as exc:
                results.append({"doc_id": doc_id, "error": str(exc)})

        return {"persistence_results": results}

    return persist_node


# T32 重构：删除模块级 persist_node = create_persist_node(llm=None) 导出。
# 原代码作为"向后兼容"使用，但 grep 全仓无人 import（仅 graphs/ingest_file_graph.py 用工厂
# create_persist_node(llm)，不用模块级实例）；llm=None 调用现在 raise，该导出意义已失。
