---
name: qa-workflow
description: 当用户提出知识问答、事实确认、流程咨询、术语解释、方案比较，且需要优先利用本地知识库回答时触发。负责 “自进化整理层命中判断 → Query 改写 → 本地证据检索 → 证据充分性判断 → 必要时触发 Get-Info Agent → 基于已验证证据生成答案 → 委托 organize-agent 固化答案” 全流程。
disable-model-invocation: false
---

# QA Workflow

## 0. 强制执行：Todo List

qa-agent 在执行本 workflow 前，**必须先调用 `TodoList` 工具**，按以下步骤生成 todo 列表，然后严格按列表顺序执行。每完成一步立即更新状态为 `completed`，再进入下一步。**禁止跳步**。

典型 todo 模板：
1. 步骤0：自进化整理层命中判断 → pending
2. 步骤1：规范化用户问题 → pending
3. 步骤2：Query 改写（L0〜L3 fan-out） → pending
4. 步骤3：本地证据检索（chunks → raw → Milvus） → pending
5. 步骤4：证据充分性判断 → pending
6. 步骤5：必要时触发 get-info-agent → pending
7. 步骤6：基于已验证证据生成答案 → pending
8. 步骤7：答案格式化与来源标注 → pending
9. 步骤8：委托 organize-agent 固化答案 → pending

**步骤8 不可跳过**：只要满足固化条件（答案完整、有证据、非一次性问题、无敏感信息、非 hit_fresh 直接返回），就必须触发 organize-agent。固化失败不影响已返回的答案。

## 1. 适用场景

在以下场景触发本 skill：

1. 用户询问一个知识点、概念、流程、配置方法、使用方式或对比结论。
2. 用户希望先基于本地知识库回答，而不是直接联网搜索。
3. 用户问题可能已经在 `data/docs/raw/` 或 `data/docs/chunks/` 中存在对应资料。
4. 用户虽然没有明确要求“最新”，但问题本身可能受版本、发布日期、工具变更影响，需要先判断本地资料是否过时。

在以下场景不要直接停留在本 skill：

1. 用户明确要求抓取最新资料、补库、同步外部文档，此时应尽快触发 `get-info-agent`。
2. 用户请求的是站点优先级维护本身，应交给 `update-priority` 相关流程。
3. 用户问题与知识库无关，或仅是闲聊，不需要进入完整 RAG 流程。

## 2. 职责边界

本 skill 的职责是：

1. 把用户问题改写成稳定、可检索、可追踪的查询集合。
2. 优先使用本地文件系统和 Milvus 获取证据。
3. 判断证据是否充分、是否过时、是否存在冲突。
4. 在本地证据不足时，明确触发 `get-info-agent` 获取新资料。
5. 最终只基于证据回答，避免把猜测包装成事实。

本 skill 不负责：

1. 直接执行网页抓取和页面清洗。
2. 直接决定外部网页如何落盘和分块。
3. 在证据缺失时编造答案。

## 3. 输入

输入至少包括：

1. 用户原始问题。
2. 用户问题中的时间约束。
3. 用户问题中的实体名、产品名、版本号、站点名、缩写。
4. 上下文中已知的历史对话线索。

## 4. 输出

输出必须包含以下之一：

1. **直接返回自进化整理层的固化答案**（命中且新鲜时），并在开头标注来源。
2. 基于本地知识回答，并附带来源路径或文档标识。
3. 说明本地证据不足，并触发 `get-info-agent` 后基于新资料回答。
4. 明确说明证据不足，当前无法可靠回答。

## 5. 执行流程

### 步骤0: 自进化整理层命中判断（固化层短路优化）

本步骤是整个 qa-workflow 的**第一个**动作，早于所有 RAG 活动。由 `crystallize-workflow` skill 执行命中判断。

#### 0.1 调用契约

传入 `crystallize-workflow` 的参数：

```json
{
  "mode": "hit_check",
  "user_question": "用户原问题原文",
  "extracted_entities": ["从问题中抽取的核心实体与术语"]
}
```

返回状态与后续动作：

| status | 含义 | 后续动作 |
|---|---|---|
| `hit_fresh` | 命中固化 skill 且未超 TTL | 直接返回 `answer_markdown`，在开头附标注 `> 📦 来自自进化整理层固化答案（skill_id: ..., revision: N, 最后确认 YYYY-MM-DD）`，**结束本次 qa-workflow**，不走后续步骤 |
| `hit_stale` | 命中但已超 TTL | 通过 `Agent` tool 呼叫 `organize-agent` 的 refresh 模式，organize-agent 携带 `execution_trace` + `pitfalls` 调 get-info-agent 补库，补库完成后本 skill 从步骤 1 重跑生成答案，最终回答开头附标注 `> 🔄 固化答案已超 TTL，本轮已自动刷新（skill_id: ..., revision: N）`；若刷新失败，降级返回旧答案并标 `> ⚠️ ...最近一次刷新失败...` |
| `miss` | 固化层无命中 | 继续走步骤 1 以后的完整 RAG 流程 |
| `degraded` | 固化层读取失败或损坏 | 静默进入 `miss` 分支，写日志，**不阻断** qa-workflow |

#### 0.2 硬约束

1. 固化层是**软依赖**：`data/crystallized/` 不存在、`index.json` 损坏、命中判断异常等情况必须静默降级到 `miss`，绝对不得因固化层异常阻断问答。
2. 命中 `hit_fresh` 后直接结束，**不要为了“稳妙”再跑一次 RAG 核验**（那会抵消固化层的性能收益）；企业级校验由周期 `crystallize-lint` 和用户反馈控制。
3. `hit_stale` 分支执行完成后，步骤 9 仍须触发（固化层 `organize-agent` 的 refresh 模式内部已做版本更新，但要确保反馈通道畅通）。
4. 当用户问题明显带时效性信号（“最新”“最近”“当前版本”）且命中的 skill 已超 TTL 的 50% 时，即使形式上 `hit_fresh`，也应视同 `hit_stale` 走刷新路径（由 LLM 判断用户时效性意图较强时）。

### 步骤1: 规范化用户问题

先把用户问题拆成以下结构：

1. 主问题。
2. 关键实体。
3. 限定条件。
4. 预期答案类型。
5. 时效性要求。

示例拆解维度：

1. 这是“是什么”的问题，还是“怎么做”的问题。
2. 用户要的是定义、步骤、对比、最佳实践，还是最新变化。
3. 是否包含版本、日期、平台、工具链约束。

### 步骤2: Query 改写（L0〜L3 fan-out）

改写的目标是**同时变得更精确（贴近正确术语）和更广泛（多角度兜底）**。Agent 必须输出一组 4 条左右的查询变体，按以下分层产出，**每层至多 1〜2 条**：

| 层级 | 名称 | 作用 | 例子（用户问 "claude code subagent 配置"） |
|------|------|------|---|
| L0 | 原句 | 保留用户意图与字面表达 | `claude code subagent 配置` |
| L1 | 术语规范化 | 缩写展开、口语改标准名、中英别名 | `Claude Code subagent configuration` |
| L2 | 意图增强 | 补动作词、产品词、版本/时间词 | `如何创建 Claude Code subagent`、`Claude Code subagent YAML frontmatter` |
| L3 | HyDE 假答 | 让自己虚构一段"理想答案的开头"再当查询用 | `"Claude Code 的 subagent 通过 .claude/agents 下的 YAML 文件定义，必填字段包括 name、description ..."` |

#### 改写硬约束

1. 保留用户原意，不得改变问题目标。
2. **L0 永远要保留**，不要被改写覆盖。
3. L1 最多 2 条；L2 最多 2 条；L3 最多 1 条。总数控制在 4〜6 条之间。
4. 中英混合主题必须至少含 1 条中文 + 1 条英文。
5. 版本敏感问题必须有一条带版本/年份/`latest`/`release`。
6. 对流程型问题补 `workflow`、`steps`、`guide`、`配置`、`示例`、`best practices`。
7. **HyDE 段落不要超过 200 字符**，写成一段单段 Markdown 即可。

#### 禁止事项

1. 不得为了提高召回率引入与用户无关的主题。
2. 不得把猜测的背景强行写进查询。
3. 不得在无任何证据时编造产品名 / 版本号。
4. 不得把超过 200 字符的自然语言段落作为非 HyDE 查询使用。

### 步骤2.5: 调用 multi-query-search 做 fan-out 检索

Agent 把上一步的查询变体直接交给 CLI，由 CLI 完成"对每条查询并发检索 → RRF 合并 → 按 `chunk_id` 去重（合成 question 行会自动折叠回父 chunk）"。

#### 标准调用

```bash
python bin/milvus-cli.py multi-query-search \
  --query "claude code subagent 配置" \
  --query "Claude Code subagent configuration" \
  --query "如何创建 Claude Code subagent" \
  --query "Claude Code 的 subagent 通过 .claude/agents 下的 YAML 文件定义..." \
  --top-k-per-query 20 --final-k 10
```

#### 执行规则

1. 每条 `--query` 对应一个改写层级；保留改写层级的顺序，便于后续解释结果。
2. `--top-k-per-query` 默认 20；除非证据特别稀疏，不要调高到 50 以上，避免噪声压过 RRF 信号。
3. `--final-k` 默认 10；这是返回给 Agent 的"候选证据"，仍需人工核读 chunk 文本。
4. 返回字段里的 `matched_query_indexes` 表示该 chunk 在哪几条变体中被命中，命中越多越值得信任。
5. 返回字段里的 `matched_kinds` 含 `question` 表示是合成 QA 命中（说明 query-doc 词汇差距由 doc2query 索引层补上了），含 `chunk` 表示是正文向量直接命中。两者都计入 RRF。

### 步骤3: 文件系统精确检索

优先检索文件系统，因为这是最直接、最稳定、最可解释的证据层。

检索顺序：

1. 先查 `data/docs/chunks/`，因为这里的分块文件更适合精确定位主题片段。
2. 再查 `data/docs/raw/`，确认完整上下文、原文结构、前后约束。
3. 如果旧结构仍存在 `data/docs/` 根目录历史文件，也应纳入兼容检索。

检索要求：

1. 先用主实体名和规范术语检索标题、YAML metadata、一级标题、二级标题。
2. 再用动作词或问题意图词检索正文。
3. 记录命中的文件、段落、标题路径、匹配词。

### 步骤4: Milvus 多查询召回（multi-query-search）

在以下情况进入 Milvus 检索：

1. Grep 无命中。
2. Grep 命中太少，无法回答。
3. Grep 命中存在多个相似主题，需要更多语义召回。
4. Grep 命中的是完整文档，但目标信息埋在长文中，需要靠向量召回补足。
5. 用户问句模糊（关键词稀少 / 仅有口语描述），文件系统几乎不可能精确命中——这种情况下 multi-query-search 是兜底主力，靠 L2 + L3 + 合成 QA 行把"模糊问"映射到"精确证据"。

Milvus 检索要求：

1. 直接调用 `python bin/milvus-cli.py multi-query-search` 把步骤 2 产出的 4〜6 条查询变体一次性传入，**不要逐条调 dense-search**——后者拿不到跨查询 RRF 合并的好处。
2. 保留每个结果的 `kind`、`doc_id`、`chunk_id`、`title`、`url`、`rrf_score`、`matched_query_indexes`、`matched_kinds`、`summary`。
3. 不得只看 `rrf_score` 不看文本内容；分数只是排序信号，最终证据成立必须靠 chunk 正文。
4. 当 `matched_kinds` 仅含 `question`（也就是只命中合成 QA 行、没命中正文行）时，必须额外用 `dense-search` 或文件系统对该 chunk 做一次正文核验，避免 doc2query 偏差被当成事实。

### 步骤5: 与文件系统结果做最终合并

1. 把 Grep / Glob 命中和 multi-query-search 返回的候选放到同一张候选表。
2. 按 `chunk_id` 再去重一次（multi-query-search 内部已经按 `chunk_id` 去重，跨"文件系统"与"向量库"还需要再合一次）。
3. 优先保留同时被文件系统命中 + 向量库命中的 chunk。
4. 只把排序高的结果当成"候选证据"，仍需人工核读文本。

### 步骤6: 证据充分性判断

必须显式判断证据是否足够回答，而不是“命中了就答”。

判定为“证据充分”的条件：

1. 至少有 1 到 3 条高相关证据直接回答问题。
2. 证据之间不冲突，或冲突可解释。
3. 关键细节在完整上下文中可验证。
4. 如果问题涉及版本或时间，本地资料没有明显过时迹象。

判定为“证据不足”的典型情形：

1. 命中结果只提到相关主题，没有回答问题本身。
2. 结果来自旧版本资料，而用户问题明显是当前版本问题。
3. 结果互相矛盾，无法确定哪条有效。
4. 命中内容只有摘要，没有正文上下文。
5. 用户明确要求“最新”、“今天”、“最近变化”、“官方最新文档”。

### 步骤7: 触发 Get-Info Agent

满足任一条件就应触发 `get-info-agent`：

1. 本地检索无有效命中。
2. 本地检索有命中但证据不足。
3. 本地命中内容明显过时。
4. 用户明确要求补充最新文档、官方资料、站外资料。

触发时应传递尽可能完整的上下文：

1. 用户原问题。
2. 你生成的查询变体。
3. 当前本地检索发现了什么、不足在哪里。
4. 期望补哪些信息。
5. 是否强调时效性。
6. 触发目标必须是 `get-info-agent`，不要由 QA 直接调用 `get-info-workflow` 或持久化类 skill。

### 步骤8: 基于证据回答

回答时必须遵守：

1. 先回答用户真正的问题，不要先铺陈一大段背景。
2. 所有关键结论都要能在证据中找到对应依据。
3. 如果答案部分来自新抓取资料，要明确说明。
4. 如果仍有空白，要明确说“现有证据不足以确认”。
5. 引用文件时优先指向 chunk 文件，再在必要时补 raw 文件。

### 步骤9: 委托 Organize Agent 固化本轮答案

这是 qa-workflow 的**最后一步**，在回答已经给到用户之后异步执行（不阻断本轮响应返回）。

#### 9.1 触发条件

满足以下全部条件时触发：

1. 本轮给出了完整答案（非“证据不足”或“无法可靠回答”）。
2. 答案基于至少 1 条本地证据，或本轮触发了 get-info-agent 抓取了新证据。
3. 问题是可复用的：事实性 / 流程性 / 概念性。
4. 答案不包含敏感信息（凭证、API key、个人身份信息）。
5. 本轮不是“固化层命中 hit_fresh 直接返回”的分支（`hit_fresh` 不需要重复固化）。

#### 9.2 分支类别

| 本轮来源 | Organize Agent 模式 |
|---|---|
| 步骤 0 返回 `miss` + 步骤 1、8 走完纯本地 RAG | `crystallize` |
| 步骤 0 返回 `miss` + 步骤 1、8 中触发了 get-info-agent | `crystallize` |
| 步骤 0 返回 `hit_stale` + 刷新成功 | `refresh`（由 organize-agent 内部在刷新流程中自动完成，本步骤只需重新触发用户反馈录入机制；若固化层内部已经自动写入，本步骤可直接跳过） |
| 用户在本轮对上一轮固化答案表达 confirm / reject / 补充 | `feedback`（与 `crystallize` 互斥：本轮只回复反馈，不站为新问答新固化） |

#### 9.3 触发参数

构造 `crystallize` 模式的 JSON payload（详见 `@agents/organize-agent.md`）：

```json
{
  "mode": "crystallize",
  "user_question": "...",
  "answer_markdown": "...",
  "source_chunks": ["依赖的 chunk_id 列表"],
  "source_urls": ["依赖的原始 URL列表"],
  "execution_summary": {
    "queries": ["L0", "L1", "L2", "L3"],
    "hit_layers": ["filesystem", "multi-query-search"],
    "get_info_triggered": false,
    "get_info_notes": null
  },
  "pitfalls_observed": [
    "本轮观察到的坑与避法，可为空数组"
  ]
}
```

#### 9.4 硬约束

1. 固化失败（磁盘满 / 权限不足 / organize-agent 不可达）时**不得回溯用户**。本轮回答已给到用户，固化是底层优化，失败只需写日志。
2. 触发 organize-agent 后不要等待它返回结果再回应用户——qa-workflow 的主路径在步骤 8 之后已结束，本步骤仅为侧面写入。
3. 不得在 `crystallize` 模式里伪造 `source_chunks`；如果本轮无本地 chunk 依赖（纯走 get-info 后写盘了新 chunk），`source_chunks` 为本轮新写入的 chunk_id。

## 6. 回答格式要求

建议输出结构：

1. 简要答案。
2. 关键依据。
3. 如果触发了 Get-Info，说明新增资料来源。
4. 如果存在限制，明确写出限制。

## 7. 失败策略

遵守 fail-fast：

1. 如果检索命令失败，直接暴露失败原因，不要假装已经检索完成。
2. 如果 Milvus 不可用，明确说明当前只能依赖文件系统证据。
3. 如果本地与新抓取资料冲突，必须显式指出，不要静默取舍。

## 8. 与其他组件的协作

1. `qa-agent` 负责调用本 skill。
2. `crystallize-workflow` 负责固化层的命中判断与新鲜度判断，由本 skill 步骤 0 与步骤 9 调用。
3. `organize-agent` 负责固化层的写入与刷新调度，由本 skill 步骤 9 触发；步骤 0 的 `hit_stale` 分支也需要通过 organize-agent 完成刷新。
4. `get-info-agent` 负责补充外部资料。
5. `update-priority` 由 Get-Info 成功抓取或调度流程触发，不由 QA 直接重写优先级配置。
