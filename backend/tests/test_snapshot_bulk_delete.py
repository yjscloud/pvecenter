"""Unit tests for the bulk snapshot delete endpoint (global snapshots page).

批量删除快照最容易坏在三处，且真机上都不好复现：

* **分派**：容器快照打到 ``/qemu/{vmid}/snapshot`` 上，PVE 直接报错；
* **越权**：批量端点没有路径参数，路由级归属守卫不生效，一不留神就绕过了
  快照级隔离；
* **单条失败拖垮整批**：一条 PVE 报错让整个请求 500，前端不知道删掉了几条。

    ../.venv/bin/python -m pytest tests/test_snapshot_bulk_delete.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app import ownership, security  # noqa: E402
from app.pve import ProxmoxError  # noqa: E402
from app.routers import vms as vms_router  # noqa: E402
from app.schemas import SnapshotBulkDelete, SnapshotBulkDeleteItem  # noqa: E402


def run(coro):
    """同步跑协程 —— 与 test_bulk 同一套路，不引入额外 pytest 插件。"""
    return asyncio.run(coro)


class FakeClient:
    """只实现删除用到的两个方法，顺带记录分派结果。"""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: List[tuple] = []

    async def qemu_snapshot_delete(self, node: str, vmid: int, name: str) -> str:
        self.calls.append(("qemu", node, vmid, name))
        if self.fail:
            raise ProxmoxError("snapshot is locked", 500)
        return f"UPID:pve:1:1:1::{node}:{vmid}:qmsnapshot:{name}:"

    async def lxc_snapshot_delete(self, node: str, vmid: int, name: str) -> str:
        self.calls.append(("lxc", node, vmid, name))
        if self.fail:
            raise ProxmoxError("snapshot is locked", 500)
        return f"UPID:pve:1:1:1::{node}:{vmid}:lxc:{name}:"


def _user(perms=("vm.snapshot", "ct.snapshot")) -> Dict[str, Any]:
    return {
        "username": "alice",
        "role": "admin",
        "permissions_override": None,
        "_perms": set(perms),
    }


def _item(node: str = "pve1", vmid: int = 101, kind: str = "qemu", name: str = "snap-a"):
    return SnapshotBulkDeleteItem(node=node, vmid=vmid, type=kind, name=name)


@pytest.fixture()
def world(monkeypatch) -> SimpleNamespace:
    """隔离外部依赖：不连 PVE、不连库、不起后台任务。"""
    client = FakeClient()
    audits: List[Dict[str, Any]] = []
    removed: List[str] = []
    scheduled: List[List[str]] = []

    monkeypatch.setattr(vms_router, "get_client", lambda: client)
    monkeypatch.setattr(vms_router, "_op_connection", lambda: "conn-1")
    monkeypatch.setattr(vms_router.pvecache, "invalidate", lambda prefix: None)
    # 后台等待任务在单测里没有意义（会一直挂到事件循环关闭），只记录参数
    monkeypatch.setattr(
        vms_router,
        "_invalidate_snapshots_after_tasks",
        lambda tasks: scheduled.append(list(tasks)),
    )

    async def fake_audit(
        request, user, action, target="", result="ok", detail=None  # noqa: ANN001
    ) -> None:
        audits.append(
            {"action": action, "target": target, "result": result, "detail": detail}
        )

    monkeypatch.setattr(security, "audit", fake_audit)
    # 权限按用户身上的 _perms 判（真实现读角色表，这里只关心「有没有这个点」）
    monkeypatch.setattr(
        security, "has_user_permission", lambda user, perm: perm in user["_perms"]
    )
    # 默认管理员视角：不做归属过滤
    monkeypatch.setattr(security, "visible_owner", lambda user: None)
    monkeypatch.setattr(security, "is_region_admin", lambda user: False)

    async def fake_delete_owner(kind: str, ref: str) -> None:
        removed.append(ref)

    monkeypatch.setattr(ownership, "delete_owner", fake_delete_owner)
    monkeypatch.setattr(
        ownership,
        "snapshot_ref",
        lambda conn, node, vmid, name: f"{conn}|{node}|{vmid}|{name}",
    )

    return SimpleNamespace(
        client=client, audits=audits, removed=removed, scheduled=scheduled
    )


# ------------------------------------------------------------------ 正常路径
def test_reports_every_item_and_dispatches_by_type(world: SimpleNamespace) -> None:
    payload = SnapshotBulkDelete(
        items=[
            _item(vmid=101, kind="qemu", name="snap-a"),
            _item(vmid=201, kind="lxc", name="snap-b"),
        ]
    )

    result = run(
        vms_router.bulk_delete_snapshots(
            payload=payload, request=SimpleNamespace(), user=_user()
        )
    )

    assert result["action"] == "snapshot_delete"
    assert (result["total"], result["ok"], result["failed"]) == (2, 2, 0)
    # 类型决定端点：容器必须走 lxc_*，否则 PVE 会回「快照不存在」
    assert [call[0] for call in world.client.calls] == ["qemu", "lxc"]
    assert [row["type"] for row in result["results"]] == ["qemu", "lxc"]
    assert all(str(row["task"]).startswith("UPID:") for row in result["results"])
    # 快照归属标记随之清掉
    assert world.removed == [
        "conn-1|pve1|101|snap-a",
        "conn-1|pve1|201|snap-b",
    ]
    # 任务结束后统一作废一次快照缓存（批量只起一个等待协程）
    assert world.scheduled == [
        [
            "UPID:pve:1:1:1::pve1:101:qmsnapshot:snap-a:",
            "UPID:pve:1:1:1::pve1:201:lxc:snap-b:",
        ]
    ]


# ------------------------------------------------------------- 逐条权限判定
def test_missing_container_permission_fails_only_that_item(
    world: SimpleNamespace,
) -> None:
    payload = SnapshotBulkDelete(
        items=[_item(vmid=101, kind="qemu", name="snap-a"), _item(vmid=201, kind="lxc", name="snap-b")]
    )

    result = run(
        vms_router.bulk_delete_snapshots(
            payload=payload,
            request=SimpleNamespace(),
            user=_user(perms=("vm.snapshot",)),
        )
    )

    assert result["ok"] == 1
    assert result["failed"] == 1
    qemu_row, lxc_row = result["results"]
    assert qemu_row["ok"] is True
    assert lxc_row["ok"] is False
    assert "ct.snapshot" in (lxc_row["error"] or "")
    # 没权限的那条不应该打到 PVE 上
    assert [call[0] for call in world.client.calls] == ["qemu"]


def test_snapshot_level_isolation_is_reported_per_item(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """普通用户删别人的快照：整批照常，越权那条只失败自己。"""
    monkeypatch.setattr(security, "visible_owner", lambda user: "alice")

    async def fake_get_owner(kind: str, ref: str) -> str:
        return "bob"

    monkeypatch.setattr(ownership, "get_owner", fake_get_owner)

    payload = SnapshotBulkDelete(items=[_item(vmid=101, name="snap-a")])
    result = run(
        vms_router.bulk_delete_snapshots(
            payload=payload, request=SimpleNamespace(), user=_user()
        )
    )

    assert result["failed"] == 1
    assert "不属于当前用户" in (result["results"][0]["error"] or "")
    assert world.client.calls == []
    assert world.removed == []


# ---------------------------------------------------------------- 失败隔离
def test_single_failure_does_not_abort_the_batch(world: SimpleNamespace) -> None:
    world.client.fail = True
    payload = SnapshotBulkDelete(
        items=[_item(vmid=101, name="snap-a"), _item(vmid=102, name="snap-b")]
    )

    result = run(
        vms_router.bulk_delete_snapshots(
            payload=payload, request=SimpleNamespace(), user=_user()
        )
    )

    assert (result["ok"], result["failed"]) == (0, 2)
    assert all(row["error"] == "snapshot is locked" for row in result["results"])
    # 一条都没受理：不排后台等待、不清缓存
    assert world.scheduled == []
    assert world.removed == []
    # 每条都要留下审计（失败原因要能追）
    assert [a["result"] for a in world.audits] == ["failed", "failed"]


def test_http_denial_from_access_guard_is_reported(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_require_snapshot_access`` 抛的是 HTTPException(403)，不能穿透整批。"""

    async def deny(*args, **kwargs):
        raise HTTPException(status_code=403, detail="快照已被保护")

    monkeypatch.setattr(vms_router, "_require_snapshot_access", deny)

    payload = SnapshotBulkDelete(items=[_item()])
    result = run(
        vms_router.bulk_delete_snapshots(
            payload=payload, request=SimpleNamespace(), user=_user()
        )
    )

    assert result["failed"] == 1
    assert result["results"][0]["error"] == "快照已被保护"
    assert world.client.calls == []


# ---------------------------------------------------------------- 请求校验
class TestRequestValidation:
    def test_empty_items_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SnapshotBulkDelete(items=[])

    def test_duplicate_targets_rejected(self) -> None:
        """同一条点两次会让 PVE 收到两次删除，第二次必然报「快照不存在」。"""
        with pytest.raises(ValidationError):
            SnapshotBulkDelete(items=[_item(), _item()])

    def test_same_vmid_with_different_snapshots_allowed(self) -> None:
        payload = SnapshotBulkDelete(
            items=[_item(name="snap-a"), _item(name="snap-b")]
        )
        assert len(payload.items) == 2

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SnapshotBulkDelete(items=[_item(name="   ")])

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SnapshotBulkDelete(items=[_item(kind="kvm")])
