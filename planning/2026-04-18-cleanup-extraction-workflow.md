# 2026-04-18 收敛计划：清理提炼工作流的过度设计

## 背景

在前几轮对话中，为了支持"非官方来源（博客/教程/问答帖）的有用内容提炼"，
引入了一张 SQLite 表 `extracted_urls` 来记录提炼来源 URL。用户复审后发现：

1. `extracted_urls` 表和 chunk frontmatter 的 `urls` 字段 + 正文 `> 来源:` 标注**信息完全重复**。
2. `knowledge-persistence` 和 `update-priority` 两个 skill 都声称负责写 `extracted_urls`，**职责重叠**。
3. `get-info-workflow` 步骤 6.1 引用了 `priority.json.official_domains` 字段，但**该字段实际不存在**。
4. 官方文档用 `url`（单数），提炼文档用 `urls`（数组），**字段不统一**。

## 决策

### 决策 1：URL 字段保持 `url`（单数）与 `urls`（数组）并存

原因：Milvus collection schema 里 `url` 已经写死为 `VARCHAR(2048)`（`@bin/milvus-cli.py:281`），
修改会导致 drop collection + 全量重新入库，迁移成本高。保持现状更务实。

### 决策 2：官方判定采用"白名单 + LLM"两级机制

- `priority.json` 增加 `official_domains` 数组（初始可为空或少量已知域名）
- 分类时先查白名单（快速路径），未命中则交给 LLM 综合判断
- LLM 判定为 official 且置信度高时，由 `update-priority` 把新域名回写到 `official_domains`（自学习）
- LLM 置信度不足时仍按 official 处理，但不写入白名单（避免污染）

## 执行清单

### 要删除的

1. **`bin/scheduler-cli.py`**
   - 删除 `extracted_urls` 建表语句
   - 删除 `record_extracted_url()` 函数
   - 删除 `query_extracted_urls()` 函数
   - 删除 `record-url` / `query-urls` 子命令

2. **`skills/knowledge-persistence/SKILL.md`**
   - 删除职责第 7 条（extracted_urls 写入）

3. **`skills/update-priority/SKILL.md`**
   - 删除职责第 4 条（extracted_urls 相关）
   - 删除步骤 3 第 4 条（调用 record-url）
   - 删除步骤 4 第 5 条（community 域名写入 sites）

4. **`README.md`**
   - 删除 `extracted_urls` 表描述
   - 删除 `record-url` / `query-urls` CLI 示例

### 要修正的

5. **`skills/get-info-workflow/SKILL.md` 步骤 6.1**
   - 改写分类逻辑：先查 `official_domains` 白名单 → 未命中交 LLM 判断 → 发现新官方域名回报给 `update-priority`
   - 保留其他提炼规则

### 要新增的

6. **`skills/update-priority/SKILL.md`**
   - 新增"自动回填 `official_domains`"的责任

7. **`README.md`**
   - 在 `priority.json` 示例中加入 `official_domains` 数组字段
   - 说明字段用途

8. **`priority.json`**（如文件存在）
   - 添加 `official_domains: []` 字段（初始空数组）

### 要保持不变的

- Milvus schema（`url: VARCHAR`）
- 官方文档 frontmatter 的 `url`（单数字符串）
- 提炼文档 frontmatter 的 `urls`（数组）
- 正文 `> 来源: <url>` 标注
- 200 字符质量门槛
- 所有提炼规则（自包含、不编造、跳过观点等）

## 进度日志

执行过程中遇到的计划外情况和偏离会记录在此。

- **2026-04-18 T1**：`docs/` 目录被 gitignore 忽略（`.gitignore:46`），改用 `planning/` 目录存放计划文件。
- **2026-04-18 T2**：`scheduler-cli.py` 清理完成。删除 `extracted_urls` 建表逻辑、`record_extracted_url()`、`query_extracted_urls()` 函数以及 `record-url` / `query-urls` 子命令。文件恢复到添加提炼表之前的状态。
- **2026-04-18 T3**：`knowledge-persistence/SKILL.md` 清理完成。删除职责第 7 条。
- **2026-04-18 T4**：`update-priority/SKILL.md` 同步完成两件事——删除 `extracted_urls` 相关职责与步骤；新增步骤 5"回填 `official_domains` 白名单"。原计划里步骤 4 和步骤 7 合并执行。
- **2026-04-18 T5**：`README.md` 清理完成。删除 `extracted_urls` 表描述和 CLI 示例。同时把 `priority.json` 示例升级到 `version: 1.1.0`，新增 `official_domains` 数组字段和字段说明。
- **2026-04-18 T6**：`get-info-workflow/SKILL.md` 步骤 6.1 已按两级判定机制（白名单快速路径 + LLM 综合判断）写好，包含四分类结果（`official-high` / `official-low` / `community` / `discard`）和兜底规则。
- **2026-04-18 T7**：`get-info-agent.md` 复核，无需修改——其现有内容（核心职责第 6 条、搜索筛选要求第 5/6 条）已和新设计完全一致，不涉及 `extracted_urls`。原计划步骤 9 收敛为"无需变更"。
- **2026-04-18 T8**：全仓搜索确认 `extracted_urls` / `record-url` / `query-urls` / `record_extracted_url` 字样只残留在本计划文档中（作为删除记录），其他文件完全清洁。

## 最终状态

### 代码层
- `bin/scheduler-cli.py`：恢复为仅处理 keywords 的简洁版本
- `priority.json`（文档示例）：新增 `official_domains` 数组字段

### Skill 层
- `get-info-workflow` 步骤 6：来源分类使用"白名单 + LLM 两级判定"，LLM 判定为官方且置信度高的新域名会回报给 `update-priority`
- `knowledge-persistence`：仅负责 keywords.db / priority.json 的常规更新，不再涉及 `extracted_urls`
- `update-priority`：新增步骤 5"回填 `official_domains` 白名单"

### 数据层
- Milvus schema 完全未动（`url: VARCHAR(2048)`）
- 官方文档 frontmatter：`url`（单数字符串）
- 提炼文档 frontmatter：`urls`（JSON 数组） + 正文 `> 来源: <url>` 标注
- `keywords.db` 只保留 `keywords` 一张表

### 自学习域名白名单闭环
1. `get-info-workflow` 分类时先查 `priority.json.official_domains`
2. 未命中 → LLM 输出四分类
3. `official-high` 结果由 `update-priority` 幂等回填到 `official_domains`
4. 下次遇到相同域名直接命中白名单，无需再次 LLM 判断
