"""
brain_base 默认配置。
具体 Milvus/Embedding 参数参考 ../brain-base-backup/bin/milvus_config.py。
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class GetInfoConfig:
    """QA → 自动外检 → 入库 → 重检索 闭环的可调参数。

    所有阈值通过这个 dataclass 注入；`QaGraph(get_info_config=GetInfoConfig(max_total=4))`
    可按场景覆盖默认值。不传走默认值。
    """

    # ---- 顶层开关 ----
    enable: bool = True
    """False 时 get_info_trigger 强制返回 needed=False，等同于关掉外检回路。"""

    max_rounds: int = 1
    """允许的外检轮数；当前实现只支持 1，预留给 Phase 11 多轮外检。"""

    # ---- 入库阶段（select_candidates + ingest_candidates） ----
    max_official: int = 5
    """单次入库最多接收的 official-doc 候选数。"""

    max_community: int = 3
    """单次入库最多接收的 community 候选数。"""

    max_total: int = 6
    """单次入库总数上限（优先级高于分类配额，防爆炸）。"""

    batch_timeout: float = 90.0
    """ingest_candidates 整批超时秒数；超时后跳过剩余候选直接 re_search。"""

    single_url_timeout: float = 30.0
    """单个 URL 走 IngestUrlGraph 的最长耗时；超时记入 ingest_errors。"""

    # ---- GetInfoGraph 内部循环 ----
    get_info_max_iter: int = 3
    """GetInfoGraph 最多跑几轮 plan-search-classify。"""

    get_info_target_official: int = 2
    """官方文档候选 ≥ 此数提前终止 GetInfoGraph。"""

    get_info_total_timeout: float = 60.0
    """GetInfoGraph 整体超时秒数。"""

    # ---- T25 fetch_extract（多 URL 爬取处理）----
    fetch_extract_concurrency: int = 3
    """fetch_extract_one 节点 LLM 调用并发上限（避免 LLM API 限流）。"""

    search_pages_per_engine: int = 2
    """每个搜索引擎抓多少页（默认 2 页 ≈ 20 个 URL/引擎）。"""

    # ---- T29 SERP 限速节流（反反爬）----
    serp_concurrency: int = 3
    """search_web_dual 节点 SERP 抓取的全局并发上限（同一时刻最多几个 chromium page 在搜）。
    超过此值排队；不影响 fetch_extract / subquery_search 等其他阶段并发。"""

    serp_min_interval_sec: float = 10.0
    """同一搜索引擎相邻两次请求之间的最短间隔（秒）。
    抑制"短时间多请求"反爬模式（google sorry/unusual_traffic）；不同 engine 间不互等。"""

    serp_max_interval_sec: float = 20.0
    """同一搜索引擎相邻两次请求之间的最长间隔（秒）；实际间隔在 [min, max] 内 uniform 随机抖动。
    模拟人类点击节奏，进一步降低反爬命中率。"""

    # ---- T26.1-c enrich_one（chunk 富化）----
    enrich_concurrency: int = 3
    """enrich_one 节点 LLM 调用并发上限（独立于 fetch_extract，避免两阶段串行重叠时计数污染）。"""

    # ---- T28 PIPE2 subquery_search_one（每子问题独立 milvus + rerank）----
    search_concurrency: int = 3
    """subquery_search_one 节点 milvus + rerank 调用并发上限（每子问题 1 个 Send，多子问题并发上限）。"""

    # T47.6 删除：T40 enable_search_strategy 配置项。search_strategy / merge_search_keywords /
    # search_web_dual 三个节点 T47.4 已从主图拔除（统一意图识别 Agent-Loop 替代），T47.6
    # 同步删除节点函数 + 配置开关，0 业务代码引用。

    # ---- T47 统一意图识别 Agent-Loop ----
    max_intent_iterations: int = 5
    """intent_planner ↺ intent_executor ↺ intent_observer 循环最大迭代次数（D5 拍板）。
    should_continue_intent 触发 iteration_count >= max_intent_iterations 时强制早退到 merge_evidence。
    可通过 env BB_MAX_INTENT_ITERATIONS 覆盖（cli 入口读取）。"""


DEFAULT_CONFIG: dict = {
    "llm_provider": os.environ.get("BB_LLM_PROVIDER", "anthropic"),
    "deep_think_llm": os.environ.get("BB_DEEP_THINK_LLM", "claude-sonnet-4-20250514"),
    "quick_think_llm": os.environ.get("BB_QUICK_THINK_LLM", "claude-sonnet-4-20250514"),
    "data_dir": os.environ.get("BB_DATA_DIR", str(PROJECT_ROOT / "data")),
    "milvus_uri": os.environ.get("KB_MILVUS_URI", "http://localhost:19530"),
    "milvus_collection": os.environ.get("KB_MILVUS_COLLECTION", "knowledge_base"),
    "embedding_provider": os.environ.get("KB_EMBEDDING_PROVIDER", "bge-m3"),
    "search_top_k": 10,
    "rrf_k": 60,
    "use_rerank": True,
    "crystallized_freshness_ttl_days": 30,
    "checkpoint_enabled": True,
}
