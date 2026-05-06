"""
brain_base 工具层。

将外部 CLI（milvus-cli / playwright-cli / doc-converter / chunker）的
调用入口收敛到本模块，节点和图层只 import 这里的纯函数，避免在
graph 进程内反复 fork subprocess 浪费资源。

设计原则：
- milvus_client：bin/milvus-cli.py 已经把核心逻辑做成顶层函数，
  通过 importlib 动态加载，缓存模块对象。
- web_fetcher：全走 playwright-cli。subprocess 单次抓取走完整生命周期；
  以前的 trafilatura 静态路径已下架（SPA 抓空骨架的伪成功问题）。
- doc_converter_tool / chunker_tool：极薄 subprocess 包装，调用频率低。
"""

from __future__ import annotations

from .milvus_client import (
    multi_query_search,
    dense_search,
    hybrid_search,
    text_search,
    hash_lookup,
    delete_by_doc_ids,
    drop_collection,
    inspect_config,
    check_runtime,
    ingest_chunks,
    list_docs,
    show_doc,
    stats,
    rerank,
)
from .web_fetcher import search_google, search_bing, fetch_page, probe_playwright
from .doc_converter_tool import convert_document, inspect_document, check_doc_converter_runtime
from .chunker_tool import chunk_markdown

__all__ = [
    # milvus
    "multi_query_search",
    "dense_search",
    "hybrid_search",
    "text_search",
    "hash_lookup",
    "delete_by_doc_ids",
    "drop_collection",
    "inspect_config",
    "check_runtime",
    "ingest_chunks",
    "list_docs",
    "show_doc",
    "stats",
    "rerank",
    # web
    "search_google",
    "search_bing",
    "fetch_page",
    "probe_playwright",
    # doc converter
    "convert_document",
    "inspect_document",
    "check_doc_converter_runtime",
    # chunker
    "chunk_markdown",
]
