"""
priority.json 与 keywords.db 的读写工具。

职责（CLAUDE.md 硬约束 12）：仅供 update-priority 节点写入；其他 skill
不应通过本模块写入。读取面向所有节点开放（QA 时可读 priority 加权）。

priority.json 结构（约定）：
    {
      "sites": {"<host>": {"priority": float, "last_seen": "YYYY-MM-DD"}},
      "keywords": {"<keyword>": int_count}
    }

keywords.db（SQLite）schema（约定）：
    CREATE TABLE keywords (
      keyword TEXT PRIMARY KEY,
      count   INTEGER NOT NULL DEFAULT 0,
      last_seen TEXT NOT NULL
    )
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from brain_base.nodes._atomic import atomic_write_json

_DEFAULT_PRIORITY_PATH = Path("data/priority.json")
_DEFAULT_KEYWORDS_DB = Path("data/keywords.db")


def read_priority_json(path: Path | None = None) -> dict[str, Any]:
    """读取 priority.json。文件不存在返回空骨架。"""
    p = Path(path) if path else _DEFAULT_PRIORITY_PATH
    if not p.exists():
        return {"sites": {}, "keywords": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def write_priority_json(data: dict[str, Any], path: Path | None = None) -> None:
    """原子写 priority.json。仅 update-priority 节点应调用。"""
    p = Path(path) if path else _DEFAULT_PRIORITY_PATH
    atomic_write_json(p, data)


def update_site_priority(
    host: str,
    delta: float,
    path: Path | None = None,
) -> float:
    """对站点 host 的 priority 增量更新，返回新值。"""
    data = read_priority_json(path)
    sites = data.setdefault("sites", {})
    entry = sites.setdefault(host, {"priority": 0.0, "last_seen": ""})
    entry["priority"] = float(entry.get("priority", 0.0)) + float(delta)
    entry["last_seen"] = date.today().isoformat()
    write_priority_json(data, path)
    return entry["priority"]


def _ensure_keywords_db(db_path: Path) -> sqlite3.Connection:
    """连接 keywords.db，按需建表。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS keywords (
            keyword   TEXT PRIMARY KEY,
            count     INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT NOT NULL
        )
        """
    )
    return conn


def update_keywords_db(
    keywords: list[str],
    db_path: Path | None = None,
) -> dict[str, int]:
    """对一批 keyword 增量计数。返回 {keyword: new_count}。"""
    if not keywords:
        return {}
    p = Path(db_path) if db_path else _DEFAULT_KEYWORDS_DB
    today = date.today().isoformat()
    result: dict[str, int] = {}
    with _ensure_keywords_db(p) as conn:
        for kw in keywords:
            kw_clean = kw.strip()
            if not kw_clean:
                continue
            conn.execute(
                """
                INSERT INTO keywords (keyword, count, last_seen)
                VALUES (?, 1, ?)
                ON CONFLICT(keyword) DO UPDATE SET
                    count = count + 1,
                    last_seen = excluded.last_seen
                """,
                (kw_clean, today),
            )
            row = conn.execute(
                "SELECT count FROM keywords WHERE keyword = ?",
                (kw_clean,),
            ).fetchone()
            if row:
                result[kw_clean] = int(row[0])
        conn.commit()
    return result


def read_keyword_count(keyword: str, db_path: Path | None = None) -> int:
    """读取单个 keyword 的累计计数。不存在返回 0。"""
    p = Path(db_path) if db_path else _DEFAULT_KEYWORDS_DB
    if not p.exists():
        return 0
    with sqlite3.connect(str(p)) as conn:
        row = conn.execute(
            "SELECT count FROM keywords WHERE keyword = ?",
            (keyword.strip(),),
        ).fetchone()
        return int(row[0]) if row else 0
