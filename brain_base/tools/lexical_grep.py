"""本地 chunks/raw 字面 grep（T23 重构：AND gate 语义）。

对一组 keywords 在本地 ``data/docs/chunks/`` + ``data/docs/raw/`` 做 AND grep
命中判断——同一文件必须包含**所有** keywords 才算命中 1 次。

设计：

- **AND 语义而非 OR**：T22 之前是每个 entity 独立 OR 计数（一个文件只要含
  任一 entity 就算命中），无法区分"openclaw 在 A 文件、卸载在 B 文件"这种
  假命中。改成 AND 后，子问题 "如何卸载 openclaw" 用 ``["openclaw", "卸载"]``
  必须同文件共现才计 1，0 命中直接走外检。
- **gate 语义**：返回值 0 → 调用方设置 ``needs_get_info=True``，跳过 Milvus
  直接外检；> 0 → 进入第二段 fanout 跑 Milvus。
- **pure Python，不调 ripgrep 子进程**：当前知识库规模 < 100 文件，顺序读
  + substring 速度毫秒级。未来文件量爆炸（>1万）再切 ripgrep。
- **fail-silent**：单文件读失败跳过，不抛错；目录不存在返回 0；从不阻断
  主流程。
- **case-insensitive**：所有匹配 lowercase。
"""

from __future__ import annotations

from pathlib import Path

# chunks 和 raw 目录都扫；二者职责：
# - chunks：分块文件，适合精确定位主题片段
# - raw：完整原文，适合确认上下文
DEFAULT_CHUNKS_DIR = Path("data/docs/chunks")
DEFAULT_RAW_DIR = Path("data/docs/raw")

# 单次扫描文件数硬保护：防止未来知识库爆炸时把 grep 阶段拖死
DEFAULT_MAX_FILES_SCANNED = 5000


def grep_keywords_and(
    keywords: list[str],
    chunks_dir: Path | None = None,
    raw_dir: Path | None = None,
    max_files_scanned: int = DEFAULT_MAX_FILES_SCANNED,
) -> int:
    """对本地 chunks + raw 做 keywords AND grep，返回命中文件数。

    AND 语义：同一文件必须包含 ``keywords`` 列表中的**所有**词才算命中 1 次；
    一个文件即使同时含全部关键词也只计 1，多次出现不重复计数。

    T30：本工具仍保留可用，但 PIPE1 ``prep_one_subquery`` 已切换到 milvus
    ``text_search`` (sparse gate top-3 avg + 阈值 0.20) 取代字面 AND grep。
    本函数现在仅用于：CLI 调试 / eval 脚本 / 未来手动诊断。

    参数：
        keywords: 要 AND 命中的关键词列表（手工提供或由其它脚本拆分；
            通常 2-5 个：主实体词 + 动作/属性词）
        chunks_dir: chunks 目录路径，None 用默认 data/docs/chunks
        raw_dir: raw 目录路径，None 用默认 data/docs/raw
        max_files_scanned: 单次最多扫描文件数（硬保护）

    返回：
        命中文件数（int）。0 = 本地无文件同时包含所有关键词，调用方据此
        设置 ``needs_get_info=True`` 走外检。

    fail-silent：
    - 目录不存在 → 跳过该目录
    - 单文件读失败（如编码异常）→ 跳过该文件
    - keywords 为空 → 返回 0（不报错）
    - 不抛异常
    """
    if not keywords:
        return 0

    # 归一化：lowercase + 去空字符串
    keywords_lower = [k.lower() for k in keywords if k and k.strip()]
    if not keywords_lower:
        return 0

    chunks_dir = chunks_dir if chunks_dir is not None else DEFAULT_CHUNKS_DIR
    raw_dir = raw_dir if raw_dir is not None else DEFAULT_RAW_DIR

    # 收集要扫的文件（chunks 优先，其次 raw）
    files_to_scan: list[Path] = []
    for d in (chunks_dir, raw_dir):
        if d.is_dir():
            files_to_scan.extend(sorted(d.glob("*.md")))
            if len(files_to_scan) >= max_files_scanned:
                files_to_scan = files_to_scan[:max_files_scanned]
                break

    if not files_to_scan:
        return 0

    hit_files = 0
    for path in files_to_scan:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except (OSError, ValueError):
            continue
        # AND：所有关键词必须同时出现在同一文件
        if all(k in text for k in keywords_lower):
            hit_files += 1

    return hit_files
