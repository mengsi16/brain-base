# Phase 2 执行计划：KnowledgePersistence 子图

## 目标

实现 chunk → enrich → milvus_ingest 子图，两条入库路径的共同下游。

## backup 中的已有能力

- `bin/chunker.py`：确定性 Markdown 分块（H2/H3 标题切分 + 递归字符切分）
- `bin/milvus-cli.py ingest_chunks()`：chunk + question 行向量入库
- `skills/chunk-enrichment/SKILL.md`：LLM 信息富化（title/summary/keywords/questions）
- `skills/knowledge-persistence/SKILL.md`：完整流程定义

## 架构设计（参考 TradingAgents）

参考 TradingAgents 的 graph setup / propagator / conditional_logic 分离模式：

### 新增文件

| 文件 | 职责 |
|------|------|
| `brain_base/graphs/persistence_graph.py` | PersistenceGraph 类：graph setup + compile |
| `brain_base/nodes/persistence.py` | 节点函数：chunk / enrich / milvus_ingest |

### PersistenceState（跟 graph 走，不单独抽）

```python
class PersistenceState(TypedDict, total=False):
    raw_md_path: str          # raw Markdown 路径
    doc_id: str
    chunk_dir: str            # chunks 输出目录
    chunk_files: list[str]    # 生成的 chunk 文件路径
    enriched: bool            # enrichment 是否完成
    milvus_inserted: int      # Milvus 入库行数
    error: str
```

### 节点设计

1. **chunk_node**：调用 `bin/chunker.py` 生成 chunk Markdown
2. **enrich_node**：调用 LLM 为每个 chunk 生成 title/summary/keywords/questions
3. **ingest_node**：调用 `bin/milvus-cli.py ingest-chunks` 完成 hybrid 入库

### 图结构

```
chunk_node → enrich_node → ingest_node → END
```

线性图，无条件边。

## 计数审查

| 文件 | 预估行数 |
|------|---------|
| `brain_base/graphs/persistence_graph.py` | ~80 |
| `brain_base/nodes/persistence.py` | ~100 |
| **合计** | **~180 行，2 个文件** |

## 验证标准

```bash
python -c "from brain_base.graphs.persistence_graph import PersistenceGraph; g = PersistenceGraph(); print('graph compiled OK')"
```
