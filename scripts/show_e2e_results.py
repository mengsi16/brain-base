# -*- coding: utf-8 -*-
"""列出 e2e_results.json 总览：每 case 的 T37/T38 验证结果 + 错误。"""
import json
import sys
from pathlib import Path

if len(sys.argv) < 2:
    print("usage: python scripts/show_e2e_results.py <e2e_results.json>")
    sys.exit(1)

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
print(f"cases={len(data)}")
print("=" * 100)
for r in data:
    cid = r.get("id", "?")
    name = r.get("name", "")
    t37 = r.get("t37_passed", None)
    t37r = r.get("t37_reason", "")
    t38 = r.get("t38_passed", None)
    err1 = r.get("turn1_error", "")
    err2 = r.get("turn2_error", "")
    turns = r.get("turns", [])

    print(f"\n[{cid}] {name}")
    print(f"  T37={t37} reason={t37r}")
    if "t38_passed" in r:
        print(f"  T38={t38}")
    if err1:
        print(f"  ❌ Turn1 error: {err1}")
    if err2:
        print(f"  ❌ Turn2 error: {err2}")
    if turns:
        for i, t in enumerate(turns):
            role = t.get("role")
            txt = t.get("text", "")
            elapsed = t.get("elapsed")
            head = f"  Turn[{i}] {role}"
            if elapsed is not None:
                head += f" ({elapsed:.1f}s)"
            head += f" len={len(txt)}"
            print(head)
            print(f"    {txt[:180]}{'...' if len(txt) > 180 else ''}")
