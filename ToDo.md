# brain-base LangGraph 重构 ToDo

> **历史归档**：上一阶段 T57 已归档至 `md/archive/ToDo-Phase-T57-T57.md`。

---

## 任务概览

- T58 E2E 基线测试（最低）— finished
- T59 MambaOut 补位 + GPU 等待函数手工验证（最低）— pending
- T60 upload_qa evidence 匹配 doc_id 诊断（最低）— pending

---

## T58 E2E 基线测试 — finished

> **背景**：主图与子图已有 unit/smoke 覆盖，但缺少一份可复跑的真实端到端基线，覆盖 QA、文件上传后 QA、删除链路，并把中间 state 与结果落盘供后续回归对比。
>
> **范围**：
>
> - 3 条真实 QA：每条记录输入、答案、evidence 数、关键状态、耗时与 state dump。
> - 2 条本地 papers 上传后 QA：用 `papers/` 下 PDF 入库，再对上传内容提问并验证 evidence 命中上传文档。
> - 删除验证：对上传文档执行 dry-run 与 confirm 删除，验证 raw/chunks/uploads 与 Milvus 检索影响面。
> - 产物落盘到 `data/logs/t58_e2e_baseline/`。
>
> **执行前计划文档**：`md/research/2026-05-21-t58-e2e-baseline.md`
>
> **当前进展**：
>
> - 已新增 `tests/e2e/test_t58_e2e_baseline.py`，真实调用 LLM / Milvus / IngestFileGraph / LifecycleGraph，不使用 mock。
> - 已完成 3 次真实 QA 验证：`data/logs/t58_e2e_baseline/20260521_064514/` 下 3 条 QA 全部 PASS，state dump + summary 已落盘。
> - 已修正脚本控制流：上传失败或没有 `uploaded_doc_ids` 时立即记录失败并返回，不再错误继续跑上传后 QA。
>
> **历史排查过程**（保留供回溯）：
>
> - 第二次运行：差 86~90 MB 被 14000 MB 阈值挡，加 `KB_MINERU_VRAM_LIMIT_MB=13000` 重跑。
> - 第三次运行：通过预检启动 MinerU，hybrid pipeline 第 2 个 Predict 阶段进入显存边缘 swap，14 分钟磁盘无写入被手动终止。
> - 根因定位（用户反问推动的源码级排查）：MinerU 的 `mineru/backend/hybrid/hybrid_analyze.py:get_batch_ratio()` 按**显卡总显存**（不扣已占用）决定 batch 乘数，16 GB GPU → ratio=8，让 MFR=128/OCR=64/LAYOUT=8 张一批。Windows 主机 + WDDM + 桌面 2.2 GB 占用下显存不够，撞墙退化 swap；Linux 容器无此问题。详见 `md/research/2026-05-21-t58-e2e-baseline.md` 「根因定位」段落。
>
> **实际产出**（2026-05-21 完成）：
>
> - **配置修复**：`.env` 新增 `MINERU_HYBRID_BATCH_RATIO=4`（强制按 12 GB 档跑，所有 sub-task batch 减半），`KB_MINERU_VRAM_LIMIT_MB=13000` 配套；两项均带详细中文注释说明 Windows 主机调优依据。
> - **测试脚本增强**：`tests/e2e/test_t58_e2e_baseline.py` 新增 `_gpu_free_mb()` + `_wait_for_gpu_release(target_mb, max_wait_sec=120, poll_sec=3)` 辅助函数，UPLOAD_CASES 主循环在 case 之间调用，解决 MinerU 子进程退出后 Windows WDDM 驱动延迟回收显存导致下一 case 预检 fail 的问题。等待 report 写入 case result 的 `gpu_release_wait` 字段。
> - **根因文档**：`md/research/2026-05-21-t58-e2e-baseline.md` 「根因定位」段落完整记录三层 batch 嵌套（页级 / hybrid sub-task / VLM）、Linux 容器为什么没事的三层原因、三档修复方案（ratio=4 / ratio=2 / Docker 路由）。
> - **第四次运行（`data/logs/t58_e2e_baseline/20260521_080826/`）验证**：
>   - 3 条 QA 全 PASS（语义未退化）。
>   - SkillRouter 上传：MinerU 完整转换 44.5 min，character=118198, 切 24 chunks, milvus 写入 200 行，`direct_search_has_doc=true`，`ingest_passed=true`（**ratio=4 修复有效，PDF 转换不再卡死**）。
>   - 删除验证 PASS：rows_deleted=200, paths cleaned。
>   - 两个跟进项转 T59 / T60（见下）。
>
> 优先级:**最低**（已完成主链路验证）。

---

## T59 MambaOut 补位 + GPU 等待函数手工验证 — pending

> **背景**：T58 第四次运行时第二个 PDF（MambaOut）因 SkillRouter 转换结束 GPU 显存未及时释放被预检挡住（`free=10997 < 13000`）。T58 已修测试脚本（`tests/e2e/test_t58_e2e_baseline.py` 加 `_wait_for_gpu_release`），但修复完未重跑验证。
>
> **范围**：
>
> - 手工执行 `python tests/e2e/test_t58_e2e_baseline.py`（完整 e2e ~50 min），验证 case 间 GPU 等待函数能否让 MambaOut 顺利启动。
> - 检查产出的 `summary.json`：`upload_cases[1].gpu_release_wait` 字段应该是 `{ok: true, free_mb: >=13000, elapsed_sec: <120}`，`upload_cases[1].ingest_passed=true`。
> - 如果 GPU 等待 timeout（120s 仍 < 13000）：把 `max_wait_sec` 提到 180s，或在 `_wait_for_gpu_release` 起头加 `import gc; gc.collect()` + `torch.cuda.empty_cache()` 主动催回收。
>
> **不需要做**：MinerU 转换本身的进一步优化已在 T58 完成。本任务只验证 GPU 等待函数。
>
> 优先级：**最低**。

---

## T60 upload_qa evidence 匹配 doc_id 诊断 — pending

> **背景**：T58 第四次运行 SkillRouter 上传成功（`direct_search_has_doc=true`、Milvus 200 rows），但 `upload_qa_matched_uploaded_doc=false`、`answer_len=61`、`evidence_count=0`。即"先 upload 再 qa"链路里 QA pipeline 答了问题但 evidence 没引到刚上传的 doc_id。
>
> **范围**：
>
> - 读 `data/logs/t58_e2e_baseline/20260521_080826/upload/upload_skillrouter_qa_state.json` 看 QA 实际拿到的 evidence 列表（如果存在）。
> - 对比 `upload_skillrouter_direct_search.json`：直接向 Milvus 查同一 question + doc_id 已确认能命中（`direct_search_has_doc=true`），说明问题在 QA 检索 / rerank / answer 阶段而非 ingest。
> - 候选猜测（待诊断）：
>   - QA 检索分数阈值过严，刚 ingest 文档未进 top-k。
>   - QA pipeline 走了固化层 / crystallized 命中而跳过原始 doc 检索。
>   - evidence 提取节点把命中的 chunk 过滤掉了。
> - 修复后在 T59 同次运行中验证（这两个跟进项可以合并跑）。
>
> 优先级：**最低**。
