"""证书 / 工作流删除的级联与引用处理。

回归点：删掉一个站点或一个工作流定义之后，引用它的数据不能留着 ——

* 站点删除：运行记录 / 部署日志 / 队列 / 租约 / 审批实例都按 ``site_id`` 记账，
  不跟着走就会留下「查不到站点的历史」；
* 工作流删除：还有待审批 / 进行中的实例时必须**拒绝**（否则这些实例永远卡在
  半路），可以删时要解绑引用它的站点并清掉历史实例。

    ../.venv/bin/python -m pytest tests/test_cert_delete_cascade.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from fastapi import HTTPException  # noqa: E402

from app import certs  # noqa: E402
from app.routers import certworkflows  # noqa: E402


def _patch_site_store(monkeypatch, sites):
    saved = {"raw": json.dumps(sites, ensure_ascii=False)}

    async def fake_get(_key, default=None):
        return saved["raw"]

    async def fake_cas(_key, value, expected):
        if expected != saved["raw"]:
            return False
        saved["raw"] = value
        return True

    async def fake_insert(_key, value):
        saved["raw"] = value
        return True

    monkeypatch.setattr(certs.store, "get_setting", fake_get)
    monkeypatch.setattr(certs.store, "compare_and_set_setting", fake_cas)
    monkeypatch.setattr(certs.store, "insert_setting_if_absent", fake_insert)
    return saved


def test_detach_workflow_only_touches_own_sites(monkeypatch):
    """工作流被删后，只解绑**自己的**站点；别人的绑定不能被动。"""
    saved = _patch_site_store(
        monkeypatch,
        [
            {"id": "s1", "username": "alice", "workflow_id": "w1", "approval_pending": True},
            {"id": "s2", "username": "alice", "workflow_id": "w2"},
            {"id": "s3", "username": "bob", "workflow_id": "w1"},
        ],
    )

    changed = asyncio.run(certs.detach_workflow("alice", "w1"))

    assert changed == 1
    data = json.loads(saved["raw"])
    assert data[0]["workflow_id"] == ""
    # 审批挂起标记一起清：审批已经不可能再来了
    assert data[0]["approval_pending"] is False
    assert data[1]["workflow_id"] == "w2"
    assert data[2]["workflow_id"] == "w1"


def test_detach_workflow_is_noop_without_binding(monkeypatch):
    saved = _patch_site_store(
        monkeypatch, [{"id": "s1", "username": "alice", "workflow_id": "w2"}]
    )
    before = saved["raw"]

    assert asyncio.run(certs.detach_workflow("alice", "w1")) == 0
    assert saved["raw"] == before


def _patch_delete_route(monkeypatch, *, active: int, deleted: bool = True):
    calls = {}

    async def fake_active(_owner, _wid):
        return active

    async def fake_delete(_owner, _wid):
        return deleted

    async def fake_detach(owner, wid):
        calls["detach"] = (owner, wid)
        return 3

    async def fake_runs(owner, wid):
        calls["runs"] = (owner, wid)
        return 4

    async def fake_audit(*args, **_kwargs):
        calls["audit"] = args[2]

    monkeypatch.setattr(certworkflows.certworkflow_run, "count_active", fake_active)
    monkeypatch.setattr(certworkflows.certworkflow_store, "delete", fake_delete)
    monkeypatch.setattr(certworkflows.certs, "detach_workflow", fake_detach)
    monkeypatch.setattr(certworkflows.certworkflow_run, "delete_for_workflow", fake_runs)
    monkeypatch.setattr(certworkflows.security, "audit", fake_audit)
    return calls


def test_delete_workflow_rejects_active_runs(monkeypatch):
    """还有待审批 / 进行中的实例时拒绝删除，并给出可操作的提示。"""
    _patch_delete_route(monkeypatch, active=2)

    try:
        asyncio.run(
            certworkflows.delete_workflow("w1", None, {"username": "alice"})
        )
    except HTTPException as exc:
        assert exc.status_code == 409
        assert "2" in str(exc.detail)
    else:  # pragma: no cover - 断言失败路径
        raise AssertionError("expected 409 for active runs")


def test_delete_workflow_cascades_and_audits(monkeypatch):
    calls = _patch_delete_route(monkeypatch, active=0)

    result = asyncio.run(
        certworkflows.delete_workflow("w1", None, {"username": "alice"})
    )

    assert result == {"removed": True, "detached_sites": 3, "removed_runs": 4}
    assert calls["detach"] == ("alice", "w1")
    assert calls["runs"] == ("alice", "w1")
    assert calls["audit"] == "cert.workflow.delete"


def test_delete_workflow_missing_returns_404(monkeypatch):
    _patch_delete_route(monkeypatch, active=0, deleted=False)

    try:
        asyncio.run(
            certworkflows.delete_workflow("w1", None, {"username": "alice"})
        )
    except HTTPException as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("expected 404 for a missing workflow")
