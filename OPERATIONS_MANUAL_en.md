# brain-base Full Operations Manual

[简体中文](./OPERATIONS_MANUAL.md) | **English**

This manual is for users who "don't want to repeatedly manually confirm permissions, preferring as much automated operation as possible".

Different from the quick start in README, this covers the complete pipeline:

1. Environment Preparation
2. Milvus Startup and Verification
3. QA Agent Full-Permission Startup
4. QA -> Get-Info Automatic Collaboration
5. Background Running Strategies
6. Common Failures and Recovery

---

## 0. Answering Your Most Important Questions First

### Can Claude Code keep Get-Info permanently running in the background, with QA calling it anytime?

Short answer:

1. QA automatically calling Get-Info: Yes.
2. Get-Info as Claude Code built-in "independent常驻 daemon process": Cannot be natively guaranteed.

Feasible approaches:

1. In the same QA session, trigger Get-Info on demand (closest to "background assistance", also recommended mode).
2. Use Windows Task Scheduler to periodically run Get-Info supplementation tasks (truly background periodic operation).
3. Keep a long-term session window open (engineering feasible, but session常驻 rather than system service).

---

## 1. Current Architecture and Call Chain

Standard call chain is:

1. User asks QA a question.
2. **QA first checks self-evolving crystallized layer (`data/crystallized/`)**: hit and fresh → directly return solidified answer; hit but stale → delegate Organize to refresh; miss → continue following RAG process.
3. QA triggers Get-Info when local knowledge is insufficient.
4. Get-Info then calls get-info-workflow and other sub-skills.
5. **After a satisfactory answer**, QA delegates Organize to solidify the answer to `data/crystallized/` for reuse next time.

Note:

1. QA should not directly call persistence skills.
2. Get-Info should not bypass pre-checks to directly ingest.
3. QA should not directly write any files under `data/crystallized/`, all executed by Organize.
4. Organize should not directly call Playwright-cli or write raw layer, completes refresh through Get-Info.

---

## 2. One-Time Preparation (Windows)

Execute in PowerShell (parent directory of `brain-base`):

```powershell
Set-Location "your\path\to\brain-base's parent directory"
```

The `Set-Location "your\path\to\brain-base's parent directory\brain-base"` appearing below means first enter the repository root directory then execute command; the `.` in `claude --plugin-dir .` also refers to current directory.

### 2.1 Install/Confirm Base Dependencies

```powershell
python --version
docker --version
claude --version
npx --version
uv --version
```

If `uv` doesn't exist, install:

```powershell
python -m pip install --user -U uv
```

### 2.2 Install Vectorization and Scraping Dependencies (Global/User-level)

```powershell
python -m pip install --user -U "pymilvus[model]" sentence-transformers FlagEmbedding
npm install -g @playwright/cli@latest
```

Notes:
1. `python -m pip install --user ...` installs to current user's Python user-level directory.
2. `FlagEmbedding` is the underlying inference library for default BGE-M3 hybrid provider, first call downloads ~1.4 GB model to `%USERPROFILE%\.cache\huggingface\`.
3. `npm install -g ...` installs to global Node environment.

For better agent integration, continue per official README; for this project's agent integration scenarios, this step is treated as required:

```powershell
playwright-cli install --skills
```

Verify:

```powershell
playwright-cli --help
```

If using project local installation rather than global, verify with `npx --no-install playwright-cli --help` in project root directory.

### 2.3 Prepare Official Milvus MCP Server Code

If directory doesn't exist:

```powershell
git clone https://github.com/zilliztech/mcp-server-milvus.git .\brain-base\mcp\mcp-server-milvus
```

Your current project connects to MCP server via plugin root directory `.mcp.json` (official plugin structure recommended approach).

---

## 3. Start Milvus (Docker)

Enter plugin directory:

```powershell
Set-Location "your\path\to\brain-base's parent directory\brain-base"
```

Start:

```powershell
docker compose up -d
```

Check status:

```powershell
docker compose ps
```

Health check:

```powershell
curl.exe -i http://localhost:9091/healthz
```

WebUI addresses:

1. Correct: `http://localhost:9091/webui/`
2. Root path `http://localhost:9091/` returning 404 is normal behavior.

---

## 4. Pre-Startup Checks (Must Pass)

Still execute in `brain-base` directory:

```powershell
python bin/milvus-cli.py inspect-config
python bin/milvus-cli.py check-runtime --require-local-model --smoke-test
```

Pass criteria:

1. `can_vectorize` is `true`
2. Can see `local_model` (default `BAAI/bge-m3`; if manually set to sentence-transformer then `all-MiniLM-L6-v2`)
3. `resolved_mode` is `hybrid` (default; `dense` under sentence-transformer)
4. `dense_dim` shows actual dimension (bge-m3 = 1024; all-MiniLM-L6-v2 = 384)

---

## 5. Full-Permission Startup of QA Agent (Automation Mode)

Execute in `brain-base` directory:

```powershell
Set-Location "your\path\to\brain-base's parent directory\brain-base"
claude --plugin-dir . --agent brain-base:qa-agent --dangerously-skip-permissions
```

This command's effects:

1. Load brain-base plugin
2. Specify QA as main agent
3. Skip permission confirmation popups (high automation)

Security notes:

1. `--dangerously-skip-permissions` is officially only recommended for use in trusted, preferably internet-isolated environments.
2. This mode bypasses permission confirmation, web scraping, file writing, and command execution will no longer ask for confirmation item by item.

---

## 6. How QA Triggers Get-Info

In QA session, Get-Info is typically triggered in the following situations:

1. You explicitly request "latest materials", "web supplementation".
2. Local chunks/raw/Milvus evidence is insufficient.
3. Local content is outdated or conflicting.

Recommended question template:

```text
Please first supplement latest official documents from the web, then answer: How to configure MCP scope for Claude Code subagent?
```

You'll see QA call Get-Info in the same task flow to complete supplementation before returning to answer phase.

---

## 7. Three Background Running Schemes

### Scheme A (Recommended): One常驻 QA Session

Characteristics:

1. You mainly converse with QA.
2. Get-Info is automatically called by QA when needed.
3. No need to separately maintain a second background process.

Suitable for: Daily Q&A and on-demand supplementation.

### Scheme B: Scheduled Background Supplementation (Task Scheduler)

Characteristics:

1. Use Windows Task Scheduler to periodically execute `claude -p` supplementation tasks.
2. QA daily answers rely more on already pre-updated local knowledge.

Example command (for scheduled task action):

```powershell
Set-Location "your\path\to\brain-base's parent directory\brain-base"; claude --plugin-dir . --agent brain-base:get-info-agent --dangerously-skip-permissions -p "Execute incremental supplementation for high-priority sites per priority.json, and update raw/chunks/Milvus and keyword statistics."
```

### Scheme C: Open a Separate Get-Info Long Session

Characteristics:

1. You open two terminals: one QA, one Get-Info.
2. Get-Info terminal stays open long-term, manually fed tasks.

Disadvantages:

1. Not a system-level daemon process.
2. Still depends on session continuous existence.

---

## 8. Default Local Vector Model

Default already switched to:

1. provider: `bge-m3`
2. model: `BAAI/bge-m3`
3. retrieval mode: `hybrid` (dense 1024-dim + sparse word-level weights)
4. device: `cpu` (set `KB_EMBEDDING_DEVICE=cuda` when GPU available)

Reasons:

1. Chinese-English mixed semantic ability significantly better than all-MiniLM-L6-v2.
2. Simultaneously produces dense + sparse, can activate this project's hybrid retrieval and synthetic QA recall.
3. CPU first startup downloads ~1.4 GB model. Cached locally after download, no repeat download.

Lightweight fallback option (for weak machines / no Chinese enhancement needed):

```powershell
$env:KB_EMBEDDING_PROVIDER = "sentence-transformer"
python bin/milvus-cli.py check-runtime --require-local-model --smoke-test
```

Note after switching: dense dimension changes from 1024 to 384, must drop old collection then re-ingest chunks. CLI will fail-fast on dim mismatch.

---

## 9. Daily Operations Checklist (Just Follow)

Daily start:

1. `docker compose up -d` (in `brain-base` directory)
2. `python bin/milvus-cli.py check-runtime --require-local-model --smoke-test`. First run downloads BGE-M3 model (1.4 GB).
3. `claude --plugin-dir . --agent brain-base:qa-agent --dangerously-skip-permissions`
4. If new chunk files added that day (frontmatter must contain `questions: [...]`), execute `python bin/milvus-cli.py ingest-chunks --chunk-pattern "data/docs/chunks/*.md"` for hybrid ingestion (CLI simultaneously writes chunk rows and question rows, return report shows `chunk_rows`/`question_rows` counts).
5. When retrieval verification needed, can run multi-query-search in command line to see RRF results: `python bin/milvus-cli.py multi-query-search --query "..." --query "..."`
6. Occasionally check self-evolving crystallized layer status: look at `skills` entry count in `data/crystallized/index.json` and `lint-report.md` (if exists).

Daily end:

1. Exit Claude session
2. Execute `docker compose down` when needing to save resources

---

## 10. Common Failures and Handling

### 10.1 WebUI 404

Symptom: Visiting `http://localhost:9091/` returns 404.

Handling:

1. Use `http://localhost:9091/webui/` instead.

### 10.2 check-runtime Failure (missing pymilvus.model or FlagEmbedding)

Handling:

```powershell
python -m pip install --user -U "pymilvus[model]" sentence-transformers FlagEmbedding
```

If error says "dense dim mismatch" or "collection missing sparse field", indicates provider switched but collection not rebuilt. Handling: Use Milvus MCP or webui to drop old collection (default name `knowledge_base`) then rerun ingest-chunks.

### 10.3 playwright-cli Unavailable

Handling:

```powershell
npm install -g @playwright/cli@latest
playwright-cli --help
```

If using project local installation rather than global, verify with `npx --no-install playwright-cli --help` in project root directory.

### 10.4 Docker Open but Milvus Unhealthy

Handling:

```powershell
docker compose ps
docker compose logs --tail=200
```

Confirm `etcd`, `minio`, `standalone` three containers are running.

### 10.5 Self-Evolving Crystallized Layer Failures

#### Solidified Answer Returns Wrong Content

Handling: Explicitly say this is wrong or outdated in the same session, qa-agent will notify organize-agent to mark the skill as `rejected`. Next `crystallize-lint` will delete the entry. Same question asked again will rerun full RAG pipeline.

#### Solidified Answer Clearly Outdated But Not Auto-Refreshed

Root cause: Solidified skill's `last_confirmed_at + freshness_ttl_days` hasn't expired yet.

Handling: Explicitly say "I need latest materials" in session, qa-agent will force trigger refresh; or manually shorten `freshness_ttl_days` before asking again.

#### `data/crystallized/index.json` Corrupted

Symptom: qa-agent reports JSON parse failure on startup, automatically degrades to `miss`.

Handling:

```powershell
Set-Location "your\path\to\brain-base\data\crystallized"
Get-ChildItem index.json.broken-* | Select-Object -First 1
# View backup file, manually repair then have organize-agent run crystallize-lint
```

Or directly delete `index.json` and let qa-agent auto-rebuild empty index on next startup, cost being `<skill_id>.md` files on disk will be treated as orphan files by `crystallize-lint` and moved to `_orphans/` directory for manual review.

#### Crystallized Layer Accumulates Too Much Interfering with Q&A

Handling: Run `crystallize-lint`. In `claude --plugin-dir . --agent brain-base:organize-agent --dangerously-skip-permissions` session say "run lint on crystallized layer", will automatically clean rejected / over 3× TTL / orphan / corrupted entries.

---

## 11. Two Commands You Can Directly Copy

### One-Click Start Base Environment

```powershell
Set-Location "your\path\to\brain-base's parent directory\brain-base"; docker compose up -d; python bin/milvus-cli.py check-runtime --require-local-model --smoke-test
```

### One-Click Enter Full-Permission QA

```powershell
Set-Location "your\path\to\brain-base's parent directory\brain-base"; claude --plugin-dir . --agent brain-base:qa-agent --dangerously-skip-permissions
```

---

## 12. Self-Evolving Crystallized Layer (Crystallized Skill Layer)

This project added **Self-Evolving Crystallized Layer** on 2026-04-18, benchmarked against Karpathy [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) pattern. No extra operation needed for daily use, qa-agent and organize-agent auto-handle. Below is informational description.

### 12.1 Where Are Solidified Answers Stored

```
data/crystallized/
├── index.json             # Global index
└── <skill_id>.md          # Each solidified skill one file
```

Whole directory is `.gitignore`d, won't enter repository. Auto-created by `organize-agent` on first write.

### 12.2 Solidified Answer Lifecycle

| Phase | Trigger Timing | Action |
|-------|----------------|--------|
| Creation | You ask qa-agent a new question, it gives answer meeting solidification conditions | Write `<skill_id>.md` + update `index.json`, `revision=1`, `user_feedback=pending` |
| Reuse | You ask similar question again | qa-agent hits `hit_fresh` direct return, marks `📦` at answer beginning |
| Refresh | Hit skill exceeds TTL, or you explicitly say "latest" | organize-agent carries original `execution_trace` + `pitfalls` calls get-info-agent to update knowledge base, qa-agent regenerates answer, overwrite write back, `revision+=1` |
| Confirm | You don't reject solidified answer in next round of dialogue | `pending` → `confirmed`, `last_confirmed_at` refresh |
| Reject | You explicitly say "wrong"/"not satisfied" | `confirmed`/`pending` → `rejected`, `crystallize-lint` cleans next time |
| Supplement | You actively supplement information | `pitfalls` append "This round omitted: <summary>", `revision+=1` |
| Cleanup | `crystallize-lint` runs | Delete `rejected` / over 3× TTL unconfirmed entries, orphan files moved to `_orphans/` |

### 12.3 TTL Default Values

`organize-agent` judges by topic on first solidification:

| Topic Type | TTL |
|------------|-----|
| Stable Concepts (Algorithms / Architecture / Design Philosophy) | 180 days |
| Product Documentation (Configuration / Commands / APIs) | 90 days |
| Rapidly Iterating Topics (beta features / previews) | 30 days |

You can manually edit corresponding `.md` file frontmatter `freshness_ttl_days` to override default.

### 12.4 Manual Maintenance Commands

Start organize-agent session, then speak natural language commands:

```powershell
Set-Location "your\path\to\brain-base"
claude --plugin-dir . --agent brain-base:organize-agent --dangerously-skip-permissions
```

Common natural language commands:

1. `run lint on crystallized layer` → Execute `crystallize-lint`
2. `force refresh skill <skill_id>` → Regardless of TTL expiration, immediately walk refresh path
3. `list all pending skills` → Export entries with `user_feedback=pending` from `index.json`

### 12.5 Why Not Use Scheduled Tasks

Crystallized layer writes, refreshes, and feedback processing are all **event-driven** (user question / satisfied answer / feedback), no scheduled tasks needed. `crystallize-lint` triggered manually in session, no need to run periodically.

---

## 13. Conclusion

Your goal "default automation, minimal interruption" is achievable:

1. QA main session + auto-trigger Get-Info (recommended main mode).
2. Self-evolving crystallized layer automatically collaborates between qa-agent and organize-agent, no user intervention needed.
3. For truly background continuous supplementation, coordinate with task scheduler for periodic operation.

But to be clear:

1. Claude Code is currently not a built-in "常驻 background service orchestrator".
2. Need to rely on session常驻 or system scheduling to achieve continuous background behavior.
