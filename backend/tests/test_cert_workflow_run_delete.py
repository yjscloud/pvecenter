"""删除审批实例：只允许终态，且按用户名隔离。

待审批 / 进行中的实例还在往前走，删掉会让正在执行的步骤失去归属；终态
（成功 / 拒绝 / 取消）的实例可以删，证书流水日志不受影响。

    ../.venv/bin/python -m pytest tests/test_cert_workflow_run_delete.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from fastapi import HTTPException  # noqa: E402

from app.routers import certworkflow_runs as route  # noqa: E402


def _patch(monkeypatch, run: Optional[Dict[str, Any]], *, deleted: bool = True):
    calls: Dict[str, Any] = {}

    async def fake_get(owner, run_id):
        calls["get"] = (owner, run_id)
        return run

    async def fake_delete(owner, run_id):
        calls["delete"] = (owner, run_id)
        return deleted

    async def fake_audit(*args, **_kwargs):
        calls["audit"] = args[2]

    monkeypatch.setattr(route.certworkflow_run, "get_run", fake_get)
    monkeypatch.setattr(route.certworkflow_run, "delete_run", fake_delete)
    monkeypatch.setattr(route.security, "audit", fake_audit)
    return calls


def test_delete_pending_run_is_rejected(monkeypatch):
    calls = _patch(monkeypatch, {"id": "run_1", "status": "pending"})

    try:
        asyncio.run(route.delete_run("run_1", None, {"username": "alice"}))
    except HTTPException as exc:
        assert exc.status_code == 409
    else:  # pragma: no cover
        raise AssertionError("expected 409 for a pending run")
    # 拒绝时不能真的去删
    assert "delete" not in calls


def test_delete_running_run_is_rejected(monkeypatch):
    _patch(monkeypatch, {"id": "run_1", "status": "running"})

    try:
        asyncio.run(route.delete_run("run_1", None, {"username": "alice"}))
    except HTTPException as exc:
        assert exc.status_code == 409
    else:  # pragma: no cover
        raise AssertionError("expected 409 for a running run")


def test_delete_finished_run(monkeypatch):
    calls = _patch(monkeypatch, {"id": "run_1", "status": "succeeded", "workflow_name": "wf"})

    result = asyncio.run(route.delete_run("run_1", None, {"username": "alice"}))

    assert result == {"removed": True}
    assert calls["get"] == ("alice", "run_1")
    assert calls["delete"] == ("alice", "run_1")
    assert calls["audit"] == "cert.workflow_run.delete"


def test_delete_missing_run_is_404(monkeypatch):
    _patch(monkeypatch, None)

    try:
        asyncio.run(route.delete_run("run_x", None, {"username": "alice"}))
    except HTTPException as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("expected 404 for a missing run")


def test_delete_conflict_when_row_vanished(monkeypatch):
    """查到了但删的时候已经不在了（并发删除）：仍按 404 回，不谎报成功。"""
    _patch(monkeypatch, {"id": "run_1", "status": "cancelled"}, deleted=False)

    try:
        asyncio.run(route.delete_run("run_1", None, {"username": "alice"}))
    except HTTPException as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("expected 404 when the row is already gone")
