---
name: get-info-workflow
description: 当 get-info-agent 接收到 QA 的外部补库请求后触发。这个 skill 只负责调度 web-research-ingest 与 knowledge-persistence 两层能力，编排外部检索、清洗、分块和持久化，不负责回答用户问题。
disable-model-invocation: false
---

# Get-Info Workflow

## 0. 强制执行：Todo List

get-info-agent 在执行本 workflow 前，**必须先调用 `TodoList` 工具**，按以下步骤生成 todo 列表，然后严格按列表顺序执行。每完成一步立即更新状态为 `completed`，再进入下一步。**禁止跳步**。

典型 todo 模板：
1. 步骤1：接收并规整任务 → pending
2. 步骤2：前置健康检查（Playwright / Milvus / bge-m3） → pending
3. 步骤3：读取 priority.json + keywords.db → pending
4. 步骤4：生成外部检索计划 → pending
5. 步骤5：调用 web-research-ingest 搜索+抓取 → pending
6. 步骤6：内容提炼与溯源标注 → pending
7. 步骤7：文档级去重与命名 → pending
8. 步骤8：调用 knowledge-persistence（≤5000字整篇1块 / >5000字语义切分 + 合成QA + chunks落盘 + Milvus入库） → pending
9. 步骤9：调用 update-priority 更新 keywords.db + priority.json → pending
10. 步骤10：返回证据摘要给 qa-agent → pending

**步骤8 和步骤9 是最容易被跳过的步骤**。raw 写入 ≠ 持久化完成。必须确认：
- chunks 已落盘到 `data/docs/chunks/`
- 每个 chunk 的 frontmatter 含 `questions` 字段
- `bin/milvus-cli.py ingest-chunks` 已执行且返回 `chunk_rows` + `question_rows`
- `keywords.db` 已更新
- `priority.json` 的 `last_update` 已刷新

以上全部确认后才能标记步骤8和步骤9为 completed。

## 1. 适用场景

在以下场景触发本 skill：

1. `qa-agent` 先判断本地知识不足，并触发 `get-info-agent`。
2. `get-info-agent` 接手后调用本 skill 执行补库编排。
3. 用户明确要求“最新资料”、“官方文档”、“联网补充”。
4. 本地已有资料，但版本老旧、主题残缺、证据相互矛盾，需要重新抓取确认。
5. 需要把新获取的外部资料持久化到知识库，供后续 Grep 与 RAG 使用。
6. 搜索结果中包含非官方来源（博客、教程、问答帖等），其中有值得提炼的知识点，需要提取后标注来源并持久化。

在以下场景不要触发：

1. `qa-agent` 本地证据已经足够且用户没有时效性要求。
2. 用户只是询问 priority 配置本身，不需要联网抓取。
3. 没有明确主题和检索目标，无法形成可执行查询。

调用链约束：

1. `qa-agent -> get-info-agent -> get-info-workflow`。
2. `qa-agent` 不直接调用本 skill。
3. 本 skill 不直接承担 QA 回答。

## 2. 职责边界

本 skill 负责：

1. 执行补库前置检查（Playwright-cli、Milvus MCP、本地向量化能力）。
2. 读取并更新站点优先级上下文。
3. 决定何时调用 `web-research-ingest`。
4. 决定何时调用 `knowledge-persistence`。
5. 确保外部补库任务按“检索/抓取 -> 清洗 -> 分块 -> 落盘 -> 入库 -> 状态更新”的顺序完成。

本 skill 不负责：

1. 直接承担 Playwright-cli 细节操作。
2. 直接承担最终的分块持久化细节。
3. 在抓取失败时编造外部资料。

## 3. 输入

推荐输入字段：

1. 用户原问题。
2. QA 改写后的查询集合。
3. 目标主题与关键实体。
4. 期望覆盖的站点或来源类型。
5. 是否要求最新资料。
6. QA 阶段已有的局部证据和不足说明。

## 4. 输出

输出应包括：

1. 获取到的有效来源列表。
2. 保存下来的 raw 文档路径。
3. 保存下来的 chunk 文档路径。
4. 已写入 Milvus 的文档标识。
5. 已更新的关键词与站点优先级信息。
6. 返回给 QA Agent 的可引用证据摘要。

## 5. 执行流程

### 步骤1: 接收并规整任务

先把任务整理成统一结构：

1. 用户真正要解决的问题是什么。
2. 哪些部分是本地缺失的。
3. 抓取目标更适合官方文档、博客、仓库文档还是问答页。
4. 是否必须优先最新资料。

### 步骤2: 执行前置健康检查

执行补库前必须完成：

1. `playwright-cli --help` 或 `npx --no-install playwright-cli --help`。
2. 校验 Milvus MCP 是否可用（通过 `/mcp` 查看 `milvus` server 状态）。
3. 执行 `python bin/milvus-cli.py check-runtime --require-local-model --smoke-test`。

任一检查失败都必须 fail-fast，禁止进入抓取和持久化。

### 步骤3: 读取并更新站点优先级上下文

执行前读取 `data/priority.json` 和 `data/keywords.db`，作用是：

1. 确认当前优先站点。
2. 查看历史高频关键词。
3. 为本次查询记录新的主题热度。

更新原则：

1. 只对本次确实相关的站点与关键词加权。
2. 不能因为一次失败搜索就盲目提升无关站点。
3. 更新时间必须回写。

### 步骤4: 生成外部检索计划

依据输入内容确定：

1. 主查询。
2. 候选查询变体。
3. 候选站点优先级。
4. 搜索顺序。

检索计划建议：

1. 先用主题最稳定、歧义最小的查询。
2. 对官方术语优先搜索官方站点。
3. 如果主题存在常见歧义，为查询补上产品名、版本词、文件名、命令名。

### 步骤5: 调用 web-research-ingest

这一层不直接描述 Playwright-cli 命令细节，而是要求：

1. 把查询计划交给 `web-research-ingest`。
2. 让它基于 `playwright-cli-ops` 完成搜索、候选页筛选、正文抓取和初步清洗。
3. 返回结构化文档草稿。

### 步骤6: 内容提炼与溯源标注

`web-research-ingest` 返回的文档草稿中，并非全部来自官方文档。对于非官方来源（博客、技术文章、教程、问答帖等），不应整篇入库，但其中可能包含值得保留的知识点。本步骤负责筛选与提炼。

#### 6.1 来源分类

对 `web-research-ingest` 返回的每一篇文档草稿，按来源类型分类：

1. **official-doc**：官方文档、官方仓库文档、权威说明页 → 直接进入步骤 7，不做提炼。
2. **community**：技术博客、教程、问答帖、社区讨论 → 进入提炼流程。
3. **discard**：纯营销页、广告页、聚合页、搜索结果页、目录页 → 丢弃，不进入提炼。

分类采用**白名单 + LLM 判断**的两级机制：

##### 第 1 级：白名单快速路径

1. 从文档 URL 提取域名（或"域名+路径前缀"，如 `github.com/anthropics`）。
2. 查询 `priority.json` 的 `official_domains` 数组。
3. 命中 → 直接归类为 `official-doc`，不再交 LLM 判断。

##### 第 2 级：LLM 综合判断（白名单未命中时）

LLM 判断依据：

1. **URL 结构特征**：如 `docs.*` / `api.*` / `*.io` 子域名、官方 GitHub 组织仓库路径。
2. **页面内容特征**：是否以产品方第一人称撰写、是否为规范性/参考性文档。
3. **署名与作者**：是否为官方团队、产品方账号。
4. **反例**：`medium.com/@...`、`dev.to/...`、`zhihu.com/...`、个人博客 → 归入 `community`；营销落地页、导航页、聚合榜单 → 归入 `discard`。

LLM 输出四分类结果：`official-high` / `official-low` / `community` / `discard`。

1. `official-high`（高置信度官方）→ 归类 `official-doc`，**且在本轮任务报告中标注"发现新官方域名候选"**，由 `update-priority` 在收尾阶段回填到 `priority.json.official_domains`。
2. `official-low`（看起来像官方但不确定）→ 归类 `official-doc` 本次，但**不**回填白名单（避免污染）。
3. `community` → 进入步骤 6.2 提炼流程。
4. `discard` → 丢弃。

##### 兜底规则

1. 如果 LLM 判断本身失败或模糊，默认归入 `community` 而非 `discard`（保守策略，宁可提炼后丢弃也不漏掉有用内容）。
2. 白名单仅作为分类加速通道，不是安全边界；用户可随时手动编辑 `priority.json` 删除误收项。

#### 6.2 提炼规则（仅对 community 类型）

对每篇 `community` 类型文档，调用 LLM 执行提炼：

1. **提炼目标**：只提取与本次检索主题直接相关的、事实性或可操作的知识点。
2. **提炼约束**：
   - 每个知识点必须是一条完整、自包含的信息（脱离原文也能理解）。
   - 不得编造原文未涉及的内容。
   - 跳过纯观点性、主观评价性内容（除非是权威人物的明确结论）。
   - 跳过仅复述官方文档而未增加新信息的内容。
   - 跳过无法验证的声明。
3. **溯源标注**：每个提炼出的知识点必须附带：
   - `url`：原始来源网址。
   - `source_title`：原始页面标题。
   - `source_author`：作者（如可识别）。
   - `extracted_at`：提炼日期（YYYY-MM-DD）。
4. **重组为文档**：将同一主题的多个提炼知识点合并为一篇 Markdown 文档，结构为：
   - frontmatter 中 `source_type: extracted`（区别于官方文档的 `official-doc`）。
   - frontmatter 中 `urls` 字段列出所有来源 URL（JSON inline 数组）。
   - 正文按知识点逻辑分组，每个知识点前用 `> 来源: <url>` 标注出处。
5. **质量门槛**：提炼后文档正文必须 ≥ 200 字符，否则丢弃（说明该非官方来源无实质可提炼内容）。

#### 6.3 输出

本步骤输出两类文档草稿：

1. **official-doc 类型**：原样传递，不做修改。
2. **extracted 类型**：经过提炼、溯源标注、重组后的新文档草稿。

两类文档统一进入步骤 7 进行去重与命名。

### 步骤7: 文档级去重与命名

落盘前必须做文档级判断：

1. 是否已经存在相同 URL 的文档。
2. 是否已经存在标题高度相似、主题高度相似的文档。
3. 如果是同一主题的新版本，应该新增新文档并在 metadata 中保留版本或抓取时间，而不是粗暴覆盖。

命名策略（强制）：

1. `doc_id` 必须带抓取日期，格式：`<topic-slug>-YYYY-MM-DD`。
2. raw 文件名必须等于 `doc_id`，即：`data/docs/raw/<doc_id>.md`。
3. chunk 文件名必须为：`data/docs/chunks/<doc_id>-<chunk-index>.md`（建议使用 3 位序号，如 `001`）。
4. `chunk_id` 必须与 chunk 文件名（去掉 `.md`）一致。
5. 同一主题的新版本必须生成新 `doc_id`（日期变化），禁止覆盖旧版本。

### 步骤8: 调用 knowledge-persistence

这一层不直接承担分块和落盘细节，而是要求：

1. 把结构化文档草稿交给 `knowledge-persistence`。
2. 由它按 5000 字符阈值规则完成分块（≤ 5000 字符整篇为 1 块；> 5000 字符按语义边界切，每块上限 5000）。
3. 在每个 chunk Markdown 落盘**之前**，由它对该 chunk 调用 LLM 生成 3〜5 条合成 QA 问题，并以 JSON inline 数组形式写入 chunk frontmatter 的 `questions` 字段。
4. 由它完成 raw/chunks 双落盘。
5. 由它调用 `python bin/milvus-cli.py ingest-chunks` 完成 hybrid 入库——CLI 会为每个 chunk 写入 1 行 `kind=chunk` + 每条 question 1 行 `kind=question`，全部共享 `chunk_id`。
6. 由它更新 `keywords.db` 与 `priority.json`。

入库顺序硬约束：

1. **生成 chunk 文本 → 生成合成 QA → 写入 chunk frontmatter → 写盘 → 调 CLI 入库**。
2. 不允许先入库再回填 questions（那会让 question 行漏掉）。
3. 不允许跳过合成 QA 直接入库（除非该 chunk 是空目录页，且明确写 `questions: []`）。

### 步骤9: 返回给 QA Agent

返回结果至少包括：

1. 新增 raw 文档路径。
2. 新增 chunk 文档路径。
3. 可直接用于回答的证据摘要。
4. 如果抓取失败或证据仍不足，要明确失败点。

## 6. 持久化最小闭环

一次成功的 Get-Info 任务，至少要完成以下闭环：

1. 有搜索证据。
2. 有正文抓取结果。
3. 若含非官方来源，有提炼记录（标注了来源 URL）。
4. 有 raw Markdown。
5. 有 chunk Markdown（已遵守 5000 字符阈值规则）。
6. 每个 chunk 的 frontmatter 含 `questions` 字段（除空目录页外应有 3〜5 个问题）；extracted 类型 chunk 的 frontmatter 还必须含 `source_type: extracted` 和 `urls` 字段。
7. 有 Milvus 入库记录，且报告同时含 `chunk_rows` 与 `question_rows` 计数。
8. 有 `keywords.db` 更新。
9. 有 `priority.json` 时间戳或权重更新。

缺任何一环，都不应宣称"知识已完成持久化"。

## 7. 失败策略

遵守 fail-fast：

1. 子 skill 任一步骤失败时，直接暴露错误。
2. 如果抓到的内容质量不足，不要强行进入持久化层。

## 8. 与其他组件的协作

1. `qa-agent` 触发 `get-info-agent`。
2. `get-info-agent` 调用本 skill 做编排。
3. `playwright-cli-ops` 负责 Playwright-cli 的稳定调用规范。
4. `web-research-ingest` 负责网页检索、抓取、初步清洗。
5. `knowledge-persistence` 负责分块与持久化。
6. `update-priority` 负责优先级更新规则的维护说明。
