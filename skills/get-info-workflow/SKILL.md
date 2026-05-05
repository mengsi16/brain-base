---
name: get-info-workflow
description: 当 get-info-agent 接收到 QA 的外部补库请求后触发。这个 skill 仅负责搜索 URL 候选列表并分类（official-doc / community / discard），返回结构化结果给 qa-agent。内容抓取、清洗、分块和持久化由 qa-agent 直接调度 content-cleaner-agent 完成，不属于本 skill 职责。
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
5. 步骤5：调用 web-research-ingest（仅搜索+URL分类，返回候选列表） → pending
6. 步骤6：返回 URL 候选列表（含 source_type / title_hint）给 qa-agent → pending

**content-cleaner-agent 不由本 get-info-agent 调度**：Claude Code 不支持三层嵌套 Agent 调用（qa-agent → get-info-agent → content-cleaner-agent）。抓取/清洗/入库的 fan-out 由 qa-agent 在接收到候选列表后直接调度（深度1并行），详见 `qa-workflow` §7.4。

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

1. 执行补库前置检查（Playwright-cli、`milvus-cli`、本地向量化能力）。
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

### 步骤2: 执行前置健康检查（report-and-continue）

执行补库前探测以下依赖，**不再 fail-fast**，改为返回结构化 `infra_status`：

1. `playwright-cli --help` 或 `npx --no-install playwright-cli --help` → `playwright_available`。
2. `python bin/milvus-cli.py inspect-config` → `milvus_config_valid`。
3. `python bin/milvus-cli.py check-runtime --require-local-model --smoke-test` → `milvus_runtime_available`。

#### 2.1 决策矩阵

依据探测结果决定本次任务的走向：

| 场景 | 决策 |
|------|------|
| 三项全部可用 | 正常继续步骤 3〜10 |
| `playwright_available=false` | **立即 abort**：没有抓取能力，无法补库。返回 `{ status: "degraded", reason: "playwright unavailable", unavailable: ["playwright"] }` 给 get-info-agent，它再返回给 qa-workflow 由其进入降级回答模式。**禁止伪造抓取结果或用 requests/curl 绕过** |
| `milvus_*=false` | **部分模式**：Playwright 可用 → 仍可抓取 + 清洗 + 分块 + 落盘到 `data/docs/raw/` 与 `data/docs/chunks/`（本地可 Grep 到），但**跳过 Milvus 入库**，返回 `{ status: "degraded", reason: "milvus unavailable", unavailable: ["milvus"], partial_results: [ { raw_path, chunk_paths } ] }`。qa-workflow 可以直接 Grep 新落盘的 chunks 作为证据 |
| `playwright_available=false` 且 `milvus_*=false` | 立即 abort，返回 `unavailable: ["playwright","milvus"]`，qa-workflow 走完全降级 |

#### 2.2 硬约束

1. 探测阶段总耗时 ≤ 15 秒；超时一律视为不可用。
2. 返回给 get-info-agent 的 `infra_status` 必须是结构化对象，不能是自由文本。get-info-agent 的 Todo 列表里必须有"读取并透传 infra_status"一步。
3. **禁止伪造**：依赖不可用时绝不允许用训练数据伪造抓取结果；正确的处理是上游 qa-workflow 进入降级回答模式，明确告知用户。
4. `partial_results` 中的每个 chunk 必须在 frontmatter 里标 `ingest_status: pending-milvus`，便于后续批量回补入库。

### 步骤3: 读取并更新站点优先级上下文

执行前读取 `data/priority.json` 和 `data/keywords.db`，作用是：

1. 确认当前优先站点。
2. 查看历史高频关键词。
3. 为本次查询记录新的主题热度。

更新原则：

1. 只对本次确实相关的站点与关键词加权。
2. 不能因为一次失败搜索就盲目提升无关站点。
3. 更新时间必须回写。

### 步骤4: 整理检索意图

依据输入内容整理用户意图和约束条件，传递给 `web-research-ingest`：

1. **主题与关键实体**：用户真正要查什么。
2. **时效要求**：是否需要最新资料、是否需要历史版本。
3. **来源偏好**：优先官方文档还是社区内容。
4. **歧义提示**：如果主题存在常见歧义，标注消歧关键词（产品名、版本词、命令名等）。

具体的查询变体生成、站点优先级选择和时间窗口策略由 `web-research-ingest` 步骤1负责。

### 步骤5: 调用 web-research-ingest（仅搜索+分类）

把查询计划交给 `web-research-ingest`，由它完成搜索、候选页筛选和 URL 分类（分类规则见 `web-research-ingest` 步骤3）。本步骤只接收 `web-research-ingest` 返回的 URL 候选列表（含 `source_type` 和 `title_hint`）。

**本步骤不抓取页面正文内容**——正文抓取由 qa-agent 收到候选列表后并行调度 `content-cleaner-agent` 完成。

### 步骤6: 返回 URL 候选列表给 qa-agent

将步骤5得到的候选列表整理为标准 JSON 格式后返回。本 workflow **到此结束**，不执行任何抓取或落盘操作。

```json
{
  "candidates": [
    { "url": "https://...", "source_type": "official-doc", "title_hint": "..." },
    { "url": "https://...", "source_type": "community", "title_hint": "..." }
  ],
  "discarded": 3,
  "infra_status": { "playwright_available": true }
}
```

**为什么在这里停止**：Claude Code 不支持三层嵌套 Agent 调用（qa-agent → get-info-agent → content-cleaner-agent）。内容抓取/清洗/入库的 fan-out 由 qa-agent 在接收到候选列表后直接调度 content-cleaner-agent 完成（深度1并行）。

## 6. 持久化最小闭环

**`get-info-workflow` 自身的成功标准**：搜索得到候选列表，并成功返回给 qa-agent。

**完整入库闭环**由 qa-agent 主导：

1. 有 URL 候选列表（本 workflow 负责）。
2. 至少一个 content-cleaner-agent 实例成功（有 raw + chunks 落盘）。
3. 每个 chunk 的 frontmatter 含 `questions` 字段。
4. 有 Milvus 入库记录，且报告含 `chunk_rows` 与 `question_rows` 计数。
5. 有 `keywords.db` 更新。
6. 有 `priority.json` 时间戳或权重更新。

## 7. 失败策略

1. 步骤5 web-research-ingest 失败 → 直接 abort，返回 `infra_status: degraded` 给 qa-agent。
2. 步骤5 返回空候选列表 → 正常返回空列表，由 qa-agent 决定是否降级回答。

## 8. 与其他组件的协作

1. `qa-agent` 触发 `get-info-agent`。
2. `get-info-agent` 调用本 skill 搜索，返回 URL 候选列表。
3. `qa-agent` 收到候选列表后，直接并行调度 `content-cleaner-agent`（深度1）。
4. `playwright-cli-ops` 负责 Playwright-cli 的稳定调用规范。
5. `web-research-ingest` 负责搜索 + URL 分类，输出候选列表。
6. `content-cleaner-agent` 负责单 URL 的抓取、清洗、落盘、入库（由 qa-agent 并行调度）。
7. `knowledge-persistence` 由 content-cleaner-agent 调用，负责分块与 Milvus 持久化。
8. `update-priority` 由 qa-agent 在所有 cleaner 收齐后调用。
