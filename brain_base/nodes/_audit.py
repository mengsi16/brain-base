"""
JSONL 审计日志追加工具。

约束（CLAUDE.md 硬约束 42）：会话历史是只读追加日志，禁止改写历史行。
本模块只暴露 append + read，不提供 update / delete。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


def append_audit_log(jsonl_path: Path, record: dict[str, Any]) -> None:
    """向 jsonl 文件追加一条记录，自动注入 ts 字段（若未提供）。"""
    p = Path(jsonl_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if "ts" not in record:
        record = {"ts": datetime.utcnow().isoformat() + "Z", **record}
    line = json.dumps(record, ensure_ascii=False)
    with p.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_audit_log(jsonl_path: Path) -> Iterator[dict[str, Any]]:
    """逐行读取 jsonl 文件，跳过空行与 JSON 损坏行。"""
    p = Path(jsonl_path)
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
