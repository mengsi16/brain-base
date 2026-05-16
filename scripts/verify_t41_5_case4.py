# -*- coding: utf-8 -*-
"""T41.5 hotfix 验证脚本：Case 4 (FastAPI) 单 turn 跑一次，验证：

1. 不再抛 NotImplementedError: Cannot copy out of meta tensor
2. bge-m3 加载日志（"loading existing colbert_linear"）只出现 1 次（缓存生效）
3. answer 非空，链路完整跑通

不复用 tests/e2e/test_qa_full_pipeline_e2e.py 的全 4 case（~28 分钟太长），
只跑 Case 4 Turn 1（~3-4 分钟），覆盖原 bug 现场。
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

# 项目根入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain_base.cli import _build_llm_from_env
from brain_base.graphs.qa_graph import QaGraph


CASE = {
    "id": "fastapi",
    "question": "FastAPI 的核心特性是什么？",
}

DUMP_DIR = Path(__file__).parent.parent / "tests" / "e2e" / "verify_t41_5"


class _BgeLoadCounter(logging.Handler):
    """统计含 'colbert_linear' 关键字的日志行数（=bge-m3 重型加载次数）。"""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.count = 0
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        if "colbert_linear" in msg or "sparse_linear" in msg:
            self.count += 1
            self.lines.append(f"[{record.name}] {msg}")


def main() -> int:
    DUMP_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("  T41.5 hotfix 验证：Case 4 (FastAPI) 单 turn")
    print("=" * 78)

    # 安装 bge 加载日志计数器（在 root logger 上抓）
    counter = _BgeLoadCounter()
    logging.getLogger().addHandler(counter)
    # 也设置 root logger 至 INFO 级别，否则 FlagEmbedding 的 INFO 行会被过滤
    logging.getLogger().setLevel(logging.INFO)

    print("\n[初始化] 构建 LLM + QaGraph...")
    t_init = time.time()
    llm = _build_llm_from_env()
    qa = QaGraph(llm=llm)
    print(f"[初始化] LLM={type(llm).__name__} ({time.time() - t_init:.1f}s)")

    print(f"\n[Turn 1] 用户: {CASE['question']}")
    t0 = time.time()
    err: str | None = None
    state: dict = {}
    try:
        state = qa.run(question=CASE["question"], conversation_history=None)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    elapsed = time.time() - t0

    # 落盘 state
    state_path = DUMP_DIR / f"state_{int(time.time())}.json"
    try:
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"  ⚠️  state 落盘失败: {e}")

    # ---- 汇总 ----
    print(f"\n{'═' * 78}")
    print("  验证结果")
    print(f"{'═' * 78}")
    print(f"  耗时: {elapsed:.1f}s")
    print(f"  错误: {err or '无'}")

    answer = state.get("answer", "") or ""
    print(f"  answer 长度: {len(answer)}")
    print(f"  bge-m3 加载日志条数（含 colbert_linear/sparse_linear）: {counter.count}")
    if counter.lines:
        print("  加载日志详情:")
        for line in counter.lines:
            print(f"    - {line}")

    print(f"\n  state 落盘: {state_path}")

    # ---- 验收判定 ----
    print(f"\n{'─' * 78}")
    pass_no_meta_err = err is None or "meta tensor" not in (err or "")
    # 缓存生效：1 次（首次加载）；最多容忍 2 次（首次 + 某种竞态首调）。
    # 原 bug 状态下会出现 N 次（N = sub-questions 数，~3-6 次）。
    pass_cache = counter.count <= 2
    pass_answer = bool(answer.strip())

    print(f"  ✅ 不抛 meta tensor 错误: {pass_no_meta_err}")
    print(f"  ✅ bge 加载只出现 ≤2 次（缓存生效）: {pass_cache} (count={counter.count})")
    print(f"  ✅ answer 非空: {pass_answer}")

    all_pass = pass_no_meta_err and pass_cache and pass_answer
    print(f"\n  总体: {'✅ T41.5 hotfix 验证通过' if all_pass else '❌ 验证失败'}")

    if not all_pass:
        # 输出 answer 头 1000 字便于诊断
        print(f"\n  answer 片段:")
        print(answer[:1000])

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
