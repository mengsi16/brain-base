# Agent 与 Skill 职责划分审计

## 原则

- **Agent**：角色定位、调用谁（skill/agent 列表）、跨 agent 路由规则、权限边界、输入/输出接口契约
- **Skill**：Todo 模板、执行步骤、算法/规则细节、模板/格式、失败策略

Agent 不重复写 skill 已有的流程细节，只保留 agent 独有的"谁做什么"和"不能做什么"。

## 已完成

- **qa-agent**：174→58 行，删除 Todo 模板、核心职责、强制规则、检索策略、触发条件、回答要求、自愈/organize 触发、固化标注、禁止事项，保留角色定位、三层架构、跨 agent 路由、元查询 CLI、权限边界

## 已修正

### P0：organize-agent（288→89 行）

删除：Todo 模板 4 种 mode、核心职责、价值评分四维度+公式+冷热判定（与 crystallize-workflow §3.5 完全重复）、刷新决策规则、用户反馈处理表、接口 JSON 详情、返回结构、失败策略、不要做的事
保留：角色定位、调用链约束图、固化前置条件、指导 get-info-agent 要领、接口简化引用、权限边界

### P0：chunk-enrichment-agent（82→32 行）

删除：Todo 模板、执行流程 6 步、frontmatter 格式硬约束
保留：角色定位、返回格式 JSON、桥接声明

### P1：upload-agent（132→53 行）

删除：Todo 模板、核心职责 6 条、支持格式表、持久化要求、分块要求、返回要求
保留：角色定位、强制执行规则（agent 级约束）、不触发反例、与 Get-Info 关系图

### P1：lifecycle-agent（130→51 行）

删除：Todo 模板、核心职责、强制执行规则 6 条、返回结构 JSON、失败策略、不要做的事
保留：角色定位、调用链约束图、输入接口简化引用、权限边界

### P2：get-info-agent（91→46 行）

删除：Todo 模板、核心职责、搜索与分类要求、返回 JSON 模板
保留：严格边界声明、agent 级约束、返回要求简化引用

### P2：content-cleaner-agent（82→62 行）

删除：Todo 模板、核心职责（合并进 agent 级约束）
保留：角色定位、agent 级约束、输入/输出接口、与其他组件关系
