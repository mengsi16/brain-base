# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

# 项目变更记录（2026-04-18）

这一节只记录"遇到什么问题 / 根因 / 怎么解决"，供后续会话快速定位，不替换上面的通用准则。

## 问题 1：默认 provider 是 `sentence-transformer`（dense 384 维），检索能力弱且没有 sparse

**根因**：最初的 `milvus_config.py` 默认 `KB_EMBEDDING_PROVIDER=sentence-transformer`、`KB_RETRIEVAL_MODE=dense`，`all-MiniLM-L6-v2` 在中英混合语料下召回明显偏弱，而且完全没有 sparse/词级通道。

**解决**：把默认 provider 切到 `bge-m3`，`KB_RETRIEVAL_MODE` 留空由 provider 自动推断（bge-m3 → hybrid；其他 → dense）。同步改 `@e:\PostGraduate\Project\plan-for-all\knowledge-base\bin\milvus_config.py`、`@e:\PostGraduate\Project\plan-for-all\knowledge-base\.env.example`、`@e:\PostGraduate\Project\plan-for-all\knowledge-base\README.md`、`@e:\PostGraduate\Project\plan-for-all\knowledge-base\OPERATIONS_MANUAL.md`。

## 问题 2：CLI 直接拒绝 hybrid，`ingest-chunks` 根本走不到 sparse 分支

**根因**：`ingest-chunks` 里有一段硬错误 "当前仅支持 dense 模式入库"——这是老代码占位，但把 BGE-M3 的 dense+sparse 能力完全屏蔽了。这是真实 bug，不是配置问题。

**解决**：在 `@e:\PostGraduate\Project\plan-for-all\knowledge-base\bin\milvus-cli.py` 里：

- `ensure_dense_collection` 改名 `ensure_collection`，加 `include_sparse` 参数；`include_sparse=True` 时建 `SPARSE_FLOAT_VECTOR` 字段 + `SPARSE_INVERTED_INDEX` 索引
- 在 collection 已存在时校验 dense dim 与 sparse 字段是否匹配，不匹配 fail-fast（避免 provider 切换后静默写脏数据）
- `ingest-chunks` 删掉硬错误，按 `runtime["mode"]` 自动走 hybrid / dense

## 问题 3：`pymilvus.exceptions.ParamError: expect 1 row`（row-level insert 失败）

**根因**：BGE-M3 的 `encode_documents()` 返回 `scipy.sparse.csr_matrix`，形状是 `(n, vocab)`。我原先写 `sparse_embeddings[i]` 切片得到的是 **2D `(1, vocab)` 子矩阵**，但 pymilvus 对 `SPARSE_FLOAT_VECTOR` 的 row-level insert 期望"每行是代表 1 行的稀疏值"，2D 形状它识别不了，直接抛 `expect 1 row`。

**解决**：把 sparse 输出统一转成 `dict[int, float]`——pymilvus 对 SPARSE_FLOAT_VECTOR 的 insert/search 都原生支持 dict 形式，且跨 scipy 版本 / matrix vs array 子类稳定。

修改在 `@e:\PostGraduate\Project\plan-for-all\knowledge-base\bin\milvus-cli.py:149-239`：

- 新增 `_sparse_matrix_to_row_dicts`：一次 `tocoo()` 遍历整个 `(n, vocab)` 矩阵，按 `coo.row` 分桶到 n 个 dict，**O(nnz) 线性时间**
- 新增 `_single_sparse_to_dict`：兜底处理 dict / 1D csr_array / `(1, vocab)` 2D 切片 / `indices+data` 四种形状
- `_encode_documents` 和 `_encode_query` 统一走 dict 路径

**教训**：pymilvus row-level insert 的"每行字段值必须是 single-row shape"约束是隐式的，scipy 切片看起来"是单行"但实际 shape 仍是 `(1, n)`，要显式转成 dict 或 1D 结构。

## 问题 4：用户口语 query 与文档术语之间词汇鸿沟大，dense-only 召回不稳

**根因**：用户问"怎么配置 subagent"，文档里写的是 "Claude Code subagent YAML frontmatter requires ..."——dense 向量能部分桥接，但缩写/中英混合/口语化时仍经常漏召回。

**解决**（两层同时做）：

- **索引侧（doc2query）**：每个 chunk 落盘前由 LLM 生成 3〜5 条用户口吻的合成问题，写入 frontmatter `questions: [...]`。`ingest-chunks` 为每条 question 单独入库（`kind=question`，`chunk_id` 指向父 chunk），显著降低词汇鸿沟
- **查询侧（fan-out）**：`qa-workflow` 步骤 2 改成 L0〜L3 分层改写（L0 原句 / L1 规范化 / L2 意图增强 / L3 HyDE 假答）；新增 CLI 子命令 `multi-query-search`，一次性并发检索所有变体 → RRF 合并 → 按 `chunk_id` 去重（question 行自动折叠回父 chunk）

## 问题 5：短 Markdown 被过度切分

**根因**：原先的 skill 文案对"分块"只说"按 Markdown 语义边界切"，没写长度阈值，导致 agent 把 500 字的短笔记也切成 3 块，每块背景不全。

**解决**：在 `@e:\PostGraduate\Project\plan-for-all\knowledge-base\skills\knowledge-persistence\SKILL.md` 和 `agents/get-info-agent.md` 里加 **5000 字符硬阈值**：
- 正文 ≤ 5000 字符 → 整篇 1 块，不切
- 正文 > 5000 字符 → 按语义切，每块上限 5000 字符

## 破坏性操作补充：`drop-collection --confirm`

切换 provider（dense dim 或 schema 字段变化）后必须 drop 旧 collection 再重新 ingest。新增 CLI 子命令 `python bin/milvus-cli.py drop-collection --confirm`，**必须显式 `--confirm` 才真删**，避免误操作。

---

## 下次遇到"ingest 失败 / 检索不对"时的快速检查顺序

1. `docker compose ps` 确认 Milvus standalone 是 `(healthy)`
2. `python bin/milvus-cli.py check-runtime --require-local-model --smoke-test` 看 `dense_dim` / `sparse_nnz` / `resolved_mode` 是否符合预期
3. `python bin/milvus-cli.py inspect-config` 确认 `embedding_provider` 和 `output_fields` 里含 `kind` / `question_id`
4. 如果报 "dense dim 不匹配" 或 "缺少 sparse 字段" → `python bin/milvus-cli.py drop-collection --confirm` 然后重 ingest
5. 如果报 "expect 1 row" / "invalid input for sparse float vector" → 检查 `_sparse_matrix_to_row_dicts` 是否被绕过，sparse 值必须是 `dict[int, float]`

---

## 问题 6：`extracted_urls` SQLite 表与 chunk frontmatter 完全冗余，且配置字段悬空引用

**根因**：为了支持 get-info "非官方来源（博客、教程、问答帖）的有用内容提炼入库"，早期新增了一张 `extracted_urls` 表（url, source_title, source_author, source_type, doc_id, chunk_id, topic, extracted_at）来记录提炼来源 URL。但这张表里每一个字段，提炼文档的 chunk frontmatter（`urls` 数组）和正文（`> 来源: <url>` 标注）里都已经存在一份——本质上是**文件系统已有信息的 SQL 镜像**。同时 `knowledge-persistence` 和 `update-priority` 两个 skill 都声称要写这张表，**职责重叠**；`get-info-workflow` 步骤 6.1 还引用了 `priority.json.official_domains` 字段，但这个字段**从未在 schema 里定义过**，属于悬空引用。

**解决**（一次性收敛）：

- 删除 `extracted_urls` 表，溯源完全交给文件系统：`urls: ["...", "..."]` 在 frontmatter + `> 来源: <url>` 在正文 + grep/file-read 查溯源
- 清理 `@e:\PostGraduate\Project\plan-for-all\knowledge-base\bin\scheduler-cli.py`：删除建表逻辑、`record_extracted_url()` / `query_extracted_urls()`、`record-url` / `query-urls` CLI 子命令
- 清理 `@e:\PostGraduate\Project\plan-for-all\knowledge-base\skills\knowledge-persistence\SKILL.md` 职责第 7 条（extracted_urls 写入）
- 清理 `@e:\PostGraduate\Project\plan-for-all\knowledge-base\skills\update-priority\SKILL.md` 原职责第 4 条 + 步骤 3 第 4 条 + 步骤 4 第 5 条（全部 extracted_urls 相关）
- 补齐 `official_domains` 字段：在 `@e:\PostGraduate\Project\plan-for-all\knowledge-base\README.md` 把 `priority.json` 示例升级到 `version: 1.1.0`，新增 `official_domains` 数组字段及字段说明
- 改写 `@e:\PostGraduate\Project\plan-for-all\knowledge-base\skills\get-info-workflow\SKILL.md` 步骤 6.1 为**"白名单 + LLM" 两级判定**：先查 `official_domains` 快路径（O(1) 域名匹配），未命中时交 LLM 输出四分类 `official-high` / `official-low` / `community` / `discard`
- `update-priority` 新增步骤 5：LLM 判为**高置信度**官方的新域名由其幂等回填 `official_domains`，形成**自学习闭环**；`official-low` 不回填（避免污染）
- **Milvus schema 完全未动**（`url: VARCHAR(2048)` 保持单数字符串）：官方文档 frontmatter 继续用 `url`（单数），提炼文档用 `urls`（数组），下游解析兼容两种格式，避免 drop collection + 全量重新入库的迁移代价

**教训**：

- 新增数据结构前必须先问一句"这些信息是不是已经在别处了"——frontmatter + 文件系统本身就是天然的可 grep 索引，不要无脑镜像到 SQL
- 职责分配要单一：`keywords.db` / `priority.json` 的写入统一归 `update-priority`，`knowledge-persistence` 不能越界；两个 skill 都声称写同一张表是架构坏味道
- 引用配置字段前要先定义：`official_domains` 被 skill 文本引用时 schema 里并没有这个字段，是悬空引用，应当和 schema 定义一起提交
- 收敛设计时计划外的发现要直接改计划：本次把原计划"步骤 4（删除旧字段）+ 步骤 7（新增 official_domains 回填）"合并为一次 `multi_edit`，更清晰；原计划"步骤 9 更新 get-info-agent"复核后发现该 agent 文本早已和新设计一致，直接标记"无需变更"

收敛完整记录见 `@e:\PostGraduate\Project\plan-for-all\knowledge-base\planning\2026-04-18-cleanup-extraction-workflow.md`。

---

## 问题 7：RAG 每次问答都要重跑检索与综合，成功回答过的问题不积累

**根因**：当前知识库只有两层——原始层（raw / chunks / Milvus）与 Schema 层（agents / skills）。每一次用户问答都要跑完整的 L0〜L3 fan-out + multi-query-search + 证据综合链路，即使上一次已经完美回答过同样的问题。Karpathy 2026-04 发布的 [LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) 正是针对这个痛点——在原始文档与回答之间加一层 LLM 自己维护的 wiki，整理一次就长期复用。

**解决**（新增**自进化整理层 Crystallized Skill Layer**）：

- 新增 agent：`@e:\PostGraduate\Project\plan-for-all\knowledge-base\agents\organize-agent.md`——整理层调度 Agent，负责固化 / 刷新 / 反馈 / 健康检查
- 新增 skills：
  - `@e:\PostGraduate\Project\plan-for-all\knowledge-base\skills\crystallize-workflow\SKILL.md`：固化层命中判断 / 新鲜度判断 / 写入 / 刷新
  - `@e:\PostGraduate\Project\plan-for-all\knowledge-base\skills\crystallize-lint\SKILL.md`：固化层周期健康检查
- 修改 qa 链路（保持向后兼容）：
  - `@e:\PostGraduate\Project\plan-for-all\knowledge-base\agents\qa-agent.md`：skills 列表加入 `crystallize-workflow`，加"先查固化层再查 RAG"、"一次满意回答后委托 organize-agent 固化"、"固化层返回时开头标注 📦 / 🔄 / ⚠️"
  - `@e:\PostGraduate\Project\plan-for-all\knowledge-base\skills\qa-workflow\SKILL.md`：新增「步骤 0：自进化整理层命中判断」与「步骤 9：委托 Organize Agent 固化本轮答案」
- 新增目录：`data/crystallized/`（被 .gitignore 忽略，运行时由 organize-agent 首次写入时自动创建，与 `data/docs/raw/` / `data/docs/chunks/` 处理方式一致）
- 文档更新：
  - `@e:\PostGraduate\Project\plan-for-all\knowledge-base\README.md`：三层架构说明 + 新流程图 + 目录结构 + 当前实现状态
  - `@e:\PostGraduate\Project\plan-for-all\knowledge-base\OPERATIONS_MANUAL.md`：第 12 章自进化整理层日常维护 + 第 10.5 节故障处理
- **完全不动的文件**（安全边界）：`agents/get-info-agent.md` / 所有 `skills/get-info-*` / `skills/web-research-ingest` / `skills/playwright-cli-ops` / `skills/knowledge-persistence` / `skills/update-priority` / `skills/mengsi16-knowledge-base` / `bin/` 全部、`.mcp.json`、`docker-compose.yml`、Milvus schema。**固化层完全叠加在原有架构之上，损坏时自动降级到原 RAG 主链，零回归风险**。

**固化 skill 结构**（扁平化，不引入 LLM Wiki v2 的置信度评分 / 知识图谱 / consolidation tiers，遵循本文件 Section 2 的 Simplicity First）：

- 每条 skill = 一个 `<skill_id>.md` + `index.json` 中的一条索引
- frontmatter 字段：`skill_id` / `description` / `trigger_keywords` / `created_at` / `last_confirmed_at` / `freshness_ttl_days` / `revision` / `user_feedback` / `source_chunks` / `source_urls`
- 正文包含可直接返回的答案 + `## 执行路径` + `## 遇到的坑` 两个小节
- `user_feedback` 三态机：`pending` → `confirmed` / `rejected`
- TTL 默认 90 天，稳定概念 180 天，快速迭代话题 30 天，由 organize-agent 首次固化时自行判断

**教训**：

- 类似 Karpathy LLM Wiki 这种"结构上只是新增一层"的改造，关键是**软依赖设计**：新层损坏、缺失、异常时必须静默降级到原有主链，绝对不能阻断问答。本次设计中 `qa-workflow` 步骤 0 返回 `degraded` 自动进 `miss` 分支，`organize-agent` 调用失败不回溯用户（答案已在步骤 8 给到），这些都是保底设计
- **不要在第一版就上 LLM Wiki v2 的全部特性**（置信度 / 知识图谱 / typed relationships / consolidation tiers 等）。先把"固化 + 命中 + 刷新 + 反馈"四个最小动作做稳定，等用量起来（>200 条 skill）再考虑升级命中判断机制
- 固化层的 `execution_trace` + `pitfalls` 不是流水账，是**让下一次抓取更高效的指南**。organize-agent 在首次固化时就要有意识地把"原 URL / 原搜索词 / 原为何有效 / 踩过的坑及避法"记下来——这些是 get-info-agent 刷新时的精准导航
- 本次计划见 `@e:\PostGraduate\Project\plan-for-all\knowledge-base\planning\2026-04-18-crystallized-skill-layer.md`，含完整风险审查表
