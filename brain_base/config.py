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
