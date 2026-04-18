# Knowledge-Base 全流程使用手册

本手册面向"不想反复手动确认权限、希望尽可能自动化运行"的使用方式。

和 README 中的快速启动不同，这里覆盖完整链路：

1. 环境准备
2. Milvus 启动与验证
3. QA Agent 全权限启动
4. QA -> Get-Info 自动协作
5. 后台化运行策略
6. 常见故障与恢复

---

## 0. 先回答你最关心的问题

### Claude Code 能否让 Get-Info 永久后台常驻，然后 QA 随时调用？

短答案：

1. QA 自动调用 Get-Info：可以。
2. Get-Info 作为 Claude Code 内置"独立常驻守护进程"：不能直接原生保证。

可行做法：

1. 在同一个 QA 会话里，按需触发 Get-Info（最接近"后台辅助"，也是推荐模式）。
2. 用 Windows 任务计划定时跑 Get-Info 补库任务（真正后台周期运行）。
3. 保持一个长期会话窗口不关闭（工程上可行，但属于会话常驻，不是系统服务）。

---

## 1. 当前架构与调用链

标准调用链是：

1. 用户对 QA 提问。
2. **QA 先查自进化整理层（`data/crystallized/`）**：命中且新鲜 → 直接返回固化答案；命中但过期 → 委托 Organize 刷新；未命中 → 继续下面的 RAG 流程。
3. QA 在本地知识不足时触发 Get-Info。
4. Get-Info 再调用 get-info-workflow 和其他子 skill。
5. **一次满意回答后**，QA 委托 Organize 把答案固化到 `data/crystallized/`，供下次复用。

注意：

1. QA 不应直接调用持久化 skill。
2. Get-Info 不应绕过前置检查直接入库。
3. QA 不应直接写 `data/crystallized/` 下任何文件，全部由 Organize 执行。
4. Organize 不应直接调 Playwright-cli 或写原始层，刷新时通过 Get-Info 完成。

---

## 2. 一次性准备（Windows）

在 PowerShell 中执行（`knowledge-base` 的父目录）：

```powershell
Set-Location "your\path\to\knowledge-base的父目录"
```

下面出现的 `Set-Location "your\path\to\knowledge-base的父目录\knowledge-base"` 表示先进入仓库根目录再执行命令；其中 `claude --plugin-dir .` 里的 `.` 指的也是当前目录。

### 2.1 安装/确认基础依赖

```powershell
python --version
docker --version
claude --version
npx --version
uv --version
```

如果 `uv` 不存在，可安装：

```powershell
python -m pip install --user -U uv
```

### 2.2 安装向量化与抓取依赖（全局/用户级）

```powershell
python -m pip install --user -U "pymilvus[model]" sentence-transformers FlagEmbedding
npm install -g @playwright/cli@latest
```

说明：
1. `python -m pip install --user ...` 会安装到当前用户的 Python 用户级目录。
2. `FlagEmbedding` 是默认 BGE-M3 hybrid provider 的底层推理库，首次调用会下载约 1.4 GB 模型到 `%USERPROFILE%\.cache\huggingface\`。
3. `npm install -g ...` 会安装到全局 Node 环境。

如需更好的 agent 集成，可按官方 README 继续执行；对本项目的 Agent 集成场景，这一步视为必需：

```powershell
playwright-cli install --skills
```

验证：

```powershell
playwright-cli --help
```

如果你使用的是项目本地安装而不是全局安装，请改用项目根目录下的 `npx --no-install playwright-cli --help` 验证。

### 2.3 准备官方 Milvus MCP Server 代码

如果目录不存在：

```powershell
git clone https://github.com/zilliztech/mcp-server-milvus.git .\knowledge-base\mcp\mcp-server-milvus
```

你当前项目通过插件根目录 `.mcp.json` 接入 MCP server（这是官方插件结构推荐方式）。

---

## 3. 启动 Milvus（Docker）

进入插件目录：

```powershell
Set-Location "your\path\to\knowledge-base的父目录\knowledge-base"
```

启动：

```powershell
docker compose up -d
```

查看状态：

```powershell
docker compose ps
```

健康检查：

```powershell
curl.exe -i http://localhost:9091/healthz
```

WebUI 地址：

1. 正确：`http://localhost:9091/webui/`
2. 根路径 `http://localhost:9091/` 返回 404 是正常现象。

---

## 4. 启动前预检（必须通过）

仍在 `knowledge-base` 目录下执行：

```powershell
python bin/milvus-cli.py inspect-config
python bin/milvus-cli.py check-runtime --require-local-model --smoke-test
```

通过标准：

1. `can_vectorize` 为 `true`
2. 能看到 `local_model`（默认 `BAAI/bge-m3`；若手动设为 sentence-transformer 则是 `all-MiniLM-L6-v2`）
3. `resolved_mode` 为 `hybrid`（默认；sentence-transformer 下是 `dense`）
4. `dense_dim` 跳出实际维度（bge-m3 = 1024；all-MiniLM-L6-v2 = 384）

---

## 5. 全权限启动 QA Agent（自动化模式）

在 `knowledge-base` 目录执行：

```powershell
Set-Location "your\path\to\knowledge-base的父目录\knowledge-base"
claude --plugin-dir . --agent knowledge-base:qa-agent --dangerously-skip-permissions
```

这条命令的效果：

1. 加载 knowledge-base plugin
2. 指定 QA 为主 agent
3. 跳过权限确认弹窗（高自动化）

安全提示：

1. `--dangerously-skip-permissions` 官方仅建议在你信任、最好无互联网访问的隔离环境中使用。
2. 该模式会绕过权限确认，联网抓取、写文件和执行命令都不会再逐条征求确认。

---

## 6. QA 如何触发 Get-Info

在 QA 会话中，以下情形通常会触发 Get-Info：

1. 你明确要求"最新资料"、"联网补充"。
2. 本地 chunks/raw/Milvus 证据不足。
3. 本地内容过时或冲突。

建议提问模板：

```text
请先联网补充最新官方文档，再回答：Claude Code 的 subagent 如何配置 MCP 作用域？
```

你会看到 QA 在同一任务流里调用 Get-Info 完成补库后再回到回答阶段。

---

## 7. 后台化运行的三种方案

### 方案A（推荐）：一个常驻 QA 会话

特点：

1. 你主要和 QA 对话。
2. Get-Info 在需要时由 QA 自动调用。
3. 不需要单独维护第二个后台进程。

适合：日常问答和按需补库。

### 方案B：定时后台补库（任务计划）

特点：

1. 用 Windows Task Scheduler 周期执行 `claude -p` 补库任务。
2. QA 日常回答更多依赖已提前更新的本地知识。

示例命令（可用于计划任务动作）：

```powershell
Set-Location "your\path\to\knowledge-base的父目录\knowledge-base"; claude --plugin-dir . --agent knowledge-base:get-info-agent --dangerously-skip-permissions -p "根据 priority.json 对高优先级站点执行增量补库，并更新 raw/chunks/Milvus 与关键词统计。"
```

### 方案C：单独开一个 Get-Info 长会话

特点：

1. 你开两个终端：一个 QA、一个 Get-Info。
2. Get-Info 终端长期不关，手动喂任务。

缺点：

1. 不是系统级守护进程。
2. 仍依赖会话持续存在。

---

## 8. 默认本地向量模型

默认已切为：

1. provider：`bge-m3`
2. 模型：`BAAI/bge-m3`
3. 检索模式：`hybrid`（dense 1024 维 + sparse 词级权重）
4. 设备：`cpu`（有 GPU 时设 `KB_EMBEDDING_DEVICE=cuda`）

理由：

1. 中英混合语义能力明显优于 all-MiniLM-L6-v2。
2. 同时产出 dense + sparse，能启动本项目的 hybrid 检索与合成 QA 召回。
3. CPU 首次启动会下载约 1.4 GB 模型。下载之后本地缓存，不重复下载。

轻量回退选项（如机型弱 / 不需要中文加强）：

```powershell
$env:KB_EMBEDDING_PROVIDER = "sentence-transformer"
python bin/milvus-cli.py check-runtime --require-local-model --smoke-test
```

改后请注意：dense 维度从 1024 变为 384，必须 drop 旧 collection 再重新 ingest-chunks。CLI 会在 dim 不匹配时 fail-fast。

---

## 9. 日常操作清单（你只要照做）

每天开始：

1. `docker compose up -d`（在 `knowledge-base` 目录）
2. `python bin/milvus-cli.py check-runtime --require-local-model --smoke-test`。首次运行会下载 BGE-M3 模型（1.4 GB）。
3. `claude --plugin-dir . --agent knowledge-base:qa-agent --dangerously-skip-permissions`
4. 若当日有新增 chunk 文件（frontmatter 里必须含 `questions: [...]`），执行 `python bin/milvus-cli.py ingest-chunks --chunk-pattern "data/docs/chunks/*.md"` 做 hybrid 入库（CLI 会同时写 chunk 行与 question 行，返回报告会给出 `chunk_rows`/`question_rows` 计数）。
5. 需要检索验证时，可在命令行跑 multi-query-search 看 RRF 结果：`python bin/milvus-cli.py multi-query-search --query "..." --query "..."`
6. 偶尔检查自进化整理层状态：看 `data/crystallized/index.json` 的 `skills` 条目数与 `lint-report.md`（如存在）。

每天结束：

1. 退出 Claude 会话
2. 需要省资源时执行 `docker compose down`

---

## 10. 常见故障与处理

### 10.1 WebUI 404

现象：访问 `http://localhost:9091/` 返回 404。

处理：

1. 改用 `http://localhost:9091/webui/`。

### 10.2 check-runtime 失败（缺少 pymilvus.model 或 FlagEmbedding）

处理：

```powershell
python -m pip install --user -U "pymilvus[model]" sentence-transformers FlagEmbedding
```

若报错提示“dense dim 不匹配”或“collection 缺少 sparse 字段”，表示换过 provider 但未重建 collection。处理：使用 Milvus MCP 或 webui drop 旧 collection（默认名 `knowledge_base`）后重跑 ingest-chunks。

### 10.3 playwright-cli 不可用

处理：

```powershell
npm install -g @playwright/cli@latest
playwright-cli --help
```

如果你使用的是项目本地安装而不是全局安装，请改用项目根目录下的 `npx --no-install playwright-cli --help` 验证。

### 10.4 Docker 已开但 Milvus 不健康

处理：

```powershell
docker compose ps
docker compose logs --tail=200
```

确认 `etcd`、`minio`、`standalone` 三个容器都在运行。

### 10.5 自进化整理层故障

#### 固化答案返回了错误内容

处理：在同一会话里明确说出这不对或过时，qa-agent 会通知 organize-agent 将该 skill 标为 `rejected`。下一次 `crystallize-lint` 会删除该条目。同一问题再问将重走完整 RAG 流程。

#### 固化答案明显过期但没自动刷新

根因：固化 skill 的 `last_confirmed_at + freshness_ttl_days` 还未到期。

处理：在会话里明说“我需要最新资料”，qa-agent 会强制触发刷新；或手动缩短 `freshness_ttl_days` 后再问。

#### `data/crystallized/index.json` 损坏

现象：qa-agent 启动时报 JSON 解析失败，自动降级到 `miss`。

处理：

```powershell
Set-Location "your\path\to\knowledge-base\data\crystallized"
Get-ChildItem index.json.broken-* | Select-Object -First 1
# 查看备份文件、手动修复后用 organize-agent 运行 crystallize-lint
```

或直接删掉 `index.json` 等 qa-agent 下次启动自动重建空索引，代价是磁盘上的 `<skill_id>.md` 会被 `crystallize-lint` 视为孤儿文件移到 `_orphans/` 目录待人工审阅。

#### 固化层越积越多干扰问答

处理：运行 `crystallize-lint`。在 `claude --plugin-dir . --agent knowledge-base:organize-agent --dangerously-skip-permissions` 会话里说“对固化层做一次 lint”，会自动清理 rejected / 超过 3× TTL / 孤儿 / 损坏条目。

---

## 11. 你可以直接复制的两条命令

### 一键启动基础环境

```powershell
Set-Location "your\path\to\knowledge-base的父目录\knowledge-base"; docker compose up -d; python bin/milvus-cli.py check-runtime --require-local-model --smoke-test
```

### 一键进入全权限 QA

```powershell
Set-Location "your\path\to\knowledge-base的父目录\knowledge-base"; claude --plugin-dir . --agent knowledge-base:qa-agent --dangerously-skip-permissions
```

---

## 12. 自进化整理层（Crystallized Skill Layer）

本项目在 2026-04-18 新增了**自进化整理层**，对标 Karpathy [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) 模式。日常使用时你不需要做额外操作，qa-agent 与 organize-agent 会自动处理。下面是知情权的说明。

### 12.1 固化答案存储在哪儿

```
data/crystallized/
├── index.json             # 全局索引
└── <skill_id>.md          # 每条固化 skill 一个文件
```

整个目录已被 `.gitignore` 忽略，不会进入仓库。由 `organize-agent` 首次写入时自动创建。

### 12.2 固化答案的生命周期

| 阶段 | 触发时机 | 动作 |
|---|---|---|
| 创建 | 你对 qa-agent 提一个新问题，它给出符合固化条件的答案 | 写 `<skill_id>.md` + 更新 `index.json`，`revision=1`，`user_feedback=pending` |
| 复用 | 你再次问相似问题 | qa-agent 命中 `hit_fresh` 直接返回，回答开头标 `📦` |
| 刷新 | 命中的 skill 已超 TTL，或你明确说“最新” | organize-agent 携带原 `execution_trace` + `pitfalls` 调 get-info-agent 更新知识库，qa-agent 重生成答案，覆盖写回，`revision+=1` |
| 确认 | 你在下一轮对话中未否定固化答案 | `pending` → `confirmed`，`last_confirmed_at` 刷新 |
| 拒绝 | 你明确说“不对”/“不满意” | `confirmed`/`pending` → `rejected`，`crystallize-lint` 下次清理 |
| 补充 | 你主动补充信息 | `pitfalls` 追加一条“本轮遗漏: <摘要>”，`revision+=1` |
| 清理 | `crystallize-lint` 运行 | 删除 `rejected` / 超 3× TTL 未确认的条目，孤儿文件移到 `_orphans/` |

### 12.3 TTL 默认值

`organize-agent` 在首次固化时根据主题自行判断：

| 主题类型 | TTL |
|---|---|
| 稳定概念（算法 / 架构 / 设计哲学） | 180 天 |
| 产品文档（配置 / 命令 / API） | 90 天 |
| 快速迭代话题（beta 功能 / 预览版） | 30 天 |

你可以手动编辑对应 `.md` 文件 frontmatter 的 `freshness_ttl_days` 覆盖默认值。

### 12.4 手动维护命令

启动 organize-agent 会话，在其中说自然语言命令即可：

```powershell
Set-Location "your\path\to\knowledge-base"
claude --plugin-dir . --agent knowledge-base:organize-agent --dangerously-skip-permissions
```

常用自然语言命令：

1. `对固化层做一次 lint` → 执行 `crystallize-lint`
2. `强制刷新 skill <skill_id>` → 不管 TTL 是否过期，立刻走刷新路径
3. `列出所有 pending 状态的 skill` → 导出 `index.json` 中 `user_feedback=pending` 的条目

### 12.5 为什么不走计划任务

固化层的写入、刷新、反馈处理都是**事件驱动**的（用户提问 / 满意回答 / 反馈），不需要计划任务。`crystallize-lint` 在会话中手动触发即可，无需定时跑。

---

## 13. 结论

你的目标"默认自动化、少打断"是可实现的：

1. QA 主会话 + 自动触发 Get-Info（推荐主模式）。
2. 自进化整理层自动在 qa-agent 与 organize-agent 间协作，用户无需介入。
3. 如需真正后台持续补库，配合任务计划做周期运行。

但要明确：

1. Claude Code 当前不是一个内建"常驻后台服务编排器"。
2. 需要靠会话常驻或系统调度来实现持续后台行为。
