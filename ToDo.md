# ToDo — brain-base

> 当前阶段：Phase 28+。历史 phase 已归档至 `md/archive/ToDo-Phase-N-M.md`（最近：`ToDo-Phase-T27-T27.md` 含 T27 QA 节点 fail-fast 改造完整记录）。
> **本文件只放 pending / executing 任务**。任务完成后等下一阶段开新任务时整体归档。

## 任务编号 = 优先级位置（重要）

- **任务编号代表优先级位置，不是时间戳**：编号越小优先级越高，越靠前。
- **高优先级永远放前面**：新加入的任务如果优先级高于现有 pending 任务，应**插队**到合适编号位置，把低优先级任务**顺延**到后面编号。
- **低优先级可以被插队顺延**：当一个高优先级任务出现，低优先级任务的编号会被**重新分配**给后面（编号增大），不要恋编号。
- **finished 任务编号不可变**：已归档的任务编号是历史档案，重排只发生在 pending 任务之间。

## 归档规则

- 每次开启新阶段（即将把第一个 `pending` 转为 `executing`）前，先用 `Move-Item` 把当前 ToDo.md 整体归档到 `md/archive/ToDo-Phase-{起始任务号}-{结束任务号}.md`，然后新建只含 pending 的 ToDo.md。
- **禁止用 edit 直接改旧内容**——改了旧内容失去回溯能力，下个 Agent 看不到完整决策上下文。
- 用命令移（PowerShell `Move-Item`），不要用 edit 工具搬运。
- 同一阶段内 pending → executing → finished 的状态推进可以 edit 同一文件，但任务条目本身不能删。

---

## 当前架构进度（CLAUDE.md 主流程图对照）

CLAUDE.md `@/CLAUDE.md:60-86` 里画的"QA 主流程框架"共 4 大块，**全部完成**（T28 兑现 PIPE2）：

| 大块 | 设计 | 当前实现 | 状态 |
|------|------|---------|------|
| 入口段 | `Q → normalize → decompose → DISP1` | `probe → crystallized_check → normalize → decompose → fanout_prep_dispatcher` | ✅ T23 |
| **PIPE1** 第一段子图 | rewrite + grep（每子问题）→ barrier 1 | `subquery_prep` × N → `barrier1` | ✅ T23 |
| **GI** get_info_block | 黑盒（外检 + 入库）| 7 节点完整流水（search_web_dual / fetch_extract / write_raw / barrier_raw / enrich / barrier_enrich / ingest）| ✅ T25 + T26.1 |
| **PIPE2** 第二段子图 | `DISP2 (fanout_search) → 每子问题独立 milvus + rerank → B2 (barrier2 聚合 evidence)` | `fanout_search_dispatcher` → `subquery_search_one` × N → `barrier2` | ✅ T28 |

CLAUDE.md 主流程图 4 大块**全部完成**。后续：T29 e2e baseline（executing）→ T30 sparse gate 替换 grep gate（**插队**，T29 e2e 暴露的 grep 字面 vs 语义不匹配）→ T31 arxiv 通道（原 T30 顺延）→ T32 chunker dedup（原 T31 顺延）。

---

## T28 PIPE2 重构（第二段子图：fanout_search × N + 每子问题独立 milvus + rerank + barrier2）— finished

> 执行计划：`@/md/research/2026-05-09-t28-pipe2-contract.md`
>
> **实际产出**（2026-05-09）：
> - **CLAUDE.md / AGENTS.md 规则 25 补丁**：「所有保留下来的 try-except 必须 logger 打错误信息（含异常类型 + message 截断 + 关键上下文）」。顺手补上两处现有无 log 的保留 try-except：structured.py 路径 1 + qa_get_info.py fetch_extract_one outer except。
> - **`brain_base/config.py:GetInfoConfig`**：加 `search_concurrency: int = 3` 字段。
> - **`brain_base/nodes/qa_search.py`**（新建 ~250 行）：SearchState TypedDict + `_get_search_semaphore` lazy create + `fanout_search_dispatcher` （1 重 gate：sub_queries 空 / sub_questions 空 / 长度不一致均防御性短路 barrier2） + `create_subquery_search_one` async 工厂（调 multi_query_search(use_rerank=True)，outer try/except + logger.warning fan-out 隔离） + `barrier2_node` 同步聚合（按 sub_idx 排序展开 + 加 sub_idx / sub_question / source / match_type 标签）。
> - **`brain_base/graphs/qa_graph.py`**：imports 加 qa_search 3 项 + 删 legacy_dense_search_node；QaState 加 sub_evidence (Annotated[list[dict], add]) + search_errors；删 legacy_dense_search 节点注册 + 两老边 (ingest→LDS / LDS→judge)；fanout_persist / fanout_enrich 短路目标 legacy_dense_search → ingest；加节点 subquery_search_one + barrier2 + 条件边 (ingest→fanout_search_dispatcher→barrier2) + 边 (subquery_search_one→barrier2 / barrier2→judge)；run() 初始化加 sub_evidence=[] / search_errors=[]。
> - **`brain_base/nodes/qa.py`**：删 `legacy_dense_search_node` 函数体（保留顶部 import 不动——`re_search_node` 等老死代码仍用，待后续清债）。
> - **`brain_base/nodes/qa_persist.py`**：fanout_persist_dispatcher / fanout_enrich_dispatcher 返回字符串改为 `"ingest"`（与 qa_graph.py mapping 对齐）；顶部 docstring 同步。
> - **`tests/unit/test_qa_search_pipe2.py`**（新建）：13 个 PIPE2 单元测试（dispatcher gate × 3 / subquery_search_one 成功·空 queries·失败隔离·queries 截断 6·empty text 过滤·Semaphore × 6 / barrier2 flatten·错误聚合·空状态·非 dict chunk 过滤 × 4）。
> - **`tests/unit/test_qa_graph_t28.py`**（新建）：5 个主图集成测试（新节点注册 / legacy_dense_search 删干净 / 老边删 / 新边加 / run() 初始化字段）。
> - **`tests/unit/test_qa_graph_t25.py` / `test_qa_graph_t26.py` / `test_qa_persist_write_raw.py` / `test_qa_persist_enrich.py`**：同步 dispatcher 短路目标断言 (`"legacy_dense_search"` → `"ingest"`) + 老 `legacy_dense_search` in nodes 断言改成 not in / 移除。
> - **CLAUDE.md / AGENTS.md 同步**：get_info_block 内部展开图末尾 LDS 节点换为 fanout_search_dispatcher → subquery_search_one → barrier2 · 3 条 PIPE2 关键约束（每子问题独立 top-K · fan-out 隔离 logger · barrier2 sub_idx 排序 flatten）· docstring 「主图继续走 legacy_dense_search」换为「走 PIPE2」。
>
> **验收**：`pytest tests/unit tests/smoke -q` 全绿（**278 pass + 1 skip**，补丁后增量：258 → 278 ≈ +13 PIPE2 单元测试 + 5 主图集成测试 + 2 补丁 logger 后原原有测试覆盖位置变化）。
>
> 关键决策（用户已确认）：
> - subquery_search_one outer try/except 保留（fan-out 单 Send 失败隔离，规则 25 允许）+ 必须 `logger.warning`
> - CLAUDE.md / AGENTS.md 规则 25 补丁：所有保留 try-except 必须 logger 打错误信息
> - 顺手补：structured.py 路径 1 / qa_get_info.py fetch_extract_one outer except 已补上 logger

> **CLAUDE.md 主流程图最后一处空白**。当前 `legacy_dense_search` 是 T23 引入的临时桥接节点，
> 注释明写"chunk 阶段后续 Phase 重构"——T28 即兑现该承诺。

### 为什么要做：举例说明

**用户问**："`RAG-Anything 是什么？怎么用？`" → decompose 拆出 2 个子问题：
- 子问题 0：`RAG-Anything 是什么？`（rewrite 出 4 个改写：`["RAG-Anything", "RAG-Anything 框架", "HKUDS RAG", "multimodal RAG"]`）
- 子问题 1：`RAG-Anything 怎么用？`（rewrite 出 4 个改写：`["RAG-Anything 用法", "RAG-Anything 教程", "RAG-Anything quickstart", "RAG-Anything install"]`）

#### 当前实现（`legacy_dense_search` 扁平搜索）

```
rewritten_queries = ["RAG-Anything", "RAG-Anything 框架", "HKUDS RAG",
                     "multimodal RAG", "RAG-Anything 用法", "RAG-Anything 教程",
                     "RAG-Anything quickstart", "RAG-Anything install"]
                     # 8 个 query 一次性 join 进 Milvus

milvus.hybrid_search(queries=8个) → top-K=10 chunks（不分子问题）

evidence = [
    {chunk_id: "...-001", score: 0.95, content: "RAG-Anything is a multimodal..."},  # 关于"是什么"
    {chunk_id: "...-002", score: 0.93, content: "RAG-Anything 由 HKUDS 团队..."},   # 关于"是什么"
    ...
    {chunk_id: "...-009", score: 0.73, content: "pip install rag-anything..."},    # 关于"怎么用"（仅此 1 条）
]
# 9/10 都是讲"是什么"，"怎么用"只拿到 1 条 → answer 节点回答"怎么用"时证据严重不足
```

**问题**：子问题 0 的强匹配 chunks 把子问题 1 的弱匹配 chunks **挤掉了**——milvus 没有"每个子问题保证 top-K"的语义，只有"全局 top-K"。

#### 目标实现（PIPE2：每子问题独立子图）

```
fanout_search_dispatcher → 发 N 个 Send（每子问题各 1 个）

每 Send 内部（async + Semaphore=3 限流）：
    multi_query_search(queries=该子问题的改写, use_rerank=True)
        → milvus hybrid (dense + sparse + RRF) → bge-reranker-v2-m3 重排 → top-10
    返回 {"sub_evidence": [{sub_idx, sub_question, chunks: [...]}]}

barrier2 聚合：
    sub_evidence (reducer add) → flatten 加 sub_idx / sub_question 标签
    → evidence = [10 条子问题 0 + 10 条子问题 1 + ...]
```

**优势**：
1. **证据隔离**：每个子问题保证拿到自己的 top-K，不会被强子问题挤掉
2. **rerank 加持**：cross-encoder 在 RRF 后做 (query, chunk) 精排，召回精度比纯 RRF 高
3. **sub_idx 标签**：answer 节点可以"按子问题组织答案"——不互相污染

### 范围

**新建** `brain_base/nodes/qa_search.py`（约 200 行）：

- `SearchState` TypedDict：子图局部 state，含 `sub_idx: int / sub_question: str / queries: list[dict]`
- `fanout_search_dispatcher`（条件边）：
  - gate：`sub_queries` 空 → 短路 `barrier2`
  - 非空 → 发 N 个 Send 到 `subquery_search_one`
- `create_subquery_search_one(config)` 工厂（async，Semaphore）：
  - 入参 SearchState，出 `{"sub_evidence": [{sub_idx, sub_question, chunks, error?}]}`
  - 步骤：调 `multi_query_search(queries=texts, use_rerank=True)`（rerank 软依赖在 milvus_client 内部封装好）
  - 失败隔离：milvus 抛错单 Send 返回 error sub_evidence（fan-out 单 Send 失败隔离，规则 25 允许）
- `barrier2_node`（sync fan-in）：
  - 入：`sub_evidence: list[dict]`（reducer add 累加）
  - 出：`evidence: list[dict]`（flatten + 加 sub_idx 标签 + 错误聚合到 search_errors）

**修改** `brain_base/graphs/qa_graph.py`（约 30 行）：

- imports 加 `qa_search` 3 项；删 `legacy_dense_search_node` import
- QaState 加 `sub_evidence: Annotated[list[dict], add]` reducer 字段 + `search_errors: list[str]`
- 节点注册：`subquery_search_one` / `barrier2` 是节点；`fanout_search_dispatcher` 是条件边
- 边修改：
  - **删** `workflow.add_node("legacy_dense_search", legacy_dense_search_node)`
  - **删** `workflow.add_edge("ingest", "legacy_dense_search")` + `workflow.add_edge("legacy_dense_search", "judge")`
  - **改** `fanout_persist_dispatcher` / `fanout_enrich_dispatcher` 的短路目标 `"legacy_dense_search"` → `"ingest"`
  - **加** `workflow.add_conditional_edges("ingest", fanout_search_dispatcher, {"barrier2": "barrier2"})`
  - **加** `workflow.add_edge("subquery_search_one", "barrier2")` + `workflow.add_edge("barrier2", "judge")`
- run() 初始化加 `sub_evidence=[]` / `search_errors=[]`
- **删除** `brain_base/nodes/qa.py::legacy_dense_search_node` 函数 + 顶部 `multi_query_search` / `list_docs` 等仅它用的 import

**测试** 新建 `tests/unit/test_qa_search_pipe2.py` + `tests/unit/test_qa_graph_t28.py`（约 200 行 / 12 测试）：

- dispatcher gate（空 sub_queries 短路 / 非空 N Send）
- subquery_search_one 成功（multi_query_search 调用次数 + 参数 + sub_idx/sub_question 透传）
- subquery_search_one 失败隔离（milvus 抛错 → 单 Send 返回 error sub_evidence）
- subquery_search_one Semaphore 限流（4 Send + concurrency=3 → 峰值 ≤ 3）
- barrier2 聚合（sub_evidence × N → evidence 带 sub_idx / sub_question 标签 + 排序）
- 主图集成（test_qa_graph_t28.py：编译 / 新节点拓扑 / 老 legacy_dense_search 删干净 / 老边删 / 新边加 / run() 初始化字段）

**同步更新** CLAUDE.md / AGENTS.md：

- `get_info_block` 内部展开图末尾 `LDS` 节点替换为 `→ fanout_search_dispatcher → subquery_search_one × N → barrier2`
- 关键约束追加 PIPE2 规则：每子问题独立检索 / rerank 软依赖（在 milvus_client 内部已封装）/ sub_idx 标签 / Semaphore 隔离 / barrier2 聚合语义

### 估算

- 代码：~230 行（200 新建 qa_search.py + 30 改 qa_graph.py + 删 legacy_dense_search 约 -40 行）
- 测试：~200 行 / 12 测试
- 前置：T27 ✅ 已完成

---

## T29 E2E 基线测试（完全外查 + 入库 + 多意图识别）— executing

> **插队原因**：T28 PIPE2 完成后主图 4 大块已打通，但从未真实跑过一轮端到端（单元测试 + smoke 拓扑测试不等于真的有 LLM + Milvus + playwright 可用）。这比起 T30/T31 优先级更高——先验证主图有效再做后续改进。
>
> **相关文档**：
> - 预期 + 评判标准：`@/md/eval/2026-05-09-e2e-baseline.md`
> - 执行计划 + 风险审查：`@/md/research/2026-05-09-t29-e2e-baseline-execution-plan.md`
>
> **测试题**：
> 1. RAGFlow 双子问题：`RAGFlow 是什么？怎么用？`
> 2. openclaw 三子问题：`openclaw 是什么？怎么启动？怎么卸载？`
>
> **核心验证能力**：
> - decompose 多意图识别（2/3 子问题正确拆分）
> - PIPE1 rewrite 防撞车（三子 grep_keywords 必异）
> - GI 完全外查流水（search → fetch_extract → write_raw → enrich → ingest）
> - PIPE2 每子问题独立 top-K + rerank（T28 核心：max/min chunks ≤ 4）
> - answer 按子问题分段 + 段间无交叉引用
>
> **执行 Phase（由用户确认 Phase B 清理后固化流程）**：
> - Phase A：前置自检（Milvus 连通 / LLM key / playwright / embedding runtime）
> - Phase B：清理 4 raw + 9 chunks + 4 doc_id Milvus 行（**首次确认，后续流程已固化免询问**）
> - Phase C：清理后自检
> - Phase D：跑题 1 RAGFlow
> - Phase E：跑题 2 openclaw
> - Phase F：填评判报告 + 归档
>
> **技术风险**（详见执行计划 §2，全部非阻断）：LLM 费用 ~$1-3/题 / playwright 失败率 / rerank 软降级 / serp 高重叠 / ingested_count 范围宽。
>
> **估算**：
> - 执行：1-3 小时（两题 LLM 调用 + SERP 抓取 + 入库 + 评判填表）
> - 代码改动：0（跑现有系统，不改代码）
> - 若发现 Fail 需修复：另开任务
>
> **验收**：题 1 5/5 + 题 2 6/6 全过 → 标 finished；有 Fail → 保留 executing + 追加 §8 失败分析到 baseline 文档。
>
> **优先级**：**高**（插队）——产品级功能验证，不过不安心推后续任务。

---

## T30 sparse gate 替换 grep gate（修 text_search bug + LLM schema/state 字段重命名）— finished

> **实际产出**（2026-05-10）：
> - **bin/milvus-cli.py**：修 `text_search` bug（原 `data=[query]` 直接喂 str 给 sparse 字段必抛 VECTOR_SPARSE_U32_F32 vs VARCHAR 类型错；改为 `_encode_query` → `data=[sparse_vector]`），返回值改走 `format_search_results` 与 dense_search/hybrid_search 对齐。
> - **brain_base/agents/schemas.py**：`RewrittenQueries` 删 `grep_keywords: list[str]`，加 `lexical_query: str`（min 2 / max 30 字）。
> - **brain_base/prompts/qa_prompts.py**：REWRITE prompt 任务二改"输出 lexical_query 短自然语言串（≤30 字 / SERP 友好）"。
> - **brain_base/nodes/qa_prep.py**：`prep_one_subquery` 内部从 `grep_keywords_and` AND 计数改调 `text_search` top-3 平均分 + 阈值 0.20；新增 `_sparse_gate_score` 内部辅助（捕获异常 → 0.0 + logger.warning，保守降级走外检）；新增 `_normalize_lexical_query` 兜底（空串退到 sub_question，截 30 字）；常量 `LEXICAL_GATE_THRESHOLD=0.20` / `LEXICAL_GATE_TOP_K=3`。`barrier1_node` 字段重命名 `sub_grep_keywords/hits` → `sub_lexical_queries/scores`。
> - **brain_base/graphs/qa_graph.py**：`QaState` 字段同步重命名 + 类型变化（list[list[str]] → list[str]，list[int] → list[float]）。
> - **brain_base/nodes/qa_get_info.py**：`merge_search_keywords_node` 改读 `sub_lexical_queries`（每子问题 1 个短串直接当 SERP query，不再 join keywords）。
> - **brain_base/nodes/qa.py / brain_base/tools/lexical_grep.py**：旧 grep gate 描述更新为指向 T30 sparse gate；`grep_keywords_and` 工具保留可用（仅供 CLI / eval / 手动诊断），docstring 注明 PIPE1 不再调用。
> - **测试改写**：`tests/unit/test_qa_prep.py` 重写（mock `_sparse_gate_score` 替代 grep；新增 low_score 触发外检 / sparse_failure_safe_degrade / lexical_query > 30 字截断 3 个测）；`tests/unit/test_prompts_context_inheritance.py` + `tests/unit/test_qa_get_info.py` 同步字段重命名；`tests/unit/test_lexical_grep.py` 不动（工具自身保留）。**全 unit + smoke 共 289 测全绿，1 skip 是数据相关无关**。
> - **bin/demo_prompts_trace.py**：4 处 grep_keywords/grep_keywords_and 同步迁移到 lexical_query/_sparse_gate_score。
> - **CLAUDE.md / AGENTS.md**：主流程图 PIPE1 子图节点 `grep(AND)` → `sparse gate(text_search top-3 avg)`；关键约束 4 处描述同步；get_info_block 子图入口 `sub_grep_keywords` → `sub_lexical_queries`；search_web_dual 描述同步。
> - **其他**：补 ingest 6 个未入库 doc 到 milvus（37/37 doc 全入库，num_entities 485 → 823），探针实测决定阈值 0.20（HIT 真集 top-3 avg ∈ [0.288, 0.337]，MISS 真集 ∈ [0.017, 0.124]，gap 中点 ≈ 0.21），e2e gate 验证 5 query 全对（含 T29 原失败用例 `RAGFlow 定义 核心概念 架构` → top-3 avg=0.3357 完美命中）。
> - **契约文档**：`md/research/2026-05-10-t30-sparse-gate-contract.md`（12 节，背景 / bug 说明 / schema 变更 / state 变更 / 阈值设计 / 失败处理 / 节点流程图 / 风险评估 / 工作量 / 执行步骤 / 验收 / 用户确认点）。
> - 临时探针脚本 `data/logs/_t30_*` 全部用完即弃删除。
>
> **T30.1 主图 conditional edge 修复延伸**（同日 e2e 验收时暴露 + 修复）：
> - **暴露场景**：T30 改 sparse gate 后 e2e 跑 `RAGFlow 是什么？怎么用？`，sparse_gate `avg=0.2647` 正确判 PASS、barrier1 `any_needs_get_info=False`，但 **search_web_dual 仍触发 4 task / 21 URLs**——浪费 ~40s SERP 时间。
> - **根因**：`@/brain_base/graphs/qa_graph.py:245` 是 `add_edge("barrier1","merge_search_keywords")` **无条件边**——主流程图画的 `GATE{any needs_get_info?}` 在代码里从来不存在，sub_needs_get_info 字段只在 `fanout_extract_dispatcher` 第 2 重 gate 消费，但那时 SERP 已抓完。**T29 之前**（grep gate 时代）grep 全 0 命中 → 全 needs_get_info=True → 走 GI 是"对的"，bug 被掩盖；**T30 后**让 PASS 路径出现才暴露此问题。
> - **修复**：
>   - `brain_base/graph/conditional_logic.py`：QA section 加 `after_barrier1(state) -> str` router（任一 sub_needs_get_info=True → `merge_search_keywords` 走 GI；全 False → `ingest` 跳过 GI 空跑后接 PIPE2）
>   - `brain_base/graphs/qa_graph.py`：`add_edge("barrier1","merge_search_keywords")` 改为 `add_conditional_edges("barrier1", self.routing.after_barrier1, {"merge_search_keywords":..., "ingest":...})`；顶部 docstring 条件边清单加 after_barrier1
>   - `tests/unit/test_qa_get_info_loop.py`：加 `test_after_barrier1_routes_by_sub_needs_get_info`（7 个断言：全 False / 部分 True / 全 True / 字段缺失 / 空列表 / None 全覆盖）
> - **可观测性增强**（同日加，在 `qa_prep.py`）：
>   - `_sparse_gate_score` 成功路径加 INFO log（top-3 各 score / avg / 阈值 / PASS|FAIL / top_doc_ids）
>   - `prep_one_subquery` 完成时加 INFO log（sub_idx / sub_question / lexical_query / score / threshold / needs_get_info 一行汇总）
>   - `barrier1_node` 聚合后加 INFO log（N 子问题状态 + any_needs_get_info）
> - **修复验证**：重跑 e2e `RAGFlow 是什么？怎么用？`：sparse_gate `avg=0.2662 PASS` → barrier1 `any_needs_get_info=False` → **`search_web_dual` 0 触发**（修复前 4 task），直接进 PIPE2 milvus 召回 → judge → answer 完整输出。**281 unit/smoke 测全绿，新增 7 断言全过**。
> - **新增/修改文件清单**（T30.1）：
>   - `brain_base/graph/conditional_logic.py`：+18 行 `after_barrier1`
>   - `brain_base/graphs/qa_graph.py`：line 245 改 conditional edge + 顶部 docstring 加 after_barrier1
>   - `brain_base/nodes/qa_prep.py`：+3 处 INFO log（sparse_gate / prep_one_subquery / barrier1_node）
>   - `tests/unit/test_qa_get_info_loop.py`：+1 个测试函数（7 断言）

> **插队原因**：T29 e2e 跑 RAGFlow 题暴露：子问题「RAGFlow 是什么」LLM 生成 grep_keywords=`['RAGFlow','定义','核心概念','架构']` 4 词 AND 全中 → 0 hit → 误触发外检。**根因**：grep 是字面匹配，LLM 倒向生成抽象元语词（定义 / 核心概念 / 理念），与文档实际不匹配。
>
> **技术路线**（用户已选 路线 2）：用 milvus sparse-only `text_search` 代替 `grep_keywords_and`，靠 sparse tokenizer + tf-idf 处理“字面 vs 语义”不匹配。
>
> **契约 + 执行计划**：`@/md/research/2026-05-10-t30-sparse-gate-contract.md`
>
> **重要发现**：`bin/milvus-cli.py:text_search` 实现是 bug（`data=[query]` 直接喂 str 给 sparse 向量字段 → milvus 报 VARCHAR vs VECTOR_SPARSE_U32_F32 类型不匹配）——项目从未跑通过。本任务需先修。
>
> **范围**（单会话可完）：
> 1. 修 `text_search`（参考 `hybrid_search`：`build_embedding_runtime` → `_encode_query` 拿 sparse_vector → `client.search(data=[sparse_vector])`）
> 2. 跑探针 `data/logs/_t30_score_probe.py` 看真实 top-1 distance 分布 → 决定 `LEXICAL_GATE_THRESHOLD`
> 3. LLM schema：删 `grep_keywords` 列表 → 加 `lexical_query: str` （≤ 30 字自然语言式）
> 4. prompt 改造：要求 LLM 输出短串而不是 keywords list
> 5. `prep_one_subquery`：grep 调用 → text_search 调用 + 阈值判定
> 6. QaState 字段重命名：`sub_grep_keywords/hits` → `sub_lexical_queries/scores`
> 7. barrier1 聚合 + GI `merge_search_keywords` 同步改读 `sub_lexical_queries`
> 8. 测试改写（`test_qa_prep.py` mock 点从 grep_keywords_and 换为 text_search；`test_lexical_grep.py` 11 测不动，grep 函数保留）
> 9. e2e 验证：跑 cli ask 「RAGFlow 是什么？怎么用？」看子 1 是否不再 0 命中
> 10. 文档同步：CLAUDE.md / AGENTS.md 主流程图 PIPE1 内部 grep → text_search
> 11. 删探针脚本（用完即弃）
>
> **验收**：子问题 1 `needs_get_info=False`（库里有时正确识别） + pytest 全绿 + e2e 不再误触发外检。
>
> **估算**：~270 行代码改动 + ~110 行测试，单会话。优先级：**高**（T29 外检路径误触发会打乱 e2e 评判、每题多加 1-3 分钟费用 + 不必要走 playwright）。

---

## T31 arxiv 专用 PDF 通道 — pending

> 原 T29，T29 插队后顺延为 T30；T30 sparse gate 插队后再顺延为 T31。来自用户在 T26.1 完成后讨论：从 SERP 拿到 arxiv abs URL 时，HTML 里的 PDF
> 是要点 "View PDF" 才能拿到，当前 fetch_extract 走 readability 抽出来的只是
> abstract 页文字，丢了正文。
>
> 思路：在 `fetch_extract_one` 内部加一个 arxiv URL 识别（host=`arxiv.org` +
> path 含 `/abs/`），命中则改走 `arxiv-paper-fetch` 子流程：
> 1. URL 转换：`arxiv.org/abs/{id}` → `arxiv.org/pdf/{id}.pdf`
> 2. 直接下载 PDF（playwright 或 httpx，arxiv 无 CDN/防爬）
> 3. 走 upload-agent 路径（MinerU 解析 PDF → markdown），不走 readability
> 4. 复用现有 `compute_body_sha256` + `hash_lookup` dedup
> 5. 复用现有 chunker + enrich + ingest 持久化流水
>
> 关键约束：
> - **upload-agent 禁止并行**（CLAUDE.md 规则 6，MinerU 14GB VRAM）
> - 与 fetch_extract_one 的 Semaphore=3 冲突 → arxiv 命中时单独走串行队列
> - 或：arxiv URL 在 fetch_extract_one 内 short-circuit 标记为 `route: arxiv-pdf`，
>   barrier_extract 后插一个 `arxiv_pdf_serial_node`（串行处理 arxiv candidates），
>   普通 candidate 走 write_raw_one 并行
>
> 估算：~80 行 + 15 测试。优先级：**中**（实测有需求才做，等 T29 完成后）。

---

## T32 chunker 段落级 dedup — pending

> 原 T15 / T28 / T30 / T31，多次顺延。T28 改派给 PIPE2 重构，T29 e2e baseline 插队 顺延为 T31，T30 sparse gate 再插队 顺延为 T32。
>
> 来自 T12 e2e：testimonials 重复 2 倍。方向：拆段（`\n\n`）+ SHA-256 去重。
> 约 25 行 + 50 行测试。T16 LLM 评分后营销页大概率拿低分被排除，
> 重复污染概率大幅下降。**当前判定：T20 通用 raw text 路径上线后，GitHub README 不走 MinerU 截断，重复来源进一步收敛，T15 优先级再次下降。等 T20 上线后跑一轮 e2e（如 T29 本次基线）看 testimonials 是否还重复，再决定做不做。**
> 优先级：**低**。
