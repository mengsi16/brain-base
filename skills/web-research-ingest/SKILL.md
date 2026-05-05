---
name: web-research-ingest
description: 当 get-info-agent 需要从外部网页补充知识时触发。负责基于查询计划调度 playwright-cli-ops skill 完成检索和候选页筛选，输出 URL 列表 + source_type 分类。不再负责抓取页面内容——内容抓取和清洗由 content-cleaner-agent 并行完成。
disable-model-invocation: false
---

# Web Research Ingest

## 1. 职责

本 skill 负责：

1. 根据主题与查询计划选择搜索策略。
2. 调用 `playwright-cli-ops` 搜索候选页面。
3. 筛选候选页，按 `official-doc` / `community` / `discard` 分类。
4. 输出 URL 列表 + 分类结果，供 qa-agent（通过 Agent tool）并行调度 content-cleaner-agent。

**本 skill 不负责**：抓取页面正文内容、清洗 Markdown、写 raw 文件——这些全部由 `content-cleaner-agent` 负责。

## 2. 输入

1. 用户原问题。
2. 查询改写结果。
3. 候选站点优先级。
4. 是否要求最新资料。

## 3. 执行步骤

### 步骤1: 生成检索计划

1. 先确定主查询。
2. 再生成 2 到 5 个检索变体。
3. 再按 `priority.json` 选择优先站点。
4. **按时间段分批检索**：优先获取近期内容，逐步扩大时间窗口。
5. **歧义消解**：如果主题存在常见歧义，为查询补上产品名、版本词、文件名、命令名等消歧关键词。
6. **分隔符变体覆盖**：如果主题名可能包含连字符/下划线/空格变体（如 `oneapi` vs `one-api` vs `one api`），必须生成所有变体作为独立查询，避免因分隔符差异漏掉正确结果。

#### 1.1 Google 时间段高级搜索语法

Google 搜索支持以下时间限定操作符：

| 操作符 | 含义 | 示例 |
|--------|------|------|
| `after:YYYY-MM-DD` | 限定发布日期在该日期之后 | `after:2026-01-26` |
| `before:YYYY-MM-DD` | 限定发布日期在该日期之前 | `before:2026-04-26` |

组合使用：`claude code subagent after:2026-03-26 before:2026-04-26`

#### 1.2 时间窗口优先级

搜索时按以下顺序分批执行，**近期优先**：

| 批次 | 时间窗口 | 说明 |
|------|----------|------|
| **第 1 批** | 近 1 个月 | `after:<今天-30天>` |
| **第 2 批** | 2~4 个月前 | `after:<今天-120天> before:<今天-30天>` |
| **第 3 批** | 5~8 个月前 | `after:<今天-240天> before:<今天-120天>` |
| **第 4 批** | 1 年内 | `after:<今天-365天> before:<今天-240天>` |
| **第 5 批** | 1~2 年前 | `after:<今天-730天> before:<今天-365天>` |

**执行规则**：

1. **先跑第 1 批**。如果第 1 批已经找到足够的高质量结果（官方文档 ≥ 2 篇，或总有效结果 ≥ 5 篇），**不再跑后续批次**。
2. 如果第 1 批结果不足，继续跑第 2 批，以此类推。
3. **不要求每次都跑满 5 批**——找到足够结果就停。
4. 用户明确要求"最新资料"时，**只跑第 1 批**，不扩大窗口。
5. 用户明确要求"历史资料"或"某个版本"时，跳过第 1 批，直接从对应时间窗口开始。
6. 每批搜索结果中，**优先抓取官方文档**，其次抓取社区内容，最后才考虑 discard。

#### 1.3 时间窗口在查询中的嵌入方式

在调用 `playwright-cli-ops` 搜索时，将时间操作符追加到查询字符串末尾：

```
原始查询: "claude code subagent configuration"
第1批查询: "claude code subagent configuration after:2026-03-26"
第2批查询: "claude code subagent configuration after:2025-12-26 before:2026-03-26"
```

如果搜索引擎不支持 `after:/before:` 操作符，退化为不带时间限定的搜索，但在文档草稿的 `fetched_at` 字段中仍记录抓取日期，后续由信源仲裁机制按时间排序。

### 步骤2: 调用 playwright-cli-ops 搜索（硬约束：必须实际搜索）

**禁止用训练数据编造 URL**：本步骤的每一个候选 URL 都必须来自 Playwright 对搜索引擎的实际调用结果，**严禁**用 LLM 训练知识直接"猜" URL。如果 Playwright 不可用，本步骤必须失败并返回 `infra_status: degraded`，不得降级为训练数据补位。

只执行搜索和筛选，**不打开候选页面、不导出页面正文**：

1. 通过 `playwright-cli` 打开搜索引擎（Google / Bing）执行搜索。
2. 从搜索结果页中提取候选 URL 列表（标题 + URL + 摘要片段）。
3. 筛掉明显低质量的条目（404、聚合榜单、广告落地页）。
4. **URL 存在性验证**：对每个候选 URL，用 `playwright-cli` 快速 HEAD 请求或打开页面确认其存在（HTTP 200）。不存在的 URL（如 `songxcn/OneAPI`）直接丢弃，不得进入候选列表。
5. **查询变体覆盖**：如果主题名包含常见分隔符变体（如 `oneapi` / `one-api` / `one_api` / `one api`），必须对每个变体分别搜索，取搜索结果最多的变体作为主结果。

### 步骤3: URL 分类

对步骤2返回的每个候选 URL 执行分类：

**第1级：白名单快速路径**（命中 `priority.json.official_domains` → `official-doc`）

**第2级：LLM 综合判断**（未命中白名单时）
- `official-high` / `official-low` → `official-doc`
- `community` → `community`
- `discard` → 丢弃，不进入输出列表

### 步骤4: 输出 URL 候选列表

输出结构化列表，供 get-info-agent 并行调度 content-cleaner-agent：

```json
{
  "candidates": [
    {
      "url": "https://github.com/openclaw/openclaw/blob/main/README.md",
      "source_type": "official-doc",
      "title_hint": "OpenClaw README"
    },
    {
      "url": "https://blog.example.com/openclaw-tips",
      "source_type": "community",
      "title_hint": "OpenClaw Tips"
    }
  ],
  "discarded": 3
}
```

## 4. 边界

1. 本 skill 只负责搜索 + URL 分类，输出候选列表。
2. 页面内容抓取、清洗、落盘、入库全部由 `content-cleaner-agent` 完成。
