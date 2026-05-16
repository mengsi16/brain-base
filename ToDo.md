# ToDo — brain-base

> 当前阶段：**Phase T47+（通道拓展 + 测试基线）**。历史 phase 已归档至 `md/archive/ToDo-Phase-N-M.md`（最近：`@/md/archive/ToDo-Phase-T46-T46.7.md` 含 T46 Agentic-RAG 工具化检索 + 迭代多跳完整决策记录 + 51 单元测试）。
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

## 当前架构进度

T46 Agentic-RAG 工具化检索 + 迭代多跳已于上阶段完成（contract / schemas / TOOL_REGISTRY / 4 跳节点 / 三路分流 / 51 单元测试），详见归档 `@/md/archive/ToDo-Phase-T46-T46.7.md`。

当前任务概览：

- T47 arxiv PDF 通道（中）
- T48 chunker 段落级 dedup（低）
- T49 E2E 基线测试（最低）

---

## T47 arxiv 专用 PDF 通道 — pending

> 来自用户在 T26.1 完成后讨论：从 SERP 拿到 arxiv abs URL 时，HTML 里的 PDF
> 是要点 "View PDF" 才能拿到，当前 fetch_extract 走 readability 抽出来的只是
> abstract 页文字，丢了正文。
>
> 思路：T46 完成后，arxiv_pdf 作为 **TOOL_REGISTRY 的一个 tool 注册**，不再是
> `fetch_extract_one` 内部的 if/else 分支。
> 1. URL 转换：`arxiv.org/abs/{id}` → `arxiv.org/pdf/{id}.pdf`
> 2. 直接下载 PDF（playwright 或 httpx，arxiv 无 CDN/防爬）
> 3. 走 upload-agent 路径（MinerU 解析 PDF → markdown），不走 readability
> 4. dedup 用 `_compute_file_sha256` + `_lookup_by_frontmatter_sha256`
> 5. 复用现有 chunker + enrich + ingest 持久化流水
>
> 关键约束：**upload-agent 禁止并行**（MinerU 14GB VRAM）。
>
> 估算：~80 行 + 15 测试。优先级：**中**。

---

## T48 chunker 段落级 dedup — pending

> 来自 T12 e2e：testimonials 重复 2 倍。方向：拆段（`\n\n`）+ SHA-256 去重。
> 约 25 行 + 50 行测试。T16 LLM 评分后营销页大概率拿低分被排除。
>
> 优先级：**低**。

---

## T49 E2E 基线测试（openclaw + baseline 字段名同步 + RAGFlow 评判表）— pending

> 必须等所有重构任务完成后再做。
>
> 优先级：**最低**。
