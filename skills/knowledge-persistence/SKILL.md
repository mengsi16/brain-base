---
name: knowledge-persistence
description: 当 get-info-agent 已拿到清洗后的文档草稿，需要把知识工业级写入本地和检索层时触发。负责 LLM 分块、raw/chunks 双落盘、Milvus 持久化、SQLite 关键词更新，以及与 Milvus MCP 工具的协作约束。
disable-model-invocation: false
---

# Knowledge Persistence

## 1. 职责边界

本 skill 负责：

1. 生成 raw Markdown。
2. 调用 Claude Code 或 Codex 模型进行语义分块。
3. 生成 chunk Markdown。
4. 调用对外暴露的 Milvus MCP 工具或等价 Milvus 持久化层完成入库。
5. 更新 `keywords.db` 与 `priority.json`。

本 skill 不负责：

1. 外部网页抓取。
2. 搜索引擎调度。

## 2. 原始文档保存

raw 文档必须：

1. 保存到 `data/docs/raw/`。
2. 使用 UTF-8 编码。
3. 带 YAML metadata。
4. 保留完整正文结构。
5. `doc_id` 必须带抓取日期，格式：`<topic-slug>-YYYY-MM-DD`。
6. raw 文件名必须与 `doc_id` 一致。

## 3. 分块规则

分块必须由 Claude Code 或 Codex 模型完成，遵守：

1. 先识别 Markdown 标题层级。
2. 对步骤型内容按阶段切块。
3. 对 FAQ 按问答切块。
4. 不在代码块、表格、列表中间硬切。
5. 每个 chunk 聚焦单一主题。
6. 必要时允许轻度重叠。
7. 每个 chunk 保留标题路径、摘要、关键词、来源 URL。

## 4. 分块文档保存

chunk 文档必须：

1. 保存到 `data/docs/chunks/`。
2. 与 raw 共享 `doc_id`。
3. 每块有唯一 `chunk_id`。
4. 可被 Grep 直接命中。
5. 文件名格式必须是 `<doc_id>-<chunk-index>.md`（建议使用 3 位序号，如 `001`）。
6. `chunk_id` 必须与 chunk 文件名（去掉 `.md`）一致。

## 5. Milvus 持久化

Milvus 层要求：

1. 禁止使用伪造向量。
2. 必须使用能返回 embedding 的 provider。
3. 支持 dense、text/BM25、hybrid 三类路径。
4. 优先通过插件根目录 `.mcp.json` 接入的官方 Milvus MCP Server（`zilliztech/mcp-server-milvus`）执行检索和写入。
5. `mcp/milvus-rag/` 仅是项目内适配层，不是 Milvus 官方原生能力。
6. 入库前必须执行 `python bin/milvus-cli.py check-runtime --require-local-model --smoke-test`，确认本地向量化模型可用。

## 6. 失败策略

1. embedding provider 未配置时直接报错。
2. raw 落盘失败、chunk 落盘失败、Milvus 入库失败、SQLite 更新失败都要单独报错。
3. 任一步骤失败，不得宣称“持久化完成”。
