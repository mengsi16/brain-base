---
name: brain-base
description: 任何需要问答或把本地文档入库的场景，默认先调用 brain-base skill。
disable-model-invocation: false
---

# brain-base

本 skill 是 **brain-base 知识库 Agent 的外部调用说明书**，部署在 `~/.claude/skills` 或 `~/.codex/skills`，供任何 Claude Code / Codex Agent 读取。它不执行检索本身，而是告诉调用方：

1. 什么时候该调知识库 Agent（问答 vs 上传）
2. 怎么拼 `claude -p` 命令行
3. 为什么必须带 `--dangerously-skip-permissions`
4. Prompt 怎么写效果最好
5. 输出长什么样、怎么解读
6. **固化反馈怎么发**（`-c` 参数）
7. 出错了怎么排查

## 1. 适用场景

brain-base 对外暴露**两个并列入口 Agent**，调用方按意图选择其一：

### 1.1 问答入口（`qa-agent`）

满足任一即触发：

1. 需要查询个人知识库中已有的文档、配置、流程、概念。
2. 需要验证某个事实是否在知识库中有记录。
3. 需要检索与某主题相关的已有 chunk 或 raw 文档。
4. 用户或上层 Agent 明确要求"先查本地知识库"。
5. 需要基于已有知识做方案比较、流程说明、术语解释。

### 1.2 上传入库入口（`upload-agent`）

满足任一即触发：

1. 调用方手上有**本地文件**（PDF / DOCX / PPTX / XLSX / LaTeX / TXT / MD / PNG / JPG），希望把它的内容沉淀进 brain-base 供后续 qa-agent 检索。
2. 用户或上层 Agent 明确要求"把这份文档导入 / 加入 / 上传到知识库"。
3. 需要批量把一个目录下的多份本地文档一次性入库。

**关键区别**：

| 输入形态 | 入库意图 | 应调用 |
|---------|---------|--------|
| 本地文件路径 | 有 | `upload-agent` |
| URL / 检索主题 | 有 | `qa-agent`（它会在证据不足时自动触发 `get-info-agent` 联网补库） |
| 任何 | 无，只想检索 | `qa-agent` |

### 1.3 不适用场景

1. 纯闲聊或与知识库无关的请求。
2. 用户只要求阅读 / 总结本地文件但**不入库** → 不触发 brain-base，直接回答即可。
3. 用户明确要求直接联网搜索而不经过知识库 → 不触发 brain-base。

## 2. 调用方式

### 2.1 问答调用（qa-agent）

```bash
claude -p "<问题内容>" \
  --plugin-dir "<BRAIN_BASE_PATH>" \
  --agent brain-base:qa-agent \
  --dangerously-skip-permissions
```

### 2.2 上传入库调用（upload-agent）

```bash
claude -p "<上传指令，含文件路径>" \
  --plugin-dir "<BRAIN_BASE_PATH>" \
  --agent brain-base:upload-agent \
  --dangerously-skip-permissions
```

Prompt 里必须包含**具体的本地文件路径**（绝对或相对于调用方工作目录的相对路径都可以），示例：

```bash
claude -p "请把以下本地文件入库：/home/me/papers/knowledge-distillation.pdf" \
  --plugin-dir "$BRAIN_BASE_PATH" \
  --agent brain-base:upload-agent \
  --dangerously-skip-permissions
```

批量：

```bash
claude -p "请把目录 /home/me/papers/ 下所有 PDF 入库" \
  --plugin-dir "$BRAIN_BASE_PATH" \
  --agent brain-base:upload-agent \
  --dangerously-skip-permissions
```

### 2.3 参数说明

| 参数 | 必填 | 说明 |
|------|------|------|
| `-p` | ✅ | 传给 agent 的 prompt。qa-agent 收的是问题，upload-agent 收的是上传指令（含文件路径） |
| `-c` | 固化反馈时必填 | 继续上一次对话（continue），用于发送固化反馈（**仅 qa-agent 需要**） |
| `--plugin-dir` | ✅ | brain-base 项目的**绝对路径**（含 `.claude-plugin/` 的目录） |
| `--agent` | ✅ | `brain-base:qa-agent`（问答）或 `brain-base:upload-agent`（上传入库） |
| `--dangerously-skip-permissions` | ✅ | **必须携带**，原因见下方 |

### 如何确定 brain-base 路径

当 brain-base 作为**项目级项目**（而非安装在 `~/.claude/plugins/`）时，调用方需要知道 brain-base 的绝对路径。推荐以下方案：

#### 方案 A：环境变量（推荐）

调用方项目在 `.env` 或启动脚本中设置：

```bash
export BRAIN_BASE_PATH="/absolute/path/to/brain-base"
```

然后调用时引用：

```bash
claude -p "问题" \
  --plugin-dir "$BRAIN_BASE_PATH" \
  --agent brain-base:qa-agent \
  --dangerously-skip-permissions
```

#### 方案 B：相对路径约定

如果调用方项目与 brain-base 有固定的目录关系（如父子目录），可使用相对路径：

```bash
# 假设目录结构：
# ~/projects/
#   ├── brain-base/
#   └── caller-project/

claude -p "问题" \
  --plugin-dir "../brain-base" \
  --agent brain-base:qa-agent \
  --dangerously-skip-permissions
```

#### 方案 C：通过 Claude Code 查找

如果 brain-base 已作为 plugin 安装到 Claude Code，可以让 Claude Code 帮你找：

```bash
# 在 Claude Code 中询问：
# "brain-base 插件安装在哪里？帮我返回它的绝对路径"
```

然后使用该路径进行调用。

### 为什么必须 `--dangerously-skip-permissions`

两个入口 Agent 在执行时都会触发文件系统与子进程调用：

- **qa-agent** 可能触发 `get-info-agent`，后者会：
  1. 启动 Playwright 浏览器抓取网页 → 需要文件写入权限
  2. 调用 `milvus-cli.py ingest-chunks` → 需要执行 Python 脚本权限
  3. 写入 `data/docs/raw/` 和 `data/docs/chunks/` → 需要文件创建权限
- **upload-agent** 会：
  1. 调用 `bin/doc-converter.py` → 执行 Python + 可能拉起 MinerU / pandoc 子进程
  2. 归档原始文件到 `data/docs/uploads/<doc_id>/` → 文件复制权限
  3. 写 `data/docs/raw/` 和 `data/docs/chunks/` → 文件创建权限
  4. 调用 `milvus-cli.py ingest-chunks` → 执行 Python 脚本权限

如果**不带** `--dangerously-skip-permissions`，Claude Code 会在每一步弹出权限确认对话框，导致：

- 作为子进程被其他 Agent 调用时，无人响应确认 → 进程挂起或直接退出
- 即使有人值守，频繁弹窗也会打断自动化流程

因此，**从外部 Agent 调起 brain-base 的任一入口时都必须跳过权限确认**。

## 3. Prompt 构造指南

### 3.1 qa-agent 的 prompt（问答）

qa-agent 接收的就是 `-p` 后面的字符串。为了让它高效工作，调用方应遵循以下格式：

```
## 问题
<用户的核心问题，一句话>

## 背景（可选）
<为什么问这个问题、当前在做什么任务>

## 时效要求（可选）
<是否需要最新资料、是否有版本约束>
```

示例：

```bash
claude -p "## 问题
Claude Code 的 subagent 怎么配置？yaml frontmatter 有哪些必填字段？

## 背景
我正在给一个 Claude Code 插件项目写 agent 定义文件，需要确认 subagent 的规范格式。

## 时效要求
需要 2025 年 4 月之后的资料" \
  --plugin-dir "<BRAIN_BASE_PATH>" \
  --agent brain-base:qa-agent \
  --dangerously-skip-permissions
```

qa-agent Prompt 硬约束：

1. **问题必须清晰**：qa-agent 会基于问题做 L0-L3 fan-out 改写，问题越清晰改写越精准。
2. **不要在 prompt 里塞检索指令**：qa-agent 自己会决定用 Grep 还是 multi-query-search，调用方不需要指定。
3. **不要在 prompt 里要求跳过补库**：如果本地证据不足，qa-agent 有权自动触发 get-info-agent 补库，这是设计意图。
4. **如果只需要纯检索不需要补库**，在 prompt 里注明"仅检索本地已有资料，不需要联网补库"即可，qa-agent 会尊重此约束。

### 3.2 upload-agent 的 prompt（上传入库）

upload-agent 接收的是**上传指令**，最关键的信息是**具体文件路径**。推荐格式：

```
## 任务
把以下本地文档入库到 brain-base。

## 文件路径
- /absolute/path/to/file1.pdf
- /absolute/path/to/file2.docx
（或给出目录：/absolute/path/to/folder/）

## 可选元信息
- 主题 slug: <自定义 slug，会进 doc_id>
- section_path: 用户文档 / 论文 / 第一章
- keywords: a, b, c
```

示例（单文件）：

```bash
claude -p "## 任务
把以下本地文档入库到 brain-base。

## 文件路径
- /home/me/papers/knowledge-distillation.pdf

## 可选元信息
- 主题 slug: kd-hinton-2015
- section_path: 用户文档 / 论文 / 知识蒸馏" \
  --plugin-dir "$BRAIN_BASE_PATH" \
  --agent brain-base:upload-agent \
  --dangerously-skip-permissions
```

示例（目录批量）：

```bash
claude -p "把目录 /home/me/papers/ 下所有 PDF 入库，section_path 统一用'用户文档 / 论文'" \
  --plugin-dir "$BRAIN_BASE_PATH" \
  --agent brain-base:upload-agent \
  --dangerously-skip-permissions
```

upload-agent Prompt 硬约束：

1. **文件路径必须明确**：upload-agent 不会猜测路径；相对路径会相对于 `--plugin-dir`（即 brain-base 仓库根）解析。**建议用绝对路径**。
2. **不要塞 URL**：URL 类请求必须走 qa-agent（它会按需触发 get-info-agent 联网补库），upload-agent 只处理本地文件。
3. **不要要求它跳过格式转换**：upload-agent 必须走 `doc-converter.py`——这是唯一保证 frontmatter、doc_id、归档、分块一致的路径。
4. **支持格式**：`.pdf` `.docx` `.pptx` `.xlsx` `.png` `.jpg` `.jpeg` `.tex` `.txt` `.md` `.markdown`。不支持 `.doc` / `.rtf` / `.epub` / `.html` / `.ppt` / `.xls`——请先另存为支持的格式。
5. **首次运行会下载约 2GB MinerU 模型**到 `~/.cache`，仅限 PDF/DOCX/PPTX/XLSX/图片；纯 TXT/MD 不受影响。`.tex` 需要系统装 pandoc。

## 4. 输出解读

### 4.1 qa-agent 的输出

qa-agent 的输出是标准 Markdown 文本，结构通常为：

1. **简要答案**：直接回答问题
2. **关键依据**：引用来源文件路径（`data/docs/chunks/...` 或 `data/docs/raw/...`）
3. **资料来源说明**：标注来自本地知识库还是新抓取资料
4. **限制与待确认项**：如有
5. **📦 固化标注**（如有）：答案来自自进化整理层，格式为 `📦 来自自进化整理层固化答案（skill_id: ..., revision: N, 最后确认 YYYY-MM-DD）`

调用方应将 qa-agent 的 stdout 输出直接作为知识库检索结果使用。

### 4.2 upload-agent 的输出

upload-agent 的输出也是 Markdown 报告，结构通常为：

1. **每个入库文档**：`doc_id` / raw MD 路径 / 原始文件归档路径 / chunks 路径列表
2. **入库计数**：`chunk_rows` / `question_rows`（来自 `ingest-chunks` 报告）
3. **失败文件**：输入路径 + 失败阶段（格式检测 / 格式转换 / 分块 / 入库）+ 错误信息
4. **总结**：成功 / 失败数量

成功后的文档可立即被 qa-agent 检索到（frontmatter `source_type: user-upload`）。

调用方应抓住返回报告的 `doc_id` 列表供后续追溯或检索参考（比如后续问 qa-agent 问题时可以显式提及“请优先从 doc_id=<x> 中找答案”）。

## 4.5 固化反馈（`-c` 参数、仅 qa-agent）

> upload-agent 不涉及固化层，**无需** `-c` 反馈。上传成功后下一次 qa-agent 检索到这些文档的答案才会经历正常的 📦 固化路径。

qa-agent 在完成一次满意问答后，会自动委托 organize-agent 将答案固化到自进化整理层（`data/crystallized/`），初始状态为 `pending`。**调用方必须在拿到答案后发送固化反馈**，否则该固化答案永远停留在 `pending` 状态，无法被后续查询命中复用。

### 反馈判断规则

调用方（通常是上层 Agent 的 LLM）根据用户反应判断反馈类型：

| 用户表现 | 反馈类型 | 说明 |
|------|------|------|
| 用户未否定、继续追问相关细节、表示满意 | `confirmed` | **默认行为**：只要用户没有明确否定，就发 confirmed |
| 用户明确否定（"不对" / "不满意" / "过时了"） | `rejected` | 固化答案将被标记为 rejected，下次不再命中 |
| 用户主动补充新信息 | `supplement` | 固化答案保留，pitfalls 追加遗漏项 |

### 发送反馈的命令

在第一次调用（获取答案）之后，**同一对话**中用 `-c` 继续发送反馈：

```bash
# 确认固化（最常见）
claude -p -c "用户未否定，确认固化上一轮答案" \
  --plugin-dir "<BRAIN_BASE_PATH>" \
  --agent brain-base:qa-agent \
  --dangerously-skip-permissions

# 拒绝固化
claude -p -c "用户明确否定上一轮答案，拒绝固化" \
  --plugin-dir "<BRAIN_BASE_PATH>" \
  --agent brain-base:qa-agent \
  --dangerously-skip-permissions

# 补充信息
claude -p -c "用户补充：<补充内容摘要>，更新固化答案" \
  --plugin-dir "<BRAIN_BASE_PATH>" \
  --agent brain-base:qa-agent \
  --dangerously-skip-permissions
```

### `-p -c` 可以一起用吗？

**可以**。`-c` 是 continue（继续上一次对话），`-p -c "xxx"` 表示在非交互模式下给上次对话发一条后续消息。qa-agent 会识别这条消息为对上一轮答案的反馈，并委托 organize-agent 更新 `user_feedback` 状态。

### 典型调用流程

```
步骤1: claude -p "问题" --plugin-dir ... --agent qa-agent --dangerously-skip-permissions
        → 拿到答案（可能含📦固化标注）

步骤2: 判断用户反应
        → 用户未否定（默认）:
           claude -p -c "用户未否定，确认固化" --plugin-dir ... --agent qa-agent --dangerously-skip-permissions
        → 用户否定:
           claude -p -c "用户否定，拒绝固化" --plugin-dir ... --agent qa-agent --dangerously-skip-permissions

步骤3: 继续执行其他任务
```

### 不发反馈会怎样？

固化答案停留在 `pending` 状态。`pending` 状态的答案**仍可被命中返回**，但不会自动转为 `confirmed`。长期停留在 `pending` 的答案在 `crystallize-lint` 清理时不会被删除，但无法获得 `last_confirmed_at` 的时间更新，影响新鲜度判断的准确性。

## 5. 前置条件

调用本 skill 前应确认：

1. **Milvus 正在运行**（两个入口都需要）：`docker ps | grep milvus`，若未启动则先 `docker compose up -d`
2. **bge-m3 模型可用**（两个入口都需要）：`python bin/milvus-cli.py check-runtime --require-local-model --smoke-test`
3. **brain-base 路径正确**：`--plugin-dir` 指向的目录下必须存在 `.claude-plugin/plugin.json`
4. **（仅 upload-agent）MinerU / pandoc 按需可用**：`python bin/doc-converter.py check-runtime`。规则：
   - 处理 PDF / DOCX / PPTX / XLSX / 图片 → MinerU 必须可用（`pip install 'mineru[pipeline]>=3.1,<4.0'`，首次会下载约 2GB 模型）
   - 处理 `.tex` → pandoc 必须在 PATH。
   - 处理 `.txt` / `.md` → 无额外依赖

若前置条件不满足：

- qa-agent 仍可运行（降级为纯文件系统 Grep），但检索质量会显著下降。
- upload-agent 在需要的后端缺失时会 fail-fast并在报告里明确列出缺失工具与安装命令。

## 6. 与其他 skill 的关系

```
外部 Agent
  ├─ 入口A（问答）：claude -p "问题" --agent qa-agent --dangerously-skip-permissions
  │    ├─ qa-workflow（检索 + 证据判断 + 回答）
  │    ├─ get-info-agent（仅在本地证据不足时自动触发）
  │    │    ├─ get-info-workflow
  │    │    ├─ web-research-ingest
  │    │    ├─ playwright-cli-ops
  │    │    ├─ knowledge-persistence   ◄ 共享下游
  │    │    └─ update-priority
  │    └─ organize-agent（问答成功后自动固化，初始 pending）
  │         ├─ crystallize-workflow
  │         └─ crystallize-lint
  │
  ├─ 入口B（上传入库）：claude -p "上传指令" --agent upload-agent --dangerously-skip-permissions
  │    ├─ upload-ingest（用户文档入库 workflow）
  │    │    ├─ doc-converter（bin/doc-converter.py：MinerU/pandoc 转 MD）
  │    │    └─ knowledge-persistence   ◄ 共享下游、入库与A路径一致
  │    └─ （不触发 organize-agent；上传不参与固化）
  │
  └─ 固化反馈（仅对 入口A 有效）：claude -p -c "反馈" --agent qa-agent --dangerously-skip-permissions
       └─ qa-agent 识别反馈 → organize-agent 更新 user_feedback
```

两条路径在 `knowledge-persistence` 汇合——无论网页补库还是用户上传，分块 / 合成 QA / Milvus 入库完全一致。

本 skill **不替代** `qa-workflow` / `upload-ingest`，而是作为外部 Agent 调用知识库的唯一入口。知识库内部的 Agent 之间仍走 `Agent` tool 直接委托。

## 7. 错误处理

### 7.1 通用（两个入口均适用）

| 情况 | 处理 |
|------|------|
| 进程退出码非 0 | 检查 Milvus 是否运行、`--plugin-dir` 是否正确 |
| 输出为空 | 可能是 prompt 过长或格式异常，缩短重试 |
| 进程挂起超时 | 可能是模型首载 / MinerU 首次下载 / Playwright 弹窗 / Milvus 连接超时，检查 Docker 与网络 |

### 7.2 qa-agent 独有

| 情况 | 处理 |
|------|------|
| 输出含"证据不足" | qa-agent 判定本地无相关资料且未触发补库（可能 prompt 里要求了不补库） |
| 输出含📦固化标注 | 正常行为，答案来自自进化整理层；记得发 `-c` 反馈 |
| `-c` 反馈后输出含"已更新" | 正常，organize-agent 已处理反馈 |
| `-c` 反馈后输出含"无上一轮固化" | 上一轮答案未被固化（可能是一次性问题或证据不足），无需反馈 |

### 7.3 upload-agent 独有

| 情况 | 处理 |
|------|------|
| 报告异常含 `mineru not found` / `未找到 mineru 可执行文件` | 安装 MinerU：`pip install 'mineru[pipeline]>=3.1,<4.0'` |
| 报告异常含 `pandoc not found` | 系统安装 pandoc（`choco install pandoc` / `brew install pandoc` / `apt install pandoc`）；仅 `.tex` 转换需要 |
| 报告异常含 `不支持的文件格式` | 文件扩展名不在支持列表（`.pdf/.docx/.pptx/.xlsx/.png/.jpg/.jpeg/.tex/.txt/.md`）——先另存为支持格式 |
| 报告含 `raw 文件已存在` | 已有相同 `doc_id` 的旧文档；重新调用时在 prompt 里要求 `overwrite` 或指定不同的 slug |
| 报告含 `ingest-chunks 失败` | Milvus 不健康 / bge-m3 模型未就绪；跑 `check-runtime --require-local-model --smoke-test` |
| 报告显示部分文件成功、部分失败 | 成功文件已入库可用；失败文件单独重试即可，不会影响已成功的 doc_id |
