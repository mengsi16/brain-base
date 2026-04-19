#!/usr/bin/env python3
"""
Milvus CLI for knowledge-base.

目标：
1. 去掉伪造的 hash 向量化。
2. 显式区分 dense / hybrid 检索。
3. 通过可配置的 embedding provider 接入真实向量化能力。
"""

import argparse
import json
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


def _parse_markdown_frontmatter(chunk_file: Path) -> dict[str, Any] | None:
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

    section_path = metadata.get("section_path", "")
    if isinstance(section_path, list):
        section_path = " / ".join(str(item) for item in section_path)

    title = metadata.get("title") or _first_heading(content)
    summary = metadata.get("summary") or _first_paragraph(content)
    keywords = metadata.get("keywords", "")
    questions = _parse_questions_value(metadata.get("questions", ""))

    return {
        "doc_id": metadata.get("doc_id", ""),
        "chunk_id": metadata.get("chunk_id", ""),
        "title": title,
        "section_path": section_path,
        "source": metadata.get("source", ""),
        "source_type": metadata.get("source_type", ""),
        "url": metadata.get("url", ""),
        "original_file": metadata.get("original_file", ""),
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

    parsed_rows: list[dict[str, Any]] = []
    skipped_files: list[str] = []
    for chunk_file in chunk_files:
        parsed = _parse_markdown_frontmatter(chunk_file)
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


def multi_query_search(
    queries: list[str], top_k_per_query: int, final_k: int, rrf_k: int = 60
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

    return {
        "queries": cleaned_queries,
        "top_k_per_query": top_k_per_query,
        "final_k": final_k,
        "rrf_k": rrf_k,
        "retrieval_mode": runtime["mode"],
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
                    "score": getattr(hit, "score", None),
                }
            )
    return formatted


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

    if args.command == "multi-query-search":
        result = multi_query_search(
            queries=args.queries,
            top_k_per_query=args.top_k_per_query,
            final_k=args.final_k,
            rrf_k=args.rrf_k,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
