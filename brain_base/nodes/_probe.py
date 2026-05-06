"""
基础设施探测：判断 Milvus / playwright-cli / doc-converter 是否可用。

QA 主图入口和 ingest 入口都需要先做探测，决定走完整路径还是降级路径
（CLAUDE.md 硬约束 14：新层必须软依赖）。
"""

from __future__ import annotations

from typing import Any


def probe_milvus(timeout: float = 5.0) -> dict[str, Any]:
    """探测 Milvus 是否可连接。

    通过 tools.milvus_client.check_runtime 间接检查（已封装 import 失败兜底）。
    """
    try:
        from brain_base.tools.milvus_client import check_runtime

        result = check_runtime(require_local_model=False, smoke_test=False)
        return {
            "available": True,
            "dense_dim": result.get("dense_dim"),
            "resolved_mode": result.get("resolved_mode"),
        }
    except Exception as exc:  # noqa: BLE001
        # 探测层允许 catch：节点根据 available 判定降级，不在节点里再 raise
        return {"available": False, "error": str(exc)[:300]}


def probe_playwright(timeout: float = 5.0) -> dict[str, Any]:
    """探测 playwright-cli 是否可用。"""
    try:
        from brain_base.tools.web_fetcher import probe_playwright as _probe

        return _probe(timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)[:300]}


def probe_doc_converter(timeout: float = 30.0) -> dict[str, Any]:
    """探测 doc-converter（MinerU / pandoc）是否可用。"""
    try:
        from brain_base.tools.doc_converter_tool import check_doc_converter_runtime

        result = check_doc_converter_runtime()
        # mineru 可用即可上传图片/PDF；pandoc 仅 .tex 需要
        return {
            "available": result.get("mineru", {}).get("available", False),
            "mineru": result.get("mineru", {}),
            "pandoc": result.get("pandoc", {}),
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)[:300]}


def probe_all() -> dict[str, dict[str, Any]]:
    """一次性返回三项探测结果，QA 主图初始化时调用。"""
    return {
        "milvus": probe_milvus(),
        "playwright": probe_playwright(),
        "doc_converter": probe_doc_converter(),
    }
