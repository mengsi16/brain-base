"""
brain-base CLI：基于 LangGraph 图的命令行入口。

替代旧的 bin/brain-base-cli.py（claude-code agent 调度），
直接调用 brain_base 包中的 LangGraph 图。
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 自动加载项目根目录 .env（dotenv 缺失时静默跳过，env 仍可外部注入）。
# 必须在 import 业务模块之前执行，否则 KB_*/BB_* 环境变量来不及生效。
try:
    from dotenv import load_dotenv

    _PROJECT_ROOT = Path(__file__).resolve().parent.parent
    load_dotenv(_PROJECT_ROOT / ".env")
except Exception:
    pass

# 配置 logging：所有节点用 logging.getLogger(__name__) 拿 logger，
# 不在这里 basicConfig 的话所有 INFO/WARNING 都被 root logger 默认 WARNING+stderr-handler 截掉，
# 排障无可观测性。环境变量 BB_LOG_LEVEL 可调节（默认 INFO）。
_log_level_name = (os.environ.get("BB_LOG_LEVEL") or "INFO").upper()
_log_level = getattr(logging, _log_level_name, logging.INFO)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)


def _build_llm_from_env():
    """从环境变量构造 LangChain LLM；缺 key 时返回 None。

    读取顺序：
        BB_LLM_PROVIDER / BB_DEEP_THINK_LLM / BB_LLM_BASE_URL / BB_LLM_API_KEY
    BB_LLM_API_KEY 留空时尝试 ANTHROPIC_API_KEY / OPENAI_API_KEY 兜底。

    T27：返回 None 不再意味着「节点走降级路径」——调用方（cmd_ask）需自行
    判定 None 后 fail-fast 退出，不能再调 ``QaGraph(llm=None)``。
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
    """调用 QaGraph 完整问答（T27 fail-fast：无 LLM 时退出 1）"""
    # CLI flag → Python 进程内 os.environ（不污染外部 PowerShell session，规则 12）
    # web_fetcher.py 通过 BB_PLAYWRIGHT_HEADLESS / BB_DEBUG_PAUSE_GOOGLE 自动识别
    # 默认有头（Google 无头检测严）；--headless 显式强制无头（服务器 / CI）
    if getattr(args, "headless", False):
        os.environ["BB_PLAYWRIGHT_HEADLESS"] = "1"
    if getattr(args, "debug_pause_google", False):
        os.environ["BB_DEBUG_PAUSE_GOOGLE"] = "1"

    from brain_base.graphs.qa_graph import QaGraph
    llm = _build_llm_from_env()
    if llm is None:
        print(
            "[error] 未配置 LLM（缺 BB_LLM_API_KEY / ANTHROPIC_API_KEY）。\n"
            "        QA 主图所有 LLM 节点都是 fail-fast，无法降级运行。\n"
            "        请在 .env 里填入 BB_LLM_API_KEY 或 ANTHROPIC_API_KEY 后重试。",
            file=sys.stderr,
        )
        return 1
    # T36：--session 多轮上下文持久化
    history: list[dict] = []
    session_path: Path | None = None
    if getattr(args, "session", None):
        session_path = Path("data/sessions") / f"{args.session}.jsonl"
        if session_path.exists():
            history = [
                json.loads(line)
                for line in session_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

    qa = QaGraph(llm=llm)
    result = qa.run(question=args.prompt, conversation_history=history or None)

    # T36：追加当前问答到 session 文件
    if session_path:
        session_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        with session_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"role": "user", "text": args.prompt, "ts": now},
                               ensure_ascii=False) + "\n")
            f.write(json.dumps({"role": "ai", "text": result.get("answer", ""),
                               "ts": now}, ensure_ascii=False) + "\n")

    # 可选：dump 完整 state 到 JSON 文件（e2e 测试评判用，避免解析 log 重建 state）
    state_dump_path = getattr(args, "state_dump", None)
    if state_dump_path:
        # 过滤不可序列化字段（如 LLM 实例 / GetInfoConfig dataclass / infra_status 内的 module 句柄）
        # 用 default=str 兜底，把不认识的对象 repr 成字符串而不是抛错
        Path(state_dump_path).parent.mkdir(parents=True, exist_ok=True)
        with open(state_dump_path, "w", encoding="utf-8") as f:
            json.dump(
                {k: v for k, v in result.items() if k not in {"llm", "get_info_config"}},
                f,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        print(f"[state-dump] 写入 {state_dump_path}", file=sys.stderr)
    # 输出答案
    answer = result.get("answer", "")
    if answer:
        print(answer)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    """交互式多轮对话（T36）。

    while-loop 内存维护 conversation_history，Ctrl+C / /q 退出。
    会话不持久化到磁盘——持久化场景用 ``ask --session``。
    """
    from brain_base.graphs.qa_graph import QaGraph

    llm = _build_llm_from_env()
    if llm is None:
        print(
            "[error] 未配置 LLM（缺 BB_LLM_API_KEY / ANTHROPIC_API_KEY）。\n"
            "        请在 .env 里填入 LLM API key 后重试。",
            file=sys.stderr,
        )
        return 1

    qa = QaGraph(llm=llm)
    history: list[dict] = []
    print("brain-base chat（输入 /q 退出）", file=sys.stderr)

    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n会话结束。", file=sys.stderr)
            break
        if question in ("/q", "/quit", "exit", "quit"):
            print("会话结束。", file=sys.stderr)
            break
        if not question:
            continue

        result = qa.run(question=question, conversation_history=history)
        answer = result.get("answer", "")
        print(f"\n{answer}\n")

        now = datetime.now(timezone.utc).isoformat()
        history.append({"role": "user", "text": question, "ts": now})
        history.append({"role": "ai", "text": answer, "ts": now})

    return 0


def cmd_ingest_file(args: argparse.Namespace) -> int:
    """调用 IngestFileGraph 导入本地文件（T32：注入 LLM + fail-fast + 错误退出码）。

    T32 F1+F10 修复：原 cli 实例化 ``IngestFileGraph()`` 不传 llm → enrich 永远走降级 →
    chunks 入 milvus 时 summary/keywords/questions 全空。现按 cmd_ask 同款 fail-fast 加载 LLM。

    备注：``cmd_ingest_url`` 当前仍有同问题（``IngestUrlGraph()`` 不传 llm），留 T 后续单独修复。
    """
    from brain_base.graphs.ingest_file_graph import IngestFileGraph

    llm = _build_llm_from_env()
    if llm is None:
        print(
            "[error] 未配置 LLM（缺 BB_LLM_API_KEY / ANTHROPIC_API_KEY / MINIMAX_API_KEY）。\n"
            "        upload 路径属核心 Agent 节点，无法降级运行（CLAUDE.md 规则 14）。\n"
            "        请在 .env 里填入 LLM API key 后重试。",
            file=sys.stderr,
        )
        return 1

    graph = IngestFileGraph(llm=llm)
    result = graph.run(input_files=args.path)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    # T32 F5+D4：按错误清单决定 exit code
    # - conversion_errors / doc_enrich_errors / persistence_results 任一含 error → 退出 1
    has_errors = (
        bool(result.get("conversion_errors"))
        or bool(result.get("doc_enrich_errors"))
        or any(r.get("error") for r in result.get("persistence_results", []))
    )
    if has_errors:
        print(
            "[warn] ingest-file 部分文件失败，详情见上方 JSON。建议人工核对失败文件后重试。",
            file=sys.stderr,
        )
        return 1
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
    p_ask.add_argument(
        "--state-dump",
        default=None,
        help="把 QaGraph.run() 返回的完整 state dict 写入 JSON 文件（e2e 测试评判用）",
    )
    p_ask.add_argument(
        "--headless",
        action="store_true",
        help="playwright 强制无头模式（覆盖 .env BB_PLAYWRIGHT_HEADLESS）；默认有头，仅服务器 / CI 使用",
    )
    p_ask.add_argument(
        "--debug-pause-google",
        action="store_true",
        help="第一次 search_google 完成后不关 page + 等回车，让你切到浏览器看 google 实际显示（覆盖 .env BB_DEBUG_PAUSE_GOOGLE）",
    )
    p_ask.add_argument(
        "--session",
        type=str,
        default=None,
        help="会话 ID，启用多轮上下文。历史存于 data/sessions/<id>.jsonl（T36）",
    )
    p_ask.set_defaults(func=cmd_ask)

    # chat（T36 新增）
    p_chat = sub.add_parser("chat", help="交互式多轮对话（会话在内存，不持久化）")
    p_chat.set_defaults(func=cmd_chat)

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
