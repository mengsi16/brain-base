"""
LangGraph 节点执行跟踪工具。

提供 `stream_with_trace(graph, initial_state, logger=None)`：
- 用 `graph.stream(mode="updates")` 逐节点消费输出
- 每完成一个节点就 INFO 一行：耗时 + 新增/覆盖的 state 字段摘要
- 同时把每个节点的完整 update payload 以 DEBUG 级别写入 JSONL 文件（可选）

设计原则：
- 调用方已经 compile 过 graph；tracer 只负责消费 stream。
- 不修改 node 实现，不改变任何 state 字段。
- 支持 logger 传 None（降级到 print），方便临时脚本使用。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any


# 每个字段的摘要最大字符数（防止 evidence 等长列表刷屏）
_FIELD_PREVIEW_LIMIT = 180


def _preview(value: Any) -> str:
    """把任意 state 字段压成单行摘要。"""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = value.replace("\n", " ⏎ ").strip()
        if len(text) > _FIELD_PREVIEW_LIMIT:
            return f"{text[:_FIELD_PREVIEW_LIMIT]}… ({len(value)}字)"
        return text or "''"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        keys = list(value.keys())[:5]
        return f"dict{{{', '.join(keys)}{'…' if len(value) > 5 else ''}}}"
    return type(value).__name__


def _format_update(node: str, payload: dict[str, Any]) -> str:
    """把一个 `{node: {field1: v1, field2: v2}}` update 渲染成多行摘要。"""
    if not isinstance(payload, dict):
        return f"  <{type(payload).__name__}> {payload}"
    if not payload:
        return "  (无字段变更)"
    parts = []
    for key, value in payload.items():
        parts.append(f"  · {key} = {_preview(value)}")
    return "\n".join(parts)


def stream_with_trace(
    graph: Any,
    initial_state: dict[str, Any],
    *,
    logger: logging.Logger | None = None,
    jsonl_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行 graph.stream 并逐节点打印执行跟踪。

    Args:
        graph: 已 compile 的 langgraph StateGraph。
        initial_state: 初始 state dict。
        logger: logging.Logger 实例；None 时 fallback 到 print。
        jsonl_path: 若给出，将每个节点的完整 update payload 追加写入 JSONL 文件。
        config: 传给 `graph.stream` 的 config（如 `{"recursion_limit": 50}`）。

    Returns:
        最终 state（merge 所有 update 后的结果）。
    """
    log = logger.info if logger else print

    if jsonl_path is not None:
        jsonl_path = Path(jsonl_path)
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        jsonl_fh = jsonl_path.open("a", encoding="utf-8")
    else:
        jsonl_fh = None

    merged_state: dict[str, Any] = dict(initial_state)
    step = 0
    graph_started = time.time()
    node_started = graph_started

    try:
        log("=" * 72)
        log(f"[GRAPH START] initial_state keys={list(initial_state.keys())}")
        log("=" * 72)

        for update in graph.stream(initial_state, config=config or {}, stream_mode="updates"):
            step += 1
            now = time.time()
            elapsed_ms = int((now - node_started) * 1000)
            node_started = now

            for node_name, node_payload in update.items():
                log("-" * 72)
                log(f"[NODE {step:02d}] {node_name} (+{elapsed_ms}ms)")
                log(_format_update(node_name, node_payload))
                if isinstance(node_payload, dict):
                    merged_state.update(node_payload)
                if jsonl_fh is not None:
                    jsonl_fh.write(json.dumps({
                        "step": step,
                        "node": node_name,
                        "elapsed_ms": elapsed_ms,
                        "update": _to_jsonable(node_payload),
                    }, ensure_ascii=False) + "\n")
                    jsonl_fh.flush()

        total_ms = int((time.time() - graph_started) * 1000)
        log("=" * 72)
        log(f"[GRAPH END] {step} 节点执行完毕，总耗时 {total_ms}ms")
        log("=" * 72)
    finally:
        if jsonl_fh is not None:
            jsonl_fh.close()

    return merged_state


def _to_jsonable(value: Any) -> Any:
    """尽力把 state 序列化为 JSON 安全值。"""
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {k: _to_jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_to_jsonable(v) for v in value]
        return repr(value)[:500]


def configure_logger(
    name: str = "brain_base",
    level: int = logging.INFO,
    log_file: str | Path | None = None,
) -> logging.Logger:
    """一键配置带控制台 + 可选文件 handler 的 logger（UTF-8）。"""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger
