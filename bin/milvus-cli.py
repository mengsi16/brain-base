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

    return {
        "doc_id": metadata.get("doc_id", ""),
        "chunk_id": metadata.get("chunk_id", ""),
        "title": title,
        "section_path": section_path,
        "source": metadata.get("source", ""),
        "url": metadata.get("url", ""),
        "summary": summary,
        "keywords": keywords,
        "chunk_text": content,
        "source_file": str(chunk_file),
    }


def _encode_documents(runtime: dict[str, Any], texts: list[str]) -> list[list[float]]:
    encoder = runtime["encoder"]
    if hasattr(encoder, "encode_documents"):
        embeddings = encoder.encode_documents(texts)
    else:
        embeddings = encoder.encode_queries(texts)

    if runtime["mode"] == "hybrid":
        dense_embeddings = embeddings["dense"]
    else:
        dense_embeddings = embeddings

    vectors: list[list[float]] = []
    for embedding in dense_embeddings:
        if hasattr(embedding, "tolist"):
            vectors.append(embedding.tolist())
        else:
            vectors.append(list(embedding))
    return vectors


def _get_dense_field_dim(collection: Collection, dense_field: str) -> int | None:
    for field in collection.schema.fields:
        if field.name == dense_field:
            dim = field.params.get("dim") if field.params else None
            return int(dim) if dim is not None else None
    return None


def ensure_dense_collection(settings: dict[str, Any], dense_dim: int) -> Collection:
    connections.connect(
        alias="default",
        uri=settings["milvus_uri"],
        token=settings["milvus_token"],
        db_name=settings["milvus_db"],
    )

    collection_name = settings["milvus_collection"]
    dense_field = dense_field_from_env(settings)
    text_field = text_field_from_env(settings)

    if not utility.has_collection(collection_name):
        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="title", dtype=DataType.VARCHAR, max_length=1024),
            FieldSchema(name="section_path", dtype=DataType.VARCHAR, max_length=2048),
            FieldSchema(name="source", dtype=DataType.VARCHAR, max_length=1024),
            FieldSchema(name="url", dtype=DataType.VARCHAR, max_length=2048),
            FieldSchema(name="summary", dtype=DataType.VARCHAR, max_length=8192),
            FieldSchema(name=text_field, dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name=dense_field, dtype=DataType.FLOAT_VECTOR, dim=dense_dim),
        ]
        schema = CollectionSchema(
            fields=fields,
            description="knowledge-base chunk embeddings",
            enable_dynamic_field=True,
        )
        collection = Collection(name=collection_name, schema=schema)
        collection.create_index(
            field_name=dense_field,
            index_params={"index_type": "AUTOINDEX", "metric_type": "IP", "params": {}},
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
                f"集合 {collection_name} 的 dense dim={existing_dim}，当前模型 dim={dense_dim}，不匹配。"
            )

    collection.load()
    return collection


def ingest_chunks(chunk_files: list[Path], replace_docs: bool = False) -> dict[str, Any]:
    settings = load_runtime_settings()
    runtime = build_embedding_runtime(settings)

    if runtime["mode"] == "hybrid":
        raise ValueError(
            "ingest-chunks 当前仅支持 dense provider。"
            "如需 hybrid，请使用官方 Milvus MCP 流程或改用 sentence-transformer。"
        )

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

    vectors = _encode_documents(runtime, [row["chunk_text"] for row in parsed_rows])
    dense_dim = len(vectors[0])
    collection = ensure_dense_collection(settings, dense_dim)

    if replace_docs:
        doc_ids = sorted({row["doc_id"] for row in parsed_rows})
        if doc_ids:
            escaped = ", ".join(json.dumps(doc_id, ensure_ascii=False) for doc_id in doc_ids)
            expr = f"doc_id in [{escaped}]"
            collection.delete(expr=expr)

    dense_field = dense_field_from_env(settings)
    text_field = text_field_from_env(settings)
    entities: list[dict[str, Any]] = []
    for row, vector in zip(parsed_rows, vectors):
        entities.append(
            {
                "doc_id": row["doc_id"],
                "chunk_id": row["chunk_id"],
                "title": row["title"],
                "section_path": row["section_path"],
                "source": row["source"],
                "url": row["url"],
                "summary": row["summary"],
                text_field: row["chunk_text"],
                dense_field: vector,
                "keywords": row["keywords"],
                "source_file": row["source_file"],
            }
        )

    insert_result = collection.insert(entities)
    collection.flush()

    inserted = getattr(insert_result, "insert_count", None)
    if inserted is None:
        inserted = len(entities)

    return {
        "collection": settings["milvus_collection"],
        "provider": runtime["provider"],
        "mode": runtime["mode"],
        "dense_dim": dense_dim,
        "inserted": int(inserted),
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
    query_embedding = runtime["encoder"].encode_queries([query])
    if runtime["mode"] == "hybrid":
        query_vector = query_embedding["dense"][0]
    else:
        query_vector = query_embedding[0]

    if hasattr(query_vector, "tolist"):
        query_vector = query_vector.tolist()
    else:
        query_vector = list(query_vector)

    output_fields = output_fields_from_env(settings)

    results = collection.search(
        data=[query_vector],
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
    query_embedding = runtime["encoder"].encode_queries([query])
    dense_vector = query_embedding["dense"][0].tolist()
    sparse_vector = query_embedding["sparse"][0]
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
                    "doc_id": getter("doc_id", ""),
                    "chunk_id": getter("chunk_id", ""),
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

    parser.print_help()


if __name__ == "__main__":
    main()
