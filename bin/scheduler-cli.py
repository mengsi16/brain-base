#!/usr/bin/env python3
"""定时调度器"""

import json
import sqlite3
import os
from datetime import datetime, timedelta
from typing import Dict, Any


def load_priority(file_path: str = "data/priority.json") -> Dict[str, Any]:
    """加载优先级配置"""
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_priority(config: Dict[str, Any], file_path: str = "data/priority.json"):
    """保存优先级配置"""
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def check_and_update(file_path: str = "data/priority.json") -> bool:
    """检查是否需要更新"""
    config = load_priority(file_path)
    last_update_str = config["last_update"].replace("Z", "+00:00")

    # 处理时区
    if "+" in last_update_str or "Z" in last_update_str:
        from datetime import datetime as dt
        last_update = dt.fromisoformat(last_update_str.replace("Z", "+00:00"))
        last_update = last_update.replace(tzinfo=None)
    else:
        last_update = datetime.fromisoformat(last_update_str)

    interval_hours = config["update_interval_hours"]
    now = datetime.now()

    if now - last_update > timedelta(hours=interval_hours):
        return True
    return False


def update_keyword_weight(keyword: str, site_id: str, file_path: str = "data/priority.json"):
    """更新关键词权重"""
    config = load_priority(file_path)

    if site_id in config["sites"]:
        if "keywords" in config["sites"][site_id]:
            config["sites"][site_id]["keywords"].append(keyword)
        else:
            config["sites"][site_id]["keywords"] = [keyword]

    config["last_update"] = datetime.now().isoformat()
    save_priority(config, file_path)
    return config


def init_keywords_db(db_path: str = "data/keywords.db") -> bool:
    """初始化SQLite关键词库"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id TEXT NOT NULL,
            keyword TEXT NOT NULL,
            query_count INTEGER DEFAULT 0,
            last_query_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_keyword ON keywords(keyword)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_site ON keywords(site_id)")

        conn.commit()
        conn.close()
        print(f"Keywords database initialized: {db_path}")
        return True
    except Exception as e:
        print(f"Database init error: {e}")
        return False


def increment_keyword(keyword: str, site_id: str, db_path: str = "data/keywords.db"):
    """增加关键词查询计数"""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute(
            "INSERT INTO keywords (site_id, keyword, query_count, last_query_at) VALUES (?, ?, 1, ?)",
            (site_id, keyword, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Increment error: {e}")
        return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Scheduler CLI")
    parser.add_argument("--check", action="store_true", help="检查是否需要更新")
    parser.add_argument("--init-db", action="store_true", help="初始化数据库")
    parser.add_argument("--keyword", help="更新关键词")
    parser.add_argument("--site", help="站点ID")
    args = parser.parse_args()

    if args.check:
        print(f"Needs update: {check_and_update()}")
    elif args.init_db:
        init_keywords_db()
    elif args.keyword and args.site:
        update_keyword_weight(args.keyword, args.site)
        print(f"Updated: {args.keyword} for {args.site}")
    else:
        parser.print_help()