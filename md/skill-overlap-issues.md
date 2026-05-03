# Skill 重叠问题清单

排查范围：content-cleaner-workflow / knowledge-persistence / get-info-workflow / web-research-ingest / chunk-enrichment

## 问题 1：content-cleaner 越界做了 knowledge-persistence 的活

- **现状**：content-cleaner 步骤5 自己算 hash + 调 hash-lookup，步骤6 自己写 raw 文件，步骤7 再"调用 knowledge-persistence"
- **问题**：knowledge-persistence §2-2.1 也定义了 hash 去重和 raw 写入规则。两边都写 = 规则漂移 + 可能重复执行
- **修正**：content-cleaner 只负责"抓取 → 清洗 → 校验"，产出内存中的清洗后 Markdown + 元数据。hash-lookup、raw 写入、chunking、enrichment、ingest 全部交给 knowledge-persistence。content-cleaner 步骤5-6 删除，步骤7 改为"把清洗结果传给 knowledge-persistence"
- **决策**：frontmatter 模板体系统一放 knowledge-persistence，分 raw（3套）和 chunk（3套）共6套模板，避免模型上下文污染。content-cleaner 不再定义模板，只声明传递哪些元数据。raw 模板含 content_sha256，chunk 模板不含。keywords 统一用 JSON inline 格式
- **状态**：✅ 已修改（content-cleaner 删除步骤5-6，步骤7改为传元数据；knowledge-persistence 新增 §2.2 raw 模板 + §2.3 必填字段表）

## 问题 2：knowledge-persistence 越界写了 keywords.db / priority.json

- **现状**：knowledge-persistence §1 第6条说"更新 keywords.db 与 priority.json（仅 get-info 路径需要）"
- **问题**：违反规则12"数据写入职责单一"——update-priority skill 才是 keywords.db / priority.json 的唯一写入者
- **修正**：knowledge-persistence 删除 §1 第6条，keywords.db / priority.json 更新只由 update-priority 负责（get-info-workflow 步骤8 调用它）
- **状态**：✅ 已修改（knowledge-persistence 删除 §1 第6条，description 移除 SQLite 关键词更新）

## 问题 3：get-info-workflow 越界定义了 URL 分类逻辑

- **现状**：web-research-ingest 步骤3 说"逻辑与 get-info-workflow 步骤6 一致"，暗示分类逻辑的真相源在 get-info-workflow
- **问题**：URL 分类的执行者是 web-research-ingest，规则定义却在 get-info-workflow。改分类规则时要改两处
- **修正**：URL 分类的唯一真相源移到 web-research-ingest。get-info-workflow 只说"调用 web-research-ingest，它返回分类后的 URL 列表"，不再内联分类逻辑
- **状态**：✅ 已修改（get-info-workflow 步骤5 移除内联分类逻辑，改为引用 web-research-ingest；web-research-ingest 步骤3 移除反向引用）

## 问题 4：get-info-workflow 越界重复了检索计划生成

- **现状**：get-info-workflow 步骤4 定义了"主查询 + 变体 + 站点优先级 + 搜索顺序"，web-research-ingest 步骤1 也定义了"主查询 + 变体 + 优先站点 + 时间窗口"
- **问题**：检索计划的细节（时间窗口分批、查询变体生成）是 web-research-ingest 的执行逻辑，get-info-workflow 只需传递"用户意图 + 主题 + 是否要最新"
- **修正**：get-info-workflow 步骤4 简化为"整理用户意图和约束条件（主题、时效要求、来源偏好）"，具体的查询变体生成和时间窗口策略留给 web-research-ingest
- **状态**：✅ 已修改（get-info-workflow 步骤4 简化为意图整理，歧义消解建议合并进 web-research-ingest 步骤1）

## 问题 5：content-cleaner 和 knowledge-persistence 都维护 frontmatter 模板

- **现状**：content-cleaner 步骤6 有简化模板，knowledge-persistence §4 有完整模板（3种 source_type）
- **问题**：同一模板两处维护，改一处忘一处
- **修正**：frontmatter 模板的唯一真相源在 knowledge-persistence（它是实际写盘的执行者）。content-cleaner 不再定义模板，只描述"传递哪些元数据给 knowledge-persistence"
- **状态**：✅ 已修改（content-cleaner 模板已在问题1中删除，改为引用 knowledge-persistence §2.2）

## 不算问题的重叠

- **chunk-enrichment 定义 frontmatter 格式约束 vs knowledge-persistence 定义模板**：chunk-enrichment 定义"怎么写"（JSON inline 数组、闭合 `---`），knowledge-persistence 定义"长什么样"（目标结构），视角不同，不算越界。但 chunk-enrichment 应引用 knowledge-persistence 的模板而不是重新定义字段列表
