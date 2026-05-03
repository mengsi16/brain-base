# knowledge-persistence

## 为什么需要这个 skill

brain-base 有两条知识入库路径：web 补库（get-info）和本地上传（upload）。两条路径的"上游"完全不同（一个抓网页，一个转 PDF），但"下游"完全相同：都要做 raw 保存、chunk 切分、信息富化、Milvus 入库。如果两条路径各自实现持久化逻辑，必然导致：

1. **规则漂移**：chunk 格式、frontmatter 字段、入库顺序约束在两处维护，改一处忘一处。
2. **重复代码**：chunker.py 调用、enrichment 触发、milvus-cli ingest 的编排逻辑写两遍。

这个 skill 就是两条入口的**共同下游**，确保无论知识从哪来，落盘和入库的规则只有一份。核心价值是：

- chunk frontmatter 的**唯一规范定义**（3 种 source_type 的模板和必填字段表）
- 入库顺序硬约束（raw → chunker → enrichment → ingest，不允许跳步或回填）
- content_sha256 去重机制（避免同一内容重复入库）
