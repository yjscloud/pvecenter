"""统一授权的纯规则：级别下发、工具裁剪与范围比较。

这些规则一旦放宽，表现不是「界面错了」，而是只读会话里突然出现写命令入口。
路由编排依赖数据库与 PVE，放在接口测试里；这里先钉住最容易回归的几处。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import ai, aitools  # noqa: E402
from app.routers.ai import _exec_access, _lease_scope_matches  # noqa: E402


def test_exec_access_defaults_to_old_behavior_for_legacy_clients() -> None:
    assert _exec_access({"allow_exec": True}, True) == "write"
    assert _exec_access({"allow_exec": True, "exec_access": "read"}, True) == "read"
    assert _exec_access({"allow_exec": True, "exec_access": "write"}, True) == "write"


def test_exec_access_is_read_when_host_channel_is_off() -> None:
    assert _exec_access({"exec_access": "write"}, False) == "read"


def test_read_only_tools_exclude_write_proposals() -> None:
    names = {
        item["function"]["name"]
        for item in aitools.openai_tools(include_exec=True, include_write=False)
    }
    assert "host_run_command" in names
    assert "host_propose_command" not in names


def test_write_tools_include_proposals() -> None:
    names = {
        item["function"]["name"]
        for item in aitools.openai_tools(include_exec=True, include_write=True)
    }
    assert "host_propose_command" in names


def test_read_only_prompt_does_not_tell_model_to_propose_writes() -> None:
    prompt = ai.scope_for(True, False)
    assert "未获授权" in prompt
    assert "必须先调用 host_propose_command" not in prompt


def test_reused_lease_requires_same_scope() -> None:
    lease = {"access": "write", "fs_rw": ["/etc"], "units": ["nginx"]}
    assert _lease_scope_matches(lease, fs_rw=["/etc"], units=["nginx"])
    assert not _lease_scope_matches(lease, fs_rw=["/tmp"], units=["nginx"])
    assert not _lease_scope_matches(lease, fs_rw=["/etc"], units=["sshd"])
