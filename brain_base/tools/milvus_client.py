"""
milvus 客户端：动态加载 bin/milvus-cli.py，把它的顶层函数包装为可
import 的接口。

为什么不直接 import：bin/milvus-cli.py 文件名带 `-`，无法用标准
import 语法。改名会破坏外部 180+ 处文档/脚本引用，因此在 brain_base
内部用 importlib 动态加载，外部 CLI 行为零破坏。

惰性加载：首次调用任何函数才加载 bin/milvus-cli.py（含 pymilvus
等重型依赖），随后通过 lru_cache 复用。
"""

from __future__ import annotations

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

_BIN_DIR = Path(__file__).resolve().parents[2] / "bin"
_CLI_PATH = _BIN_DIR / "milvus-cli.py"


@lru_cache(maxsize=1)
def _load_cli_module():
    """动态加载 bin/milvus-cli.py 并缓存模块对象。"""
    if not _CLI_PATH.exists():
        raise FileNotFoundError(f"未找到 milvus-cli.py: {_CLI_PATH}")

    # bin/ 目录加入 sys.path，确保 milvus-cli.py 能 import 同目录下的 milvus_config
    bin_dir_str = str(_BIN_DIR)
    if bin_dir_str not in sys.path:
        sys.path.insert(0, bin_dir_str)

    spec = importlib.util.spec_from_file_location("brain_base_milvus_cli", _CLI_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 spec: {_CLI_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["brain_base_milvus_cli"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------


def multi_query_search(
    queries: list[str],
    top_k_per_query: int = 20,
    final_k: int = 12,
    rrf_k: int = 60,
    use_rerank: bool = False,
) -> dict[str, Any]:
    """多查询 RRF 融合检索。返回 {candidates, ...}。"""
    return _load_cli_module().multi_query_search(
        queries=queries,
        top_k_per_query=top_k_per_query,
        final_k=final_k,
        rrf_k=rrf_k,
        use_rerank=use_rerank,
    )


def dense_search(query: str, top_k: int = 12) -> list[dict[str, Any]]:
    """稠密向量检索。"""
    return _load_cli_module().dense_search(query, top_k)


def hybrid_search(query: str, top_k: int = 12) -> list[dict[str, Any]]:
    """稠密+稀疏混合检索（仅 hybrid 模式可用）。"""
    return _load_cli_module().hybrid_search(query, top_k)


def text_search(query: str, top_k: int = 12) -> list[dict[str, Any]]:
    """全文匹配检索（基于 sparse 字段）。"""
    return _load_cli_module().text_search(query, top_k)


def rerank(
    query: str,
    candidates: list[dict[str, Any]],
    top_n: int | None = None,
) -> list[dict[str, Any]]:
    """bge-reranker-v2-m3 cross-encoder 重排（软依赖）。"""
    return _load_cli_module().rerank(query, candidates, top_n=top_n)


# ---------------------------------------------------------------------------
# 写入与删除
# ---------------------------------------------------------------------------


def ingest_chunks(
    chunk_files: list[Path],
    replace_docs: bool = False,
) -> dict[str, Any]:
    """把 chunk markdown 文件批量写入 Milvus。"""
    return _load_cli_module().ingest_chunks(chunk_files, replace_docs=replace_docs)


def delete_by_doc_ids(doc_ids: list[str], confirm: bool = False) -> dict[str, Any]:
    """按 doc_id 批量删除。confirm=False 仅 dry-run。"""
    return _load_cli_module().delete_by_doc_ids(doc_ids, confirm=confirm)


def drop_collection(confirm: bool = False) -> dict[str, Any]:
    """整张 collection 删除（切换 provider 时使用）。confirm=True 才真删。"""
    return _load_cli_module().drop_collection(confirm=confirm)


# ---------------------------------------------------------------------------
# 文件系统视图（不依赖 Milvus 连接）
# ---------------------------------------------------------------------------


def hash_lookup(sha256_hex: str, raw_dir: Path | None = None) -> dict[str, Any]:
    """按 body SHA-256 在 raw 目录查找已有文档（去重用）。"""
    mod = _load_cli_module()
    return mod.hash_lookup(sha256_hex, raw_dir=raw_dir or mod._RAW_DIR_DEFAULT)


def list_docs(
    raw_dir: Path | None = None,
    chunks_dir: Path | None = None,
) -> dict[str, Any]:
    """列出全部 raw / chunks 文档。"""
    mod = _load_cli_module()
    return mod.list_docs(
        raw_dir=raw_dir or mod._RAW_DIR_DEFAULT,
        chunks_dir=chunks_dir or mod._CHUNKS_DIR_DEFAULT,
    )


def show_doc(
    doc_id: str,
    raw_dir: Path | None = None,
    chunks_dir: Path | None = None,
) -> dict[str, Any]:
    """显示单个文档的 frontmatter + chunks 概览。"""
    mod = _load_cli_module()
    return mod.show_doc(
        doc_id,
        raw_dir=raw_dir or mod._RAW_DIR_DEFAULT,
        chunks_dir=chunks_dir or mod._CHUNKS_DIR_DEFAULT,
    )


def stats(
    raw_dir: Path | None = None,
    chunks_dir: Path | None = None,
) -> dict[str, Any]:
    """知识库整体统计。"""
    mod = _load_cli_module()
    return mod.stats(
        raw_dir=raw_dir or mod._RAW_DIR_DEFAULT,
        chunks_dir=chunks_dir or mod._CHUNKS_DIR_DEFAULT,
    )


# ---------------------------------------------------------------------------
# 运行时检查
# ---------------------------------------------------------------------------


def inspect_config() -> dict[str, Any]:
    """打印当前 Milvus / embedding 配置。"""
    return _load_cli_module().inspect_config()


def check_runtime(
    require_local_model: bool = False,
    smoke_test: bool = False,
) -> dict[str, Any]:
    """运行时探测：dense_dim / sparse_nnz / resolved_mode。"""
    return _load_cli_module().check_runtime(require_local_model, smoke_test)
