"""
bin/chunker.py 的薄封装。

chunker.py 命令格式：
- python bin/chunker.py <raw_path> [--output-dir <dir>] [--min N] [--max N] [--overlap N] [--dry-run]

确定性 Markdown 分块（CLAUDE.md memory：物理切分由 chunker.py 完成，
LLM 只做后续富化）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

_BIN_DIR = Path(__file__).resolve().parents[2] / "bin"
_CHUNKER = _BIN_DIR / "chunker.py"


def chunk_markdown(
    raw_path: Path,
    output_dir: Path | None = None,
    min_chars: int | None = None,
    max_chars: int | None = None,
    overlap: int | None = None,
    dry_run: bool = False,
    timeout: float = 120,
) -> dict[str, Any]:
    """对 raw markdown 文件做确定性分块。返回 {chunk_files, chunk_count, ...}。

    解析 chunker.py 的 stdout：成功时打印分块结果；dry_run 不写文件。
    """
    if not _CHUNKER.exists():
        raise FileNotFoundError(f"未找到 chunker.py: {_CHUNKER}")

    args: list[str] = [sys.executable, str(_CHUNKER), str(raw_path)]
    if output_dir:
        args.extend(["--output-dir", str(output_dir)])
    if min_chars is not None:
        args.extend(["--min", str(min_chars)])
    if max_chars is not None:
        args.extend(["--max", str(max_chars)])
    if overlap is not None:
        args.extend(["--overlap", str(overlap)])
    if dry_run:
        args.append("--dry-run")

    proc = subprocess.run(
        args,
        capture_output=True,
        timeout=timeout,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"chunker.py 失败 rc={proc.returncode} stderr={proc.stderr[:500]}"
        )

    # 收集生成的 chunk 文件路径：chunker 默认输出到
    # data/docs/chunks/<doc_id>/，stdout 末尾会有汇总信息。
    chunk_dir = output_dir
    if chunk_dir is None:
        # chunker.py 内部默认 ROOT_DIR / "data" / "docs" / "chunks"
        chunk_dir = _BIN_DIR.parent / "data" / "docs" / "chunks"

    chunk_files: list[Path] = []
    if not dry_run and chunk_dir.exists():
        # raw_path 一般是 data/docs/raw/<doc_id>.md，chunker 输出在
        # data/docs/chunks/<doc_id>/<doc_id>__chunk-XX.md
        doc_stem = raw_path.stem
        candidate_dir = chunk_dir / doc_stem
        if candidate_dir.exists():
            chunk_files = sorted(candidate_dir.glob(f"{doc_stem}__chunk-*.md"))
        else:
            chunk_files = sorted(chunk_dir.glob(f"{doc_stem}__chunk-*.md"))

    return {
        "raw_path": str(raw_path),
        "chunk_files": [str(p) for p in chunk_files],
        "chunk_count": len(chunk_files),
        "stdout": proc.stdout,
        "dry_run": dry_run,
    }
