---
name: web-research-ingest
description: 当 get-info-agent 需要从外部网页补充知识时触发。负责基于查询计划调度 playwright-cli-ops skill 完成检索、候选页筛选、正文抓取与初步清洗，生成可交给持久化 skill 的结构化文档草稿。
disable-model-invocation: false
---

# Web Research Ingest

## 1. 职责

本 skill 负责：

1. 根据主题与查询计划选择搜索策略。
2. 调用 `playwright-cli-ops` 获取页面内容。
3. 对抓取到的页面做初步清洗与结构化。
4. 产出适合交给持久化层处理的文档草稿。

## 2. 输入

1. 用户原问题。
2. 查询改写结果。
3. 候选站点优先级。
4. 是否要求最新资料。

## 3. 执行步骤

### 步骤1: 生成检索计划

1. 先确定主查询。
2. 再生成 2 到 5 个检索变体。
3. 再按 `priority.json` 选择优先站点。

### 步骤2: 调用 playwright-cli-ops

1. 搜索候选页面。
2. 筛掉低质量页面。
3. 打开高价值页面。
4. 导出正文内容。

### 步骤3: 初步清洗

1. 去掉导航、广告、页脚、推荐阅读。
2. 保留标题层级、正文、代码块、表格、FAQ、步骤列表。
3. 标注来源、URL、抓取时间、主题。

### 步骤4: 产出文档草稿

输出结构至少包含：

1. `title`
2. `source`
3. `url`
4. `fetched_at`
5. `topic`
6. `content_markdown`

## 4. 边界

1. 本 skill 只做到“拿到高质量 Markdown 草稿”。
2. 分块、落盘、Milvus 写入交给 `knowledge-persistence`。
