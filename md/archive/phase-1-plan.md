# Phase 1 执行计划：brain_base 包骨架

## 目标

搭建 `brain_base/` Python 包骨架，包含 State 定义、Config、Checkpointer。

## 改造步骤

### 1.1 新增 `brain_base/__init__.py`
- 空文件，仅声明包

### 1.2 新增 `brain_base/config.py`
- 定义 `DEFAULT_CONFIG` 字典，字段参考 `../brain-base-backup/bin/milvus_config.py`：
  - `llm_provider`: "anthropic"
  - `deep_think_llm`: "claude-sonnet-4-20250514"
  - `quick_think_llm`: "claude-sonnet-4-20250514"
  - `data_dir`: 项目根/data
  - `milvus_uri`: "http://localhost:19530"
  - `milvus_collection`: "knowledge_base"
  - `embedding_provider`: "bge-m3"
  - `retrieval_mode`: "hybrid"
  - `search_top_k`: 10
  - `search_top_k_per_query`: 20
  - `rrf_k`: 60
  - `use_rerank`: True
  - `crystallized_freshness_ttl_days`: 30
  - `checkpoint_enabled`: True

### 1.3 新增 `brain_base/state.py`
- 定义所有 State TypedDict：
  - `QaState(MessagesState)` — QA 主图状态
  - `IngestFileState(TypedDict)` — 文件入库状态
  - `IngestUrlState(TypedDict)` — URL 入库状态
  - `CrystallizedHitState(TypedDict)` — 固化层命中状态
  - `PersistenceState(TypedDict)` — 持久化管道状态
  - `LifecycleState(TypedDict)` — 生命周期管理状态
- 参考 `../brain-base-backup/skills/qa-workflow/SKILL.md` 中的状态字段

### 1.4 新增 `brain_base/checkpointer.py`
- 参考 TradingAgents 的 checkpointer.py 模式
- `get_checkpointer(data_dir, session_id)` → SqliteSaver context manager
- `thread_id(session_id)` → 确定性 thread_id
- `clear_checkpoint(data_dir, session_id)` → 清理

### 1.5 新增 `brain_base/graphs/__init__.py` 和 `brain_base/nodes/__init__.py`
- 空文件

## 计数审查

| 文件 | 预估行数 |
|------|---------|
| `brain_base/__init__.py` | 0 |
| `brain_base/config.py` | ~50 |
| `brain_base/state.py` | ~120 |
| `brain_base/checkpointer.py` | ~60 |
| `brain_base/graphs/__init__.py` | 0 |
| `brain_base/nodes/__init__.py` | 0 |
| **合计** | **~230 行，7 个文件** |

工作量合理，开始执行。

## 验证标准

```bash
python -c "from brain_base.config import DEFAULT_CONFIG; print(DEFAULT_CONFIG['llm_provider'])"
# 预期输出: anthropic

python -c "from brain_base.state import QaState; print(list(QaState.__annotations__.keys()))"
# 预期输出: 所有字段名列表
```
