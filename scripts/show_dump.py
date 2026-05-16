# -*- coding: utf-8 -*-
"""读 cli ask --state-dump 产出的 JSON，把关键字段（含完整 answer）格式化输出。

用法：python scripts/show_dump.py tests/e2e/dump_t41_turn1.json
"""

import json
import sys
from pathlib import Path


def show(path: str) -> None:
    p = Path(path)
    if not p.exists():
        print(f"[error] dump 文件不存在: {path}", file=sys.stderr)
        sys.exit(1)
    d = json.loads(p.read_text(encoding="utf-8"))

    print("=" * 78)
    print(f"  state-dump: {path}")
    print("=" * 78)

    # 链路关键字段
    print("\n--- T37 / 上下文改写 ---")
    print(f"  question              : {d.get('question', '')!r}")
    print(f"  normalized_query      : {d.get('normalized_query', '')!r}")
    print(f"  contextualized_query  : {d.get('contextualized_query')!r}")
    print(f"  rewrite_reason        : {d.get('rewrite_reason', '')!r}")

    print("\n--- T38 / 时效 + 子问题 ---")
    print(f"  time_sensitive        : {d.get('time_sensitive', False)}")
    print(f"  time_range            : {d.get('time_range')}")
    print(f"  sub_questions         : {d.get('sub_questions', [])}")
    print(f"  sub_lexical_scores    : {d.get('sub_lexical_scores', [])}")
    print(f"  gi_trigger_reasons    : {d.get('gi_trigger_reasons', [])}")
    decisions = d.get("gi_decisions", [])
    if decisions:
        print(f"  gi_decisions          :")
        for x in decisions:
            print(
                f"    - sub#{x.get('sub_idx')} triggered={x.get('triggered')} "
                f"reason={x.get('reason')!r} score={x.get('sparse_score'):.3f}"
            )

    print("\n--- 检索 / 证据 ---")
    print(f"  search_keywords       : {d.get('search_keywords', [])}")
    print(f"  ingested_count        : {d.get('ingested_count', 0)}")
    sub_ev = d.get("sub_evidence", [])
    print(f"  sub_evidence          : {len(sub_ev)} 个子问题")
    for i, s in enumerate(sub_ev):
        ev = s.get("evidence", [])
        print(f"    sub#{i} {len(ev)} 条证据")
        for j, e in enumerate(ev[:5]):
            src = e.get("source", "?")
            txt = e.get("text", "") or e.get("content", "")
            print(f"      [{j}] src={src!r} text='{txt[:120]}{'...' if len(txt) > 120 else ''}'")

    print("\n--- 固化层 ---")
    print(f"  crystallized_status   : {d.get('crystallized_status', 'miss')}")
    print(f"  crystallized_skill_id : {d.get('crystallized_skill_id')}")
    cr = d.get("crystallize_result", {})
    if cr:
        print(f"  crystallize_result    : status={cr.get('status')} "
              f"skill_id={cr.get('skill_id')} layer={cr.get('layer')}")

    print("\n--- 完整 answer（不截断）---")
    print("─" * 78)
    print(d.get("answer", "(空)"))
    print("─" * 78)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python scripts/show_dump.py <dump.json>", file=sys.stderr)
        sys.exit(1)
    show(sys.argv[1])
