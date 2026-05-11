# ToDo — brain-base

> 当前阶段：**Phase T35+（第三次重构 / 细节打磨·续）**。历史 phase 已归档至 `md/archive/ToDo-Phase-N-M.md`（最近：`@/md/archive/ToDo-Phase-T34-T34.1.md` 含 T34 固化层激活诊断 + T34.1 bootstrap 死循环修复完整决策记录）。
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

## 当前架构进度（CLAUDE.md 主流程图对照）

CLAUDE.md `@/CLAUDE.md:60-86` 里画的"QA 主流程框架"共 4 大块，**全部完成**。**第三次重构（细节打磨）继续推进**。当前状态：

- T35 老测试 mock 清理（`_FakeLLM` → 真调 Minimax）（高）
- T36 arxiv PDF 通道（中）
- T37 chunker 段落级 dedup（低）
- T38 E2E 基线测试（最低，必须等所有任务完成后再做）

---

## T35 老测试 mock 清理（`_FakeLLM` → 真调 Minimax）— pending

> **背景**（用户 2026-05-10 反馈）：T31 unit test 被用户指出「mock LLM 测试是错误的，不烧钱测试给虚假安全感」。已补 CLAUDE.md / AGENTS.md 规则 14：**LLM 节点语义测试必须真调**（默认 Minimax），禁用 mock / `_FakeLLM` / `CapturingLLM`。T31 已按新规则整改（删 mock、去 requires_llm 默认跳过），本任务负责清理老测试。
>
> **范围**（8 个文件/主题包含 mock LLM，需逐个评估重写 vs 保留）：
>
> 1. **`tests/conftest.py:404 mock_llm` fixture** — sentinel 风格，只给「图编译/拓扑测试（不调 LLM 节点）」用。留不动（规则 14 明确例外）。
>
> 2. **`tests/unit/test_qa_decompose.py:_FakeLLM`** — mock decompose 节点语义输出。**重写为真调**（拆题 + 不拆题两场景，验 sub_questions 生成质量）。
>
> 3. **`tests/unit/test_qa_prep.py:_FakeLLM`** — mock rewrite（`prep_one_subquery`）+ sparse gate。**重写为真调 LLM rewrite 部分**（验 L0-L3 改写生成 + lexical_query 质量）；sparse gate 内部 mock `_sparse_gate_score` 可保留（那是 milvus 调用，不是 LLM）。
>
> 4. **`tests/unit/test_qa_get_info.py:_FakeLLM` / `_ExplodingLLM`** — mock fetch_extract LLM 评估。**重写 fetch_extract 评估为真调**（验 FetchExtractResult 6 字段输出质量）；`_ExplodingLLM` 验「命中路径不调 LLM」仍需保留（这是负面断言不是 LLM 语义验证）。
>
> 5. **`tests/unit/test_prompts_context_inheritance.py:CapturingLLM`** — 验 rewrite / judge / self_check user_prompt 拼装含"原问题 + 同级子问题"。**这里 CapturingLLM 不是验 LLM 输出质量而是验 prompt 拼装逻辑**——边界案例。**选项**：保留（prompt 拼装逻辑不是 LLM 语义）；或重写为真调 + 在 trace logger 里 grep 拼装后的 user_prompt。**待用户决定**。
>
> 6. **`tests/e2e/test_qa_full_pipeline.py`** — 全端真调 LLM，不动。
>
> 7. **`pytest.ini` markers `requires_llm` 描述** — 已同步改为"not auto-skipped"。保持。
>
> 8. **`README.md` `requires_llm 默认跳过`描述** — 待同步改为"默认必跑，缺 key fail"。
>
> **需老测试重写后验证：**跑 `pytest tests/unit tests/smoke -q` 全绿（LLM 真调需 .env 配 MINIMAX_API_KEY）。
>
> **估算**：~200 行重写 / 1-2h，含跑成本 ~$0.5-2 / 轮。
>
> **优先级**：**高**（老 mock 测试每次跑都在误导，越早清越好）。插队位置：T34 固化层后、T36 arxiv 前。

---

## T36 arxiv 专用 PDF 通道 — pending

> 来自用户在 T26.1 完成后讨论：从 SERP 拿到 arxiv abs URL 时，HTML 里的 PDF
> 是要点 "View PDF" 才能拿到，当前 fetch_extract 走 readability 抽出来的只是
> abstract 页文字，丢了正文。
>
> 思路：在 `fetch_extract_one` 内部加一个 arxiv URL 识别（host=`arxiv.org` +
> path 含 `/abs/`），命中则改走 `arxiv-paper-fetch` 子流程：
> 1. URL 转换：`arxiv.org/abs/{id}` → `arxiv.org/pdf/{id}.pdf`
> 2. 直接下载 PDF（playwright 或 httpx，arxiv 无 CDN/防爬）
> 3. 走 upload-agent 路径（MinerU 解析 PDF → markdown），不走 readability
> 4. dedup 用 `_compute_file_sha256` + `_lookup_by_frontmatter_sha256`（走 MinerU/upload 路径，不用 `hash_lookup`——CLAUDE.md 规则 54）
> 5. 复用现有 chunker + enrich + ingest 持久化流水
>
> 关键约束：
> - **upload-agent 禁止并行**（CLAUDE.md 规则 6，MinerU 14GB VRAM）
> - 与 fetch_extract_one 的 Semaphore=3 冲突 → arxiv 命中时单独走串行队列
> - 或：arxiv URL 在 fetch_extract_one 内 short-circuit 标记为 `route: arxiv-pdf`，
>   barrier_extract 后插一个 `arxiv_pdf_serial_node`（串行处理 arxiv candidates），
>   普通 candidate 走 write_raw_one 并行
>
> 估算：~80 行 + 15 测试。优先级：**中**（实测有需求才做，本任务无依赖关系）。

---

## T37 chunker 段落级 dedup — pending

> 来自 T12 e2e：testimonials 重复 2 倍。方向：拆段（`\n\n`）+ SHA-256 去重。
> 约 25 行 + 50 行测试。T16 LLM 评分后营销页大概率拿低分被排除，
> 重复污染概率大幅下降。**当前判定：T20 通用 raw text 路径上线后，GitHub README 不走 MinerU 截断，重复来源进一步收敛，优先级再次下降。等 T38 e2e 跑完看 testimonials 是否还重复，再决定做不做。**
>
> 优先级：**低**。

---

## T38 E2E 基线测试（openclaw + baseline 字段名同步 + RAGFlow 评判表）— pending

> **背景**：原 T29 · 多次插队后顺延为 T38。第二次重构（T28-T30.1）期间 RAGFlow 题已跑过一轮（`@/data/logs/e2e-baseline-ragflow-fixed.state.json` + `@/data/logs/e2e-baseline-ragflow-fixed.log`）；openclaw 题待跑、评判表待填。**留到第三次重构（细节打磨）所有任务完成后再做**。
>
> **相关文档**：
> - 评判文档：`@/md/eval/2026-05-09-e2e-baseline.md`
> - 原 T29 执行计划：`@/md/research/2026-05-09-t29-e2e-baseline-execution-plan.md`
>
> **剩余动作**（RAGFlow 已跑，openclaw 待跑，评判表待填）：
>
> 1. **同步 baseline 文档字段名**（T30 重命名后未同步）：
>    - `sub_grep_hits` → `sub_lexical_scores`（语义：命中数 0/N → sparse top-3 平均分 0.0-1.0）
>    - `sub_grep_keywords` → `sub_lexical_query`（多 keyword list → 单短串 ≤30 字）
>    - `sub_needs_get_info` 保留，但触发条件 `hits == 0` → `score < 0.20`
>    - 影响 §4 / §5 #2 / §6 / §7 表格里所有 `sub_grep_*` 引用
>
> 2. **填 RAGFlow 评判表**：从 `data/logs/e2e-baseline-ragflow-fixed.state.json` 提取 12 项填 §7 题 1 表，按 §5 5/5 验收。
>
> 3. **跑 openclaw 题**（三子问题）：
>    - 命令：`python -m brain_base.cli ask "openclaw 是什么？怎么启动？怎么卸载？" --state-dump data/logs/e2e-baseline-openclaw.state.json > data/logs/e2e-baseline-openclaw.answer.md 2> data/logs/e2e-baseline-openclaw.stderr.log`
>    - 验收：§5 6/6 全过（多意图 / 防撞车 / GI / 入库 / PIPE2 平衡 max/min ≤ 4 / answer 分段）
>    - 关键评估点：sparse gate 在三子问题上识别正确 + T28 PIPE2 平衡
>
> 4. **跑完归档**：把 baseline 文档 + 4 个 log + 2 个 state-dump 一起搬到 `md/eval/archive/2026-05-XX-e2e-baseline-pass/`。
>
> **估算**：1-2 小时（baseline 同步 ~15 分钟 + RAGFlow 评判表 ~15 分钟 + openclaw 跑 + 评判 + 归档 ~1 小时）。
>
> **优先级**：**最低**（必须等第三次重构所有任务完成后再做；编号 T38 表示当前位置）。
