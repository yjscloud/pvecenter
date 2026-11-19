"""证书工作流接口的契约测试（不依赖 MySQL）。

持久化层用内存字典替换，重点验证：
- 导入成功 / 失败的结构化响应（ok、errors、warnings、format）；
- 重名导入自动改名；
- 列表 / 详情 / 创建 / 更新 / 删除 / 导出 的响应形状与状态码；
- 校验接口在保存前就能给出可定位的错误。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app.routers import certworkflows  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    store: Dict[str, Dict[str, Any]] = {}
    counter = {"n": 0}

    async def list_for(owner: str) -> List[Dict[str, Any]]:
        return [
            {key: value for key, value in item.items() if key != "nodes"}
            for item in store.values()
            if item["username"] == owner
        ]

    async def names(owner: str) -> set:
        return {item["name"] for item in store.values() if item["username"] == owner}

    async def name_exists(owner: str, name: str, exclude_id: str = "") -> bool:
        return any(
            item["username"] == owner
            and item["name"] == name
            and item["id"] != exclude_id
            for item in store.values()
        )

    async def create(owner: str, definition: Dict[str, Any], origin: str = "editor"):
        counter["n"] += 1
        workflow_id = f"wf_test{counter['n']}"
        now = int(time.time())
        item = {
            "id": workflow_id,
            "username": owner,
            "name": definition["name"],
            "description": definition.get("description") or "",
            "version": 1,
            "origin": origin,
            "node_count": len(definition.get("nodes") or []),
            "created_at": now,
            "updated_at": now,
            "nodes": definition.get("nodes") or [],
        }
        store[workflow_id] = item
        return {key: value for key, value in item.items() if key != "username"}

    async def get(owner: str, workflow_id: str) -> Optional[Dict[str, Any]]:
        item = store.get(workflow_id)
        if not item or item["username"] != owner:
            return None
        return {key: value for key, value in item.items() if key != "username"}

    async def update(owner: str, workflow_id: str, definition: Dict[str, Any]):
        item = store.get(workflow_id)
        if not item or item["username"] != owner:
            return None
        item.update(
            name=definition["name"],
            description=definition.get("description") or "",
            node_count=len(definition.get("nodes") or []),
            nodes=definition.get("nodes") or [],
            version=item["version"] + 1,
            updated_at=int(time.time()),
        )
        return {key: value for key, value in item.items() if key != "username"}

    async def delete(owner: str, workflow_id: str) -> bool:
        item = store.get(workflow_id)
        if not item or item["username"] != owner:
            return False
        store.pop(workflow_id)
        return True

    for name, fn in [
        ("list_for", list_for),
        ("names", names),
        ("name_exists", name_exists),
        ("create", create),
        ("get", get),
        ("update", update),
        ("delete", delete),
    ]:
        monkeypatch.setattr(certworkflows.certworkflow_store, name, fn)

    application = FastAPI()
    application.include_router(certworkflows.router)
    application.dependency_overrides[certworkflows.CERT_VIEW] = lambda: {
        "username": "admin"
    }
    application.dependency_overrides[certworkflows.CERT_MANAGE] = lambda: {
        "username": "admin"
    }
    with TestClient(application) as test_client:
        yield test_client


YAML_DEMO = """name: 演示流程
description: 监控后通知
nodes:
  - type: monitor
    options:
      domain: example.com
  - type: notify
    options:
      channels: [inapp, email]
      recipients: ops@example.com
"""


def test_import_yaml_creates_workflow_and_renames_duplicate(client):
    first = client.post(
        "/api/cert-workflows/import",
        json={"filename": "demo.yaml", "content": YAML_DEMO},
    )
    assert first.status_code == 200
    body = first.json()
    assert body["ok"] is True
    assert body["format"] == "yaml"
    assert body["errors"] == []
    assert body["workflow"]["name"] == "演示流程"
    assert body["workflow"]["node_count"] == 2
    assert body["workflow"]["origin"] == "import"
    assert body["workflow"]["nodes"][1]["needs"] == [
        body["workflow"]["nodes"][0]["id"]
    ]

    second = client.post(
        "/api/cert-workflows/import",
        json={"filename": "demo.yaml", "content": YAML_DEMO},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["ok"] is True
    assert body["renamed_from"] == "演示流程"
    assert body["workflow"]["name"] == "演示流程 (2)"
    assert any(item["path"] == "name" for item in body["warnings"])


def test_import_json_minimal(client):
    content = json.dumps(
        {
            "nodes": [
                {"type": "domain_monitor", "options": {"domain": "a.example.com"}}
            ]
        }
    )
    response = client.post(
        "/api/cert-workflows/import",
        json={"filename": "site.json", "content": content},
    )
    body = response.json()
    assert body["ok"] is True
    assert body["format"] == "json"
    assert body["workflow"]["name"] == "site"


def test_import_bad_yaml_returns_line_and_does_not_create(client):
    response = client.post(
        "/api/cert-workflows/import",
        json={"filename": "broken.yaml", "content": "name: x\nnodes: ["},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["errors"][0]["line"] == 2
    assert "YAML 解析失败" in body["errors"][0]["message"]
    assert client.get("/api/cert-workflows").json()["count"] == 0


def test_import_unknown_node_type_returns_path(client):
    content = """name: x
nodes:
  - type: send-mail
    options: {}
"""
    response = client.post(
        "/api/cert-workflows/import",
        json={"filename": "bad.yaml", "content": content},
    )
    body = response.json()
    assert body["ok"] is False
    assert body["errors"][0]["path"] == "nodes[0].type"
    assert "不支持的节点类型" in body["errors"][0]["message"]


def test_import_wrong_format_and_empty_file(client):
    response = client.post(
        "/api/cert-workflows/import",
        json={"filename": "demo.txt", "content": "name: x"},
    )
    assert "仅支持导入" in response.json()["errors"][0]["message"]

    response = client.post(
        "/api/cert-workflows/import",
        json={"filename": "demo.yaml", "content": "   "},
    )
    assert "为空" in response.json()["errors"][0]["message"]


def test_validate_reports_all_errors(client):
    response = client.post(
        "/api/cert-workflows/validate",
        json={
            "name": "",
            "nodes": [
                {"id": "a", "type": "domain_monitor", "options": {}},
                {
                    "id": "b",
                    "type": "cert_apply",
                    "needs": ["missing"],
                    "options": {"domains": ["ok.com"]},
                },
            ],
        },
    )
    body = response.json()
    assert body["valid"] is False
    assert len(body["errors"]) >= 3
    assert body["definition"] is None


def test_crud_and_export(client):
    created = client.post(
        "/api/cert-workflows",
        json={
            "name": "手工流程",
            "description": "从空白开始",
            "nodes": [],
            "origin": "blank",
        },
    )
    assert created.status_code == 200
    workflow = created.json()["workflow"]
    assert workflow["origin"] == "blank"
    assert workflow["node_count"] == 0

    listing = client.get("/api/cert-workflows").json()
    assert listing["count"] == 1
    assert listing["items"][0]["name"] == "手工流程"

    updated = client.put(
        f"/api/cert-workflows/{workflow['id']}",
        json={
            "name": "手工流程 v2",
            "description": "",
            "nodes": [
                {"type": "domain_monitor", "options": {"domain": "example.com"}}
            ],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["workflow"]["version"] == 2
    assert updated.json()["workflow"]["node_count"] == 1

    yaml_export = client.get(f"/api/cert-workflows/{workflow['id']}/export?format=yaml")
    assert yaml_export.status_code == 200
    assert "手工流程 v2" in yaml_export.text
    assert "application/x-yaml" in yaml_export.headers["content-type"]

    json_export = client.get(f"/api/cert-workflows/{workflow['id']}/export?format=json")
    assert json.loads(json_export.text)["name"] == "手工流程 v2"

    removed = client.delete(f"/api/cert-workflows/{workflow['id']}")
    assert removed.status_code == 200
    assert client.get(f"/api/cert-workflows/{workflow['id']}").status_code == 404


def test_duplicate_name_on_manual_create_returns_conflict(client):
    payload = {"name": "同名", "description": "", "nodes": [], "origin": "blank"}
    assert client.post("/api/cert-workflows", json=payload).status_code == 200
    conflict = client.post("/api/cert-workflows", json=payload)
    assert conflict.status_code == 409
    assert "同名" in conflict.json()["detail"]


def test_template_metadata_is_localized(client):
    response = client.get("/api/cert-workflows/templates")
    body = response.json()
    assert len(body["templates"]) == 2
    names = {item["name"] for item in body["templates"]}
    assert "标准证书流程" in names
    assert all(item["name_key"].startswith("workflow.template.") for item in body["templates"])


def test_node_catalog_contract(client):
    body = client.get("/api/cert-workflows/node-types").json()
    types = {item["type"] for item in body["types"]}
    assert types == {
        "domain_monitor",
        "cert_apply",
        "deploy_upload",
        "notification",
    }
    assert body["max_nodes"] > 0
