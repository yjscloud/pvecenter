"""Unit tests for the bulk snapshot restore endpoint (per-guest snapshot choice).

批量还原最容易坏在四处，且真机上都不好复现：

* **串名**：把 A 机器选的快照名下发到 B 机器（回滚到错误的点，数据直接回到旧状态）；
* **分派**：容器还原打到 ``/qemu/{vmid}/snapshot`` 上，PVE 报「快照不存在」；
* **越权**：批量端点没有路径参数，路由级归属守卫不生效，漏判就绕过快照级隔离；
* **快照链截断**：还原会删掉目标之后的快照，界面与归属记录都要跟着变。

    ../.venv/bin/python -m pytest tests/test_snapshot_bulk_restore.py -v
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
from app.schemas import SnapshotBulkRestore, SnapshotBulkRestoreItem  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeClient:
    """只实现还原用到的方法，顺带记录「哪台机器回滚到了哪个快照」。"""

    def __init__(self, fail: bool = False, snaps: Any = None) -> None:
        self.fail = fail
        self.calls: List[tuple] = []
        # vmid -> 该机器的快照链（默认两条：base -> later）
        self.snaps = snaps if snaps is not None else {
            101: [
                {"name": "base", "parent": ""},
                {"name": "later", "parent": "base"},
                {"name": "current", "parent": "later"},
            ]
        }

    async def qemu_snapshots(self, node: str, vmid: int):
        return self.snaps.get(vmid, [])

    async def lxc_snapshots(self, node: str, vmid: int):
        return self.snaps.get(vmid, [])

    async def qemu_snapshot_rollback(self, node: str, vmid: int, name: str) -> str:
        self.calls.append(("qemu", node, vmid, name))
        if self.fail:
            raise ProxmoxError("VM is locked (backup)", 500)
        return f"UPID:pve:1:1:1::{node}:{vmid}:qmrollback:{name}:"

    async def lxc_snapshot_rollback(self, node: str, vmid: int, name: str) -> str:
        self.calls.append(("lxc", node, vmid, name))
        if self.fail:
            raise ProxmoxError("VM is locked (backup)", 500)
        return f"UPID:pve:1:1:1::{node}:{vmid}:lxcrollback:{name}:"


def _user(perms=("vm.snapshot", "ct.snapshot")) -> Dict[str, Any]:
    return {
        "username": "alice",
        "role": "admin",
        "permissions_override": None,
        "_perms": set(perms),
    }


def _item(node: str = "pve1", vmid: int = 101, kind: str = "qemu", name: str = "base"):
    return SnapshotBulkRestoreItem(node=node, vmid=vmid, type=kind, name=name)


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
    monkeypatch.setattr(
        security, "has_user_permission", lambda user, perm: perm in user["_perms"]
    )
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
def test_each_guest_rolls_back_to_its_own_snapshot(world: SimpleNamespace) -> None:
    """核心诉求：同一批里每台机器回到**自己**选的快照，不许串。"""
    world.client.snaps[201] = [
        {"name": "daily-1103", "parent": ""},
        {"name": "current", "parent": "daily-1103"},
    ]
    payload = SnapshotBulkRestore(
        items=[
            _item(vmid=101, kind="qemu", name="base"),
            _item(vmid=201, kind="lxc", name="daily-1103"),
        ]
    )

    result = run(
        vms_router.bulk_restore_snapshots(
            payload=payload, request=SimpleNamespace(), user=_user()
        )
    )

    assert result["action"] == "snapshot_restore"
    assert (result["total"], result["ok"], result["failed"]) == (2, 2, 0)
    # 名字与类型都按各自那条走（串名/串端点都会让这里挂）
    assert world.client.calls == [
        ("qemu", "pve1", 101, "base"),
        ("lxc", "pve1", 201, "daily-1103"),
    ]
    assert [row["name"] for row in result["results"]] == ["base", "daily-1103"]
    assert [row["type"] for row in result["results"]] == ["qemu", "lxc"]
    assert all(str(row["task"]).startswith("UPID:") for row in result["results"])
    # 还原会截断快照链：等任务结束再清一次缓存
    assert world.scheduled == [
        [
            "UPID:pve:1:1:1::pve1:101:qmrollback:base:",
            "UPID:pve:1:1:1::pve1:201:lxcrollback:daily-1103:",
        ]
    ]


def test_discarded_snapshots_lose_their_ownership_records(
    world: SimpleNamespace,
) -> None:
    """还原到 base 会丢掉 later：它的归属记录必须一起清，否则同名再建会认错人。"""
    payload = SnapshotBulkRestore(items=[_item(vmid=101, name="base")])

    result = run(
        vms_router.bulk_restore_snapshots(
            payload=payload, request=SimpleNamespace(), user=_user()
        )
    )

    assert result["ok"] == 1
    assert world.removed == ["conn-1|pve1|101|later"]


# -------------------------------------------------------------- 不存在 / 越权
def test_missing_snapshot_fails_only_that_item(world: SimpleNamespace) -> None:
    payload = SnapshotBulkRestore(
        items=[_item(vmid=101, name="nothing-here")]
    )

    result = run(
        vms_router.bulk_restore_snapshots(
            payload=payload, request=SimpleNamespace(), user=_user()
        )
    )

    assert result["failed"] == 1
    error = result["results"][0]["error"] or ""
    assert "pve1/101" in error and "nothing-here" in error
    # 没下发到 PVE，也就没有任务要等
    assert world.client.calls == []
    assert world.scheduled == []
    assert [a["result"] for a in world.audits] == ["failed"]


def test_missing_container_permission_fails_only_that_item(
    world: SimpleNamespace,
) -> None:
    payload = SnapshotBulkRestore(
        items=[
            _item(vmid=101, kind="qemu", name="base"),
            _item(vmid=201, kind="lxc", name="base"),
        ]
    )

    result = run(
        vms_router.bulk_restore_snapshots(
            payload=payload,
            request=SimpleNamespace(),
            user=_user(perms=("vm.snapshot",)),
        )
    )

    assert result["ok"] == 1
    assert result["failed"] == 1
    qemu_row, lxc_row = result["results"]
    assert qemu_row["ok"] is True
    assert "ct.snapshot" in (lxc_row["error"] or "")
    assert [call[0] for call in world.client.calls] == ["qemu"]


def test_snapshot_level_isolation_is_reported_per_item(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """普通用户还原别人的快照：整批照常，越权那条只失败自己。"""
    monkeypatch.setattr(security, "visible_owner", lambda user: "alice")

    async def fake_get_owner(kind: str, ref: str) -> str:
        return "bob"

    monkeypatch.setattr(ownership, "get_owner", fake_get_owner)

    result = run(
        vms_router.bulk_restore_snapshots(
            payload=SnapshotBulkRestore(items=[_item(vmid=101, name="base")]),
            request=SimpleNamespace(),
            user=_user(),
        )
    )

    assert result["failed"] == 1
    assert "不属于当前用户" in (result["results"][0]["error"] or "")
    assert world.client.calls == []
    assert world.removed == []


def test_access_denial_from_guard_does_not_break_the_batch(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def deny(*args, **kwargs):
        raise HTTPException(status_code=403, detail="快照已被保护")

    monkeypatch.setattr(vms_router, "_require_snapshot_access", deny)

    result = run(
        vms_router.bulk_restore_snapshots(
            payload=SnapshotBulkRestore(items=[_item()]),
            request=SimpleNamespace(),
            user=_user(),
        )
    )

    assert result["failed"] == 1
    assert result["results"][0]["error"] == "快照已被保护"
    assert world.client.calls == []


# ---------------------------------------------------------------- 失败隔离
def test_single_failure_does_not_abort_the_batch(world: SimpleNamespace) -> None:
    world.client.fail = True
    world.client.snaps[102] = [{"name": "base", "parent": ""}]
    payload = SnapshotBulkRestore(
        items=[_item(vmid=101, name="base"), _item(vmid=102, name="base")]
    )

    result = run(
        vms_router.bulk_restore_snapshots(
            payload=payload, request=SimpleNamespace(), user=_user()
        )
    )

    assert (result["ok"], result["failed"]) == (0, 2)
    assert all("locked" in (row["error"] or "") for row in result["results"])
    assert world.scheduled == []
    assert world.removed == []
    assert [a["result"] for a in world.audits] == ["failed", "failed"]


# ------------------------------------------------------- 快照链（会被丢弃的）
def test_discarded_names_walk_the_parent_chain() -> None:
    snaps = [
        {"name": "a", "parent": ""},
        {"name": "b", "parent": "a"},
        {"name": "c", "parent": "b"},
        {"name": "current", "parent": "c"},
    ]
    assert vms_router._discarded_snapshot_names(snaps, "a") == ["b", "c"]
    assert vms_router._discarded_snapshot_names(snaps, "b") == ["c"]
    # 还原到最新一份：后面没有可丢的
    assert vms_router._discarded_snapshot_names(snaps, "c") == []
    # 清单顺序不保证（PVE 不保证返回顺序），结果集合必须一致
    assert set(vms_router._discarded_snapshot_names(list(reversed(snaps)), "a")) == {
        "b",
        "c",
    }


# --------------------------------------------------------------- schema 校验
def test_schema_rejects_duplicate_guest() -> None:
    with pytest.raises(ValidationError) as exc:
        SnapshotBulkRestore(
            items=[_item(vmid=101, name="a"), _item(vmid=101, name="b")]
        )
    assert "只能指定一个快照" in str(exc.value)


def test_schema_rejects_current_and_empty_name() -> None:
    with pytest.raises(ValidationError):
        SnapshotBulkRestore(items=[_item(name="current")])
    with pytest.raises(ValidationError):
        SnapshotBulkRestore(items=[_item(name="   ")])


def test_schema_rejects_empty_batch() -> None:
    with pytest.raises(ValidationError):
        SnapshotBulkRestore(items=[])
