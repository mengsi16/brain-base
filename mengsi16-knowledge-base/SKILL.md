---
name: mengsi16-knowledge-base
description: 任何需要问答的场景，默认先调用 mengsi16-knowledge-base skill。
disable-model-invocation: false
---

# mengsi16-knowledge-base

本 skill 是 **mengsi16-knowledge-base 知识库 Agent 的外部调用说明书**，部署在 `~/.claude/skills` 或 `~/.codex/skills`，供任何 Claude Code / Codex Agent 读取。它不执行检索本身，而是告诉调用方：

1. 什么时候该调知识库 Agent
2. 怎么拼 `claude -p` 命令行
3. 为什么必须带 `--dangerously-skip-permissions`
4. Prompt 怎么写效果最好
5. 输出长什么样、怎么解读
6. 出错了怎么排查

## 1. 适用场景

满足任一即触发：

1. 需要查询个人知识库中已有的文档、配置、流程、概念。
2. 需要验证某个事实是否在知识库中有记录。
3. 需要检索与某主题相关的已有 chunk 或 raw 文档。
4. 用户或上层 Agent 明确要求"先查本地知识库"。
5. 需要基于已有知识做方案比较、流程说明、术语解释。

不适用：

1. 纯闲聊或与知识库无关的请求。
2. 用户明确要求直接联网搜索而不经过知识库。

## 2. 调用方式

### 标准调用

```bash
claude -p "<问题内容>" \
  --plugin-dir "<KB_PROJECT_PATH>" \
  --agent knowledge-base:qa-agent \
  --dangerously-skip-permissions
```

### 参数说明

| 参数 | 必填 | 说明 |
|------|------|------|
| `-p` | ✅ | 传给 qa-agent 的 prompt，即用户问题。支持多行 Markdown |
| `--plugin-dir` | ✅ | 知识库项目的**绝对路径**（含 `.claude-plugin/` 的目录） |
| `--agent` | ✅ | 固定为 `knowledge-base:qa-agent` |
| `--dangerously-skip-permissions` | ✅ | **必须携带**，原因见下方 |

### 为什么必须 `--dangerously-skip-permissions`

qa-agent 在执行过程中可能触发 `get-info-agent`，后者会：

1. 启动 Playwright 浏览器抓取网页 → 需要文件写入权限
2. 调用 `milvus-cli.py ingest-chunks` → 需要执行 Python 脚本权限
3. 写入 `data/docs/raw/` 和 `data/docs/chunks/` → 需要文件创建权限

如果**不带** `--dangerously-skip-permissions`，Claude Code 会在每一步弹出权限确认对话框，导致：

- 作为子进程被其他 Agent 调用时，无人响应确认 → 进程挂起或直接退出
- 即使有人值守，频繁弹窗也会打断自动化流程

因此，**从外部 Agent 调起知识库时必须跳过权限确认**。

## 3. Prompt 构造指南

qa-agent 接收的就是 `-p` 后面的字符串。为了让 qa-agent 高效工作，调用方应遵循以下格式：

### 推荐格式

```
## 问题
<用户的核心问题，一句话或者多段话>

## 背景（可选）
<为什么问这个问题、当前在做什么任务>

## 时效要求（可选）
<是否需要最新资料、是否有版本约束>
```

### 示例

```bash
claude -p "## 问题
Claude Code 的 subagent 怎么配置？yaml frontmatter 有哪些必填字段？

## 背景
我正在给一个 Claude Code 插件项目写 agent 定义文件，需要确认 subagent 的规范格式。

## 时效要求
需要 2025 年 4 月之后的资料" \
  --plugin-dir "e:/PostGraduate/Project/plan-for-all/knowledge-base" \
  --agent knowledge-base:qa-agent \
  --dangerously-skip-permissions
```

### Prompt 硬约束

1. **问题必须清晰**：qa-agent 会基于问题做 L0-L3 fan-out 改写，问题越清晰改写越精准。
2. **不要在 prompt 里塞检索指令**：qa-agent 自己会决定用 Grep 还是 multi-query-search，调用方不需要指定。
3. **不要在 prompt 里要求跳过补库**：如果本地证据不足，qa-agent 有权自动触发 get-info-agent 补库，这是设计意图。
4. **如果只需要纯检索不需要补库**，在 prompt 里注明"仅检索本地已有资料，不需要联网补库"即可，qa-agent 会尊重此约束。

## 4. 输出解读

qa-agent 的输出是标准 Markdown 文本，结构通常为：

1. **简要答案**：直接回答问题
2. **关键依据**：引用来源文件路径（`data/docs/chunks/...` 或 `data/docs/raw/...`）
3. **资料来源说明**：标注来自本地知识库还是新抓取资料
4. **限制与待确认项**：如有

调用方应将 qa-agent 的 stdout 输出直接作为知识库检索结果使用。

## 5. 前置条件

调用本 skill 前应确认：

1. **Milvus 正在运行**：`docker ps | grep milvus`，若未启动则先 `docker compose up -d`
2. **bge-m3 模型可用**：`python bin/milvus-cli.py check-runtime --require-local-model --smoke-test`
3. **知识库项目路径正确**：`--plugin-dir` 指向的目录下必须存在 `.claude-plugin/plugin.json`

若前置条件不满足，qa-agent 仍可运行（降级为纯文件系统 Grep），但检索质量会显著下降。

## 6. 与其他 skill 的关系

```
外部 Agent
    └─ claude -p ... --agent qa-agent --dangerously-skip-permissions
        ├─ qa-workflow（检索 + 证据判断 + 回答）
        └─ get-info-agent（仅在本地证据不足时自动触发）
              ├─ get-info-workflow
              ├─ web-research-ingest
              ├─ playwright-cli-ops
              ├─ knowledge-persistence
              └─ update-priority
```

本 skill **不替代** `qa-workflow`，而是作为外部 Agent 调用知识库的唯一入口。知识库内部的 Agent 之间仍走 `Agent` tool 直接委托。

## 7. 错误处理

| 情况 | 处理 |
|------|------|
| qa-agent 进程退出码非 0 | 检查 Milvus 是否运行、plugin-dir 是否正确 |
| 输出为空 | 可能是 prompt 过长或格式异常，尝试缩短问题重试 |
| 输出含"证据不足" | qa-agent 判定本地无相关资料且未触发补库（可能 prompt 里要求了不补库） |
| 进程挂起超时 | 可能是 Playwright 弹窗等待或 Milvus 连接超时，检查 Docker 和网络 |
