# -*- coding: utf-8 -*-
"""T47.2：extract_urls 节点（D7 A 方案）。

正则提取用户问题中的 URL，去重保序后写入 ``user_urls`` 字段。

设计理由（契约 §3 + §14 D7 拍板）：
- 把 user_urls 提取从 normalize 节点剥离，独立成轻量同步节点
- 不调 LLM，~30 行
- 让 normalize 专注于 LLM 改写 + URL 上下文消费 + 摘要产出
- 让主图条件路由（``route_after_extract_urls``）按 user_urls 是否非空决定是否走 url_pre_fetch
- ``user_urls`` 不再作为流程分流标志，只是状态字段（契约 §0 核心原则 1）

主图位置（T47.4 接入）：
``crystallized_check (miss) → extract_urls → [user_urls 非空] → url_pre_fetch → normalize``
``                                          → [user_urls 空]   → normalize``

契约引用：md/research/2026-05-17-t47-unified-intent-agent-contract.md §3 + §14 D7
"""

from __future__ import annotations

import re
from typing import Any, Callable

# 与 T46 normalize 内提取逻辑保持完全一致——T47.4 接入后 normalize 那段会被删
_URL_RE = re.compile(r'https?://[^\s<>"\)\]]+')
# URL 末尾常带的 markdown / 中文标点，提取后剥离
_TRAILING_PUNCT = ".,;:!?"


def create_extract_urls() -> Callable:
    """工厂函数：返回 extract_urls 同步节点函数。

    保持与其他 LLM 节点工厂一致的 ``create_*`` 命名，便于 graph 注册时按统一模式
    调用。本节点不调 LLM，工厂签名不接受 ``llm`` 参数。
    """

    def extract_urls_node(state: dict[str, Any]) -> dict[str, Any]:
        question = state.get("question", "") or ""
        matches = _URL_RE.findall(question)
        seen: set[str] = set()
        deduped: list[str] = []
        for u in matches:
            u = u.rstrip(_TRAILING_PUNCT)
            if u and u not in seen:
                seen.add(u)
                deduped.append(u)
        return {"user_urls": deduped}

    return extract_urls_node
