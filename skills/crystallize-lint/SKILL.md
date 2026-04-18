---
name: crystallize-lint
description: 当 organize-agent 需要对自进化固化层做周期性健康检查时触发。负责清理被用户 rejected 的 skill、清理长期未访问的 skill、校验 index.json 与 .md 文件的一致性、检测孤儿文件和缺失索引，不负责读写具体固化内容。
disable-model-invocation: false
---

# Crystallize Lint

## 1. 背景

自进化固化层随时间增长会出现以下问题：

1. 被用户标记为 `rejected` 的 skill 长期堆积。
2. 索引 `index.json` 与磁盘 `.md` 文件不一致（孤儿文件、缺失索引）。
3. 长期未访问且已过 3× TTL 的 skill 成为僵尸条目。
4. frontmatter 字段缺失、格式损坏。
5. `source_chunks` 引用的 chunk 文件已被删除（依赖断裂）。

本 skill 负责周期性扫描并修复上述问题，对标 Karpathy LLM Wiki 原文提到的"lint operation"。

## 2. 职责边界

本 skill 负责：

1. 检测并清理 `rejected` 状态的 skill。
2. 检测并清理长期未访问的僵尸 skill（超过 `3 × freshness_ttl_days` 且 `user_feedback != confirmed`）。
3. 检测 `index.json` 与磁盘 `.md` 文件的一致性。
4. 检测 frontmatter 字段缺失或损坏的 skill。
5. 检测 `source_chunks` 引用断裂（依赖的 chunk 已删除）。
6. 输出健康报告（写入 `data/crystallized/lint-report.md`）。

本 skill 不负责：

1. 写入新固化内容（那是 `crystallize-workflow` 的职责）。
2. 刷新过期 skill（那是 `crystallize-workflow` 步骤 3 的职责）。
3. 修复 `data/docs/raw/` 或 `data/docs/chunks/` 的问题。

## 3. 触发时机

1. **手动触发**：用户在 organize-agent 会话中说"清理固化层 / lint 一下固化 skill"。
2. **自动触发**（可选）：每次 organize-agent 写入 10 条新固化 skill 后触发一次增量 lint。
3. **启动触发**（可选）：qa-agent 启动首次读 `index.json` 发现字段异常时，委托 organize-agent 运行本 skill。

## 4. 检查项

### 4.1 清理规则

| 规则 | 动作 |
|---|---|
| `user_feedback == rejected` | 删除 `.md` 文件 + 从 `index.json` 移除条目 |
| `now > last_confirmed_at + 3 × freshness_ttl_days` 且 `user_feedback != confirmed` | 删除 `.md` 文件 + 从 `index.json` 移除条目 |
| `index.json` 中的 `skill_id` 对应 `.md` 不存在 | 从 `index.json` 移除条目 |
| 磁盘上的 `<skill_id>.md` 在 `index.json` 中无对应条目 | 标记为孤儿文件，移入 `data/crystallized/_orphans/` 待人工审阅 |
| frontmatter 解析失败 | 在 `index.json` 中该条目打 `corrupted: true`，下次 lint 时人工修复或归为孤儿 |
| `source_chunks` 列出的 chunk_id 在 `data/docs/chunks/` 中不存在 | 记录到健康报告，`revision` 不变，**不自动删除**（chunk 可能被重命名/迁移，留人工判断） |

### 4.2 一致性校验

1. `index.json.version` 字段存在且为 `"1.0.0"`。
2. 每条 skill 的 `skill_id` 在数组中唯一。
3. 每条 skill 的必填字段（`skill_id` / `description` / `trigger_keywords` / `last_confirmed_at` / `freshness_ttl_days` / `revision` / `user_feedback`）全部存在。
4. `user_feedback` 只能是 `pending` / `confirmed` / `rejected` 三值之一。
5. `freshness_ttl_days` 为正整数。
6. `last_confirmed_at` 为合法 ISO-8601 时间戳。

### 4.3 Markdown 文件校验

1. 存在合法的 YAML frontmatter（被 `---` 包围）。
2. frontmatter 的 `skill_id` 与文件名一致（去 `.md` 后缀）。
3. 正文包含 `## 执行路径` 或 `## 执行路径（execution_trace）` 标题。
4. 正文包含 `## 遇到的坑` 或 `## 遇到的坑（pitfalls）` 标题（允许为空，但小节标题必须存在）。

## 5. 执行流程

### 步骤 1：读取现状

1. 读 `data/crystallized/index.json`。
2. 列 `data/crystallized/*.md`。
3. 如果目录不存在 → 直接返回"无需 lint"。

### 步骤 2：分类统计

把所有条目分成以下类别：

1. `healthy`：全部字段正常、未过 3× TTL、`user_feedback != rejected`。
2. `to_delete_rejected`：`user_feedback == rejected`。
3. `to_delete_stale`：超过 3× TTL 且非 confirmed。
4. `index_missing_md`：索引有但文件不存在。
5. `orphan_md`：文件存在但索引无。
6. `corrupted`：frontmatter 损坏或字段缺失。
7. `dependency_broken`：`source_chunks` 引用断裂。

### 步骤 3：执行清理

按类别处理：

1. 对 `to_delete_rejected` 与 `to_delete_stale`：
   - 删除 `<skill_id>.md`。
   - 从 `index.json.skills` 移除条目。
2. 对 `index_missing_md`：
   - 从 `index.json.skills` 移除条目。
3. 对 `orphan_md`：
   - `mkdir -p data/crystallized/_orphans`。
   - `rename <skill_id>.md _orphans/<skill_id>-<timestamp>.md`。
4. 对 `corrupted`：
   - 在 `index.json` 中该条目打 `corrupted: true`（若条目不存在则补一个占位条目）。
   - 不自动删除，等待人工处理。
5. 对 `dependency_broken`：
   - 仅记录到健康报告，不修改内容。

### 步骤 4：更新 `index.json`

1. 刷新 `updated_at` 为当前时间。
2. 原子写入（`.tmp` → `fsync` → `rename`）。

### 步骤 5：生成健康报告

写入 `data/crystallized/lint-report.md`：

```markdown
# Crystallize Lint Report

- 检查时间：2026-04-18T23:40:00+08:00
- 总 skill 数：12（lint 前）→ 10（lint 后）

## 清理

- 删除 rejected：1 条
  - `old-subagent-design-2026-02-01`
- 删除 stale：1 条
  - `anthropic-beta-feature-2025-12-15`（最后确认 2025-12-15，TTL 30 天 × 3 = 90 天已过）

## 孤儿文件

- `data/crystallized/_orphans/unknown-topic-2026-03-01-20260418234000.md`

## 依赖断裂（需人工审阅）

- `claude-code-subagent-design-2026-04-18` 依赖 chunk `claude-code-subagent-2026-03-01-002`，已不存在

## 损坏文件

- 无
```

健康报告**覆盖写入**（不追加），每次 lint 反映最新状态。

## 6. 命令与约束

### 6.1 删除约束

1. 删除 `.md` 文件前必须先从 `index.json` 移除条目（避免竞态读取到已删文件）。
2. 所有删除不进回收站，直接物理删除（除 orphan 走归档目录）。
3. lint 过程中若任何一步失败，**已完成的改动不回滚**（部分清理优于完全失败），但健康报告必须标记"lint interrupted"。

### 6.2 原子性约束

1. `index.json` 整体重写（当前规模下无需增量）。
2. 删除 `.md` 与更新 `index.json` 必须在同一次 lint 中完成，不允许中途退出留下不一致状态。

### 6.3 日志

1. 每次 lint 的摘要（清理条数、发现问题数）必须输出到健康报告。
2. 详细操作日志（每个文件的增删）写到控制台给用户看，不持久化。

## 7. 失败策略

1. 目录不存在 → 返回"无需 lint"，不报错。
2. `index.json` 损坏 → 备份到 `index.json.broken-<timestamp>`，重建空索引，所有 `.md` 文件标记为孤儿。
3. 某个 `.md` 文件读失败 → 跳过该文件，记录到健康报告，继续处理其他。
4. 磁盘满 / 权限不足 → 明确报错并中断 lint，已完成的改动保留。

## 8. 与其他组件的协作

```
organize-agent
  ├─ 周期性或按需触发本 skill
  └─ 读健康报告决定是否需要人工介入

crystallize-workflow
  └─ 在读 index.json 时遇到异常字段 → 委托 organize-agent 运行本 skill
```

本 skill **不直接**被 qa-agent 调用。qa-agent 只负责问答主路径，维护工作全部交给 organize-agent。
