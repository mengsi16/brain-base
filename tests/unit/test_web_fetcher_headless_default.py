# -*- coding: utf-8 -*-
"""验证 _resolve_headless 默认反转为 **有头**（Google 无头检测严格）。

覆盖：
- 缺失环境变量 → False（有头）
- 空字符串 → False
- "0" / "false" / "no" / "off" → False（有头）
- "1" / "true" / "yes" / "on" → True（无头）
- override 参数优先于环境变量
- override=False 强制有头（即使 env=1）
- override=True 强制无头（即使 env=0）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from brain_base.tools.web_fetcher import _resolve_headless


class TestResolveHeadlessDefault:
    """默认反转：缺失 / 非显式无头值 → 有头 (False)。"""

    def test_missing_env_defaults_to_headed(self, monkeypatch):
        monkeypatch.delenv("BB_PLAYWRIGHT_HEADLESS", raising=False)
        assert _resolve_headless() is False, "缺失环境变量应默认有头"

    def test_empty_string_defaults_to_headed(self, monkeypatch):
        monkeypatch.setenv("BB_PLAYWRIGHT_HEADLESS", "")
        assert _resolve_headless() is False

    @pytest.mark.parametrize("val", ["0", "false", "False", "FALSE", "no", "No", "off"])
    def test_explicit_false_is_headed(self, monkeypatch, val):
        """显式设 false-ish 值 → 有头（与默认一致）。"""
        monkeypatch.setenv("BB_PLAYWRIGHT_HEADLESS", val)
        assert _resolve_headless() is False, f"{val!r} 应是有头"

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "True", "yes", "Yes", "on"])
    def test_explicit_true_is_headless(self, monkeypatch, val):
        """显式设 true-ish 值 → 无头（CI / 服务器场景）。"""
        monkeypatch.setenv("BB_PLAYWRIGHT_HEADLESS", val)
        assert _resolve_headless() is True, f"{val!r} 应是无头"

    def test_unknown_value_defaults_to_headed(self, monkeypatch):
        """未识别的值视为 false（有头）——向后兼容宽松策略。"""
        monkeypatch.setenv("BB_PLAYWRIGHT_HEADLESS", "maybe")
        assert _resolve_headless() is False


class TestResolveHeadlessOverride:
    """override 参数优先级最高。"""

    def test_override_false_forces_headed(self, monkeypatch):
        monkeypatch.setenv("BB_PLAYWRIGHT_HEADLESS", "1")  # env 请求无头
        assert _resolve_headless(override=False) is False, "override=False 强制有头"

    def test_override_true_forces_headless(self, monkeypatch):
        monkeypatch.setenv("BB_PLAYWRIGHT_HEADLESS", "0")  # env 请求有头
        assert _resolve_headless(override=True) is True, "override=True 强制无头"

    def test_override_none_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("BB_PLAYWRIGHT_HEADLESS", "1")
        assert _resolve_headless(override=None) is True
        monkeypatch.setenv("BB_PLAYWRIGHT_HEADLESS", "0")
        assert _resolve_headless(override=None) is False
