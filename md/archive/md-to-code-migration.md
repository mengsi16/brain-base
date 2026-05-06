# MD约束 → 代码约束 迁移清单

## 问题本质

Markdown 指令文件对 LLM 是**建议性约束**，不是代码强制。MiniMax M2.5 在多步骤条件状态机上的遵循能力极差（这也是 chunker.py 存在的原因）。关键判断放在 MD 里，LLM 概率性忽略，导致：

1. get-info-agent 抓内容+写文件（应该只搜索返回URL列表）
2. qa-agent 拿到富文本直接用（应该走cleaner入库后再回答）
3. 多URL合并为一个raw文档（应该一个URL一个raw）
4. official-doc被翻译/概括/删章节（应该完整保留）

## 逐 Skill 问题清单

### content-cleaner-workflow

| 位置 | 当前(MD约束) | 问题 | 应该(代码约束) |
|------|-------------|------|---------------|
| 步骤3 | "禁止概括/缩写/改写""章节结构不变""禁止跨URL合并" | LLM忽略，official-doc被删章节 | `bin/cleaner-validator.py`：抓取时记录原始HTML h2数量，清洗后比对Markdown h2数量，不一致拒绝落盘 |
| 步骤4 | "长度比率≥50%""章节计数一致""回退步骤3重做" | LLM自校验=无校验；重做结果几乎一样 | `bin/cleaner-validator.py`：一次性代码校验，不过就fail，不重做 |
| 步骤5 | SHA-256去重 | LLM手动算hash不可能 | 代码计算hash+调milvus-cli查重 |
| 步骤6 | frontmatter组装 | LLM手动写，格式易错 | 代码写frontmatter |
| 步骤7 | "调用knowledge-persistence" | LLM可能跳过 | 代码编排4步原子操作 |

### knowledge-persistence

| 位置 | 当前(MD约束) | 问题 | 应该(代码约束) |
|------|-------------|------|---------------|
| 步骤5.3 | "写raw → chunker.py → enrichment → 入库"顺序硬约束 | LLM可能跳步 | `bin/persist-pipeline.py`：Python编排4步原子操作 |
| 步骤6 | "必须执行check-runtime""必须调ingest-chunks" | LLM可能跳过 | 代码强制执行 |
| 步骤2.1 | content_sha256去重 | LLM无法手动算hash | 代码计算+查重 |
| 第1节 | "两条入口的共同下游" | 没有共享的Python入口点 | `bin/persist-pipeline.py`作为统一入口 |

### web-research-ingest

| 位置 | 当前(MD约束) | 问题 | 应该(代码约束) |
|------|-------------|------|---------------|
| 步骤2 | "只执行搜索不打开候选页面" | LLM经常打开候选页面看内容 | 代码层面限制：搜索skill不暴露eval/screenshot接口给get-info-agent |
| 步骤1.2 | 5批次时间窗口分批检索 | 复杂条件状态机，LLM遵循能力差 | `bin/web-search.py`：代码控制批次，够就停 |
| 步骤3 | URL分类（白名单+LLM判断） | 白名单部分可以代码做 | 代码查priority.json做白名单分类，LLM只做剩余判断 |
| 步骤4 | JSON输出格式 | LLM经常不遵守 | 代码解析LLM输出，提取URL列表 |

### get-info-workflow

| 位置 | 当前(MD约束) | 问题 | 应该(代码约束) |
|------|-------------|------|---------------|
| todo模板 | 步骤6还写着"并行调度content-cleaner-agent" | 与实际步骤6（返回URL列表）不一致，LLM困惑 | 修正todo模板 |
| 步骤2 | 健康检查决策矩阵（3种场景） | LLM遵循能力差 | `brain-base-cli.py`：代码跑检查，代码决定走向 |
| 步骤3 | 读取priority.json+keywords.db | LLM可能跳过 | 代码预加载 |
| 步骤6 | JSON输出格式 | LLM经常返回富文本 | 代码解析LLM输出，提取URL |
| 第4节 | 输出含"raw文档路径/chunk文档路径" | 本workflow已不负责落盘，描述过时 | 修正输出描述 |

### qa-workflow

| 位置 | 当前(MD约束) | 问题 | 应该(代码约束) |
|------|-------------|------|---------------|
| 步骤7.4 | 并行调度content-cleaner-agent | LLM拿到富文本直接用，不走cleaner | qa-agent调`brain-base-cli.py ingest-url`，CLI编排全流程 |
| 步骤7.1 | prompt里加JSON格式硬约束 | 子agent读自己的md，不看prompt | CLI解析子agent输出，代码提取URL |
| 步骤7.2 | 降级分支（3条条件） | LLM判断不可靠 | 代码检测超时和错误码 |
| 步骤-1 | 基础设施探测 | LLM可能跳过或伪造 | CLI启动前自动检查 |

### playwright-cli-ops

| 位置 | 当前(MD约束) | 问题 | 应该(代码约束) |
|------|-------------|------|---------------|
| 第2节 | 职责含"页面打开与内容导出" | get-info-agent不应有内容导出能力 | 拆搜索/导出为独立命令，get-info-agent只授权搜索命令 |
| 3.1 | SPA等待/重试策略 | LLM不会精确执行"等待5秒重试2次" | `bin/fetch-page.py`：代码控制等待和重试 |
| 第4节 | 输出含"导出的原始页面内容" | get-info-agent拿到正文就会直接用 | 搜索模式只返回URL+标题+摘要 |

### upload-ingest

| 位置 | 当前(MD约束) | 问题 | 应该(代码约束) |
|------|-------------|------|---------------|
| 步骤5 | 入库顺序硬约束 | 同knowledge-persistence | `bin/persist-pipeline.py`统一编排 |
| 步骤4.5 | hash去重 | LLM无法手动算 | 代码计算 |
| 步骤4 | frontmatter组装 | LLM手动写易格式错误 | 代码写frontmatter |
| 步骤3 | doc-converter调用 | 已是代码路径，相对可靠 | - |

## 核心重构方向

### 现状：qa-agent 通过 Agent tool 调子agent

```
qa-agent --Agent tool--> get-info-agent --Agent tool--> content-cleaner-agent
                          ↑ 全靠MD约束                  ↑ 全靠MD约束
                          ↑ LLM概率性忽略               ↑ LLM概率性忽略
```

### 目标：qa-agent 通过 Bash tool 调 CLI，CLI 用 Python 编排

```
qa-agent --Bash tool--> brain-base-cli.py ingest-url
                        ↓ Python代码编排（确定性）
                        ├─ 1. 调 get-info-agent 搜索（claude -p）
                        ├─ 2. 解析输出，提取URL列表
                        ├─ 3. 并行调 content-cleaner-agent（claude -p x N）
                        ├─ 4. 每个cleaner内部：fetch→validate→persist-pipeline
                        └─ 5. 汇总结果返回
```

### 新增代码文件

| 文件 | 职责 |
|------|------|
| `bin/persist-pipeline.py` | 原子化入库：raw→chunker→enrichment→ingest，4步不可跳步 |
| `bin/cleaner-validator.py` | 清洗校验：h2计数比对、长度比率、SHA-256去重、frontmatter组装 |
| `bin/fetch-page.py` | 页面抓取：SPA等待/重试、空壳检测、原始内容保存 |
| `bin/web-search.py` | 搜索编排：时间窗口分批、够就停、白名单分类 |

### 修改代码文件

| 文件 | 改动 |
|------|------|
| `bin/brain-base-cli.py` | `ingest-url`：从"调一次get-info-agent"改为"编排全流程" |
| `bin/brain-base-cli.py` | `clean-url`：内部调fetch-page→cleaner-validator→persist-pipeline |

### MD文件精简方向

- **保留**：LLM擅长的事——理解意图、分类判断、信息富化、答案生成
- **删除**：确定性逻辑——顺序编排、数值校验、格式约束、重试策略
- **结果**：MD文件从"全流程指令"变成"LLM职责说明+代码调用接口"
