---
name: knowledge-persistence
description: 当 get-info-agent 或 upload-agent 已拿到清洗/转换后的文档草稿，需要把知识工业级写入本地和检索层时触发。负责调用 bin/chunker.py 生成 chunks、LLM 信息富化（summary/keywords/questions）、raw/chunks 双落盘、Milvus hybrid 持久化。
disable-model-invocation: false
---

# Knowledge Persistence

## 1. 职责边界

本 skill 是**两条入口的共同下游**：

1. `get-info-workflow` 走完外部补库后把文档草稿交给本 skill（来源 = `official-doc` / `community`）。
2. `upload-ingest` workflow 走完本地格式转换后把文档草稿交给本 skill（来源 = `user-upload`）。

两条入口共享本 skill 下面的 chunk 生成、合成 QA、落盘、入库逻辑。

本 skill 负责：

1. 生成或接收 raw Markdown。
2. 调用 `bin/chunker.py` 生成 chunk Markdown。
3. 调用 `chunk-enrichment` skill 对每个 chunk 进行信息富化：生成 title/summary/keywords/3〜5 条合成 QA 问题（doc2query），写回 chunk frontmatter。
4. 确保 chunk Markdown 已写入。
5. 调用本仓 `bin/milvus-cli.py ingest-chunks` 完成 hybrid 入库（chunk 行 + 每条 question 一行）。

本 skill 不负责：

1. 外部网页抓取（那是 `web-research-ingest`）。
2. 本地文档格式转换（那是 `bin/doc-converter.py`）。
3. 搜索引擎调度。
4. 直接执行 QA 问答（那是 `qa-workflow` 的职责）。

## 2. 原始文档保存

raw 文档必须：

1. 保存到 `data/docs/raw/`。
2. 使用 UTF-8 编码。
3. 带 frontmatter（含 `content_sha256` 字段，见 2.1）。
4. 保留完整正文结构。
5. `doc_id` 必须带抓取日期，格式：`<topic-slug>-YYYY-MM-DD`。
6. raw 文件名必须与 `doc_id` 一致。

### 2.1 content_sha256 去重字段（P2-1）

每个 raw 文档的 frontmatter 必须包含 `content_sha256` 字段，值是正文（不含 frontmatter）的 SHA-256 十六进制摘要，用于入库前去重与事后审计：

1. **哈希范围**：raw Markdown 的 body（去除两个 `---` 围栏之间的 frontmatter 后剩下的全部正文）。
2. **计算时机**：在 frontmatter 组装完成、写入 `data/docs/raw/` 之前；入库前调用 `python bin/milvus-cli.py hash-lookup <sha256>` 查重。
3. **去重动作**：
   - `status: "hit"` → 跳过本次入库，复用已有 doc_id，并在返回里说明 `skipped_duplicate`。
   - `status: "miss"` → 继续入库，把 `content_sha256` 写入 frontmatter。
4. **边界**：
   - 换行统一按 LF 计算（`\r\n` / `\r` → `\n`），避免跨平台签名漂移。
   - **hash 前 `strip("\n")`** 去除首尾换行；frontmatter-body 分隔空行和文件末尾换行不计入内容。
   - 只 hash body，不 hash frontmatter——`fetched_at` 等字段在同内容不同时抓取时会变，全文 hash 会失去去重意义。
5. **历史数据迁移**：对不带此字段的旧 raw，运维可用 `python bin/milvus-cli.py backfill-hashes --dry-run` 先预览、再真跑补齐。
6. **定期体检**：`python bin/milvus-cli.py find-duplicates` 列出所有 hash 冲突组与 `hash_mismatch`（declared ≠ actual）情况。

### 2.2 raw frontmatter 模板

#### official-doc raw 模板

```yaml
---
doc_id: <topic-slug>-YYYY-MM-DD
title: <页面标题>
source_type: official-doc
source: <来源标识，如 anthropic-docs>
url: <单个页面 URL，不写站点首页>
fetched_at: YYYY-MM-DD
content_sha256: <正文 SHA-256>
keywords: ["关键词1", "关键词2"]
---
```

#### community raw 模板

```yaml
---
doc_id: <topic-slug>-YYYY-MM-DD
title: <页面标题>
source_type: community
source: <来源标识，如 community-blog>
url: <单个页面 URL>
fetched_at: YYYY-MM-DD
content_sha256: <正文 SHA-256>
keywords: ["关键词1", "关键词2"]
---
```

#### user-upload raw 模板

```yaml
---
doc_id: <topic-slug>-YYYY-MM-DD
title: <文档标题>
source_type: user-upload
source: user-upload
original_file: data/docs/uploads/<doc_id>/<原始文件名>
url:
fetched_at: YYYY-MM-DD
content_sha256: <正文 SHA-256>
keywords: ["关键词1", "关键词2"]
---
```

### 2.3 raw frontmatter 必填字段总结

| 字段 | 必填 | 说明 |
|------|------|------|
| `doc_id` | ✅ | 文档唯一标识，末尾带 `-YYYY-MM-DD` |
| `title` | ✅ | 页面/文档标题 |
| `source_type` | ✅ | `official-doc` / `community` / `user-upload` 三选一 |
| `source` | ✅ | 来源标识 |
| `url` | official-doc / community 必填 | 单个 URL 字符串；user-upload 留空 |
| `original_file` | user-upload 必填 | uploads/ 归档路径 |
| `fetched_at` | ✅ | ISO 日期 |
| `content_sha256` | ✅ | 正文 SHA-256，用于去重 |
| `keywords` | ✅ | JSON inline 数组，5〜10 个关键词 |

## 3. chunk 生成（只调用 bin/chunker.py）

```bash
python bin/chunker.py <raw_md_path> --output-dir data/docs/chunks
```

本 skill 不在提示词中描述切分策略。切分策略只维护在 `bin/chunker.py`，避免 LLM 读取冗余规则后误执行物理切分。

`bin/chunker.py` 输出基础 frontmatter（doc_id, chunk_id），enrichment 字段（title, summary, keywords, questions）由 `chunk-enrichment` skill 填充。

## 4. 分块文档保存

chunk 文档必须：

1. 保存到 `data/docs/chunks/`。
2. 与 raw 共享 `doc_id`。
3. 每块有唯一 `chunk_id`。
4. 可被 Grep 直接命中。
5. 文件名格式必须是 `<doc_id>-<chunk-index>.md`（建议使用 3 位序号，如 `001`）。
6. `chunk_id` 必须与 chunk 文件名（去掉 `.md`）一致。

### chunk frontmatter 模板

```markdown
---
doc_id: claude-code-subagent-2026-04-18
chunk_id: claude-code-subagent-2026-04-18-001
title: Claude Code Subagent 创建流程
section_path: Claude Code / Subagent / 创建
source: anthropic-docs
source_type: official-doc
url: https://docs.anthropic.com/...
fetched_at: 2026-04-18
summary: 简述本块讲了什么，便于 Grep 与排序
keywords: ["claude-code", "subagent", "创建", "frontmatter"]
questions: ["如何在 Claude Code 中创建 subagent?", "subagent 的 frontmatter 必填字段是什么?", "subagent 与 plugin 的关系?"]
---

# 正文 Markdown ...
```

`fetched_at` 必须是 ISO 日期（`YYYY-MM-DD`），记录**文档内容最后一次从源站抓取的日期**（不是入库日期，也不是文档本身的发布日期，尽管在初次入库时三者通常重合）。qa-workflow 的时效性分级依赖此字段；缺失时会退化到 doc_id 末尾的日期，但日期精度会丢失。

`questions` 字段是 **JSON inline 数组**（每个元素一个完整问题字符串）。这是 `bin/milvus-cli.py` 当前唯一支持的解析格式，避免引入 PyYAML 依赖。

### community 类型 chunk frontmatter 模板

当文档来源为非官方内容（博客、教程、问答帖等）时，frontmatter 必须使用以下扩展模板：

```markdown
---
doc_id: claude-code-subagent-community-2026-04-18
chunk_id: claude-code-subagent-community-2026-04-18-001
title: Claude Code Subagent 社区实践要点
section_path: Claude Code / Subagent / 社区实践
source: community-blog
source_type: community
url: https://blog.example.com/post-1
fetched_at: 2026-04-18
summary: 从社区博客提炼的 Subagent 实践要点
keywords: ["claude-code", "subagent", "社区实践", "经验总结"]
questions: ["社区中常见的 Subagent 配置陷阱有哪些?", "Subagent 与 Tool 的实际使用场景区别是什么?"]
---

# 正文 Markdown ...

> 来源: https://blog.example.com/post-1

知识点内容...
```

community 类型额外约束：

1. `source_type` 必须为 `community`（官方文档为 `official-doc`，用户上传为 `user-upload`）。
2. `url` 字段记录本 chunk 的来源 URL（**单个字符串**，不是数组）。一个 chunk 只来自一个 URL，不跨 URL 合并。
3. 正文中每个知识点前必须用 `> 来源: <url>` 标注出处。
4. **禁止将多个 URL 的内容合并为一个 chunk**——即使主题相同，每个 URL 也必须独立成文、独立建 chunk。
5. community 内容经过提炼（去营销、提取知识点），但**不跨信源整合**——整合归 crystallized 层。

### user-upload 类型 chunk frontmatter 模板

当文档来源为用户本地上传（走 `upload-agent` → `upload-ingest` workflow）时，frontmatter 必须使用以下扩展模板：

```markdown
---
doc_id: my-paper-2026-04-19
chunk_id: my-paper-2026-04-19-001
title: 我的论文 / 第一章 引言
section_path: 用户文档 / 论文 / 第一章
source: user-upload
source_type: user-upload
original_file: data/docs/uploads/my-paper-2026-04-19/my-paper.pdf
url:
fetched_at: 2026-04-19
summary: 简述本块讲了什么，便于 Grep 与排序
keywords: ["深度学习", "模型压缩", "知识蒸馏"]
questions: ["知识蒸馏的基本思路是什么?", "学生模型需要多大才足够?", "温度参数如何影响蒸馏效果?"]
---

# 正文 Markdown ...
```

user-upload 类型额外约束：

1. `source_type` 必须为 `user-upload`（与 `official-doc` / `community` 并列，是第三种合法值）。
2. `original_file` 字段记录原始文件在 `data/docs/uploads/<doc_id>/` 下的归档路径。该字段通过 Milvus `enable_dynamic_field=True`（已开启）自动写入，无需 schema 迁移。
3. `url` 字段冒号后留空（不要写 `""`——当前 frontmatter 解析器不去引号，会写入字面量 `""` 字符串）；schema 字段存在即可。
4. 不需要 `url` 字段（那是 official-doc / community 类型专用）。
5. 不要在正文里手写"> 来源: ..."标注（那是 community 类型专用；用户上传的溯源依靠 `original_file` 字段回指归档文件）。
6. `keywords` 可以在首次入库时留空或由 `upload-agent` 基于正文粗提取；后续可由 `organize-agent` 在固化过程中完善。
7. `fetched_at` 填用户上传的日期（通常和 `doc_id` 末尾日期一致）。如果用户提供了更精确的原文档日期（例如 PDF 元数据中的 `CreationDate`），应以原文档日期为准。

### frontmatter 必填字段总结

| 字段 | 必填 | 说明 |
|------|------|------|
| `doc_id` | ✅ | 文档唯一标识，末尾带 `-YYYY-MM-DD` |
| `chunk_id` | ✅ | `<doc_id>-<NNN>` |
| `title` | ✅ | chunk 标题 |
| `section_path` | ✅ | 章节路径 |
| `source` | ✅ | 来源标识，例 `anthropic-docs` / `user-upload` |
| `source_type` | ✅ | `official-doc` / `community` / `user-upload` 三选一 |
| `fetched_at` | ✅ | **新増（P1-4）**：ISO 日期，qa-workflow 时效性分级的主键 |
| `summary` | ✅ | 一行简述 |
| `keywords` | ✅ | 逗号分隔关键词 |
| `questions` | ✅ | JSON inline 数组的合成 QA |
| `url` | 含 official-doc / community 必填 | 单个 URL 字符串；user-upload 留空 |
| `original_file` | user-upload 必填 | uploads/ 归档路径 |

## 5. 信息富化（chunk-enrichment skill）

enrichment 规则已独立为 `chunk-enrichment` skill，本 skill 不再内联描述。详见 `skills/chunk-enrichment/SKILL.md`。

### 5.1 触发时机

`bin/chunker.py` 生成基础 chunk 文件后，**先调用 `chunk-enrichment` 写回 enrichment frontmatter，再入库**。这样入库时 frontmatter 里已经有完整 `title` / `summary` / `keywords` / `questions` 字段，CLI 会自动为每个问题写入一行向量。

### 5.2 已有 chunk 时的 enrichment 检查

如果入库前发现 chunk 文件已存在（例如手动跑过 `chunker.py`），必须先检查 enrichment 是否完整：

1. 读取每个 chunk 的 frontmatter。
2. 如果任一 chunk 的 `title` / `summary` / `keywords` / `questions` 为空，触发 `chunk-enrichment` 补填。
3. enrichment 完成后再入库。

也可通过 CLI 独立触发：`python bin/brain-base-cli.py enrich-chunks --doc-id <doc_id>`。

### 5.3 入库顺序硬约束

1. **写 raw → 调 chunker.py 生成 chunk → 调 chunk-enrichment 填充 frontmatter → 调 CLI 入库**。
2. 不允许先入库再回填 questions（那会让 question 行漏掉）。
3. 不允许跳过 enrichment 直接入库（除非该 chunk 是空目录页，且明确写 `questions: []`）。

## 6. Milvus 持久化（默认 hybrid，bge-m3）

Milvus 层要求：

1. 禁止使用伪造向量。
2. 必须使用能返回 embedding 的 provider。
3. **当前默认 provider 为 `bge-m3`**，对应 `KB_RETRIEVAL_MODE=hybrid`，会同时写入 dense 与 sparse 向量。
4. 入库前必须执行 `python bin/milvus-cli.py check-runtime --require-local-model --smoke-test`，确认 bge-m3 模型可用。
5. 调用 `python bin/milvus-cli.py ingest-chunks --chunk-pattern "data/docs/chunks/*.md"` 完成入库；CLI 会：
   - 为每个 chunk 写入 1 行 `kind=chunk`（向量来自 chunk 正文）。
   - 为每条 `questions[i]` 额外写入 1 行 `kind=question`，`question_id=<chunk_id>-q<NN>`，`chunk_id` 仍指向父 chunk（向量来自问题文本本身）。
6. 切换 provider（例如从 `bge-m3` 切回 `sentence-transformer`）必须先 drop 旧 collection 再重新入库；CLI 会在 dim 或 schema 不一致时 fail-fast 而不是静默写脏数据。
7. 所有交互式检索、健康检查与批量入库都统一通过本仓 `milvus-cli.py` 完成。

## 7. 失败策略

1. embedding provider 未配置时直接报错。
2. raw 落盘失败、chunk 落盘失败、合成 QA 生成失败、Milvus 入库失败、SQLite 更新失败都要单独报错。
3. 任一步骤失败，不得宣称"持久化完成"。
4. 合成 QA 生成失败时，允许把对应 chunk 的 `questions` 字段写空数组并继续入库（chunk 行还能正常召回），但必须在返回上下文里明确报告"合成 QA 失败的 chunk 列表"。
