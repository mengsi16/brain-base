---
name: brain-base
description: 任何需要问答或把本地文档入库的场景，默认先调用 brain-base skill。
disable-model-invocation: false
---

# brain-base

本 skill 是 **brain-base 的外部 Agent 调用手册**，面向 Agent Loop / 工程编排器 / 多 Agent 系统。brain-base 是一个基于 **LangGraph StateGraph** 的个人知识库，提供三层架构 RAG（原始层 → 检索层 → 固化层）。

核心定位：
1. brain-base 是一个**稳定的知识底座**，不是一堆零散的 Agent Prompt
2. 所有调用统一走 `python -m brain_base.cli`（LangGraph CLI）
3. 外部 Agent 只关心 **search / ask / ingest / remove-doc / health** 五类意图
4. 内部 7 个 LangGraph 子图 + 6 工具 TOOL_REGISTRY 的复杂细节收敛在 CLI 后面（T50 后原 IngestUrlGraph 删除）

## 1. 调用原则

### 1.1 默认优先级

1. **首选 `python -m brain_base.cli`**：这是给外部强 Agent 用的稳定调用边界。
2. **不要自己重写 brain-base 内部工作流**：不要在外部 Agent 里重做"改写 / 证据判断 / 分块 / 合成 QA / 固化"这些逻辑。
3. **检索和问答分开**：只想拿候选证据 → `search`；需要完整 Agentic RAG 回答 → `ask`。
4. **URL 入库和本地文件入库分开**：**问题里包含 URL 时，`ask` 主图会自动 fetch + 去重 + 入库順带回答**（不需额外提示词、也不需独立命令；T50 删了重复设计的 `ingest-url`，本来就是 ask 的 URL 处理分支）；本地文件 → `ingest-file`。

### 1.2 什么情况该调 brain-base

满足任一即触发：
1. 需要复用已有知识库做问答、对比、方案分析、术语解释。
2. 需要把外部网页、GitHub 项目页、README、官方文档补进知识库。
3. 需要把本地 PDF / DOCX / MD / TXT / 代码文件沉淀进知识库。
4. 需要让知识随着问答自动积累，而不是每次都从零检索。
5. 需要把 RAG 能力交给一个专门的知识系统，而不是在上层业务 Agent 内部重复实现。

### 1.3 不该调 brain-base 的情况

1. 纯闲聊或与知识库无关的对话。
2. 用户只想临时读一个文件但**不要求入库**。
3. 调用方明确要求"直接联网搜索，不写知识库"。
4. 只是想做极轻量字符串匹配，不需要 RAG / 入库 / 积累。

## 2. 命令矩阵

统一入口：
```bash
python -m brain_base.cli <command> [options]
```

### 2.1 命令一览表

| 意图 | 命令 | 是否走 LLM | 典型用途 |
|------|------|-----------|----------|
| 健康检查 | `health` | 否 | 启动前探测 Milvus / playwright / LLM |
| 纯检索 | `search` | 否 | 拿候选 chunk，不生成答案 |
| 完整问答 | `ask` | 是 | Agentic RAG：固化命中 → 意图循环 → 检索 → 回答 → 自检 → 固化 |
| 交互式多轮 | `chat` | 是 | 内存维护对话历史，自动指代消解，`/q` 退出 |
| URL 入库 | `ask "<问题 + URL>"` | 是 | 问题中出现 URL 时，ask 主图自动 fetch + 去重 + 入库（T50 删了重复的 ingest-url）：GitHub 项目页 / README / 官方文档 / 网页 |
| 本地文件入库 | `ingest-file` | 是 | PDF / DOCX / PPTX / XLSX / MD / TXT / 图片入库（MinerU + pandoc） |
| 删除文档 | `remove-doc` | 是 | 跨存储层一致性删除（dry-run + confirm 两阶段） |
| 固化层清理 | `lint` | 否 | 清理 rejected / 过期固化条目 |
| 固化命中检查 | `crystallize-check` | 否 | 判断某问题是否命中固化层（不生成答案） |

### 2.2 最核心的调用选择

| 你手上有什么 | 想要什么 | 应调用 |
|---------------|----------|--------|
| 一个问题 | 完整回答 | `ask` |
| 一个问题 | 只要候选证据 | `search` |
| 一个 URL | 写入知识库（順带问答） | `ask "<问题中包含 URL>"`，如 `ask "介绍一下 https://x.com/y"` |
| 一个本地文件路径 | 写入知识库 | `ingest-file` |
| 一段 Markdown / README 正文 | 写入知识库 | 先落盘到 `.md` 文件，再 `ingest-file --path` |
| 一个 doc_id 要删除 | 清理过期/重复文档 | `remove-doc` |
| 一个问题 | 判断是否已有固化答案 | `crystallize-check` |
| 需要持续多轮对话 | 跨进程上下文保持 | `ask --session <id>` |
| 需要交互式对话 | 人在终端里聊 | `chat` |

## 3. 命令详解

### 3.1 `health`

用途：启动前一次性探测 brain-base 基础设施。
```bash
python -m brain_base.cli health
```
返回 JSON 包含 Milvus / playwright / LLM 三项状态。

适合：系统启动自检、CI 冒烟检查、Agent Loop 开机前探测。

### 3.2 `search`

用途：**纯检索**，不生成答案，不调 LLM。
```bash
python -m brain_base.cli search \
  --query "claude code subagent" \
  --query "how to create claude code subagent" \
  --top-k-per-query 20 \
  --final-k 10 \
  --no-rerank
```
特点：
1. 直接走 Milvus hybrid 检索（bge-m3 dense + sparse）
2. 默认启用 bge-reranker-v2-m3 cross-encoder 重排（`--no-rerank` 跳过）
3. 返回结构化候选列表（含 chunk 文本 / doc_id / score / source_type）

适合：
1. "先查库里有没有，再决定要不要 ask"
2. 业务 Agent 想自己做多路融合
3. 爬虫入库后做验证回查

### 3.3 `ask`

用途：走完整 QA 链路（LangGraph QaGraph）。
```bash
python -m brain_base.cli ask "Claude Code 的 subagent 怎么配置？"
```
内部流程：
1. 固化层命中且新鲜 → 直接返回缓存答案（秒级）
2. 未命中 → normalize 改写 → decompose 分解 → intent_planner/executor/observer 意图循环（LLM 自主调度 6 工具：web_search / fetch_url / raw_text / local_search / arxiv_pdf / github_raw）
3. 外检抓到的内容自动入库 → 重新检索
4. judge 评估证据充分性 → answer 生成回答 → self_check 自检忠实度/完整性/一致性
5. 满意答案自动固化到 `data/crystallized/`（value_score ≥ 0.3），下次相似问题短路返回

输出：answer Markdown 文本到 stdout；log 到 stderr。

**多轮对话**（跨进程持久化）：
```bash
# 第一轮
python -m brain_base.cli ask "RAGFlow 是什么？" --session rag-talk

# 第二轮（自动消解「它」→ RAGFlow）
python -m brain_base.cli ask "它支持哪些文档格式？" --session rag-talk
```
对话历史持久化到 `data/sessions/<id>.jsonl`，同 id 自动续上。指代词「它/那个/还有别的吗？」由 normalize 节点基于历史自动消解。

**调试**：
```bash
# 把完整 QaState dump 到 JSON 文件（e2e 测试评判用）
python -m brain_base.cli ask "问题" --state-dump ./debug/state.json

# playwright 强制无头（服务器 / CI）
python -m brain_base.cli ask "问题" --headless
```

### 3.4 `chat`

用途：交互式多轮对话（人在终端里聊）。
```bash
python -m brain_base.cli chat
```
特点：
1. 内存维护对话历史（`/q` 退出后丢失）
2. 自动指代消解
3. 单轮 LLM 异常不崩进程（try/except 包单轮）
4. 适合开发调试 / 手动探索知识库

### 3.5 URL 入库（问题里带 URL、复用 `ask` 主图）

**关键设计**：ask 主图本身就是 URL 感知的。不需要"收录请求"、"ingest 提示词"、独立命令。**只要问题中出现 URL，主图会自动走 `extract_urls → url_pre_fetch → fetch_url 工具` 入库順带回答。**

推荐用法（自然问句即可）：
```bash
python -m brain_base.cli ask "介绍一下 https://docs.litellm.ai/"
python -m brain_base.cli ask "https://github.com/some/repo 这个项目是干什么的？"
python -m brain_base.cli ask "比较 https://a.com 和 https://b.com 的区别"
```

ask 主图内部自动完成：`extract_urls` 提取 user_urls → `url_pre_fetch` 调 `fetch_url` 工具（readability + sha256 + hash_lookup 去重）→ `qa_persist.write_raw_one`（写 `source_priority` P0-P3）→ `chunker` 切块 → `enrich` → Milvus 入库→ 主图继续检索+回答。

全过程 SHA-256 去重（已存在的内容短路跳过，不会重复入库）。

**历史说明**：T50 前有 `ingest-url` 独立子命令，但与 ask URL 处理分支重复、已删。外部 Agent 不要在问题前缀加"请收录"/"ingest"/"入库"等提示词——直接问你想问的自然问题即可。

适合：
1. 外部 Agent 拿到 URL 后不用区分"补库" vs "问答"，统一走 `ask`（两者同一个主图）
2. 像 `github-trending-monitor` 这种"自己抓榜单，但项目详情页交给 brain-base"的架构：对每个项目 URL 调 `ask "介绍一下 <url>"`
3. 返回的是问答 + 入库摘要 JSON（后者在 `evidence` / `get_info_ingested` 字段）

### 3.6 `ingest-file`

用途：本地文件入库。
```bash
python -m brain_base.cli ingest-file \
  --path ./papers/paper.pdf \
  --path ./notes.md
```
参数：
- `--path`：文件路径，可多次指定

支持格式：PDF / DOCX / PPTX / XLSX / MD / TXT / 图片 / LaTeX（MinerU 3.x + pandoc）。

内部走 IngestFileGraph：convert（MinerU/pandoc → MD）→ frontmatter（含 SHA-256 去重）→ doc_enrich（LLM 文档级摘要/关键词）→ persist（chunk → enrich → Milvus）。

**如果你手上有 Markdown / README 正文但不想先自己落盘**：当前 CLI 没有 `ingest-text` 命令。workaround：先把内容写到临时 `.md` 文件，再 `ingest-file --path`：
```bash
# Windows PowerShell
Set-Content -Path ./data/temp/my-readme.md -Value $markdownContent -Encoding UTF8
python -m brain_base.cli ingest-file --path ./data/temp/my-readme.md
```

### 3.7 `remove-doc`

用途：跨存储层一致性删除文档。
```bash
# dry-run：只输出删除清单
python -m brain_base.cli remove-doc --doc-id my-doc-2026-05-06 --reason "过期文档"

# confirm：执行删除
python -m brain_base.cli remove-doc --doc-id my-doc-2026-05-06 --confirm --reason "确认删除"
```
参数：
- `--doc-id`：doc_id，可多次指定
- `--url`：按 URL 查找，可多次指定
- `--sha256`：按 SHA-256 查找
- `--confirm`：必须显式加上才真删（默认 dry-run）
- `--force-recent`：跳过时间保护
- `--reason`：删除原因

内部走 LifecycleGraph：resolve → scan → dry_run →（confirm 时）delete_milvus → delete_files → clean_index → audit。

适合：Agent Loop 定期清理过期/重复文档。

### 3.8 `lint`

用途：固化层健康检查。
```bash
python -m brain_base.cli lint
```
内部走 LintGraph：scan 固化层全部条目 → check 新鲜度 → degrade 过期条目 → delete rejected 条目。

适合：定期维护，清理固化层中的孤儿文件和过期答案。

### 3.9 `crystallize-check`

用途：判断某问题是否命中固化层（不生成答案，不调 LLM）。
```bash
python -m brain_base.cli crystallize-check --question "LiteLLM 是什么？"
```
返回 JSON 含 `status` 字段（`hit_fresh` / `hit_stale` / `cold_observed` / `cold_promoted` / `miss` / `degraded`）。

适合：外部 Agent 想先判断"这个问题是不是已经有高质量固化答案了"，再决定走 `ask` 还是直接拿缓存。

## 4. 强 Agent 的推荐调用策略

### 4.1 默认流程
```text
启动前：health

要回答问题：
  1. 可选先 crystallize-check（判断是否已有固化答案）
  2. 再 ask（固化命中秒返，未命中走完整 RAG）
  3. 固化是自动的（value_score ≥ 0.3 自动写入），无需外部触发反馈

要补库：
  1. URL → `ask "<问题中包含 URL>"`（问题里带 URL 即可，ask 主图自动 fetch+hash_lookup+入库順带回答；T50 删了重复的 ingest-url）
  2. 本地文件 → ingest-file

要多轮对话：
  1. 跨进程 → ask --session <id>
  2. 人在终端 → chat

要删除文档：
  1. remove-doc --doc-id <ID> --reason "原因"（dry-run）
  2. remove-doc --doc-id <ID> --confirm --reason "确认"（执行）

定期维护：
  1. lint（清理固化层）
```
### 4.2 业务场景映射

#### 场景 A：Agent Loop 问答

1. `ask "问题"`
2. 拿 stdout 的 answer Markdown 当最终回答
3. 无需发反馈——固化是自动的

#### 场景 B：监控/爬虫型系统补库（如 github-trending-monitor）

1. 先抓索引页/榜单页（业务系统自己负责）
2. 对每个项目 URL 直接 `ask "介绍一下 <url>"`（任何包含 URL 的问题都会触发 ask 主图自动 fetch+入库，内部 hash_lookup 去重已存在内容短路跳过）
3. 入库后必要时 `search` 验证可检索性
4. 过期项目 → `remove-doc --doc-id <ID> --confirm` 清理
5. 需要对项目问答 → `ask` + `ask --session` 多轮对话

#### 场景 C：本地文档批量入库

1. `ingest-file --path ./doc1.pdf --path ./doc2.md --path ./doc3.docx`
2. 检查返回 JSON 中的 `conversion_errors` / `persistence_results` 确认成功率

#### 场景 D：只想做"知识检索候选"而不是完整问答

1. 用 `search`
2. 外部 Agent 自己消费结果并做业务裁决
3. 不要为了拿候选证据去调 `ask`

#### 场景 E：跨进程多轮对话

1. 第一轮：`ask "问题" --session my-session`
2. 第二轮：`ask "追问" --session my-session`（自动消解指代）
3. 历史文件：`data/sessions/my-session.jsonl`

## 5. 输出格式

### 5.1 文本输出类（`ask` / `chat`）

- **stdout**：answer Markdown 文本（最终回答正文）
- **stderr**：log 流（节点执行日志，`BB_LOG_LEVEL` 控制级别，默认 INFO）
- **exit code**：0 = 成功，1 = 失败（LLM key 缺失 / 异常）

### 5.2 JSON 输出类（其余命令）

- **stdout**：JSON（子图 result dict）
- **stderr**：log 流
- **exit code**：0 = 成功，1 = 失败

### 5.3 调试输出

`ask --state-dump <path>` 把完整 QaState dict 写入 JSON 文件（含 evidence / sub_questions / answer / crystallize_result 等全部字段），适合 e2e 测试评判和排障。

## 6. 环境变量

调用前在 brain-base 项目根目录的 `.env` 中配置（复制 `.env.example`）：

### 6.1 必填

| 变量 | 作用 | 示例 |
|------|------|------|
| `BB_LLM_PROVIDER` | LLM provider | `anthropic` / `openai` / `minimax` / `glm` / `deepseek` / `qwen` / `xai` / `openrouter` |
| `BB_DEEP_THINK_LLM` | 模型名 | `claude-sonnet-4-20250514` / `MiniMax-M2.7` / `deepseek-chat` |
| `BB_LLM_API_KEY` | API key | `sk-xxx`；缺时尝试 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` 兜底 |

### 6.2 可选

| 变量 | 默认 | 作用 |
|------|------|------|
| `BB_LLM_BASE_URL` | 空（用 provider 默认） | 自定义 API 端点（如 MiniMax Anthropic 兼容：`https://api.minimaxi.com/anthropic`） |
| `BB_LOG_LEVEL` | `INFO` | 日志级别（`DEBUG` / `INFO` / `WARNING` / `ERROR`） |
| `BB_PLAYWRIGHT_HEADLESS` | 空（默认有头） | `1` = 强制无头（服务器 / CI）；默认有头（Google 反检测） |
| `BB_DEBUG_PAUSE_GOOGLE` | 空 | `1` = Google 搜索后不关 page 等回车（调试用） |

### 6.3 路径约定

`BRAIN_BASE_PATH`：外部 Agent 可设此环境变量指向 brain-base 仓库根目录，方便在任意 cwd 下执行：
```bash
cd $BRAIN_BASE_PATH && python -m brain_base.cli ask "问题"
```

## 7. 内部架构（外部 Agent 应了解的最小集合）

### 7.1 LangGraph 7 子图

brain-base 所有业务逻辑落在 7 个 LangGraph StateGraph 子图上（T50: 原 IngestUrlGraph 删除，URL 入库走 ask 路径），通过 `BrainBaseGraph` 顶层按 `mode` 分发：

| 子图 | 职责 |
|------|------|
| **QaGraph** | 用户问答全流程（固化命中 → 意图 Agent-Loop → 检索 → 回答 → 自检 → 固化）；URL 入库同样由主路径 `fetch_url` 工具 + `qa_persist.write_raw_one` 完成 |
| **IngestFileGraph** | 本地文件入库（convert → frontmatter → doc_enrich → persist） |
| **PersistenceGraph** | 持久化管道（chunk → enrich → Milvus ingest） |
| **CrystallizeGraph** | 固化答案到整理层（hit_check → freshness_check / crystallize_write） |
| **GetInfoGraph** | 多步搜索循环（plan → search → classify → loop） |
| **LifecycleGraph** | 跨存储删除（resolve → scan → dry_run → delete → audit） |
| **LintGraph** | 固化层健康检查（scan → check → degrade → delete） |

### 7.2 QA 主流程（一句话版）

固化命中且新鲜 → 直接返回缓存答案。否则：normalize 改写 → decompose 分解子问题 → intent_planner/executor/observer 循环（LLM 自主调度工具搜索/抓取/检索）→ 证据汇聚 → 入库 → 重新检索 → judge → answer → self_check → 自动固化。

### 7.3 持久化三层

| 层 | 存储 | 作用 |
|---|---|---|
| **原始层** | `data/docs/raw/` + Milvus chunks | 不可变的原始证据 |
| **检索层** | Milvus hybrid（bge-m3 dense+sparse + bge-reranker） | 语义 + 关键词混合检索 |
| **固化层** | `data/crystallized/`（hot/cold 分层） | 高质量答案缓存，相似问题短路返回 |

### 7.4 source_priority 三档

| 优先级 | 标识 | 说明 |
|--------|------|------|
| **official-doc** | 官方文档 | 最高权重，优先召回 |
| **community** | 社区内容 | 中等权重 |
| **user-upload** | 用户上传 | 基础权重 |

## 8. 前置条件

调用前应确认：
1. Docker compose 已拉起 Milvus 三件套 + brain-base-worker（`docker compose up -d`）
2. `.env` 已配置 LLM key（`BB_LLM_API_KEY`）
3. `python -m brain_base.cli health` 三项 ok（Milvus / playwright / LLM）
4. 若走 ingest-file 路径：MinerU / pandoc 按需可用（Docker 容器内已预装）

## 9. 错误处理

| 情况 | 处理建议 |
|------|----------|
| `health` 显示 Milvus 不通 | `docker compose up -d` 重启 Milvus 三件套 |
| `ask` 报 "未配置 LLM" | 检查 `.env` 中 `BB_LLM_API_KEY` 是否已填 |
| `ask` 返回 exit code 1 | 看 stderr 日志定位具体节点失败原因 |
| 问题里带 URL 但返回空 `get_info_ingested` | URL 可能已被 `hash_lookup` SHA-256 去重短路（正常行为，不是错误） |
| `ingest-file` 返回 conversion_errors | 检查文件格式是否支持、MinerU 是否 OOM（16GB 显卡峰值 ~1.1GB） |
| `remove-doc` 忘记 `--confirm` | 默认 dry-run 只打清单不删，重新加 `--confirm` 执行 |
| `search` 返回空结果 | 知识库可能还没相关内容，先 `ask "<问题带 URL>"` 补库再搜 |

一句话总结：
**外部 Agent 应把 brain-base 当成「LangGraph 驱动的知识基础设施」来调，所有调用走 `python -m brain_base.cli`。固化是自动的，去重是自动的，你只需要关心 ask / search / ingest / remove-doc 四个动词。**
