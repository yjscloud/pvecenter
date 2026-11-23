"""迁移的两条硬性约束：目标节点必须同集群、迁移后归属必须跟着机器走。

背景（两个都在真实多 PVE 部署里踩到过）：

1. 界面上的节点清单来自**合并**的 ``/nodes`` —— 多套彼此独立的 Proxmox 的节点
   出现在同一个下拉里。PVE 的迁移只能在同一个集群里换节点，选中另一套集群的
   节点只会在 PVE 侧报一句看不懂的错误；
2. 归属 ref 是 ``<连接>:<节点>:<vmid>``。迁移换了节点、记录没跟着走，这台机器在
   普通用户眼里就"消失"了：列表按当前节点精确比对、详情页 404、快照标记也失联。

接口层（真实 app + mock PVE：跨集群目标必须 400、迁移后归属搬到新节点、列表仍
认得出主人）在 ``tests/test_api_routes.py::TestGuestMigrationOwnership``。两者分开
是因为那里的 ``api`` fixture 会把全局连接池绑在 TestClient 的循环上，而本文件用
``asyncio.run`` 各建各的循环 —— 混在一个文件里会互相绊倒（见 conftest 的
``clean_mysql_db`` 与 ``test_owner_transfer._with_pool`` 的注释）。

    ../.venv/bin/python -m pytest tests/test_guest_migrate_ownership.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import migrateguard, ownership, store  # noqa: E402
from app.pve import ProxmoxError  # noqa: E402

CONN = "c1"


async def _with_pool(coro):
    from app import database

    try:
        return await coro
    finally:
        await database.close_pool()


def _run(coro):
    return asyncio.run(_with_pool(coro))


@pytest.fixture()
def db(clean_mysql_db):
    _run(ownership.init_table())
    _run(store.init_db())
    yield


# --------------------------------------------------------------- 目标节点校验
class _FakeClient:
    """只提供迁移校验要用的 ``nodes()``。"""

    def __init__(self, names: List[str]) -> None:
        self.names = names
        self.calls = 0

    async def nodes(self) -> List[Dict[str, Any]]:
        self.calls += 1
        return [{"node": name, "status": "online"} for name in self.names]


def test_target_on_the_same_cluster_is_accepted(monkeypatch) -> None:
    client = _FakeClient(["pve1", "pve2"])

    async def no_other_cluster(_node: str):
        return None

    monkeypatch.setattr(migrateguard, "_owner_cluster_of", no_other_cluster)

    assert _run(migrateguard.ensure_target_node(client, "pve1", "pve2")) == "pve2"


def test_target_of_another_cluster_is_rejected(monkeypatch) -> None:
    """另一套 PVE 的节点名：明确拒绝，并在文案里点出它是别处的节点。"""
    client = _FakeClient(["pve1", "pve2"])

    async def other_cluster(node: str):
        return "备机房-pve9" if node == "pve9" else None

    monkeypatch.setattr(migrateguard, "_owner_cluster_of", other_cluster)

    with pytest.raises(ProxmoxError) as excinfo:
        _run(migrateguard.ensure_target_node(client, "pve1", "pve9"))

    assert excinfo.value.status_code == 400
    assert "备机房-pve9" in excinfo.value.message
    assert "pve9" in excinfo.value.message


def test_unknown_target_node_is_rejected(monkeypatch) -> None:
    client = _FakeClient(["pve1"])

    async def no_other_cluster(_node: str):
        return None

    monkeypatch.setattr(migrateguard, "_owner_cluster_of", no_other_cluster)

    with pytest.raises(ProxmoxError) as excinfo:
        _run(migrateguard.ensure_target_node(client, "pve1", "ghost"))

    assert excinfo.value.status_code == 400
    assert "ghost" in excinfo.value.message


def test_migrating_onto_the_same_node_is_rejected(monkeypatch) -> None:
    """同节点不是"迁移"：PVE 会报错，前端也不该把当前节点列为目标。"""
    client = _FakeClient(["pve1"])

    async def no_other_cluster(_node: str):
        return None

    monkeypatch.setattr(migrateguard, "_owner_cluster_of", no_other_cluster)

    with pytest.raises(ProxmoxError) as excinfo:
        _run(migrateguard.ensure_target_node(client, "pve1", "pve1"))

    assert excinfo.value.status_code == 400
    assert client.calls == 0, "同节点的判定不该白打一次 PVE"


# ----------------------------------------------------------------- 归属跟随
def test_move_guest_keeps_owner_and_created_for_new_node(db) -> None:
    ref = ownership.vm_ref(CONN, "pve1", 100)
    snap = ownership.snapshot_ref(CONN, "pve1", 100, "before-upgrade")
    _run(ownership.set_owner(ownership.KIND_VM, ref, "alice"))
    _run(ownership.set_owner(ownership.KIND_SNAPSHOT, snap, "alice"))
    created = _run(_created_of(ownership.KIND_VM, ref))

    moved = _run(ownership.move_guest(CONN, "pve1", "pve2", 100))

    assert moved["guests_moved"] == 1
    assert moved["snapshots_moved"] == 1
    assert moved["dropped"] == 0
    # 人不变、创建时间不变，只是位置变了
    assert _run(ownership.get_owner(ownership.KIND_VM, ownership.vm_ref(CONN, "pve2", 100))) == "alice"
    assert _run(_created_of(ownership.KIND_VM, ownership.vm_ref(CONN, "pve2", 100))) == created
    # 旧位置不留残影（否则 VMID 回收时会错伤新机器）
    assert _run(ownership.get_owner(ownership.KIND_VM, ref)) is None
    # 快照标记跟着机器走：快照是机器的从属物
    assert (
        _run(
            ownership.get_owner(
                ownership.KIND_SNAPSHOT,
                ownership.snapshot_ref(CONN, "pve2", 100, "before-upgrade"),
            )
        )
        == "alice"
    )


def test_move_guest_handles_container_legacy_kind(db) -> None:
    """容器的归属在升级前记在 KIND_VM 下：迁移时一并跟到新节点。"""
    ref = ownership.vm_ref(CONN, "pve1", 105)
    _run(ownership.set_owner(ownership.KIND_VM, ref, "bob"))

    moved = _run(ownership.move_guest(CONN, "pve1", "pve2", 105))

    assert moved["guests_moved"] == 1
    assert (
        _run(ownership.get_owner_for(ownership.KIND_CT, ownership.vm_ref(CONN, "pve2", 105)))
        == "bob"
    )


def test_move_guest_drops_the_stale_duplicate(db) -> None:
    """新位置已经有一条记录（上一次迁移留下的）：保留它，不留两行。"""
    old = ownership.vm_ref(CONN, "pve1", 100)
    new = ownership.vm_ref(CONN, "pve2", 100)
    _run(ownership.set_owner(ownership.KIND_VM, old, "alice"))
    _run(ownership.set_owner(ownership.KIND_VM, new, "alice"))
    _created = _run(_created_of(ownership.KIND_VM, new))

    moved = _run(ownership.move_guest(CONN, "pve1", "pve2", 100))

    assert moved["dropped"] == 1
    assert _run(ownership.get_owner(ownership.KIND_VM, old)) is None
    assert _run(ownership.get_owner(ownership.KIND_VM, new)) == "alice"
    assert _run(_created_of(ownership.KIND_VM, new)) == _created


def test_move_guest_is_identity_based(db) -> None:
    """记录挂在更早的节点上（升级前迁过、没搬账）也能被纠正。"""
    stale = ownership.vm_ref(CONN, "pve0", 100)
    _run(ownership.set_owner(ownership.KIND_VM, stale, "alice"))

    _run(ownership.move_guest(CONN, "pve1", "pve2", 100))

    assert _run(ownership.get_owner(ownership.KIND_VM, stale)) is None
    assert _run(ownership.get_owner(ownership.KIND_VM, ownership.vm_ref(CONN, "pve2", 100))) == "alice"


def test_owner_lookup_survives_the_migration_window(db) -> None:
    """记录还在旧节点（迁移任务没跑完）时，按新节点也要认得出主人。"""
    _run(
        ownership.set_owner(
            ownership.KIND_VM, ownership.vm_ref(CONN, "pve1", 100), "alice"
        )
    )

    assert (
        _run(ownership.get_owner_for(ownership.KIND_VM, ownership.vm_ref(CONN, "pve2", 100)))
        == "alice"
    )
    # 快照回落到所属机器的归属时同样要认得出（VMID 在连接内唯一）
    owners = _run(ownership.owners_map(ownership.KIND_VM))
    assert (
        ownership.vm_owner_from(owners, "pve2", 100, CONN) == "alice"
    )


def test_owner_lookup_does_not_cross_clusters(db) -> None:
    """同名 VMID 在另一套集群上是别人的机器 —— 按 VMID 认领必须锁定连接。"""
    _run(
        ownership.set_owner(
            ownership.KIND_VM, ownership.vm_ref("c1", "pve1", 100), "alice"
        )
    )

    assert (
        _run(ownership.get_owner_for(ownership.KIND_VM, ownership.vm_ref("c2", "pve9", 100)))
        is None
    )


def test_owner_lookup_refuses_ambiguous_rows(db) -> None:
    """两个节点上各有一行、归属人不同：无法判定，返回 None 交给严格判定。"""
    _run(
        ownership.set_owner(
            ownership.KIND_VM, ownership.vm_ref(CONN, "pve1", 100), "alice"
        )
    )
    _run(
        ownership.set_owner(
            ownership.KIND_VM, ownership.vm_ref(CONN, "pve2", 100), "bob"
        )
    )

    assert (
        _run(ownership.get_owner_for(ownership.KIND_VM, ownership.vm_ref(CONN, "pve3", 100)))
        is None
    )


async def _created_of(kind: str, ref: str):
    from app import database

    async with database.connect() as db:
        cursor = await db.execute(
            "SELECT created FROM resource_owner WHERE kind = ? AND ref = ?", (kind, ref)
        )
        row = await cursor.fetchone()
    return row[0] if row else None
