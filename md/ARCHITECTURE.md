# brain_base 代码架构

> 本文档说明 `brain_base/` 包内各模块的职责和调用关系，给开发/接手者一份单页地图。
> 运维与外部 CLI 命令请参考 `OPERATIONS_MANUAL.md`；运行架构图与里程碑请参考 `BRAIN_BASE_CHARTER.md`。

## 1. 包结构

```
brain_base/
├── __init__.py
├── config.py / checkpointer.py / cli.py     # 顶层配置、checkpointer、命令行入口
├── llm_clients/                             # LLM provider 抽象（OpenAI/Anthropic/Google/Azure）
├── prompts/                                 # 仅含 LLM 必须看的语义提示
├── agents/
│   ├── schemas.py                           # 业务结果 + LLM 中间步骤 Pydantic schema
│   ├── utils/
│   │   ├── agent_states.py                  # 各图 TypedDict 状态
│   │   ├── agent_utils.py                   # 哈希、doc_id、frontmatter 组装、msg_delete
│   │   └── structured.py                    # invoke_structured / bind_structured
│   ├── qa_agent.py / persistence_agent.py   # 各 agent 入口（薄封装，create_xxx_agent(llm)）
│   ├── ingest_file_agent.py / ingest_url_agent.py
│   ├── crystallize_agent.py / lifecycle_agent.py / lint_agent.py
├── graph/                                   # 顶层编排（参考 TradingAgents）
│   ├── setup.py / brain_base_graph.py
│   ├── conditional_logic.py                 # 全部图的条件边路由集合
│   └── propagation.py
├── graphs/                                  # 各业务子图
│   ├── qa_graph.py
│   ├── persistence_graph.py
│   ├── ingest_file_graph.py / ingest_url_graph.py
│   ├── crystallize_graph.py
│   ├── lifecycle_graph.py / lint_graph.py
│   └── get_info_graph.py                    # plan-search-classify 多步循环
├── nodes/                                   # 节点函数与小工具
│   ├── qa.py / persistence.py / crystallize.py
│   ├── ingest_file.py / ingest_url.py
│   ├── lifecycle.py / lint.py / get_info.py
│   ├── _hash.py                             # SHA-256 内容哈希（薄封装）
│   ├── _atomic.py                           # atomic_write_text / atomic_write_json
│   ├── _frontmatter.py                      # YAML frontmatter 解析 + 注入
│   ├── _probe.py                            # Milvus / Playwright / DocConverter 探测
│   ├── _priority_io.py                      # priority.json / keywords.db 读写
│   └── _audit.py                            # JSONL 审计日志（只追加）
└── tools/                                   # 外部 CLI 的 Python 封装
    ├── milvus_client.py                     # importlib 动态加载 bin/milvus-cli.py
    ├── web_fetcher.py                       # subprocess 调 playwright-cli（全走 PW，不依赖 trafilatura）
    ├── doc_converter_tool.py                # subprocess 调 bin/doc-converter.py
    └── chunker_tool.py                      # subprocess 调 bin/chunker.py
```

## 2. 设计分工

| 关注点 | 落点 |
|---|---|
| LLM 必须看到的语义内容 | `prompts/*.py` |
| LLM 输出的字段类型 / 数量约束 | `agents/schemas.py` 的 Pydantic Schema（`Literal` / `min_length` / `ge`/`le`） |
| 节点路由 / 条件边 | `graph/conditional_logic.py` + 各 `graphs/*_graph.py` 的 `add_conditional_edges` |
| 外部 CLI 调用 / 文件 IO / 重试 / 超时 | `tools/*.py` + `nodes/_*.py` |

> 这套分工是 LangGraph 重构的核心：**prompt 不写 JSON 格式段、不写 Python 命令、不写时间窗口分批**——这些约束分别由 schema、tools 和 graph 路由强制。

## 3. LLM 节点工厂模式

所有需要 LLM 的节点采用 `create_xxx_node(llm)` 工厂模式（参考 TradingAgents）：

```python
def create_xxx_node(llm: Any = None) -> Callable:
    def xxx_node(state):
        if llm is None:
            return _degraded_xxx(state)         # llm=None 走纯 Python 兜底
        result = invoke_structured(             # with_structured_output 失败回落 JSON 解析
            llm, XxxSchema, XXX_SYSTEM_PROMPT, _build_user_prompt(state)
        )
        return {"some_field": result.field}
    return xxx_node
```

`llm=None` 降级路径必须保留（CLAUDE.md 硬约束 14：新层必须软依赖）。

## 4. 多步循环：GetInfoGraph

`graphs/get_info_graph.py` 是 brain_base 唯一带显式循环的子图：

```
init → plan → search → classify → check_continue
                                         │
                                         ├── continue → 回到 plan
                                         └── end → END
```

终止条件**全部由 `nodes/get_info.py::check_continue_node` 用 Python 判定**：达到 `max_iterations` / 总超时 / 找到 `target_official_count` 篇 official-doc / `next_query` 为空 / 搜索降级且无候选——五种情况均走 `_route="end"`。

## 5. 顶层入口

| 模式 | 入口 | 主要图 |
|---|---|---|
| `ask` | `BrainBaseGraph(llm).run(mode="ask", question=...)` | `QaGraph` → 命中即返回，否则 `search → judge → answer → self_check → crystallize` |
| `ingest-file` | 同上，`mode="ingest-file"` | `IngestFileGraph` → 走 `convert → frontmatter → persist` |
| `ingest-url` | 同上，`mode="ingest-url"` | `IngestUrlGraph` → 走 `fetch → clean → completeness → frontmatter → persist` |
| `remove-doc` | 同上，`mode="remove-doc"` | `LifecycleGraph` → `confirm=False` 时仅 dry_run |
| `lint` | 同上，`mode="lint"` | `LintGraph` |

外部 CLI 调用的命令形态保持不变（`python bin/milvus-cli.py ...`），见 `OPERATIONS_MANUAL.md`。
