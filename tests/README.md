# brain-base 测试套件

围绕 `pytest.ini` 的四类目录组织：**smoke**（CLI JSON 契约 / 冒烟）/ **unit**（图与节点纯逻辑）/ **e2e**（真实 LLM + Milvus + 网络）/ **probes**（一次性调研脚本，pytest 不收集）。默认 `pytest` 命令只跑前两类，**约 30 秒**完成。

## 目录结构

```
tests/
├── conftest.py                         # 共享 fixtures：临时 crystal_dir / chunks_dir / raw_dir_for_hash 等
├── smoke/                              # 冒烟测试（离线、快、CLI JSON 契约）
│   ├── test_crystallize_cli.py
│   ├── test_milvus_cli.py
│   ├── test_content_hash.py
│   └── test_eval_recall.py
├── unit/                               # 单元测试（in-process 纯逻辑，无外部依赖）
│   └── test_qa_get_info_loop.py        # T10 自动外检闭环：图编译 / 配额 / 启发式 / 防死循环
├── e2e/                                # 端到端测试（默认跳过，需 LLM + Milvus + Playwright）
│   └── test_qa_full_pipeline.py        # 完整 QA 链路：外检 → 入库 → 重检索 → 自检
└── probes/                             # 调研 / 诊断脚本（pytest norecursedirs 忽略）
    ├── README.md
    ├── attn_backend_memory.py          # T11 mineru-html 显存调研
    ├── sdpa_kernel_memory.py
    ├── bing_search_probe.py            # SERP 解析探针
    └── serp_parsing_probe.py
```

## 运行

首次安装：

```powershell
python -m pip install pytest python-dotenv
```

默认套件（smoke + unit，无外部依赖，~30s）：

```powershell
python -m pytest                          # 默认跳过 requires_milvus / requires_llm
python -m pytest tests/smoke -q
python -m pytest tests/unit -q
python -m pytest -v --durations=5         # 显示最慢 5 个
```

按 marker 选择：

```powershell
python -m pytest -m requires_milvus       # 只跑需要 Milvus 的（先 docker compose up -d）
python -m pytest -m requires_llm          # 只跑需要真实 LLM 的（先在 .env 配 BB_LLM_API_KEY）
python -m pytest -m "requires_llm and requires_milvus"   # 完整 e2e
```

## 覆盖范围

### smoke/（offline，CLI JSON 契约保护）

| 文件 | 覆盖 CLI | 说明 |
|---|---|---|
| `test_crystallize_cli.py` | `bin/crystallize-cli.py` 7 个命令 | stats / list-hot / list-cold / show-cold / hit / promote / demote 端到端 |
| `test_milvus_cli.py` | `bin/milvus-cli.py` 纯文件系统命令 | list-docs / show-doc / stats / stale-check + JSON 契约稳定性 |
| `test_content_hash.py` | P2-1 三件套 | hash-lookup / find-duplicates / backfill-hashes + LF/CRLF 哈希等价 |
| `test_eval_recall.py` | `bin/eval-recall.py` | build-queries / run / diff / record-feedback / coverage-check |

**对 agent 的价值**：QaGraph / GetInfoGraph / LifecycleGraph 都把 CLI JSON 当合约消费，字段名漂移会导致 agent 静默错路；smoke 第一时间捕获。

### unit/（in-process，纯逻辑）

| 文件 | 覆盖 | 重点断言 |
|---|---|---|
| `test_qa_get_info_loop.py` | `QaGraph` 编译 + `select_candidates` + `get_info_trigger` 启发式 + `ConditionalLogic` 路由 | 5 个外检节点必须注册；max_official/max_community/max_total 配额；attempted=True 第二轮强制 answer（防死循环） |

### e2e/（默认跳过，外部依赖）

| 文件 | Markers | 依赖 |
|---|---|---|
| `test_qa_full_pipeline.py` | `requires_llm` + `requires_milvus` + `slow` | 任一 LLM provider 的 `BB_LLM_API_KEY` + 本地 Milvus + Playwright-cli |

也可作为独立脚本带详细 trace 跑：

```powershell
python tests/e2e/test_qa_full_pipeline.py
# → data/logs/e2e_trace.log + data/logs/e2e_trace.jsonl
```

### probes/（不被 pytest 收集）

调研脚本，文件名故意不以 `test_` 开头，且 `pytest.ini` 用 `norecursedirs = tests/probes` 显式排除。详见 [`probes/README.md`](./probes/README.md)。

## marker 速查

| Marker | 默认 | 触发条件 | 用途 |
|---|---|---|---|
| `offline` | 跑 | 总是 | 离线冒烟，无外部依赖 |
| `requires_milvus` | **跳** | `-m requires_milvus` | 需要 `docker compose up -d` 起 Milvus |
| `requires_llm` | **跳** | `-m requires_llm` | 需要 `BB_LLM_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` 任一 |
| `slow` | 跑 | 总是（标记用） | 仅作分类标签，跑 `--durations=5` 时方便定位 |

## 修改 CLI / 图后建议流程

1. 改代码
2. `pytest -q`（smoke + unit，~30s）
3. 红：判断是 **测试过时**（字段名/路由合理更新）还是 **功能回归**
4. 测试过时 → 同步更新断言；回归 → 修代码
5. 改了图节点：跑 `pytest tests/unit -v` 验证路由仍正确
6. 改了 CLI JSON 输出：跑 `pytest tests/smoke -v` 验证下游 agent 解析不破

## 新增测试

- **回归 / CLI 契约** → `tests/smoke/`，文件名 `test_*.py`
- **图 / 节点纯逻辑** → `tests/unit/`，文件名 `test_*.py`
- **端到端真实链路** → `tests/e2e/`，加 `@pytest.mark.requires_llm` / `requires_milvus`
- **一次性调研** → `tests/probes/`，文件名**不要**以 `test_` 开头
