# -*- coding: utf-8 -*-
"""T37 端到端测试：多轮对话指代消解（真调 LLM，默认 Minimax）。

4 组对话场景：
1. 代词指代："它"指代上文实体
2. 省略主语：追问上文主题的属性
3. 指示代词："那个东西" 指代上文讨论对象
4. 对照组：主题切换不消解
"""

import json
import sys
from pathlib import Path

# 确保项目根在 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from brain_base.nodes.qa import create_normalize_node
from brain_base.cli import _build_llm_from_env


def run_normalize_with_history(llm, question: str, history: list[dict]) -> dict:
    """调用 normalize_node 并返回 state 输出。"""
    node_fn = create_normalize_node(llm)
    state = {
        "question": question,
        "conversation_history": history,
    }
    return node_fn(state)


def main():
    print("=" * 70)
    print("T37 端到端测试：多轮对话指代消解")
    print("=" * 70)

    llm = _build_llm_from_env()
    print(f"\nLLM 构建成功: {type(llm).__name__}\n")

    # ---- 测试用例 ----
    cases = [
        {
            "name": "Case 1: 代词「它」指代上文实体 RAGFlow",
            "history": [
                {"role": "user", "text": "RAGFlow 是什么？", "ts": "2026-05-12T00:00:00Z"},
                {"role": "ai", "text": "RAGFlow 是一个开源的检索增强生成框架，支持深度文档理解和多路召回。", "ts": "2026-05-12T00:00:01Z"},
            ],
            "question": "它支持哪些文件格式？",
            "expect_resolved": "RAGFlow",  # 消解后应包含 RAGFlow
        },
        {
            "name": "Case 2: 省略主语，追问上文 LangGraph 的属性",
            "history": [
                {"role": "user", "text": "LangGraph 和 LangChain 有什么区别？", "ts": "2026-05-12T00:01:00Z"},
                {"role": "ai", "text": "LangGraph 是 LangChain 生态中的图编排框架，强调有向图状态机模式，而 LangChain 是更通用的 LLM 应用框架。", "ts": "2026-05-12T00:01:01Z"},
            ],
            "question": "怎么安装？",
            "expect_resolved": "LangGraph",  # 消解后应包含 LangGraph
        },
        {
            "name": "Case 3: 指示代词「那个框架」指代 Milvus",
            "history": [
                {"role": "user", "text": "Milvus 的向量索引性能如何？", "ts": "2026-05-12T00:02:00Z"},
                {"role": "ai", "text": "Milvus 2.x 支持 HNSW、IVF_FLAT 等索引，百万级向量检索延迟可控制在 10ms 以内。", "ts": "2026-05-12T00:02:01Z"},
            ],
            "question": "那个框架支持 GPU 加速吗？",
            "expect_resolved": "Milvus",  # 消解后应包含 Milvus
        },
        {
            "name": "Case 4: 主题切换 → 不应消解（对照组）",
            "history": [
                {"role": "user", "text": "RAGFlow 怎么部署？", "ts": "2026-05-12T00:03:00Z"},
                {"role": "ai", "text": "RAGFlow 支持 Docker 和源码部署两种方式...", "ts": "2026-05-12T00:03:01Z"},
            ],
            "question": "FastAPI 的异步性能怎么样？",
            "expect_resolved": None,  # 主题切换，contextualized_query 应为 null
        },
    ]

    # ---- 执行 ----
    results = []
    for i, case in enumerate(cases, 1):
        print(f"\n{'─' * 60}")
        print(f"  {case['name']}")
        print(f"{'─' * 60}")
        print(f"  历史: {case['history'][-1]['role']}: {case['history'][-1]['text'][:50]}...")
        print(f"  当前问题: {case['question']}")

        try:
            output = run_normalize_with_history(llm, case["question"], case["history"])
            ctx_q = output.get("contextualized_query")
            norm_q = output.get("normalized_query", "")
            time_sens = output.get("time_sensitive", False)

            print(f"\n  [输出]")
            print(f"    normalized_query:    {norm_q}")
            print(f"    contextualized_query: {ctx_q}")
            print(f"    time_sensitive:      {time_sens}")

            # 验证
            if case["expect_resolved"] is None:
                # 对照组：应该 null 或不含上文实体
                passed = ctx_q is None
                verdict = "✅ PASS（主题切换，未消解）" if passed else f"❌ FAIL（预期 null，实际 {ctx_q!r}）"
            else:
                # 应该消解出上文实体
                passed = (
                    ctx_q is not None
                    and case["expect_resolved"].lower() in ctx_q.lower()
                ) or (case["expect_resolved"].lower() in norm_q.lower())
                verdict = (
                    f"✅ PASS（消解出 {case['expect_resolved']}）" if passed
                    else f"❌ FAIL（预期含 {case['expect_resolved']!r}，ctx={ctx_q!r}，norm={norm_q!r}）"
                )

            print(f"    判定: {verdict}")
            results.append({"case": case["name"], "passed": passed, "output": output})

        except Exception as e:
            print(f"    ❌ ERROR: {e}")
            results.append({"case": case["name"], "passed": False, "error": str(e)})

    # ---- 汇总 ----
    print(f"\n\n{'=' * 70}")
    passed_count = sum(1 for r in results if r.get("passed"))
    total = len(results)
    print(f"  结果汇总: {passed_count}/{total} PASSED")
    print(f"{'=' * 70}")

    return 0 if passed_count == total else 1


if __name__ == "__main__":
    sys.exit(main())
