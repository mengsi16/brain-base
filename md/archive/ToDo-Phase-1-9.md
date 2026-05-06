# LangGraph 重构 ToDo

> 状态：`pending` / `executing` / `finished`
> 任务规则参考 `CLAUDE.md`：ToDo.md 驱动、执行前清点工作量、完成后写 `finished` 简要产出、参考 `../brain-base-backup/` 与 TradingAgents。

## 已完成（Phase 1–8）

| Phase | 内容 | 关键产出 |
|---|---|---|
| 1 | brain_base 包骨架 | `brain_base/__init__.py`、`config.py`、`checkpointer.py`、`graphs/`、`nodes/` |
| 2 | KnowledgePersistence 子图 | `graphs/persistence_graph.py`、`nodes/persistence.py` |
| 3 | IngestFile + IngestUrl 图 | `graphs/ingest_file_graph.py`、`graphs/ingest_url_graph.py`、`nodes/ingest_file.py`、`nodes/ingest_url.py`；删除 `skills/` `agents/` `.claude-plugin/` `bin/brain-base-cli.py` |
| 4 | QaGraph + CrystallizeGraph | `graphs/qa_graph.py`、`graphs/crystallize_graph.py`、`nodes/qa.py`、`nodes/crystallize.py` |
| 5 | LifecycleGraph + LintGraph + CLI | `graphs/lifecycle_graph.py`、`graphs/lint_graph.py`、`nodes/lifecycle.py`、`nodes/lint.py`、`brain_base/cli.py` |
| 6 | TradingAgents 完整结构对齐 | `agents/utils/{agent_states.py,agent_utils.py}`、`agents/schemas.py`、`graph/{setup,conditional_logic,propagation,brain_base_graph}.py`、`llm_clients/` |
| 7 | 提示词迁移（首版） | `prompts/*.py` 共 8 个文件，覆盖 11 个原 SKILL；QA 节点 4 个 LLM 工厂上线；`BrainBaseGraph(llm)` 全链路透传 |
| 8 | LLM 节点工厂全面接入 | 新增工厂：`create_enrich_node` / `create_value_score_node` / `create_decompose_node` / `create_self_check_node`；QA 拓扑增 `decompose` 与 `self_check` 节点 |

---

## 待执行任务（T1–T8）

> 每个任务必须可独立验证：完成后跑一次「验收命令」，通过再进入下一项。
> 执行顺序：**T1 → T3 → T2 → T4 → T5 → T6 → T7 → T8**（先打地基/工具/schemas，再瘦身/接入，最后多步循环和文档）。

---

### T1 工具层骨架 + milvus-cli 函数化 — executing

**目标**：把节点对外部 CLI 的 `subprocess` 调用改为 Python `import` 直接调用，常驻 graph 进程能复用客户端。

**新增文件**

| 文件 | 职责 | 预估行数 |
|---|---|---|
| `brain_base/tools/__init__.py` | 公共导出 | ~15 |
| `brain_base/tools/milvus_client.py` | 包装 milvus-cli 拆出的纯函数（`multi_query_search` / `hash_lookup` / `count_rows` / `delete_by_doc_ids` / `check_runtime`），客户端缓存 | ~180 |
| `brain_base/tools/web_fetcher.py` | subprocess 调 `playwright-cli`，封装 `search_google` / `search_bing` / `fetch_page`；SPA 重试在内部 | ~220 |
| `brain_base/tools/doc_converter_tool.py` | subprocess 调 `bin/doc-converter.py convert` | ~60 |
| `brain_base/tools/chunker_tool.py` | subprocess 调 `bin/chunker.py` | ~40 |

**改动文件**

- `bin/milvus-cli.py`：把每个子命令的实现从 `argparse` 入口拆出来成顶层函数（`def multi_query_search(...) -> dict` 等），保留 `main()` argparse 入口供调试。**不破坏外部 CLI 行为**。

**执行步骤**

1. 先读 `bin/milvus-cli.py` 当前实现，把 5 个核心子命令（`multi-query-search`、`hash-lookup`、`count-rows`、`delete-by-doc-ids`、`check-runtime`）的逻辑函数化。
2. 新建 `tools/milvus_client.py`：`@lru_cache` 客户端、5 个公开函数。
3. 新建 `tools/web_fetcher.py`：subprocess 调 `npx playwright-cli`（或项目内置入口），封装搜索引擎和单页抓取，SPA 失败用 `wait_for_selector + 重试`。
4. 新建 `tools/doc_converter_tool.py` / `tools/chunker_tool.py`：极薄 subprocess 包装。
5. `tools/__init__.py` 导出全部公共函数。

**验收**

```python
from brain_base.tools.milvus_client import multi_query_search, hash_lookup, check_runtime
from brain_base.tools.web_fetcher import search_google, fetch_page
from brain_base.tools.doc_converter_tool import convert_document
from brain_base.tools.chunker_tool import chunk_markdown
print(check_runtime()["dense_dim"])
```

CLI 回归：`python bin/milvus-cli.py count-rows` 仍正常输出。

---

### T2 Pydantic schemas 扩充 — finished

**实际产出**

- `brain_base/agents/schemas.py` 在原 4 类业务 schema 基础上追加 15 个 LLM 中间步骤 schema：
  `NormalizedQuestion` / `DecomposedQuestion` + `SubQuestion` / `RewrittenQueries` + `RewrittenQuery` / `EvidenceJudgment` / `SelfCheckResult` / `GetInfoTrigger` / `ValueScore` / `HitCheckResult` / `CrystallizedSkill` / `ChunkEnrichment` / `NextQueryPlan` / `UrlClassification` + `UrlClassificationBatch` / `CompletenessJudgment` / `RecallDiagnosis`。
- 关键约束全部由 Pydantic 强制：`Literal` 枚举、`min_length` / `max_length`、`ge` / `le`——后续 prompts 不再需要写「输出 N 个 keyword」「层级必须是 L0–L3」等。
- 验收：15 个 schema 均可 `model_json_schema()`；约束触发用例（`keywords<5`、`sub_questions>4`、`expected_type` 非法、`confidence>1.0`）全部正确抛 `ValidationError`。

**目标**：给所有 LLM 中间步骤添加 schema，`llm.with_structured_output(Schema)` 替代 prompt 里的「输出 JSON 格式」段。

**改动文件**：`brain_base/agents/schemas.py`

**新增 schema 清单**

| 节点 | Schema | 关键字段 |
|---|---|---|
| qa.normalize | `NormalizedQuestion` | `expected_type: Literal["fact","procedure","concept","comparison","opinion"]`、`time_sensitive: bool` |
| qa.decompose | `DecomposedQuestion` | `sub_questions: list[SubQ]` (max=4)；`SubQ.type: Literal["sub-fact","synthesis"]` |
| qa.rewrite | `RewrittenQueries` | `queries: list[Query]` (max=6)；`Query.layer: Literal["L0","L1","L2","L3"]` |
| qa.judge | `EvidenceJudgment` | `recommendation: Literal["generate_answer","trigger_get_info","degrade"]` |
| qa.self_check | `SelfCheckResult` | `faithfulness/completeness/consistency: Literal["pass","fail"]` |
| qa.get_info_trigger | `GetInfoTrigger` | `time_range_hint: Literal["1mo","3mo","1y","none"]` |
| crystallize.value_score | `ValueScore` | 4 维 `float[0,1]`、`recommended_layer: Literal["hot","cold","skip"]`、`trigger_keywords: list[str]` (3..8) |
| crystallize.hit_check | `HitCheckResult` | `status: Literal["hit_fresh","hit_stale","cold_observed","cold_promoted","miss","degraded"]` |
| persistence.enrich | `ChunkEnrichment` | `summary: str` (max=200)、`keywords: list[str]` (5..10)、`questions: list[str]` (3..8) |
| get_info.plan_next_query | `NextQueryPlan` | `mode: Literal["broaden","narrow","site_search","translate"]` |
| get_info.classify_url | `UrlClassification` | `source_type: Literal["official-doc","community","discard"]`、`confidence: float[0,1]` |
| ingest_url.completeness | `CompletenessJudgment` | `status: Literal["ok","spa-failed","insufficient-content","over-cleaned"]` |
| self_heal.recall_diagnosis | `RecallDiagnosis` | `root_cause: Literal[...]`、`missing_dimensions: list[6 维 Literal]` |

**预估**：~300 行新增。

**验收**

```python
from brain_base.agents.schemas import (
    NormalizedQuestion, DecomposedQuestion, RewrittenQueries,
    EvidenceJudgment, SelfCheckResult, ValueScore, HitCheckResult,
    ChunkEnrichment, NextQueryPlan, UrlClassification, CompletenessJudgment, RecallDiagnosis,
)
for cls in [NormalizedQuestion, ValueScore, ChunkEnrichment]:
    assert cls.model_json_schema()
```

---

### T3 nodes/_*.py 工具模块 — finished

**实际产出**

- 新增 `brain_base/nodes/_hash.py`、`_atomic.py`、`_frontmatter.py`、`_probe.py`、`_priority_io.py`、`_audit.py`。
- `_hash.py` re-export `agents/utils/agent_utils.compute_content_hash`，并补充 `compute_body_sha256`（CRLF 归一化、首尾空行裁剪后取 SHA-256）。
- `_frontmatter.py` 提供 `split_frontmatter` / `parse_frontmatter` / `dump_frontmatter` / `inject_enrichment` / `reassemble` 五件套，覆盖 ingest_url / ingest_file / persistence / lifecycle 各处需求。
- `_probe.py` 三项探测均包装 `tools/` 层调用，QA 主图初始化用 `probe_all()` 一次拿到。
- `_priority_io.py` 提供 priority.json 读写与 keywords.db SQLite 增量更新；按 CLAUDE.md 硬约束 12，仅 update-priority 节点应写入。
- `_audit.py` 只暴露 append + read，禁止改写（CLAUDE.md 硬约束 42）。
- 验收：六模块全部 import 成功；frontmatter 解析-注入-reassemble、SQLite keywords 增量、原子写、jsonl 追加端到端跑通。

**目标**：把「SHA-256 / 原子写 / frontmatter 解析与注入 / 基础设施探测 / 优先级 IO / 审计日志」抽到独立模块，节点直接 import。

**新增文件**

| 文件 | 职责 | 预估行数 |
|---|---|---|
| `brain_base/nodes/_hash.py` | `compute_content_hash(text)`（薄封装，引用 `agents/utils/agent_utils.compute_content_hash`） | ~25 |
| `brain_base/nodes/_atomic.py` | `atomic_write_text(path, content)` / `atomic_write_json(path, obj)`（先写 .tmp 再 `os.replace`） | ~50 |
| `brain_base/nodes/_frontmatter.py` | `parse_frontmatter(text)` / `inject_enrichment(text, fields)` / `dump_frontmatter(meta)` | ~130 |
| `brain_base/nodes/_probe.py` | `probe_milvus()` / `probe_playwright()` / `probe_doc_converter()` 带超时 | ~100 |
| `brain_base/nodes/_priority_io.py` | `read_priority_json` / `write_priority_json` / `update_keywords_db` 带文件锁 | ~150 |
| `brain_base/nodes/_audit.py` | `append_audit_log(jsonl_path, record)` | ~30 |

**重叠处理**（按 CLAUDE.md 规则 #10）

- `compute_content_hash` 已存在 `agents/utils/agent_utils.py`：`_hash.py` 只 re-export，**不重写**。
- `_priority_io.py` 是 `update-priority` 专属写入路径，遵守硬约束 #12（`knowledge-persistence` 不写 `priority.json` / `keywords.db`）。

**验收**

```python
from brain_base.nodes._hash import compute_content_hash
from brain_base.nodes._atomic import atomic_write_json
from brain_base.nodes._frontmatter import parse_frontmatter, inject_enrichment
from brain_base.nodes._probe import probe_milvus
from brain_base.nodes._priority_io import read_priority_json
from brain_base.nodes._audit import append_audit_log
```

---

### T4 prompts 瘦身 — finished

**实际产出**

- 8 个 prompt 文件全部重写。文件总字节从 33823 → 20609，缩减 39%。
- 关键禁用词从 prompt 主体清除：「输出 JSON 格式」段全部删除（schema 替代）；`python bin/...` 命令调用删除；`after:` / `before:` Google 操作符段删除；frontmatter 字段模板删除（`_frontmatter.py` 与 `agent_utils.build_frontmatter` 接管）。
- `get_info_prompts.py` 新增 `PLAN_NEXT_QUERY_SYSTEM_PROMPT` 与 `CLASSIFY_URL_SYSTEM_PROMPT`（多步循环用），原 `URL_CANDIDATE_SYSTEM_PROMPT` / `TIME_RANGE_SEARCH_SYSTEM_PROMPT` 保留为别名以兼容现有引用。
- 全部原 `*_SYSTEM_PROMPT` / `*_USER_PROMPT_TEMPLATE` 常量名保留，节点工厂的 import 不需要改。

**目标**：每个 prompt 段瘦身到 50–150 字符**纯语义**。删除 JSON 格式段、字段数量约束、命令调用、时间窗口分批策略、重试次数。

**改动文件**：`brain_base/prompts/*.py` 全部 8 个

**保留**

- 角色定义、判断维度的语义、典型场景、不得编造类硬约束、避坑指引（如「Amazon 不走 Cloudflare」「外文必须翻译为中文」）。

**删除**

- 「输出 JSON 格式如下：…」整段 → 由 `with_structured_output(Schema)` 替代。
- 「字段数量必须 3–5 个」 → 由 Pydantic `min_length` / `max_length` 替代。
- 「调用 `python bin/milvus-cli.py …`」 → 节点 import 调用。
- 「重试 2 次后失败」 → Python `for` + `try/except`。
- 「Google `after:/before:` 操作符」 → `web_fetcher` 内部。
- 「frontmatter 用 `---` 包围 JSON inline 数组」 → `_frontmatter.py` 强制。

**预估**：8 个文件总计从 ~33KB 缩到 ~10KB（净减约 -700 行）。

**验收**：每个 prompts 文件大小较当前缩减 ≥ 60%；`grep -E "python bin/|输出 JSON|重试 [0-9]+ 次"` 无命中。

---

### T5 节点工厂改造（接 schemas） — finished

**实际产出**

- 新增 `brain_base/agents/utils/structured.py`：`invoke_structured(llm, schema, system, user)` 优先 `with_structured_output`、失败回落到 JSON 文本解析、再失败可走 `fallback`；`bind_structured(llm, schema)` 工厂。
- `nodes/qa.py` 改造：`probe_node` 复用 `_probe.probe_*`、`search_node` 改用 `tools.milvus_client.multi_query_search`；6 个 LLM 工厂全部走 `invoke_structured` 并按 schema 字段映射回 state（`normalize` / `decompose` / `rewrite` / `judge` / `self_check` 五个用 schema；`answer` 仍是自由文本）。
- `nodes/crystallize.py` 改造：`create_value_score_node` 改用 `ValueScore` schema；新增 `create_skill_gen_node`（`CrystallizedSkill` schema）；`crystallize_write_node` 优先吃 `skill_payload`，同时保留旧 state 字段回落以兼容。
- `nodes/persistence.py` 改造：`create_enrich_node` 改用 `ChunkEnrichment` schema；frontmatter 解析-注入-reassemble 改走 `_frontmatter.py`，删除原文件中的私有 `_split_frontmatter` / `_inject_enrichment` / `_invoke_llm_json` / `_chunk_needs_enrich` 重复实现（`_chunk_needs_enrich` 仅保留模块内私用）。
- `nodes/ingest_url.py` 新增 `create_completeness_check_node`（`CompletenessJudgment` schema），llm=None 时降级到字符长度阈值。
- 全部 `llm=None` 降级路径保留，graph 在无 LLM 模式下仍可 compile + run。
- 验收：6 个 graph（QaGraph / PersistenceGraph / IngestFileGraph / IngestUrlGraph / CrystallizeGraph / LifecycleGraph）全部 import + compile 成功；`llm=None` 降级路径手测 6 个节点全部输出正确字段。

**目标**：所有 LLM 节点改为 `llm.with_structured_output(Schema)` 模式。

**改动文件**

| 文件 | 改动 |
|---|---|
| `brain_base/nodes/qa.py` | 6 个工厂改用 `with_structured_output`：`create_normalize_node` / `create_decompose_node` / `create_rewrite_node` / `create_judge_node` / `create_answer_node` / `create_self_check_node` |
| `brain_base/nodes/crystallize.py` | `create_value_score_node`（已存在）改用 `ValueScore` schema；新增 `create_hit_check_node` / `create_skill_gen_node` |
| `brain_base/nodes/persistence.py` | `create_enrich_node` 改用 `ChunkEnrichment` schema |
| `brain_base/nodes/ingest_url.py` | 新增 `create_completeness_check_node` 用 `CompletenessJudgment` |
| `brain_base/agents/utils/structured.py` | **新增**：通用 `bind_structured(llm, schema)` + `invoke_with_fallback(node, fallback_factory)` 帮助函数 |

**降级路径必须保持**：`llm=None` 时走 Python 兜底，与现状一致（CLAUDE.md 规则 #14：新层必须软依赖）。

**预估**：现有节点文件总计 ~50KB，删除 prompt 拼接 + 改 schema 后约缩到 ~35KB；新增 `structured.py` ~80 行。

**验收**：跑 QaGraph 一个简单问题，`normalize/decompose/rewrite/judge/answer/self_check` 6 个节点输出全部为对应 schema 实例。

---

### T6 GetInfoGraph 多步循环 — finished

**实际产出**

- 新增 `brain_base/nodes/get_info.py`：5 个节点 `init_state_node` / `create_plan_node(llm)` / `search_web_node` / `create_classify_node(llm)` / `check_continue_node`，配合启发式分类回退（域名 hints）。
- 新增 `brain_base/graphs/get_info_graph.py`：`GetInfoGraph(llm)` 拓扑 `init → plan → search → classify → check_continue →（continue 回 plan / end → END）`，`recursion_limit = max_iterations*5+10` 防 langgraph 内置上限触顶。
- 终止条件全部由 `check_continue_node` 用 Python 判定：达到 `max_iterations` / 总超时 / 找到 `target_official_count` 篇 official-doc / 没有 next_query / 搜索降级且无候选——五种情况全部走 `_route="end"`。
- 验收：图 compile 成功；五种终止条件分支用例全部通过；启发式分类把 docs.python.org → official-doc，stackoverflow → community，pinterest → discard。

**目标**：实现 plan-search-classify-loop，最多 5 轮、找到 ≥ 3 篇 official-doc 即停。

**新增文件**

| 文件 | 职责 | 预估行数 |
|---|---|---|
| `brain_base/graphs/get_info_graph.py` | `GetInfoGraph(llm=None)`，节点串联 + 条件边 | ~120 |
| `brain_base/nodes/get_info.py` | `create_plan_node(llm)` / `create_classify_node(llm)` / `search_web_node` / `check_continue_node` | ~250 |

**节点串联**

```
plan_next_query (LLM)
  → search_web (Python: tools.web_fetcher.search_*)
  → classify_results (LLM)
  → check_continue (Python 判定)
       ├─ continue → 回到 plan_next_query
       └─ end → END
```

**State**

```python
class GetInfoState(TypedDict, total=False):
    user_question: str
    queries_tried: list[str]
    candidates: list[dict]   # {url, title_hint, source_type, confidence}
    iteration: int
    max_iterations: int           # 默认 5
    target_official_count: int    # 默认 3
    per_iteration_timeout: float  # 默认 20.0
    total_timeout: float          # 默认 90.0
    started_at: float
    degraded: bool
    degraded_reason: str | None
```

**check_continue 纯代码**：达到 `max_iterations` / 总超时 / 已找到 `target_official_count` 篇 official-doc 则 `end`，否则 `continue`。

**集成**：QaGraph 的 `get_info_trigger` 节点判定需要补库时，调 `GetInfoGraph(llm).run({"user_question": ..., ...})`，candidates 传给已存在的 ingest_url 路径。

**验收**

```python
from brain_base.graphs.get_info_graph import GetInfoGraph
g = GetInfoGraph().build()
result = g.invoke({"user_question": "n8n self host docker setup"})
assert result["iteration"] <= 5
assert isinstance(result["candidates"], list)
```

---

### T7 conditional_edges 集中 — finished

**实际产出**

- `brain_base/graph/conditional_logic.py` 重写为 `ConditionalLogic` 类，集中以下路由方法：
  `route_by_mode` / `after_crystallized_check` / `after_judge` / `after_hit_check` / `after_freshness` / `should_write_crystallize` / `should_execute_lifecycle` / `after_completeness_check` / `route_get_info_continue`。
- 改造 `qa_graph.py` / `crystallize_graph.py` / `lifecycle_graph.py` / `ingest_url_graph.py`：删除文件内零散的 `_xxx` 路由函数，改用 `self.routing.xxx` 调用 ConditionalLogic 方法。
- `qa_graph.py` 在 `judge` 后新增 `add_conditional_edges`（当前两条分支都汇到 `answer`，今后接 GetInfoGraph 时插 `trigger_get_info` 分支只需扩 mapping）。
- `ingest_url_graph.py` 新增 `completeness` 节点 + 路由：completeness=ok 走 frontmatter，否则直接 END，避免错误内容写文件。
- 验收：ConditionalLogic 11 条路由用例全部通过；7 个图（QA / Crystallize / Lifecycle / Persistence / IngestFile / IngestUrl / GetInfo）全部 compile 成功。

**目标**：把 prompt 里所有「如果 X 就 Y」的判断剥到 `conditional_edges`。

**改动文件**

| 文件 | 新增条件边 |
|---|---|
| `brain_base/graphs/qa_graph.py` | crystallized 命中短路、Milvus 不可用跳过、get_info 触发分支、self_check 降级跳过 |
| `brain_base/graphs/crystallize_graph.py` | hot/cold 两阶段命中、`value_score < 0.3` 跳过、cost_benefit 豁免 |
| `brain_base/graphs/lifecycle_graph.py` | `confirm=False` 短路、Milvus 删除失败立即停 |
| `brain_base/graph/conditional_logic.py` | 集中所有路由函数 |

**预估**：~100 行新增条件边代码。

**验收**：`python -c "from brain_base.graphs.qa_graph import QaGraph; QaGraph().build().get_graph().draw_mermaid()"` 能输出包含全部条件分支的图。

---

### T8 文档同步 — finished

**实际产出**

- 归档 5 份过期文档到 `md/archive/`：`AGENT_SKILL_SEPARATION.md` / `skill-overlap-issues.md` / `content-cleaner-new-flow.md` / `current-flow.md` / `md-to-code-migration.md`（这些是旧 skill 体系/流程设计稿，与重构后代码不一致）。
- 新增 `md/ARCHITECTURE.md`（~120 行）作为代码架构单页地图：包结构、设计分工表（prompts / schemas / conditional_logic / tools 四层职责）、LLM 节点工厂模式说明、GetInfoGraph 多步循环拓扑、顶层入口映射表；明确指向 `OPERATIONS_MANUAL.md` 为运维入口、`BRAIN_BASE_CHARTER.md` 为里程碑入口。
- 不改 `README.md` / `OPERATIONS_MANUAL.md`：它们面向的是最终用户/运维者，外部命令形态未变（`python bin/milvus-cli.py ...` / `brain-base-cli.py` 等），硬改只会增加噪音（遵循 CLAUDE.md 通用规则 #4 只改必须改的）。
- 全链路最终验证：
  - 8 个 graph 全部 compile 成功（QaGraph 12/12、PersistenceGraph 5/4、IngestUrlGraph 7/7、IngestFileGraph 5/4、GetInfoGraph 7/7、LintGraph 6/5、CrystallizeGraph 4/4、LifecycleGraph 9/9）。
  - schemas 模块导出 37 个业务 schema + 5 个常用 Literal/Enum。
  - `ConditionalLogic` 11 条路由全部单元验证通过。
  - `invoke_structured` 在 `llm=None` 时走各节点降级分支（6 个 QA 节点 + value_score + completeness_check 均输出正确字段）。

**目标**：归档已迁移完毕的 markdown 文档，同步 `README.md` 与 `OPERATIONS_MANUAL.md`。

**改动文件**

| 文件 | 改动 |
|---|---|
| `md/AGENT_SKILL_SEPARATION.md` | 移到 `md/archive/`（旧体系产物） |
| `md/skill-overlap-issues.md` | 同上 |
| `md/content-cleaner-new-flow.md` / `md/current-flow.md` | 同上（旧流程图） |
| `md/md-to-code-migration.md` | 更新到当前实际状态 |
| `md/OPERATIONS_MANUAL.md` | 增补 GetInfoGraph 多步循环说明、`tools/` 层结构 |
| `README.md` | 同步主图与 CLI 命令 |

**验收**：`README.md` 主图与 `brain_base/cli.py` 命令一致；过期文档已归档。

---

### T9 运行时跟踪 + Anthropic 端到端验证 — finished

**实际产出**

- 新增 `brain_base/agents/utils/tracing.py`：
  - `stream_with_trace(graph, initial_state, logger=None, jsonl_path=None, config=None)` 用 `graph.stream(stream_mode="updates")` 逐节点消费输出，每完成一个节点 INFO 一行（节点名 / 耗时 / 字段摘要），并可同时写 JSONL trace 文件。
  - `configure_logger(name, level, log_file)` 一键配置 UTF-8 控制台 + 文件 handler。
- `brain_base/agents/utils/__init__.py` 导出 `configure_logger` / `stream_with_trace`。
- `brain_base/llm_clients/anthropic_client.py`：base_url 改用 `langchain-anthropic` 新版参数名 `base_url`（旧 `anthropic_api_url` 已弃用），`KNOWN_MODELS` 扩充 MiniMax 兼容模型 `MiniMax-M2.7` / `MiniMax-M2.1`。
- 新增 `tmp_e2e_trace.py`：MiniMax Anthropic 兼容端点（`https://api.minimaxi.com/anthropic` + `MiniMax-M2.7`）下跑 QaGraph，问题 `讲讲 LiteLLM ……`。
- 验证结果：
  - 全链路 10 节点全部执行（probe → crystallized_check → normalize → decompose → rewrite → search → judge → answer → self_check → crystallize_answer），总耗时 75 秒。
  - 4 个 LLM 节点（normalize / decompose / rewrite / judge）全部成功调用 MiniMax 并返回 Pydantic 结构化输出。
  - 软依赖降级路径全部生效：Milvus collection 缺失 → search 静默返回空 evidence；crystallized 不可用 → status=degraded 触发 self_check skip。
  - JSONL trace 完整记录每个节点的 update payload，可独立离线分析。

---

## 不在本批范围内

- 不重写 graph 框架本身（StateGraph 用法不动）。
- 不引入新 LLM provider（`llm_clients/` 不动）。
- 不改 Milvus schema（embedding 字段维度等不动）。
- 不重构 `bin/eval-recall.py` / `bin/source-priority.py` / `bin/scheduler-cli.py`。
- 不增加新业务功能——纯重构，对外行为应保持一致。
