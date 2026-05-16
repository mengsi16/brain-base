# -*- coding: utf-8 -*-
"""端到端测试前的基础设施健康检查：Milvus + Playwright + LLM。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def check_milvus() -> bool:
    try:
        from pymilvus import MilvusClient
        c = MilvusClient(uri="http://localhost:19530")
        cols = c.list_collections()
        print(f"  ✅ Milvus OK，collections={cols}")
        return True
    except Exception as e:
        print(f"  ❌ Milvus FAIL: {type(e).__name__}: {e}")
        return False


def check_llm() -> bool:
    try:
        from brain_base.cli import _build_llm_from_env
        llm = _build_llm_from_env()
        print(f"  ✅ LLM OK，type={type(llm).__name__}")
        return True
    except Exception as e:
        print(f"  ❌ LLM FAIL: {type(e).__name__}: {e}")
        return False


def check_playwright() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        print(f"  ✅ Playwright Chromium OK")
        return True
    except Exception as e:
        print(f"  ❌ Playwright FAIL: {type(e).__name__}: {e}")
        return False


def main() -> int:
    print("基础设施健康检查...")
    ok_milvus = check_milvus()
    ok_llm = check_llm()
    ok_pw = check_playwright()
    all_ok = ok_milvus and ok_llm and ok_pw
    print(f"\n{'✅ 全部就绪' if all_ok else '❌ 有失败项，E2E 测试可能不完整'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
