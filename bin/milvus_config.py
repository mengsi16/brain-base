#!/usr/bin/env python3
"""
Milvus runtime configuration helpers.

这里不做任何伪造向量化。
未配置 provider 时直接 fail-fast。
"""

import json
import os
from importlib.util import find_spec
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LOCAL_EMBEDDING_PROVIDERS = {"default", "sentence-transformer", "bge-m3"}


@dataclass(frozen=True)
class ChunkRecord:
    doc_id: str
    chunk_id: str
    title: str
    source: str
    url: str
    summary: str
    content: str

    @staticmethod
    def required_keys() -> set[str]:
        return {"doc_id", "chunk_id", "title", "source", "url", "summary", "content"}


def load_runtime_settings() -> dict[str, Any]:
    provider = os.environ.get("KB_EMBEDDING_PROVIDER", "bge-m3").strip().lower()
    retrieval_mode = os.environ.get("KB_RETRIEVAL_MODE", "").strip().lower()
    if not retrieval_mode:
        retrieval_mode = "hybrid" if provider == "bge-m3" else "dense"

    return {
        "milvus_uri": os.environ.get("KB_MILVUS_URI", "http://localhost:19530"),
        "milvus_token": os.environ.get("KB_MILVUS_TOKEN", ""),
        "milvus_db": os.environ.get("KB_MILVUS_DB", "default"),
        "milvus_collection": os.environ.get("KB_MILVUS_COLLECTION", "knowledge_base"),
        "dense_field": os.environ.get("KB_MILVUS_DENSE_FIELD", "dense_vector"),
        "sparse_field": os.environ.get("KB_MILVUS_SPARSE_FIELD", "sparse_vector"),
        "text_field": os.environ.get("KB_MILVUS_TEXT_FIELD", "chunk_text"),
        "output_fields": os.environ.get(
            "KB_MILVUS_OUTPUT_FIELDS",
            "kind,doc_id,chunk_id,question_id,title,section_path,source,url,summary",
        ),
        "embedding_provider": provider,
        "retrieval_mode": retrieval_mode,
        "openai_model": os.environ.get("KB_OPENAI_EMBEDDING_MODEL", "text-embedding-3-large"),
        "openai_dimensions": int(os.environ.get("KB_OPENAI_EMBEDDING_DIMENSIONS", "3072")),
        "sentence_transformer_model": os.environ.get(
            "KB_SENTENCE_TRANSFORMER_MODEL",
            "all-MiniLM-L6-v2",
        ),
        "embedding_device": os.environ.get("KB_EMBEDDING_DEVICE", "cpu"),
        "bge_m3_model_path": os.environ.get("KB_BGEM3_MODEL_PATH", "BAAI/bge-m3"),
        "openai_api_key": os.environ.get("OPENAI_API_KEY", ""),
    }


def collection_from_env(settings: dict[str, Any]) -> str:
    return settings["milvus_collection"]


def dense_field_from_env(settings: dict[str, Any]) -> str:
    return settings["dense_field"]


def sparse_field_from_env(settings: dict[str, Any]) -> str:
    return settings["sparse_field"]


def text_field_from_env(settings: dict[str, Any]) -> str:
    return settings["text_field"]


def output_fields_from_env(settings: dict[str, Any]) -> list[str]:
    return [field.strip() for field in settings["output_fields"].split(",") if field.strip()]


def local_embedding_model_from_settings(settings: dict[str, Any]) -> str:
    provider = settings["embedding_provider"]
    if provider == "sentence-transformer":
        return settings["sentence_transformer_model"]
    if provider == "bge-m3":
        return settings["bge_m3_model_path"]
    if provider == "default":
        return "pymilvus.default"
    return ""


def _ensure_hf_endpoint() -> str:
    """Auto-detect HuggingFace connectivity and set HF_ENDPOINT if needed.

    Priority:
    1. If HF_ENDPOINT is already set (by user or env), respect it.
    2. If HF_HUB_OFFLINE=1, skip check (offline mode).
    3. Try connecting to huggingface.co; if unreachable, auto-switch to mirror.

    Returns the resolved endpoint URL.
    """
    # User explicitly set → respect
    explicit = os.environ.get("HF_ENDPOINT", "").strip()
    if explicit:
        return explicit

    # Offline mode → no download needed
    if os.environ.get("HF_HUB_OFFLINE", "").strip() == "1":
        return ""

    # Try primary endpoint
    import urllib.request
    import urllib.error
    primary = "https://huggingface.co"
    mirrors = [
        "https://hf-mirror.com",
        "https://huggingface.do.mirr.one",
    ]
    timeout = 5

    try:
        req = urllib.request.Request(primary, method="HEAD")
        urllib.request.urlopen(req, timeout=timeout)
        return primary
    except (urllib.error.URLError, OSError, TimeoutError):
        pass

    # Primary failed → try mirrors
    for mirror in mirrors:
        try:
            req = urllib.request.Request(mirror, method="HEAD")
            urllib.request.urlopen(req, timeout=timeout)
            os.environ["HF_ENDPOINT"] = mirror
            import sys as _sys
            print(
                f"  [HF] huggingface.co 不可达，自动切换镜像：{mirror}",
                file=_sys.stderr,
            )
            return mirror
        except (urllib.error.URLError, OSError, TimeoutError):
            continue

    # All failed → warn and try anyway (might work with partial connectivity)
    import sys as _sys
    print(
        "  [HF] 警告：huggingface.co 和镜像站均不可达，模型下载可能失败。"
        "\n  建议手动设置 HF_ENDPOINT 环境变量，或设置 HF_HUB_OFFLINE=1 使用本地缓存。",
        file=_sys.stderr,
    )
    return ""


def _force_offline_if_cached(model_name: str) -> None:
    """If the model is already cached locally, force HF_HUB_OFFLINE=1.

    This prevents transformers' ``is_base_mistral`` from making a network
    call to the HF API even when the model files are all on disk.
    """
    if os.environ.get("HF_HUB_OFFLINE", "").strip() == "1":
        return  # already offline
    cache_dir = Path(os.environ.get(
        "HF_CACHE_DIR",
        Path.home() / ".cache" / "huggingface" / "hub",
    ))
    # Normalize model name: BAAI/bge-m3 → models--BAAI--bge-m3
    cache_subdir = "models--" + model_name.replace("/", "--")
    if (cache_dir / cache_subdir).is_dir():
        os.environ["HF_HUB_OFFLINE"] = "1"


def build_embedding_runtime(settings: dict[str, Any]) -> dict[str, Any]:
    if find_spec("pymilvus.model") is None:
        raise ValueError(
            "当前环境缺少 pymilvus 的 embedding model 扩展。请安装 `pymilvus[model]`。"
        )

    # If model is cached, force offline to avoid transformers' is_base_mistral bug
    provider = settings["embedding_provider"]
    if provider == "bge-m3":
        _force_offline_if_cached(settings["bge_m3_model_path"])
    elif provider == "sentence-transformer":
        _force_offline_if_cached(settings["sentence_transformer_model"])

    # If not offline (model not cached), auto-detect HF mirror
    if not os.environ.get("HF_HUB_OFFLINE"):
        _ensure_hf_endpoint()

    from pymilvus import model

    provider = settings["embedding_provider"]
    if provider == "default":
        encoder = model.DefaultEmbeddingFunction()
        return {"provider": provider, "mode": settings["retrieval_mode"], "encoder": encoder}

    if provider == "sentence-transformer":
        encoder = model.dense.SentenceTransformerEmbeddingFunction(
            model_name=settings["sentence_transformer_model"],
            device=settings["embedding_device"],
        )
        return {"provider": provider, "mode": "dense", "encoder": encoder}

    if provider == "openai":
        api_key = settings["openai_api_key"]
        if not api_key:
            raise ValueError("使用 openai embedding provider 时必须设置 OPENAI_API_KEY。")
        encoder = model.dense.OpenAIEmbeddingFunction(
            model_name=settings["openai_model"],
            api_key=api_key,
            dimensions=settings["openai_dimensions"],
        )
        return {"provider": provider, "mode": "dense", "encoder": encoder}

    if provider == "bge-m3":
        encoder = model.hybrid.BGEM3EmbeddingFunction(
            model_name=settings["bge_m3_model_path"],
            device=settings["embedding_device"],
        )
        return {"provider": provider, "mode": "hybrid", "encoder": encoder}

    raise ValueError(
        "不支持的 embedding provider。支持值: default, sentence-transformer, openai, bge-m3"
    )


def check_embedding_runtime(
    settings: dict[str, Any] | None = None,
    require_local_model: bool = False,
    smoke_test: bool = False,
) -> dict[str, Any]:
    settings = settings or load_runtime_settings()
    provider = settings["embedding_provider"]
    is_local_provider = provider in LOCAL_EMBEDDING_PROVIDERS

    if require_local_model and not is_local_provider:
        raise ValueError(
            "当前 provider 不是本地向量模型。请设置 KB_EMBEDDING_PROVIDER=sentence-transformer 或 bge-m3。"
        )

    runtime = build_embedding_runtime(settings)
    report: dict[str, Any] = {
        "provider": provider,
        "resolved_mode": runtime["mode"],
        "is_local_provider": is_local_provider,
        "local_model": local_embedding_model_from_settings(settings),
        "embedding_device": settings["embedding_device"],
        "smoke_test": smoke_test,
        "can_vectorize": False,
    }

    if smoke_test:
        probe_text = ["vectorization health check"]
        if runtime["mode"] == "hybrid":
            embedding = runtime["encoder"].encode_queries(probe_text)
            dense_vector = embedding["dense"][0]
            sparse_vector = embedding["sparse"][0]
            report["dense_dim"] = len(dense_vector)

            if hasattr(sparse_vector, "nnz"):
                report["sparse_nnz"] = int(sparse_vector.nnz)
            elif hasattr(sparse_vector, "indices"):
                report["sparse_nnz"] = len(sparse_vector.indices)
            else:
                report["sparse_nnz"] = None
        else:
            dense_vector = runtime["encoder"].encode_queries(probe_text)[0]
            report["dense_dim"] = len(dense_vector)

    report["can_vectorize"] = True
    return report


def parse_chunk_file(chunk_file: Path) -> list[ChunkRecord]:
    text = chunk_file.read_text(encoding="utf-8").strip()
    if not text:
        return []

    records: list[dict[str, Any]]
    if text.startswith("["):
        records = json.loads(text)
    else:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]

    normalized: list[ChunkRecord] = []
    for record in records:
        missing = ChunkRecord.required_keys() - record.keys()
        if missing:
            raise ValueError(f"{chunk_file} 存在缺失字段: {sorted(missing)}")
        normalized.append(
            ChunkRecord(
                doc_id=record["doc_id"],
                chunk_id=record["chunk_id"],
                title=record["title"],
                source=record["source"],
                url=record["url"],
                summary=record["summary"],
                content=record["content"],
            )
        )
    return normalized
