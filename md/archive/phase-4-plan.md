# Phase 4 执行计划：QaGraph 主图 + CrystallizeGraph

## 目标

实现 QA 主图和 Crystallize 子图，替代旧的 qa-workflow / crystallize-workflow。

## backup 中的已有能力

- `skills/qa-workflow/SKILL.md`：完整 QA 流程（13 步）
- `skills/crystallize-workflow/SKILL.md`：固化层命中判断 + 写入
- `bin/milvus-cli.py`：`multi_query_search()`、`hybrid_search()`、`dense_search()`、`rerank()`
- `bin/crystallize-cli.py`：固化层 CLI 操作

## 架构设计

### 新增文件

| 文件 | 职责 |
|------|------|
| `brain_base/graphs/qa_graph.py` | QaGraph 类 |
| `brain_base/graphs/crystallize_graph.py` | CrystallizeGraph 类 |
| `brain_base/nodes/qa.py` | QA 节点函数 |
| `brain_base/nodes/crystallize.py` | 固化层节点函数 |

### QaGraph 节点与流程

```
probe_infra → check_crystallized ──(hit_fresh)──→ END
                  │                    │
                  │(miss/degraded)      │(hit_stale)
                  ↓                    ↓
            normalize_query         refresh_crystallized
                  │                    │
                  ↓                    ↓
            decompose_query           ↓
                  │                    │
                  ↓                    ↓
            rewrite_query             ↓
                  │                    │
                  ↓                    ↓
            search_evidence           ↓
                  │                    │
                  ↓                    ↓
            judge_evidence ──(sufficient)──→ generate_answer
                  │                              │
                  │(insufficient)                 ↓
                  ↓                         format_answer
            trigger_get_info                    │
                  │                              ↓
                  ↓                         crystallize_answer
            re_search ─────────────────────→ END
```

简化版（先实现核心链路，条件边后续迭代）：

```
probe → crystallized_check → normalize → rewrite → search → judge → answer → crystallize → END
```

### CrystallizeGraph 节点

```
hit_check ──(miss)──→ END
    │
    │(hit)
    ↓
freshness_check ──(fresh)──→ END
    │
    │(stale)
    ↓
refresh ──→ END
```

独立写入模式：`crystallize_write` 节点（由 QaGraph 末尾调用）

### QaState

```python
class QaState(TypedDict, total=False):
    question: str
    infra_status: dict
    crystallized_status: str  # hit_fresh / hit_stale / cold_observed / miss / degraded
    crystallized_answer: str
    normalized_query: str
    sub_queries: list[str]
    rewritten_queries: list[str]  # L0-L3 查询变体
    evidence: list[dict]
    evidence_sufficient: bool
    answer: str
    formatted_answer: str
    error: str
```

### CrystallizeState

```python
class CrystallizeState(TypedDict, total=False):
    mode: str  # hit_check / crystallize
    user_question: str
    extracted_entities: list[str]
    status: str  # hit_fresh / hit_stale / cold_observed / miss / degraded
    skill_id: str
    answer_markdown: str
    value_score: float
    layer: str  # hot / cold
```

## 计数审查

| 文件 | 预估行数 |
|------|---------|
| `brain_base/graphs/qa_graph.py` | ~120 |
| `brain_base/graphs/crystallize_graph.py` | ~80 |
| `brain_base/nodes/qa.py` | ~180 |
| `brain_base/nodes/crystallize.py` | ~100 |
| **合计** | **~480 行，4 个文件** |

## 验证标准

```bash
python -c "from brain_base.graphs.qa_graph import QaGraph; g = QaGraph(); print('QaGraph OK')"
python -c "from brain_base.graphs.crystallize_graph import CrystallizeGraph; g = CrystallizeGraph(); print('CrystallizeGraph OK')"
```
