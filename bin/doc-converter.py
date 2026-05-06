#!/usr/bin/env python3
"""
Doc Converter CLI for knowledge-base user uploads.

目标：
1. 把用户本地文档（PDF / DOCX / PPTX / XLSX / 图片 / HTML / LaTeX / TXT / MD）统一转成
   纯正文 Markdown，写到 ``data/docs/raw/<doc_id>.md``。
2. 同时把原始文件归档到 ``data/docs/uploads/<doc_id>/<original_filename>``，便于溯源。
3. 不写 frontmatter——frontmatter 组装由 upstream 的 ``upload-ingest`` skill 负责。
4. 输出 JSON 摘要（stdout）供 skill / agent 读取。

后端：
- PDF / DOCX / PPTX / XLSX / 图片 → MinerU CLI（``mineru``），Apache 2.0 base 许可，CJK 强
- HTML (.html/.htm) → MinerU-HTML（SLM 主体提取 + MinerU-Webkit 转 Markdown）
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
from typing import Any, Dict


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

_MINERU_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg"}
_MINERU_HTML_EXTS = {".html", ".htm"}
_PANDOC_EXTS = {".tex"}
_PLAIN_EXTS = {".txt"}
_MARKDOWN_EXTS = {".md", ".markdown"}

# Source code files. Key is the lowercase extension, value is the Markdown
# fenced-code-block language identifier. Code files are wrapped in a fenced
# block so downstream chunking, synthetic-QA generation, and rendering all
# keep the code intact and language-aware.
_CODE_EXTS: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".lua": "lua",
    ".dart": "dart",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".ps1": "powershell",
    ".sql": "sql",
    ".r": "r",
    ".jl": "julia",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hs": "haskell",
    ".ml": "ocaml",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".jsonc": "json",
    ".xml": "xml",
    ".css": "css",
    ".scss": "scss",
    ".vue": "vue",
    ".svelte": "svelte",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".env": "dotenv",
    ".dockerfile": "dockerfile",
    ".mk": "makefile",
    ".gradle": "groovy",
    ".groovy": "groovy",
}

SUPPORTED_EXTS = (
    _MINERU_EXTS
    | _MINERU_HTML_EXTS
    | _PANDOC_EXTS
    | _PLAIN_EXTS
    | _MARKDOWN_EXTS
    | set(_CODE_EXTS.keys())
)


def detect_backend(path: Path) -> str:
    """Return one of: ``mineru`` / ``mineru_html`` / ``pandoc`` / ``plain`` / ``markdown`` / ``code``."""
    ext = path.suffix.lower()
    if ext in _MINERU_EXTS:
        return "mineru"
    if ext in _MINERU_HTML_EXTS:
        return "mineru_html"
    if ext in _PANDOC_EXTS:
        return "pandoc"
    if ext in _PLAIN_EXTS:
        return "plain"
    if ext in _MARKDOWN_EXTS:
        return "markdown"
    if ext in _CODE_EXTS:
        return "code"
    raise ValueError(
        f"不支持的文件格式: {ext}。支持列表: {sorted(SUPPORTED_EXTS)}"
    )


def _code_language_for(path: Path) -> str:
    """Return the Markdown fenced-code-block language tag for a code file.

    Unknown extensions fall back to an empty string so the fenced block still
    renders without syntax highlighting.
    """
    return _CODE_EXTS.get(path.suffix.lower(), "")


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


def resolve_mineru_python(mineru_bin: str | None = None) -> str:
    """Resolve the Python interpreter that should import/run MinerU.

    如果 ``mineru_bin`` 指向某个独立虚拟环境里的 ``mineru(.exe)``，优先使用同目录下的
    ``python(.exe)``，从而绕过 CLI 的本地 FastAPI + 轮询封装，同时继续复用该环境里
    已安装的 MinerU / transformers 依赖。
    """
    resolved = resolve_mineru_bin(mineru_bin)
    path = Path(resolved)
    if path.parent.exists():
        for candidate_name in ("python.exe", "python"):
            candidate = path.parent / candidate_name
            if candidate.is_file():
                return str(candidate)
    return sys.executable


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


def _count_pdf_pages(input_path: Path) -> int:
    """Count pages in a PDF file. Returns 0 if unable to determine."""
    try:
        import fitz  # PyMuPDF – lightweight, already a transitive dep
        doc = fitz.open(str(input_path))
        count = doc.page_count
        doc.close()
        return count
    except Exception:  # noqa: BLE001
        # Fallback: try pdfinfo from poppler-utils
        try:
            proc = subprocess.run(
                ["pdfinfo", str(input_path)],
                capture_output=True, text=True, timeout=10,
            )
            for line in proc.stdout.splitlines():
                if line.startswith("Pages:"):
                    return int(line.split(":")[1].strip())
        except Exception:  # noqa: BLE001
            pass
        return 0


def _run_mineru_via_python_api(
    input_path: Path,
    work_dir: Path,
    mineru_bin: str | None = None,
    page_range: str | None = None,
) -> None:
    """Run MinerU via its synchronous local Python API.

    这样可以绕过 ``mineru`` CLI 内部的"本地 FastAPI + wait_for_task_result 轮询"封装层，
    避免出现文档已解析完成但客户端卡在结果轮询阶段、最终超时失败的问题。

    ``page_range``：可选页范围（如 ``"1-10"``），仅对 PDF 有效。为 None 时处理全部页面。
    """
    python_exe = resolve_mineru_python(mineru_bin)

    # Set MINERU_PROCESSING_WINDOW_SIZE to limit VRAM usage.
    # Default 64 pages per window; reduce to env var or 10 for large docs.
    window_size = os.environ.get("MINERU_PROCESSING_WINDOW_SIZE", "").strip()
    env_extra: dict[str, str] = {}
    if not window_size:
        env_extra["MINERU_PROCESSING_WINDOW_SIZE"] = "10"
    else:
        env_extra["MINERU_PROCESSING_WINDOW_SIZE"] = window_size

    do_parse_args = [
        "    output_dir=str(output_dir),",
        "    pdf_file_names=[input_path.stem],",
        "    pdf_bytes_list=[input_path.read_bytes()],",
        "    p_lang_list=['ch'],",
        "    backend='hybrid-auto-engine',",
        "    parse_method='auto',",
        "    formula_enable=True,",
        "    table_enable=True,",
        "    f_draw_layout_bbox=False,",
        "    f_draw_span_bbox=False,",
        "    f_dump_md=True,",
        "    f_dump_middle_json=True,",
        "    f_dump_model_output=True,",
        "    f_dump_orig_pdf=True,",
        "    f_dump_content_list=True,",
    ]
    if page_range is not None:
        do_parse_args.append(f"    page_range='{page_range}',")
    do_parse_args.append(")")

    script = "\n".join(
        [
            "from pathlib import Path",
            "import sys",
            "from mineru.cli.common import do_parse",
            "input_path = Path(sys.argv[1])",
            "output_dir = Path(sys.argv[2])",
            "do_parse(",
            *do_parse_args,
        ]
    )
    cmd = [python_exe, "-c", script, str(input_path), str(work_dir)]
    try:
        proc = subprocess.run(
            cmd, check=False,
            env={**os.environ, **env_extra},
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"未找到可用的 MinerU Python 解释器：{python_exe}。"
            " 请检查 KB_MINERU_BIN / --mineru-bin 是否指向有效环境。"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"MinerU 本地 Python API 转换失败 (exit={proc.returncode})。"
            " 具体错误请查看上方终端输出（stderr 已直通）。"
        )


def _merge_batch_markdowns(batch_dirs: list[Path], final_work_dir: Path, stem: str) -> Path:
    """Merge markdown outputs from batched MinerU runs into a single file.

    Each batch produces its own ``<stem>.md`` and ``images/`` directory.
    This function concatenates all markdown bodies and consolidates images
    into the final work directory.
    """
    parts: list[str] = []
    images_dst = final_work_dir / "images"
    images_dst.mkdir(parents=True, exist_ok=True)
    img_idx = 0

    for batch_dir in batch_dirs:
        md_path = _find_mineru_output(batch_dir, stem)
        if md_path is None:
            continue
        body = md_path.read_text(encoding="utf-8")

        # MinerU puts images at <stem>/hybrid_auto/images/ (same dir as the .md)
        batch_images = md_path.parent / "images"
        if batch_images.is_dir():
            for img_file in sorted(batch_images.iterdir()):
                if not img_file.is_file():
                    continue
                new_name = f"batch{img_idx}_{img_file.name}"
                shutil.copy2(img_file, images_dst / new_name)
                # Rewrite image references in body
                body = body.replace(
                    f"images/{img_file.name}",
                    f"images/{new_name}",
                )
        img_idx += 1
        parts.append(body)

    final_md = final_work_dir / f"{stem}.md"
    final_md.write_text("\n\n".join(parts), encoding="utf-8")
    return final_md


# Default page batch size for large PDFs.
# PDFs with more pages than this will be split into batches.
_DEFAULT_PAGE_BATCH_SIZE = 10


def _resolve_page_batch_size() -> int:
    """Resolve page batch size from env or default."""
    env_val = os.environ.get("KB_MINERU_PAGE_BATCH_SIZE", "").strip()
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    return _DEFAULT_PAGE_BATCH_SIZE


def convert_via_mineru(
    input_path: Path,
    work_dir: Path,
    mineru_bin: str | None = None,
    vram_limit_mb: int | None = None,
) -> tuple[str, Path]:
    """Run MinerU on ``input_path``. Returns ``(markdown_body, md_path)``.

    ``md_path`` 指向 MinerU 实际产出的 ``<stem>.md`` 所在位置（其同级
    ``images/`` 目录是提取出来的图片资源），供 caller 根据需要把
    图片搬到长期归档位置并 rewrite MD 里的相对路径。

    默认优先走 MinerU 的本地同步 Python API，而不是 ``mineru`` CLI 的异步轮询封装。
    原因是用户实测存在"解析已完成但卡在 wait_for_task_result，最终超时"的上游 bug；
    直接调用本地 API 可以绕过这一层。

    **显存保护**：
    1. 启动前检查 GPU 空闲 VRAM ≥ *vram_limit_mb*（默认 14 GB），不够则 fail-fast。
    2. 对 PDF 自动按页分批处理：超过 ``KB_MINERU_PAGE_BATCH_SIZE``（默认 10）页的 PDF
       会被拆分成多个批次，每批单独调用 MinerU，最后合并结果。
       这避免了 MinerU 一次性处理所有页面导致显存溢出。
    3. 设置 ``MINERU_PROCESSING_WINDOW_SIZE=10``（默认），控制 MinerU 内部滑动窗口大小。
    """
    limit = resolve_vram_limit_mb(vram_limit_mb)
    check_vram_before_mineru(limit)

    work_dir.mkdir(parents=True, exist_ok=True)

    # For PDFs, check page count and batch if needed
    is_pdf = input_path.suffix.lower() == ".pdf"
    if is_pdf:
        page_count = _count_pdf_pages(input_path)
        batch_size = _resolve_page_batch_size()
        if page_count > batch_size and page_count > 0:
            print(
                f"  PDF 共 {page_count} 页，按每批 {batch_size} 页分批处理（防止显存溢出）",
                file=sys.stderr,
            )
            return _convert_pdf_in_batches(
                input_path, work_dir, page_count, batch_size, mineru_bin=mineru_bin,
            )

    # Small PDF or non-PDF: process in one go
    _run_mineru_via_python_api(input_path, work_dir, mineru_bin=mineru_bin)
    md_path = _find_mineru_output(work_dir, input_path.stem)
    return md_path.read_text(encoding="utf-8"), md_path


def _convert_pdf_in_batches(
    input_path: Path,
    work_dir: Path,
    page_count: int,
    batch_size: int,
    mineru_bin: str | None = None,
) -> tuple[str, Path]:
    """Convert a large PDF in page-range batches, then merge results."""
    batch_dirs: list[Path] = []

    for start_page in range(1, page_count + 1, batch_size):
        end_page = min(start_page + batch_size - 1, page_count)
        page_range = f"{start_page}-{end_page}"
        batch_idx = len(batch_dirs)
        batch_work = work_dir / f"_batch_{batch_idx:03d}_p{start_page}-{end_page}"
        batch_work.mkdir(parents=True, exist_ok=True)

        print(
            f"  批次 {batch_idx + 1}: 第 {start_page}-{end_page} 页 / 共 {page_count} 页",
            file=sys.stderr,
        )

        _run_mineru_via_python_api(
            input_path, batch_work, mineru_bin=mineru_bin, page_range=page_range,
        )
        batch_dirs.append(batch_work)

        # Wait between batches for GPU memory to be fully released
        if end_page < page_count:
            _time.sleep(3)

    # Merge all batch outputs into final work_dir
    md_path = _merge_batch_markdowns(batch_dirs, work_dir, input_path.stem)

    # Keep batch subdirectories under _mineru_work for traceability.
    # Do NOT delete them — user wants full MinerU output preserved.

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
# Backend: MinerU-HTML (HTML → 主体提取 → Markdown)
# ---------------------------------------------------------------------------

# 送进 SLM 之前需要剥离的噪音标签——这些标签不携带正文内容，但能轻易把 token 占满。
# 典型场景：Docusaurus / Next.js / Nuxt 这类静态站会在 <head> 塞几百个
# <link rel="prefetch"> / <link rel="preload">，134KB 的页面里 head 就能占
# 16K+ tokens，导致 SLM 截断后看不到 <main> 元素而判定"主体为空"。
_HTML_NOISE_TAGS = ("script", "style", "noscript", "iframe", "svg")
_HTML_NOISE_LINK_RELS = ("prefetch", "preload", "dns-prefetch", "preconnect", "modulepreload")


def _strip_html_noise(html: str) -> str:
    """剥离不影响主体语义的噪音标签，压缩送进 SLM 的 token 数。

    保留：``<title>``、``<body>`` 内所有结构标签（header/nav/main/article/aside/footer
    及 div/section/p/h1-6/ul/li/table/code 等），以及 SLM 识别正文所需的语义信号。
    剥离：``<script>`` / ``<style>`` / ``<noscript>`` / ``<iframe>`` / ``<svg>``，
    以及 ``<link rel="prefetch|preload|...">`` 这类纯性能优化指示。
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover - bs4 是 mineru_html 的传递依赖
        raise RuntimeError("未找到 `beautifulsoup4`。请安装：pip install beautifulsoup4") from exc
    soup = BeautifulSoup(html, "html.parser")
    for tag_name in _HTML_NOISE_TAGS:
        for el in soup.find_all(tag_name):
            el.decompose()
    for el in soup.find_all("link"):
        rel = el.get("rel") or []
        if isinstance(rel, str):
            rel = [rel]
        if any(r.lower() in _HTML_NOISE_LINK_RELS for r in rel):
            el.decompose()
    # HTML 注释也是纯噪音
    from bs4 import Comment

    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()
    return str(soup)


def convert_via_mineru_html(input_path: Path) -> str:
    """用 MinerU-HTML 从 HTML 提取主体内容并转为 Markdown。

    Pipeline：HTML 简化 → SLM 分类元素（main/other）→ 提取主体 HTML →
    MinerU-Webkit 转 Markdown。

    fail-fast：MinerU-HTML 失败直接抛 RuntimeError，**不静默降级**——
    降级到 trafilatura 只能拿到 plain text（丢失标题层级/代码块/列表结构），
    会让下游误以为入库成功但实际质量极差。

    GPU 配置：默认 `cuda:0` 直接占用单卡（避免 accelerate `device_map="auto"`
    预分配 90% GPU 显存）。与 bge-m3 共存时调用方应通过 subprocess 隔离
    （`doc_converter_tool.convert_html_to_markdown` 已实现）。没 GPU 时设
    `BB_MINERU_HTML_DEVICE=cpu`。
    """
    html_content = input_path.read_text(encoding="utf-8-sig", errors="replace")
    html_content = html_content.replace("\r\n", "\n").replace("\r", "\n")

    # ===== 预清洗：剥离 prefetch/preload/script/style 等噪音 =====
    # SPA 站（Docusaurus/Next.js）的 <head> 里 prefetch 链接能占满 16K+ tokens，
    # 导致正文被 max_input_tokens 截断后 SLM 完全看不到 <main>。先做一遍 BS4
    # 清洗，把字符数压到 SLM 容易吃下的范围。
    _orig_chars = len(html_content)
    html_content = _strip_html_noise(html_content)
    print(
        f"[mineru-html] html cleaned {_orig_chars} -> {len(html_content)} chars",
        file=sys.stderr, flush=True,
    )

    # ===== monkey-patch caching_allocator_warmup =====
    # transformers ≥4.40 在 from_pretrained 内会预 alloc (mem_get_info(0) - 1.2GiB)
    # 用于 caching allocator 暖启动。Windows WDDM 下 mem_get_info 把 shared GPU memory
    # （系统 RAM）也计入 free，导致它尝试 alloc 远超物理显存 → OOM。
    # 这步是 loading 速度优化，不做也不影响正确性，仅模型 load 慢几秒。
    # 必须在 import transformers 模型之前 patch。可用 BB_MINERU_HTML_DISABLE_WARMUP=0 关闭。
    if os.environ.get("BB_MINERU_HTML_DISABLE_WARMUP", "1") != "0":
        try:
            import transformers.modeling_utils as _tmu

            _tmu.caching_allocator_warmup = lambda *a, **k: None
            print("[mineru-html] patched caching_allocator_warmup (Windows OOM workaround)",
                  file=sys.stderr, flush=True)
        except Exception as _e:
            print(f"[mineru-html] WARN: patch warmup failed: {_e}", file=sys.stderr, flush=True)

    # ===== 自实现 backend，绕开 mineru_html 自带 transformers backend 的 pipeline 二次 dispatch =====
    # mineru_html 官方 TransformersInferenceBackend 内部用了 `pipeline(model=..., device_map=...)`，
    # 把已 dispatch 的 model 再传一次 device_map 触发重复分配，在 16GB 显卡上必然 OOM
    # （实测 19GB allocated，单次 alloc 16GiB）。最小复现验证：直接 from_pretrained 加载只占 1.09GB。
    # 因此本函数只复用 mineru_html 的 prompt/parser（MinerUHTMLGeneric + MinerUHTMLConfig），
    # 推理后端走我们自己的 LeanBackend，规避包内 BUG。
    try:
        from mineru_html import MinerUHTMLConfig, MinerUHTMLGeneric
        from mineru_html.inference.base_backend import InferenceBackend, ModelResponse
        from mineru_html.base import DEFALUT_MODEL
    except ImportError as exc:
        raise RuntimeError(
            "未找到 `mineru_html` 包。请安装：pip install mineru_html"
        ) from exc

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    device_map = os.environ.get("BB_MINERU_HTML_DEVICE", "cuda:0")
    max_new_tokens = int(os.environ.get("BB_MINERU_HTML_MAX_TOKENS", str(16 * 1024)))
    dtype_str = os.environ.get("BB_MINERU_HTML_DTYPE", "float16")
    dtype = getattr(torch, dtype_str) if dtype_str != "auto" else "auto"
    gpu_budget = os.environ.get("BB_MINERU_HTML_GPU_MEM", "4GiB")

    class _LeanTransformersBackend(InferenceBackend):
        """直接 model.generate 推理，不走 pipeline，避免 mineru_html 默认 backend 的重复 dispatch。"""

        def __init__(self, model_path: str):
            super().__init__(max_context_window=256 * 1024, response_format="compact")
            self.model_path = model_path
            self._model = None
            self._tokenizer = None

        def setup_llm(self):
            if self._model is not None:
                return
            kwargs: Dict[str, Any] = {
                "trust_remote_code": True,
                "device_map": device_map,
                "dtype": dtype,
            }
            if isinstance(device_map, str) and device_map.startswith("cuda"):
                gpu_idx = int(device_map.split(":", 1)[1]) if ":" in device_map else 0
                kwargs["max_memory"] = {gpu_idx: gpu_budget, "cpu": "16GiB"}
            print(
                f"[mineru-html] loading model dtype={dtype_str} kwargs="
                f"{ {k: v for k, v in kwargs.items() if k != 'trust_remote_code'} }",
                file=sys.stderr, flush=True,
            )
            self._model = AutoModelForCausalLM.from_pretrained(self.model_path, **kwargs)
            self._model.eval()
            print(
                f"[mineru-html] loaded; gpu_alloc="
                f"{torch.cuda.memory_allocated(0) / 1e9:.2f} GB"
                if torch.cuda.is_available() else "[mineru-html] loaded (cpu)",
                file=sys.stderr, flush=True,
            )

        def get_tokenizer(self):
            if self._tokenizer is None:
                self._tokenizer = AutoTokenizer.from_pretrained(self.model_path)
            return self._tokenizer

        def generate(self, prompt_list, **kwargs):
            assert self._model is not None and self._tokenizer is not None
            # 16GB 显卡极限：bf16 hunyuan-0.5B + sdpa attention 大约支持 6K token prompt
            # 超长 prompt 会触发 attention O(n²) 显存爆炸（实测 16K prompt 单次 alloc 16+GiB OOM）。
            # 默认 6144，可用 BB_MINERU_HTML_MAX_INPUT 调小（牺牲尾部内容准确性）。
            max_input_tokens = int(os.environ.get("BB_MINERU_HTML_MAX_INPUT", "6144"))
            results: list[ModelResponse] = []
            for i, prompt in enumerate(prompt_list):
                # hunyuan tokenizer 会塞 token_type_ids，但 hunyuan model 不接受 → 显式排除
                enc = self._tokenizer(prompt, return_tensors="pt")
                input_ids = enc["input_ids"].to(self._model.device)
                attn = enc.get("attention_mask")
                attn = attn.to(self._model.device) if attn is not None else None
                if input_ids.shape[-1] > max_input_tokens:
                    # 注意：mineru_html 的 prompt 末尾固定带 `</body></html><｜hy_Assistant｜><think>`
                    # 这个 SLM 终结指令——丢了它模型不知道何时停止，会退化成 `1main2main...3050main`
                    # 这种 token 重复循环（实测尾部砍掉后输出到 3050main 远超实际 254 个 item）。
                    # 因此采用 head + tail 截断：保留头部 HTML 大部分 + 尾部 ~128 tokens 的终结指令。
                    # tail_keep=128 比实际终结串（≈ 20 tokens）大很多，给若干末尾元素保留余量。
                    tail_keep = min(128, max_input_tokens // 8)
                    head_keep = max_input_tokens - tail_keep
                    print(
                        f"[mineru-html] WARN: prompt {input_ids.shape[-1]} > "
                        f"max_input_tokens {max_input_tokens}, head+tail truncating "
                        f"(head={head_keep} + tail={tail_keep})",
                        file=sys.stderr, flush=True,
                    )
                    head = input_ids[:, :head_keep]
                    tail = input_ids[:, -tail_keep:]
                    input_ids = torch.cat([head, tail], dim=-1)
                    if attn is not None:
                        attn_head = attn[:, :head_keep]
                        attn_tail = attn[:, -tail_keep:]
                        attn = torch.cat([attn_head, attn_tail], dim=-1)
                pad_id = self._tokenizer.pad_token_id
                if pad_id is None:
                    pad_id = self._tokenizer.eos_token_id
                t0 = _time.perf_counter()
                gpu_before = (
                    torch.cuda.memory_allocated(0) / 1e9
                    if torch.cuda.is_available() else 0.0
                )
                print(
                    f"[mineru-html] gen[{i+1}/{len(prompt_list)}] prompt_tokens={input_ids.shape[-1]} "
                    f"max_new={max_new_tokens} gpu_alloc={gpu_before:.2f}GB",
                    file=sys.stderr, flush=True,
                )
                with torch.no_grad():
                    out = self._model.generate(
                        input_ids=input_ids,
                        attention_mask=attn,
                        max_new_tokens=max_new_tokens,
                        do_sample=False,
                        pad_token_id=pad_id,
                        eos_token_id=self._tokenizer.eos_token_id,
                    )
                new_tokens = out[0, input_ids.shape[-1]:]
                text = self._tokenizer.decode(new_tokens, skip_special_tokens=True)
                gpu_after = (
                    torch.cuda.memory_allocated(0) / 1e9
                    if torch.cuda.is_available() else 0.0
                )
                print(
                    f"[mineru-html] gen[{i+1}] done in {_time.perf_counter()-t0:.1f}s "
                    f"new_tokens={new_tokens.shape[-1]} gpu_alloc={gpu_after:.2f}GB",
                    file=sys.stderr, flush=True,
                )
                results.append(ModelResponse(generated_text=text))
                # 调试支持：BB_MINERU_HTML_DUMP_DIR 设置时把 raw output 落盘，便于排查
                # SLM 输出格式漂移、parser 解析失败等场景。生产关闭。
                _dump_dir = os.environ.get("BB_MINERU_HTML_DUMP_DIR", "")
                if _dump_dir:
                    try:
                        from pathlib import Path as _P

                        _dp = _P(_dump_dir)
                        _dp.mkdir(parents=True, exist_ok=True)
                        (_dp / f"slm_out_{i}.txt").write_text(text, encoding="utf-8")
                        (_dp / f"slm_prompt_{i}.txt").write_text(prompt, encoding="utf-8")
                        print(f"[mineru-html] dumped raw output -> {_dp}/slm_out_{i}.txt",
                              file=sys.stderr, flush=True)
                    except Exception as _de:
                        print(f"[mineru-html] WARN: dump failed: {_de}", file=sys.stderr, flush=True)
                # 每条生成完释放显存碎片，避免多 chunk 累积
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            return results

    config = MinerUHTMLConfig(
        # use_fall_back='empty'：MinerU SLM 失败时返回空字符串，由本函数下方
        # `if not main_content: raise RuntimeError(...)` 抛错，实现 fail-fast。
        # 不用 'trafilatura' 降级——降级会拿到无结构 plain text，让下游误以为成功但质量极差。
        use_fall_back="empty",
        early_load=True,
        output_format="mm_md",
    )
    backend = _LeanTransformersBackend(DEFALUT_MODEL)
    backend.response_format = config.response_format
    extractor = MinerUHTMLGeneric(llm=backend, config=config)
    try:
        results = extractor.process(html_content)
        # 调试期 dump：把完整 case 状态落到 BB_MINERU_HTML_DUMP_DIR，便于排查
        # SLM 输出 → main_html → main_content 链路上每步的产物。
        _dump_dir = os.environ.get("BB_MINERU_HTML_DUMP_DIR", "")
        if _dump_dir and results:
            try:
                from pathlib import Path as _P

                _dp = _P(_dump_dir)
                _dp.mkdir(parents=True, exist_ok=True)
                _r = results[0]
                _info = {
                    "has_output_data": _r.output_data is not None,
                    "main_html_len": len(_r.output_data.main_html) if _r.output_data and _r.output_data.main_html else 0,
                    "main_content_len": len(_r.output_data.main_content) if _r.output_data and _r.output_data.main_content else 0,
                    "error": str(_r.error) if hasattr(_r, "error") and _r.error else None,
                    "case_id": getattr(_r, "case_id", None),
                }
                (_dp / "case_state.json").write_text(json.dumps(_info, ensure_ascii=False, indent=2), encoding="utf-8")
                if _r.output_data and _r.output_data.main_html:
                    (_dp / "main_html.html").write_text(_r.output_data.main_html, encoding="utf-8")
                if _r.output_data and _r.output_data.main_content:
                    (_dp / "main_content.md").write_text(_r.output_data.main_content, encoding="utf-8")
                print(f"[mineru-html] dumped case state -> {_dp}/case_state.json {_info}",
                      file=sys.stderr, flush=True)
            except Exception as _de:
                print(f"[mineru-html] WARN: case dump failed: {_de}", file=sys.stderr, flush=True)
        if not results or not results[0].output_data or not results[0].output_data.main_content:
            raise RuntimeError("MinerU-HTML 提取主体为空（SLM 未识别出 main 元素）")
        return results[0].output_data.main_content
    finally:
        if hasattr(extractor, "llm") and hasattr(extractor.llm, "cleanup"):
            extractor.llm.cleanup()


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


def convert_code(input_path: Path) -> str:
    """Read a source-code file and wrap it in a fenced Markdown code block.

    The language identifier is inferred from the file extension. A short header
    line records the original file name so downstream chunks retain provenance
    even when split across multiple chunks.
    """
    raw = input_path.read_text(encoding="utf-8-sig", errors="replace")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    language = _code_language_for(input_path)
    # If the code itself contains ``` we use four backticks for the outer fence
    # so the body is not prematurely terminated.
    outer_fence = "````" if "```" in raw else "```"
    header = f"# 源码：{input_path.name}\n\n"
    return f"{header}{outer_fence}{language}\n{raw}\n{outer_fence}\n"


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
    keep_mineru_work: bool = True,
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
        # Keep _mineru_work by default for traceability (images, JSON, etc.)
        # Only clean up if user explicitly passes --no-keep-mineru-work
        if not keep_mineru_work:
            shutil.rmtree(work_dir, ignore_errors=True)
    elif backend == "mineru_html":
        body = convert_via_mineru_html(input_path)
    elif backend == "pandoc":
        body = convert_via_pandoc(input_path)
    elif backend == "plain":
        body = convert_plain_text(input_path)
    elif backend == "markdown":
        body = convert_markdown(input_path)
    elif backend == "code":
        body = convert_code(input_path)
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


def _check_mineru_html_available() -> dict[str, Any]:
    """检查 mineru_html 包是否可导入。"""
    try:
        import mineru_html
        return {"available": True, "version": getattr(mineru_html, "__version__", "unknown"), "error": None}
    except ImportError:
        return {"available": False, "version": None, "error": "`mineru_html` 未安装，pip install mineru_html"}


def check_runtime(mineru_bin: str | None = None) -> dict[str, Any]:
    return {
        "mineru": _check_command(resolve_mineru_bin(mineru_bin), "--version"),
        "mineru_html": _check_mineru_html_available(),
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
                keep_mineru_work=not args.no_keep_mineru_work,
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
    core_ok = report["mineru"]["available"] and report["pandoc"]["available"]
    if not core_ok:
        print(
            "\n提示：MinerU 处理 PDF/DOCX/PPTX/XLSX/图片；pandoc 处理 LaTeX (.tex)。"
            "\n  - 安装 MinerU: pip install 'mineru[pipeline]>=3.1,<4.0'"
            "\n  - 安装 pandoc: 参考 https://pandoc.org/installing.html",
            file=sys.stderr,
        )
    if not report["mineru_html"]["available"]:
        print(
            "\n提示：MinerU-HTML 处理 HTML 文件。"
            "\n  - 安装: pip install mineru_html"
            "\n  - GPU 加速: pip install mineru_html[vllm]",
            file=sys.stderr,
        )
    return 0 if core_ok else 3


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
        "--no-keep-mineru-work",
        action="store_true",
        help="Delete MinerU's intermediate work dir (_mineru_work) after conversion. Default: keep for traceability.",
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
