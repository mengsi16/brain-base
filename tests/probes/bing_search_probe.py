# -*- coding: utf-8 -*-
"""探查 Bing 搜索结果里真实 URL 在哪个属性。"""
from brain_base.tools.web_fetcher import _run_playwright, _parse_json_or_empty

url = "https://cn.bing.com/search?q=LiteLLM&ensearch=1&count=5"
js = (
    "async (page) => {"
    " await page.goto(" + repr(url) + ", { waitUntil: 'domcontentloaded', timeout: 30000 });"
    " try { await page.waitForSelector('li.b_algo', { timeout: 15000 }); } catch (e) {}"
    " await page.waitForTimeout(2000);"
    " const items = await page.evaluate(() => {"
    "  const out = [];"
    "  document.querySelectorAll('li.b_algo').forEach(b => {"
    "   const h2a = b.querySelector('h2 a');"
    "   const cite = b.querySelector('cite');"
    "   const tilk = b.querySelector('a.tilk');"
    "   out.push({"
    "    h2a_href: h2a ? h2a.href : '',"
    "    h2a_aria: h2a ? (h2a.getAttribute('aria-label') || '') : '',"
    "    cite_text: cite ? cite.innerText : '',"
    "    tilk_href: tilk ? tilk.href : '',"
    "    h2_text: (b.querySelector('h2') || {}).innerText || ''"
    "   });"
    "  });"
    "  return out.slice(0, 3);"
    " });"
    " return JSON.stringify(items);"
    "}"
)
raw = _run_playwright(["run-code", js, "--raw"], timeout=120)
parsed = _parse_json_or_empty(raw)
for i, it in enumerate(parsed or []):
    print(f"--- result {i} ---")
    for k, v in it.items():
        print(f"  {k}: {repr(v)[:200]}")
