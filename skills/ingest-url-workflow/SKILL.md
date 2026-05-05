---
name: ingest-url-workflow
description: 当 ingest-url-agent 接收到已知 URL 列表后触发。负责对每个 URL 调度 content-cleaner-agent 完成抓取、清洗、分块、富化、Milvus 入库，最后调用 update-priority 更新关键词和站点优先级。输入是已知 URL，不需要搜索发现。
disable-model-invocation: false
---

# Ingest URL Workflow

## 0. 强制执行：Todo List

ingest-url-agent 在执行本 workflow 前，**必须先调用 `TodoList` 工具**，按以下步骤生成 todo 列表，然后严格按列表顺序执行。每完成一步立即更新状态为 `completed`，再进入下一步。**禁止跳步**。

典型 todo 模板：
1. 步骤1：解析输入（URL 列表 / source_type / topic） → pending
2. 步骤2：前置健康检查（Playwright / Milvus / bge-m3） → pending
3. 步骤3：分批调度 content-cleaner-agent（每批 ≤5 个并行） → pending
4. 步骤4：汇总所有 content-cleaner-agent 结果 → pending
5. 步骤5：调用 update-priority 更新 keywords.db + priority.json → pending
6. 步骤6：返回入库摘要 → pending

**步骤3 是最容易跳过的步骤**。URL 列表已知 ≠ 入库完成。必须确认每个 content-cleaner-agent 实例均已返回摘要（包含 chunk_rows + question_rows），才能标记步骤3为 completed。

## 1. 适用场景

在以下场景触发本 skill：

1. 用户通过 `brain-base-cli ingest-url --url <url1> --url <url2>` 命令触发。
2. 已知 URL 列表，需要完整入库（抓取 → 清洗 → 分块 → 富化 → Milvus 入库）。
3. 不需要搜索发现 URL——URL 已经明确给出。

在以下场景不要触发：

1. 需要搜索发现 URL——走 `get-info-agent` + `get-info-workflow`。
2. 用户问问题——走 `qa-agent`。
3. 本地文件上传——走 `upload-agent` + `upload-ingest`。

## 2. 职责边界

本 skill 负责：

1. 校验输入 URL 列表。
2. 前置健康检查。
3. 对每个 URL 调度 `content-cleaner-agent` 完成完整入库闭环。
4. 汇总所有 URL 的入库结果。
5. 调用 `update-priority` 更新关键词和站点优先级。

本 skill 不负责：

1. 搜索发现 URL（那是 `get-info-workflow` 的职责）。
2. 单个 URL 的抓取/清洗细节（那是 `content-cleaner-workflow` 的职责）。
3. 分块/富化/Milvus 入库的具体实现（那是 `knowledge-persistence` 的职责）。
4. 回答用户问题（那是 `qa-workflow` 的职责）。

## 3. 输入

| 字段 | 必填 | 说明 |
|------|------|------|
| `urls` | ✅ | URL 列表（1~N 个） |
| `source_type` | 可选 | 统一指定 `official-doc` 或 `community`；未指定时由 content-cleaner-agent 根据域名自动判断 |
| `topic` | 可选 | 主题关键词，用于 doc_id 命名 |

## 4. 输出

返回 JSON 摘要：

```json
{
  "total_urls": 3,
  "success": 2,
  "failed": 1,
  "results": [
    {
      "url": "https://...",
      "doc_id": "openwrt-repo-2026-05-04",
      "source_type": "official-doc",
      "raw_path": "data/docs/raw/openwrt-repo-2026-05-04.md",
      "chunk_paths": ["data/docs/chunks/openwrt-repo-2026-05-04-001.md"],
      "chunk_rows": 1,
      "question_rows": 5,
      "content_sha256": "<由 content-cleaner-agent 计算>",
      "extraction_status": "ok",
      "errors": []
    }
  ],
  "failures": [
    {
      "url": "https://...",
      "stage": "clean",
      "error": "SPA 抓取失败"
    }
  ],
  "priority_updated": true
}
```

## 5. 执行流程

### 步骤1：解析输入

1. 确认 URL 列表非空。
2. 对每个 URL 做基本格式校验（以 `http://` 或 `https://` 开头）。
3. 确定 `source_type`：用户指定 → 使用指定值；未指定 → 根据域名启发式判断（`.org`/`.dev`/`docs.*` → `official-doc`，其余 → `community`）。
4. 确定 `topic`：用户指定 → 使用指定值；未指定 → 从 URL 域名或路径提取。

### 步骤2：前置健康检查

执行入库前必须完成：

1. `python bin/milvus-cli.py check-runtime --require-local-model --smoke-test` — 确认 Milvus + bge-m3 可用。
2. Playwright 可用性检查（`npx --no-install playwright-cli --help` 或 `playwright-cli --help`）。

决策矩阵（与 `get-info-workflow` §2.1 一致）：

| 场景 | 决策 |
|------|------|
| 全部可用 | 正常继续 |
| Playwright 不可用 | 立即 abort，无法抓取页面 |
| Milvus 不可用 | 部分模式：仍可抓取+清洗+落盘，跳过 Milvus 入库，chunk 标 `ingest_status: pending-milvus` |
| 两者都不可用 | 立即 abort |

### 步骤3：分批调度 content-cleaner-agent

对 URL 列表分批调度，每批最多 5 个并行：

1. 每批 ≤5 个 URL，通过 `Agent` tool 并行调用 `content-cleaner-agent`。
2. 对每个 URL 传入：`url` / `source_type` / `topic`。
3. 等待当前批所有实例返回后，再启动下一批。
4. 某个实例失败时记录错误，不影响同批其他实例和后续批次。

**每个 content-cleaner-agent 实例负责完整闭环**（`content-cleaner-workflow` 定义）：
- 抓取页面 → 清洗为 Markdown → 写 raw → 调 chunker.py → 调 chunk-enrichment → 调 milvus-cli ingest-chunks

### 步骤4：汇总所有 content-cleaner-agent 结果

收集所有实例的返回摘要，统计：

1. 成功数 / 失败数。
2. 总 chunk_rows / question_rows。
3. 失败列表（含 URL / 失败阶段 / 错误信息）。

### 步骤5：调用 update-priority

入库成功后调用 `update-priority` skill：

1. 更新 `keywords.db`：写入本次入库涉及的关键词。
2. 更新 `priority.json`：写入本次涉及的站点域名和时间戳。

### 步骤6：返回入库摘要

按 §4 的 JSON 格式返回摘要。部分失败时不得宣称整体成功。

## 6. 持久化最小闭环

一次成功的 URL 入库任务，每个 URL 至少要完成：

1. raw Markdown 写到 `data/docs/raw/<doc_id>.md`，含 frontmatter + `content_sha256`。
2. chunk Markdown 写到 `data/docs/chunks/<doc_id>-<NNN>.md`，含 `questions` 字段。
3. Milvus 入库记录报告含 `chunk_rows` 与 `question_rows`。

## 7. 失败策略

1. 前置健康检查 Playwright 不可用 → 立即 abort，无法抓取。
2. 某个 content-cleaner-agent 实例失败 → 记录错误，继续其他 URL。
3. 所有 URL 均失败 → 返回全部失败列表，不调用 update-priority。
4. 部分成功 → 对成功部分调用 update-priority，失败部分单列报告。
5. update-priority 失败 → 不影响入库结果，在摘要中标注 `priority_updated: false`。
