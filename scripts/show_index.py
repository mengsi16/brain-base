# -*- coding: utf-8 -*-
"""列出 data/crystallized/index.json 里所有 skill 的关键字段。"""
import json
from pathlib import Path

p = Path("data/crystallized/index.json")
if not p.exists():
    print("[error] index.json 不存在")
    raise SystemExit(1)

d = json.loads(p.read_text(encoding="utf-8"))
skills = d.get("skills", [])
print(f"index.json: {len(skills)} 个 skill")
print("=" * 100)
for s in skills:
    print(f"  - {s['skill_id']:<35} "
          f"layer={s['layer']:<5} "
          f"scenario={s['scenario']:<12} "
          f"vs={s.get('value_score', 0):.2f}")
    print(f"      entities         : {s.get('entities', [])}")
    print(f"      trigger_keywords : {s.get('trigger_keywords', [])}")
    print(f"      desc             : {s.get('description', '')[:80]}")
    print()
