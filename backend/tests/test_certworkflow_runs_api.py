"""工作流运行接口的契约测试（持久化层用内存实现替换）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import certworkflow_run as run_mod  # noqa: E402
from app.routers import certworkflow_runs  # noqa: E402


DEFINITION = {
    "id": "wf_demo",
    "name": "标准证书流程",
    "description": "",
    "node_count": 2,
    "nodes": [
        {"id": "monitor", "type": "domain_monitor", "title": "", "needs": []},
        {"id": "apply", "type": "cert_apply", "title": "", "needs": ["monitor"]},
    ],
}


@pytest.fixture()
def client(monkeypatch):
    runs = {}

    async def workflow_get(owner, workflow_id):
        return dict(DEFINITION) if workflow_id == "wf_demo" else None

    async def site_get(site_id, owner=None):
        if site_id == "site_1":
            return {"id": "site_1", "domain": "example.com"}
        return None

    async def list_runs(owner, site_id="", status="", limit=100):
        return [
            value
            for value in runs.values()
            if (not site_id or value["site_id"] == site_id)
            and (not status or value["status"] == status)
        ]

    async def stats_for(owner):
        return {
            "pending": sum(1 for item in runs.values() if item["status"] == "pending"),
            "running": sum(1 for item in runs.values() if item["status"] == "running"),
            "succeeded": 0,
            "rejected": 0,
            "cancelled": 0,
            "total": len(runs),
            "active": len(runs),
        }

    async def create_run(owner, workflow_id, workflow_name, definition, **kwargs):
        steps = run_mod.build_steps(definition)
        if not steps:
            raise run_mod.RunError("no_nodes")
        stamp = 100
        value = {
            "id": "run_1",
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "site_id": kwargs.get("site_id", ""),
            "site_domain": kwargs.get("site_domain", ""),
            "title": kwargs.get("title", ""),
            "status": run_mod.STATUS_PENDING,
            "current_index": -1,
            "steps": steps,
            "requester": owner,
            "approver": "",
            "request_note": kwargs.get("note", ""),
            "approval_note": "",
            "created_at": stamp,
            "updated_at": stamp,
            "started_at": 0,
            "finished_at": 0,
            "actions": ["approve", "reject", "cancel"],
        }
        runs["run_1"] = value
        return value

    async def get_run(owner, run_id):
        return runs.get(run_id)

    async def transition_run(owner, run_id, action, note="", operator=""):
        value = runs.get(run_id)
        if value is None:
            raise run_mod.RunError("not_found")
        updated = run_mod.apply_action(
            value, action, note=note, operator=operator, now=200
        )
        updated["actions"] = run_mod.RUN_ACTIONS.get(updated["status"], [])
        runs[run_id] = updated
        return updated

    # 注意：工作流定义读取用的是 certworkflows 模块里的 store 引用
    import app.certworkflow_store as workflow_store
    import app.certs as certs_mod

    async def no_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(workflow_store, "get", workflow_get)
    monkeypatch.setattr(certs_mod, "get_site", site_get)
    monkeypatch.setattr(certworkflow_runs.security, "audit", no_audit)
    for name, fn in [
        ("list_runs", list_runs),
        ("stats_for", stats_for),
        ("create_run", create_run),
        ("get_run", get_run),
        ("transition_run", transition_run),
    ]:
        monkeypatch.setattr(run_mod, name, fn)

    application = FastAPI()
    application.include_router(certworkflow_runs.router)
    application.dependency_overrides[certworkflow_runs.CERT_VIEW] = lambda: {
        "username": "admin"
    }
    application.dependency_overrides[certworkflow_runs.CERT_MANAGE] = lambda: {
        "username": "admin"
    }
    with TestClient(application) as test_client:
        yield test_client


def test_create_run_with_site_and_default_title(client):
    response = client.post(
        "/api/cert-workflow-runs",
        json={"workflow_id": "wf_demo", "site_id": "site_1", "note": "上线"},
    )
    assert response.status_code == 200
    body = response.json()["run"]
    assert body["status"] == "pending"
    assert body["current_index"] == -1
    assert body["site_domain"] == "example.com"
    assert body["title"] == "标准证书流程 · example.com"
    assert body["request_note"] == "上线"
    assert body["actions"] == ["approve", "reject", "cancel"]
    assert [step["node_id"] for step in body["steps"]] == ["monitor", "apply"]


def test_create_run_unknown_site_and_workflow(client):
    assert (
        client.post(
            "/api/cert-workflow-runs",
            json={"workflow_id": "wf_demo", "site_id": "nope"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/cert-workflow-runs", json={"workflow_id": "missing"}
        ).status_code
        == 404
    )


def test_create_run_without_nodes_returns_conflict(client, monkeypatch):
    import app.certworkflow_store as workflow_store

    async def empty_get(owner, workflow_id):
        return {"id": workflow_id, "name": "空流程", "nodes": []}

    monkeypatch.setattr(workflow_store, "get", empty_get)
    response = client.post(
        "/api/cert-workflow-runs", json={"workflow_id": "wf_demo"}
    )
    assert response.status_code == 409
    assert "没有节点" in response.json()["detail"]


def test_list_and_stats(client):
    client.post("/api/cert-workflow-runs", json={"workflow_id": "wf_demo"})
    body = client.get("/api/cert-workflow-runs").json()
    assert body["count"] == 1
    assert body["stats"]["pending"] == 1
    assert body["stats"]["active"] == 1

    filtered = client.get("/api/cert-workflow-runs?status=cancelled").json()
    assert filtered["count"] == 0


def test_transition_flow(client):
    client.post("/api/cert-workflow-runs", json={"workflow_id": "wf_demo"})

    approved = client.post(
        "/api/cert-workflow-runs/run_1/transition",
        json={"action": "approve", "note": "同意"},
    )
    assert approved.status_code == 200
    body = approved.json()["run"]
    assert body["status"] == "running"
    assert body["approver"] == "admin"
    assert body["actions"] == ["advance", "cancel"]
    assert body["steps"][0]["status"] == "running"

    advanced = client.post(
        "/api/cert-workflow-runs/run_1/transition",
        json={"action": "advance", "note": "监控完成"},
    ).json()["run"]
    assert advanced["steps"][0]["status"] == "done"
    assert advanced["steps"][1]["status"] == "running"

    done = client.post(
        "/api/cert-workflow-runs/run_1/transition", json={"action": "advance"}
    ).json()["run"]
    assert done["status"] == "succeeded"
    assert done["actions"] == []

    conflict = client.post(
        "/api/cert-workflow-runs/run_1/transition", json={"action": "advance"}
    )
    assert conflict.status_code == 409


def test_reject_and_invalid_action(client):
    client.post("/api/cert-workflow-runs", json={"workflow_id": "wf_demo"})
    rejected = client.post(
        "/api/cert-workflow-runs/run_1/transition",
        json={"action": "reject", "note": "信息不全"},
    ).json()["run"]
    assert rejected["status"] == "rejected"
    assert rejected["approval_note"] == "信息不全"

    invalid = client.post(
        "/api/cert-workflow-runs/run_1/transition", json={"action": "submit"}
    )
    assert invalid.status_code == 400
    assert "不支持" in invalid.json()["detail"]


def test_transition_missing_run(client):
    response = client.post(
        "/api/cert-workflow-runs/run_missing/transition", json={"action": "approve"}
    )
    assert response.status_code == 404
