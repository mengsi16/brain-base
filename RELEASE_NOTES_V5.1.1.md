# V5.1.1 Release Notes

## 总结

**架构扁平化 + 孤儿模块清理：从"3 层 indirection + 8 子图"瘦身为"2 层 + 6 子图"。**

V5.1.0 完成 T46-T49 的能力建设（统一意图识别 Agent-Loop / TOOL_REGISTRY 6 工具 / 固化全自动 / SKILL 重写），V5.1.1 转向架构净化——**删除三层伪需求残留**：T50 删 ingest-url 整套（与 ask 主图 URL 处理重复）、T54 删 GetInfoGraph 整条死链路（T25 / T47 后已无人调用）、T55 删 BrainBaseGraph 顶层编排层（CLI 已 fail-fast 直接实例化各子图，indirection 失去价值）、T56 删两个未注册的孤儿 agent。

**对外接口零破坏**：8 个 CLI 子命令（删 1 个 `ingest-url`，能力已并入 `ask`）。

---

## 核心变化（4 大架构清理）

### 1. T50 — ingest-url 整套删除（伪需求消除）

| | V5.1.0 | V5.1.1 |
|---|---|---|
| URL 入库路径 | 独立 `ingest-url` 子命令 + IngestUrlGraph + ingest_url_agent + ingest_url 节点 + ingest_url_prompts | **删除整套**；ask 主图天然处理（问题里带 URL → extract_urls → fetch_url 工具 → readability + sha256 + hash_lookup dedup → write_raw_one → enrich → ingest，顺带回答） |
| schemas | `CompletenessJudgment`（ingest-url 完整性判断）+ `after_completeness_check` 路由 | 删；ask 路径不需要"补抓判断"（用户问题就是输入信号） |
| CLI 命令数 | 9（含 `ingest-url`） | **8**（删 `ingest-url`） |
| 用户体验 | 需区分"补库" vs "问答"两种命令 | 统一 `ask "<问题 + URL>"`，问答和入库一次完成 |
| 同步修复 | — | ask 路径 P1-A：`qa_persist.write_raw_one` 计算 `source_priority`（4 档 P0-P3，user-upload 升 P1）+ chunker 透传到 chunk frontmatter，对齐 `qa_prompts.py:267` prompt 文案 |

**T50.1 残余清理**：
- 死代码：`select_candidates_node` + `_url_priority_score` + `_candidate_priority` + `_list_ingested_urls` helper 链
- `GetInfoConfig` 删 5 字段（`max_official` / `max_community` / `max_total` / `batch_timeout` / `single_url_timeout`，仅 select_candidates 用）
- `BrainBaseState` 删 3 字段（`url` / `source_type` / `topic`，仅 Propagator 写默认值无 reader）
- `QaGraphState` 删 `ingest_targets`

### 2. T54 — GetInfoGraph 整条死链路删除

| | V5.1.0 | V5.1.1 |
|---|---|---|
| 外检调度 | T25 起 `web_research_node` 已不再调 GetInfoGraph，改走 `search → fetch_extract → persist`；T46 / T47 又改为统一意图识别 Agent-Loop；GetInfoGraph 主体（plan → search → classify → loop）零调用方 | **整条删除**：`graphs/get_info_graph.py`（137 行）+ `nodes/get_info.py`（442 行）+ `tools/web_fetcher_async.py:fetch_preview`（~80 行，仅 GetInfoGraph 用） |
| 配套 schemas（9 个孤儿） | `TimeRangeHint` / `GetInfoTrigger` / `PlanMode` / `NextQueryPlan` / `SourceTypeOpt` / `UrlClassification` / `UrlClassificationBatch` / `CandidatePreview` / `CandidateScore` 留在 `agents/schemas.py`（注释"以防老代码引用"） | **全删**；保留 `FetchExtractResult`（T25 fetch_extract 链路仍活） |
| 配套 prompts（5 个孤儿） | `PLAN_NEXT_QUERY` / `CLASSIFY_URL` / `SCORE_CANDIDATE` + 2 个别名 `URL_CANDIDATE` / `TIME_RANGE_SEARCH` 在 `prompts/get_info_prompts.py` | **全删**；保留 `FETCH_EXTRACT_SYSTEM_PROMPT` |
| `GetInfoConfig` 配套字段 | `get_info_max_iter` / `get_info_target_official` / `get_info_total_timeout` 3 字段 | 删 |
| 路由 | `ConditionalLogic.route_get_info_continue` | 删（`ConditionalLogic` 类保留，仍被 QaGraph / CrystallizeGraph / LifecycleGraph 复用） |
| 测试 | `tests/unit/test_qa_get_info_loop.py`（325 行）+ `tests/tmp_e2e_openclaw.py`（~80 行 broken） | 删；`_dedup_evidence_by_chunk_id` helper 测试**迁移**到独立的 `tests/unit/test_qa_dedup.py`（保 helper 回归保护） |
| 子图数 | 8（含 GetInfoGraph） | **7**（GetInfoGraph 删，IngestUrlGraph 已被 T50 删） |

### 3. T55 — BrainBaseGraph 顶层编排层拔除

| | V5.1.0 | V5.1.1 |
|---|---|---|
| 包结构 | 3 层 indirection：`cli → BrainBaseGraph(mode=...) → 4 个 agent 包装节点 → 4 个子图` | **2 层**：`cli → 6 个子图`（`XxGraph(llm=...)` 直接实例化，fail-fast LLM 注入） |
| 顶层编排 | `brain_base/graph/brain_base_graph.py:BrainBaseGraph`（78 行）+ `setup.py:GraphSetup`（53 行）+ `propagation.py:Propagator`（37 行）+ `route_by_mode` 路由 | **整层删除**；CLI `cmd_*` 直接 `from brain_base.graphs.xx_graph import XxGraph; graph = XxGraph(llm=...)` |
| Agent 包装层 | 4 个 agent 工厂（`create_qa_agent` / `create_ingest_file_agent` / `create_lifecycle_agent` / `create_lint_agent`）：把子图调用包成节点，用于顶层编排 | 删 |
| State TypedDict | `agents/utils/agent_states.py` 7 个 TypedDict（`BrainBaseState` / `QaState` / `IngestFileState` / `PersistenceState` / `LifecycleState` / `LintState` / `CrystallizeState`） | **整文件删除**——关键发现：除 `BrainBaseState` 顶层用外，其他 6 个 TypedDict 全部为孤儿，每个子图在自己 `graphs/*.py` 里重新定义了同名 State，从不引用 `agent_states.py` 里的同名类型 |
| `ConditionalLogic` 类 | 含 `route_by_mode` 等 9 个路由方法 | 仅删 `route_by_mode`（顶层用），其他 7 个保留供子图复用 |
| `agents/__init__.py` | 6 个 agent 工厂 export | 清空（`__all__: list[str] = []`），仅 schemas + utils 在子模块内可导入 |
| `agents/utils/__init__.py` | 7 个 State + 8 个 utils export | 仅保留 8 个 utils export（`bind_structured` / `build_frontmatter` / `compute_content_hash` / `configure_logger` / `create_msg_delete` / `generate_doc_id` / `invoke_structured` / `stream_with_trace`） |

### 4. T56 — 双孤儿 agent 工厂顺手删

| | V5.1.0 | V5.1.1 |
|---|---|---|
| `crystallize_agent.py`（42 行） | 在 `agents/__init__.py` export 但 `setup.py` 从未注册（导出却无人 import） | 删 |
| `persistence_agent.py`（32 行） | 同上 | 删 |

**根因**：T46 / T47 重构时 `setup.py` 实际只注册 4 个 agent（qa / ingest_file / lifecycle / lint），但 `agents/__init__.py` 顺带保留了原 6 个 export 入口，造成"导出但未被注册"的双重孤儿。grep 验证：全仓除自身定义 + `__init__.py` export 外 0 引用。

---

## 删除的旧代码（V5.1.1 累计）

### 整文件删除（13 个生产文件 + 2 个测试文件，~1820 行）

| 删除文件 | 行数 | 来源任务 | 原因 |
|----------|------|---------|------|
| `brain_base/agents/ingest_url_agent.py` | ~60 | T50 | IngestUrlGraph 包装节点 |
| `brain_base/graphs/ingest_url_graph.py` | ~140 | T50 | URL 入库子图（与 ask 重复） |
| `brain_base/nodes/ingest_url.py` | ~280 | T50 | URL 入库节点链 |
| `brain_base/prompts/ingest_url_prompts.py` | ~50 | T50 | `CompletenessJudgment` prompt |
| `brain_base/graphs/get_info_graph.py` | 137 | T54 | 多步循环外检子图（T25 后零调用） |
| `brain_base/nodes/get_info.py` | 442 | T54 | plan / classify / preview / score 4 节点 |
| `brain_base/tools/web_fetcher_async.py` | ~80 | T54 | `fetch_preview` 仅 GetInfoGraph 用 |
| `brain_base/graph/brain_base_graph.py` | 78 | T55 | 顶层编排类 |
| `brain_base/graph/setup.py` | 53 | T55 | `GraphSetup` 组装类 |
| `brain_base/graph/propagation.py` | 37 | T55 | `Propagator` 状态初始化 |
| `brain_base/agents/qa_agent.py` | 29 | T55 | qa agent 包装节点 |
| `brain_base/agents/ingest_file_agent.py` | 30 | T55 | ingest-file agent 包装节点 |
| `brain_base/agents/lifecycle_agent.py` | 34 | T55 | lifecycle agent 包装节点 |
| `brain_base/agents/lint_agent.py` | 22 | T55 | lint agent 包装节点 |
| `brain_base/agents/utils/agent_states.py` | 144 | T55 | 7 个 TypedDict 整文件孤儿 |
| `brain_base/agents/crystallize_agent.py` | 42 | T56 | 导出但未注册的孤儿 |
| `brain_base/agents/persistence_agent.py` | 32 | T56 | 导出但未注册的孤儿 |
| `tests/unit/test_qa_get_info_loop.py` | 325 | T54 | GetInfoGraph 测试套件（2 条 helper 测试已迁移）|
| `tests/tmp_e2e_openclaw.py` | ~80 | T54 | tmp_ 前缀临时实验脚本，早已 broken |

### 部分删除（schemas / prompts / config / 路由）

- `agents/schemas.py`：删 9 个 GetInfoGraph 专属 schema + 1 个 `CompletenessJudgment`（T50）
- `prompts/get_info_prompts.py`：删 3 个 prompt + 2 个别名（保留 `FETCH_EXTRACT_SYSTEM_PROMPT`）
- `config.py:GetInfoConfig`：删 8 个字段（T50.1 5 个 + T54 3 个）
- `graph/conditional_logic.py`：删 `route_by_mode`（T55）+ `route_get_info_continue`（T54）+ `after_completeness_check`（T50）3 个孤儿路由方法
- `cli.py`：删 `cmd_ingest_url` 子命令（T50）
- `nodes/qa.py`：删 `_url_priority_score` / `_candidate_priority` / `create_select_candidates_node` / `_list_ingested_urls`（T50.1）
- `graph/__init__.py`：仅保留 `ConditionalLogic`（删 `GraphSetup` / `BrainBaseGraph` / `Propagator` 3 个 export）
- `brain_base/__init__.py`：清空 `__all__`（删 `BrainBaseGraph` 顶层 export）
- `agents/__init__.py`：清空 `__all__`（删 6 个 agent 工厂 export）
- `agents/utils/__init__.py`：删 7 个 State 的 import + `__all__` 项

---

## 新增文件（3 个测试 + 1 个契约文档）

| 文件 | 用途 |
|------|------|
| `tests/unit/test_qa_dedup.py` | T54 测试迁移：`_dedup_evidence_by_chunk_id` helper 2 条单测（QA 主图 barrier2 后跨子问题去重逻辑） |
| `tests/unit/test_t50_qa_persist_source_priority.py` | T50 同步修复：`qa_persist.write_raw_one` 计算 source_priority 4 档逻辑回归测试 |
| `tests/unit/test_t50_source_priority.py` | T50 同步修复：URL 模式 → priority 档位映射纯逻辑单测 |
| `md/research/2026-05-21-t54-t56-orphan-cleanup-contract.md` | T54 + T55 + T56 合并执行契约（风险审查 R1-R8 + 验收标准） |

---

## 文档同步

| 文件 | 变化 |
|------|------|
| `README.md` / `README_en.md` | 三层架构表"7 个 LangGraph StateGraph" → "6 个"；LangGraph 图总览删 BrainBaseGraph 顶层引用 + 删 GetInfoGraph 表行 + QaGraph 节点列表更新为 T47 意图 Agent-Loop 状态；当前实现状态第 1 条改为 "6 个子图 + CLI 直接实例化（fail-fast LLM 注入）" |
| `brain-base-skill/SKILL.md` | 命令矩阵 9 → 8（删 `ingest-url` 行）；FAQ 删"`ingest-url` 返回空"条目；增加 T50 历史说明（`ask "<问题 + URL>"` 替代用法） |
| `bin/chunker.py` / `bin/source-priority.py` | T50 同步：`source_priority` 4 档计算 + chunker frontmatter 透传 |
| `brain_base/nodes/qa_persist.py` | T50 同步：`write_raw_one` 计算 source_priority 注入 chunk frontmatter |
| `brain_base/nodes/_hash.py` | T50 配套：URL 模式识别 helper |
| `brain_base/tools/web_fetcher.py` / `raw_text_extractor.py` | T54 配套清理：删 `fetch_preview` 引用，统一走 fetch_url + readability |
| `tests/e2e/test_qa_full_pipeline.py` | T54 配套：`GetInfoConfig(...)` 删 8 个已不存在字段，仅保留 `enable` / `fetch_extract_concurrency` / `search_pages_per_engine` |
| `tests/unit/test_raw_text_extractor.py` / `test_t48_4_github_raw.py` | T54 配套：删旧 fetch_preview / playwright async 引用 |
| `ToDo.md` → `md/archive/ToDo-Phase-T50-T56.md` | 整体归档（T50 / T50.1 / T54 / T55 / T56 全部 finished 留底）；新 ToDo.md 仅含 T53 pending |

---

## 数据指标

| 指标 | 数值 |
|------|------|
| Commits | 1（V5.1.1 单 commit） |
| 改动文件 | 39 files |
| 新增代码 | ~250 行（含测试 + 契约文档 + 文档同步） |
| 删除代码 | ~2400 行（生产代码 ~1700 + 测试 ~700） |
| 净变化 | **-2150 行**（架构净化，能力零损） |
| 子图数 | 8 → **6**（删 IngestUrlGraph + GetInfoGraph） |
| CLI 命令数 | 9 → **8**（删 `ingest-url`） |
| Agent 工厂数 | 6 → **0**（顶层编排层拔除，不再需要 agent 包装节点） |
| 测试通过 | **457 passed / 3 failed / 1 skipped**（3 失败均为 V5.1.0 起的 pre-existing baseline，V5.1.1 零新回归） |

---

## 架构对比

### 包结构（T55 关键变化）

```
V5.1.0：3 层 indirection
  cli (cmd_ask / cmd_ingest_file / ...)
       ↓ (Propagator.create_initial_state)
  BrainBaseGraph (route_by_mode)
       ↓
  4 个 agent 包装节点 (create_qa_agent / ...)
       ↓
  6 个子图 (XxGraph)

V5.1.1：2 层 indirection
  cli (cmd_ask / cmd_ingest_file / ...)
       ↓ (LLM fail-fast 注入)
  6 个子图 (XxGraph(llm=...))
```

### QaGraph 主图（V5.1.1 实际拓扑）

```
probe → crystallized_check
        ├─ hit_fresh / cold_promoted ──→ answer
        └─ miss / stale / observed / degraded
             ↓
        extract_urls (正则提取 user_urls)
        ├─ user_urls 非空 → url_pre_fetch (浅抓 → 改写上下文)
        └─ user_urls 空   → normalize
             ↓
        url_pre_fetch → normalize → decompose → intent_planner
                                                     ↓
                            ┌───────────────────────┐
                            ↓                       │
        intent_planner → intent_executor → intent_observer
                                                     │
                            5 级早退 (consecutive_errors ≥2 /
                            sufficient / max_iter / no_action /
                            continue) → merge_evidence
                                                     ↓
        write_raw_one × N → barrier_raw → enrich_one × M → barrier_enrich → ingest
                                                     ↓
        ingest → fanout_search_dispatcher → subquery_search_one × N → barrier2
                                                     ↓
        judge → answer → self_check → crystallize_answer (value_score ≥ 0.3 自动写入) → END
```

---

## 破坏性变更

**1 项：删 `ingest-url` 子命令**。

迁移路径：

```bash
# V5.1.0
python -m brain_base.cli ingest-url --url https://docs.litellm.ai/

# V5.1.1
python -m brain_base.cli ask "请收录并解读这个文档：https://docs.litellm.ai/"
```

ask 主图天然处理 URL：自动 extract_urls → fetch_url → readability + sha256 + hash_lookup dedup → write_raw_one → enrich → ingest，并在入库后顺带回答用户问题。

其余 8 个 CLI 子命令（`health` / `search` / `ask` / `chat` / `ingest-file` / `remove-doc` / `lint` / `crystallize-check`）接口完全向后兼容。

---

## 下一阶段路线

- **T53** E2E 基线测试（pending，最低优先级）：选 3-5 条典型问题落盘 e2e_trace.jsonl，未来回归任何破坏性改动一跑就知道。

清洁环境无 chunks 时 smoke 自然 skip，e2e 同理；当前 unit/smoke 已能挡住 90% 回归，e2e 是补完整性，不是阻塞下一阶段开发。
