"""
内容哈希工具（薄封装）。

实际实现位于 agents/utils/agent_utils.compute_content_hash，本模块
只 re-export 给节点层使用，避免节点 import 路径过深。
"""

from __future__ import annotations

import hashlib

from brain_base.agents.utils.agent_utils import compute_content_hash


def compute_body_sha256(body: str) -> str:
    """计算入库正文的 SHA-256（CRLF 归一化、首尾空行裁剪后取 64 位 hex）。

    与 ingest_url / ingest_file 节点中的 hash 计算逻辑一致，便于跨入口
    去重（hash_lookup）。
    """
    normalized = body.replace("\r\n", "\n").strip("\n").encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


__all__ = ["compute_content_hash", "compute_body_sha256"]
