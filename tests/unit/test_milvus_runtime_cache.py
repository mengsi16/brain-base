# -*- coding: utf-8 -*-
"""T41.5: 验证 milvus_config.build_embedding_runtime 和 milvus-cli._build_reranker
模块级缓存——同一参数多次调用复用同一实例 + 多线程并发只构造一次。

不依赖真实 pymilvus.model / FlagEmbedding（mock 掉）；只验缓存语义本身。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 把 bin/ 加入 sys.path 以便 import milvus_config
_BIN_DIR = Path(__file__).resolve().parents[2] / "bin"
if str(_BIN_DIR) not in sys.path:
    sys.path.insert(0, str(_BIN_DIR))


# ===========================================================================
# build_embedding_runtime 缓存测试
# ===========================================================================


@pytest.fixture
def mock_pymilvus_model(monkeypatch):
    """Mock pymilvus.model 模块——所有 EmbeddingFunction 类返回 MagicMock 实例，
    每次实例化都有独立的 id（用于断言"是否真的构造了多次"）。"""
    fake_model = MagicMock()
    fake_model.DefaultEmbeddingFunction = MagicMock(side_effect=lambda *a, **kw: MagicMock(name="default"))
    fake_model.dense.SentenceTransformerEmbeddingFunction = MagicMock(
        side_effect=lambda *a, **kw: MagicMock(name="st")
    )
    fake_model.dense.OpenAIEmbeddingFunction = MagicMock(
        side_effect=lambda *a, **kw: MagicMock(name="openai")
    )
    fake_model.hybrid.BGEM3EmbeddingFunction = MagicMock(
        side_effect=lambda *a, **kw: MagicMock(name="bge-m3")
    )
    # find_spec("pymilvus.model") 返回非 None，且 from pymilvus import model 拿到 fake_model
    fake_pymilvus = MagicMock()
    fake_pymilvus.model = fake_model
    monkeypatch.setitem(sys.modules, "pymilvus", fake_pymilvus)
    monkeypatch.setitem(sys.modules, "pymilvus.model", fake_model)
    return fake_model


@pytest.fixture
def mc(monkeypatch):
    """加载 milvus_config + 清空缓存 + patch find_spec 让 pymilvus.model 可见。"""
    import milvus_config  # noqa: E402

    # 清空缓存防其他测试污染
    milvus_config.reset_embedding_runtime_cache()

    # patch find_spec 永远返非 None（mock 后真 pymilvus.model 不存在但我们假装它存在）
    monkeypatch.setattr(milvus_config, "find_spec", lambda name: object())

    # patch 离线 / HF endpoint 检测——避免测试时联网
    monkeypatch.setattr(milvus_config, "_force_offline_if_cached", lambda *a, **kw: None)
    monkeypatch.setattr(milvus_config, "_ensure_hf_endpoint", lambda: "")

    yield milvus_config

    milvus_config.reset_embedding_runtime_cache()


def _bge_settings(device: str = "cuda") -> dict:
    return {
        "embedding_provider": "bge-m3",
        "bge_m3_model_path": "BAAI/bge-m3",
        "embedding_device": device,
    }


def test_build_runtime_caches_bge_m3(mc, mock_pymilvus_model):
    """同 settings 多次调用 → BGEM3EmbeddingFunction 只构造 1 次。"""
    settings = _bge_settings("cuda")

    r1 = mc.build_embedding_runtime(settings)
    r2 = mc.build_embedding_runtime(settings)
    r3 = mc.build_embedding_runtime(settings)

    # 缓存命中：返回同一 dict 对象（is 而非 ==）
    assert r1 is r2 is r3
    # 关键断言：底层 BGEM3EmbeddingFunction 只被实例化 1 次（不是 3 次）
    assert mock_pymilvus_model.hybrid.BGEM3EmbeddingFunction.call_count == 1


def test_build_runtime_different_device_different_cache(mc, mock_pymilvus_model):
    """device 变化 → 重新构造（不同 cache key）。"""
    r_cuda = mc.build_embedding_runtime(_bge_settings("cuda"))
    r_cpu = mc.build_embedding_runtime(_bge_settings("cpu"))

    assert r_cuda is not r_cpu
    assert mock_pymilvus_model.hybrid.BGEM3EmbeddingFunction.call_count == 2


def test_build_runtime_different_provider_different_cache(mc, mock_pymilvus_model):
    """provider 变化 → 不同 cache key。"""
    bge = mc.build_embedding_runtime(_bge_settings("cuda"))
    st_settings = {
        "embedding_provider": "sentence-transformer",
        "sentence_transformer_model": "all-MiniLM-L6-v2",
        "embedding_device": "cuda",
    }
    st = mc.build_embedding_runtime(st_settings)

    assert bge is not st
    assert mock_pymilvus_model.hybrid.BGEM3EmbeddingFunction.call_count == 1
    assert mock_pymilvus_model.dense.SentenceTransformerEmbeddingFunction.call_count == 1


def test_build_runtime_thread_safe(mc, mock_pymilvus_model):
    """N 个线程并发调同一 settings → 底层 EmbeddingFunction 只构造 1 次。"""
    settings = _bge_settings("cuda")
    results: list = []
    errors: list = []

    def worker():
        try:
            results.append(mc.build_embedding_runtime(settings))
        except Exception as e:  # pragma: no cover - 仅测试时记录
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"线程内异常: {errors}"
    assert len(results) == 16
    # 所有线程拿到的是同一个缓存实例
    first = results[0]
    assert all(r is first for r in results)
    # 底层只构造 1 次（双检锁有效）
    assert mock_pymilvus_model.hybrid.BGEM3EmbeddingFunction.call_count == 1


def test_reset_cache_forces_rebuild(mc, mock_pymilvus_model):
    """reset_embedding_runtime_cache 后下次调用重新构造。"""
    settings = _bge_settings("cuda")
    r1 = mc.build_embedding_runtime(settings)
    mc.reset_embedding_runtime_cache()
    r2 = mc.build_embedding_runtime(settings)

    # 缓存清空 → 重新构造（新实例）
    assert r1 is not r2
    assert mock_pymilvus_model.hybrid.BGEM3EmbeddingFunction.call_count == 2


# ===========================================================================
# _build_reranker 缓存测试
# ===========================================================================


@pytest.fixture
def cli_mod():
    """动态加载 bin/milvus-cli.py 模块。"""
    import importlib.util

    cli_path = _BIN_DIR / "milvus-cli.py"
    spec = importlib.util.spec_from_file_location("brain_base_milvus_cli_test", cli_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.reset_reranker_cache()  # 清空避免污染
    yield mod
    mod.reset_reranker_cache()


def test_reranker_caches_success(cli_mod, monkeypatch):
    """FlagReranker 构造成功后多次调用复用同一实例。"""
    fake_reranker_class = MagicMock(side_effect=lambda *a, **kw: MagicMock(name="reranker"))
    fake_flag_emb = MagicMock()
    fake_flag_emb.FlagReranker = fake_reranker_class
    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake_flag_emb)

    r1 = cli_mod._build_reranker("cpu")
    r2 = cli_mod._build_reranker("cpu")
    r3 = cli_mod._build_reranker("cpu")

    assert r1 is r2 is r3
    assert fake_reranker_class.call_count == 1


def test_reranker_caches_none_on_import_failure(cli_mod, monkeypatch):
    """导入 FlagEmbedding 失败 → 返回 None 并缓存 None（不再重试）。"""
    # 模拟 import 失败：把 FlagEmbedding 设为 None 触发 AttributeError
    counter = {"calls": 0}

    def faulty_import(*a, **kw):
        counter["calls"] += 1
        raise ImportError("no FlagEmbedding")

    fake_mod = MagicMock()
    type(fake_mod).FlagReranker = property(lambda _: faulty_import())
    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake_mod)

    r1 = cli_mod._build_reranker("cpu")
    r2 = cli_mod._build_reranker("cpu")
    r3 = cli_mod._build_reranker("cpu")

    assert r1 is None and r2 is None and r3 is None
    # 失败也只尝试 1 次（被缓存为 None）
    assert counter["calls"] == 1


def test_reranker_different_device_different_cache(cli_mod, monkeypatch):
    """device 不同 → 不同 cache key。"""
    fake_reranker_class = MagicMock(side_effect=lambda *a, **kw: MagicMock(name="reranker"))
    fake_flag_emb = MagicMock()
    fake_flag_emb.FlagReranker = fake_reranker_class
    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake_flag_emb)

    r_cpu = cli_mod._build_reranker("cpu")
    r_cuda = cli_mod._build_reranker("cuda")

    assert r_cpu is not r_cuda
    assert fake_reranker_class.call_count == 2
