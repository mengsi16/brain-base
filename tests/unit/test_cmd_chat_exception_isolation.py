# -*- coding: utf-8 -*-
"""T42 单测：cmd_chat 单轮异常隔离行为。

验证 `cmd_chat` while-loop 在 `qa.run` 抛错时：
1. 不让进程退出（继续等下一轮输入）
2. 打印 `[本轮失败]` 到 stderr
3. 不污染 conversation_history（错误轮不写入）

完全用 monkeypatch 模拟 input + QaGraph，不真调 LLM / Milvus，纯快速单测。
"""
from __future__ import annotations

import argparse
from io import StringIO
from typing import Any

import pytest


class _FakeQaGraph:
    """模拟 QaGraph：第一轮抛 ValueError，第二轮正常返回。

    用来验证 cmd_chat 抓错后能继续下一轮。
    """

    def __init__(self, llm: Any) -> None:
        self.calls = 0
        self.history_at_each_call: list[list[dict]] = []

    def run(self, *, question: str, conversation_history: list[dict] | None) -> dict:
        self.calls += 1
        # 记录每次调用时收到的 history 快照（深复制避免引用篡改）
        self.history_at_each_call.append(
            list(conversation_history) if conversation_history else []
        )
        if self.calls == 1:
            # 第 1 轮抛错，模拟 LLM schema 失配 / 网络抖动等
            raise ValueError("mocked LLM schema failure")
        # 第 2 轮正常返回
        return {"answer": f"answer-for-{question}"}


def test_cmd_chat_isolates_single_round_failure(monkeypatch, capsys):
    """T42 验收 #5：单轮 LLM 异常不应崩 chat 进程，应继续下一轮。

    场景：用户输入 "boom" → qa.run 抛 ValueError → chat 不退出，打印 [本轮失败]
    继续输入 "hello" → qa.run 正常返回 → 输出 answer 并写入 history
    最后输入 "/q" → 正常退出
    """
    from brain_base import cli

    # 模拟 _build_llm_from_env 返回非 None 让 cli 走到 while-loop（不真调 LLM）
    monkeypatch.setattr(cli, "_build_llm_from_env", lambda: "fake-llm-sentinel")

    # 模拟 QaGraph：用同一个 _FakeQaGraph 实例追踪 calls 和 history
    fake_qa = _FakeQaGraph(llm="fake-llm-sentinel")

    # cmd_chat 内部 from brain_base.graphs.qa_graph import QaGraph
    # → monkeypatch 这个模块的 QaGraph 类，让构造时返回 fake_qa
    import brain_base.graphs.qa_graph as qa_graph_module
    monkeypatch.setattr(qa_graph_module, "QaGraph", lambda llm: fake_qa)

    # 模拟 stdin：3 轮输入 "boom" → "hello" → "/q"
    stdin_lines = iter(["boom", "hello", "/q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(stdin_lines))

    # 执行 cmd_chat
    args = argparse.Namespace()
    rc = cli.cmd_chat(args)

    # 验收 1：正常退出（rc=0），不应因 ValueError 崩
    assert rc == 0

    # 验收 2：qa.run 被调用 2 次（boom 一次、hello 一次；/q 是退出指令不调）
    assert fake_qa.calls == 2

    # 验收 3：第 2 次调用时 history 为空（错误轮未写入污染下轮上下文）
    history_at_call_2 = fake_qa.history_at_each_call[1]
    assert history_at_call_2 == [], (
        f"错误轮不应污染下轮 history，但收到：{history_at_call_2}"
    )

    # 验收 4：stderr 应含「[本轮失败]」提示 + ValueError 类型
    captured = capsys.readouterr()
    assert "[本轮失败]" in captured.err, (
        f"stderr 应含 [本轮失败] 标记：{captured.err!r}"
    )
    assert "ValueError" in captured.err, (
        f"stderr 应含异常类型 ValueError：{captured.err!r}"
    )
    assert "mocked LLM schema failure" in captured.err, (
        f"stderr 应含原始错误消息：{captured.err!r}"
    )

    # 验收 5：stdout 应含第 2 轮成功 answer
    assert "answer-for-hello" in captured.out


def test_cmd_chat_normal_round_writes_history(monkeypatch, capsys):
    """正常 1 轮：input "hello" → qa.run 正常 → history 应写入 2 条 (user + ai)。"""
    from brain_base import cli

    monkeypatch.setattr(cli, "_build_llm_from_env", lambda: "fake-llm-sentinel")

    fake_qa = _FakeQaGraph(llm="fake-llm-sentinel")
    # 让 _FakeQaGraph 第 1 轮也成功（用一个修改后的版本）

    class _AlwaysOkQa:
        def __init__(self, llm: Any) -> None:
            self.runs: list[tuple[str, list[dict]]] = []

        def run(self, *, question: str, conversation_history: list[dict] | None) -> dict:
            hist = list(conversation_history) if conversation_history else []
            self.runs.append((question, hist))
            return {"answer": f"answer-for-{question}"}

    qa = _AlwaysOkQa(llm="fake-llm-sentinel")
    import brain_base.graphs.qa_graph as qa_graph_module
    monkeypatch.setattr(qa_graph_module, "QaGraph", lambda llm: qa)

    # 2 轮输入 + 退出
    stdin_lines = iter(["q1", "q2", "/q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(stdin_lines))

    args = argparse.Namespace()
    rc = cli.cmd_chat(args)
    assert rc == 0

    # 验：q2 调用时 history 应已含 q1 的 user+ai
    assert len(qa.runs) == 2
    _, hist_at_q2 = qa.runs[1]
    assert len(hist_at_q2) == 2  # q1 user + q1 ai
    roles = [item["role"] for item in hist_at_q2]
    assert roles == ["user", "ai"]
    assert hist_at_q2[0]["text"] == "q1"
    assert hist_at_q2[1]["text"] == "answer-for-q1"
