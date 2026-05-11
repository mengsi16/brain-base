# -*- coding: utf-8 -*-
"""T32 验证：upload 路径全量重构。

**默认必跑**（按 CLAUDE.md 规则 14：LLM 测试不跳过）。跑前在 .env 配任一 key：
    MINIMAX_API_KEY  (首选，默认 provider)
    GLM_API_KEY      (可选)
    BB_LLM_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY

覆盖（8 个 case）：
1. DocEnrichment pydantic schema 长度约束（不调 LLM）
2. inject_doc_enrichment 工具函数（不调 LLM）
3. create_enrich_node(llm=None) 应 raise（不调 LLM）
4. create_doc_enrich_node(llm=None) 应 raise（不调 LLM）
5. IngestFileGraph(llm=None) 应 raise（不调 LLM）
6. ingest_node(state with enriched=False) 应 raise（不调 LLM）
7. doc_enrich_node Minimax 真调（验 DocEnrichment 2 字段 + frontmatter 写回）
8. chunk enrich_node Minimax 真调（验 ChunkEnrichment 4 字段 + chunk frontmatter 写回）
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

# 加载 .env（按规则 12：测试脚本用 load_dotenv 而非 $env:）
try:
    from dotenv import load_dotenv

    _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    load_dotenv(_PROJECT_ROOT / ".env")
except Exception:
    pass

from brain_base.agents.schemas import ChunkEnrichment, DocEnrichment
from brain_base.nodes._frontmatter import (
    inject_doc_enrichment,
    parse_frontmatter,
    split_frontmatter,
)


# ---------------------------------------------------------------------------
# LLM 凭证解析（与 test_t31_query_rewrite_llm.py 同款，规则 14）
# ---------------------------------------------------------------------------


def _resolve_llm_credentials() -> dict | None:
    """从 env 找 LLM 凭证；MINIMAX 优先，GLM 次之，BB_LLM_* 兜底。"""
    minimax_key = (os.environ.get("MINIMAX_API_KEY") or "").strip()
    if minimax_key:
        # Minimax 走 Anthropic-compatible API（factory 不识别 "minimax" provider）
        return {
            "provider": "anthropic",
            "model": (os.environ.get("MINIMAX_MODEL") or "MiniMax-M2"),
            "base_url": (os.environ.get("MINIMAX_BASE_URL") or "").strip() or None,
            "api_key": minimax_key,
        }
    glm_key = (os.environ.get("GLM_API_KEY") or "").strip()
    if glm_key:
        return {
            "provider": "glm",
            "model": (os.environ.get("GLM_MODEL") or "glm-4.6"),
            "base_url": (os.environ.get("GLM_BASE_URL") or "").strip() or None,
            "api_key": glm_key,
        }
    api_key = (
        os.environ.get("BB_LLM_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()
    if not api_key:
        return None
    return {
        "provider": (os.environ.get("BB_LLM_PROVIDER") or "anthropic").lower(),
        "model": (
            os.environ.get("BB_DEEP_THINK_LLM")
            or "claude-sonnet-4-20250514"
        ),
        "base_url": (os.environ.get("BB_LLM_BASE_URL") or "").strip() or None,
        "api_key": api_key,
    }


def _build_llm():
    creds = _resolve_llm_credentials()
    if creds is None:
        return None
    from brain_base.llm_clients.factory import create_llm_client

    client = create_llm_client(
        provider=creds["provider"],
        model=creds["model"],
        base_url=creds["base_url"],
        api_key=creds["api_key"],
        temperature=0.2,
        max_tokens_to_sample=1024,
        timeout=60,
        max_retries=2,
    )
    return client.get_llm()


@pytest.fixture(scope="module")
def llm_real():
    """module-scope 真调 LLM。缺 key fail 不 skip（规则 14）。"""
    llm = _build_llm()
    if llm is None:
        pytest.fail(
            "未配置 LLM API key：请在 .env 加 MINIMAX_API_KEY（首选）/ GLM_API_KEY / "
            "BB_LLM_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY 中任一。"
            "LLM 测试是核心必跑（CLAUDE.md 规则 14）不允许跳过。"
        )
    return llm


# ---------------------------------------------------------------------------
# Case 1: DocEnrichment pydantic schema 长度约束
# ---------------------------------------------------------------------------


def test_doc_enrichment_pydantic_schema():
    """summary 20-400 / keywords 1-30 约束应被强制。

    schema 数量边界已放宽（keywords min=1 max=30）：prompt 仍引导 5-15，但 LLM 实际数量不严格遵守
    （SkillRouter chunk-022 实测 LLM 输出 11 个 keyword 超原 max=10 导致整 chunk ValidationError 入库失败），
    因此 schema 上限提到 30 防爆 + 下限放到 1 防完全 lazy，prompt 引导 5-15 作为软约束目标。
    """
    # 合法：5 个（prompt 推荐范围内）
    valid = DocEnrichment(
        summary="本文提出一种新的检索方法，结合稀疏与稠密向量召回。",
        keywords=["检索", "稀疏向量", "稠密向量", "召回", "融合"],
    )
    assert valid.summary.startswith("本文")
    assert len(valid.keywords) == 5

    # 合法：1 个（schema 下限）
    DocEnrichment(
        summary="本文提出一种新的检索方法，结合稀疏与稠密向量召回。",
        keywords=["检索"],
    )

    # 合法：20 个（在 schema 上限 30 内，超过 prompt 引导 15 但接受）
    DocEnrichment(
        summary="本文提出一种新的检索方法，结合稀疏与稠密向量召回。",
        keywords=[f"k{i}" for i in range(20)],
    )

    # summary 过短（< 20）
    with pytest.raises(ValidationError):
        DocEnrichment(summary="太短", keywords=["a"])

    # summary 过长（> 400）
    with pytest.raises(ValidationError):
        DocEnrichment(summary="x" * 401, keywords=["a"])

    # keywords 完全空（< 1）
    with pytest.raises(ValidationError):
        DocEnrichment(
            summary="本文提出一种新的检索方法，结合稀疏与稠密向量召回。",
            keywords=[],
        )

    # keywords 超防爆上限（> 30）
    with pytest.raises(ValidationError):
        DocEnrichment(
            summary="本文提出一种新的检索方法，结合稀疏与稠密向量召回。",
            keywords=[f"k{i}" for i in range(31)],
        )


# ---------------------------------------------------------------------------
# Case 2: inject_doc_enrichment 工具函数
# ---------------------------------------------------------------------------


def test_inject_doc_enrichment_replaces_existing_fields():
    """已存在 summary / keywords 应被替换；其他字段保留。"""
    fm = (
        "---\n"
        "doc_id: foo-2026-05-10\n"
        "title: 旧标题\n"
        "summary: \n"
        "keywords: []\n"
        "url:\n"
        "---"
    )
    new_fm = inject_doc_enrichment(
        fm,
        summary="新摘要：本文做了一件事。",
        keywords=["k1", "k2", "k3"],
    )
    meta = parse_frontmatter(new_fm + "\n\nbody")
    assert meta["summary"] == "\"新摘要：本文做了一件事。\""
    assert meta["keywords"] == ["k1", "k2", "k3"]
    # 其他字段保留
    assert meta["doc_id"] == "foo-2026-05-10"
    assert meta["title"] == "旧标题"


def test_inject_doc_enrichment_appends_when_missing():
    """字段不存在时追加到末尾 ---  之前。"""
    fm = (
        "---\n"
        "doc_id: bar-2026-05-10\n"
        "title: 文档\n"
        "---"
    )
    new_fm = inject_doc_enrichment(
        fm,
        summary="追加的摘要 20 字凑足够长度限制约束",
        keywords=["k1", "k2"],
    )
    # summary 与 keywords 应都出现
    assert "summary:" in new_fm
    assert "keywords:" in new_fm
    # 末尾应仍是 ---
    assert new_fm.rstrip().endswith("---")
    # title 不被改写
    assert "title: 文档" in new_fm


# ---------------------------------------------------------------------------
# Case 3-6: 各 fail-fast guard
# ---------------------------------------------------------------------------


def test_create_enrich_node_fails_when_llm_none():
    """T32 F2/F3：chunk enrich llm=None 应 raise（CLAUDE.md 规则 14）。"""
    from brain_base.nodes.persistence import create_enrich_node

    with pytest.raises(RuntimeError, match="llm 必须提供"):
        create_enrich_node(llm=None)


def test_create_doc_enrich_node_fails_when_llm_none():
    """T32 C8：doc_enrich_node llm=None 应 raise（CLAUDE.md 规则 14）。"""
    from brain_base.nodes.ingest_file import create_doc_enrich_node

    with pytest.raises(RuntimeError, match="llm 必须提供"):
        create_doc_enrich_node(llm=None)


def test_ingest_file_graph_fails_when_llm_none():
    """T32 F1：IngestFileGraph(llm=None) 应 raise（CLAUDE.md 规则 14）。"""
    from brain_base.graphs.ingest_file_graph import IngestFileGraph

    with pytest.raises(RuntimeError, match="llm 必须提供"):
        IngestFileGraph(llm=None)


def test_ingest_node_fails_when_not_enriched():
    """T32 F5：ingest_node state.enriched=False 时应 raise，避免残缺 chunk 入 milvus。"""
    from brain_base.nodes.persistence import ingest_node

    state = {
        "chunk_files": ["fake.md"],  # 必须非空才走到 enriched 检查
        "enriched": False,
    }
    with pytest.raises(RuntimeError, match="enrich 阶段未完成或失败"):
        ingest_node(state)


# ---------------------------------------------------------------------------
# Case 7: doc_enrich_node Minimax 真调
# ---------------------------------------------------------------------------


@pytest.mark.requires_llm
def test_doc_enrich_node_minimax_real_call(llm_real, tmp_path):
    """真调 LLM 验 DocEnrichment 2 字段输出 + frontmatter 写回。"""
    from brain_base.nodes.ingest_file import create_doc_enrich_node

    raw_path = tmp_path / "ragflow-test.md"
    raw_path.write_text(
        "---\n"
        "doc_id: ragflow-test-2026-05-10\n"
        "title: RAGFlow 快速入门\n"
        "source: user-upload\n"
        "source_type: user-upload\n"
        "url:\n"
        "fetched_at: 2026-05-10\n"
        "content_sha256: deadbeef\n"
        "keywords: []\n"
        "---\n\n"
        "# RAGFlow 快速入门\n\n"
        "RAGFlow 是一个开源的 RAG 引擎，支持多种文档格式与混合检索。\n\n"
        "## 安装\n\n"
        "通过 docker compose 启动：`docker compose up -d`。默认占用 80 / 9380 / 5455 端口。\n\n"
        "## 使用\n\n"
        "首次登录用 admin@ragflow.io 创建账号。支持 PDF / Markdown / DOCX 上传。\n",
        encoding="utf-8",
    )

    node = create_doc_enrich_node(llm_real)
    out = node({"raw_paths": [str(raw_path)]})

    assert out["doc_enriched"] is True, f"doc_enriched 应为 True：{out}"
    assert out["doc_enriched_count"] == 1, f"应富化 1 个：{out}"
    assert out["doc_enrich_errors"] == [], f"应无错误：{out['doc_enrich_errors']}"
    assert out["raw_paths"] == [str(raw_path)], "成功的 raw_path 应保留"

    # 验文件 frontmatter 真的被写入了 summary / keywords
    final_text = raw_path.read_text(encoding="utf-8")
    fm, body = split_frontmatter(final_text)
    assert fm, "frontmatter 应仍存在"
    meta = parse_frontmatter(final_text)

    # summary：长度 20-400（DocEnrichment 约束）+ 内容相关 RAGFlow
    summary_raw = meta["summary"]
    # parse_frontmatter 拿到的字符串带引号（json.dumps 输出），去掉一层引号再判
    summary_text = summary_raw.strip('"').strip("'")
    assert 20 <= len(summary_text) <= 400, f"summary 长度违约：{len(summary_text)} / {summary_text!r}"
    assert "RAGFlow" in summary_text or "ragflow" in summary_text.lower(), (
        f"summary 应含主题词 RAGFlow：{summary_text!r}"
    )

    # keywords：5-15 个，应含 RAGFlow 或相关词
    keywords = meta["keywords"]
    assert isinstance(keywords, list), f"keywords 应为 list：{type(keywords)}"
    assert 5 <= len(keywords) <= 15, f"keywords 数量违约：{len(keywords)} / {keywords!r}"
    keywords_lower = " ".join(k.lower() for k in keywords)
    assert "ragflow" in keywords_lower or "rag" in keywords_lower, (
        f"keywords 应含 RAGFlow 相关：{keywords!r}"
    )

    # title 不应被 doc_enrich 改写（仍是 frontmatter_node H1 提取版）
    assert meta["title"] == "RAGFlow 快速入门", f"title 不应被覆写：{meta['title']!r}"


# ---------------------------------------------------------------------------
# Case 8: chunk enrich_node Minimax 真调
# ---------------------------------------------------------------------------


@pytest.mark.requires_llm
def test_chunk_enrich_node_minimax_real_call(llm_real, tmp_path):
    """真调 LLM 验 ChunkEnrichment 4 字段输出 + chunk frontmatter 写回（T32 F9 验证 prompt 加 JSON 示例后仍正常）。"""
    from brain_base.nodes.persistence import create_enrich_node

    chunk_path = tmp_path / "test-chunk-001.md"
    chunk_path.write_text(
        "---\n"
        "doc_id: ragflow-test-2026-05-10\n"
        "chunk_id: test-chunk-001\n"
        "title: \"\"\n"
        "summary: \"\"\n"
        "keywords: []\n"
        "questions: []\n"
        "---\n\n"
        "# 启动 RAGFlow\n\n"
        "通过 docker compose 启动 RAGFlow 服务：\n\n"
        "```bash\ndocker compose -f docker/docker-compose.yml up -d\n```\n\n"
        "服务启动后默认占用 80 端口（HTTP）、9380 端口（API）、5455 端口（gRPC）。\n\n"
        "首次启动需等待 2-3 分钟下载镜像与初始化数据库。完成后访问 http://localhost 注册账号即可使用。\n",
        encoding="utf-8",
    )

    node = create_enrich_node(llm_real)
    out = node({"chunk_files": [str(chunk_path)]})

    assert out["enriched"] is True, f"enriched 应为 True：{out}"
    assert out["enriched_count"] == 1, f"应富化 1 个 chunk：{out}"

    # 验 chunk frontmatter 4 字段全写入
    final_text = chunk_path.read_text(encoding="utf-8")
    meta = parse_frontmatter(final_text)

    # title：1-80 字
    title_text = meta["title"].strip('"').strip("'")
    assert 1 <= len(title_text) <= 80, f"title 长度违约：{len(title_text)} / {title_text!r}"

    # summary：10-200 字 + 含主题
    summary_text = meta["summary"].strip('"').strip("'")
    assert 10 <= len(summary_text) <= 200, f"summary 长度违约：{len(summary_text)}"

    # keywords：5-10 个
    keywords = meta["keywords"]
    assert isinstance(keywords, list) and 5 <= len(keywords) <= 10, (
        f"keywords 违约：{keywords!r}"
    )

    # questions：3-8 条
    questions = meta["questions"]
    assert isinstance(questions, list) and 3 <= len(questions) <= 8, (
        f"questions 违约：{questions!r}"
    )

    # 主题词 RAGFlow / docker 应至少出现在 summary 或 keywords 之一
    text_blob = (summary_text + " " + " ".join(keywords)).lower()
    assert "ragflow" in text_blob or "docker" in text_blob, (
        f"主题词 RAGFlow / docker 应出现在 summary 或 keywords：summary={summary_text!r} keywords={keywords!r}"
    )

    # 没有 enrich_error 字段
    assert "enrich_error" not in meta, f"成功 chunk 不应有 enrich_error：{meta}"
