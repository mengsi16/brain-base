# -*- coding: utf-8 -*-
"""完整打印 e2e_results.json：每 case 的 turn1+turn2 完整 answer，不截断。"""
import json
import sys
from pathlib import Path

if len(sys.argv) < 2:
    print("usage: python scripts/show_e2e_full.py <e2e_results.json>")
    sys.exit(1)

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))

print("=" * 80)
print(f"  e2e_results.json  cases={len(data)}")
print("=" * 80)

for r in data:
    cid = r.get("id", "?")
    name = r.get("name", "")
    print("\n" + "█" * 80)
    print(f"  [{cid}]  {name}")
    print("█" * 80)
    print(f"  T37={r.get('t37_passed', '?')}  reason={r.get('t37_reason', '')}")
    if "t38_passed" in r:
        print(f"  T38={r.get('t38_passed', '?')}")
    if r.get("turn1_error"):
        print(f"  ❌ Turn1 error: {r['turn1_error']}")
    if r.get("turn2_error"):
        print(f"  ❌ Turn2 error: {r['turn2_error']}")

    turns = r.get("turns", [])
    for i, t in enumerate(turns):
        role = t.get("role", "?")
        txt = t.get("text", "")
        elapsed = t.get("elapsed")

        if role == "user":
            print(f"\n──── 用户 ────────────────────────────────────────────────────")
            print(txt)
        else:  # ai
            head = f"\n──── AI ({elapsed:.1f}s, len={len(txt)}) ──────────────────────────────────"
            print(head)
            print(txt if txt else "(空)")
            print("─" * 70)
