# -*- coding: utf-8 -*-
"""调试 SERP 解析：直接 goto Bing 搜索看页面结构。"""
from brain_base.tools.web_fetcher import _run_playwright, _parse_json_or_empty


def probe(query: str = "LiteLLM"):
    # 极简：先确认 run-code 自身能跑
    simple = "async (page) => { return 'hello'; }"
    print("=== simple test ===")
    try:
        out = _run_playwright(["run-code", simple, "--raw"], timeout=30)
        print("simple raw:", repr(out)[:300])
    except Exception as e:
        print("simple err:", e)

    url = f"https://cn.bing.com/search?q={query}&ensearch=1&count=5"
    code = (
        "async (page) => {"
        " await page.goto('" + url + "', { waitUntil: 'domcontentloaded', timeout: 30000 });"
        " try { await page.waitForSelector('#b_results li', { timeout: 15000 }); } catch (e) {}"
        " await page.waitForTimeout(2500);"
        " const out = await page.evaluate(() => ({"
        "  url: location.href,"
        "  title: document.title,"
        "  cnt_b_algo: document.querySelectorAll('li.b_algo').length,"
        "  cnt_main_li: document.querySelectorAll('#b_results > li').length,"
        "  body_len: document.body.innerText.length,"
        "  sample: (document.querySelector('h2') || {}).innerText || ''"
        " }));"
        " return JSON.stringify(out);"
        "}"
    )
    print("\n=== bing test ===")
    print("code:", code[:200], "...")
    raw = _run_playwright(["run-code", code, "--raw"], timeout=120)
    print("=== raw stdout (first 600) ===")
    print(raw[:600])
    print("=== parsed ===")
    parsed = _parse_json_or_empty(raw)
    if isinstance(parsed, dict):
        for k, v in parsed.items():
            print(f"  {k}: {repr(v)[:300]}")


if __name__ == "__main__":
    probe()
