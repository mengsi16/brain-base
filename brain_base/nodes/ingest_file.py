"""
IngestFile 图节点函数。

流程：convert → frontmatter → persist
参考 ../brain-base-backup/skills/upload-ingest/SKILL.md

核心逻辑直接 import bin/ 下的 Python 模块，不通过 subprocess。
"""

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

import importlib

# bin/ 下的模块名带连字符，用 importlib 动态导入
_doc_converter = importlib.import_module("bin.doc-converter")
convert_one = _doc_converter.convert_one


def convert_node(state: dict[str, Any]) -> dict[str, Any]:
    """调用 bin.doc_converter.convert_one 完成格式转换"""
    input_files = state.get("input_files", [])
    if not input_files:
        return {"error": "convert_node: input_files 为空", "converted": [], "conversion_errors": []}

    upload_date_str = state.get("upload_date", date.today().isoformat())
    upload_date = date.fromisoformat(upload_date_str) if upload_date_str else None

    converted: list[dict] = []
    conversion_errors: list[dict] = []

    for fp in input_files:
        try:
            result = convert_one(
                input_path=Path(fp),
                output_dir=Path("data/docs/raw"),
                uploads_dir=Path("data/docs/uploads"),
                upload_date=upload_date,
            )
            converted.append(result)
        except Exception as exc:
            conversion_errors.append({"input": fp, "error": str(exc)})

    return {"converted": converted, "conversion_errors": conversion_errors}


def frontmatter_node(state: dict[str, Any]) -> dict[str, Any]:
    """为每个 raw MD 组装 user-upload frontmatter"""
    converted = state.get("converted", [])
    if not converted:
        return {"raw_paths": [], "error": state.get("error", "frontmatter_node: 无转换结果")}

    upload_date = state.get("upload_date", date.today().isoformat())
    raw_paths: list[str] = []

    for item in converted:
        raw_path = Path(item["raw_path"])
        if not raw_path.exists():
            continue

        body = raw_path.read_text(encoding="utf-8")
        sha256 = hashlib.sha256(
            body.replace("\r\n", "\n").strip("\n").encode("utf-8")
        ).hexdigest()

        title = item.get("doc_id", "untitled")
        for line in body.split("\n"):
            if line.startswith("# "):
                title = line[2:].strip()
                break

        fm = (
            "---\n"
            f"doc_id: {item['doc_id']}\n"
            f"title: {title}\n"
            "source: user-upload\n"
            "source_type: user-upload\n"
            f"original_file: {item.get('original_file', '')}\n"
            "url:\n"
            f"fetched_at: {upload_date}\n"
            f"content_sha256: {sha256}\n"
            "keywords: []\n"
            "---\n\n"
        )
        raw_path.write_text(fm + body, encoding="utf-8")
        raw_paths.append(str(raw_path))

    return {"raw_paths": raw_paths}


def create_persist_node(llm: Any = None):
    """持久化节点工厂：循环调用 PersistenceGraph 处理多个 raw 文件"""
    from brain_base.graphs.persistence_graph import PersistenceGraph

    def persist_node(state: dict[str, Any]) -> dict[str, Any]:
        raw_paths = state.get("raw_paths", [])
        if not raw_paths:
            return {"persistence_results": [], "error": state.get("error", "")}

        pg = PersistenceGraph(llm=llm)
        results: list[dict] = []

        for rp in raw_paths:
            path = Path(rp)
            doc_id = path.stem
            try:
                r = pg.run(raw_md_path=rp, doc_id=doc_id)
                results.append(r)
            except Exception as exc:
                results.append({"doc_id": doc_id, "error": str(exc)})

        return {"persistence_results": results}

    return persist_node


# 向后兼容
persist_node = create_persist_node(llm=None)
