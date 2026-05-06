# Phase 3 执行计划：IngestFile + IngestUrl 图

## 目标

实现两条入库路径的 LangGraph 图：
1. **IngestFileGraph**：本地文件上传 → doc-converter 转换 → frontmatter 组装 → knowledge-persistence
2. **IngestUrlGraph**：URL 抓取 → HTML 清洗 → frontmatter 组装 → knowledge-persistence

两条路径在最后一步汇合到 PersistenceGraph（Phase 2 已完成）。

## backup 中的已有能力

- `bin/doc-converter.py`：格式转换（MinerU/pandoc/plain/code 等）
- `skills/upload-ingest/SKILL.md`：文件上传入库完整流程
- `skills/content-cleaner-workflow/SKILL.md`：URL 抓取清洗入库完整流程
- `bin/playwright-cli-ops`：Playwright 页面抓取（外部 CLI）

## 架构设计

### 新增文件

| 文件 | 职责 |
|------|------|
| `brain_base/graphs/ingest_file_graph.py` | IngestFileGraph 类 |
| `brain_base/graphs/ingest_url_graph.py` | IngestUrlGraph 类 |
| `brain_base/nodes/ingest_file.py` | 文件入库节点函数 |
| `brain_base/nodes/ingest_url.py` | URL 入库节点函数 |

### IngestFileState

```python
class IngestFileState(TypedDict, total=False):
    input_files: list[str]      # 输入文件路径
    upload_date: str            # ISO 日期
    converted: list[dict]       # doc-converter 返回的 results
    conversion_errors: list[dict]
    raw_paths: list[str]        # 写好 frontmatter 的 raw 文件
    persistence_results: list[dict]  # PersistenceGraph.run() 返回值
    error: str
```

### IngestUrlState

```python
class IngestUrlState(TypedDict, total=False):
    url: str
    source_type: str           # official-doc / community
    topic: str
    title_hint: str
    raw_content: str           # 抓取的原始内容
    cleaned_md: str            # 清洗后的 Markdown
    raw_md_path: str           # 写入的 raw 文件路径
    doc_id: str
    persistence_result: dict   # PersistenceGraph.run() 返回值
    extraction_status: str     # ok / spa-failed / insufficient-content
    error: str
```

### IngestFileGraph 节点

1. **convert_node**：调用 `bin/doc-converter.py convert` 转换文件
2. **frontmatter_node**：为每个 raw MD 组装 user-upload frontmatter
3. **persist_node**：调用 PersistenceGraph.run() 完成分块入库

```
convert_node → frontmatter_node → persist_node → END
```

### IngestUrlGraph 节点

1. **fetch_node**：调用 playwright-cli-ops 抓取页面
2. **clean_node**：HTML → Markdown 清洗
3. **frontmatter_node**：组装 official-doc/community frontmatter
4. **persist_node**：调用 PersistenceGraph.run() 完成分块入库

```
fetch_node → clean_node → frontmatter_node → persist_node → END
```

## 计数审查

| 文件 | 预估行数 |
|------|---------|
| `brain_base/graphs/ingest_file_graph.py` | ~60 |
| `brain_base/graphs/ingest_url_graph.py` | ~70 |
| `brain_base/nodes/ingest_file.py` | ~90 |
| `brain_base/nodes/ingest_url.py` | ~100 |
| **合计** | **~320 行，4 个文件** |

## 验证标准

```bash
python -c "from brain_base.graphs.ingest_file_graph import IngestFileGraph; g = IngestFileGraph(); print('IngestFile OK')"
python -c "from brain_base.graphs.ingest_url_graph import IngestUrlGraph; g = IngestUrlGraph(); print('IngestUrl OK')"
```
