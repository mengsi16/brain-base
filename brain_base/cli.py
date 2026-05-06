"""
brain-base CLI：基于 LangGraph 图的命令行入口。

替代旧的 bin/brain-base-cli.py（claude-code agent 调度），
直接调用 brain_base 包中的 LangGraph 图。
"""

import argparse
import json
import os
import sys
from pathlib import Path

# 自动加载项目根目录 .env（dotenv 缺失时静默跳过，env 仍可外部注入）。
# 必须在 import 业务模块之前执行，否则 KB_*/BB_* 环境变量来不及生效。
try:
    from dotenv import load_dotenv

    _PROJECT_ROOT = Path(__file__).resolve().parent.parent
    load_dotenv(_PROJECT_ROOT / ".env")
except Exception:
    pass


def _build_llm_from_env():
    """从环境变量构造 LangChain LLM；缺 key 时返回 None（让节点走降级路径）。

    读取顺序：
        BB_LLM_PROVIDER / BB_DEEP_THINK_LLM / BB_LLM_BASE_URL / BB_LLM_API_KEY
    BB_LLM_API_KEY 留空时尝试 ANTHROPIC_API_KEY / OPENAI_API_KEY 兜底。
    """
    api_key = (os.environ.get("BB_LLM_API_KEY") or "").strip()
    if not api_key:
        # 兜底：按 provider 找 SDK 标准 env
        provider = (os.environ.get("BB_LLM_PROVIDER") or "anthropic").lower()
        if provider == "anthropic":
            api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        elif provider in ("openai", "xai", "deepseek", "qwen", "glm", "openrouter"):
            api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return None

    provider = (os.environ.get("BB_LLM_PROVIDER") or "anthropic").lower()
    model = (
        os.environ.get("BB_DEEP_THINK_LLM")
        or os.environ.get("BB_QUICK_THINK_LLM")
        or "claude-sonnet-4-20250514"
    )
    base_url = (os.environ.get("BB_LLM_BASE_URL") or "").strip() or None

    from brain_base.llm_clients.factory import create_llm_client

    client = create_llm_client(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=0.2,
        max_tokens_to_sample=2048,
        timeout=60,
        max_retries=2,
    )
    return client.get_llm()


def cmd_health(args: argparse.Namespace) -> int:
    """检查基础设施可用性"""
    from brain_base.nodes.qa import probe_node
    result = probe_node({"question": ""})
    print(json.dumps(result.get("infra_status", {}), ensure_ascii=False, indent=2))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """执行多查询检索"""
    from brain_base.nodes.qa import _get_milvus_cli
    milvus_cli = _get_milvus_cli()
    result = milvus_cli.multi_query_search(
        queries=args.query,
        top_k_per_query=args.top_k_per_query,
        final_k=args.final_k,
        rrf_k=args.rrf_k,
        use_rerank=not args.no_rerank,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """调用 QaGraph 完整问答（自动从 .env 构造 LLM；无 key 时降级到规则路径）"""
    from brain_base.graphs.qa_graph import QaGraph
    llm = _build_llm_from_env()
    if llm is None:
        print("[warn] 未配置 LLM（缺 BB_LLM_API_KEY / ANTHROPIC_API_KEY），所有 LLM 节点降级到规则路径", file=sys.stderr)
    qa = QaGraph(llm=llm)
    result = qa.run(question=args.prompt)
    # 输出答案
    answer = result.get("answer", "")
    if answer:
        print(answer)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_ingest_file(args: argparse.Namespace) -> int:
    """调用 IngestFileGraph 导入本地文件"""
    from brain_base.graphs.ingest_file_graph import IngestFileGraph
    graph = IngestFileGraph()
    result = graph.run(input_files=args.path)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_ingest_url(args: argparse.Namespace) -> int:
    """调用 IngestUrlGraph 导入 URL"""
    from brain_base.graphs.ingest_url_graph import IngestUrlGraph
    graph = IngestUrlGraph()
    result = graph.run(
        url=args.url,
        source_type=args.source_type,
        topic=args.topic,
        title_hint=args.title_hint or "",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_remove_doc(args: argparse.Namespace) -> int:
    """调用 LifecycleGraph 删除文档"""
    from brain_base.graphs.lifecycle_graph import LifecycleGraph
    graph = LifecycleGraph()
    result = graph.run(
        doc_ids=args.doc_id,
        urls=args.url,
        sha256=args.sha256 or "",
        confirm=args.confirm,
        force_recent=args.force_recent,
        reason=args.reason or "",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    """调用 LintGraph 清理固化层"""
    from brain_base.graphs.lint_graph import LintGraph
    graph = LintGraph()
    result = graph.run()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_crystallize_check(args: argparse.Namespace) -> int:
    """调用 CrystallizeGraph 命中判断"""
    from brain_base.graphs.crystallize_graph import CrystallizeGraph
    cg = CrystallizeGraph()
    result = cg.hit_check(user_question=args.question)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brain-base", description="brain-base LangGraph CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # health
    p_health = sub.add_parser("health", help="检查基础设施可用性")
    p_health.set_defaults(func=cmd_health)

    # search
    p_search = sub.add_parser("search", help="执行多查询检索")
    p_search.add_argument("--query", action="append", required=True, help="查询字符串，可多次指定")
    p_search.add_argument("--top-k-per-query", type=int, default=20)
    p_search.add_argument("--final-k", type=int, default=10)
    p_search.add_argument("--rrf-k", type=int, default=60)
    p_search.add_argument("--no-rerank", action="store_true")
    p_search.set_defaults(func=cmd_search)

    # ask
    p_ask = sub.add_parser("ask", help="调用 QA 图完整问答")
    p_ask.add_argument("prompt", help="用户问题")
    p_ask.set_defaults(func=cmd_ask)

    # ingest-file
    p_ingest_file = sub.add_parser("ingest-file", help="导入本地文件")
    p_ingest_file.add_argument("--path", action="append", required=True, help="文件路径，可多次指定")
    p_ingest_file.set_defaults(func=cmd_ingest_file)

    # ingest-url
    p_ingest_url = sub.add_parser("ingest-url", help="导入 URL")
    p_ingest_url.add_argument("--url", required=True, help="要入库的 URL")
    p_ingest_url.add_argument("--source-type", default="community", choices=["official-doc", "community"])
    p_ingest_url.add_argument("--topic", default="untitled", help="主题关键词")
    p_ingest_url.add_argument("--title-hint", default="", help="标题提示")
    p_ingest_url.set_defaults(func=cmd_ingest_url)

    # remove-doc
    p_remove = sub.add_parser("remove-doc", help="删除文档（默认 dry-run）")
    p_remove.add_argument("--doc-id", action="append", help="doc_id，可多次指定")
    p_remove.add_argument("--url", action="append", help="按 URL 查找")
    p_remove.add_argument("--sha256", default="", help="按 SHA-256 查找")
    p_remove.add_argument("--confirm", action="store_true", help="必须显式加上才真删")
    p_remove.add_argument("--force-recent", action="store_true")
    p_remove.add_argument("--reason", default="")
    p_remove.set_defaults(func=cmd_remove_doc)

    # lint
    p_lint = sub.add_parser("lint", help="清理固化层")
    p_lint.set_defaults(func=cmd_lint)

    # crystallize-check
    p_cc = sub.add_parser("crystallize-check", help="固化层命中判断")
    p_cc.add_argument("--question", required=True, help="用户问题")
    p_cc.set_defaults(func=cmd_crystallize_check)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
