"""
bin/doc-converter.py 的薄封装。

doc-converter.py 命令格式：
- convert --input <file...> --output-dir <dir>
- inspect --input <file...>
- check-runtime [--mineru-bin <path>]

调用频率低（一次入库一次），用 subprocess 不需要 import 优化。
fail-fast：失败抛 RuntimeError，不吞错（CLAUDE.md 硬约束 25）。

`convert_html_to_markdown` 走 docker compose exec 路由到容器内 doc-converter.py：
- Windows PyTorch 不带 flash kernel，hunyuan attention 在 16K prompt 上 prefill OOM
- Linux PyTorch（cu124）默认带 flash + mem_efficient kernel，4060Ti SM89 完全支持
  → 容器内 mineru-html 能吃 16K-32K prompt 不 OOM
- 主机 bge-m3 与容器 mineru-html 共享同一张物理 4060Ti，但通过子进程边界天然错峰
- 临时 HTML/MD 经 ./data/temp/<uid>/ 主机目录中转（已挂 /app/data/temp 进容器）

其他命令（convert_document / inspect_document / check_runtime）仍走主机 python，
因为 mineru[pipeline] PDF 解析不在本批改造范围（T11）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BIN_DIR = _PROJECT_ROOT / "bin"
_DOC_CONVERTER = _BIN_DIR / "doc-converter.py"

# Docker 容器内路径前缀（基于 docker-compose.yml 的 volume 挂载）：
#   主机 ./bin            → 容器 /app/bin
#   主机 ./data           → 容器 /app/data
_CONTAINER_DOC_CONVERTER = "/app/bin/doc-converter.py"
_DATA_TEMP = _PROJECT_ROOT / "data" / "temp"

# Docker compose 服务名（与 docker-compose.yml 里 services.brain-base-worker 一致）
_DOCKER_SERVICE = "brain-base-worker"


def _to_container_path(host_path: Path) -> str:
    """把主机绝对路径转成容器路径（基于 ./<x> → /app/<x> 挂载）。

    要求 host_path 在 _PROJECT_ROOT 下，否则抛错（fail-fast）。
    """
    rel = host_path.resolve().relative_to(_PROJECT_ROOT)
    return f"/app/{rel.as_posix()}"


def _from_container_path(container_path: str) -> Path:
    """把容器路径 /app/data/... 转成主机路径。

    用于解析 doc-converter 输出 JSON 里的 raw_path 字段。
    """
    if container_path.startswith("/app/"):
        rel = container_path[len("/app/"):]
        return _PROJECT_ROOT / rel
    return Path(container_path)


def convert_html_to_markdown(html: str, *, timeout: float = 300.0, verbose: bool = False) -> str:
    """把 HTML 字符串经容器内 MinerU-HTML 转成高质量 markdown 字符串。

    路径设计（主机 ↔ 容器）：
        主机 ./data/temp/<uid>/in.html       <→ 容器 /app/data/temp/<uid>/in.html
        主机 ./data/temp/<uid>/out/...       <→ 容器 /app/data/temp/<uid>/out/...
        主机 ./data/temp/<uid>/uploads/...   <→ 容器 /app/data/temp/<uid>/uploads/...

    步骤：
    1. 主机写 HTML 到 ./data/temp/<uid>/in.html（uid = uuid.uuid4().hex[:12]）
    2. subprocess 调 docker compose exec -T <服务> python /app/bin/doc-converter.py convert ...
    3. 读容器返回的 raw_path（容器路径），转主机路径，读 markdown
    4. finally 清理整个 ./data/temp/<uid>/

    通过 BB_MINERU_HTML_* 环境变量传给容器；MinerU 失败抛 RuntimeError 由调用方处理。
    """
    if not html or not html.strip():
        return ""
    if not _DOC_CONVERTER.exists():
        raise FileNotFoundError(f"未找到 doc-converter.py: {_DOC_CONVERTER}")

    _DATA_TEMP.mkdir(parents=True, exist_ok=True)
    uid = uuid.uuid4().hex[:12]
    work_dir = _DATA_TEMP / uid
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        input_path = work_dir / "in.html"
        output_dir = work_dir / "out"
        uploads_dir = work_dir / "uploads"
        input_path.write_text(html, encoding="utf-8")

        # GPU 配置传给容器：Linux PyTorch + flash kernel 下显存压力远低于 Windows。
        # 默认值面向 4060Ti 16GB（与主机 bge-m3 ~1.5GB 共享物理卡，错峰使用）：
        # - max_input=16384：Linux flash kernel 下 16K prompt prefill 显存 O(n) 而非 O(n²)
        # - max_new_tokens=8192：长文档充裕，主体内容平均 4-6K
        # - dtype=float16：匹配模型权重
        # - GPU_MEM=10GiB：留 6GB 给主机 bge-m3 + 系统其他占用
        env_pairs = {
            "BB_MINERU_HTML_DEVICE": os.environ.get("BB_MINERU_HTML_DEVICE", "cuda:0"),
            "BB_MINERU_HTML_GPU_MEM": os.environ.get("BB_MINERU_HTML_GPU_MEM", "10GiB"),
            "BB_MINERU_HTML_MAX_INPUT": os.environ.get("BB_MINERU_HTML_MAX_INPUT", "16384"),
            "BB_MINERU_HTML_MAX_TOKENS": os.environ.get("BB_MINERU_HTML_MAX_TOKENS", "8192"),
            "BB_MINERU_HTML_DTYPE": os.environ.get("BB_MINERU_HTML_DTYPE", "float16"),
        }
        env_args: list[str] = []
        for k, v in env_pairs.items():
            env_args.extend(["-e", f"{k}={v}"])

        cmd = [
            "docker", "compose", "exec", "-T",
            *env_args,
            _DOCKER_SERVICE,
            "python", _CONTAINER_DOC_CONVERTER, "convert",
            "--input", _to_container_path(input_path),
            "--output-dir", _to_container_path(output_dir),
            "--uploads-dir", _to_container_path(uploads_dir),
        ]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            cwd=str(_PROJECT_ROOT),  # docker compose 要在含 docker-compose.yml 的目录跑
        )
        if verbose and proc.stderr:
            sys.stderr.write(proc.stderr)
            sys.stderr.flush()
        if proc.returncode not in (0, 2):
            raise RuntimeError(
                f"docker exec doc-converter convert 异常 rc={proc.returncode} "
                f"stderr={proc.stderr[:500]}"
            )

        try:
            payload = json.loads(proc.stdout.strip() or "{}")
        except json.JSONDecodeError:
            payload = {}
        results = payload.get("results", []) or []
        if not results:
            errors = payload.get("errors") or []
            raise RuntimeError(
                f"doc-converter 未返回结果 errors={errors} stderr={proc.stderr[:300]}"
            )

        # 容器内 raw_path → 主机路径
        container_raw = results[0].get("raw_path", "")
        host_raw = _from_container_path(container_raw)
        if not host_raw.is_file():
            raise RuntimeError(
                f"doc-converter 输出文件不存在: {host_raw} (container={container_raw})"
            )

        return host_raw.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _run(args: list[str], timeout: float = 600) -> dict[str, Any]:
    """统一调用 doc-converter.py 并解析 JSON 输出。"""
    if not _DOC_CONVERTER.exists():
        raise FileNotFoundError(f"未找到 doc-converter.py: {_DOC_CONVERTER}")
    proc = subprocess.run(
        [sys.executable, str(_DOC_CONVERTER), *args],
        capture_output=True,
        timeout=timeout,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode not in (0, 2, 3):
        # 0 成功；2 部分失败；3 runtime check 失败（仍有 JSON 输出）
        raise RuntimeError(
            f"doc-converter {args[0]} 异常 rc={proc.returncode} stderr={proc.stderr[:500]}"
        )
    stdout = proc.stdout.strip()
    if not stdout:
        return {"results": [], "errors": ["doc-converter 无输出"]}
    return json.loads(stdout)


def convert_document(
    inputs: list[Path] | None = None,
    input_dir: Path | None = None,
    output_dir: Path | None = None,
    upload_date: str | None = None,
    mineru_bin: str | None = None,
    timeout: float = 600,
) -> dict[str, Any]:
    """转换一个或一批文件到 Markdown。返回 doc-converter 原始 JSON。"""
    args = ["convert"]
    if inputs:
        args.append("--input")
        args.extend(str(p) for p in inputs)
    elif input_dir:
        args.extend(["--input-dir", str(input_dir)])
    else:
        raise ValueError("必须提供 inputs 或 input_dir")
    if output_dir:
        args.extend(["--output-dir", str(output_dir)])
    if upload_date:
        args.extend(["--upload-date", upload_date])
    if mineru_bin:
        args.extend(["--mineru-bin", mineru_bin])
    return _run(args, timeout=timeout)


def inspect_document(
    inputs: list[Path] | None = None,
    input_dir: Path | None = None,
) -> dict[str, Any]:
    """dry-run：只检测格式与建议 doc_id，不做转换。"""
    args = ["inspect"]
    if inputs:
        args.append("--input")
        args.extend(str(p) for p in inputs)
    elif input_dir:
        args.extend(["--input-dir", str(input_dir)])
    else:
        raise ValueError("必须提供 inputs 或 input_dir")
    return _run(args, timeout=60)


def check_doc_converter_runtime(mineru_bin: str | None = None) -> dict[str, Any]:
    """检查 MinerU / pandoc 是否可用。"""
    args = ["check-runtime"]
    if mineru_bin:
        args.extend(["--mineru-bin", mineru_bin])
    return _run(args, timeout=30)
