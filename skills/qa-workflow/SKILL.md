---
name: qa-workflow
description: 当用户提出知识问答、事实确认、流程咨询、术语解释、方案比较，且需要优先利用本地知识库回答时触发。负责 “自进化整理层命中判断 → Query 改写 → 本地证据检索 → 证据充分性判断 → 必要时触发 Get-Info Agent → 基于已验证证据生成答案 → 委托 organize-agent 固化答案” 全流程。
disable-model-invocation: false
---

# QA Workflow

## 0. 强制执行：Todo List

qa-agent 在执行本 workflow 前，**必须先调用 `TodoList` 工具**，按以下步骤生成 todo 列表，然后严格按列表顺序执行。每完成一步立即更新状态为 `completed`，再进入下一步。**禁止跳步**。

典型 todo 模板：
1. 步骤−1：基础设施快速探测（Milvus / Playwright 可用性） → pending
2. 步骤0：自进化整理层命中判断 → pending
3. 步骤1：规范化用户问题 → pending
4. 步骤1.5：复杂问题分解（简单问题跳过） → pending
5. 步骤2：Query 改写（L0〜L3 fan-out） → pending
6. 步骤3：本地证据检索（chunks → raw → Milvus，Milvus 不可用时自动跳过） → pending
7. 步骤4：证据充分性判断 → pending
8. 步骤5：必要时触发 get-info-agent（get-info 不可用时进入降级分支） → pending
9. 步骤6：基于已验证证据生成答案（或降级回答） → pending
10. 步骤7：答案格式化与来源标注 → pending
11. 步骤8：委托 organize-agent 固化答案（降级模式下跳过） → pending
12. 步骤9：Recall trace 输出 + 自愈触发判断 → pending

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

### 步骤−1: 基础设施快速探测（非阻断）

本步骤在所有业务动作之前运行，目的是为后续步骤提供一个 **基础设施状态快照**，避免后面在多处重复探测、或者被缺失组件阻断。

#### −1.1 探测动作（快速、宽容）

执行以下检查，**不以失败中断流程**：

1. `python bin/milvus-cli.py check-runtime --require-local-model --smoke-test` －— 探测 Milvus + bge-m3。异常或非零退出 → 标记 `milvus_available = false`。
2. `playwright-cli --help` 或等价命令 — 探测 Playwright-cli。命令不存在或非零退出 → 标记 `playwright_available = false`。
3. 检查 `data/crystallized/` 目录和 `index.json` 是否可读 — 失败 → 标记 `crystallized_available = false`（不阻断步骤 0 进入 degraded 分支）。

保存成结构化的 `infra_status`：

```json
{
  "milvus_available": true,
  "playwright_available": false,
  "crystallized_available": true,
  "get_info_reachable": "unknown",
  "probed_at": "2026-04-23T14:22:00Z"
}
```

`get_info_reachable` 字段的值依赖 `playwright_available`；也可在步骤 7 真正触发 get-info-agent 时再次评估。

#### −1.2 降级预览

根据 `infra_status` 有以下分支预处理：

| 场景 | 后续影响 |
|------|---------|
| 三项均可用 | 完整走步骤 0〜9，get-info 可触发 |
| `milvus_available=false` | 步骤 4 跳过 Milvus、仅依靠文件系统 Grep；步骤 6 的充分性门卡相应放宽 |
| `playwright_available=false` | 步骤 7 不得触发 get-info-agent，直接进入步骤 8.2 的**降级回答**分支 |
| `crystallized_available=false` | 步骤 0 静默返回 `degraded`，步骤 9 跳过固化委托 |
| 三项都不可用 | 步骤 3〜4 仅文件系统，步骤 8.2 强制降级回答，步骤 9 跳过固化 |

#### −1.3 硬约束

1. 本步骤的目的是 **收集状态**，不是 **判定失败**。任何探测返回状态不得肽断用户问答。
2. 探测耗时必须控制在 **≤ 10 秒**；单个命令超时直接标为不可用。
3. `infra_status` 在后续步骤中作为上下文延续使用，不要在每个步骤重复探测。

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

返回状态与后续动作（hit_check 模式两阶段：先 hot 后 cold，详见 crystallize-workflow §4.1）：

| status | 含义 | 后续动作 |
|---|---|---|
| `hit_fresh` | hot 层命中且未超 TTL | 直接返回 `answer_markdown`，在开头附标注 `> 📦 来自自进化整理层固化答案（skill_id: ..., revision: N, 最后确认 YYYY-MM-DD）`，**结束本次 qa-workflow**，不走后续步骤 |
| `hit_stale` | hot 层命中但已超 TTL | 通过 `Agent` tool 呼叫 `organize-agent` 的 refresh 模式，organize-agent 携带 `execution_trace` + `pitfalls` 调 get-info-agent 补库，补库完成后本 skill 从步骤 1 重跑生成答案，最终回答开头附标注 `> 🔄 固化答案已超 TTL，本轮已自动刷新（skill_id: ..., revision: N）`；若刷新失败，降级返回旧答案并标 `> ⚠️ ...最近一次刷新失败...` |
| `cold_promoted` | cold 层命中且刚达到晋升阈值，已自动 promote 到 hot | 视同 `hit_fresh`，但标注改为 `> ⬆️ 来自自进化整理层固化答案（skill_id: ..., 本轮由 cold 层晋升, hit_count 达到阈值）` |
| `cold_observed` | cold 层命中但未达晋升阈值，hit_count +1 已记录 | **不直接返回**；把 `cold_evidence_summary` 作为辅助证据携带进入步骤 1，走完整 RAG 流程。最终回答时把冷藏摘要作为一条候选证据纳入证据表（按 `community` 类型处理） |
| `miss` | hot 和 cold 层都无命中 | 继续走步骤 1 以后的完整 RAG 流程 |
| `degraded` | 固化层读取失败或损坏 | 静默进入 `miss` 分支，写日志，**不阻断** qa-workflow |

#### 0.2 硬约束

1. 固化层是**软依赖**：`data/crystallized/` 不存在、`index.json` 损坏、命中判断异常等情况必须静默降级到 `miss`，绝对不得因固化层异常阻断问答。
2. 命中 `hit_fresh` 或 `cold_promoted` 后直接结束，**不要为了"稳妥"再跑一次 RAG 核验**（那会抵消固化层的性能收益）；企业级校验由周期 `crystallize-lint` 和用户反馈控制。
3. `hit_stale` 分支执行完成后，步骤 9 仍须触发（固化层 `organize-agent` 的 refresh 模式内部已做版本更新，但要确保反馈通道畅通）。
4. 当用户问题明显带时效性信号（"最新""最近""当前版本"）且命中的 skill 已超 TTL 的 50% 时，即使形式上 `hit_fresh`，也应视同 `hit_stale` 走刷新路径（由 LLM 判断用户时效性意图较强时）。

#### 0.3 冷藏观察（cold_observed）使用规则

进入此分支时：

1. `crystallize-workflow` 已经把 `hit_count += 1` 和 `last_hit_at` 写入 `index.json`，**不需要本 workflow 再写**。
2. qa-workflow 继续走完整 RAG（步骤 1〜8），但**必须**把 `cold_evidence_summary` 作为一条额外候选证据纳入步骤 5 的合并表，**按 `source_type: community` 处理**（因为冷藏条目本身就是 LLM 综合过的次级证据，不是原始文档）。
3. 证据表里显示为：`| 冷藏层摘要 | 🟡 提炼（cold） | crystallized:<skill_id> | YYYY-MM-DD | N 天 |`，让用户看到这条证据的冷藏身份。
4. 本 workflow 的最终回答如果确实用到了冷藏摘要，在步骤 8.1.2 的整体 `可信度` 之后、`⚠️ 时效性提示` 之前额外加一行：`> ❄️ 本答案参考了冷藏固化条目 <skill_id>（hit_count: N），继续反复命中将自动晋升到活跃层。`

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

### 步骤1.5: 复杂问题分解

当用户问题属于以下**任一**类型时，必须将其分解为 2〜4 个独立子问题：

| 问题类型 | 识别特征 | 分解示例 |
|---------|---------|----------|
| **多部问题** | 一个问题包含 ≥2 个独立子意图，用"和""同时""另外"等连接 | "A 怎么配置，B 怎么部署" → ["A 怎么配置", "B 怎么部署"] |
| **对比问题** | 要求比较 A 与 B 的差异/优劣/适用场景 | "A 和 B 有什么区别" → ["A 是什么/核心特性", "B 是什么/核心特性", "A 与 B 的关键差异"] |
| **因果链问题** | 需要多步推理，前一步的答案是后一步的前提 | "为什么 X 导致 Y" → ["X 的机制是什么", "Y 的触发条件是什么", "X 如何触发 Y"] |
| **方案选型** | 要求在多种方案中推荐，需分别了解各方案 | "选 A 还是 B" → ["A 的优缺点", "B 的优缺点", "A 与 B 的选型建议"] |

#### 分解规则

1. **简单问题不分解**：单一事实性问题（"X 是什么"/"如何做 Y"）直接走步骤 2，标记 `decomposition_needed: false`。
2. **子问题必须独立可答**：每个子问题都能独立检索到证据并得到答案，不依赖其他子问题的结果。
3. **子问题数量上限 4**：超过 4 个子问题时，合并相关子问题，保留最核心的 2〜4 个。
4. **保留原始问题**：分解后仍保留用户原问题作为 `parent_question`，最终答案必须整体回答原问题。
5. **子问题走独立检索**：每个子问题各自走步骤 2（Query 改写）→ 步骤 3/4（检索），收集各自的证据集。
6. **证据合并后再回答**：所有子问题的证据合并到同一张候选表，然后统一走步骤 5（充分性判断）→ 步骤 6（生成答案）。
7. **禁止中途询问用户**：分解结果由 LLM 自行判断，不暂停等用户确认（CLAUDE.md 规则 24）。

#### 分解输出格式

```json
{
  "decomposition_needed": true,
  "parent_question": "用户原问题",
  "sub_questions": [
    {"id": "sq-1", "text": "子问题1", "type": "sub-fact"},
    {"id": "sq-2", "text": "子问题2", "type": "sub-fact"},
    {"id": "sq-3", "text": "综合对比/推理", "type": "synthesis"}
  ]
}
```

`type` 为 `sub-fact` 的子问题需要独立检索证据；`type` 为 `synthesis` 的子问题不需要独立检索，其答案由其他子问题的证据综合得出。

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
  --top-k-per-query 20 --final-k 10 --rerank
```

`--rerank` 启用 cross-encoder（bge-reranker-v2-m3）重排序：RRF 合并后，对 `final_k` 条候选用 reranker 对 (query, summary) 打分，按 `rerank_score` 降序重排。**软依赖**：模型不可用时静默回退到纯 RRF 排序，不报错。推荐始终加 `--rerank`。

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

**前置前提**：`infra_status.milvus_available == true`。若 `false`，本步骤整体跳过，在证据汇总里标记 `milvus_skipped: true`，由步骤 6 的充分性门卡和步骤 7 的降级决策考虑后续动作。

在以下情况进入 Milvus 检索：

1. Grep 无命中。
2. Grep 命中太少，无法回答。
3. Grep 命中存在多个相似主题，需要更多语义召回。
4. Grep 命中的是完整文档，但目标信息埋在长文中，需要靠向量召回补足。
5. 用户问句模糊（关键词稀少 / 仅有口语描述），文件系统几乎不可能精确命中——这种情况下 multi-query-search 是兵底主力，靠 L2 + L3 + 合成 QA 行把“模糊问”映射到“精确证据”。

Milvus 检索要求：

1. 直接调用 `python bin/milvus-cli.py multi-query-search` 把步骤 2 产出的 4〜6 条查询变体一次性传入，**不要逐条调 dense-search**——后者拿不到跨查询 RRF 合并的好处。
2. 保留每个结果的 `kind`、`doc_id`、`chunk_id`、`title`、`url`、`rrf_score`、`matched_query_indexes`、`matched_kinds`、`summary`。
3. 不得只看 `rrf_score` 不看文本内容；分数只是排序信号，最终证据成立必须靠 chunk 正文。
4. 当 `matched_kinds` 仅含 `question`（也就是只命中合成 QA 行、没命中正文行）时，必须额外用 `dense-search` 或文件系统对该 chunk 做一次正文核验，避免 doc2query 偏差被当成事实。
5. **控制耗时与稳定性**：如果执行期间 `milvus-cli.py` 非零退出（例如 Milvus 服务在探测后断线），动态更新 `infra_status.milvus_available = false` 並跳出本步骤；不得令整个 qa-workflow 崩溃。

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

### 步骤7: 触发 Get-Info Agent（含降级分支）

#### 7.1 正常触发条件

满足以下 **所有条件** 时才真正触发 `get-info-agent`：

1. 符合业务需要（以下任一）：
   - 本地检索无有效命中。
   - 本地检索有命中但证据不足。
   - 本地命中内容明显过时。
   - 用户明确要求补充最新文档、官方资料、站外资料。
2. 基础设施可用：`infra_status.playwright_available == true`。

触发时应传递尽可能完整的上下文：

1. 用户原问题。
2. 你生成的查询变体。
3. 当前本地检索发现了什么、不足在哪里。
4. 期望补哪些信息。
5. 是否强调时效性。
6. 触发目标必须是 `get-info-agent`，不要由 QA 直接调用 `get-info-workflow` 或持久化类 skill。

**传给 get-info-agent 的 prompt 必须包含以下硬约束：**

```
返回要求：只返回 JSON 候选列表，格式如下，禁止包含任何其他文字：
{
  "candidates": [{"url": "...", "source_type": "official-doc|community", "title_hint": "..."}],
  "discarded": N,
  "infra_status": {"playwright_available": true|false}
}
```

**收到回复后验证**：如果回复不是以上 JSON 格式（而是一段自然语言/富文本内容），说明 get-info-agent 超越了边界。必须忽略其返回内容，从中尝试提取 URL，进入步骤5.5。

#### 7.2 降级分支（核心）

以下任一成立就进入降级分支，跳过 get-info-agent 触发，直接进入步骤 8 中的 **降级回答模式**（8.2）：

1. `infra_status.playwright_available == false`。
2. get-info-agent 返回 `infra_status: { status: "degraded", ... }`。
3. get-info-agent 调用抛异常或超时（建议设 2 分钟硬上限）。

**进入降级分支时**：

1. 在内部证据上下文记录 `degraded_reason`（如 `"playwright unavailable"` / `"get-info timeout"`）。
2. 不触发 get-info-agent，立即进入步骤 8。
3. 用户那一侧仅看到步骤 8.2 输出的降级答案，不会看到 "触发失败" 的错误。

#### 7.3 硬约束

1. 绝不允许因基础设施问题直接向用户返回错误终结会话。基础设施不可用 → **降级回答**；绝不中断。
2. 降级是 **最后手段**。能成功触发 get-info-agent 的问题不得逐降级。
3. 降级决策要在本步骤中明确产出，并在步骤 8 的答案中显示标注，不得隐藏。

#### 7.4 并行调度 content-cleaner-agent（get-info-agent 返回后必须执行）

`get-info-agent` 完成后只返回 **URL 候选列表**（含 `source_type` 和 `title_hint`）。如果它返回的是详细文模内容，则不得用其直接回答——那是未入库的临时数据，必须走完整入库流程。qa-agent 接收到列表后，必须立即用 `Agent` tool 并行启动 `content-cleaner-agent`，完成实际的抓取+清洗+入库：

1. 过滤掉 `discard` 类型。
2. 对每个 `official-doc` 或 `community` URL，**通过 `Agent` tool 独立启动一个 `content-cleaner-agent` 实例**，传入：
   - `url`：该 URL
   - `source_type`：该 URL 的分类
   - `topic`：当前主题关键词
   - `title_hint`：标题提示
3. 并行等待所有实例完成（最多5个同时运行，超出按批次串行）。
4. 收集各实例返回的摘要 JSON，汇总 `chunk_rows` 与 `question_rows`。

**为什么必须在 qa-agent 层调度**：Claude Code 不支持嵌套 Agent 调用（`qa-agent → get-info-agent → content-cleaner-agent` 三层不可行）。get-info-agent 是深度1调用，content-cleaner-agent 也是深度1调用，两者都由 qa-agent 直接发起。

**get-info-agent 返回 URL 列表但不写文件** 是强约束——如果 get-info-agent 已经开始写文件，说明它的指令没有被遵守，应回退后重新触发。

### 步骤8: 基于证据回答（正常 / 降级两种模式）

#### 8.1 正常模式（有合格本地证据或成功补库）

回答时必须遵守的**基本规则**：

1. 先回答用户真正的问题，不要先铺陈一大段背景。
2. 所有关键结论都要能在证据中找到对应依据。
3. 如果答案部分来自新抓取资料，要明确说明。
4. 如果仍有空白，要明确说"现有证据不足以确认"。
5. 引用文件时优先指向 chunk 文件，再在必要时补 raw 文件。

#### 8.1.1 可信度三档分级（必填）

每篇证据按 `source_type` 和"证据年龄"（从 `fetched_at` frontmatter 字段或 `doc_id` 末尾的 `-YYYY-MM-DD` 提取）打一个档位：

| 档位 | 条件 | Emoji |
|------|------|-------|
| **Tier-1 高可信** | `source_type == official-doc` **且** 证据年龄 ≤ 90 天 | 🟢 |
| **Tier-2 中可信** | `source_type == community`（有 `> 来源:` 溯源标注）**且** ≤ 180 天；或 `official-doc` 90〜180 天 | 🟡 |
| **Tier-3 低可信** | `source_type == user-upload`（用户自己上传）；或任何 > 180 天；或 `source_type == unknown` | 🟠 |

整篇答案的可信度**取最低档**，不得取平均或最高档（证据链的可信度由最弱一环决定）。

#### 8.1.2 强制回答模板

```markdown
<答案正文：先结论后展开，严格基于证据>

---

### 📚 来源与时效

| 证据 | 类型 | 来源 | 日期 | 年龄 |
|------|------|------|------|------|
| `<chunk 文件路径>` | 🟢/🟡/🟠 <中文类型> | `<source 字段>` | YYYY-MM-DD | N 天 |
| ... | ... | ... | ... | ... |

**可信度**：🟢/🟡/🟠 <整篇档位，取最低> — <一句话说明，例："基于官方文档，最新证据 12 天内"；"基于社区提炼资料，最早证据 173 天前"；"用户本地上传，可信度由您自行判断"> 

<可选：若证据年龄 > 90 天，加以下一行>
**⚠️ 时效性提示**：本答案最早证据距今 <N> 天，若关注最新版本请刷新。

<可选：若需要刷新或补证据，加以下一行>
💡 **获取更新证据**：请我"补抓 <主题> 最新官方文档"，或运行 `python bin/milvus-cli.py stats` 查看当前库状态。
```

#### 8.1.3 硬约束

1. **证据表必填**：即使只有 1 条证据也必须出现证据表。不得省略。
2. **可信度档位不得虚高**：缺字段（`source_type=unknown` / 无 `fetched_at` 且 `doc_id` 无日期）一律按 Tier-3 处理，不得默认为 Tier-1。
3. **年龄计算基准**：优先用 chunk frontmatter 的 `fetched_at`（ISO 日期）；缺失时退化到 `doc_id` 末尾的 `-YYYY-MM-DD`；再缺失时标"未知"并按 Tier-3 处理。
4. **> 90 天必须告警**：任何证据年龄超过 90 天就必须出现 `⚠️ 时效性提示`，不得忽略。
5. **> 180 天建议刷新**：任何证据年龄超过 180 天时，`💡 获取更新证据` 必须出现，并明确推荐调用 get-info-agent 补抓（如果 Playwright 可用）。
6. **user-upload 不自动降档**：`source_type == user-upload` 恒定为 Tier-3——但**不是**因为质量差，而是因为可信度取决于用户自己的资料质量，系统无法仲裁。说明时要礼貌表述（"可信度由您自行判断"），不要暗示这是差证据。

#### 8.2 降级模式（基础设施不可用且本地证据不足）

触发条件（任一成立）：

1. 步骤 7 已进入降级分支（`degraded_reason` 非空），且本地证据不足以独立回答。
2. Milvus 不可用且 Grep 结果稀疏，get-info 也無法触发。

**降级答案格式**（必须使用）：

```markdown
> ⚠️ **降级回答** ｜ 缺失基础设施：<Milvus / Playwright / 两者>  
> 本答案主要基于 Claude 训练数据<可选补充：+ N 条本地文件系统证据>，未经过本地知识库或最新网页证据核验。

<答案正文>

---

💡 **恢复建议**：<针对 infra_status 给出具体恢复命令，例如启动 Milvus / 安装 Playwright>。恢复后重新提问以获得可核验的答案。
```

**降级模式硬约束**：

1. 降级模式下，答案不得给出以下内容（训练数据可能已过时或不精确）：
   - 具体版本号 / 发布日期 / API 参数默认值
   - 完整的且声称可直接运行的代码片段（基于未核验的 API）
   - 官方文档的具体 URL（Claude 容易编造过期或错误的文档链接）
2. 擅长回答的是概念性、原理性、方法论问题；不擅长的是实时事实和具体配置。对后者应明确说“降级模式无法给出可靠结果，请恢复基础设施后重新提问”。
3. 如果有少量本地证据，上面的 `+ N 条本地文件系统证据` 字段应填真实数量，并在答案正文中显示引用该证据（阻止 LLM 忽略真实命中只用训练数据）。

### 步骤8.5: 答案质量自检（Maker-Checker 循环）

在步骤 8 生成答案后、步骤 9 输出 recall trace 前，对已生成的答案做结构化自检。这是 Agentic RAG 的 **Maker-Checker** 闭环：生成（Maker）后必须评估（Checker），不合格则修正。

#### 8.5.1 评估维度

| 维度 | 含义 | 评估方法 | 不合格阈值 |
|------|------|---------|-----------|
| **忠实度（faithfulness）** | 答案中的每个关键断言是否都能在证据中找到对应依据 | 逐条检查答案中的关键断言，对照证据文本确认 | ≥1 条断言无证据支撑 → 不合格 |
| **完整性（completeness）** | 答案是否覆盖了用户问题的所有要点 | 对照步骤 1 的规范化问题结构（主问题 + 限定条件），检查是否有遗漏 | 遗漏 ≥1 个核心要点 → 不合格 |
| **一致性（consistency）** | 答案内部是否自洽，无自相矛盾 | 检查答案前后文是否有逻辑冲突 | 存在自相矛盾 → 不合格 |

#### 8.5.2 自检输出格式

```json
{
  "faithfulness": "pass | fail",
  "faithfulness_issues": ["断言X无证据支撑"],
  "completeness": "pass | fail",
  "completeness_gaps": ["未覆盖限定条件：版本约束"],
  "consistency": "pass | fail",
  "consistency_conflicts": ["前文说X，后文说非X"],
  "overall": "pass | fail",
  "retry_needed": false
}
```

#### 8.5.3 不合格时的动作

1. **忠实度不合格**：删除无证据支撑的断言，替换为"现有证据不足以确认"；不得编造新证据。
2. **完整性不合格**：在答案末尾明确标注"以下要点现有证据不足以回答：<遗漏要点>"；不得用训练数据补全。
3. **一致性不合格**：保留时间更近/来源更可信的断言，删除矛盾断言并标注"存在信源冲突"。
4. **修正后重检一次**：修正后重新评估三个维度，仍不合格则接受当前答案并在回答中标注 `> ⚠️ 答案自检未通过：<不合格维度>`。
5. **最多修正一轮**：不允许无限循环修正。一轮修正后仍不合格，标注后继续。

#### 8.5.4 硬约束

1. **自检不阻断流程**：即使自检全部 fail，答案仍须返回给用户（带标注），不得因自检失败拒绝回答。
2. **降级模式跳过自检**：步骤 8 走了降级分支（8.2）时，跳过本步骤——降级答案本身已标注不可靠，自检无意义。
3. **自检不引入新证据**：修正答案时只能删除或标注，不能凭空添加证据中不存在的内容。
4. **自检结果写入 recall trace**：步骤 9 的 recall trace 中增加 `answer_eval` 字段，记录自检结果，供后续分析。

### 步骤9: Recall Trace 输出与自愈触发

本步骤在步骤 8（回答）之后执行，负责输出本轮的 recall trace 并判断是否需要触发后台自愈。

#### 9.1 Recall Trace 格式

在内部上下文中记录以下结构化信息（不输出给用户）：

```json
{
  "question": "用户原问题",
  "returned_chunk_ids": ["chunk-1", "chunk-2"],
  "returned_doc_ids": ["doc-1"],
  "retrieval_scores": [0.03, 0.01],
  "answer_summary": "回答摘要（50字以内）",
  "answer_eval": {
    "faithfulness": "pass | fail",
    "completeness": "pass | fail",
    "consistency": "pass | fail",
    "overall": "pass | fail"
  },
  "session_id": "当前会话标识",
  "timestamp": "2026-04-26T12:00:00Z"
}
```

其中 `retrieval_scores` 来自步骤 4 的 multi-query-search 返回结果。如果步骤 4 被跳过（Milvus 不可用），`retrieval_scores` 为空数组。

#### 9.2 自愈触发条件

满足以下**任一**条件时，触发后台自愈进程：

1. **低分命中**：`retrieval_scores` 非空且最高分 ≤ 0.02（召回层可能漏了更好的 chunk）
2. **无命中**：`returned_chunk_ids` 为空且步骤 3 的文件系统检索也无命中
3. **用户反馈**：用户在下一轮对话中对本轮回答表达不满（"不对"/"错了"/"不是这个"），且 `retrieval_scores` 显示低分

#### 9.3 触发方式

将 recall trace 写入信号文件，然后启动独立 `claude -p` 进程处理：

```bash
# 1. 写信号文件
python -c "
import json, os, datetime
signal = {
    'question': '<用户原问题>',
    'returned_chunk_ids': <chunk_id 列表>,
    'returned_doc_ids': <doc_id 列表>,
    'retrieval_scores': <分数列表>,
    'answer_summary': '<回答摘要>',
    'feedback_type': 'low_score',
    'session_id': '<session_id>'
}
os.makedirs('data/eval/self-heal-pending', exist_ok=True)
open('data/eval/self-heal-pending/<session_id>.json', 'w', encoding='utf-8').write(
    json.dumps(signal, ensure_ascii=False, indent=2)
)
"

# 2. 后台触发自愈（fire-and-forget）
claude -p "读取 data/eval/self-heal-pending/<session_id>.json 中的反馈信号，使用 self-heal-workflow skill 执行召回自愈。完成后删除信号文件。" &
```

#### 9.4 硬约束

1. **fire-and-forget**：启动 `claude -p` 后不等待结果，用户对话不受阻塞。
2. **信号文件必须先写**：`claude -p` 进程需要从文件读取信号，不能只靠命令行参数（太长会截断）。
3. **不向用户暴露自愈**：用户看不到自愈过程，只能看到回答。自愈是底层优化。
4. **自愈失败不影响回答**：即使 `claude -p` 启动失败，本轮回答已经给到用户。
5. **同一 session 不重复触发**：如果 `data/eval/self-heal-pending/<session_id>.json` 已存在，不重复写入。

### 步骤10: 委托 Organize Agent 固化本轮答案

在回答已经给到用户之后异步执行（不阻断本轮响应返回）。

#### 10.1 触发条件

满足以下全部条件时触发：

1. 本轮给出了完整答案（非“证据不足”或“无法可靠回答”）。
2. 答案基于至少 1 条本地证据，或本轮触发了 get-info-agent 抓取了新证据。
3. 问题是可复用的：事实性 / 流程性 / 概念性。
4. 答案不包含敏感信息（凭证、API key、个人身份信息）。
5. 本轮不是“固化层命中 hit_fresh 直接返回”的分支（`hit_fresh` 不需要重复固化）。

#### 10.2 分支类别

| 本轮来源 | Organize Agent 模式 |
|---|---|
| 步骤 0 返回 `miss` + 步骤 1、8 走完纯本地 RAG | `crystallize` |
| 步骤 0 返回 `miss` + 步骤 1、8 中触发了 get-info-agent | `crystallize` |
| 步骤 0 返回 `hit_stale` + 刷新成功 | `refresh`（由 organize-agent 内部在刷新流程中自动完成，本步骤只需重新触发用户反馈录入机制；若固化层内部已经自动写入，本步骤可直接跳过） |
| 用户在本轮对上一轮固化答案表达 confirm / reject / 补充 | `feedback`（与 `crystallize` 互斥：本轮只回复反馈，不站为新问答新固化） |

#### 10.3 触发参数

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

#### 10.4 硬约束

1. 固化失败（磁盘满 / 权限不足 / organize-agent 不可达）时**不得回溯用户**。本轮回答已给到用户，固化是底层优化，失败只需写日志。
2. 触发 organize-agent 后不要等待它返回结果再回应用户——qa-workflow 的主路径在步骤 8 之后已结束，本步骤仅为侧面写入。
3. 不得在 `crystallize` 模式里伪造 `source_chunks`；如果本轮无本地 chunk 依赖（纯走 get-info 后写盘了新 chunk），`source_chunks` 为本轮新写入的 chunk_id。

## 6. 回答格式要求

建议输出结构：

1. 简要答案。
2. 关键依据。
3. 如果触发了 Get-Info，说明新增资料来源。
4. 如果存在限制，明确写出限制。

## 7. 失败策略（degrade-first）

核心原则：任何基础设施层错误都必须 **先尝试降级**，再考虑向用户返回错误。仅业务级错误（问题无法理解、用户明确要求某个不存在的文档）才直接告知。

1. **检索命令失败**：对 `milvus-cli.py multi-query-search` 非零退出，先更新 `infra_status.milvus_available = false`，继续走文件系统证据，进入步骤 8.1 正常模式，不要立刻报错。
2. **Milvus 不可用**：跳过步骤 4，只用文件系统证据；如果证据不足 → 试触发 get-info；如 get-info 也不可用 → 进入降级模式回答（步骤 8.2）。绝不能直接向用户报 "Milvus 不可用" 而不给答案。
3. **get-info-agent 异常 / 超时**：仅记录 `degraded_reason` 进入步骤 8.2，不向用户暴露 stack trace。
4. **本地与新抓取资料冲突**（业务级）：必须显式指出，不要静默取舍。
5. **用户要求的某个具体文件不在知识库里**（业务级）：直接告知用户找不到该文档，提醒其使用 upload-agent 入库。

## 8. 与其他组件的协作

1. `qa-agent` 负责调用本 skill。
2. `crystallize-workflow` 负责固化层的命中判断与新鲜度判断，由本 skill 步骤 0 与步骤 9 调用。
3. `organize-agent` 负责固化层的写入与刷新调度，由本 skill 步骤 9 触发；步骤 0 的 `hit_stale` 分支也需要通过 organize-agent 完成刷新。
4. `get-info-agent` 负责补充外部资料。
5. `update-priority` 由 Get-Info 成功抓取或调度流程触发，不由 QA 直接重写优先级配置。
6. `self-heal-workflow` 由本 skill 步骤 9 触发，负责召回失败后的 question 补充和重新入库。触发方式为独立 `claude -p` 进程，fire-and-forget。
