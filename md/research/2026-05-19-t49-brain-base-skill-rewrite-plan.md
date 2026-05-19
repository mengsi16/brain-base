# T49 brain-base-skill 重写执行计划

> **任务**：把 `brain-base-skill/SKILL.md` 从过时的 claude-plugin 模式（`bin/brain-base-cli.py` + `claude -p --agent`）重写为对齐当前 LangGraph CLI（`python -m brain_base.cli`）的外部 Agent 调用手册。
>
> **创建时间**：2026-05-19
>
> **执行前必须用户确认**（CLAUDE.md：执行前写详细计划 + 技术审查）。

---

## 1. 偏差对照表（现行 SKILL.md vs 实际 CLI）

### 1.1 命令路径

| SKILL.md 描述 | 实际 CLI | 状态 |
|--------------|---------|------|
| `python bin/brain-base-cli.py` | 文件不存在 | ❌ 全部失效 |
| `python -m brain_base.cli` | 实际入口 | 需要改成这个 |

### 1.2 命令矩阵对比

| 命令 | SKILL.md 描述 | 实际 CLI | 处理 |
|------|--------------|---------|------|
| `health` | ✅ | ✅ | 保留 + 修参数（删 `--require-local-model --smoke-test` 等不存在的 flag） |
| `search` | ✅ | ✅ | 保留 + 对齐参数（实际是 `--query`/`--top-k-per-query`/`--final-k`/`--rrf-k`/`--no-rerank`） |
| `exists` | ✅ | ❌ 不存在 | **删除整个 §3.3**（CLI 无此命令；如需去重靠 ingest 内部 sha256/url frontmatter dedup 自动处理） |
| `ask` | ✅ | ✅ | 保留 + 重写：删除 `claude -p` 包装描述，改为 `python -m brain_base.cli ask "<prompt>"`；加 `--session/--state-dump/--headless/--debug-pause-google` |
| `ingest-url` | ✅ | ✅ | 保留 + 对齐参数（实际只有 `--url/--source-type/--topic/--title-hint`，没有 `--latest`） |
| `ingest-file` | ✅ | ✅ | 保留 + 对齐参数（实际是 `--path` 可多次） |
| `ingest-text` | ✅ | ❌ 不存在 | **删除整个 §3.7**（如需文本入库，先写成 `.md` 文件再 `ingest-file`） |
| `feedback` | ✅ | ❌ 不存在 | **删除整个 §3.11 + §4.1 反馈流程 + §7.2 §7.3 反馈规则**（固化层是基于 LLM 自动判断 value_score + crystallize_answer 节点写入，无需外部反馈触发） |
| `resume` | ✅ | ❌ 不存在 | **删除整个 §3.8**；多轮对话改为 `ask --session <id>` JSONL 持久化机制 |
| `history` | ✅ | ❌ 不存在 | **删除整个 §3.9**；如需查看会话直接读 `data/sessions/<id>.jsonl` |
| `remove-doc` | ✅ | ✅ | 保留 + 对齐参数（实际 `--doc-id`/`--url`/`--sha256`/`--confirm`/`--force-recent`/`--reason`） |
| `chat` | ❌ 未提 | ✅ | **新增**：交互式多轮（内存维护 history） |
| `lint` | ❌ 未提 | ✅ | **新增**：固化层周期清理 |
| `crystallize-check` | ❌ 未提 | ✅ | **新增**：固化层命中判断（不调 LLM） |

### 1.3 输出格式

| SKILL.md 描述 | 实际 CLI | 处理 |
|--------------|---------|------|
| 「所有命令都输出 JSON」（统一 JSON 壳：`session_id/result.ok/result.exit_code/result.stdout/result.stderr`） | `ask`/`chat` 输出 answer 文本到 stdout，log 到 stderr；`search`/`ingest-*`/`remove-doc`/`lint`/`health`/`crystallize-check` 输出 JSON 到 stdout，log 到 stderr | **重写 §7.1**：分两类 — answer-text 类（ask/chat）+ JSON 类（其余）；用 `--state-dump <path>` 拿完整 state |
| `session_id` 是后续反馈的关键主键 | 无 session_id 概念；`ask --session <id>` 是用户自命名的字符串，落盘 `data/sessions/<id>.jsonl` | **重写**：session 是用户自定义字符串，不是 CLI 自动生成的 UUID |

### 1.4 环境变量

| SKILL.md 描述 | 实际 CLI | 处理 |
|--------------|---------|------|
| `BRAIN_BASE_PATH` | ✅ 仍可用作 cwd 提示，但不是 CLI 内部读取 | 保留作为「外部 Agent 用来定位 brain-base 仓库」的提示 |
| `BRAIN_BASE_CLAUDE_BIN` | ❌ 不存在 | **删除**（claude-plugin 残留） |
| `BB_LLM_PROVIDER` | ✅ | **新增**：anthropic / openai / minimax / glm / deepseek / qwen / xai / openrouter |
| `BB_LLM_BASE_URL` | ✅ | **新增** |
| `BB_LLM_API_KEY` | ✅ | **新增**（缺时兜底 ANTHROPIC_API_KEY / OPENAI_API_KEY） |
| `BB_DEEP_THINK_LLM` | ✅ | **新增**：模型名 |
| `BB_LOG_LEVEL` | ✅ | **新增**：日志级别 |
| `BB_PLAYWRIGHT_HEADLESS` | ✅ | **新增**：默认有头（Google 反检测） |

### 1.5 架构图

| SKILL.md 描述 | 实际架构 | 处理 |
|--------------|---------|------|
| `qa-agent` / `get-info-agent` / `upload-agent` / `lifecycle-agent` / `organize-agent` 五个 claude plugin | LangGraph 8 子图：BrainBaseGraph 顶层 + QaGraph / IngestFileGraph / IngestUrlGraph / LifecycleGraph / LintGraph / CrystallizeGraph / GetInfoGraph / PersistenceGraph | **重写 §9**：按子图重画调用关系 |
| 内部 workflow：qa-workflow / get-info-workflow / web-research-ingest / playwright-cli-ops / knowledge-persistence / update-priority / lifecycle-workflow | 实际节点：probe / crystallized_check / extract_urls / url_pre_fetch / normalize / decompose / intent_planner / intent_executor / intent_observer / merge_evidence / fanout_persist / write_raw_one / barrier_raw / fanout_enrich / enrich_one / barrier_enrich / ingest / fanout_search / subquery_search_one / barrier2 / judge / answer / self_check / crystallize_answer | **重写 §9**：但不全暴露内部节点，只暴露外部 Agent 应该理解的「QA 主流程」+「6 工具 TOOL_REGISTRY」概念 |

### 1.6 §6「claude -p 直调」整段失效

§6.1 - §6.4 全部基于 `claude -p --plugin-dir --agent brain-base:xxx`，整套已经不存在。**整段删除**，不留兜底。

---

## 2. 风险审查

### 2.1 依赖关系
- **零代码改动**：本任务只改 `brain-base-skill/SKILL.md` 一个文件，不动 `brain_base/` / `bin/` / 测试。
- **零回归面**：CLI 行为不变，外部 Agent 当前要么已经按实际 CLI 调（已经在用），要么按旧 SKILL.md 调（已经全失败）——无论改不改 SKILL.md 都不影响现有可工作的集成。

### 2.2 有争议的设计决策

| 决策点 | 选项 A（采纳） | 选项 B | 理由 |
|--------|---------------|-------|------|
| 是否暴露 6 工具 TOOL_REGISTRY | **不暴露给外部 Agent 直接调** | 暴露 | 外部 Agent 应只关心 `ask`，工具是 intent_planner LLM 内部决策；暴露反而引诱外部重写 RAG 逻辑（违背 SKILL.md §1.1「不要重写 brain-base 内部工作流」原则） |
| 是否在 SKILL 里描述 LangGraph 子图 | **简单提及（数量+名字+一句话职责）** | 详细描述 | 外部 Agent 不需要知道节点级细节；想了解的去看 README.md 架构章节。SKILL.md 保持「调用手册」定位 |
| 是否描述固化层的 hit_fresh/hit_stale 等 6 状态 | **不描述** | 描述 | 外部 Agent 看到的只有 ask 的 answer 文本，固化命中是性能优化对调用方透明 |
| 多轮对话用 `--session` 还是 `chat` | **两者都说，分场景** | 只说一种 | 实际 CLI 两种都支持：`chat` 内存只单进程，`ask --session` 跨进程持久化；外部 Agent 编排器需要后者，交互式人用前者 |
| 是否在 SKILL 顶部加显著的「v2 重写说明」 | **不加** | 加 changelog | SKILL.md 是给外部 Agent 看的契约，不需要自我历史化；如需追溯看 git log |

### 2.3 兼容性陷阱

- **github-trending-monitor 风险**：用户记忆里有 fd5ed207 提到 github-trending-monitor 想用 brain-base-skill 集成，按现行 SKILL.md 实现会全盘踩坑。**改完 SKILL.md 后无需通知集成方迁移**，因为旧 CLI（`bin/brain-base-cli.py`）根本不存在，集成方还没集成成功，不存在「破坏向后兼容」问题。
- **`ingest-text` 已删但有需求场景**：原 SKILL.md §3.7 提到「上层 Agent 已拿到 Markdown / README 正文」想直接入库的场景。当前 CLI 没有此命令——**给出 workaround**：先 `Out-File / Set-Content` 写到 `data/temp/<name>.md`，再 `ingest-file --path data/temp/<name>.md`。
- **`exists` 已删但有需求场景**：原 SKILL.md §3.3 入库前去重场景。当前 CLI 内部 `frontmatter_node` 已经会按 sha256 自动 dedup（短路 `dedup_skipped`），无需外部去重——**说明这一点**，避免外部 Agent 自己手拼去重逻辑。
- **`feedback` 已删但反馈机制要解释**：当前固化是 `crystallize_answer` 节点根据 LLM 评分 `value_score >= 0.3` 自动写入，不靠外部触发——**简单说明这一点**，让外部 Agent 知道「不需要主动 confirm，命中固化层是自动的」。

### 2.4 测试盲区

- 没有 SKILL.md 的自动化测试覆盖。**验收靠人工 review** + 用 grep 验证：
  - 不再出现 `bin/brain-base-cli.py` 字符串
  - 不再出现 `--plugin-dir` / `--agent brain-base:` / `claude -p` 字符串
  - 不再出现 `feedback` / `resume` / `history` / `exists` / `ingest-text` / `BRAIN_BASE_CLAUDE_BIN` 字符串
  - 所有命令示例用 `python -m brain_base.cli` 起头
  - 所有 env 用 `BB_LLM_*` / `BB_LOG_LEVEL` / `BB_PLAYWRIGHT_HEADLESS`

### 2.5 工作量量级

- **预估**：~400-450 行新 SKILL.md（原 461 行）
- **改动模式**：整体重写，不是逐行 edit——claude-plugin 残留太多，逐行改容易遗漏
- **执行方式**：write_to_file 覆盖（先 read 再 write）；约 1 次 write_to_file 调用
- **不需拆子任务**：单文件文档重写，可一次完成

---

## 3. 新 SKILL.md 章节大纲（重写后结构）

```
---
name: brain-base
description: 任何需要问答或把本地文档入库的场景，默认先调用 brain-base skill。
disable-model-invocation: false
---

# brain-base

[1 段定位说明：基于 LangGraph 的知识底座，外部 Agent 通过 python -m brain_base.cli 调用]

## 1. 调用原则
  1.1 默认优先级（首选 brain_base.cli + 不要重写内部 + 检索/问答分开 + URL/文件入库分开）
  1.2 什么情况该调（5 条触发条件）
  1.3 什么情况不该调（4 条排除条件）

## 2. 命令矩阵
  2.1 命令一览表（9 个命令：health / search / ask / chat / ingest-url / ingest-file / remove-doc / lint / crystallize-check）
  2.2 「我手上有什么 → 调什么」决策表

## 3. 命令详解
  3.1 health
  3.2 search
  3.3 ask（含 --session / --state-dump / --headless / --debug-pause-google）
  3.4 chat（交互式）
  3.5 ingest-url
  3.6 ingest-file（含 .md/.txt 文本场景：先落盘再 ingest-file 的 workaround）
  3.7 remove-doc（dry-run + confirm 两阶段）
  3.8 lint
  3.9 crystallize-check

## 4. 强 Agent 推荐调用策略
  4.1 默认流程（启动→问答→入库）
  4.2 业务场景映射
       场景 A：Agent Loop 问答
       场景 B：监控/爬虫型补库（github-trending-monitor 类）
       场景 C：本地文档批量入库
       场景 D：只想做检索候选
       场景 E：跨进程多轮对话

## 5. 输出格式
  5.1 stdout 文本类（ask / chat）→ answer markdown
  5.2 stdout JSON 类（其余）→ 子图 result dict
  5.3 stderr → log（BB_LOG_LEVEL 控制）
  5.4 --state-dump：拿完整 QaState 用于调试 / e2e

## 6. 环境变量
  6.1 必填：BB_LLM_API_KEY / BB_LLM_PROVIDER / BB_DEEP_THINK_LLM
  6.2 可选：BB_LLM_BASE_URL / BB_LOG_LEVEL / BB_PLAYWRIGHT_HEADLESS / BB_DEBUG_PAUSE_GOOGLE
  6.3 路径：BRAIN_BASE_PATH（外部 Agent 定位仓库的约定，CLI 内部不读）

## 7. 内部架构（外部 Agent 应了解的最小集合）
  7.1 LangGraph 8 子图（一句话职责，不展开）
  7.2 QA 主流程（一句话：固化命中 → 直返 / 否则 → 意图 Agent-Loop → 检索 → 回答 → 自检 → 固化）
  7.3 持久化三层（raw / chunks+Milvus / crystallized）
  7.4 6 工具 TOOL_REGISTRY（intent_planner LLM 自主调用，外部 Agent 不直接看到）
  7.5 source_priority 三档（official-doc / community / user-upload）

## 8. 前置条件
  - Docker compose 已起 Milvus 三件套 + brain-base-worker
  - .env 配好 LLM key
  - python -m brain_base.cli health 三项 ok

## 9. 错误处理
  各命令常见错误 + 处理建议（health 不通 / LLM key 缺 / Milvus 未起 / playwright 未装 / MinerU OOM）

## 10. 一句话总结
  外部 Agent 把 brain-base 当成「LangGraph 驱动的知识基础设施」调，所有调用走 python -m brain_base.cli。
```

---

## 4. 验收标准

1. **grep 验证**（必须 0 命中）：
   - `bin/brain-base-cli.py`
   - `--plugin-dir`
   - `--agent brain-base:`
   - `claude -p`
   - `BRAIN_BASE_CLAUDE_BIN`
   - `feedback` / `resume` / `history` / `exists` / `ingest-text` 作为 CLI 子命令出现的位置（说明性提及如「无需 feedback，固化是自动的」可保留）

2. **grep 验证**（必须有命中）：
   - `python -m brain_base.cli` 出现 ≥ 9 处（每个命令至少一处示例）
   - `BB_LLM_API_KEY` / `BB_LLM_PROVIDER` / `BB_DEEP_THINK_LLM` 出现
   - `LangGraph` / `StateGraph` 出现（架构定位）
   - `--session` 出现（多轮对话替代 resume）
   - `chat` / `lint` / `crystallize-check` 三个新命令各有一节

3. **章节完整性**：按上述 §3 大纲 1-10 章全部存在

4. **可读性**：保持原 SKILL.md 的「外部强 Agent 调用手册」定位语气，不堆砌 LangGraph 内部实现细节

5. **行数控制**：~400-500 行（删冗余 + 加新能力描述，净变化预期 -30 到 +40 行）

---

## 5. 风险缓解

| 风险 | 缓解 |
|------|------|
| 重写后某个外部 Agent 依赖现行 SKILL.md 描述 | 现行描述全部失效（命令路径不存在），不可能有依赖；改完不会破坏任何能跑的集成 |
| 误删重要场景描述 | 偏差对照表 §1 已逐项过；新大纲 §3 的 §3.6 / §4.2 / §1.3 保留所有有效场景 |
| 新增能力描述过多导致文档冗长 | 严格按「外部 Agent 需要知道」原则；架构细节只在 §7 一句话带过，详情指引到 README.md |
| 用户希望保留 `feedback` 等命令的语义 | **本计划默认不补 CLI 命令**——用户如希望补 `feedback` 等，应起单独 CLI 开发任务，不在本任务范围 |

---

## 6. 执行步骤（用户确认后）

1. 标记 ToDo.md T49 `pending → executing`
2. `read_file` brain-base-skill/SKILL.md（已完成，461 行）
3. `write_to_file` brain-base-skill/SKILL.md（按 §3 大纲整体重写）— 单次 write，约 400-500 行
4. grep 验证（§4 验收标准）
5. 标记 ToDo.md T49 `executing → finished` + 写实际产出
6. `git add brain-base-skill/SKILL.md ToDo.md md/research/2026-05-19-t49-*.md`
7. `git commit -m "docs(T49): rewrite brain-base-skill for LangGraph CLI" -m "..."`
8. `git push tlh main`

---

## 7. 用户确认点

执行前请用户确认以下决策：

1. **是否暴露 6 工具 TOOL_REGISTRY** 给外部 Agent？（默认 §2.2 选 A：不暴露，仅在 §7 一句话提及）
2. **`ingest-text` 用 workaround 描述** 是否够？（默认 §2.3：用「先落盘再 `ingest-file`」workaround 替代）
3. **`feedback` 自动化机制描述程度**？（默认 §2.3：一句话说明「固化是自动的，无需外部触发」）
4. **是否补 OPERATIONS_MANUAL_zh.md 等其他文档**？（默认 §2.2：只改 SKILL.md，README 已对，OPERATIONS_MANUAL 是给维护者的）
