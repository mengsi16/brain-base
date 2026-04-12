#!/usr/bin/env python3
"""
Project-local Milvus adapter.

注意：
1. 这个文件不再伪装成“Milvus 原生 MCP Server”。
2. 官方生态已有独立的 Milvus MCP Server，本地项目应优先对接官方实现。
3. 当前脚本只作为项目内的轻量适配入口，复用真实 embedding provider 配置。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BIN_DIR = ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from milvus_cli import dense_search, hybrid_search, inspect_config, text_search  # noqa: E402


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="knowledge-base Milvus adapter")
    parser.add_argument("--inspect-config", action="store_true", help="打印 Milvus 配置")
    parser.add_argument("--dense-search", help="执行 dense 检索")
    parser.add_argument("--hybrid-search", help="执行 hybrid 检索")
    parser.add_argument("--text-search", help="执行 BM25 / sparse 文本检索")
    parser.add_argument("--top-k", type=int, default=10, help="返回结果数")
    args = parser.parse_args()

    if args.inspect_config:
        print(json.dumps(inspect_config(), ensure_ascii=False, indent=2))
        return

    if args.dense_search:
        print(json.dumps(dense_search(args.dense_search, args.top_k), ensure_ascii=False, indent=2))
        return

    if args.hybrid_search:
        print(json.dumps(hybrid_search(args.hybrid_search, args.top_k), ensure_ascii=False, indent=2))
        return

    if args.text_search:
        print(json.dumps(text_search(args.text_search, args.top_k), ensure_ascii=False, indent=2))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
