# -*- coding: utf-8 -*-
"""T25-A convert_html_to_markdown_readability 测试。

mock subprocess.Popen 拦截 Node.js 子进程调用，不真起 node。
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from brain_base.tools import doc_converter_tool as dct


class _FakePopen:
    """模拟 subprocess.Popen，可控 returncode / stdout / stderr / 是否超时。"""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        raise_timeout: bool = False,
    ):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._raise_timeout = raise_timeout
        self.killed = False
        self.last_input: bytes | None = None

    def communicate(self, *, input: bytes, timeout: float):  # noqa: A002
        self.last_input = input
        if self._raise_timeout:
            raise subprocess.TimeoutExpired(cmd="node", timeout=timeout)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True


@pytest.fixture
def fake_node_in_path():
    """让 shutil.which('node') 返回假路径。"""
    with patch.object(dct.shutil, "which", return_value="C:/fake/node.exe"):
        yield


@pytest.fixture
def script_exists(monkeypatch):
    """让 _BIN_DIR / readability-converter.js 文件存在判定通过（真文件已存在则直接 yield）。"""
    real = dct._BIN_DIR / "readability-converter.js"
    if real.exists():
        yield
    else:
        # 极端情况下脚本不在仓库——构造一个 Path mock 让 .exists() 返回 True
        original_truediv = type(dct._BIN_DIR).__truediv__

        def _patched(self, other):
            p = original_truediv(self, other)
            if str(p).endswith("readability-converter.js"):
                p_mock = MagicMock()
                p_mock.exists.return_value = True
                p_mock.__str__ = lambda _: str(p)
                return p_mock
            return p

        monkeypatch.setattr(type(dct._BIN_DIR), "__truediv__", _patched)
        yield


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


def test_readability_returns_markdown_on_success(fake_node_in_path, script_exists):
    """子进程 rc=0 + stdout markdown → 返回 markdown 字符串。"""
    fake = _FakePopen(returncode=0, stdout="# 标题\n\n正文".encode("utf-8"))
    with patch.object(dct.subprocess, "Popen", return_value=fake):
        result = dct.convert_html_to_markdown_readability("<html><body>x</body></html>")
    assert result == "# 标题\n\n正文"
    assert fake.last_input == b"<html><body>x</body></html>"


def test_readability_empty_html_returns_empty_string(fake_node_in_path, script_exists):
    """空 HTML 直接返回 ""，不起子进程。"""
    with patch.object(dct.subprocess, "Popen") as popen:
        result = dct.convert_html_to_markdown_readability("")
    assert result == ""
    popen.assert_not_called()


def test_readability_whitespace_only_returns_empty_string(fake_node_in_path, script_exists):
    """纯空白 HTML 直接返回 ""，不起子进程。"""
    with patch.object(dct.subprocess, "Popen") as popen:
        result = dct.convert_html_to_markdown_readability("   \n\t  ")
    assert result == ""
    popen.assert_not_called()


# ---------------------------------------------------------------------------
# 失败路径（fail-fast）
# ---------------------------------------------------------------------------


def test_readability_subprocess_nonzero_raises(fake_node_in_path, script_exists):
    """子进程非零退出 → RuntimeError，错误信息包含 stderr。"""
    fake = _FakePopen(returncode=3, stderr="Readability 未抽出主体".encode("utf-8"))
    with patch.object(dct.subprocess, "Popen", return_value=fake):
        with pytest.raises(RuntimeError, match="readability-converter 失败 rc=3"):
            dct.convert_html_to_markdown_readability("<html>bad</html>")


def test_readability_subprocess_timeout_raises(fake_node_in_path, script_exists):
    """子进程超时 → RuntimeError，并 kill 子进程。"""
    fake = _FakePopen(raise_timeout=True)
    with patch.object(dct.subprocess, "Popen", return_value=fake):
        with pytest.raises(RuntimeError, match="readability-converter 超时"):
            dct.convert_html_to_markdown_readability("<html>x</html>", timeout=0.1)
    assert fake.killed is True


def test_readability_node_not_in_path_raises(script_exists):
    """node 不在 PATH → RuntimeError。"""
    with patch.object(dct.shutil, "which", return_value=None):
        with pytest.raises(RuntimeError, match="node 不在 PATH"):
            dct.convert_html_to_markdown_readability("<html>x</html>")
