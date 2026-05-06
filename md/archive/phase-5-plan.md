# Phase 5 执行计划：LifecycleGraph + LintGraph + CLI 重写

## 目标

1. **LifecycleGraph**：文档生命周期管理（删除/归档），跨存储一致性
2. **LintGraph**：固化层清理（降级/删除过期/被 rejected 的条目）
3. **CLI 重写**：用 LangGraph 图替代旧的 brain-base-cli.py 的 claude-code agent 调度

## backup 中的已有能力

- `skills/lifecycle-workflow/SKILL.md`：完整删除流程（dry-run + confirm 两阶段）
- `skills/crystallize-lint/SKILL.md`：固化层周期清理
- `bin/milvus-cli.py`：`delete_by_doc_ids()`、`hash_lookup()`、`show_doc()`
- `bin/brain-base-cli.py`：11 条命令的 CLI 入口

## 架构设计

### 新增文件

| 文件 | 职责 |
|------|------|
| `brain_base/graphs/lifecycle_graph.py` | LifecycleGraph 类 |
| `brain_base/graphs/lint_graph.py` | LintGraph 类 |
| `brain_base/nodes/lifecycle.py` | 生命周期管理节点函数 |
| `brain_base/nodes/lint.py` | 固化层清理节点函数 |
| `brain_base/cli.py` | 新 CLI 入口 |

### LifecycleGraph 节点

```
resolve_doc_ids → scan_impact → dry_run_report → [confirm?] → delete_milvus → delete_files → clean_index → audit_log → END
```

### LintGraph 节点

```
scan_crystallized → check_freshness → degrade_expired → delete_rejected → END
```

### CLI 命令

用 argparse 直接调用 LangGraph 图，不再走 claude-code agent 调度：
- `ask` → QaGraph.run()
- `ingest-file` → IngestFileGraph.run()
- `ingest-url` → IngestUrlGraph.run()
- `remove-doc` → LifecycleGraph.run()
- `search` → milvus_cli.multi_query_search()
- `health` → probe_node()
- `lint` → LintGraph.run()

## 计数审查

| 文件 | 预估行数 |
|------|---------|
| `brain_base/graphs/lifecycle_graph.py` | ~60 |
| `brain_base/graphs/lint_graph.py` | ~50 |
| `brain_base/nodes/lifecycle.py` | ~120 |
| `brain_base/nodes/lint.py` | ~80 |
| `brain_base/cli.py` | ~200 |
| **合计** | **~510 行，5 个文件** |

## 验证标准

```bash
python -c "from brain_base.graphs.lifecycle_graph import LifecycleGraph; g = LifecycleGraph(); print('Lifecycle OK')"
python -c "from brain_base.graphs.lint_graph import LintGraph; g = LintGraph(); print('Lint OK')"
python -c "from brain_base.cli import main; print('CLI OK')"
```
