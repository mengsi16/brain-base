#!/usr/bin/env python3
"""
Milvus CLI for knowledge-base.

目标：
1. 去掉伪造的 hash 向量化。
2. 显式区分 dense / hybrid 检索。
3. 通过可配置的 embedding provider 接入真实向量化能力。
"""

import argparse
import hashlib
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pymilvus import (
    AnnSearchRequest,
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    MilvusClient,
    RRFRanker,
    connections,
    utility,
)

from milvus_config import (
    ChunkRecord,
    build_embedding_runtime,
    check_embedding_runtime,
    collection_from_env,
    dense_field_from_env,
    load_runtime_settings,
    output_fields_from_env,
    parse_chunk_file,
    sparse_field_from_env,
    text_field_from_env,
)


def connect_collection(settings: dict[str, Any]) -> Collection:
    connections.connect(
        alias="default",
        uri=settings["milvus_uri"],
        token=settings["milvus_token"],
        db_name=settings["milvus_db"],
    )
    collection = Collection(settings["milvus_collection"])
    collection.load()
    return collection


def collection_has_field(collection: Collection, field_name: str) -> bool:
    return any(field.name == field_name for field in collection.schema.fields)


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节长度截断字符串，确保不超过 max_bytes"""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # 从 max_bytes 处截断，避免截断多字节字符中间
    truncated = encoded[:max_bytes]
    try:
        return truncated.decode("utf-8")
    except UnicodeDecodeError:
        # 去掉最后一个不完整的多字节字符
        return truncated[:max_bytes - (truncated[-1] >> 6 & 0b11 == 0b10 and 1 or truncated[-1] >> 5 & 0b110 == 0b110 and 2 or 1)].decode("utf-8", errors="ignore")


def _first_heading(markdown: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _first_paragraph(markdown: str) -> str:
    lines = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            if lines:
                break
            continue
        if stripped.startswith("#"):
            continue
        lines.append(stripped)
    paragraph = " ".join(lines).strip()
    return paragraph[:500]


DOC2QUERY_INDEX_PATH = Path("data/eval/doc2query-index.json")


def _load_doc2query_index() -> dict[str, list[str]]:
    """Load doc2query-index.json and return {chunk_id: questions} mapping."""
    if not DOC2QUERY_INDEX_PATH.exists():
        return {}
    try:
        data = json.loads(DOC2QUERY_INDEX_PATH.read_text(encoding="utf-8"))
        return {
            cid: entry["questions"]
            for cid, entry in data.get("entries", {}).items()
            if "questions" in entry
        }
    except (json.JSONDecodeError, KeyError):
        return {}


def _parse_questions_value(raw_value: str) -> list[str]:
    """Parse a frontmatter ``questions`` value.

    Supported inline form (recommended, easiest to keep diff-friendly):

        questions: ["What is X?", "How to do Y?"]

    Non-JSON input returns an empty list rather than raising, so legacy chunk files
    without synthetic questions keep working.
    """
    raw_value = (raw_value or "").strip()
    if not raw_value or not raw_value.startswith("["):
        return []
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def _parse_markdown_frontmatter(
    chunk_file: Path, doc2query_index: dict[str, list[str]] | None = None
) -> dict[str, Any] | None:
    text = chunk_file.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None

    parts = text.split("---", 2)
    if len(parts) < 3:
        return None

    metadata_text = parts[1]
    content = parts[2].strip()

    metadata: dict[str, Any] = {}
    for line in metadata_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()

    if not metadata.get("doc_id") or not metadata.get("chunk_id"):
        return None

    chunk_id = metadata.get("chunk_id", "")
    section_path = metadata.get("section_path", "")
    if isinstance(section_path, list):
        section_path = " / ".join(str(item) for item in section_path)

    title = _truncate_utf8(metadata.get("title") or _first_heading(content), 1024)
    section_path = _truncate_utf8(section_path, 1024)
    summary = metadata.get("summary") or _first_paragraph(content)
    keywords = metadata.get("keywords", "")

    # Priority: doc2query-index.json > chunk frontmatter
    if doc2query_index is not None and chunk_id in doc2query_index:
        questions = doc2query_index[chunk_id]
    else:
        questions = _parse_questions_value(metadata.get("questions", ""))

    return {
        "doc_id": metadata.get("doc_id", ""),
        "chunk_id": chunk_id,
        "title": title,
        "section_path": section_path,
        "source": metadata.get("source", ""),
        "source_type": metadata.get("source_type", ""),
        "url": metadata.get("url", ""),
        "original_file": metadata.get("original_file", ""),
        "fetched_at": metadata.get("fetched_at", ""),
        "summary": summary,
        "keywords": keywords,
        "chunk_text": content,
        "questions": questions,
        "source_file": str(chunk_file),
    }


def _to_float_list(vector: Any) -> list[float]:
    if hasattr(vector, "tolist"):
        return vector.tolist()
    return list(vector)


def _sparse_matrix_to_row_dicts(sparse_obj: Any, n_rows: int) -> list[dict[int, float]]:
    """Convert a BGE-M3 sparse output into a list of ``dict[int, float]`` (one per row).

    pymilvus' row-level insert for ``SPARSE_FLOAT_VECTOR`` expects each row value to
    represent exactly one sparse vector. Passing a 2D scipy slice of shape ``(1, vocab)``
    fails with "expect 1 row". Dict form ``{col_idx: value}`` is accepted natively and
    is stable across scipy versions / array vs matrix subclasses.

    Accepts:

    1. ``scipy.sparse`` matrix of shape ``(n, vocab)`` — grouped by ``.tocoo()``.
    2. An already-iterable list of per-row dicts / sparse rows — normalized element-wise.
    """
    row_dicts: list[dict[int, float]] = [dict() for _ in range(n_rows)]

    # Case 1: scipy.sparse matrix / array with .tocoo(); handles 2D shape (n, vocab).
    if hasattr(sparse_obj, "tocoo") and hasattr(sparse_obj, "shape") and len(sparse_obj.shape) == 2:
        coo = sparse_obj.tocoo()
        for r, c, v in zip(coo.row, coo.col, coo.data):
            row_dicts[int(r)][int(c)] = float(v)
        return row_dicts

    # Case 2: iterable of per-row objects (list / array of dicts / sparse rows).
    for idx, row in enumerate(sparse_obj):
        row_dicts[idx] = _single_sparse_to_dict(row)
    return row_dicts


def _single_sparse_to_dict(row: Any) -> dict[int, float]:
    """Normalize a single sparse row into ``dict[int, float]``.

    Accepts: dict, scipy 1-row matrix/array, 1D csr_array, or object with ``.indices`` / ``.data``.
    """
    if isinstance(row, dict):
        return {int(k): float(v) for k, v in row.items()}
    if hasattr(row, "tocoo"):
        coo = row.tocoo()
        # 2D (1, vocab) matrix slice -> use coo.col; 1D array -> coo.coords[0].
        if len(getattr(coo, "shape", ())) == 2:
            cols = coo.col
        else:
            cols = coo.coords[0] if hasattr(coo, "coords") else coo.col
        return {int(c): float(v) for c, v in zip(cols, coo.data)}
    if hasattr(row, "indices") and hasattr(row, "data"):
        return {int(c): float(v) for c, v in zip(row.indices, row.data)}
    # Fallback: assume iterable of (col, val) pairs.
    return {int(c): float(v) for c, v in row}


def _encode_documents(
    runtime: dict[str, Any], texts: list[str]
) -> tuple[list[list[float]], list[dict[int, float]] | None]:
    """Encode documents for ingestion.

    Returns:
        (dense_vectors, sparse_vectors_or_None). sparse_vectors is a list of
        ``dict[int, float]`` (one dict per row) when hybrid is active; ``None`` for
        dense-only providers.
    """
    encoder = runtime["encoder"]
    if hasattr(encoder, "encode_documents"):
        embeddings = encoder.encode_documents(texts)
    else:
        embeddings = encoder.encode_queries(texts)

    if runtime["mode"] == "hybrid":
        dense_embeddings = embeddings["dense"]
        sparse_embeddings = embeddings["sparse"]
        dense_vectors = [_to_float_list(v) for v in dense_embeddings]
        sparse_vectors = _sparse_matrix_to_row_dicts(sparse_embeddings, len(texts))
        return dense_vectors, sparse_vectors

    dense_vectors = [_to_float_list(v) for v in embeddings]
    return dense_vectors, None


def _encode_query(runtime: dict[str, Any], query: str) -> tuple[list[float], dict[int, float] | None]:
    """Encode a single query. Returns (dense_vector, sparse_dict_or_None)."""
    embeddings = runtime["encoder"].encode_queries([query])
    if runtime["mode"] == "hybrid":
        dense_vector = _to_float_list(embeddings["dense"][0])
        sparse_rows = _sparse_matrix_to_row_dicts(embeddings["sparse"], 1)
        return dense_vector, sparse_rows[0]
    dense_vector = _to_float_list(embeddings[0])
    return dense_vector, None


def _get_dense_field_dim(collection: Collection, dense_field: str) -> int | None:
    for field in collection.schema.fields:
        if field.name == dense_field:
            dim = field.params.get("dim") if field.params else None
            return int(dim) if dim is not None else None
    return None


def ensure_collection(
    settings: dict[str, Any], dense_dim: int, include_sparse: bool
) -> Collection:
    """Create or validate the Milvus collection.

    When include_sparse is True, the schema adds a SPARSE_FLOAT_VECTOR field so that
    bge-m3 hybrid (dense + sparse) retrieval works end-to-end. When False, only the
    dense field is created (sentence-transformer / openai / default providers).
    """
    connections.connect(
        alias="default",
        uri=settings["milvus_uri"],
        token=settings["milvus_token"],
        db_name=settings["milvus_db"],
    )

    collection_name = settings["milvus_collection"]
    dense_field = dense_field_from_env(settings)
    sparse_field = sparse_field_from_env(settings)
    text_field = text_field_from_env(settings)

    if not utility.has_collection(collection_name):
        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="kind", dtype=DataType.VARCHAR, max_length=16),
            FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="question_id", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="title", dtype=DataType.VARCHAR, max_length=1024),
            FieldSchema(name="section_path", dtype=DataType.VARCHAR, max_length=2048),
            FieldSchema(name="source", dtype=DataType.VARCHAR, max_length=1024),
            FieldSchema(name="url", dtype=DataType.VARCHAR, max_length=2048),
            FieldSchema(name="summary", dtype=DataType.VARCHAR, max_length=8192),
            FieldSchema(name=text_field, dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name=dense_field, dtype=DataType.FLOAT_VECTOR, dim=dense_dim),
        ]
        if include_sparse:
            fields.append(
                FieldSchema(name=sparse_field, dtype=DataType.SPARSE_FLOAT_VECTOR)
            )

        schema = CollectionSchema(
            fields=fields,
            description="knowledge-base chunk + synthetic-question embeddings",
            enable_dynamic_field=True,
        )
        collection = Collection(name=collection_name, schema=schema)
        collection.create_index(
            field_name=dense_field,
            index_params={"index_type": "AUTOINDEX", "metric_type": "IP", "params": {}},
        )
        if include_sparse:
            collection.create_index(
                field_name=sparse_field,
                index_params={
                    "index_type": "SPARSE_INVERTED_INDEX",
                    "metric_type": "IP",
                    "params": {"drop_ratio_build": 0.2},
                },
            )
    else:
        collection = Collection(name=collection_name)
        existing_dim = _get_dense_field_dim(collection, dense_field)
        if existing_dim is None:
            raise ValueError(
                f"集合 {collection_name} 缺少 dense 字段 {dense_field}，请重建集合或改配置。"
            )
        if existing_dim != dense_dim:
            raise ValueError(
                f"集合 {collection_name} 的 dense dim={existing_dim}，当前模型 dim={dense_dim}，"
                "不匹配。请 drop 旧集合再重新入库（provider 变更需要重建 collection）。"
            )
        if include_sparse and not collection_has_field(collection, sparse_field):
            raise ValueError(
                f"当前 provider 使用 hybrid，但集合 {collection_name} 没有 sparse 字段 "
                f"{sparse_field}。请 drop 旧集合后重新入库以创建 hybrid schema。"
            )
        if not include_sparse and collection_has_field(collection, sparse_field):
            # Dense-only ingest into a hybrid schema is allowed: sparse field can stay empty.
            # Milvus does not require sparse vectors on every row.
            pass

    collection.load()
    return collection


def ingest_chunks(chunk_files: list[Path], replace_docs: bool = False) -> dict[str, Any]:
    settings = load_runtime_settings()
    runtime = build_embedding_runtime(settings)
    doc2query_index = _load_doc2query_index()

    parsed_rows: list[dict[str, Any]] = []
    skipped_files: list[str] = []
    for chunk_file in chunk_files:
        parsed = _parse_markdown_frontmatter(chunk_file, doc2query_index=doc2query_index)
        if parsed is None:
            skipped_files.append(str(chunk_file))
            continue
        parsed_rows.append(parsed)

    if not parsed_rows:
        raise ValueError("未找到可入库的 chunk 文件（确认是带 frontmatter 的 Markdown）。")

    if replace_docs and skipped_files:
        raise ValueError(
            "replace 模式下存在解析失败文件，已中止以避免部分覆盖："
            + ", ".join(skipped_files)
        )

    # Build embedding plan: 1 row per chunk + N rows per synthetic question.
    # Each row tracks which text will be embedded and which source chunk it belongs to.
    ingest_plan: list[dict[str, Any]] = []
    for row in parsed_rows:
        ingest_plan.append(
            {
                "kind": "chunk",
                "question_id": "",
                "text": row["chunk_text"],
                "source_row": row,
            }
        )
        for q_index, question_text in enumerate(row.get("questions", []) or []):
            ingest_plan.append(
                {
                    "kind": "question",
                    "question_id": f"{row['chunk_id']}-q{q_index + 1:02d}",
                    "text": question_text,
                    "source_row": row,
                }
            )

    texts_to_encode = [item["text"] for item in ingest_plan]
    dense_vectors, sparse_vectors = _encode_documents(runtime, texts_to_encode)
    dense_dim = len(dense_vectors[0])
    include_sparse = runtime["mode"] == "hybrid" and sparse_vectors is not None
    collection = ensure_collection(settings, dense_dim, include_sparse=include_sparse)

    if replace_docs:
        doc_ids = sorted({row["doc_id"] for row in parsed_rows})
        if doc_ids:
            escaped = ", ".join(json.dumps(doc_id, ensure_ascii=False) for doc_id in doc_ids)
            expr = f"doc_id in [{escaped}]"
            collection.delete(expr=expr)

    dense_field = dense_field_from_env(settings)
    sparse_field = sparse_field_from_env(settings)
    text_field = text_field_from_env(settings)

    entities: list[dict[str, Any]] = []
    for idx, item in enumerate(ingest_plan):
        src = item["source_row"]
        entity: dict[str, Any] = {
            "kind": item["kind"],
            "doc_id": src["doc_id"],
            "chunk_id": src["chunk_id"],
            "question_id": item["question_id"],
            "title": src["title"],
            "section_path": src["section_path"],
            "source": src["source"],
            "url": src["url"],
            "summary": src["summary"],
            text_field: item["text"],
            dense_field: dense_vectors[idx],
            "keywords": src["keywords"],
            "source_file": src["source_file"],
        }
        # Optional fields via dynamic_field (schema has enable_dynamic_field=True).
        # Only write when non-empty to avoid polluting rows that never used them.
        if src.get("source_type"):
            entity["source_type"] = src["source_type"]
        if src.get("original_file"):
            entity["original_file"] = src["original_file"]
        if include_sparse:
            entity[sparse_field] = sparse_vectors[idx]
        entities.append(entity)

    insert_result = collection.insert(entities)
    collection.flush()

    inserted = getattr(insert_result, "insert_count", None)
    if inserted is None:
        inserted = len(entities)

    chunk_row_count = sum(1 for item in ingest_plan if item["kind"] == "chunk")
    question_row_count = sum(1 for item in ingest_plan if item["kind"] == "question")

    return {
        "collection": settings["milvus_collection"],
        "provider": runtime["provider"],
        "mode": runtime["mode"],
        "dense_dim": dense_dim,
        "sparse_enabled": include_sparse,
        "inserted": int(inserted),
        "chunk_rows": chunk_row_count,
        "question_rows": question_row_count,
        "doc_ids": sorted({row["doc_id"] for row in parsed_rows}),
        "chunk_files": [str(path) for path in chunk_files],
        "skipped_files": skipped_files,
    }


def dense_search(query: str, top_k: int) -> list[dict[str, Any]]:
    settings = load_runtime_settings()
    runtime = build_embedding_runtime(settings)
    collection = connect_collection(settings)
    dense_field = dense_field_from_env(settings)
    if not collection_has_field(collection, dense_field):
        raise ValueError(
            f"集合 {settings['milvus_collection']} 缺少字段 {dense_field}，无法执行 dense 检索。"
        )
    dense_vector, _ = _encode_query(runtime, query)
    output_fields = output_fields_from_env(settings)

    results = collection.search(
        data=[dense_vector],
        anns_field=dense_field,
        param={"metric_type": "IP", "params": {"nprobe": 10}},
        limit=top_k,
        output_fields=output_fields,
    )
    return format_search_results(results)


def hybrid_search(query: str, top_k: int) -> list[dict[str, Any]]:
    settings = load_runtime_settings()
    runtime = build_embedding_runtime(settings)
    if runtime["mode"] != "hybrid":
        raise ValueError("当前 provider 不支持 hybrid 模式，请改用 bge-m3。")

    collection = connect_collection(settings)
    dense_field = dense_field_from_env(settings)
    sparse_field = sparse_field_from_env(settings)
    if not collection_has_field(collection, dense_field) or not collection_has_field(collection, sparse_field):
        raise ValueError(
            "当前集合缺少 hybrid 所需字段（dense 或 sparse）。"
            "请用支持 hybrid 的 provider 重新建库并入库。"
        )
    dense_vector, sparse_vector = _encode_query(runtime, query)
    output_fields = output_fields_from_env(settings)

    requests = [
        AnnSearchRequest(
            data=[dense_vector],
            anns_field=dense_field,
            param={"metric_type": "IP", "params": {"nprobe": 10}},
            limit=top_k,
        ),
        AnnSearchRequest(
            data=[sparse_vector],
            anns_field=sparse_field,
            param={"metric_type": "IP", "params": {}},
            limit=top_k,
        ),
    ]

    results = collection.hybrid_search(
        reqs=requests,
        rerank=RRFRanker(60),
        limit=top_k,
        output_fields=output_fields,
    )
    return format_search_results(results)


def _search_one_query(
    collection: Collection,
    runtime: dict[str, Any],
    settings: dict[str, Any],
    query: str,
    top_k: int,
    output_fields: list[str],
) -> list[dict[str, Any]]:
    """Run a single query against the collection. Picks hybrid if available, else dense."""
    dense_field = dense_field_from_env(settings)
    sparse_field = sparse_field_from_env(settings)
    has_dense = collection_has_field(collection, dense_field)
    has_sparse = collection_has_field(collection, sparse_field)
    if not has_dense:
        raise ValueError(
            f"集合缺少 dense 字段 {dense_field}，无法执行多查询检索。"
        )

    dense_vector, sparse_vector = _encode_query(runtime, query)
    use_hybrid = runtime["mode"] == "hybrid" and has_sparse and sparse_vector is not None

    if use_hybrid:
        requests = [
            AnnSearchRequest(
                data=[dense_vector],
                anns_field=dense_field,
                param={"metric_type": "IP", "params": {"nprobe": 10}},
                limit=top_k,
            ),
            AnnSearchRequest(
                data=[sparse_vector],
                anns_field=sparse_field,
                param={"metric_type": "IP", "params": {}},
                limit=top_k,
            ),
        ]
        results = collection.hybrid_search(
            reqs=requests,
            rerank=RRFRanker(60),
            limit=top_k,
            output_fields=output_fields,
        )
    else:
        results = collection.search(
            data=[dense_vector],
            anns_field=dense_field,
            param={"metric_type": "IP", "params": {"nprobe": 10}},
            limit=top_k,
            output_fields=output_fields,
        )
    return format_search_results(results)


def _build_reranker(device: str = "cpu") -> Any:
    """构建 bge-reranker-v2-m3 cross-encoder 重排模型（软依赖）。

    FlagEmbedding 已在 requirements.txt 中，与 bge-m3 同家族。
    导入失败时返回 None，调用方跳过重排。
    """
    try:
        from FlagEmbedding import FlagReranker
        reranker = FlagReranker(
            "BAAI/bge-reranker-v2-m3",
            use_fp16=(device != "cpu"),
            device=device,
        )
        return reranker
    except Exception:
        return None


def rerank(
    query: str, candidates: list[dict[str, Any]], top_n: int | None = None,
) -> list[dict[str, Any]]:
    """对候选结果做 cross-encoder 重排序。

    对每个候选构造 (query, chunk_text) 对，由 reranker 打分后按分降序排列。
    chunk_text 取自 candidates 中的 'summary' 字段（比完整正文短，推理快）。
    若 summary 缺失则跳过该候选。
    reranker 不可用时原样返回 candidates。
    """
    from milvus_config import _resolve_device  # type: ignore
    device = _resolve_device(os.environ.get("KB_EMBEDDING_DEVICE", "auto"))
    reranker = _build_reranker(device)
    if reranker is None:
        return candidates

    pairs: list[tuple[str, str]] = []
    valid_indices: list[int] = []
    for idx, cand in enumerate(candidates):
        text = cand.get("summary", "")
        if not text:
            continue
        pairs.append((query, text))
        valid_indices.append(idx)

    if not pairs:
        return candidates

    scores = reranker.compute_score(pairs, normalize=True)
    if isinstance(scores, (int, float)):
        scores = [scores]

    for i, score in zip(valid_indices, scores):
        candidates[i]["rerank_score"] = round(float(score), 6)

    ranked = sorted(
        candidates,
        key=lambda c: c.get("rerank_score", -1.0),
        reverse=True,
    )
    if top_n is not None:
        ranked = ranked[:top_n]
    return ranked


def multi_query_search(
    queries: list[str], top_k_per_query: int, final_k: int,
    rrf_k: int = 60, use_rerank: bool = False,
) -> dict[str, Any]:
    """Run fan-out retrieval for multiple query rewrites, then RRF-merge and dedupe by chunk_id.

    Question rows (kind=question) share their parent chunk_id with chunk rows, so the
    dedupe step naturally collapses synthetic-question hits onto the owning chunk.
    The final result prefers the chunk-row payload for display, but keeps the
    aggregated RRF score and records which queries matched.
    """
    cleaned_queries = [q.strip() for q in queries if q and q.strip()]
    if not cleaned_queries:
        raise ValueError("multi-query-search 至少需要 1 个非空查询。")

    settings = load_runtime_settings()
    runtime = build_embedding_runtime(settings)
    collection = connect_collection(settings)
    output_fields = output_fields_from_env(settings)

    per_query_hits: list[list[dict[str, Any]]] = []
    for query in cleaned_queries:
        per_query_hits.append(
            _search_one_query(
                collection=collection,
                runtime=runtime,
                settings=settings,
                query=query,
                top_k=top_k_per_query,
                output_fields=output_fields,
            )
        )

    # RRF aggregation keyed by chunk_id (falling back to id when chunk_id missing).
    aggregated: dict[str, dict[str, Any]] = {}
    for query_index, hits in enumerate(per_query_hits):
        for rank, hit in enumerate(hits):
            group_key = hit.get("chunk_id") or str(hit.get("id", ""))
            if not group_key:
                continue
            contribution = 1.0 / (rrf_k + rank + 1)
            bucket = aggregated.setdefault(
                group_key,
                {
                    "chunk_hit": None,
                    "first_hit": hit,
                    "rrf_score": 0.0,
                    "matched_queries": set(),
                    "matched_kinds": set(),
                },
            )
            bucket["rrf_score"] += contribution
            bucket["matched_queries"].add(query_index)
            bucket["matched_kinds"].add(hit.get("kind", ""))
            # Prefer chunk-kind hit for display payload.
            if hit.get("kind") == "chunk" and bucket["chunk_hit"] is None:
                bucket["chunk_hit"] = hit

    ranked = sorted(
        aggregated.items(),
        key=lambda kv: kv[1]["rrf_score"],
        reverse=True,
    )[:final_k]

    final_results: list[dict[str, Any]] = []
    for group_key, bucket in ranked:
        display_hit = bucket["chunk_hit"] or bucket["first_hit"]
        final_results.append(
            {
                **display_hit,
                "group_key": group_key,
                "rrf_score": round(bucket["rrf_score"], 6),
                "matched_query_indexes": sorted(bucket["matched_queries"]),
                "matched_kinds": sorted(k for k in bucket["matched_kinds"] if k),
            }
        )

    # Cross-encoder 重排序（软依赖，可选）
    reranked = False
    if use_rerank and final_results:
        # 用 L0（第一条查询）作为重排 query；若多条查询则拼接
        rerank_query = cleaned_queries[0] if len(cleaned_queries) == 1 else " ".join(cleaned_queries[:2])
        final_results = rerank(rerank_query, final_results)
        reranked = any("rerank_score" in r for r in final_results)

    return {
        "queries": cleaned_queries,
        "top_k_per_query": top_k_per_query,
        "final_k": final_k,
        "rrf_k": rrf_k,
        "retrieval_mode": runtime["mode"],
        "reranked": reranked,
        "results": final_results,
    }


def text_search(query: str, top_k: int) -> list[dict[str, Any]]:
    settings = load_runtime_settings()
    collection = connect_collection(settings)
    sparse_field = sparse_field_from_env(settings)
    if not collection_has_field(collection, sparse_field):
        raise ValueError(
            f"集合 {settings['milvus_collection']} 缺少字段 {sparse_field}，"
            "当前仅支持 dense 检索。"
        )

    client = MilvusClient(
        uri=settings["milvus_uri"],
        token=settings["milvus_token"],
        db_name=settings["milvus_db"],
    )
    return client.search(
        collection_name=settings["milvus_collection"],
        data=[query],
        anns_field=sparse_field,
        limit=top_k,
        output_fields=output_fields_from_env(settings),
    )


def delete_by_doc_ids(
    doc_ids: list[str],
    confirm: bool,
) -> dict[str, Any]:
    """Delete every Milvus row whose ``doc_id`` is in ``doc_ids``.

    Used by lifecycle-agent's remove-doc workflow.  Refuses to run unless
    ``confirm=True`` so accidental invocations cannot wipe rows.

    Returns a per-doc breakdown so the caller can audit what was actually
    deleted; missing collection / no rows is reported, not raised.
    """
    if not doc_ids:
        raise ValueError("delete-by-doc-ids 需要至少一个 doc_id")
    if not confirm:
        raise ValueError(
            "delete-by-doc-ids 是破坏性操作。请显式加 --confirm 才会真正删除。"
        )
    settings = load_runtime_settings()
    connections.connect(
        alias="default",
        uri=settings["milvus_uri"],
        token=settings["milvus_token"],
        db_name=settings["milvus_db"],
    )
    collection_name = settings["milvus_collection"]
    if not utility.has_collection(collection_name):
        return {
            "milvus_uri": settings["milvus_uri"],
            "collection": collection_name,
            "collection_existed": False,
            "doc_ids": doc_ids,
            "rows_deleted": 0,
            "per_doc": [],
        }
    collection = Collection(collection_name)
    collection.load()

    per_doc: list[dict[str, Any]] = []
    total_deleted = 0
    for doc_id in doc_ids:
        # Count first so we can report exactly how many rows the doc had.
        expr = f'doc_id == "{doc_id}"'
        try:
            existing = collection.query(expr=expr, output_fields=["doc_id"], limit=16384)
            row_count = len(existing)
        except Exception as exc:
            per_doc.append({"doc_id": doc_id, "rows_deleted": 0, "error": str(exc)})
            continue
        if row_count == 0:
            per_doc.append({"doc_id": doc_id, "rows_deleted": 0, "note": "no rows in milvus"})
            continue
        collection.delete(expr=expr)
        per_doc.append({"doc_id": doc_id, "rows_deleted": row_count})
        total_deleted += row_count
    collection.flush()
    return {
        "milvus_uri": settings["milvus_uri"],
        "collection": collection_name,
        "collection_existed": True,
        "doc_ids": doc_ids,
        "rows_deleted": total_deleted,
        "per_doc": per_doc,
    }


def drop_collection(confirm: bool) -> dict[str, Any]:
    """Drop the configured Milvus collection. Used when switching embedding provider.

    Refuses to run unless ``confirm=True`` so accidental invocations cannot wipe data.
    """
    if not confirm:
        raise ValueError(
            "drop-collection 是破坏性操作。请显式加 --confirm 才会真正删除。"
        )
    settings = load_runtime_settings()
    connections.connect(
        alias="default",
        uri=settings["milvus_uri"],
        token=settings["milvus_token"],
        db_name=settings["milvus_db"],
    )
    collection_name = settings["milvus_collection"]
    existed = utility.has_collection(collection_name)
    if existed:
        utility.drop_collection(collection_name)
    return {
        "milvus_uri": settings["milvus_uri"],
        "milvus_db": settings["milvus_db"],
        "collection": collection_name,
        "existed_before": existed,
        "dropped": existed,
    }


def inspect_config() -> dict[str, Any]:
    settings = load_runtime_settings()
    return {
        "milvus_uri": settings["milvus_uri"],
        "milvus_db": settings["milvus_db"],
        "milvus_collection": collection_from_env(settings),
        "dense_field": dense_field_from_env(settings),
        "sparse_field": sparse_field_from_env(settings),
        "text_field": text_field_from_env(settings),
        "retrieval_mode": settings["retrieval_mode"],
        "embedding_provider": settings["embedding_provider"],
        "sentence_transformer_model": settings["sentence_transformer_model"],
        "bge_m3_model_path": settings["bge_m3_model_path"],
        "embedding_device": settings["embedding_device"],
        "requires_pymilvus_model_extra": True,
        "output_fields": output_fields_from_env(settings),
    }


def check_runtime(require_local_model: bool, smoke_test: bool) -> dict[str, Any]:
    settings = load_runtime_settings()
    result = check_embedding_runtime(
        settings=settings,
        require_local_model=require_local_model,
        smoke_test=smoke_test,
    )
    result["milvus_uri"] = settings["milvus_uri"]
    result["milvus_collection"] = settings["milvus_collection"]
    return result


def print_ingest_plan(chunk_file: Path) -> dict[str, Any]:
    settings = load_runtime_settings()
    runtime = build_embedding_runtime(settings)
    records = parse_chunk_file(chunk_file)
    if not records:
        raise ValueError(f"分块文件为空: {chunk_file}")

    plan = {
        "chunk_count": len(records),
        "collection": collection_from_env(settings),
        "provider": runtime["provider"],
        "mode": runtime["mode"],
        "dense_field": dense_field_from_env(settings),
        "sparse_field": sparse_field_from_env(settings),
        "text_field": text_field_from_env(settings),
        "required_chunk_keys": sorted(ChunkRecord.required_keys()),
    }
    return plan


def format_search_results(results: list[Any]) -> list[dict[str, Any]]:
    formatted: list[dict[str, Any]] = []
    for hits in results:
        for hit in hits:
            entity = getattr(hit, "entity", {})
            getter = entity.get if hasattr(entity, "get") else lambda *_: ""
            formatted.append(
                {
                    "id": getattr(hit, "id", ""),
                    "kind": getter("kind", ""),
                    "doc_id": getter("doc_id", ""),
                    "chunk_id": getter("chunk_id", ""),
                    "question_id": getter("question_id", ""),
                    "title": getter("title", ""),
                    "section_path": getter("section_path", ""),
                    "source": getter("source", ""),
                    "url": getter("url", ""),
                    "summary": getter("summary", ""),
                    # chunk_text 必须返回——QA 链路 answer/judge 节点用它做证据正文，
                    # 没有 chunk_text 时下游只能看到 metadata（title/url/summary），
                    # LLM 会判定"证据为空"。getter 对缺字段返回 ""，对存在字段返回正文。
                    "chunk_text": getter("chunk_text", ""),
                    "score": getattr(hit, "score", None),
                }
            )
    return formatted


# ---------------------------------------------------------------------------
# Knowledge base browsing (filesystem-only; works even when Milvus is down)
# ---------------------------------------------------------------------------

_RAW_DIR_DEFAULT = Path("data/docs/raw")
_CHUNKS_DIR_DEFAULT = Path("data/docs/chunks")
_DOC_ID_DATE_RE = re.compile(r"-(\d{4}-\d{2}-\d{2})$")


def _parse_iso_date(value: str) -> date | None:
    """Parse an ISO date string (YYYY-MM-DD). Returns None if unparseable."""
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _evidence_date(fetched_at: str, doc_id: str) -> date | None:
    """Resolve evidence date per P1-4 rule: fetched_at > doc_id tail date > None."""
    parsed = _parse_iso_date(fetched_at)
    if parsed is not None:
        return parsed
    return _parse_iso_date(_extract_doc_date(doc_id))


def _age_days(evidence_date: date | None, today: date | None = None) -> int | None:
    if evidence_date is None:
        return None
    today = today or date.today()
    return max(0, (today - evidence_date).days)


def _trust_tier(source_type: str, age_days: int | None) -> str:
    """Three-tier trust classification per qa-workflow step 8.1.1.

    Tier-1 (green): official-doc & age <= 90 days
    Tier-2 (yellow): extracted & age <= 180; or official-doc & 90 < age <= 180
    Tier-3 (orange): user-upload / unknown / age > 180 / age unknown
    """
    st = (source_type or "").strip().lower()
    if age_days is None or st in ("", "unknown"):
        return "tier-3"
    if st == "user-upload":
        return "tier-3"
    if age_days > 180:
        return "tier-3"
    if st == "official-doc":
        return "tier-1" if age_days <= 90 else "tier-2"
    if st == "extracted":
        return "tier-2" if age_days <= 180 else "tier-3"
    return "tier-3"


def _parse_raw_frontmatter(raw_file: Path) -> dict[str, Any]:
    """Parse a raw Markdown file's frontmatter. Unlike chunk files, raw docs
    may or may not have frontmatter; missing fields are not fatal."""
    metadata: dict[str, Any] = {}
    try:
        text = raw_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return metadata
    if not text.startswith("---"):
        return metadata
    parts = text.split("---", 2)
    if len(parts) < 3:
        return metadata
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    return metadata


def _extract_doc_date(doc_id: str) -> str:
    match = _DOC_ID_DATE_RE.search(doc_id)
    return match.group(1) if match else ""


def _scan_chunks_dir(chunks_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Group parsed chunk metadata by doc_id. Returns empty dict if dir missing."""
    by_doc: dict[str, list[dict[str, Any]]] = {}
    if not chunks_dir.exists():
        return by_doc
    for chunk_file in sorted(chunks_dir.glob("*.md")):
        parsed = _parse_markdown_frontmatter(chunk_file)
        if parsed is None:
            continue
        doc_id = parsed.get("doc_id", "")
        if not doc_id:
            continue
        by_doc.setdefault(doc_id, []).append(parsed)
    return by_doc


def list_docs(
    raw_dir: Path = _RAW_DIR_DEFAULT,
    chunks_dir: Path = _CHUNKS_DIR_DEFAULT,
) -> dict[str, Any]:
    """List every document in the KB with title / source_type / date / chunks
    count. Pure filesystem read; degrade-safe (no Milvus dependency)."""
    chunks_by_doc = _scan_chunks_dir(chunks_dir)
    raw_files = sorted(raw_dir.glob("*.md")) if raw_dir.exists() else []
    raw_doc_ids = {f.stem for f in raw_files}

    today = date.today()
    docs: list[dict[str, Any]] = []
    for raw_file in raw_files:
        doc_id = raw_file.stem
        meta = _parse_raw_frontmatter(raw_file)
        chunks = chunks_by_doc.get(doc_id, [])
        first_chunk = chunks[0] if chunks else {}
        source_type = meta.get("source_type", "") or first_chunk.get("source_type", "")
        fetched_at = meta.get("fetched_at", "") or first_chunk.get("fetched_at", "")
        ev_date = _evidence_date(fetched_at, doc_id)
        age = _age_days(ev_date, today=today)
        docs.append(
            {
                "doc_id": doc_id,
                "title": meta.get("title") or first_chunk.get("title", ""),
                "source_type": source_type,
                "source": meta.get("source", "") or first_chunk.get("source", ""),
                "doc_date": _extract_doc_date(doc_id),
                "fetched_at": fetched_at,
                "evidence_date": ev_date.isoformat() if ev_date else "",
                "age_days": age,
                "trust_tier": _trust_tier(source_type, age),
                "chunks_count": len(chunks),
                "raw_path": str(raw_file),
                "has_chunks": bool(chunks),
            }
        )

    # Orphan chunks: chunk files exist but raw file is missing (data inconsistency)
    for doc_id, chunks in chunks_by_doc.items():
        if doc_id in raw_doc_ids:
            continue
        first_chunk = chunks[0] if chunks else {}
        source_type = first_chunk.get("source_type", "")
        fetched_at = first_chunk.get("fetched_at", "")
        ev_date = _evidence_date(fetched_at, doc_id)
        age = _age_days(ev_date, today=today)
        docs.append(
            {
                "doc_id": doc_id,
                "title": first_chunk.get("title", ""),
                "source_type": source_type,
                "source": first_chunk.get("source", ""),
                "doc_date": _extract_doc_date(doc_id),
                "fetched_at": fetched_at,
                "evidence_date": ev_date.isoformat() if ev_date else "",
                "age_days": age,
                "trust_tier": _trust_tier(source_type, age),
                "chunks_count": len(chunks),
                "raw_path": None,
                "has_chunks": True,
                "orphan": "missing_raw",
            }
        )

    docs.sort(key=lambda d: (d.get("doc_date") or "", d["doc_id"]), reverse=True)
    return {
        "total_docs": len(docs),
        "total_chunks": sum(d["chunks_count"] for d in docs),
        "raw_dir": str(raw_dir),
        "chunks_dir": str(chunks_dir),
        "docs": docs,
    }


def show_doc(
    doc_id: str,
    raw_dir: Path = _RAW_DIR_DEFAULT,
    chunks_dir: Path = _CHUNKS_DIR_DEFAULT,
) -> dict[str, Any]:
    """Show a single document's frontmatter + its chunk list. Pure filesystem read."""
    raw_file = raw_dir / f"{doc_id}.md"
    raw_meta = _parse_raw_frontmatter(raw_file) if raw_file.exists() else {}
    chunks = _scan_chunks_dir(chunks_dir).get(doc_id, [])
    first_chunk = chunks[0] if chunks else {}

    source_type = raw_meta.get("source_type", "") or first_chunk.get("source_type", "")
    fetched_at = raw_meta.get("fetched_at", "") or first_chunk.get("fetched_at", "")
    ev_date = _evidence_date(fetched_at, doc_id)
    age = _age_days(ev_date)

    return {
        "doc_id": doc_id,
        "raw_path": str(raw_file) if raw_file.exists() else None,
        "raw_exists": raw_file.exists(),
        "raw_frontmatter": raw_meta,
        "doc_date": _extract_doc_date(doc_id),
        "source_type": source_type,
        "fetched_at": fetched_at,
        "evidence_date": ev_date.isoformat() if ev_date else "",
        "age_days": age,
        "trust_tier": _trust_tier(source_type, age),
        "chunks_count": len(chunks),
        "chunks": [
            {
                "chunk_id": chunk.get("chunk_id", ""),
                "title": chunk.get("title", ""),
                "section_path": chunk.get("section_path", ""),
                "source_file": chunk.get("source_file", ""),
                "fetched_at": chunk.get("fetched_at", ""),
                "questions_count": len(chunk.get("questions", [])),
            }
            for chunk in chunks
        ],
    }


def stats(
    raw_dir: Path = _RAW_DIR_DEFAULT,
    chunks_dir: Path = _CHUNKS_DIR_DEFAULT,
) -> dict[str, Any]:
    """High-level KB statistics: doc / chunk / question counts, source_type
    distribution, trust tier distribution, date range. Pure filesystem read."""
    overview = list_docs(raw_dir, chunks_dir)
    chunks_by_doc = _scan_chunks_dir(chunks_dir)

    source_type_counts: dict[str, int] = {}
    tier_counts: dict[str, int] = {"tier-1": 0, "tier-2": 0, "tier-3": 0}
    dates: list[str] = []
    total_questions = 0
    orphan_count = 0
    docs_without_chunks = 0
    docs_missing_fetched_at = 0

    for doc in overview["docs"]:
        st = doc.get("source_type") or "unknown"
        source_type_counts[st] = source_type_counts.get(st, 0) + 1
        tier = doc.get("trust_tier") or "tier-3"
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        if doc.get("doc_date"):
            dates.append(doc["doc_date"])
        if doc.get("orphan"):
            orphan_count += 1
        if not doc.get("has_chunks"):
            docs_without_chunks += 1
        if not doc.get("fetched_at"):
            docs_missing_fetched_at += 1
        for chunk in chunks_by_doc.get(doc["doc_id"], []):
            total_questions += len(chunk.get("questions", []))

    dates.sort()
    return {
        "total_docs": overview["total_docs"],
        "total_chunks": overview["total_chunks"],
        "total_questions": total_questions,
        "orphan_docs_missing_raw": orphan_count,
        "docs_without_chunks": docs_without_chunks,
        "docs_missing_fetched_at": docs_missing_fetched_at,
        "source_type_distribution": source_type_counts,
        "trust_tier_distribution": tier_counts,
        "earliest_doc_date": dates[0] if dates else None,
        "latest_doc_date": dates[-1] if dates else None,
        "raw_dir": str(raw_dir),
        "chunks_dir": str(chunks_dir),
    }


def stale_check(
    days: int = 90,
    raw_dir: Path = _RAW_DIR_DEFAULT,
    chunks_dir: Path = _CHUNKS_DIR_DEFAULT,
) -> dict[str, Any]:
    """Scan all documents and report those whose evidence is older than N days,
    or whose evidence date is unknown (missing fetched_at and no doc_id date tail).

    Output groups docs into three buckets:
      - ``stale``: age_days > ``days`` (default 90)
      - ``unknown_age``: no fetched_at + no doc_id tail date
      - ``fresh``: age_days <= ``days`` (returned only as a count)

    Pure filesystem read; works without Milvus."""
    overview = list_docs(raw_dir, chunks_dir)
    stale: list[dict[str, Any]] = []
    unknown_age: list[dict[str, Any]] = []
    fresh_count = 0

    for doc in overview["docs"]:
        age = doc.get("age_days")
        record = {
            "doc_id": doc["doc_id"],
            "title": doc.get("title", ""),
            "source_type": doc.get("source_type", ""),
            "evidence_date": doc.get("evidence_date", ""),
            "age_days": age,
            "trust_tier": doc.get("trust_tier", ""),
            "chunks_count": doc.get("chunks_count", 0),
        }
        if age is None:
            unknown_age.append(record)
        elif age > days:
            stale.append(record)
        else:
            fresh_count += 1

    stale.sort(key=lambda d: d.get("age_days") or 0, reverse=True)
    return {
        "threshold_days": days,
        "total_docs": overview["total_docs"],
        "fresh_count": fresh_count,
        "stale_count": len(stale),
        "unknown_age_count": len(unknown_age),
        "stale_docs": stale,
        "unknown_age_docs": unknown_age,
    }


# ---------------------------------------------------------------------------
# P2-1 Content-hash deduplication
# ---------------------------------------------------------------------------
#
# Design rationale: raw Markdown body (NOT including frontmatter) is hashed
# with SHA-256. Any field inside the frontmatter (fetched_at, source, etc.)
# can legitimately change between re-fetches of the same URL, but the body
# is what the user actually cares about. Stable across retries ⇒ dedupe
# works; sensitive to content change ⇒ a real update still gets ingested.
#
# Storage: the hash is stored IN the raw Markdown's frontmatter as the
# ``content_sha256`` field. No separate index file to keep in sync.
# ``list-docs`` / ``show-doc`` / ``backfill-hashes`` all read it from there.


def _split_raw_markdown(raw_file: Path) -> tuple[str, str]:
    """Split a raw Markdown file into (frontmatter_block_raw, body).

    ``frontmatter_block_raw`` is the text BETWEEN the two ``---`` fences
    (without the fence lines themselves). If the file has no frontmatter,
    returns ("", full_text).

    Body is everything after the closing fence, stripped of a single leading
    newline but otherwise preserved verbatim. Only the body is hashed."""
    try:
        text = raw_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", ""
    if not text.startswith("---"):
        return "", text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return "", text
    frontmatter_block = parts[1].strip("\n")
    body = parts[2]
    if body.startswith("\n"):
        body = body[1:]
    return frontmatter_block, body


def _compute_body_sha256(body: str) -> str:
    """SHA-256 of the body text, normalised to LF line endings and UTF-8 bytes.

    Normalisation steps:
    1. ``\\r\\n`` / ``\\r`` → ``\\n`` (guards against Windows/Unix line-ending churn)
    2. Strip leading/trailing ``\\n`` (the frontmatter-body separator and any
       trailing EOF newline are NOT considered part of the body's content)

    This keeps the hash stable across:
    - Fresh body straight from doc-converter (no frontmatter, no separator)
    - Raw file on disk with frontmatter prefix + blank-line separator + body + EOF \\n"""
    normalised = body.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _raw_content_sha256(raw_file: Path) -> str:
    """Compute the body SHA-256 for a raw Markdown file, ignoring frontmatter."""
    _, body = _split_raw_markdown(raw_file)
    return _compute_body_sha256(body)


def _frontmatter_field(frontmatter_block: str, field_name: str) -> str | None:
    """Extract a field value from an already-extracted frontmatter block.

    Naive line-based parser (consistent with ``_parse_raw_frontmatter``).
    Returns None if the field is missing or blank."""
    for line in frontmatter_block.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() == field_name:
            value = value.strip()
            return value or None
    return None


def _build_hash_index(raw_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Scan all raw Markdown files and group them by body SHA-256.

    Each value is a list of ``{doc_id, raw_path, declared_sha256, actual_sha256}``
    entries. ``declared_sha256`` is what the frontmatter says (if anything);
    ``actual_sha256`` is the freshly-recomputed value. They should match for
    any healthy doc; divergence indicates tampering or a stale frontmatter."""
    index: dict[str, list[dict[str, Any]]] = {}
    if not raw_dir.exists():
        return index
    for raw_file in sorted(raw_dir.glob("*.md")):
        frontmatter_block, body = _split_raw_markdown(raw_file)
        actual = _compute_body_sha256(body)
        declared = _frontmatter_field(frontmatter_block, "content_sha256")
        index.setdefault(actual, []).append(
            {
                "doc_id": raw_file.stem,
                "raw_path": str(raw_file),
                "declared_sha256": declared,
                "actual_sha256": actual,
                "has_frontmatter": bool(frontmatter_block),
            }
        )
    return index


def hash_lookup(
    sha256_hex: str,
    raw_dir: Path = _RAW_DIR_DEFAULT,
) -> dict[str, Any]:
    """Return every doc whose body SHA-256 matches the given hex digest.

    Called by get-info-workflow / upload-ingest BEFORE writing a new raw
    file, to decide whether the incoming content is already on disk."""
    normalised = sha256_hex.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalised):
        return {
            "status": "invalid_hash",
            "query_sha256": sha256_hex,
            "hint": "expected 64-hex-char SHA-256 digest",
        }
    index = _build_hash_index(raw_dir)
    matches = index.get(normalised, [])
    return {
        "status": "hit" if matches else "miss",
        "query_sha256": normalised,
        "match_count": len(matches),
        "matches": matches,
        "raw_dir": str(raw_dir),
    }


def find_duplicates(
    raw_dir: Path = _RAW_DIR_DEFAULT,
) -> dict[str, Any]:
    """Scan raw_dir and report every group of docs sharing the same body hash.

    Also reports ``hash_mismatch`` groups where the frontmatter's declared
    ``content_sha256`` disagrees with the body's actual hash — usually means
    the body was edited in-place without refreshing the hash field."""
    index = _build_hash_index(raw_dir)
    duplicate_groups: list[dict[str, Any]] = []
    hash_mismatches: list[dict[str, Any]] = []
    total_docs = 0

    for sha, entries in index.items():
        total_docs += len(entries)
        if len(entries) > 1:
            duplicate_groups.append(
                {
                    "content_sha256": sha,
                    "doc_count": len(entries),
                    "doc_ids": [e["doc_id"] for e in entries],
                    "raw_paths": [e["raw_path"] for e in entries],
                }
            )
        for entry in entries:
            declared = entry["declared_sha256"]
            if declared and declared.lower() != entry["actual_sha256"]:
                hash_mismatches.append(
                    {
                        "doc_id": entry["doc_id"],
                        "declared_sha256": declared,
                        "actual_sha256": entry["actual_sha256"],
                        "raw_path": entry["raw_path"],
                    }
                )

    duplicate_groups.sort(key=lambda g: g["doc_count"], reverse=True)
    return {
        "total_docs_scanned": total_docs,
        "unique_content_count": len(index),
        "duplicate_group_count": len(duplicate_groups),
        "duplicate_groups": duplicate_groups,
        "hash_mismatch_count": len(hash_mismatches),
        "hash_mismatches": hash_mismatches,
        "raw_dir": str(raw_dir),
    }


def _inject_content_sha256(raw_file: Path, sha256_hex: str) -> bool:
    """Insert or update the ``content_sha256`` field in a raw file's
    frontmatter. Returns True if the file was modified on disk.

    If the file has no frontmatter, the function refuses to add one
    (because that's the job of the ingest skill, not this helper) and
    returns False. Use atomic tmp+rename to avoid half-written files."""
    try:
        text = raw_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if not text.startswith("---"):
        return False
    parts = text.split("---", 2)
    if len(parts) < 3:
        return False

    frontmatter_block = parts[1].strip("\n")
    body_part = parts[2]

    new_lines: list[str] = []
    replaced = False
    for line in frontmatter_block.splitlines():
        if ":" in line:
            key, _ = line.split(":", 1)
            if key.strip() == "content_sha256":
                new_lines.append(f"content_sha256: {sha256_hex}")
                replaced = True
                continue
        new_lines.append(line)
    if not replaced:
        new_lines.append(f"content_sha256: {sha256_hex}")

    new_text = "---\n" + "\n".join(new_lines) + "\n---" + body_part
    if new_text == text:
        return False

    tmp_path = raw_file.with_suffix(raw_file.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(new_text)
        fh.flush()
        os.fsync(fh.fileno())
    tmp_path.replace(raw_file)
    return True


def backfill_hashes(
    raw_dir: Path = _RAW_DIR_DEFAULT,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Compute content_sha256 for every raw doc that's missing the field
    and write it back into the frontmatter.

    Docs without any frontmatter are left untouched (that's a pipeline
    concern, not a backfill concern). Docs with a stale ``content_sha256``
    that disagrees with the recomputed body hash are refreshed.

    Returns counts in each category so the operator knows what happened."""
    if not raw_dir.exists():
        return {
            "status": "raw_dir_missing",
            "raw_dir": str(raw_dir),
        }

    updated: list[str] = []
    already_ok: list[str] = []
    no_frontmatter: list[str] = []
    refreshed_mismatch: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    for raw_file in sorted(raw_dir.glob("*.md")):
        try:
            frontmatter_block, body = _split_raw_markdown(raw_file)
            if not frontmatter_block:
                no_frontmatter.append(raw_file.stem)
                continue
            actual = _compute_body_sha256(body)
            declared = _frontmatter_field(frontmatter_block, "content_sha256")
            if declared and declared.lower() == actual:
                already_ok.append(raw_file.stem)
                continue
            if declared and declared.lower() != actual:
                refreshed_mismatch.append(
                    {
                        "doc_id": raw_file.stem,
                        "declared_sha256": declared,
                        "actual_sha256": actual,
                    }
                )
            if not dry_run:
                _inject_content_sha256(raw_file, actual)
            updated.append(raw_file.stem)
        except OSError as exc:
            errors.append({"doc_id": raw_file.stem, "error": str(exc)})

    return {
        "status": "ok" if not errors else "partial_errors",
        "dry_run": dry_run,
        "raw_dir": str(raw_dir),
        "updated_count": len(updated),
        "already_ok_count": len(already_ok),
        "no_frontmatter_count": len(no_frontmatter),
        "refreshed_mismatch_count": len(refreshed_mismatch),
        "error_count": len(errors),
        "updated": updated,
        "already_ok": already_ok,
        "no_frontmatter": no_frontmatter,
        "refreshed_mismatch": refreshed_mismatch,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Knowledge Base Milvus CLI")
    subparsers = parser.add_subparsers(dest="command")

    dense_parser = subparsers.add_parser("dense-search", help="执行 dense 向量检索")
    dense_parser.add_argument("query", help="查询文本")
    dense_parser.add_argument("--top-k", type=int, default=10)

    hybrid_parser = subparsers.add_parser("hybrid-search", help="执行 dense+sparse 混合检索")
    hybrid_parser.add_argument("query", help="查询文本")
    hybrid_parser.add_argument("--top-k", type=int, default=10)

    text_parser = subparsers.add_parser("text-search", help="执行 BM25 / sparse 文本检索")
    text_parser.add_argument("query", help="查询文本")
    text_parser.add_argument("--top-k", type=int, default=10)

    inspect_parser = subparsers.add_parser("inspect-config", help="打印当前 Milvus 与 provider 配置")
    inspect_parser.set_defaults(command="inspect-config")

    runtime_parser = subparsers.add_parser(
        "check-runtime",
        help="检查 embedding runtime 是否可用（可选 smoke test）",
    )
    runtime_parser.add_argument(
        "--require-local-model",
        action="store_true",
        help="要求 provider 必须是本地向量模型（sentence-transformer/default/bge-m3）",
    )
    runtime_parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="执行一次最小向量化以验证可用性",
    )

    ingest_parser = subparsers.add_parser("plan-ingest", help="打印分块入库计划，不执行写入")
    ingest_parser.add_argument("chunk_file", type=Path, help="chunk JSON 或 JSONL 文件")

    chunk_ingest_parser = subparsers.add_parser(
        "ingest-chunks",
        help="将 Markdown chunk 文件向量化并写入 Milvus",
    )
    chunk_ingest_parser.add_argument(
        "--chunk-pattern",
        default="data/docs/chunks/*.md",
        help="chunk 文件 glob（默认: data/docs/chunks/*.md）",
    )
    chunk_ingest_parser.add_argument(
        "--chunk-files",
        nargs="*",
        default=[],
        help="指定 chunk 文件列表（优先于 --chunk-pattern）",
    )
    ingest_mode_group = chunk_ingest_parser.add_mutually_exclusive_group()
    ingest_mode_group.add_argument(
        "--append",
        action="store_true",
        help="只追加不删除旧记录（默认行为）",
    )
    ingest_mode_group.add_argument(
        "--replace-docs",
        action="store_true",
        help="按 doc_id 覆盖重写（先删后写，谨慎使用）",
    )

    drop_parser = subparsers.add_parser(
        "drop-collection",
        help="(危险) 删除当前 KB_MILVUS_COLLECTION 指定的集合，用于 provider 切换后重建 schema",
    )
    drop_parser.add_argument(
        "--confirm",
        action="store_true",
        help="必须显式加上才会真正删除；无该参数时只会报错，不删数据",
    )

    delete_docs_parser = subparsers.add_parser(
        "delete-by-doc-ids",
        help="(危险) 按 doc_id 删除 Milvus 行；供 lifecycle-agent 的 remove-doc 流程调用",
    )
    delete_docs_parser.add_argument(
        "--doc-id",
        action="append",
        dest="doc_ids",
        required=True,
        help="要删除的 doc_id；可重复多次",
    )
    delete_docs_parser.add_argument(
        "--confirm",
        action="store_true",
        help="必须显式加上才会真正删除；无该参数时只会报错，不删数据",
    )

    multi_parser = subparsers.add_parser(
        "multi-query-search",
        help="对多条查询并发执行检索，按 RRF 合并并按 chunk_id 去重",
    )
    multi_parser.add_argument(
        "--query",
        action="append",
        dest="queries",
        default=[],
        help="一条查询文本；可重复多次指定（L0 原句 / L1 规范化 / L2 意图增强 / L3 HyDE 等）",
    )
    multi_parser.add_argument(
        "--top-k-per-query",
        type=int,
        default=20,
        help="每条查询从 Milvus 取回的候选数（默认 20）",
    )
    multi_parser.add_argument(
        "--final-k",
        type=int,
        default=10,
        help="RRF 合并 + 按 chunk_id 去重后返回的条数（默认 10）",
    )
    multi_parser.add_argument(
        "--rrf-k",
        type=int,
        default=60,
        help="RRF 常数 k（默认 60，与 Milvus RRFRanker 对齐）",
    )
    multi_parser.add_argument(
        "--rerank",
        action="store_true",
        help="RRF 合并后用 bge-reranker-v2-m3 cross-encoder 重排序（软依赖，模型不可用时静默跳过）",
    )

    list_docs_parser = subparsers.add_parser(
        "list-docs",
        help="列出知识库所有文档（doc_id / title / source_type / date / chunks 数；纯文件系统读，不依赖 Milvus）",
    )
    list_docs_parser.add_argument(
        "--raw-dir",
        type=Path,
        default=_RAW_DIR_DEFAULT,
        help=f"raw 文档目录（默认: {_RAW_DIR_DEFAULT}）",
    )
    list_docs_parser.add_argument(
        "--chunks-dir",
        type=Path,
        default=_CHUNKS_DIR_DEFAULT,
        help=f"chunk 文档目录（默认: {_CHUNKS_DIR_DEFAULT}）",
    )

    show_doc_parser = subparsers.add_parser(
        "show-doc",
        help="显示单个文档的 frontmatter + 所有 chunks 概览（纯文件系统读）",
    )
    show_doc_parser.add_argument("doc_id", help="要查看的 doc_id")
    show_doc_parser.add_argument("--raw-dir", type=Path, default=_RAW_DIR_DEFAULT)
    show_doc_parser.add_argument("--chunks-dir", type=Path, default=_CHUNKS_DIR_DEFAULT)

    stats_parser = subparsers.add_parser(
        "stats",
        help="知识库概览统计（总 docs / chunks / questions / source_type / trust_tier 分布 / 日期范围；纯文件系统读）",
    )
    stats_parser.add_argument("--raw-dir", type=Path, default=_RAW_DIR_DEFAULT)
    stats_parser.add_argument("--chunks-dir", type=Path, default=_CHUNKS_DIR_DEFAULT)

    stale_parser = subparsers.add_parser(
        "stale-check",
        help="列出证据年龄超过阈值（默认 90 天）的文档，以及 evidence date 未知的文档；qa-workflow 时效性分级的辅助工具",
    )
    stale_parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="年龄阈值（默认 90 天；P1-4 规则下 >90 天出警告，>180 天建议刷新）",
    )
    stale_parser.add_argument("--raw-dir", type=Path, default=_RAW_DIR_DEFAULT)
    stale_parser.add_argument("--chunks-dir", type=Path, default=_CHUNKS_DIR_DEFAULT)

    hash_lookup_parser = subparsers.add_parser(
        "hash-lookup",
        help="按 body SHA-256 查找已有文档（P2-1 去重：入库前检查内容是否已在库）",
    )
    hash_lookup_parser.add_argument("sha256", help="64 位十六进制 SHA-256 digest")
    hash_lookup_parser.add_argument("--raw-dir", type=Path, default=_RAW_DIR_DEFAULT)

    find_dupes_parser = subparsers.add_parser(
        "find-duplicates",
        help="扫描所有 raw 文档按 body SHA-256 聚合，列出重复组与 hash 漂移（P2-1）",
    )
    find_dupes_parser.add_argument("--raw-dir", type=Path, default=_RAW_DIR_DEFAULT)

    backfill_parser = subparsers.add_parser(
        "backfill-hashes",
        help="为缺失 content_sha256 的 raw 文档补 frontmatter 字段（历史数据迁移；P2-1）",
    )
    backfill_parser.add_argument("--raw-dir", type=Path, default=_RAW_DIR_DEFAULT)
    backfill_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只报告改动计划，不写入文件",
    )

    parser.add_argument("--version", action="store_true", help="显示版本")
    args = parser.parse_args()

    if args.version:
        print("milvus-cli v2.0.0")
        return

    if args.command == "dense-search":
        print(json.dumps(dense_search(args.query, args.top_k), ensure_ascii=False, indent=2))
        return

    if args.command == "hybrid-search":
        print(json.dumps(hybrid_search(args.query, args.top_k), ensure_ascii=False, indent=2))
        return

    if args.command == "text-search":
        print(json.dumps(text_search(args.query, args.top_k), ensure_ascii=False, indent=2))
        return

    if args.command == "inspect-config":
        print(json.dumps(inspect_config(), ensure_ascii=False, indent=2))
        return

    if args.command == "check-runtime":
        print(
            json.dumps(
                check_runtime(args.require_local_model, args.smoke_test),
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "plan-ingest":
        print(json.dumps(print_ingest_plan(args.chunk_file), ensure_ascii=False, indent=2))
        return

    if args.command == "ingest-chunks":
        if args.chunk_files:
            chunk_files = [Path(path) for path in args.chunk_files]
        else:
            chunk_files = sorted(Path().glob(args.chunk_pattern))

        replace_docs = bool(args.replace_docs)
        result = ingest_chunks(chunk_files=chunk_files, replace_docs=replace_docs)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "drop-collection":
        result = drop_collection(confirm=bool(args.confirm))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "delete-by-doc-ids":
        result = delete_by_doc_ids(
            doc_ids=args.doc_ids,
            confirm=bool(args.confirm),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "multi-query-search":
        result = multi_query_search(
            queries=args.queries,
            top_k_per_query=args.top_k_per_query,
            final_k=args.final_k,
            rrf_k=args.rrf_k,
            use_rerank=args.rerank,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "list-docs":
        print(
            json.dumps(
                list_docs(raw_dir=args.raw_dir, chunks_dir=args.chunks_dir),
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "show-doc":
        print(
            json.dumps(
                show_doc(args.doc_id, raw_dir=args.raw_dir, chunks_dir=args.chunks_dir),
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "stats":
        print(
            json.dumps(
                stats(raw_dir=args.raw_dir, chunks_dir=args.chunks_dir),
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "stale-check":
        print(
            json.dumps(
                stale_check(days=args.days, raw_dir=args.raw_dir, chunks_dir=args.chunks_dir),
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "hash-lookup":
        print(
            json.dumps(
                hash_lookup(args.sha256, raw_dir=args.raw_dir),
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "find-duplicates":
        print(
            json.dumps(
                find_duplicates(raw_dir=args.raw_dir),
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "backfill-hashes":
        print(
            json.dumps(
                backfill_hashes(raw_dir=args.raw_dir, dry_run=args.dry_run),
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    parser.print_help()


if __name__ == "__main__":
    main()
