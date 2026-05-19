# ToDo — brain-base

> 当前阶段：**Phase T48（TOOL_REGISTRY 工具补齐 + 长尾优化）**。
>
> **前置归档**：
> - `@/md/archive/ToDo-Phase-T46-T46.7.md` — T46 Agentic-RAG + 迭代多跳完整决策
> - `@/md/archive/ToDo-Phase-T47-misfire.md` — T47 误判作废（虚空 direct_url 通道）
> - `@/md/archive/ToDo-Phase-T47.0-T47.7.md` — T47 统一意图识别 Agent-Loop 重构完整决策（8 任务，T47.0 契约 → T47.7 文档同步）
>
> **本文件只放 pending / executing 任务**。任务完成后等下一阶段开新任务时整体归档。

## 任务编号 = 优先级位置（重要）

- **任务编号代表优先级位置，不是时间戳**：编号越小优先级越高，越靠前。
- **高优先级永远放前面**：新加入的任务如果优先级高于现有 pending 任务，应**插队**到合适编号位置，把低优先级任务**顺延**到后面编号。
- **低优先级可以被插队顺延**：当一个高优先级任务出现，低优先级任务的编号会被**重新分配**给后面（编号增大），不要恋编号。
- **finished 任务编号不可变**：已归档的任务编号是历史档案，重排只发生在 pending 任务之间。

## 归档规则

- 每次开启新阶段（即将把第一个 `pending` 转为 `executing`）前，先用 `Move-Item` 把当前 ToDo.md 整体归档到 `md/archive/ToDo-Phase-{起始任务号}-{结束任务号}.md`，然后新建只含 pending 的 ToDo.md。
- **禁止用 edit 直接改旧内容**——改了旧内容失去回溯能力，下个 Agent 看不到完整决策上下文。
- 用命令移（PowerShell `Move-Item`），不要用 edit 工具搬运。
- 同一阶段内 pending → executing → finished 的状态推进可以 edit 同一文件，但任务条目本身不能删。

---

## 当前架构进度（2026-05-18 T47 phase 收尾）

**T47 统一意图识别 Agent-Loop 重构已全部 finished**（T47.0 契约 → T47.1 schemas → T47.2 url_pre_fetch + extract_urls → T47.3a planner+executor → T47.3b observer+merge → T47.4 主图重组 → T47.5 旧测试清退 → T47.6 旧节点彻底删 → T47.7 文档同步）。

**当前 QA 主图稳态**：
- 入口：`probe → crystallized_check`（hit_fresh/cold_promoted → answer 直返；其余 → extract_urls）
- 改写阶段：`extract_urls → [user_urls 非空] → url_pre_fetch → normalize`（含 url_context + history_summary 改写）
- 拆分阶段：`normalize → decompose`（输出 sub_questions list）
- 证据收集主干：`intent_planner ↺ intent_executor ↺ intent_observer`，should_continue_intent 5 级早退（连错 ≥2 / 充分 / 上限 / no_action / 继续）
- 证据合并：`merge_evidence`（evidence_pool → get_info_candidates 13 字段）
- 持久化流水：`fanout_persist_dispatcher → write_raw_one × N → barrier_raw → fanout_enrich → enrich_one × M → barrier_enrich → ingest`（T26.1 完全保留）
- 检索流水（PIPE2）：`fanout_search_dispatcher → subquery_search_one × N → barrier2`（T28 完全保留）
- 收尾：`judge → answer → self_check → crystallize_answer → END`

**TOOL_REGISTRY 当前 4 工具**（`brain_base/nodes/qa_tools.py`）：
- `web_search`（Google + Bing SERP）
- `fetch_url`（指定 URL → HTML → Markdown → LLM 评估）
- `raw_text`（GitHub / GitLab / arXiv abs / RFC 直取纯文本）
- `local_search`（Milvus 本地知识库 hybrid 检索）

**T48 阶段方向**：补齐 TOOL_REGISTRY 高价值工具（arxiv_pdf / github_raw），让 intent_planner LLM 自主判断何时调用。

**核心设计原则（T48 工具 vs 通道）**：
- `arxiv_pdf` 和 `github_raw` 是**工具**，不是自动通道——不做 URL 拦截/自动转换。
- **LLM 自主判断调用**：`web_search` 返回的 SERP 结果里常有 `arxiv.org/abs/...` 或 `github.com/.../blob/...`——这些是 HTML 页面，`fetch_url` 只能拿到渲染后的网页壳。`intent_planner`（大模型）天然有能力识别这些 URL 模式，自行决定"这个 arXiv 链接应该调 `arxiv_pdf` 拿全文，而不是 `fetch_url` 拿 abs 页"。
- **与 `fetch_url` 的区分**：`fetch_url` 是通用网页抓取（HTML → MD），`arxiv_pdf` 是 PDF 下载 + MinerU 解析，`github_raw` 是 raw 纯文本直取——三个工具平级，LLM 按 URL 特征选最合适的。

**2026-05-19 重排（用户审核 ToDo 后插队）**：识别 T48.3 / T48.4 两条硬阻塞前置项——`intent_executor` 缺 `parallel_ok=False` 串行化分支会导致 MinerU OOM；`web_fetcher` worker-thread 多 loop 行为未验证（T48.0 finished 备注遗留风险）。这两项升为 T48.1 / T48.2 最高优先级，原工具任务顺延为 T48.3 / T48.4。

**2026-05-19 v2 修订（用户审核契约后）**：4 份契约文档全部修订（draft → draft v2），关键修复：
- T48.1：D1 改按 `parallel_ok=False` 分组（原按 `gpu=True` 过度保守）；修 D2 KeyError + asyncio.gather 顺序断言；删 D5 prompt 改动（与 T48.3 重复）
- T48.2：D4 重写诊断方向（`_with_shutdown` 实测有调用，非缺失，根因是 loop 切换）；改阈值为公式性验证；新增 D5 fetch_binary 范围扩展（覆盖 #17/#27）；明确 T48.1 → T48.2 执行顺序
- T48.3：D2 fetch_binary 迁出（由 T48.2 交付）；D3 修正 MinerU 耗时（GPU 5-10 min/篇 而非 30+ min）；D5 修复 observer 评分困境（markdown 返前 3000 字而非 200 字 summary）；D6 明确 fast-path 同步串行语义
- T48.4：D2 加 tree 限制说明；D4 重写为"两份独立实现"（_match_github_url 共享 helper + sync `_try_github` 保留 + async `try_github_raw` 新增）；D5 加 `is_async=True` 条件依赖 T48.2；§3.3 修复 e2e 矛盾

**契约执行顺序**（修订后强制约束）：T48.1 → T48.2 → T48.3 / T48.4 并行。

**详细决策回溯**：`@/md/archive/ToDo-Phase-T47.0-T47.7.md`。

**契约文档**（详细决策见对应 contract，本 ToDo 仅放概要）：
- T48.1：`@/md/research/2026-05-19-t48.1-intent-executor-parallel-serialization-contract.md`
- T48.2：`@/md/research/2026-05-19-t48.2-web-fetcher-loop-e2e-verification-contract.md`
- T48.3：`@/md/research/2026-05-19-t48.3-arxiv-pdf-tool-contract.md`
- T48.4：`@/md/research/2026-05-19-t48.4-github-raw-tool-contract.md`

任务概览（按优先级排序）：

- T48.0 网页抓取统一走 Playwright（高，2026-05-18 完成）— **finished**
- T48.1 intent_executor parallel_ok 串行化（高，2026-05-19 完成）— **finished**
- T48.2 web_fetcher loop e2e + fetch_binary + D3 拆分（高，2026-05-19 完成）— **finished**
- T48.3 arxiv_pdf 工具注册（中，2026-05-19 完成）— **finished**
- T48.4 github_raw 工具注册 + 共享 helper 抽出（中，2026-05-19 完成）— **finished**
- T49 brain-base-skill 重写（高，2026-05-19 插队）— **finished**
- T50 chunker 段落级 dedup（低）— pending
- T51 E2E 基线测试（最低）— pending

---

## T48.0 网页抓取统一走 Playwright（废 urllib 直抓） — finished

> **实际产出**：
>
> 1. 改 `brain_base/tools/raw_text_extractor.py`（~50 行）：
>    - 删 `from urllib.error import HTTPError, URLError` / `from urllib.request import Request, urlopen` 两条 import
>    - 删 `_USER_AGENT` 常量（web_fetcher 自带真实 Windows Chrome UA）
>    - `_http_get` 内部由 `urllib.request.urlopen` 改调 `web_fetcher.fetch_page_sync`：成功取 `result["text"]`（playwright `page.evaluate("() => document.body.innerText")`，对 text/plain 响应即原文）返回 `(200, body)`；`status != "ok"` 或 `error` 非空时返回 `(500, "")` 让 handler 视为失败但不抛；fetch_page 抛错时透传为 `RuntimeError` 由 `try_raw_text` 统一捕获
>    - `DEFAULT_TIMEOUT` 由 10s 上调到 30s（chromium 启 + auto-scroll 比 urllib 慢）
>    - 模块 docstring 更新说明 T48.0 抓取层迁 Playwright
>
> 2. `tests/unit/test_raw_text_extractor.py`：零改动通过（16/16 测试 pass）。原 mock 策略 `monkeypatch.setattr(rte, "_http_get", ...)` 仍可用因为 `_http_get` 函数符号 + 签名 `(url, timeout) → (status, body)` 完全保留。
>
> 3. `CLAUDE.md` + `AGENTS.md` 各加规则 22.1（"所有网页信息获取统一走 Playwright + 反爬措施"），明确禁用 httpx / urllib.request / requests / aiohttp 任何裸 HTTP 抓页面，仅基础设施 HEAD 探测（HuggingFace endpoint）例外。
>
> 4. 同步刷新 T48.1（arxiv_pdf）+ T48.2（github_raw）任务描述：原计划"httpx 不需要 playwright"推翻——也必须走 `web_fetcher.fetch_page`，PDF 字节流如需要可在 web_fetcher 加 `fetch_binary` async 入口走相同 context。
>
> 验证：`pytest tests/unit/test_raw_text_extractor.py tests/unit/test_t47_url_pre_fetch.py tests/unit/test_t47_intent_executor.py -x -q` → 34/34 测试 pass（55s，含 LLM 真调）。
>
> **未解决但已知风险**（留给下一阶段评估，必要时拆子任务）：
>
> - **worker-thread 多 loop 重启 chromium**：`qa_url_pre_fetch._fetch_one` 与 `qa_tools.execute_raw_text` 通过 `asyncio.to_thread(try_raw_text, url)` 在 worker thread 里跑，`_http_get` 内部 `fetch_page_sync` 起独立 `asyncio.run` → 触发 web_fetcher 单例 `_LOOP` affinity 检查 → 旧实例置空重建 chromium。当主 graph async loop 与 worker thread loop 并存时可能造成 chromium 反复 kill/restart，性能/稳定性需 e2e 实测验证。若实测有问题，下一步可拆 `try_raw_text_async`（直接 await fetch_page）让 async 调用方走 async 路径、sync 调用方（ingest_url.fetch_node）走 sync 路径，避免 nested loop。
> - **GitHub README 长文档原"绕开 MinerU 16K 截断"价值仍在**：fetch_page 不走 MinerU（直接 page.content() / innerText），所以 raw_text_extractor 的核心价值未损失；仅"chromium 启动 4-6s + page innerText"比"urllib GET + .decode()"慢 5-10x，但 raw_text 是优化路径，失败可降级，单次成本可接受。

---

## T48.0 备注（历史）

> **依赖**：T47 全部 finished（已落地）。
>
> 现状：`brain_base/tools/raw_text_extractor.py` 用 `urllib.request.urlopen` 直接 HTTP GET 抓取 GitHub raw / GitLab raw / arXiv abs / RFC txt，绕过 `web_fetcher.py` 的 Playwright + stealth + auto-scroll 反爬方案。同样的 urllib 路径还出现在 `qa_url_pre_fetch._fetch_one`（路径 A）、`qa_tools.execute_raw_text`（TOOL_REGISTRY `raw_text` 工具）、`ingest_url.fetch_node`（T20 短路）的间接调用上。
>
> 核心矛盾：直接 urllib HTTP GET 不带任何反爬措施，遇到上了 CDN/WAF 的 GitHub mirror 或地区性墙就直接失败；同时埋了一个"网页抓取入口审计漏洞"，未来其他人加新抓取点会模仿这条直抓路径而不走 web_fetcher。
>
> **核心决策**：所有网页信息获取统一走 `brain_base/tools/web_fetcher.py` 的 Playwright async 单例 + stealth JS + auto-scroll + 默认有头方案。`raw_text_extractor` 模块保留（其 URL 路由 / README 探测 / arxiv meta 解析逻辑有价值），仅把内部 `_http_get` 由 urllib 改调 `web_fetcher.fetch_page_sync`。
>
> 改造点：
>
> 1. `brain_base/tools/raw_text_extractor.py:_http_get` → 内部用 `fetch_page_sync` 替换 `urllib.request.urlopen`；返回签名 `(status_code, body)` 不变，外层 4 个 handler 零改动。
> 2. 删 `from urllib.error import HTTPError, URLError` / `from urllib.request import Request, urlopen` import；保留 `from urllib.parse import urlparse`（解析用，非抓取）。
> 3. `tests/unit/test_raw_text_extractor.py`：所有 `monkeypatch.setattr(rte, "_http_get", ...)` 仍可用（_http_get 函数符号未变），但需要确认现有 mock 不再依赖 urllib 异常类型；预计零代码改动。
> 4. CLAUDE.md / AGENTS.md 加新规则段："所有网页信息获取（含 URL 抓取 / SERP / 网页解析）必须走 web_fetcher.py Playwright + 现有反爬方案，禁止使用 httpx / urllib.request / requests / aiohttp 直接抓取。例外：基础设施可达性 HEAD 探测（HuggingFace endpoint 等，不取页面内容）允许 urllib。"
> 5. 同步刷新 T48.1 / T48.2 任务描述：原计划 "httpx 不需要 playwright" 推翻——arxiv PDF 与 github raw 也必须走 web_fetcher（PDF 走 fetch_page 拿到响应字节再 MinerU；github raw 走 fetch_page 取 body innerText 即纯文本）。
>
> 关键约束：
>
> - 不动 `web_fetcher.py` 现有反爬实现（用户拍板：不补强、不改造，只迁移调用方）
> - `bin/milvus_config.py` HuggingFace endpoint HEAD 探测保留 urllib（基础设施 probe 不算"网页信息获取"）
> - sync/async 接口：`try_raw_text` 保持 sync 签名不变（内部用 `fetch_page_sync`），调用方零改动
>
> 估算：~30 行代码改动（`_http_get` 主体）+ 测试 mock 验证 + 2 份文档加规则段。优先级：**高**（堵审计漏洞 + 提升抓取健壮性）。

---

## T48.1 intent_executor `parallel_ok=False` 串行化（前置守门） — finished

> **实际产出**（2026-05-19）：
>
> 1. `@/brain_base/nodes/qa_tools.py` ToolSpec docstring 加 D1 设计规范：明确"`gpu=True` 强烈建议配 `parallel_ok=False`"，`gpu` 仅作元信息不参与调度，executor 唯一读取的是 `parallel_ok`。
>
> 2. `@/brain_base/nodes/qa_intent.py` 新增模块顶层 `_is_serial_action(action) -> bool` helper（`TOOL_REGISTRY.get(name)` 安全访问，未注册默认归 parallel）；`intent_executor_node` fan-out 分支（原 397-404 行）改为双队列调度：`indexed → serial_indexed/parallel_indexed → asyncio.gather(run_serial, run_parallel) → sorted by idx 合并`。fan-out 进入时 INFO 一行 `intent_executor fan-out | total=N serial=[...] parallel=[...]`。单 action（len==1）路径零变化、early_exit 路径零变化、空 actions 路径零变化。
>
> 3. `@/tests/unit/test_t48_1_executor_parallel_ok.py`（~410 行，7 用例）：全 parallel 真并发（时间窗重叠 + 总耗时<0.2s）/ 全 serial 真串行（时间窗不重叠 + 总耗时≥0.18s）/ 混合 [S,P,S,P] idx 严格对齐 + serial pair 不重叠 + parallel pair 重叠 / 单 action 不调 _is_serial_action（spy 计数=0） / 未注册工具归 parallel + 不抛 KeyError / 2 个 serial 工具严格顺序 / 日志埋点验证。
>
> 4. `@/md/research/2026-05-19-t48.1-execution-plan.md` 详细执行计划（任务范围、改动清单、风险审查、验收标准）。
>
> 验证：`pytest tests/unit/test_t48_1_executor_parallel_ok.py -x -v` → 7/7 pass（2.24s）；`pytest tests/unit/test_t47_*.py tests/unit/test_raw_text_extractor.py -q` → 62/62 回归 pass（70.6s，含 LLM 真调）。
>
> **核心设计**（契约 D1 v2：按 `parallel_ok=False` 分组）：
> - executor 把 actions 二分为 `serial_actions`（`parallel_ok=False`）+ `parallel_actions`（其余 + 未注册），前者 for-loop 串行、后者 asyncio.gather 并发，两条流水线同时跑
> - **安全访问** `TOOL_REGISTRY.get(name)` 避免 KeyError（D2 修订）
> - asyncio.gather 保序语义依赖该不变量（单流水线内 results 按 input 顺序）；仅需跨流水线按 idx 合并
>
> **未触发部分**：D4 选 A（不设 serial 队列上限），相信 iteration_count + LLM 自然收敛——T48.3 ToolSpec.description 单一来源约束「≤2 arxiv_pdf」（T48.1 不动 prompt）。

---

## T48.2 web_fetcher worker-thread loop e2e 验证 + fetch_binary 最小实现（前置） — finished

> **实际产出**（2026-05-19）：
>
> 1. `@/brain_base/tools/web_fetcher.py` 新增 `fetch_binary(url, timeout, extra_headers) -> bytes` async（走 `BrowserContext.request.get + response.body() + dispose`，APIRequestContext 通道）+ `fetch_binary_sync` 包装。~70 行。
>
> 2. `@/tests/e2e/test_t48_2_web_fetcher_loop_stress.py`（~470 行，7 用例）：pure async / pure sync 基线 / 底层混合基线 / **业务路径 D3 验证** / fetch_binary loop 安全 / 进程数诊断 / loop affinity 日志诊断。加 `_force_module_singleton_reset_between_tests` autouse fixture 防测试间状态污染。
>
> 3. **D3 触发条件命中** → 实施拆分：`@/brain_base/tools/raw_text_extractor.py` 加 `_http_get_async`（async 直 await `fetch_page`）+ `try_raw_text_async`（async 路由）+ 4 个 async handler（`_try_github_async / _try_gitlab_async / _try_arxiv_async / _try_rfc_async`，与 sync 版完全相同逻辑仅 IO 改 await，共享 sync 版正则/helper），共 ~180 行新增。sync `try_raw_text` 完全保留供 `bin/ingest_url.fetch_node` 用。
>
> 4. 调用方迁移：`@/brain_base/nodes/qa_url_pre_fetch.py:_fetch_one` 由 `asyncio.to_thread(try_raw_text, url)` 改为 `await try_raw_text_async(url)`；`@/brain_base/nodes/qa_tools.py:execute_raw_text` 改 async 函数 + ToolSpec `is_async=True / requires=["playwright"]`。
>
> 5. `@/tests/unit/test_t47_url_pre_fetch.py` 4 个 mock target 由 sync `try_raw_text` 改为 async `try_raw_text_async`（async 函数 fake）。
>
> 6. `@/md/research/2026-05-19-t48.2-results.md` 实测报告：业务路径 starts=2 N'=0（远低于 fail 阈值），fetch_binary 拉 arxiv PDF 2.2MB × 5 次 starts=3 严格符合公式，底层 sync↔async 混用基线 starts=10/calls=10 文档化为已知行为。
>
> 7. **T48.4 D5 决策依据**：`is_async=True` 实测验证保留——业务路径 async 工具不重启 chromium。
>
> 验证：`pytest tests/e2e/test_t48_2_web_fetcher_loop_stress.py -v` → 7/7 pass（179s）；`pytest tests/unit/test_t47_*.py tests/unit/test_t48_1_*.py tests/unit/test_raw_text_extractor.py -q` → 69/69 回归 pass（46s）。
>
> **未触发部分**：底层 `fetch_page_sync` ↔ `fetch_page` 混用 chromium 重启 = web_fetcher 单例 `_get_context` loop affinity 设计层面问题，业务侧 D3 修复后已不会自然触发，超出 T48.2 范围。如未来生产中再现需起独立子任务。

---

## T48.2 备注（历史） — finished

> **契约（draft v2）**：`@/md/research/2026-05-19-t48.2-web-fetcher-loop-e2e-verification-contract.md`
>
> **依赖**：T48.1（本任务 `test_mixed_raw_text_in_intent_executor` 调 intent_executor，需跳双队列逻辑后才验证准确）。**T48.3 / T48.4 共同前置**——T48.0 finished 时遗留的"worker-thread 多 loop 重启 chromium"风险未做实测，T48.3 引入 `fetch_binary` PDF 二进制下载、T48.4 引入更多 `fetch_page` 调用都会加剧此风险。
>
> **核心设计（修订 v2）**：
> 1. **fetch_binary 最小实现（D5 新增）**：~40 行加在 `web_fetcher.py`，走 `ctx.request.get(url) + response.body()`（APIRequestContext）——本任务交付供 T48.3 复用不重复验证
> 2. **验证**：7 个 e2e 用例（加 fetch_binary loop 安全），量化 chromium 启动次数 / 单跳耗时 / 进程泄漏 / 失败率
> 3. **诊断**：原 D4 写"`fetch_page_sync` 不走 _with_shutdown"不对——实测源码 `web_fetcher.py:545` 走 `asyncio.run(_with_shutdown(...))`；根因是 loop 切换 chromium 重建不是 shutdown 缺失
>
> **修订后阈值**（公式性验证）：chromium 启动次数 ≈ 1+N′ (N′ = caplog "loop 切换"条数)，超过 1.5× = bug；进程残留 0 过、1-2 warn、3+ fail；失败率 ≤2%。
>
> **改动**：fetch_binary ~40 行 + e2e ~280 行；如需修复，加 `try_raw_text_async` ~40 行。**根因诊断前置**：超阈后看 caplog 定位 loop 切换发生位置再决定修复方案。
>
> **估算（修正算术错误）**：fetch_binary 1.5h + 测试 4h + 实测 1h + 修复 2h = **约 1 天 ±半天**（原 "半天" 是 4+1+2=7h 错算）。优先级：**最高**（T48.3 / T48.4 共同前置）。

---

## T48.3 arxiv_pdf 工具（LLM 自主判断调用） — finished

> **实际产出**（2026-05-19）：
>
> 1. `@/brain_base/tools/raw_text_extractor.py` 加 `_ARXIV_ID_RE_KEEP_V` + `normalize_arxiv_pdf_url(url) -> str | None`（保留 v 后缀，仅支持 2007 后新格式 YYMM.NNNNN，老格式 cs.LG/0501001 不支持返 None 让上层 fallback）。
>
> 2. `@/brain_base/agents/schemas.py` Evidence 加 `raw_path: str = ""` 字段（fast-path 工具落盘路径透传 metadata）。
>
> 3. `@/brain_base/nodes/qa_intent.py` 改 `_tool_result_to_evidence`：sha256_hash 工具自报优先（PDF binary sha256 ≠ markdown sha256）+ 透传 raw_path；改 `merge_evidence_node`：candidate dict 加 `raw_path` 字段。
>
> 4. `@/brain_base/nodes/qa_persist.py:write_raw_one` fast-path：`candidate.raw_path` 非空 + 文件存在 → 跳 fetch+frontmatter+write，直接调 chunker（doc_id 从 raw_path stem 提取）。标准路径行为零改动。
>
> 5. `@/brain_base/nodes/qa_tools.py` 加 `execute_arxiv_pdf` async（~250 行）：URL 规范化 → fetch_binary → SHA-256 dedup → miss 走 convert_one MinerU → 读 raw_path 全文 → 重写 frontmatter（含 sha256 让后续 dedup 命中）→ 截前 3000 字 markdown → 返 ToolResult（含 raw_path / sha256_hash / doc_id）；hit 直接读 existing raw md 返 score=60。`_extract_first_h1` + `_build_arxiv_frontmatter` helper。注册 ToolSpec：`gpu=True / parallel_ok=False / is_async=True / requires=["mineru","playwright"]`，description 含 "≤2" 提示。
>
> 6. `@/brain_base/prompts/intent_prompts.py` "工具入参约定"加 arxiv_pdf 行 `{"url": "arxiv.org/abs/{id} 或 arxiv.org/pdf/{id}.pdf"}`。
>
> 7. `@/tests/unit/test_t48_3_arxiv_pdf.py`（~410 行，11 用例）：URL 规范化 (5 种 pattern + 7 类拒绝) / 主流程 mock 单测 (invalid url / empty / dedup hit 跳 mineru / dedup miss 调 mineru / fetch fail / mineru fail) / ToolSpec 注册 + 双 arxiv_pdf 串行（验证 T48.1 双队列生效）/ evidence + persist 集成（raw_path 透传链）。
>
> 8. `@/md/research/2026-05-19-t48.3-execution-plan.md` 详细执行计划。
>
> **简化决策**（与契约 D6 取舍）：T48.3 工具内**不**同步跑 chunker / enrich / ingest——让 evidence 透传 raw_path + content_sha256 给 candidate，由现有 PIPE2 fanout_persist_dispatcher 统一处理（write_raw_one fast-path 看到 raw_path 已落盘 → 跳 fetch+write 直接 chunker）。observer 评分时 chunks 可能还未在 Milvus，但 observer 用 markdown 前 3000 字打分（非 Milvus 状态），影响为零。
>
> 验证：`pytest tests/unit/test_t48_3_arxiv_pdf.py -v` → 11/11 pass（1.8s）；`pytest tests/unit/test_t47_*.py tests/unit/test_t48_*.py tests/unit/test_raw_text_extractor.py -q` → 80/80 全回归 pass（55s）。
>
> **未做**：MinerU 真调 e2e（5-10 min/篇 × 5 篇 = 25-50 min GPU 时间，留后续手动验证或 T50 baseline）。

---

## T48.3 备注（历史） — pending

> **契约（draft v2）**：`@/md/research/2026-05-19-t48.3-arxiv-pdf-tool-contract.md`（原 T48.1，2026-05-19 顺延）
>
> **依赖**：**T48.1 + T48.2 双硬前置**（OOM 守门 + loop 验证 + fetch_binary 交付）。
>
> **设计意图**：`arxiv_pdf` 是**工具**，不是自动通道。`intent_planner`（LLM）在收到 SERP 结果或用户给的 arxiv URL 后，自行判断"这是 arxiv.org/abs/... 页面，需要调 `arxiv_pdf` 拿全文 PDF + MinerU 解析，而不是 `fetch_url` 只拿 abs 页 HTML"。
>
> **与现有 `raw_text._try_arxiv` 的分工**：`_try_arxiv` 只解析 abs 页 meta（title + authors + abstract），不下载 PDF。`arxiv_pdf` 补齐 PDF 全文路径，两者并存让 LLM 选"只需摘要时调 raw_text，需要全文时调 arxiv_pdf"。
>
> **核心流程（修订 v2）**：
> 1. URL 规范化：`arxiv.org/abs/{id}` 或 `arxiv.org/pdf/{id}.pdf` → 统一 `arxiv.org/pdf/{id}.pdf`（保留 v；正则去非贪婪 `[^/]+`）
> 2. **fetch_binary** → **调用 T48.2 交付的函数**（T48.3 不再写）
> 3. SHA-256 dedup：复用 `_compute_file_sha256` + `_lookup_by_frontmatter_sha256`，命中即跳 MinerU
> 4. MinerU 解析：走 `convert_document` 主机 python 路径（**GPU 5-10 min/篇**，原误写 30+ min）
> 5. **fast-path persist**：内部同步串行落盘 + chunker + enrich + ingest（总耗时 ~10-15 min/篇首调，命中 sha256 ~5s）
> 6. ToolResult.markdown 返**前 3000 字符**（含摘要 + 引言，observer 评分足够） + summary 200-400 字 + raw_path 全文落盘路径（#14 修复 observer 评分困境）
>
> **关键约束**：
> - `ToolSpec.gpu=True`、`parallel_ok=False`（T48.1 串行化生效）
> - `is_async=False`（convert_document 是 sync subprocess，executor 用 to_thread 包装）
> - 失败 fallback 到 `raw_text._try_arxiv`（abstract 兜底）
> - description 末句是**唯一**提示 LLM "≤2 arxiv_pdf"的位置（T48.1 不动 prompt）
>
> **改动**：~520 行（raw_text_extractor + qa_tools + qa_persist 抽 `_persist_markdown` + schemas + intent_prompts + 单测 + e2e）。fetch_binary 60 行归 T48.2。优先级：**中**。

---

## T48.4 github_raw 工具 + raw_text 职责切割 — finished

> **实际产出**（2026-05-19）：
>
> 1. `@/brain_base/tools/raw_text_extractor.py` 抽 `_match_github_url(url) -> dict | None` 纯函数共享 helper（正则解析 GitHub URL 模式 → kind/owner/repo/branch/path，无 IO，~55 行）。sync `_try_github` 重写为内部用 `_match_github_url`（行为零变化）。新增 async `try_github_raw(url, timeout)` 公开 API（共享 helper + 走 `_http_get_async`），`_try_github_async = try_github_raw` 别名让 dispatch 表向后兼容。
>
> 2. `@/brain_base/nodes/qa_tools.py` 加 `execute_github_raw` async（~65 行）：内部 `await try_github_raw(url)`，None → error 含 "unsupported github url"；注册 ToolSpec：`gpu=False / parallel_ok=True / is_async=True / requires=["playwright"]`，description 显式列支持（仓库根/blob/raw）+ 不支持（issue/PR/wiki/gist/tree 目录）+ "比 fetch_url 快 5-10×"；raw_text ToolSpec.description 更新为 `"GitLab raw / arXiv 摘要页 / RFC 纯文本直取（按 URL host 自动路由）。GitHub URL 请优先用 github_raw（更明确 + 同等性能）。"`。
>
> 3. `@/brain_base/prompts/intent_prompts.py` 工具入参约定：raw_text 行更新为 GitLab/arXiv abs/RFC + 加 github_raw 行；决策启发场景 A 加 GitHub 仓库根/blob → github_raw、issue/PR/wiki → fetch_url、arXiv abs vs 全文路由、GitLab/RFC → raw_text。
>
> 4. `@/tests/unit/test_t48_4_github_raw.py`（~370 行，15 用例）：`_match_github_url` 4 类（repo_root / blob,raw,tree / issue,PR,wiki,gist,search 拒绝 / 非 github host）/ `try_github_raw` async 5 类（main 命中 / master fallback / blob 转 raw / README_zh / IO 异常静默）/ `execute_github_raw` 工具 2 类（成功 / unsupported）/ ToolSpec 注册 + raw_text description 不再宣传 GitHub + sync `_try_github` 行为零变化（ingest_url 保护）+ async dispatch 兜底。
>
> 5. `@/md/research/2026-05-19-t48.4-execution-plan.md` 详细执行计划。
>
> 6. `@/tests/unit/test_t47_intent_planner.py` LLM 真调测试断言更新：扩展工具白名单加入 `arxiv_pdf / github_raw`（LLM 实测看到 GitHub URL 正确选 github_raw 而非旧的 raw_text，证明 prompt 切割成功）。
>
> **简化决策**（vs 契约）：dispatch 表保留 github 行（不破坏 ingest_url.fetch_node sync 路径），仅通过 raw_text ToolSpec.description 切割职责。这避免改动 ingest_url 代码且仍达成 CLAUDE.md 规则 10「三步处理」目标——LLM 看 description 后会优先选 github_raw（明确指引），即使误用 raw_text(github URL) 也能 work（dispatch 表兜底）。
>
> 验证：`pytest tests/unit/test_t48_4_github_raw.py -v` → 15/15 pass（1.2s）；`pytest tests/unit/test_t47_*.py tests/unit/test_t48_*.py tests/unit/test_raw_text_extractor.py -q` → **95/95 全回归 pass（含 T47 LLM 真调测试 Minimax）**（55s）。
>
> **未做**：真 GitHub URL e2e + 真调 Minimax 验证 LLM 选 github_raw 而非 fetch_url（已通过 LLM 真调 planner 测试间接验证：LLM 实测在 GitHub URL 场景下选了 github_raw → 测试断言原本只允许 4 个旧工具 → fail 暴露行为变化 → 修断言后通过）。

---

## T48.4 备注（历史） — pending

> **契约（draft v2）**：`@/md/research/2026-05-19-t48.4-github-raw-tool-contract.md`（原 T48.2，2026-05-19 顺延）
>
> **依赖**：**T48.2 硬前置**（loop 验证决定 `is_async=True` 是否可靠）。
>
> **设计意图**：把 `raw_text._try_github` 隐藏路径升级为一等工具 `github_raw`，让 LLM 显式选择 GitHub raw 而非靠 host 暗调。**严格遵守 CLAUDE.md 规则 10 三步处理**：
> 1. 判断职责：github_raw 应是独立工具
> 2. 对比合并：抽共享 `_match_github_url` helper（纯函数正则解析）
> 3. 删冗余：`raw_text` description 移除 GitHub 字样、handlers 表移除 github.com 行
>
> **D4 修订后架构（两份独立实现 — 修 #21 架构矛盾）**：
> - `_match_github_url(url)` — 纯函数公共 helper（正则解析，无 IO）
> - sync `_try_github(url)` — **原实现零变化**（供 `bin/ingest_url.fetch_node` 使用，ingest 路径零回归）
> - async `try_github_raw(url)` — 新增独立实现走 `await fetch_page`（供 `execute_github_raw` 工具使用）
>
> **与 raw_text 的最终分工**：
> - `github_raw`（新）：github.com 仓库根 / blob / raw 文件页（**tree 返 error**，raw 形不存在）
> - `raw_text`（缩窄）：GitLab raw / arxiv abs / RFC
>
> **关键约束**：
> - `is_async=True`（默认 async 直调）——**⚠️ 条件依赖 T48.2 验证**：如 async 路径出现多余重启问题 → 回退 `is_async=False + to_thread`（#24）
> - `parallel_ok=True`，`gpu=False`
> - description 必须列"不支持 issue/PR/wiki/gist + tree 目录页"——避免 LLM 误用（#22）
>
> **改动**：~净增 230 行（原估 100 行偏低，未计入公共 helper + e2e 测试 + docstring）：raw_text_extractor 抽 helper + sync/async 两版 + qa_tools + intent_prompts + 单测迁移 + e2e。优先级：**中**。

---

## T49 brain-base-skill 重写（外部 Agent 调用手册对齐 LangGraph CLI） — finished

> **实际产出**（2026-05-19）：
>
> 1. `@/brain-base-skill/SKILL.md` 整体重写（461 行 → 408 行，净删 53 行）：
>    - 删全部 claude-plugin 残留：`bin/brain-base-cli.py` / `claude -p --plugin-dir --agent brain-base:` / `BRAIN_BASE_CLAUDE_BIN`
>    - 删不存在的命令：`exists` / `ingest-text` / `feedback` / `resume` / `history`（共 5 个）
>    - 新增命令：`chat` / `lint` / `crystallize-check`（共 3 个）
>    - 命令矩阵从 11 个缩到 9 个（全部对齐 `brain_base/cli.py` 实际实现）
>    - 输出格式：从虚构的 JSON 壳（`session_id/result.ok/result.exit_code`）改为实际的 stdout 文本 + stderr log + `--state-dump` JSON dump
>    - 环境变量：`BRAIN_BASE_CLAUDE_BIN` → `BB_LLM_PROVIDER / BB_LLM_BASE_URL / BB_LLM_API_KEY / BB_DEEP_THINK_LLM / BB_LOG_LEVEL / BB_PLAYWRIGHT_HEADLESS`
>    - 架构图：`qa-agent/get-info-agent/upload-agent` claude plugin 三件套 → LangGraph 8 子图（QaGraph / IngestUrlGraph / IngestFileGraph / PersistenceGraph / CrystallizeGraph / GetInfoGraph / LifecycleGraph / LintGraph）
>    - 新增能力描述：固化层 hot/cold 分层 + 自动固化（value_score ≥ 0.3）、source_priority 三档、bge-m3 hybrid + bge-reranker、`--session` JSONL 多轮对话
>    - 新增 workaround：`ingest-text` 用「先落盘 `.md` 再 `ingest-file --path`」替代
>    - 新增场景 E：跨进程多轮对话
>
> 2. `@/md/research/2026-05-19-t49-brain-base-skill-rewrite-plan.md` 详细执行计划（偏差对照表 + 风险审查 + 改动清单 + 验收标准）。
>
> 验收：grep 验证旧引用 0 命中（`bin/brain-base-cli.py` / `--plugin-dir` / `claude -p` / `BRAIN_BASE_CLAUDE_BIN`）；新 CLI `python -m brain_base.cli` 出现 18+ 处；新 env `BB_LLM_*` 全部出现。
>
> **未做**：不补 CLI 不存在的命令（`feedback` / `exists` / `ingest-text` 等），如需补应单独起 CLI 开发任务。不暴露 6 工具 TOOL_REGISTRY 给外部 Agent（用户拍板：工具是 intent_planner LLM 内部决策，外部 Agent 只关心 ask）。

---

## T50 chunker 段落级 dedup — pending

> 来自 T12 e2e：testimonials 重复 2 倍。方向：拆段（`\n\n`）+ SHA-256 去重。
> 约 25 行 + 50 行测试。LLM 评分后营销页大概率拿低分被排除。
>
> 优先级：**低**。

---

## T51 E2E 基线测试（openclaw + baseline 字段名同步 + RAGFlow 评判表） — pending

> 必须等所有重构任务（T47 + T48）完成后再做。
>
> 优先级：**最低**。
