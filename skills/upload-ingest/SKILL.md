---
name: upload-ingest
description: 当 upload-agent 接收到用户本地文档入库请求后触发。只负责调度 doc-converter 完成格式转换，再把结果交给 knowledge-persistence 做分块与 Milvus 入库。与 get-info-workflow 平行，不经过外部补库链路。
disable-model-invocation: false
---

# Upload Ingest Workflow

## 0. 强制执行：Todo List

upload-agent 在执行本 workflow 前，**必须先调用 `TodoList` 工具**，按以下步骤生成 todo 列表，然后严格按列表顺序执行。每完成一步立即更新状态为 `completed`，再进入下一步。**禁止跳步**。

典型 todo 模板：
1. 步骤1：接收并规整上传任务 → pending
2. 步骤2：前置健康检查（doc-converter / Milvus / bge-m3） → pending
3. 步骤3：调用 doc-converter 完成格式转换与原始文件归档 → pending
4. 步骤4：为每个 raw MD 组装 user-upload frontmatter → pending
5. 步骤5：调用 knowledge-persistence（分块 + 合成QA + chunks 落盘 + Milvus 入库） → pending
6. 步骤6：返回入库摘要给 upload-agent → pending

**步骤 5 是最容易被跳过的步骤**。raw 写入 ≠ 持久化完成。必须确认：
- chunks 已落盘到 `data/docs/chunks/`
- 每个 chunk 的 frontmatter 含 `questions` 字段
- `bin/milvus-cli.py ingest-chunks` 已执行且返回 `chunk_rows` + `question_rows`

以上全部确认后才能标记步骤 5 为 completed。

## 1. 适用场景

在以下场景触发本 skill：

1. 用户明确说“上传/导入/添加本地文档到知识库”。
2. 用户把下列任一类型文件交给助手并要求入库：
   - **文档**：PDF / DOCX / PPTX / XLSX / LaTeX / TXT / MD / PNG / JPG / JPEG
   - **源码**：Python (`.py` / `.pyi`)、JS/TS (`.js` / `.jsx` / `.ts` / `.tsx` / `.mjs` / `.cjs`)、Go (`.go`)、Rust (`.rs`)、Java (`.java`)、Kotlin (`.kt` / `.kts`)、Scala、Swift、C/C++ (`.c` / `.h` / `.cpp` / `.cc` / `.cxx` / `.hpp` / `.hh`)、C# (`.cs`)、Ruby、PHP、Lua、Dart、Shell (`.sh` / `.bash` / `.zsh`)、PowerShell (`.ps1`)、SQL、R、Julia、Elixir、Erlang、Haskell、OCaml、Vue、Svelte、Groovy 等
   - **配置 / 标记**：TOML、YAML/YML、JSON/JSONC、XML、HTML/HTM、CSS/SCSS、INI/CFG/CONF、.env、Dockerfile、Makefile
3. 用户给出目录路径，希望批量入库其中所有支持格式的文件。

在以下场景不要触发：

1. 用户只是让你阅读文档，并不要求入库。
2. 用户想抓取网页——那是 `get-info-agent` 的职责，不是本路径。
3. 用户文件格式不在 `bin/doc-converter.py` 的 `SUPPORTED_EXTS` 中——应当明确告知用户不支持并停下。权威列表以该常量为准，本文档仅枚举常见类型。

调用链约束：

1. `upload-agent -> upload-ingest workflow`（本 skill）。
2. 本 skill 不经过 `get-info-workflow`，不调用 `web-research-ingest`，不调用 `playwright-cli-ops`。
3. 本 skill 不负责回答用户问题（那是 `qa-agent` 的职责）。

## 2. 职责边界

本 skill 负责：

1. 校验输入文件存在且格式受支持。
2. 调度 `bin/doc-converter.py` 执行格式转换。
3. 为转换后的 raw Markdown 组装 `user-upload` 类型 frontmatter。
4. 调度 `knowledge-persistence` 完成分块、合成 QA、落盘与 Milvus 入库。
5. 汇总并返回入库摘要。

本 skill 不负责：

1. 实现 PDF / DOCX / LaTeX 的解析细节（交给 `doc-converter`）。
2. 实现分块与合成 QA 算法（交给 `knowledge-persistence`）。
3. 写入 Milvus 的具体实现（交给 `bin/milvus-cli.py ingest-chunks`）。
4. 更新 `priority.json` / `keywords.db`（上传路径无 URL、无站点，不涉及）。

## 3. 输入

推荐输入字段：

1. 一个或多个**本地文件路径**（绝对或相对于仓库根）。
2. 可选：上传日期（ISO `YYYY-MM-DD`，默认今天；用于生成 `doc_id`）。
3. 可选：主题 slug（覆盖默认的文件名 slug）。
4. 可选：标题 / section_path / keywords 等元信息（如用户在会话中提供）。

**不接受**的输入：

1. URL（应该走 `get-info-agent`）。
2. 不存在的路径（必须 fail-fast）。
3. 不支持的格式（必须 fail-fast，并在错误消息里列出支持的格式）。

## 4. 输出

输出应包括：

1. 每个入库文档的 `doc_id`、`raw_path`、`original_file`（归档路径）。
2. chunk 数量与 question 数量（从 `ingest-chunks` 报告中获取）。
3. 失败文件列表及失败阶段（检测 / 转换 / 分块 / 入库）。

## 5. 执行流程

### 步骤 1: 接收并规整上传任务

把任务整理成统一结构：

1. 待处理文件清单（展开目录后得到的有效文件）。
2. 上传日期（用户提供 or 默认今天）。
3. 可选元信息（标题、主题 slug、keywords 等）。

对每个文件校验：

1. 文件存在。
2. 扩展名在 `bin/doc-converter.py` 的 `SUPPORTED_EXTS` 中。常见覆盖：
   - **文档类**：`.pdf / .docx / .pptx / .xlsx / .png / .jpg / .jpeg / .tex / .txt / .md / .markdown`
   - **源码类**：`.py / .ts / .tsx / .js / .jsx / .go / .rs / .java / .kt / .c / .h / .cpp / .cs / .rb / .php / .swift / .sh / .ps1 / .sql / .r` 等
   - **配置类**：`.toml / .yaml / .yml / .json / .xml / .html / .css / .ini` 等
   - 完整列表以 `detect_backend()` 的实际分支为准。
3. 非空（`stat().st_size > 0`）。

任一校验不通过，记录到失败列表并跳过该文件，不要中断整个批次。

### 步骤 2: 前置健康检查

执行上传入库前必须完成：

1. `python bin/doc-converter.py check-runtime` — 确认 MinerU 和/或 pandoc 按需可用（PDF/DOCX/PPTX/XLSX/图片 依赖 MinerU；`.tex` 依赖 pandoc；TXT/MD/源码/配置文件无外部依赖，纯 Python 直读）。
2. `python bin/milvus-cli.py check-runtime --require-local-model --smoke-test` — 确认 Milvus + bge-m3 可用。

原则：

1. 只有本批次**实际需要**的后端不可用才 fail-fast。例如批次全是 `.txt`，则 MinerU 缺失不算致命。
2. 任一健康检查致命失败，终止本批次并明确指出缺失工具与安装命令。

### 步骤 3: 调用 doc-converter 完成格式转换

> **⚠️ GPU 并发硬约束**：MinerU 单文件峰值约 14 GB VRAM，16 GB 显卡同一时刻只能跑一个转换任务。
> - **禁止**在多个并行 Bash 命令中分别调用 `doc-converter`。
> - **禁止**同时启动多个 `doc-converter` 进程处理不同文件。
> - 所有文件必须通过**单次** `doc-converter convert` 调用（`--input` 接受多个文件），由其内部逐个顺序处理 + VRAM 阈值检测。
> - 如果需要分批调用，必须等上一条命令完全结束后再启动下一条。
>
> **显存保护（自动分批）**：`doc-converter` 内部已实现两层显存保护：
> 1. **MinerU 滑动窗口**：自动设置 `MINERU_PROCESSING_WINDOW_SIZE=10`（默认），控制 MinerU 内部单次处理的页窗口大小。可通过环境变量覆盖。
> 2. **PDF 页分批**：超过 `KB_MINERU_PAGE_BATCH_SIZE`（默认 10）页的 PDF 会自动按页范围分批调用 MinerU（如 `page_range="1-10"` → `"11-20"` → ...），每批间等待 3 秒释放显存，最后合并 Markdown + 图片。可通过环境变量覆盖批大小。

对每个受支持文件调用：

```bash
python bin/doc-converter.py convert \
    --input <file1> <file2> ... \
    --output-dir data/docs/raw/ \
    --uploads-dir data/docs/uploads/ \
    [--upload-date YYYY-MM-DD] \
    [--overwrite]
```

CLI 返回 JSON（stdout）包含 `results[]` 和 `errors[]`。本步骤必须：

1. 解析 JSON，逐个处理 `results`。
2. 对 `errors` 明确保留，后续合并到最终返回报告中。
3. 不自己重新实现转换逻辑——doc-converter 是唯一实现入口。
4. **本步骤到此为止只完成 storage**：原始文件归档、raw Markdown 落盘、图片资源保留；禁止在这里做任何静态分块、预分块或 questions 生成。

每条 result 含：

- `doc_id`（`<slug>-YYYY-MM-DD`）
- `raw_path`（`data/docs/raw/<doc_id>.md`，纯正文，无 frontmatter）
- `archive_dir`（`data/docs/uploads/<doc_id>/`，该文档的完整归档目录）
- `original_file`（`data/docs/uploads/<doc_id>/<filename>`，归档路径）
- `images_dir`（若 MinerU 抽取出图片，则为 `data/docs/uploads/<doc_id>/images/`；否则为 `null`）
- `has_images`（布尔值，表示该文档是否有被保留下来的图片资源）
- `char_count`、`format`、`backend`

`doc-converter` 的 storage 契约是：

1. 原始输入文件必须归档到 `data/docs/uploads/<doc_id>/`。
2. 转换后的 raw Markdown 必须落到 `data/docs/raw/<doc_id>.md`。
3. 若 MinerU 产生图片资源，必须在清理 `_mineru_work` 之前搬运到 `data/docs/uploads/<doc_id>/images/`，并确保 raw Markdown 中的相对图片路径改写后不会断链。
4. `_mineru_work` 是调试中间产物，不属于长期 storage 契约；默认可删除，仅在显式 debug 时保留。

### 步骤 4: 为每个 raw MD 组装 user-upload frontmatter

对 `doc-converter` 返回的每个 `raw_path`，就地写回带 frontmatter 的 Markdown。frontmatter 模板必须严格遵循 `knowledge-persistence` 定义的 **user-upload** 类型：

```markdown
---
doc_id: <doc_id>
title: <由用户提供或从首个 # 标题提取或用原始文件名>
section_path: 用户文档 / <主题 slug>
source: user-upload
source_type: user-upload
original_file: <original_file 路径>
url:
fetched_at: <YYYY-MM-DD，通常是上传日期>
content_sha256: <由步骤 4.5 的 Bash 命令计算，禁止编造>
summary: <首段摘要，≤ 500 字符>
keywords: ["<由用户提供或后续 organize 补全的关键词>", "..."]
---

<原 raw MD 正文>
```

规则：

1. `doc_id` 必须和 `doc-converter` 返回一致。
2. `source_type` 必须为 `user-upload`（**新增值**，与 `official-doc` / `community` 并列）。
3. `original_file` 必须填 `doc-converter` 返回的归档路径。
4. `url` 字段冒号后直接留空（不写 `""`，frontmatter 解析器不去引号会得到字面量 `""`）。
5. `title` 取值顺序：用户显式提供 → raw 正文首个 H1/H2 → 原始文件名（去扩展名）。
6. `summary` 取值：raw 正文首段非空段落（≤ 500 字符）。
7. `content_sha256` 必填（P2-1）：按 LF 换行规范后的 body SHA-256 十六进制摘要；由步骤 4.5 的 Bash 命令计算并回填，**绝对禁止 LLM 编造**。
8. 这一步**不做**分块、不生成合成 QA、不做任何预分块——那些全部是 `knowledge-persistence` 的职责，且必须由 Agent/LLM 执行。

### 步骤 4.5: 内容哈希去重（P2-1，硬约束）

组装好 frontmatter 并写入 raw 文件后，对每个待入库的 raw 文档做内容去重：

**⚠️ SHA-256 必须由以下 Bash 命令计算，绝对禁止 LLM 编造哈希值。**

1. 写入 raw 文件后，用 Bash 执行以下命令计算 body 的 SHA-256：

```bash
python -c "import hashlib;p=\"<刚写入的raw_md_path>\";t=open(p,encoding='utf-8').read();body=t.split('---',2)[2] if t.startswith('---') else t;print(hashlib.sha256(body.replace(chr(13)+chr(10),chr(10)).strip(chr(10)).encode('utf-8')).hexdigest())"
```

2. 把输出的 64 位十六进制字符串回填到 frontmatter 的 `content_sha256` 字段（替换占位值）。
3. 调用 `python bin/milvus-cli.py hash-lookup <sha256>`：
   - `status: "hit"` → **跳过本文件**，不再调 knowledge-persistence；但仍把归档文件保留在 `data/docs/uploads/<doc_id>/`（原始归档是快速复查的凭据，且已在步骤 3 完成）。在最终报告里标为 `skipped_duplicate`，附上 `existing_doc_ids`。
   - `status: "miss"` → 继续步骤 5。
4. 若 CLI 异常（Milvus 未起不影响——这条 CLI 是纯文件系统读），退化为不去重直接继续，但在报告里标 `hash_check_degraded: true`。
5. 用户同一文件重复上传（常见场景：误传、语义化命名不一致）会在此处被拦住，避免重复分块、重复入 Milvus。

### 步骤 5: 调用 knowledge-persistence

把上一步写好 frontmatter 的 raw MD 列表交给 `knowledge-persistence`，由它完成：

1. 调用 `bin/chunker.py` 生成基础 chunk Markdown。
2. 调用 `chunk-enrichment` skill 为每个 chunk 生成 `title` / `summary` / `keywords` / 3〜5 条合成 QA，写入 chunk frontmatter。
3. raw/chunks 双落盘（raw 已存在则保留，chunks 新建）。
4. 调 `python bin/milvus-cli.py ingest-chunks --chunk-pattern "data/docs/chunks/<doc_id>-*.md"` 完成 hybrid 入库。

**入库顺序硬约束**（与 get-info 一致）：

1. 写 raw → 调 chunker.py 生成 chunk → 调 chunk-enrichment 填充 frontmatter → 调 CLI 入库。
2. 不允许先入库再回填 questions。
3. 不允许跳过 enrichment 直接入库（除非该 chunk 是空目录页，且明确写 `questions: []`）。
4. 如果 chunk 文件已存在但 enrichment 缺失，先调 `chunk-enrichment` 补填再入库；也可通过 `brain-base-cli enrich-chunks --doc-id <doc_id>` 独立触发。

### 步骤 6: 返回入库摘要

返回给 upload-agent 的报告至少包含：

1. 每个成功文档：`doc_id` / `raw_path` / `original_file` / 生成的 chunk 路径列表 / chunk 数 / question 数。
2. 每个失败文件：输入路径 / 失败阶段（检测 / 转换 / 分块 / 入库）/ 错误信息。
3. `ingest-chunks` 返回的 `chunk_rows` / `question_rows` / `doc_ids` 等关键计数。
4. 若部分失败，明确列出失败部分，不得假装整体成功。

## 6. 持久化最小闭环

一次成功的上传入库任务，至少要完成以下闭环：

1. 原始文件归档到 `data/docs/uploads/<doc_id>/`。
2. raw Markdown 写到 `data/docs/raw/<doc_id>.md`，含 `user-upload` 类型 frontmatter。
3. chunk Markdown 写到 `data/docs/chunks/<doc_id>-<NNN>.md`，含 `questions` 字段（空目录页允许 `questions: []`）。
4. Milvus 入库记录报告含 `chunk_rows` 与 `question_rows`。

**不需要**更新 `priority.json` / `keywords.db`——上传路径没有 URL、没有搜索、没有站点优先级。

## 7. 失败策略

1. `doc-converter` 整体失败（全部 result 为空且 errors 非空）→ 汇报失败原因给用户，不要进入步骤 4。
2. `doc-converter` 部分成功 → 对成功部分继续执行步骤 4 / 5；失败部分单列报告。
3. 步骤 4 frontmatter 组装失败 → 该文档剔除，其他继续。
4. 步骤 5 分块或入库失败 → 不得宣称"持久化完成"；需要保留 raw 与 chunks 文件供人工复查，同时报告失败阶段。
5. 禁止静默吞错：任一阶段失败都必须在最终摘要里可见。

## 8. 与 get-info-workflow 的边界

**严格禁止**：

1. 本 skill 不调用 `web-research-ingest`、不调用 `playwright-cli-ops`、不写 `get-info-workflow` 的任何字段。
2. 本 skill 不处理 URL 类输入；用户给 URL 时应路由回 `get-info-agent`。
3. 本 skill 不写 `priority.json`、不写 `keywords.db`。

**复用**：

1. 本 skill 复用 `knowledge-persistence` 的全部能力（分块规则、合成 QA、Milvus 入库）——这是两条路径的汇合点。
2. 本 skill 复用 `bin/milvus-cli.py ingest-chunks` 与健康检查命令。
