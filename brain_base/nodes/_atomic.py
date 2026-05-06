"""
原子写工具：先写到同目录的 .tmp 文件，再 os.replace 到目标路径。

避免半写状态被读者读到（特别是并发 enrich / lifecycle 期间）。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """原子写文本文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 同目录创建临时文件，确保 os.replace 走同一文件系统（跨盘 replace 会失败）
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp_path, str(path))
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def atomic_write_json(
    path: Path,
    obj: Any,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> None:
    """原子写 JSON 文件。"""
    text = json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii)
    atomic_write_text(Path(path), text + "\n")
