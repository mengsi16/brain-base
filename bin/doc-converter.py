#!/usr/bin/env python3
"""
Doc Converter CLI for knowledge-base user uploads.

目标：
1. 把用户本地文档（PDF / DOCX / PPTX / XLSX / 图片 / LaTeX / TXT / MD）统一转成
   纯正文 Markdown，写到 ``data/docs/raw/<doc_id>.md``。
2. 同时把原始文件归档到 ``data/docs/uploads/<doc_id>/<original_filename>``，便于溯源。
3. 不写 frontmatter——frontmatter 组装由 upstream 的 ``upload-ingest`` skill 负责。
4. 输出 JSON 摘要（stdout）供 skill / agent 读取。

后端：
- PDF / DOCX / PPTX / XLSX / 图片 → MinerU CLI（``mineru``），Apache 2.0 base 许可，CJK 强
- LaTeX (.tex) → pandoc 系统命令
- TXT / MD → 直接读取（UTF-8）

用法见 ``python bin/doc-converter.py --help``。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time as _time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

_MINERU_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg"}
_PANDOC_EXTS = {".tex"}
_PLAIN_EXTS = {".txt"}
_MARKDOWN_EXTS = {".md", ".markdown"}

SUPPORTED_EXTS = _MINERU_EXTS | _PANDOC_EXTS | _PLAIN_EXTS | _MARKDOWN_EXTS


def detect_backend(path: Path) -> str:
    """Return one of: ``mineru`` / ``pandoc`` / ``plain`` / ``markdown``."""
    ext = path.suffix.lower()
    if ext in _MINERU_EXTS:
        return "mineru"
    if ext in _PANDOC_EXTS:
        return "pandoc"
    if ext in _PLAIN_EXTS:
        return "plain"
    if ext in _MARKDOWN_EXTS:
        return "markdown"
    raise ValueError(
        f"不支持的文件格式: {ext}。支持列表: {sorted(SUPPORTED_EXTS)}"
    )


# ---------------------------------------------------------------------------
# doc_id generation
# ---------------------------------------------------------------------------

_SLUG_STRIP = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")
_SLUG_TRIM = re.compile(r"^-+|-+$")


def make_doc_id(original_stem: str, upload_date: _dt.date | None = None) -> str:
    """Generate ``<slug>-YYYY-MM-DD`` id from original filename stem.

    遵循 knowledge-persistence 的命名约束：doc_id 必须带抓取/上传日期。
    Slug 保留中英文字母数字，非法字符合并成单个 ``-``；两端去 ``-``。
    """
    date = upload_date or _dt.date.today()
    slug = original_stem.lower()
    slug = _SLUG_STRIP.sub("-", slug)
    slug = _SLUG_TRIM.sub("", slug)
    if not slug:
        slug = "upload"
    return f"{slug}-{date.isoformat()}"


# ---------------------------------------------------------------------------
# Backend: MinerU (PDF / DOCX / PPTX / XLSX / images)
# ---------------------------------------------------------------------------

def _find_mineru_output(mineru_dir: Path, stem: str) -> Path:
    """Locate the Markdown file MinerU produces under ``mineru_dir``.

    MinerU 3.x 输出结构通常是 ``<out>/<stem>/auto/<stem>.md`` 或 ``<out>/<stem>/vlm/<stem>.md``。
    版本/后端不同时路径略有差异，这里用 glob 兜底。
    """
    candidates = sorted(mineru_dir.rglob(f"{stem}.md"))
    if not candidates:
        # Fallback: any .md under the target stem directory
        sub = mineru_dir / stem
        if sub.is_dir():
            candidates = sorted(sub.rglob("*.md"))
    if not candidates:
        raise FileNotFoundError(
            f"MinerU 输出目录 {mineru_dir} 下找不到任何 .md 结果。"
            " 请检查 MinerU 是否真正完成转换。"
        )
    # Prefer shortest path (root-level over nested) when multiple match.
    candidates.sort(key=lambda p: len(p.parts))
    return candidates[0]


def resolve_mineru_bin(explicit: str | None = None) -> str:
    value = explicit or os.environ.get("KB_MINERU_BIN", "")
    value = value.strip()
    return value or "mineru"


# ---------------------------------------------------------------------------
# GPU VRAM guard (MinerU 单文件即占 ~14 GB，16 GB 显卡不能并行)
# ---------------------------------------------------------------------------

# MinerU hybrid-transformers 后端单文件峰值约 14 GB VRAM。
# 低于此阈值时拒绝启动，避免 OOM 崩溃。
_DEFAULT_VRAM_LIMIT_MB = 14_000  # 14 GB


def _query_gpu_vram() -> tuple[int, int] | None:
    """Return ``(free_mb, total_mb)`` of the first NVIDIA GPU, or ``None``."""
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.free,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    line = proc.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def resolve_vram_limit_mb(explicit: int | None = None) -> int:
    """Resolve the minimum free VRAM (MB) required to launch MinerU."""
    if explicit is not None:
        return explicit
    env_val = os.environ.get("KB_MINERU_VRAM_LIMIT_MB", "").strip()
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    return _DEFAULT_VRAM_LIMIT_MB


def check_vram_before_mineru(vram_limit_mb: int) -> None:
    """Raise if GPU free VRAM < *vram_limit_mb*.  No GPU → skip (let MinerU decide)."""
    vram = _query_gpu_vram()
    if vram is None:
        # 没有 NVIDIA GPU 或 nvidia-smi 不可用——不阻止，让 MinerU 自行报错。
        return
    free_mb, total_mb = vram
    if free_mb < vram_limit_mb:
        raise RuntimeError(
            f"GPU 可用显存不足：当前空闲 {free_mb:,} MB / 总计 {total_mb:,} MB，"
            f"需要 ≥ {vram_limit_mb:,} MB。"
            f"\n  MinerU 单文件峰值约 14 GB，请关闭占用显存的其他进程后重试。"
        )


def convert_via_mineru(
    input_path: Path,
    work_dir: Path,
    mineru_bin: str | None = None,
    vram_limit_mb: int | None = None,
) -> tuple[str, Path]:
    """Run MinerU CLI on ``input_path``. Returns ``(markdown_body, md_path)``.

    ``md_path`` 指向 MinerU 实际产出的 ``<stem>.md`` 所在位置（其同级
    ``images/`` 目录是提取出来的图片资源），供 caller 根据需要把
    图片搬到长期归档位置并 rewrite MD 里的相对路径。

    stdout/stderr 直接继承父进程（终端），让用户实时看到：
    - HuggingFace Hub 的模型下载进度条（首次运行 ~2GB）
    - MinerU 自身的 layout/OCR 解析进度
    - 任何警告或错误

    代价：脚本无法把 MinerU 的报错文本回填到 JSON errors[] 字段，只能报告退出码。
    这是正确的取舍——MinerU 单次运行可能几分钟到几十分钟，静默 buffer 等同于
    "无反馈死等"。用户看到的终端输出即是最权威的诊断信息。

    **显存保护**：启动前检查 GPU 空闲 VRAM ≥ *vram_limit_mb*（默认 14 GB），
    不够则 fail-fast，避免 OOM 崩溃后残留半成品。
    """
    limit = resolve_vram_limit_mb(vram_limit_mb)
    check_vram_before_mineru(limit)

    work_dir.mkdir(parents=True, exist_ok=True)
    cmd = [resolve_mineru_bin(mineru_bin), "-p", str(input_path), "-o", str(work_dir)]
    try:
        proc = subprocess.run(cmd, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "未找到 `mineru` 可执行文件。请先 `pip install 'mineru[pipeline]>=3.1,<4.0'`"
            "（或 `mineru[core]`；默认 hybrid-transformers 后端需要本地 torch）。"
        ) from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"MinerU 转换失败 (exit={proc.returncode})。"
            " 具体错误请查看上方终端输出（stderr 已直通）。"
        )

    md_path = _find_mineru_output(work_dir, input_path.stem)
    return md_path.read_text(encoding="utf-8"), md_path


def _rescue_mineru_images(
    md_path: Path,
    body: str,
    archive_dir: Path,
    doc_id: str,
) -> str:
    """Rescue MinerU-extracted images from the transient work dir.

    MinerU 把提取出的图片放在 MD 文件同级的 ``images/`` 子目录。由于 ``_mineru_work``
    目录会被清理，本函数把图片搬到 ``archive_dir/images/``（即 uploads/<doc_id>/images/）
    并 rewrite MD body 里的相对路径，从 ``images/xxx.jpg`` 改为相对 raw/、chunks/
    的路径 ``../uploads/<doc_id>/images/xxx.jpg``（两者都在 data/docs/ 下同级）。

    如果 MinerU 没产出 images 目录（纯文字 PDF）或该目录为空，返回原 body。
    """
    images_src = md_path.parent / "images"
    if not images_src.is_dir():
        return body

    image_files = [p for p in images_src.iterdir() if p.is_file()]
    if not image_files:
        return body

    images_dst = archive_dir / "images"
    images_dst.mkdir(parents=True, exist_ok=True)
    for img in image_files:
        shutil.copy2(img, images_dst / img.name)

    # Rewrite ![alt](images/xxx.ext) → ![alt](../uploads/<doc_id>/images/xxx.ext)
    # 只替换以 'images/' 开头的相对路径，不沾染已是绝对/其他路径的引用。
    return re.sub(
        r"!\[([^\]]*)\]\(images/([^)]+)\)",
        lambda m: f"![{m.group(1)}](../uploads/{doc_id}/images/{m.group(2)})",
        body,
    )


# ---------------------------------------------------------------------------
# Backend: pandoc (LaTeX)
# ---------------------------------------------------------------------------

def convert_via_pandoc(input_path: Path) -> str:
    """Convert ``.tex`` to Markdown via pandoc."""
    cmd = [
        "pandoc",
        str(input_path),
        "--from=latex",
        "--to=gfm+tex_math_dollars+raw_tex",
        "--wrap=preserve",
    ]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "未找到 `pandoc` 可执行文件。请从 https://pandoc.org/installing.html 安装，"
            "或 `choco install pandoc` / `brew install pandoc` / `apt install pandoc`。"
        ) from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"pandoc 转换失败 (exit={proc.returncode})\n"
            f"stderr: {proc.stderr[-500:] if proc.stderr else '<empty>'}"
        )
    return proc.stdout


# ---------------------------------------------------------------------------
# Backend: plain text / markdown passthrough
# ---------------------------------------------------------------------------

def convert_plain_text(input_path: Path) -> str:
    """Treat ``.txt`` as raw body. Strip BOM, normalize line endings."""
    text = input_path.read_text(encoding="utf-8-sig", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_existing_frontmatter(text: str) -> str:
    """Remove an existing YAML frontmatter block if present.

    upload-ingest 会统一补 frontmatter，原 MD 上的 frontmatter 可能字段不全或冲突，
    直接去掉更清晰；需要保留的元信息应通过 skill 参数显式传入。
    """
    if not text.startswith("---"):
        return text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return text
    return parts[2].lstrip("\n")


def convert_markdown(input_path: Path) -> str:
    text = input_path.read_text(encoding="utf-8-sig", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return strip_existing_frontmatter(text)


# ---------------------------------------------------------------------------
# Pipeline: one file → raw MD + uploads archive
# ---------------------------------------------------------------------------

def convert_one(
    input_path: Path,
    output_dir: Path,
    uploads_dir: Path,
    overwrite: bool = False,
    upload_date: _dt.date | None = None,
    keep_mineru_work: bool = False,
    mineru_bin: str | None = None,
    vram_limit_mb: int | None = None,
) -> dict[str, Any]:
    """Convert a single file. Returns summary dict."""
    if not input_path.is_file():
        raise FileNotFoundError(f"输入文件不存在: {input_path}")

    backend = detect_backend(input_path)
    doc_id = make_doc_id(input_path.stem, upload_date=upload_date)

    raw_path = output_dir / f"{doc_id}.md"
    if raw_path.exists() and not overwrite:
        raise FileExistsError(
            f"目标 raw 文件已存在: {raw_path}。加 --overwrite 以强制覆盖。"
        )

    # Archive original file first (before heavy conversion, so on conversion
    # failure the user still has a copy for retry/inspection).
    archive_dir = uploads_dir / doc_id
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / input_path.name
    if archive_path.resolve() != input_path.resolve():
        shutil.copy2(input_path, archive_path)

    # Convert to Markdown body.
    images_dir: Path | None = None

    if backend == "mineru":
        work_dir = archive_dir / "_mineru_work"
        body, md_path = convert_via_mineru(input_path, work_dir, mineru_bin=mineru_bin, vram_limit_mb=vram_limit_mb)
        # 把 MinerU 提取的图片搬到 archive_dir/images/ 并 rewrite body 里的相对路径，
        # 避免 _mineru_work 被清理后 raw MD 的图片引用全部断链。
        body = _rescue_mineru_images(md_path, body, archive_dir, doc_id)
        candidate_images_dir = archive_dir / "images"
        if candidate_images_dir.is_dir():
            images_dir = candidate_images_dir
        if not keep_mineru_work:
            shutil.rmtree(work_dir, ignore_errors=True)
    elif backend == "pandoc":
        body = convert_via_pandoc(input_path)
    elif backend == "plain":
        body = convert_plain_text(input_path)
    elif backend == "markdown":
        body = convert_markdown(input_path)
    else:  # pragma: no cover - detect_backend already raises
        raise ValueError(f"未知 backend: {backend}")

    body = body.strip() + "\n"
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(body, encoding="utf-8")

    return {
        "doc_id": doc_id,
        "raw_path": str(raw_path).replace("\\", "/"),
        "archive_dir": str(archive_dir).replace("\\", "/"),
        "original_file": str(archive_path).replace("\\", "/"),
        "images_dir": str(images_dir).replace("\\", "/") if images_dir else None,
        "has_images": bool(images_dir),
        "char_count": len(body),
        "format": input_path.suffix.lower().lstrip("."),
        "backend": backend,
    }


# ---------------------------------------------------------------------------
# Runtime checks
# ---------------------------------------------------------------------------

def _check_command(cmd: str, version_flag: str = "--version") -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [cmd, version_flag],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except FileNotFoundError:
        return {"available": False, "version": None, "error": f"`{cmd}` 不在 PATH"}
    except subprocess.TimeoutExpired:
        return {"available": False, "version": None, "error": f"`{cmd} {version_flag}` 超时"}
    if proc.returncode != 0:
        return {"available": False, "version": None, "error": proc.stderr.strip()[:200]}
    version = (proc.stdout or proc.stderr).strip().splitlines()[0] if (proc.stdout or proc.stderr) else ""
    return {"available": True, "version": version, "error": None}


def check_runtime(mineru_bin: str | None = None) -> dict[str, Any]:
    return {
        "mineru": _check_command(resolve_mineru_bin(mineru_bin), "--version"),
        "pandoc": _check_command("pandoc", "--version"),
        "python": {"version": sys.version.split()[0], "executable": sys.executable},
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _iter_inputs(args: argparse.Namespace) -> list[Path]:
    if args.input:
        return [Path(p) for p in args.input]
    if args.input_dir:
        root = Path(args.input_dir)
        if not root.is_dir():
            raise FileNotFoundError(f"输入目录不存在: {root}")
        return [
            p for p in sorted(root.rglob("*"))
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
        ]
    raise ValueError("必须指定 --input 或 --input-dir 其中之一。")


def _parse_date(value: str | None) -> _dt.date | None:
    if not value:
        return None
    return _dt.date.fromisoformat(value)


def cmd_convert(args: argparse.Namespace) -> int:
    inputs = _iter_inputs(args)
    if not inputs:
        print(json.dumps({"results": [], "errors": ["没有符合条件的输入文件"]}, ensure_ascii=False))
        return 1

    output_dir = Path(args.output_dir)
    uploads_dir = Path(args.uploads_dir)
    upload_date = _parse_date(args.upload_date)
    vram_limit_mb = getattr(args, "vram_limit", None)

    # 严格顺序处理：MinerU 单文件峰值 ~14 GB VRAM，不允许并行。
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    total = len(inputs)
    for idx, path in enumerate(inputs, 1):
        backend = detect_backend(path)
        needs_gpu = backend == "mineru"
        if needs_gpu:
            vram = _query_gpu_vram()
            vram_info = f"（GPU 空闲 {vram[0]:,}/{vram[1]:,} MB）" if vram else "（未检测到 GPU）"
            print(f"\n[{idx}/{total}] {path.name} → MinerU {vram_info}", file=sys.stderr)
        else:
            print(f"\n[{idx}/{total}] {path.name} → {backend}", file=sys.stderr)

        try:
            summary = convert_one(
                input_path=path,
                output_dir=output_dir,
                uploads_dir=uploads_dir,
                overwrite=args.overwrite,
                upload_date=upload_date,
                keep_mineru_work=args.keep_mineru_work,
                mineru_bin=args.mineru_bin,
                vram_limit_mb=vram_limit_mb,
            )
            results.append(summary)
        except Exception as exc:  # noqa: BLE001 - surface all error types
            errors.append({"input": str(path), "error": str(exc)})

        # MinerU 子进程结束后 GPU 显存应已释放；短暂等待确保驱动回收完毕。
        if needs_gpu and idx < total:
            _time.sleep(2)

    payload = {"results": results, "errors": errors}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


def cmd_inspect(args: argparse.Namespace) -> int:
    inputs = _iter_inputs(args)
    summary = []
    for path in inputs:
        try:
            backend = detect_backend(path)
            summary.append(
                {
                    "input": str(path),
                    "format": path.suffix.lower().lstrip("."),
                    "backend": backend,
                    "size_bytes": path.stat().st_size if path.is_file() else None,
                    "proposed_doc_id": make_doc_id(path.stem),
                }
            )
        except Exception as exc:  # noqa: BLE001
            summary.append({"input": str(path), "error": str(exc)})
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_check_runtime(args: argparse.Namespace) -> int:
    report = check_runtime(mineru_bin=args.mineru_bin)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    both_ok = report["mineru"]["available"] and report["pandoc"]["available"]
    if not both_ok:
        print(
            "\n提示：MinerU 处理 PDF/DOCX/PPTX/XLSX/图片；pandoc 处理 LaTeX (.tex)。"
            "\n  - 安装 MinerU: pip install 'mineru[pipeline]>=3.1,<4.0'"
            "\n  - 安装 pandoc: 参考 https://pandoc.org/installing.html",
            file=sys.stderr,
        )
    return 0 if both_ok else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doc-converter",
        description="Convert user-uploaded documents (PDF/DOCX/PPTX/XLSX/LaTeX/TXT/MD/images) to Markdown for the knowledge base ingest pipeline.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_convert = sub.add_parser("convert", help="Convert one file or a directory of files.")
    group = p_convert.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", nargs="+", help="One or more input file paths.")
    group.add_argument("--input-dir", help="Directory containing input files (recursed).")
    p_convert.add_argument(
        "--output-dir",
        default="data/docs/raw",
        help="Target directory for converted Markdown (default: data/docs/raw).",
    )
    p_convert.add_argument(
        "--uploads-dir",
        default="data/docs/uploads",
        help="Archive directory for original files (default: data/docs/uploads).",
    )
    p_convert.add_argument(
        "--upload-date",
        default=None,
        help="ISO date (YYYY-MM-DD) to stamp into doc_id; defaults to today.",
    )
    p_convert.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing <doc_id>.md in output-dir.",
    )
    p_convert.add_argument(
        "--keep-mineru-work",
        action="store_true",
        help="Keep MinerU's intermediate work dir under uploads/<doc_id>/_mineru_work (debug only).",
    )
    p_convert.add_argument(
        "--mineru-bin",
        default=None,
        help="Path to MinerU executable. Defaults to KB_MINERU_BIN or `mineru` from PATH.",
    )
    p_convert.add_argument(
        "--vram-limit",
        type=int,
        default=None,
        help="Minimum free GPU VRAM (MB) required to launch MinerU. "
             "Defaults to KB_MINERU_VRAM_LIMIT_MB or 14000 (14 GB). "
             "Set to 0 to skip VRAM check.",
    )
    p_convert.set_defaults(func=cmd_convert)

    p_inspect = sub.add_parser("inspect", help="Dry-run: detect format & propose doc_id without converting.")
    g2 = p_inspect.add_mutually_exclusive_group(required=True)
    g2.add_argument("--input", nargs="+")
    g2.add_argument("--input-dir")
    p_inspect.set_defaults(func=cmd_inspect)

    p_check = sub.add_parser("check-runtime", help="Check whether MinerU and pandoc are available.")
    p_check.add_argument(
        "--mineru-bin",
        default=None,
        help="Path to MinerU executable. Defaults to KB_MINERU_BIN or `mineru` from PATH.",
    )
    p_check.set_defaults(func=cmd_check_runtime)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
