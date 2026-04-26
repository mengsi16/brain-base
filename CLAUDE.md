# CLAUDE.md

每条规则一句话说明要解决什么问题。

## 通用编码规则

1. **禁止写入仅当前上下文可见的内容**：改进项目时，不要往文件里写"修改了什么""对比之前如何"等依赖旧状态才能理解的内容——其他上下文的 Agent 看不到旧状态，这些信息对他们只是噪音。
2. **先想再写**：假设不明确就问，多种理解就列出，有更简单方案就说——避免基于错误假设写出一大段要重来的代码。
3. **只写解决当前问题的最少代码**：不加没要求的功能、不抽只用一次的抽象、不处理不可能发生的错误——200 行能缩成 50 行就该缩。
4. **只改必须改的**：不顺便"改善"相邻代码、不重构没坏的东西、匹配已有风格——每行变更必须能追溯到用户请求。
5. **目标驱动**：把任务变成可验证的成功标准，循环执行直到验证通过——弱标准（"让它能用"）需要反复确认，强标准可以独立循环。
6. **不改无关功能**：当前指令只改功能 A 时不要动功能 B——已完成且正确的功能改动容易引入回归。
7. **注释用中文 UTF-8**：生成的注释必须用中文，文件编码 UTF-8——项目面向中文用户。
8. **中文输出需检查乱码**：生成中文后必须确认无乱码，有则修正——部分环境下默认编码不是 UTF-8 会导致中文损坏。
9. **改函数先理解再叠加**：修改函数前先理解原实现逻辑，在原逻辑基础上叠加修改，不要移除已有逻辑——避免丢失已验证的正确行为。

## Agent 调度约束

6. **upload-agent 禁止并行**：MinerU 单文件峰值 ~14GB VRAM，16GB 显卡同一时刻只能跑一个——N 个文件必须一次调用顺序处理，严禁拆成 N 个并行任务导致 OOM。
7. **其他 agent 默认允许并行**（get-info / qa / organize 等不占 GPU），除非该 agent 自身标注"禁止并行"。

## 项目硬约束

8. **embedding 默认 bge-m3 hybrid**：sentence-transformer dense-only 在中英混合语料下召回弱且无 sparse 通道，已切到 bge-m3。
9. **pymilvus sparse 必须用 `dict[int, float]`**：scipy sparse 矩阵切片 shape 仍是 2D，pymilvus 不认，会抛 `expect 1 row`。
10. **短文不切分**：正文 ≤5000 字符整篇 1 块，>5000 才按语义切——防止短笔记被切成背景不全的碎片。
11. **不镜像文件系统数据到 SQL**：frontmatter + 文件系统本身是可 grep 索引，冗余 SQL 表只会造成职责重叠和同步负担。
12. **数据写入职责单一**：`keywords.db` / `priority.json` 写入归 `update-priority`，`knowledge-persistence` 不能越界——两个 skill 写同一张表是架构坏味道。
13. **引用字段前先定义**：skill 文本引用配置字段时 schema 里必须已存在，悬空引用会导致运行时找不到。
14. **新层必须软依赖**：固化层（crystallized）损坏/缺失时静默降级到 RAG 主链，绝对不能阻断问答。
15. **所有 agent 强制 TodoList**：LLM 倾向跳步，必须第一步生成 todo、按序执行、每步标记 completed——跳步是固有缺陷，TodoList 是唯一硬约束。
16. **subagent（-p 模式）无法与用户交互**：任何需要"问用户确认"的设计在 -p 模式下都会失效，固化写入是自动的不需要询问。
17. **上传路径独立于 get-info**：upload-agent 与 get-info-agent 平行，故障隔离——MinerU 挂不影响爬网页，Playwright 挂不影响本地上传。
18. **上传路径不调 update-priority**：没有 URL/搜索/站点优先级可更新，强行复用只会污染 `priority.json` 和 `keywords.db`。
19. **frontmatter `url:` 不写 `""`**：解析器 `split(":",1)[1].strip()` 不去引号，字面量 `""` 会当值写入 Milvus——冒号后留空即可。
20. **drop-collection 必须 --confirm**：切换 provider 后需 drop 旧 collection 重 ingest，`--confirm` 防误操作。
21. **mermaid 图表用 sequenceDiagram**：流程图（flowchart）节点多时连线杂乱难读，改用时序图（sequenceDiagram）以参与者交互展示流程；中括号内容必须用双引号包裹（`participant X as "名称"`），否则大概率渲染失败。
22. **Amazon 不走 Cloudflare**：Amazon 有自己的 CDN/DDoS 防护（AWS Shield / CloudFront），`solve_cloudflare=True` 只会多耗 5-15 秒甚至超时且无收益。
23. **审核与执行必须分离**：同一 LLM 既执行又审核会敷衍通过，audit 必须是独立 agent 且只报告不修复——修复责任交 orchestrator 重新触发执行。
24. **流水线禁止中途询问用户**：收到触发后必须从头执行到底，报错记录后继续推进，不得在任何步骤暂停等待用户确认——适用于所有 agent。
25. **禁止 try-except，fail-fast**：能第一时间报错就第一时间报错，try-except 会隐藏真实问题导致后续排障困难。
26. **调试 HTML 解析先读 raw HTML**：BS4 `select_one` 可能遗漏后续同 ID 容器，用它验证自己没意义——先确认源数据完整，再排查解析逻辑。
27. **选择器和正则面向开放集合**：不要枚举已知值（如 `module-N` / `brand-story-*`），用通用模式一次性覆盖，否则每遇到新前缀就要改代码。
28. **Windows 下外部子进程优先用 `subprocess.Popen`**：`asyncio.create_subprocess_exec` 在 Windows 上可能抛 `NotImplementedError`，不要想当然使用。
29. **错误信息端到端透传**：链路中任何一层不得把真实错误降级成"未知错误"或空字符串，否则排障被无意义信息阻塞。

## 故障排查顺序

ingest 失败 / 检索不对时按序检查：

1. `docker compose ps` → Milvus `(healthy)`
2. `python milvus-cli.py check-runtime --require-local-model --smoke-test` → `dense_dim` / `sparse_nnz` / `resolved_mode`
3. `python milvus-cli.py inspect-config` → `embedding_provider` 含 `kind` / `question_id`
4. dense dim 不匹配 / 缺 sparse 字段 → `python milvus-cli.py drop-collection --confirm` 后重 ingest
5. `expect 1 row` / `invalid input for sparse float vector` → sparse 值必须是 `dict[int, float]`
