# -*- coding: utf-8 -*-
"""Regression test for MinerU page_range parameter (bug fix 2026-05-11).

**Bug**: brain-base 早期 `_run_mineru_via_python_api` 给 MinerU `do_parse` 传
``page_range='X-Y'`` 字符串参数，但 MinerU `do_parse` 实际参数名是
``start_page_id`` + ``end_page_id``（0-indexed），``page_range`` 被 ``**kwargs``
吃掉但函数内部不用 → 每批都处理完整 PDF（22 页 PDF 跑 3 个 batch 实际处理 66 页）。

**Fix**: 解析 ``page_range='X-Y'``（1-idx inclusive） → 转换成
``start_page_id={X-1}`` + ``end_page_id={Y-1}``（0-idx inclusive）传给 do_parse。

测试用 mock subprocess.run 拦截 MinerU 调用，验证生成脚本里包含正确参数。
"""
from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# bin 模块名带连字符，importlib 动态加载
_doc_converter = importlib.import_module("bin.doc-converter")
_run_mineru_via_python_api = _doc_converter._run_mineru_via_python_api


@pytest.fixture
def mock_mineru_env():
    """Mock掉 subprocess.run / resolve_mineru_python / check_vram_before_mineru，
    让 _run_mineru_via_python_api 不真跑 MinerU。返回 subprocess.run 的 mock，
    供测试断言生成的 script 内容。"""
    mock_proc = MagicMock()
    mock_proc.returncode = 0

    with patch.object(_doc_converter, "resolve_mineru_python", return_value="C:/fake/python.exe"), \
         patch.object(_doc_converter, "subprocess") as mock_subprocess:
        mock_subprocess.run.return_value = mock_proc
        # CalledProcessError 等异常类透传，否则 try/except 处会出错
        mock_subprocess.CalledProcessError = subprocess.CalledProcessError
        yield mock_subprocess


def _get_generated_script(mock_subprocess) -> str:
    """从 mock subprocess.run 的调用参数里提取生成的 Python 脚本字符串。"""
    assert mock_subprocess.run.called, "subprocess.run 应被调用"
    call_args = mock_subprocess.run.call_args
    cmd = call_args.args[0] if call_args.args else call_args.kwargs.get("args") or call_args.kwargs["cmd"]
    # cmd = [python_exe, "-c", script, input_path_str, work_dir_str]
    assert len(cmd) >= 3, f"cmd 至少有 [python, -c, script]：{cmd!r}"
    assert cmd[1] == "-c", f"cmd[1] 应为 '-c'：{cmd!r}"
    return cmd[2]


# ---------------------------------------------------------------------------
# Case 1: page_range='1-10' → start_page_id=0, end_page_id=9
# ---------------------------------------------------------------------------


def test_page_range_first_10_pages(mock_mineru_env, tmp_path):
    """page_range='1-10' (1-idx inclusive) → start_page_id=0, end_page_id=9 (0-idx inclusive)."""
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    work_dir = tmp_path / "work"

    _run_mineru_via_python_api(pdf, work_dir, page_range="1-10")

    script = _get_generated_script(mock_mineru_env)
    assert "start_page_id=0," in script, f"应含 start_page_id=0：{script[:500]}"
    assert "end_page_id=9," in script, f"应含 end_page_id=9：{script[:500]}"
    # Regression guard: 旧 bug 是直接传 page_range='1-10' 字符串
    assert "page_range=" not in script, (
        f"修复后不应再有 page_range= 参数（MinerU do_parse 无此参数）：{script[:500]}"
    )


# ---------------------------------------------------------------------------
# Case 2: page_range='11-20' → start_page_id=10, end_page_id=19
# ---------------------------------------------------------------------------


def test_page_range_middle_batch(mock_mineru_env, tmp_path):
    """page_range='11-20' → start_page_id=10, end_page_id=19."""
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    work_dir = tmp_path / "work"

    _run_mineru_via_python_api(pdf, work_dir, page_range="11-20")

    script = _get_generated_script(mock_mineru_env)
    assert "start_page_id=10," in script
    assert "end_page_id=19," in script


# ---------------------------------------------------------------------------
# Case 3: page_range='21-22' → start_page_id=20, end_page_id=21
# ---------------------------------------------------------------------------


def test_page_range_last_partial_batch(mock_mineru_env, tmp_path):
    """page_range='21-22'（不足一批的尾部，DeepSeek-OCR 实测场景）→ start=20, end=21。"""
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    work_dir = tmp_path / "work"

    _run_mineru_via_python_api(pdf, work_dir, page_range="21-22")

    script = _get_generated_script(mock_mineru_env)
    assert "start_page_id=20," in script
    assert "end_page_id=21," in script


# ---------------------------------------------------------------------------
# Case 4: page_range=None → 不带 start/end 参数（do_parse 默认从 0 到最后一页）
# ---------------------------------------------------------------------------


def test_page_range_none_omits_params(mock_mineru_env, tmp_path):
    """page_range=None：不应在 do_parse 里加 start_page_id/end_page_id；让 MinerU 默认全 PDF。"""
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    work_dir = tmp_path / "work"

    _run_mineru_via_python_api(pdf, work_dir, page_range=None)

    script = _get_generated_script(mock_mineru_env)
    assert "start_page_id=" not in script, (
        f"page_range=None 时不应注入 start_page_id：{script[:500]}"
    )
    assert "end_page_id=" not in script
    assert "page_range=" not in script


# ---------------------------------------------------------------------------
# Case 5: 边界——page_range='1-1' 单页（start_page_id=0, end_page_id=0）
# ---------------------------------------------------------------------------


def test_page_range_single_page(mock_mineru_env, tmp_path):
    """page_range='1-1' 单页：start=0, end=0（验证 max(0, x-1) 不会出负数）。"""
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    work_dir = tmp_path / "work"

    _run_mineru_via_python_api(pdf, work_dir, page_range="1-1")

    script = _get_generated_script(mock_mineru_env)
    assert "start_page_id=0," in script
    assert "end_page_id=0," in script
