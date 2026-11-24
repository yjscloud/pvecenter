"""P0 安全修复的回归用例。

对应审计文档（``/root/pvecenter-security-audit/docs/``）：

* PVE-SSRF-001 —— 注册码探活失败不能无限退码（否则一张码 = 内网扫描额度）；
* PVE-AUTH-001 —— 改邮箱要验证当前口令（+ 2FA），只凭会话令牌不算数；
* PVE-AUTHZ-001 —— 节点 / 存储名拼进 PVE API 路径前必须编码 + 白名单；
* PVE-AUTHZ-002 —— 存储内容清单不再透传「类型未知」的条目；
* PVE-AUTHZ-003 —— 虚拟机克隆必须落归属（对齐容器克隆）。

这些用例都不连真实 PVE：要么是纯函数，要么 monkeypatch 掉客户端，
要么走 ``test_api_routes`` 的 ``api`` fixture（mock Proxmox + 每用例空库）。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi import HTTPException
from starlette.requests import Request

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import connregister, database, ownership  # noqa: E402
from app import pve as pve_module  # noqa: E402
from app.routers import storages, vms  # noqa: E402
from app.config import settings  # noqa: E402

from conftest import connect  # noqa: E402
from test_api_routes import ADMIN, api, auth_headers  # noqa: E402,F401


def _run(coro: Any) -> Any:
    async def _wrapper() -> Any:
        try:
            return await coro
        finally:
            await database.close_pool()

    return asyncio.run(_wrapper())


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/test",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


# --------------------------------------------------------------- PVE-SSRF-001
def _seed_code(code_id: str) -> None:
    conn = connect(settings.db_name)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO connection_register_codes"
                " (id, code_hash, created, expires, max_uses, used_count)"
                " VALUES (%s,%s,%s,%s,5,0)",
                (code_id, code_id.ljust(64, "0"), 1, 9999999999),
            )
        conn.commit()
    finally:
        conn.close()


def _create_code_table() -> None:
    """只建注册码这一张表：本用例不启动应用，表得自己准备。"""
    conn = connect(settings.db_name)
    try:
        with conn.cursor() as cursor:
            cursor.execute(connregister.SCHEMA.strip().rstrip(";"))
        conn.commit()
    finally:
        conn.close()


def test_register_failure_refund_is_capped() -> None:
    """同一张码最多免费失败 3 次，第 4 次起不再退额度。"""
    _create_code_table()
    _seed_code("code-a")
    # 连接池绑定在创建它的 loop 上；上一个用例可能留下一个已关闭循环的池
    database._mysql_pool = None

    async def _exercise() -> list:
        # 同一个事件循环里连续调用：连接池绑定在创建它的 loop 上，
        # 每步都 asyncio.run 会拿到「Future attached to a different loop」。
        await connregister.init_table()
        results = [await connregister.record_failure("code-a") for _ in range(4)]
        # 成功后清零，下次接入重新获得重试额度
        await connregister.reset_failures("code-a")
        results.append(await connregister.record_failure("code-a"))
        return results

    results = _run(_exercise())

    assert results[:3] == [True, True, True]
    assert results[3] is False
    assert results[4] is True


# --------------------------------------------------------------- PVE-AUTH-001
def test_email_change_requires_current_password(api) -> None:  # noqa: F811
    headers = auth_headers(api)  # type: ignore[arg-type]

    missing = api.put(
        "/api/auth/me", json={"email": "attacker@example.com"}, headers=headers
    )
    assert missing.status_code == 400
    assert "密码" in missing.text

    wrong = api.put(
        "/api/auth/me",
        json={"email": "attacker@example.com", "current_password": "nope"},
        headers=headers,
    )
    assert wrong.status_code == 400

    ok = api.put(
        "/api/auth/me",
        json={
            "email": "owner@example.com",
            "current_password": ADMIN["password"],
        },
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["email"] == "owner@example.com"


def test_email_unchanged_does_not_require_password(api) -> None:  # noqa: F811
    """单纯点保存（邮箱没变）不该被口令卡住。"""
    headers = auth_headers(api)  # type: ignore[arg-type]
    current = api.get("/api/auth/me", headers=headers).json()["email"]

    resp = api.put("/api/auth/me", json={"email": current}, headers=headers)

    assert resp.status_code == 200, resp.text


# ----------------------------------------------------- PVE-AUTHZ-001 / 002
def test_api_path_encodes_dynamic_segments() -> None:
    """``..`` / ``?`` / ``/`` 都不会活到 URL 里，注入形态变成普通字符串。"""
    path = pve_module.api_path(
        "nodes", "../access", "storage", "a?b", "content", "local:iso/x.iso"
    )

    assert path == (
        "/nodes/..%2Faccess/storage/a%3Fb/content/local%3Aiso%2Fx.iso"
    )
    # 动态段里的斜杠被编码，不会多切出一级路径：6 个 "/" = 前导 + 5 段
    assert path.count("/") == 6
    assert "?" not in path


@pytest.mark.parametrize(
    "node,storage",
    [
        ("../access/users", "local"),
        ("pve1", "../etc"),
        ("pve1?x=1", "local"),
        ("", "local"),
        ("pve1", "local lvm"),
        ("pve1", "local#x"),
    ],
)
def test_storage_location_rejects_injection(node: str, storage: str) -> None:
    with pytest.raises(HTTPException) as exc:
        storages._check_location(node, storage)
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "node,storage", [("pve1", "local"), ("pve-1.example", "local-lvm"), ("a1", "ceph_ssd")]
)
def test_storage_location_accepts_real_names(node: str, storage: str) -> None:
    storages._check_location(node, storage)


def test_storage_content_drops_unrecognised_items(monkeypatch) -> None:
    """打到别的 PVE 接口时，返回的条目没有 content 字段，不能原样透传。"""
    monkeypatch.setattr(storages.security, "visible_owner", lambda user: "alice")
    monkeypatch.setattr(storages.security, "is_region_admin", lambda user: False)
    monkeypatch.setattr(storages.guestguard, "call_connection", lambda *a, **k: "c1")

    async def _owners() -> Dict[str, str]:
        return {}

    monkeypatch.setattr(storages.ownership, "guest_owners_map", _owners)
    monkeypatch.setattr(
        storages.ownership, "vm_owner_from", lambda owners, node, vmid, conn: "bob"
    )

    items = [
        {"volid": "local:iso/ubuntu.iso", "content": "iso"},  # 共享，保留
        {"volid": "local:vztmpl/debian.tar.zst", "content": "vztmpl"},  # 共享，保留
        {"volid": "local:100/vm-100-disk-0.raw", "content": "images", "vmid": 100},
        {"volid": "local:backup/vzdump-qemu-100.vma.zst", "content": "backup", "vmid": 100},
        {"userid": "root@pam", "email": "root@example.com"},  # 越权拿到的用户表
        {"sub": "UPID:pve1:0001", "status": "running"},  # 任务列表
    ]

    out = _run(
        storages._scope_storage_content(items, {"username": "alice"}, "pve1", "local")
    )

    assert [i.get("volid") for i in out] == [
        "local:iso/ubuntu.iso",
        "local:vztmpl/debian.tar.zst",
    ]


# --------------------------------------------------------------- PVE-AUTHZ-003
class _FakeClient:
    def __init__(self) -> None:
        self.cloned: Dict[str, Any] = {}

    async def qemu_clone(self, node: str, vmid: int, **kwargs: Any) -> str:
        self.cloned = {"node": node, "vmid": vmid, **kwargs}
        return "UPID:pve1:00001234:00000000:00000000:qmclone:100:root@pam:"

    async def wait_for_task(self, task: str, timeout: int = 600) -> None:
        return None

    async def qemu_config(self, node: str, vmid: int) -> Dict[str, Any]:
        return {}

    async def qemu_set_config(self, node: str, vmid: int, cfg: Dict[str, Any]) -> None:
        return None


def test_vm_clone_records_ownership(monkeypatch) -> None:
    """克隆成功后必须落归属：不落的话机器既不计配额，也能被别人整机克隆走。"""
    fake = _FakeClient()
    recorded: Dict[str, str] = {}

    monkeypatch.setattr(vms, "get_client", lambda: fake)
    monkeypatch.setattr(vms, "_op_connection", lambda: "c1")

    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    async def _fake_cloneguard(*args: Any, **kwargs: Any) -> None:
        return None

    async def _fake_set_owner(kind: str, ref: str, username: str) -> None:
        recorded.update(kind=kind, ref=ref, user=username)

    monkeypatch.setattr(vms.quota, "enforce_for_create", _noop)
    monkeypatch.setattr(vms.quota, "enforce_create", _noop)
    monkeypatch.setattr(vms.cloneguard, "ensure_cross_node_clone", _fake_cloneguard)
    monkeypatch.setattr(vms.prefs, "forget_guest_refs", _noop)
    monkeypatch.setattr(vms.ownership, "set_owner", _fake_set_owner)
    monkeypatch.setattr(vms.security, "audit", _noop)

    payload = vms.CloneRequest(newid=201, full=True)

    out = _run(
        vms.clone_vm(
            "pve1", 100, payload, _request(), {"username": "alice", "role": "user"}
        )
    )

    assert fake.cloned["newid"] == 201
    assert recorded == {
        "kind": ownership.KIND_VM,
        "ref": ownership.vm_ref("c1", "pve1", 201),
        "user": "alice",
    }
    assert out["vmid"] == 201
