---
name: content-cleaner-workflow
description: 当 content-cleaner-agent 接收到单个 URL 后触发。负责抓取页面内容、清洗为 Markdown、校验完整性、写入 raw、调用 knowledge-persistence 分块入库。一个 URL = 一次调用。
disable-model-invocation: false
---

# Content Cleaner Workflow

## 0. 强制执行：Todo List

content-cleaner-agent 在执行本 workflow 前，**必须先调用 `TodoList` 工具**，按以下步骤生成 todo 列表，然后严格按列表顺序执行。每完成一步立即更新状态为 `completed`，再进入下一步。**禁止跳步**。

典型 todo 模板：
1. 步骤1：解析输入（url / source_type / topic） → pending
2. 步骤2：调用 playwright-cli-ops 抓取页面内容 → pending
3. 步骤3：清洗为 Markdown → pending
4. 步骤4：完整性校验（章节计数 / 长度比对） → pending
5. 步骤5：计算 content_sha256 + hash-lookup 去重 → pending
6. 步骤6：组装 frontmatter，写入 data/docs/raw/ → pending
7. 步骤7：调用 knowledge-persistence 分块+入库 → pending
8. 步骤8：返回摘要 JSON → pending

## 1. 输入

| 字段 | 必填 | 说明 |
|------|------|------|
| `url` | ✅ | 要抓取的页面 URL（单个字符串） |
| `source_type` | ✅ | `official-doc` 或 `community`，由 get-info-agent 分类后传入 |
| `topic` | ✅ | 主题关键词，用于 doc_id 命名（如 `openclaw`） |
| `title_hint` | 可选 | 页面标题提示 |

## 2. 执行步骤

### 步骤1：解析输入

确认 `url`、`source_type`、`topic` 均已提供；任一缺失直接 fail-fast 报错。

### 步骤2：调用 playwright-cli-ops 抓取

调用 `playwright-cli-ops` 打开 `url` 并导出原始页面内容：

1. 记录原始抓取内容的字符数 `raw_char_count`（清洗校验用）。
2. 如果 `playwright-cli-ops` 返回 `spa_render_failed: true`，进入步骤 3 降级路径，在 frontmatter 标注 `extraction_status: spa-failed`，正文只保留元信息，**禁止用 LLM 训练知识补写正文**。

### 步骤3：清洗为 Markdown

#### 3.1 所有来源共同规则

1. 去掉导航栏、侧边栏、面包屑、页脚链接、Cookie 提示、推荐阅读、广告。
2. 保留标题层级、正文、代码块、表格、FAQ、步骤列表、注意事项（callout）。
3. 翻译是允许的，但翻译不得遗漏原文的任何章节或段落。

#### 3.2 official-doc 额外硬约束

1. **禁止概括/缩写/改写**：每个段落、列表项、代码块必须完整保留（翻译时字数可变，但内容不可丢失）。
2. **章节结构不变**：原始页面有多少个二级标题，清洗后就必须有多少个二级标题，禁止合并或删除章节。
3. **禁止跨 URL 合并**：本步骤只处理一个 URL，禁止引入其他 URL 的内容。

#### 3.3 community 额外规则

1. 提取与本次主题直接相关的、事实性或可操作的知识点。
2. 每个知识点必须自包含（脱离原文也能理解）。
3. 每个知识点前标注 `> 来源: <url>`。
4. 跳过纯观点性、无法验证的声明。
5. 提炼后正文 ≥ 200 字符，否则丢弃（返回 `extraction_status: insufficient-content`）。

### 步骤4：完整性校验

清洗完成后必须校验，**不通过则回退步骤3重做**：

1. **长度比率**：清洗后正文字符数 ≥ `raw_char_count × 0.5`（排除导航/广告删除的合理部分）。如果低于 50% 且无法解释原因，判定为过度清洗，回退重做。
2. **official-doc 章节计数**：清洗前后二级标题数量一致。如有差异，逐章节检查是否有内容被误删。
3. **无 LLM 补写痕迹**：对照 URL 内容，确认没有引入原页面不存在的段落。

### 步骤5：content_sha256 + hash-lookup 去重

1. 将清洗后正文按 LF 规范化（`\r\n` → `\n`），`strip("\n")`，计算 SHA-256。
2. 调用 `python bin/milvus-cli.py hash-lookup <sha256>`：
   - `status: "hit"` → 内容已存在，**跳过落盘**，返回 `{skipped: true, reason: "content_identical", existing_doc_ids: [...]}`。
   - `status: "miss"` → 继续步骤6，把 `content_sha256` 写入 frontmatter。

### 步骤6：组装 frontmatter，写入 raw

raw 文件写入 `data/docs/raw/<doc_id>.md`，frontmatter 模板：

```yaml
---
doc_id: <topic-slug>-YYYY-MM-DD
title: <页面标题>
source_type: official-doc | community
url: <单个页面 URL，不写站点首页>
fetched_at: YYYY-MM-DD
content_sha256: <步骤5计算的哈希>
keywords:
  - <关键词1>
  - <关键词2>
---
```

命名规则：
- `doc_id` 格式：`<topic-slug>-YYYY-MM-DD`，同主题同日多 URL 加 `-N` 后缀（如 `-2`、`-3`）。
- raw 文件名等于 `doc_id`。

### 步骤7：调用 knowledge-persistence

把 raw 文档草稿交给 `knowledge-persistence` skill，由它完成：

1. 调用 `bin/chunker.py` 生成 chunk Markdown。
2. 调用 `chunk-enrichment` skill 填充 frontmatter（title/summary/keywords/questions）。
3. 调用 `python bin/milvus-cli.py ingest-chunks` 完成 hybrid 入库。

### 步骤8：返回摘要

返回 JSON：

```json
{
  "url": "https://...",
  "doc_id": "openclaw-overview-2026-05-03",
  "source_type": "official-doc",
  "raw_path": "data/docs/raw/openclaw-overview-2026-05-03.md",
  "chunk_paths": ["data/docs/chunks/openclaw-overview-2026-05-03-001.md"],
  "chunk_rows": 1,
  "question_rows": 5,
  "content_sha256": "abc123...",
  "extraction_status": "ok",
  "errors": []
}
```

`extraction_status` 枚举值：`ok` / `spa-failed` / `insufficient-content` / `over-cleaned`（校验失败后最终放弃）。

## 3. 失败策略

1. 步骤2 抓取失败 → 返回 `extraction_status: spa-failed`，不进入后续步骤，不伪造内容。
2. 步骤4 校验失败且重做两次仍不通过 → 返回 `extraction_status: over-cleaned`，不落盘。
3. 步骤5 hash-lookup 命中 → 正常返回 `skipped: true`，不算错误。
4. 步骤7 knowledge-persistence 失败 → raw 文件已写盘，标注 `ingest_status: pending-milvus`，返回错误详情。

## 4. 职责边界

本 skill 负责：单 URL 抓取、清洗、落盘、入库。

本 skill 不负责：
- 搜索/发现 URL（由 `web-research-ingest` 负责）。
- 分类 URL 为 official-doc/community/discard（由 `get-info-workflow` 步骤6负责）。
- 并行编排多个 URL（由 `get-info-agent` 用 `Agent` tool 并行调用多个 `content-cleaner-agent` 实例）。
- 回答用户问题（由 `qa-agent` 负责）。
