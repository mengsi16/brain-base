"""
Agent 共享工具函数。

参考 TradingAgents 的 agents/utils/agent_utils.py 模式。
"""

import hashlib
from datetime import date
from pathlib import Path
from typing import Any


def compute_content_hash(text: str) -> str:
    """计算文本 SHA-256 内容哈希"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def generate_doc_id(prefix: str, url: str = "", filepath: str = "") -> str:
    """生成 doc_id：前缀-日期-哈希"""
    source = url or filepath or "unknown"
    h = hashlib.sha256(source.encode("utf-8")).hexdigest()[:8]
    today = date.today().isoformat()
    return f"{prefix}-{today}-{h}"


def build_frontmatter(
    doc_id: str,
    source_type: str = "user-upload",
    url: str = "",
    title: str = "",
    content_hash: str = "",
    extra: dict[str, Any] | None = None,
) -> str:
    """组装 YAML frontmatter 字符串"""
    lines = ["---"]
    lines.append(f"doc_id: {doc_id}")
    lines.append(f"source_type: {source_type}")
    if url:
        lines.append(f"url: {url}")
    if title:
        lines.append(f"title: {title}")
    if content_hash:
        lines.append(f"content_hash: {content_hash}")
    lines.append(f"fetched_at: {date.today().isoformat()}")
    if extra:
        for k, v in extra.items():
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def create_msg_delete():
    """创建消息清理节点（参考 TradingAgents 的 create_msg_delete）"""

    def msg_delete_node(state: dict[str, Any]) -> dict[str, Any]:
        return {"messages": []}

    return msg_delete_node
