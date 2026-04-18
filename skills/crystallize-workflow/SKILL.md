---
name: crystallize-workflow
description: 当 qa-agent 已基于本地或新抓取证据完成一次满意回答后触发，或当 qa-agent 启动时需要判断"用户问题是否命中已有固化答案"时触发。本 skill 负责固化答案的读写、命中判断、新鲜度判断、刷新调度，是自进化整理文档层的工作流定义。
disable-model-invocation: false
---

# Crystallize Workflow

## 0. 强制执行：Todo List

organize-agent 在执行本 workflow 前，**必须先调用 `TodoList` 工具**，按当前 mode 的步骤生成 todo 列表，然后严格按列表顺序执行。每完成一步立即更新状态为 `completed`，再进入下一步。**禁止跳步**。

**hit_check 模式**（由 qa-agent 在 qa-workflow 步骤0调用）：
1. 检查 data/crystallized/index.json 是否存在 → pending
2. 关键词粗筛 → pending
3. 语义精判 → pending
4. 新鲜度判断 → pending
5. 返回命中结果 → pending

**crystallize 模式**：
1. 读取 index.json 检查同主题 skill → pending
2. 生成 skill_id + frontmatter → pending
3. 写入 <skill_id>.md → pending
4. 更新 index.json → pending

**refresh 模式**：
1. 读取原 skill 提取 execution_trace + pitfalls → pending
2. 覆盖写回 <skill_id>.md（revision+1） → pending
3. 更新 index.json → pending

**feedback 模式**：
1. 更新 user_feedback 状态 → pending
2. 更新 index.json → pending

**重要**：本 skill 由 organize-agent 执行，organize-agent 是 subagent，**不与用户交互**。所有用户反馈由 qa-agent 在主会话中捕获后传入。固化写入是自动的，不需要询问用户。

## 1. 背景

本 skill 实现 Karpathy [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) 模式下的**自进化整理层**：在原始文档（`data/docs/raw/` + `data/docs/chunks/` + Milvus）之上，额外维护一层由 LLM 整理、可直接复用的**固化答案**。

关键差异：

1. **RAG 层**：每次问答都要重新检索、重新综合。
2. **固化层**：问答成功后整理一次，相似问题直接返回，不再重跑 RAG。

## 2. 职责边界

本 skill 负责：

1. 判断用户问题是否命中已有 Crystallized Skill。
2. 判断命中 skill 的新鲜度（`last_confirmed_at + freshness_ttl_days` vs now）。
3. 生成新固化答案的 Markdown 文件与 `index.json` 条目。
4. 刷新过期 skill（通过 organize-agent 协调 get-info-agent）。
5. 处理用户反馈状态迁移（pending / confirmed / rejected）。

本 skill 不负责：

1. 直接执行 RAG 检索（那是 `qa-workflow` 的职责）。
2. 直接执行网页抓取（那是 `get-info-agent` 及其子 skill 的职责）。
3. 修改 `data/docs/raw/` 或 `data/docs/chunks/` 或 Milvus（**固化层不侵入原始层**）。
4. 运行统计与健康清理（那是 `crystallize-lint` 的职责）。

## 3. 目录与文件

### 3.1 存储位置

```
data/crystallized/
├── index.json              # 全局索引
└── <skill_id>.md           # 每条固化 skill 一个文件
```

`data/crystallized/` 目录由本 skill 在首次写入时自动创建（与 `data/docs/raw/` 的处理一致）。`.gitignore` 已忽略整个 `data/`，固化层不会进入 git。

### 3.2 `skill_id` 命名规则

`<topic-slug>-<YYYY-MM-DD>`：

1. `topic-slug`：小写短横线连接的主题标识，对应该 skill 回答的问题主题。
2. `YYYY-MM-DD`：首次固化日期。

示例：

1. `claude-code-subagent-design-2026-04-18`
2. `anthropic-mcp-server-setup-2026-04-18`

同一主题的重写（revision > 1）**保留原 skill_id**（即原始日期），通过 `revision` 字段递增。只有完全不同主题才生成新 skill_id。

### 3.3 `index.json` 结构

```json
{
  "version": "1.0.0",
  "updated_at": "2026-04-18T23:40:00+08:00",
  "skills": [
    {
      "skill_id": "claude-code-subagent-design-2026-04-18",
      "description": "用户询问 Claude Code subagent 的设计思路、架构、配置方式等相似问题时触发",
      "trigger_keywords": ["claude code", "subagent", "子 agent", "agent 架构"],
      "last_confirmed_at": "2026-04-18T23:40:00+08:00",
      "freshness_ttl_days": 90,
      "revision": 1,
      "user_feedback": "pending"
    }
  ]
}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `skill_id` | ✅ | 对应 `<skill_id>.md` 的文件名（去掉 `.md`） |
| `description` | ✅ | 自然语言触发描述。用户问题与此语义匹配即命中 |
| `trigger_keywords` | ✅ | 关键词数组，用于 grep 快速过滤（JSON inline 数组） |
| `last_confirmed_at` | ✅ | 最后一次被确认可用的时间（ISO-8601 含时区） |
| `freshness_ttl_days` | ✅ | 新鲜度阈值（天），超过即需刷新 |
| `revision` | ✅ | 修订版本号，从 1 开始 |
| `user_feedback` | ✅ | `pending` / `confirmed` / `rejected` 三态 |

### 3.4 固化 Markdown 文件结构

```markdown
---
skill_id: claude-code-subagent-design-2026-04-18
description: 用户询问 Claude Code subagent 的设计思路、架构、配置方式等相似问题时触发
trigger_keywords: ["claude code", "subagent", "子 agent", "agent 架构"]
created_at: 2026-04-18T23:40:00+08:00
last_confirmed_at: 2026-04-18T23:40:00+08:00
freshness_ttl_days: 90
revision: 1
user_feedback: pending
source_chunks:
  - claude-code-subagent-2026-04-15-001
  - claude-code-subagent-2026-04-15-002
source_urls:
  - https://docs.anthropic.com/claude-code/subagents
---

# 固化答案：Claude Code Subagent 设计思路

<可直接返回给用户的答案正文，Markdown 格式>

## 执行路径（execution_trace）

1. 访问 `https://docs.anthropic.com/claude-code/subagents`（来自 `priority.json` 的 anthropic 站点）
2. Playwright-cli 抓取正文后生成 `data/docs/chunks/claude-code-subagent-2026-04-15-001.md`
3. multi-query-search 使用查询变体：`claude code subagent 配置` / `Claude Code subagent configuration` / ...

## 遇到的坑（pitfalls）

1. 第一次搜索 `sub-agent`（带连字符）在官方站点命中为零，应使用 `subagent`（无连字符）。
2. 官方文档对 YAML frontmatter 必填字段的说明在 "Creating subagents" 小节，不在目录首页。
```

frontmatter 约束：

1. `trigger_keywords`、`source_chunks`、`source_urls` 全部使用 **JSON inline 数组**或 YAML list 两种形式之一，由 organize-agent 固定采用 JSON inline（与 `questions` 字段一致，避免引入 PyYAML 依赖）。
2. 时间戳统一 ISO-8601 含时区（例 `2026-04-18T23:40:00+08:00`）。
3. `execution_trace` 与 `pitfalls` 写在**正文里**，不放 frontmatter——便于阅读和 LLM 直接引用。

## 4. 执行流程

### 步骤 1：命中判断（由 qa-agent 在 qa-workflow 步骤 0 调用）

输入：用户问题原文 + 已提取的关键实体与术语。

执行：

1. 检查 `data/crystallized/index.json` 是否存在。不存在 → 返回"无命中，走 RAG"。
2. 读取 `index.json`，遍历 `skills` 数组。
3. 对每条 skill：
   - **关键词粗筛**：用户问题是否包含该 skill 的 `trigger_keywords` 任一项（大小写不敏感）。
   - **语义精判**：粗筛命中的 skill 交给 LLM 判断其 `description` 与用户问题的语义相似度。
4. 若有多个 skill 语义命中，取 `last_confirmed_at` 最新的一条。
5. 若无命中，返回"无命中，走 RAG"。

**命中判定阈值**（由 LLM 自行判断，无需数值化）：

1. 用户问题与 `description` 描述的触发场景**主题一致、关键实体重合、意图相同**。
2. 允许措辞不同、语言不同（中英），但**不允许主题漂移**（例如 skill 是"subagent 设计"，用户问的是"MCP 配置"，不命中）。

### 步骤 2：新鲜度判断（命中后）

输入：命中的 skill 条目。

执行：

1. 计算 `expires_at = last_confirmed_at + freshness_ttl_days`。
2. `now < expires_at` → **新鲜**，直接返回该 skill 的 `.md` 文件内容给 qa-agent。
3. `now >= expires_at` → **过期**，进入步骤 3 刷新。

### 步骤 3：刷新过期 skill（过期命中）

执行：

1. 读取 `data/crystallized/<skill_id>.md`，提取「执行路径」与「遇到的坑」两个小节。
2. 通知 qa-agent 不直接返回固化答案，而是触发 `organize-agent`。
3. organize-agent 携带 execution_trace 和 pitfalls 调用 `get-info-agent`：
   - execution_trace 作为**执行指引**，让 get-info-agent 优先走原路径。
   - pitfalls 作为**避坑提示**，让 get-info-agent 不重蹈覆辙。
4. get-info-agent 按常规流程（搜索 / 抓取 / 清洗 / 分块 / 入库）完成刷新。
5. qa-agent 基于刷新后的证据重新生成答案。
6. organize-agent 覆盖写回 `<skill_id>.md`：
   - `revision` +1。
   - `last_confirmed_at` 更新为当前时间。
   - `source_chunks` 与 `source_urls` 更新为新依赖。
   - 正文答案更新。
   - 必要时在"遇到的坑"新增本轮发现的坑。
   - `user_feedback` 保持不变（刷新不等于用户反馈）。
7. 同步更新 `index.json` 中对应条目的 `last_confirmed_at` 与 `revision`。

**刷新失败降级**：

1. 若 get-info-agent 抓取失败或新内容不足以回答，organize-agent **不覆盖**原 skill，但在 `index.json` 该条目下打一个"本轮刷新失败"的标记（可选字段 `last_refresh_failed_at`）。
2. qa-agent 降级返回旧答案，但必须在回答开头提示："⚠️ 固化答案已超出 TTL 且最近一次刷新失败，内容可能过时。"

### 步骤 4：固化新答案（qa-agent 完成一次满意回答后）

输入：

1. 用户原问题。
2. qa-agent 给出的最终答案 Markdown。
3. 本轮 qa-workflow 的检索与改写记录（L0〜L3 查询、命中的 chunk_id 列表、抓取的 URL）。
4. 本轮 get-info-agent 的执行摘要（若有触发）。

执行（由 organize-agent 驱动，调用本 skill 完成文件写入）：

1. 基于问题主题生成 `skill_id`。
2. 由 LLM 生成 `description`（自然语言触发描述）与 `trigger_keywords`（3〜8 个短词）。
3. 由 LLM 基于主题类型选择 `freshness_ttl_days`：
   - 稳定概念（算法 / 架构 / 设计哲学）→ 180 天。
   - 产品文档（配置 / 命令 / API）→ 90 天。
   - 快速迭代话题（beta 功能 / 预览版）→ 30 天。
4. 收集 `source_chunks`（依赖的 chunk_id 列表）与 `source_urls`。
5. 写 `<skill_id>.md`：
   - frontmatter：全部必填字段，`revision: 1`，`user_feedback: pending`。
   - 正文：答案 Markdown + `## 执行路径` + `## 遇到的坑`。
6. 写 `index.json` 新增一条（幂等：若 `skill_id` 已存在则走步骤 3 的刷新路径）。
7. 原子写：先写临时文件 `.md.tmp` / `.json.tmp`，`fsync` 后 `rename` 到最终名。

### 步骤 5：处理用户反馈

qa-agent 识别到用户反馈后通知 organize-agent：

| 用户信号 | 动作 |
|---|---|
| 用户在下一轮对话未否定固化答案 | `pending` → `confirmed`；`last_confirmed_at` 更新为当前时间；`revision` 不变 |
| 用户明确说"不对 / 不满意 / 这不对 / 过时了"等否定词 | `confirmed`/`pending` → `rejected`；触发重写流程（走步骤 3，但不依赖 execution_trace，视为全新问答） |
| 用户主动补充新信息 | 视为隐式反馈"不完整"：保留原 skill 状态，但在 pitfalls 追加一条"本轮遗漏：<用户补充内容摘要>"，`revision` +1 |

`rejected` 状态的 skill 由 `crystallize-lint` 定期清理（见该 skill 的文档）。

## 5. 命令与约束

### 5.1 读取约束

1. 命中判断必须读 `index.json`，**不允许**跳过索引直接 glob `*.md`（避免启动期扫描开销）。
2. `index.json` 读失败 → 静默降级到"无固化层"，写日志但不阻断 qa-workflow。
3. 命中后读对应 `.md` 文件，frontmatter 与正文都可用。

### 5.2 写入约束

1. 写入必须原子：`.tmp` → `fsync` → `rename`，避免并发读写读到半成品 JSON。
2. `index.json` 写入时整体重写（当前规模下无需增量更新）。
3. 首次写入时若 `data/crystallized/` 不存在，自动 `mkdir -p` 创建。
4. 写入前必须校验 frontmatter 字段齐全，缺字段直接 fail-fast，不写半成品文件。

### 5.3 幂等约束

1. `skill_id` 唯一。相同 `skill_id` 二次写入必须走"更新"语义（`revision` +1、`last_confirmed_at` 刷新），不能简单覆盖。
2. `trigger_keywords` 与 `description` 更新时同步更新 `index.json`。

## 6. 与其他组件的协作

```
qa-agent
  ├─ qa-workflow 步骤 0 → 调用本 skill 做命中判断
  │                        ├─ 命中 + 新鲜 → 返回答案
  │                        ├─ 命中 + 过期 → 委托 organize-agent 刷新
  │                        └─ 未命中 → 返回"走 RAG"
  │
  ├─ qa-workflow 步骤 1〜8 → 原有 RAG 流程
  │
  └─ qa-workflow 步骤 9 → 回答完成后调用 organize-agent 固化
                           organize-agent
                             └─ 调用本 skill 完成写入

crystallize-lint
  └─ 定期清理 rejected / 长期未访问的 skill
```

## 7. 失败策略

遵守 fail-fast 但**不阻断主流程**：

1. `index.json` 损坏（JSON 解析失败）→ 备份到 `index.json.broken-<timestamp>`，初始化空索引并继续。
2. `<skill_id>.md` frontmatter 解析失败 → 在 `index.json` 中该条打 `corrupted: true` 标记，跳过命中，写日志。
3. 写入失败（磁盘满 / 权限不足）→ 明确报错，qa-agent 仍正常返回本轮答案，只是不固化。
4. 刷新路径失败 → 见步骤 3 的"刷新失败降级"。

## 8. 与 qa-workflow 的接口契约

qa-workflow 调用本 skill 时传入：

```json
{
  "mode": "hit_check",
  "user_question": "...",
  "extracted_entities": ["..."]
}
```

返回：

```json
{
  "status": "hit_fresh | hit_stale | miss | degraded",
  "skill_id": "... 或 null",
  "answer_markdown": "... 或 null",
  "execution_trace": "... 或 null（仅 hit_stale 时提供）",
  "pitfalls": "... 或 null（仅 hit_stale 时提供）",
  "revision": "... 或 null"
}
```

qa-workflow 完成回答后调用本 skill 的写入模式：

```json
{
  "mode": "crystallize",
  "user_question": "...",
  "answer_markdown": "...",
  "source_chunks": ["..."],
  "source_urls": ["..."],
  "execution_summary": "..."
}
```

返回：

```json
{
  "status": "created | updated | skipped",
  "skill_id": "...",
  "revision": 1
}
```
