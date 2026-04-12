---
name: get-info-workflow
description: 当 get-info-agent 接收到 QA 的外部补库请求后触发。这个 skill 只负责调度 web-research-ingest 与 knowledge-persistence 两层能力，编排外部检索、清洗、分块和持久化，不负责回答用户问题。
disable-model-invocation: false
---

# Get-Info Workflow

## 1. 适用场景

在以下场景触发本 skill：

1. `qa-agent` 先判断本地知识不足，并触发 `get-info-agent`。
2. `get-info-agent` 接手后调用本 skill 执行补库编排。
3. 用户明确要求“最新资料”、“官方文档”、“联网补充”。
4. 本地已有资料，但版本老旧、主题残缺、证据相互矛盾，需要重新抓取确认。
5. 需要把新获取的外部资料持久化到知识库，供后续 Grep 与 RAG 使用。

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

### 步骤6: 文档级去重与命名

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

### 步骤7: 调用 knowledge-persistence

这一层不直接承担分块和落盘细节，而是要求：

1. 把结构化文档草稿交给 `knowledge-persistence`。
2. 由它完成 raw/chunks 双落盘。
3. 由它通过 Milvus MCP 或等价持久化层执行入库。
4. 由它更新 `keywords.db` 与 `priority.json`。

### 步骤8: 返回给 QA Agent

返回结果至少包括：

1. 新增 raw 文档路径。
2. 新增 chunk 文档路径。
3. 可直接用于回答的证据摘要。
4. 如果抓取失败或证据仍不足，要明确失败点。

## 6. 持久化最小闭环

一次成功的 Get-Info 任务，至少要完成以下闭环：

1. 有搜索证据。
2. 有正文抓取结果。
3. 有 raw Markdown。
4. 有 chunk Markdown。
5. 有 Milvus 入库记录。
6. 有 `keywords.db` 更新。
7. 有 `priority.json` 时间戳或权重更新。

缺任何一环，都不应宣称“知识已完成持久化”。

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
