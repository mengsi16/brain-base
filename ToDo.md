# ToDo（Phase 10：QA → 自动外检 → 入库 → 重检索 闭环接通）

> 状态：`pending` / `executing` / `finished`。
> 上一阶段（Phase 1–9）归档于 `md/archive/ToDo-Phase-1-9.md`。

## 背景

`QaGraph` 当前的 `judge` 在 `evidence_sufficient=false` 时**短路直奔 `answer`**，导致本地查不到时直接返回「未找到本地证据」。设计上应该闭环（backup 旧 qa-workflow 步骤 5 / 5.5），但接线没接通。零件已齐：

- `GetInfoGraph` 子图（多步搜索循环）→ 输出 `candidates: list[{url, source_type, title_hint, confidence, snippet}]`
- `IngestUrlGraph` 子图 → 单 URL 抓取 + 清洗 + 分块 + 入 Milvus
- `GetInfoTrigger` Pydantic schema + `GET_INFO_TRIGGER_SYSTEM_PROMPT`
- `EvidenceJudgment.recommendation` 已含 `trigger_get_info` 枚举值
- `bin/source-priority.py` 五档分级（P0 official+新 → P3 community+旧 → P5 unknown）已落 chunk frontmatter

需要做的就是**在 QaGraph 里接进 4 个新节点 + 改一处路由 + 加 4 个 state 字段**。

---

## 设计要点（针对用户三个问题）

### Q1：单次最多入几个 URL？

**分层配额而非固定数字**：

| 类型 | 单次入库上限 | 说明 |
|---|---|---|
| `official-doc` | 5 | 优先吸收官方文档 |
| `community` | 3 | 社区内容限额，避免被低质量博客污染 |
| `discard` | 0 | 直接过滤 |
| 总计 | **≤ 6** | 上限优先级高于分类配额（防爆炸） |

**整批超时**：`web_research + ingest_candidates` 合计 90 秒（Milvus 写入慢则跳过剩余）。

### Q2：怎么判断 URL 内容值不值得入库？

**5 层过滤**（前 4 层 GetInfoGraph 已做，第 5 层新增）：

1. 域名黑名单（pinterest / facebook / reddit-poll → discard）— `_DISCARD_DOMAIN_HINTS`
2. 域名白名单（docs. / developer. / github.com → official-doc）— `_OFFICIAL_DOMAIN_HINTS`
3. LLM `UrlClassification` schema 综合判 official-doc / community / discard
4. discard 直接不进 candidates（`get_info.py:165` 已实现）
5. **入库前去重（新增）**：对每个 candidate 用 `tools.milvus_client.hash_lookup` 查 URL 是否已入库（按 url 字段精确匹配），命中跳过

### Q3：什么 URL 值得识别为高优先级？

**5 个识别信号**（在 GetInfoGraph 的 classify 节点 + select_candidates 节点共同实现）：

- **白名单命中**：`data/priority.json.official_domains` 命中 → 直接 P0 official-doc
- **结构信号**：URL 含 `/docs/` / `/api/` / `/reference/` / `/guide/` / `/blog/`
- **域名信号**：`docs.*` / `developer.*` / `github.com/*` / `*.io/docs`
- **时效信号**：search 已用 Google `after:` / `before:` 操作符做时间窗口（`web_fetcher.py` 已实现）
- **黑名单信号**：短视频 / SEO 农场 / 论坛投票页 → discard

入库后 `bin/source-priority.py` 五档分级（P0–P5）会写进 chunk frontmatter，检索时 RRF 自动按优先级排序——这部分**入库子图已经做了**，本次不重复。

---

## T10 接通 QA 自动外检 + 入库回路 — finished

### 实际产出（2026-05-06 收尾）

闭环已通：本地无证据时自动外检 → ingest LiteLLM 文档 → 重检索 → answer 给出包含真实代码示例与证据汇总表的高质量回答。验收 e2e（`python -B tmp_e2e_trace.py`）：

- `evidence_sufficient = True`，`evidence count = 7`，`crystallized_status = degraded`
- 答案含三段式结构（是什么 / 解决了什么 / 怎么用）+ Python SDK / Proxy Server 代码示例 + 7 条 official-doc 证据汇总
- raw（`docs-litellm-ai_untitled-2026-05-06.md` 4180 字节）+ chunks（4175 字节）+ Milvus（chunks_count=1）三层落盘一致

收尾期补的关键修复（落在 `brain_base/agents/utils/agent_states.py` / `brain_base/graphs/ingest_url_graph.py` / `brain_base/agents/utils/structured.py` / `brain_base/nodes/qa.py` / `bin/milvus-cli.py` / `bin/milvus_config.py`），具体行为已固化为 CLAUDE.md 规则 46-50。

---

## T10 接通 QA 自动外检 + 入库回路（历史背景） — finished

### 配置接口化（新增）

新增 `brain_base/config.py:GetInfoConfig` dataclass，所有阈值通过它注入，默认值写在 dataclass 字段里：

```python
@dataclass
class GetInfoConfig:
    # 入库阶段（select_candidates + ingest_candidates）
    max_official: int = 5
    max_community: int = 3
    max_total: int = 6
    batch_timeout: float = 90.0
    single_url_timeout: float = 30.0
    # GetInfoGraph 内部循环
    get_info_max_iter: int = 3
    get_info_target_official: int = 2
    get_info_total_timeout: float = 60.0
    # 顶层开关
    enable: bool = True            # False 时 get_info_trigger 强制返回 needed=False
    max_rounds: int = 1            # 当前只支持 1，预留给 Phase 11
```

注入路径：`QaGraph(llm=..., get_info_config=GetInfoConfig(max_total=4))`，节点工厂通过闭包捕获 config 读取阈值。不传走默认值。

### plan_node 顺手修复（Q1 答的两个小问题）

- `candidates_brief` 加上 `title_hint`，让 plan LLM 看清"已有什么类型的候选"
- user_prompt 显式放 `用户问题：{user_question}`（之前漏了）



### 新增节点（`brain_base/nodes/qa.py`，~120 行）

| 节点 | 类型 | 输入 | 输出 |
|---|---|---|---|
| `create_get_info_trigger_node(llm)` | LLM 工厂 | question, evidence, infra_status | `{trigger_get_info: bool, search_hint: str, get_info_reason: str}` |
| `web_research_node` | 纯 Python | question, search_hint, llm | `{get_info_candidates: list[dict], get_info_attempted: True}` |
| `create_select_candidates_node()` | 纯 Python | candidates | `{ingest_targets: list[dict]}`（按配额筛选+去重） |
| `ingest_candidates_node` | 纯 Python | ingest_targets, llm | `{get_info_ingested: list[str], ingest_errors: list[str]}` |
| `re_search_node` | 纯 Python（复用 search_node 实现） | rewritten_queries | `{evidence: list[dict]}`（覆盖原 evidence） |

**关键实现细节**：

- `get_info_trigger`：llm=None 启发式 = `evidence=[] && infra_status.playwright_available && !get_info_attempted` → True
- `web_research`：内部 `GetInfoGraph(llm).run(question, max_iterations=3, target_official_count=2, total_timeout=60)`
- `select_candidates`：
  ```
  official = [c for c in candidates if c.source_type == "official-doc"][:5]
  community = [c for c in candidates if c.source_type == "community"][:3]
  picked = (official + community)[:6]
  # 用 hash_lookup 过滤已入库 URL
  picked = [c for c in picked if not _url_already_ingested(c.url)]
  ```
- `ingest_candidates`：串行（**第一版不并行**——便于跟踪 + 单 URL 失败容忍），单 URL 30s 超时，整批 60s 超时；失败记入 `ingest_errors` 不阻断后续
- `re_search`：直接调 `tools.milvus_client.multi_query_search(rewritten_queries)`，把结果赋给 `evidence`

### State 新增字段（`brain_base/agents/utils/agent_states.py:QaState`）

```python
get_info_attempted: bool          # 防死循环（第二次到 judge 强制 answer）
get_info_candidates: list[dict]   # GetInfoGraph 返回的全部候选
ingest_targets: list[dict]        # 经 select_candidates 筛选后的入库目标
get_info_ingested: list[str]      # 成功入库的 doc_id 列表
ingest_errors: list[str]          # 入库失败的 URL + 错误信息
```

### 路由改造（`brain_base/graph/conditional_logic.py`）

```python
def after_judge(self, state):
    if state.get("evidence_sufficient", False):
        return "answer"
    if state.get("get_info_attempted", False):
        return "answer"          # 第二轮强制 answer，防死循环
    return "get_info_trigger"

def after_get_info_trigger(self, state):
    if not state.get("trigger_get_info", False):
        return "answer"
    if not state.get("infra_status", {}).get("playwright_available", False):
        return "answer"          # 软依赖降级
    return "web_research"
```

### QaGraph 拓扑改造（`brain_base/graphs/qa_graph.py`）

```
… judge ─┬─ evidence_sufficient=true ─→ answer
         └─ false ─→ get_info_trigger ─┬─ needed=false ─→ answer
                                       └─ needed=true ─→ web_research
                                                       → select_candidates
                                                       → ingest_candidates
                                                       → re_search
                                                       → judge（第二轮，attempted=True 强制走 answer）
```

### 验收（必须全部通过）

1. **单元**：
   - `select_candidates`：10 候选（3 official / 5 community / 2 discard）→ 输出 6（3+3）
   - `select_candidates` + 去重：候选 URL 中 2 个已入库 → 输出排除这 2 个
   - `get_info_trigger` 启发式：evidence=[] + playwright=true + not attempted → True
   - 防死循环：第二次到 judge 不管 evidence_sufficient 取值都走 answer
2. **端到端**（`python -B tmp_e2e_trace.py`）：
   - 链路 ≥ 14 节点：probe → crystallized_check → normalize → decompose → rewrite → search → judge → get_info_trigger → web_research → select_candidates → ingest_candidates → re_search → judge → answer → self_check → crystallize_answer
   - `evidence` 在 re_search 后非空（拿到至少 1 条 LiteLLM 入库结果）
   - `get_info_ingested` 至少 1 个 doc_id
   - 最终 `answer` 包含 LiteLLM 真实信息（不再是「未找到本地证据」）

### 工程量

| 文件 | 改动 | 估行数 |
|---|---|---|
| `brain_base/nodes/qa.py` | 新增 5 个节点工厂 | +120 |
| `brain_base/graphs/qa_graph.py` | 加节点 + 改路由 | +20 |
| `brain_base/graph/conditional_logic.py` | 改 `after_judge` + 新 `after_get_info_trigger` | +15 |
| `brain_base/agents/utils/agent_states.py` | 新增 5 字段 | +10 |
| 总计 | 全部新增/扩展，不删除既有代码 | **+165** |

### 不在本批范围内

- 并行 ingest（用 langgraph `Send` API）— 第一版串行，验证闭环后再考虑
- `priority.json` 白名单读取 — classify 节点已经能用启发式 + LLM 做掉，白名单是后续优化
- 自愈触发（recall trace + 问题自愈）— backup 项目 self-heal-workflow 的事，不在本批
- 多轮外检（attempted 后还不够再触发）— 第一版只允许 1 轮外检

---

## T11 mineru-html 路由到 Docker 容器（解决 16GB GPU OOM） — finished

### 实际产出（2026-05-06）

- `Dockerfile`：apt 镜像切清华、cu124 torch 强制覆盖、删 trafilatura、加 `libcairo2 / libpango-1.0-0 /
  libpangocairo-1.0-0 / libgdk-pixbuf-2.0-0`（webpage_converter 转 markdown 必需）
- `docker-compose.yml`：加 `./bin:/app/bin` + `./data/temp:/app/data/temp` 热挂载
- `brain_base/tools/doc_converter_tool.py`：subprocess 改走 `docker compose exec`，路径主机↔容器双向转换
- `bin/doc-converter.py`：
  1. `_strip_html_noise`：BS4 剥 `<script>/<style>/<link rel=prefetch|preload>/<noscript>/<iframe>/<svg>` 噪音
     （Docusaurus 134KB 页 → 109KB；prefetch 链接被 SLM 当正文导致主体识别失败的根源之一）
  2. `_LeanTransformersBackend`：自实现绕开 mineru_html 的 pipeline 二次 dispatch（实测 OOM 19GB → 1.10GB）
  3. **head + tail 截断**：保留 prompt 头部 HTML + 尾部 128 tokens 终结指令 `<｜hy_Assistant｜><think>`
     （之前纯 tail 截断丢了终结指令导致 SLM 输出 `1main2main...3050main` 死循环到 max_new_tokens 上限）
  4. monkey-patch `caching_allocator_warmup`（Windows WDDM 兼容性，容器内可关）
  5. 可选 dump（`BB_MINERU_HTML_DUMP_DIR`）：落盘 prompt / SLM raw output / case state，便于排查
- `brain_base/tools/web_fetcher.py`、`brain_base/nodes/ingest_url.py`：删 trafilatura，全走 playwright
- `brain_base/cli.py` + `.env` / `.env.example`：dotenv 自动加载，LLM API key/base_url 走环境变量
- `requirements.txt`：删 trafilatura

### 验收数据

| 配置                              | prompt_tokens | new_tokens | 耗时    | 输出                              |
|-----------------------------------|---------------|------------|---------|-----------------------------------|
| 16K input + 8K gen（旧裁尾）      | 16384         | 8192       | 337.7s  | 主体为空（终结指令丢失，模型死循环） |
| 8K input + 4K gen（旧裁尾）       | 8192          | 4096       | 132.2s  | 主体为空（终结指令丢失，模型死循环） |
| 8K input + 4K gen（**head+tail**）| 8192          | 513        | 17.4s   | 2242 字符 markdown                |
| **16K input + 8K gen（head+tail）**| 16384        | ~1.5K      | 32.5s   | **6432 字符 markdown 最完整**     |

GPU peak 仅 **1.10GB**（4060Ti 16GB 富余），flash kernel 在容器 Linux PyTorch 下稳定可用。
推荐生产配置：`BB_MINERU_HTML_MAX_INPUT=16384` / `MAX_TOKENS=8192` / `GPU_MEM=10GiB`。

### 关键经验（写进项目硬约束）

1. **mineru_html 链路最末步依赖 libcairo**：缺失时报"主体为空"误导排查方向，必须在 Dockerfile 装齐
2. **SLM prompt 截断必须保留尾部终结指令**：纯裁尾会让模型停不下来退化成 token 重复循环
3. **Docusaurus / SPA 站送进 SLM 前必须剥 prefetch link**：head 里几百个 prefetch 占满 token 窗口

---

## T11 mineru-html 路由到 Docker 容器（历史背景） — finished

### 背景

主机 Windows 上 mineru-html 0.5B 模型在 16K prompt prefill 时 OOM：hunyuan attention 用 4D float mask
强制 PyTorch SDPA 走 math kernel（O(n²) 显存）。验证过 `attn_implementation=sdpa` + 强制 `EFFICIENT_ATTENTION`
后端均无效（Q/K/V 头数不一致禁用 fused kernel；Windows PyTorch 编译时未启用 flash kernel）。
Linux PyTorch 默认带 flash kernel，4060Ti SM89 完全支持，能跑 16K-32K prompt 不 OOM。

### 工作量清点

- `docker-compose.yml`：加 `./bin:/app/bin` 和 `./data/temp:/app/data/temp` volume（2 行）
- `Dockerfile`：已加 cu124 强制覆盖、删 trafilatura（已完成）
- `requirements.txt`：删 trafilatura（已完成）
- `brain_base/tools/web_fetcher.py`：删 trafilatura，全走 playwright（已完成）
- `brain_base/nodes/ingest_url.py`：单次 playwright 抓取（已完成）
- `brain_base/tools/doc_converter_tool.py:convert_html_to_markdown`：subprocess 调 docker compose exec 路由
  到容器内 `/app/bin/doc-converter.py`（约 60 行修改 + 临时路径走 `data/temp/`）
- `docker compose build --no-cache brain-base-worker`：重建镜像（一次性 8 分钟）
- 端到端测试：跑 LiteLLM 问答（16K HTML 页）确认不 OOM

### 执行步骤

1. 改 `docker-compose.yml` 加 volume（bin/ + data/temp/）
2. 起后端 build（用户等候）
3. 改 `doc_converter_tool.convert_html_to_markdown`：本地 HTML 写到 `./data/temp/<id>.html`，subprocess 调
   `docker compose exec -T brain-base-worker python /app/bin/doc-converter.py convert /app/data/temp/<id>.html ...`，
   读取 `./data/temp/<id>.md` 拿结果
4. build 完成后跑 16K HTML 测试，确认 GPU + flash kernel 工作
5. 跑 e2e LiteLLM 问答验证

### 风险

- Windows 主机 subprocess 调 `docker compose exec` 的编码：用 `-T` 关闭 TTY，stdout 用 utf-8 解码
- 容器 build 期间下载 cu124 wheel 约 2GB；HF 缓存挂载持久化避免重下 bge-m3
- 路径转换：`./data/temp/foo.html`（主机）↔ `/app/data/temp/foo.html`（容器），需要在调用处统一

### 不在本批范围

- brain-base 主进程进容器（QaGraph 仍在主机）
- mineru[pipeline] PDF 解析也路由到容器（本批仅 mineru-html）
- 容器 health check（容器跑完即退，不需要 long-lived monitor）

---

## 通用规则（参考 `CLAUDE.md`）

- ToDo 驱动：`pending` → `executing` → `finished`
- 执行前清点工作量；超出预估及时回头对齐
- 完成后写 `finished` 简要产出，下一个 Agent 看得到
- 软依赖：playwright / Milvus 不可用时静默降级，不阻断 answer
- fail-fast：单 URL 抓取/入库失败记入 `ingest_errors`，整批继续
