# chunk-enrichment

## 为什么需要这个 skill

chunker.py 做物理切分时只能产出基础 frontmatter（doc_id / chunk_id），但检索层需要更多元数据才能高效召回：

1. **summary**：cross-encoder reranker 用 summary 而非全文打分，没有 summary 的 chunk 无法被重排。
2. **keywords**：sparse 向量之外的关键词补充，提升精确匹配召回。
3. **questions（doc2query）**：用户问法和 chunk 标题往往差异很大（用户问"怎么装"，chunk 标题是"Installation Guide"），为每个 chunk 生成 3-5 条用户口吻的问题，入库后作为独立向量行，能大幅提升问法匹配率。

如果让 knowledge-persistence 内联 enrichment 规则，会导致：
- knowledge-persistence 已经很长（入库顺序、frontmatter 模板、Milvus 配置），再塞 enrichment 规则会膨胀到无法维护
- enrichment 可能被独立触发（已有 chunk 但 enrichment 缺失），需要单独调用入口

这个 skill 就是"chunk → LLM 富化 → 写回 frontmatter"的**独立封装**，核心价值是：

- enrichment 字段的生成规则（title/summary/keywords/questions 各自的约束）
- doc2query 六维度覆盖（direct/action/comparison/fault/alias/version），确保问题多样性
- 自检步骤：生成后必须验证问题能在 chunk 正文里找到答案，防止幻觉问题污染检索
