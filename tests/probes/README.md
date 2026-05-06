# tests/probes — 调研与诊断脚本

这里放**不被 pytest 自动收集**的一次性调研 / 诊断脚本（文件名故意不以 `test_` 开头，且 `pytest.ini` 用 `norecursedirs` 排除本目录）。

它们不是回归测试，而是为了：

- 排查特定环境下的 GPU / CUDA / transformer kernel 行为
- 探查外部站点（搜索引擎、爬虫目标）的真实结构
- 验证某个底层假设是否成立（attention backend、SDPA kernel、prompt 长度）

每个脚本都设计为 `python tests/probes/<name>.py [args]` 直接跑，输出打到 stdout，**不写持久化数据**（除非脚本头部明确声明）。

## 当前清单

| 脚本 | 来源 | 用途 |
|------|------|------|
| `attn_backend_memory.py` | T11 mineru-html OOM 排查 | 比较 `sdpa` / `eager` / `flash_attention_2` 三种 attention 后端在 16K prompt prefill 时的显存峰值 |
| `sdpa_kernel_memory.py` | 同上 | 强制单一 `SDPBackend`（`EFFICIENT_ATTENTION` / `FLASH_ATTENTION` / `MATH`）测显存差异 |
| `bing_search_probe.py` | GetInfoGraph SERP 调试 | 探查 Bing 搜索结果 DOM 中真实 URL 在哪个属性（`<h2><a>` / `cite` / `a.tilk`） |
| `serp_parsing_probe.py` | 同上 | 直接 goto Bing 搜索页面，dump 计数与样本，用于排查 SERP 解析失败 |

## 使用约定

1. **不要把探针脚本改名为 `test_*`** ——pytest 会把它收集进来执行，附带很重的 GPU / 网络副作用。
2. **执行前留意外部副作用**：
   - `attn_backend_memory.py` / `sdpa_kernel_memory.py` 会下载 `opendatalab/MinerU-HTML-v1.1-hunyuan0.5B-compact`（约 1 GB），需要 CUDA。
   - `bing_search_probe.py` / `serp_parsing_probe.py` 会真实访问 Bing，受限于网络环境与反爬策略。
3. **新增探针**：放在本目录、文件名不以 `test_` 开头、写一个 docstring 说明用途和触发条件即可。
