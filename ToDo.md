# brain-base LangGraph 重构 ToDo

> **历史归档**：上一阶段 T50 / T50.1 / T54 / T55 / T56 三轮孤儿模块清理已归档至
> `md/archive/ToDo-Phase-T50-T56.md`（伴随 V5.1.1 发布）。前序阶段归档列表见
> `md/archive/`。

---

## 当前架构（V5.1.1 起）

**6 个 LangGraph 子图**（CLI 直接实例化，无顶层编排层）：

- **QaGraph**（`graphs/qa_graph.py`）— 用户问答全流程，含统一意图识别 Agent-Loop（intent_planner ↺ intent_executor ↺ intent_observer，5 级早退）+ 持久化流水（merge_evidence → write_raw_one × N → enrich_one × M → ingest）+ PIPE2（subquery_search_one × N → barrier2）+ Maker-Checker 自检 + 自动固化
- **IngestFileGraph**（`graphs/ingest_file_graph.py`）— 本地文件入库（convert → frontmatter → doc_enrich → persist 内嵌 PersistenceGraph）
- **PersistenceGraph**（`graphs/persistence_graph.py`）— chunker + enrichment + Milvus（chunk → enrich → ingest，被 IngestFileGraph 内部复用）
- **LifecycleGraph**（`graphs/lifecycle_graph.py`）— 跨存储一致性删除（resolve → scan → dry_run → delete_milvus → delete_files → clean_index → audit）
- **LintGraph**（`graphs/lint_graph.py`）— 固化层周期清理（scan → check → degrade → delete）
- **CrystallizeGraph**（`graphs/crystallize_graph.py`）— 固化层命中判断（hit_check → freshness_check）

**8 个 CLI 子命令**（对外稳定边界）：

`health` / `search` / `ask` / `chat` / `ingest-file` / `remove-doc` / `lint` / `crystallize-check`

---

## 任务概览

- T53 E2E 基线测试（最低）— pending

---

## T53 E2E 基线测试 — pending

> **背景**：T46 / T47 / T48 / T49 / T50 / T54 / T55 / T56 完成后，主图（QaGraph）+ 5 个子图的核心节点在 unit/smoke 层都有覆盖（457 PASS），但缺一份"端到端真用例"基线（输入 question → 完整跑通 → 答案 + evidence + 中间态 trace 落盘）。
>
> **范围（待细化）**：
>
> - 选 3-5 条典型问题（含固化命中 / 多意图分解 / 联网外检 / URL 入库 / 多轮对话各一条）
> - 每条记录：输入 / 最终答案 / evidence 数 / 节点跳数 / LLM 调用次数 / 总耗时 / fail-fast 触发情况
> - 落盘 `data/logs/e2e_trace.jsonl` + `data/logs/e2e_trace.log`（已有 `tests/e2e/test_qa_full_pipeline.py` 框架，扩展用例）
> - 未来回归任何破坏性改动，e2e 基线一跑就知道
>
> **执行前需明确**：用例选型 / 跑测条件（必须有 LLM key / Milvus 已有数据） / CI 配置（默认 skip 还是 nightly run）。
>
> 优先级：**最低**（清洁环境无 chunks 时 smoke 自然 skip，e2e 同理；当前 unit/smoke 已能挡住 90% 回归，e2e 是补完整性，不是阻塞下一阶段开发）。
