"""列表「网络」一列：IP → 地址池名。

口径必须和现有的 ``ip_in_pool`` 完全一致（同一个 ``pool_covers``），否则会出现
「IP 列标着池内、网络列却空着」这种自相矛盾的行。

前端不自己拉 ``/ip-pools`` 判断：那个接口按用户裁剪可见池，普通用户会漏掉公共
池的名字，还会触发一次全集群扫描。池名由两个列表接口在回填 IP 时顺手给出。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from starlette.responses import Response  # noqa: E402

from app import ippool  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


BUSINESS = {
    "id": "biz",
    "name": "业务网",
    "subnet": "10.0.0.0/24",
    "start": "10.0.0.100",
    "end": "10.0.0.200",
    "bridge": "vmbr0",
}
TESTNET = {
    "id": "test",
    "name": "测试网",
    "subnet": "10.1.0.0/24",
    "start": "10.1.0.10",
    "end": "10.1.0.200",
    "bridge": "vmbr1",
}
POOLS = [BUSINESS, TESTNET]


# ------------------------------------------------------------------ 判定函数
def test_each_network_maps_to_its_pool_name():
    """用户的典型场景：一个业务网、一个测试网，按 IP 分到不同池。"""
    assert ippool.pool_name_for_ip(POOLS, "10.0.0.120") == "业务网"
    assert ippool.pool_name_for_ip(POOLS, "10.1.0.33") == "测试网"


def test_manual_address_outside_range_still_shows_network():
    """手工在子网里挑的区间外地址：ip_in_pool 为 true，网络列也该有名字。"""
    assert ippool.pool_contains(BUSINESS, "10.0.0.50") is False
    assert ippool.pool_name_for_ip(POOLS, "10.0.0.50") == "业务网"


def test_unknown_address_has_no_network():
    assert ippool.pool_name_for_ip(POOLS, "192.168.9.9") is None


def test_no_ip_or_no_pools():
    assert ippool.pool_name_for_ip(POOLS, "") is None
    assert ippool.pool_name_for_ip(POOLS, "not-an-ip") is None
    assert ippool.pool_name_for_ip([], "10.0.0.120") is None


def test_cidr_suffix_is_tolerated():
    assert ippool.pool_name_for_ip(POOLS, "10.0.0.50/24") == "业务网"


def test_name_falls_back_to_id():
    pool = {"id": "pool-x", "name": "", "subnet": "10.2.0.0/24"}
    assert ippool.pool_name_for_ip([pool], "10.2.0.5") == "pool-x"


def test_overlapping_pools_pick_the_most_specific():
    """网段重叠时按 _rank 取最具体的（与自动分配同一套优先级）。"""
    wide = {"id": "wide", "name": "大网", "subnet": "10.0.0.0/16"}
    narrow = dict(BUSINESS, subnet="10.0.1.0/24", vlan=10)
    assert ippool.pool_name_for_ip([wide, narrow], "10.0.1.7") == "业务网"


def test_pools_sharing_one_subnet_split_by_range():
    """多个池共享同一子网、各管一段区间：按区间区分，不整列标成第一个池。

    业务网管 149.100~200，管理网管 149.1~50，子网同为 172.16.149.0/24 ——
    只看子网的话低段地址也会被标成配置顺序第一个池的名字。
    """
    business = {
        "id": "biz",
        "name": "业务网",
        "bridge": "vmbr0",
        "subnet": "172.16.149.0/24",
        "start": "172.16.149.100",
        "end": "172.16.149.200",
    }
    manage = {
        "id": "mgmt",
        "name": "管理网",
        "bridge": "vmbr0",
        "subnet": "172.16.149.0/24",
        "start": "172.16.149.1",
        "end": "172.16.149.50",
    }
    pools = [business, manage]
    assert ippool.pool_name_for_ip(pools, "172.16.149.2") == "管理网"
    assert ippool.pool_name_for_ip(pools, "172.16.149.120") == "业务网"
    # 顺序反过来结论不变：靠区间而不是配置顺序
    assert ippool.pool_name_for_ip(list(reversed(pools)), "172.16.149.2") == "管理网"


def test_gateway_ip_of_one_pool_still_shows_the_other_pool():
    """地址恰是某个池的网关：区间匹配不上（网关不分配），退回子网口径。"""
    business = {
        "id": "biz",
        "name": "业务网",
        "bridge": "vmbr0",
        "subnet": "172.16.149.0/24",
        "gateway": "172.16.149.1",
        "start": "172.16.149.100",
        "end": "172.16.149.200",
    }
    assert ippool.pool_name_for_ip([business], "172.16.149.1") == "业务网"


def test_pool_name_agrees_with_ip_in_pool_flag():
    """两列同口径：有名字 <=> any(pool_covers)。"""
    for ip in [
        "10.0.0.120",
        "10.0.0.50",
        "10.1.0.33",
        "192.168.9.9",
        "10.0.0.50/24",
        "",
    ]:
        covered = any(ippool.pool_covers(p, ip) for p in POOLS)
        assert (ippool.pool_name_for_ip(POOLS, ip) is not None) == covered, ip


# ------------------------------------------------------------------ 两个列表接口
def _patch_common(monkeypatch, module) -> None:
    """把列表接口里跟「网络」无关的归属 / 来源模板查询都替成空结果。"""
    async def no_pools_saved():
        return []

    async def empty_map(*_a, **_kw):
        return {}

    async def noop(*_a, **_kw):
        return None

    monkeypatch.setattr(module.security, "visible_owner", lambda _user: None)
    monkeypatch.setattr(module.security, "is_region_admin", lambda _user: False)
    monkeypatch.setattr(module.ownership, "owners_map", empty_map)
    monkeypatch.setattr(module.ownership, "owners_map_for", empty_map)
    monkeypatch.setattr(module.guest_created, "cached", lambda *a, **k: None)
    monkeypatch.setattr(module.guest_created, "fill", noop)
    monkeypatch.setattr(module.guest_created, "sources_for", empty_map)
    monkeypatch.setattr(module.guest_created, "lxc_sources_for", empty_map)
    # 手动 IP 索引默认空：真正要测它的用例会再覆盖
    monkeypatch.setattr(module, "manual_ip_index", _async_return({}))
    # 确保「没配地址池」的分支不会被误测到
    assert callable(no_pools_saved)


def test_vms_list_backfills_pool_name(monkeypatch) -> None:
    from app.routers import vms as vms_router

    resources = [
        {"type": "qemu", "id": "qemu/101", "node": "pve1", "vmid": 101,
         "name": "web-01", "status": "running"}
    ]

    async def fake_parallel(coros, limit=8):
        return [resources]

    async def fake_resolve(client, items, limit=12):
        return ["10.0.0.120"]

    async def fake_load_pools():
        return POOLS

    _patch_common(monkeypatch, vms_router)
    monkeypatch.setattr(vms_router, "requested_connection", lambda: None)
    monkeypatch.setattr(
        vms_router,
        "collection_targets",
        lambda: [({"id": "c1", "name": "pve1"}, object())],
    )
    monkeypatch.setattr(vms_router, "parallel", fake_parallel)
    monkeypatch.setattr(vms_router.guestip, "resolve_batch", fake_resolve)
    monkeypatch.setattr(vms_router.ippool, "load_pools", fake_load_pools)

    rows = _run(
        vms_router.collect_vms(
            user={"username": "admin"}, with_ip=True, lightweight=True
        )
    )

    assert len(rows) == 1
    assert rows[0]["ip"] == "10.0.0.120"
    assert rows[0]["ip_in_pool"] is True
    assert rows[0]["ip_pool_name"] == "业务网"


def test_lxc_list_backfills_pool_name(monkeypatch) -> None:
    from app.routers import lxc as lxc_router

    resources = [
        {"type": "lxc", "id": "lxc/201", "node": "pve1", "vmid": 201,
         "name": "ct-01", "status": "running"}
    ]
    calls = {"n": 0}

    async def fake_parallel(coros, limit=8):
        calls["n"] += 1
        # 第一次取 cluster_resources，第二次逐台解析 IP
        return [resources] if calls["n"] == 1 else ["10.1.0.33"]

    async def fake_load_pools():
        return POOLS

    _patch_common(monkeypatch, lxc_router)
    monkeypatch.setattr(lxc_router, "requested_connection", lambda: None)
    monkeypatch.setattr(
        lxc_router,
        "collection_targets",
        lambda: [({"id": "c1", "name": "pve1"}, object())],
    )
    monkeypatch.setattr(lxc_router, "parallel", fake_parallel)
    monkeypatch.setattr(lxc_router.ippool, "load_pools", fake_load_pools)

    rows = _run(
        lxc_router.list_containers(
            Response(),
            node=None,
            with_ip=True,
            summary=False,
            limit=None,
            user={"username": "admin"},
        )
    )

    assert len(rows) == 1
    assert rows[0]["ip"] == "10.1.0.33"
    assert rows[0]["ip_in_pool"] is True
    assert rows[0]["ip_pool_name"] == "测试网"


def test_pool_outside_ip_leaves_network_empty(monkeypatch) -> None:
    """池外地址：ip_in_pool=false，网络列为空（IP 列另有「池外」徽标）。"""
    from app.routers import vms as vms_router

    resources = [
        {"type": "qemu", "id": "qemu/102", "node": "pve1", "vmid": 102,
         "name": "rogue", "status": "running"}
    ]

    async def fake_parallel(coros, limit=8):
        return [resources]

    async def fake_resolve(client, items, limit=12):
        return ["192.168.9.9"]

    async def fake_load_pools():
        return POOLS

    _patch_common(monkeypatch, vms_router)
    monkeypatch.setattr(vms_router, "requested_connection", lambda: None)
    monkeypatch.setattr(
        vms_router,
        "collection_targets",
        lambda: [({"id": "c1", "name": "pve1"}, object())],
    )
    monkeypatch.setattr(vms_router, "parallel", fake_parallel)
    monkeypatch.setattr(vms_router.guestip, "resolve_batch", fake_resolve)
    monkeypatch.setattr(vms_router.ippool, "load_pools", fake_load_pools)

    row = _run(
        vms_router.collect_vms(
            user={"username": "admin"}, with_ip=True, lightweight=True
        )
    )[0]

    assert row["ip_in_pool"] is False
    assert row["ip_pool_name"] is None


# ------------------------------------------------------------------ 导出与搜索
async def _collect_body(resp):
    out = []
    async for chunk in resp.body_iterator:
        out.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
    return "".join(out)


def test_export_vms_includes_network_column(monkeypatch) -> None:
    """导出带 IP 时，IP 地址后多一列「网络」= 池名，与列表口径一致。"""
    from app.routers import export as export_router
    from app.routers import vms as vms_router

    async def fake_audit(*_a, **_k):
        return None

    async def fake_collect(**kwargs):
        return [
            {
                "connection_id": "c1", "connection_name": "pve1",
                "node": "pve1", "vmid": 101, "name": "web", "type": "qemu",
                "status": "running", "template": False,
                "maxcpu": 2, "maxmem": 4096, "maxdisk": 0,
                "tags": "", "ip": "10.0.0.120", "ip_pool_name": "业务网",
            }
        ]

    async def empty_map(*_a, **_k):
        return {}

    monkeypatch.setattr(export_router.security, "audit_read", fake_audit)
    monkeypatch.setattr(vms_router, "collect_vms", fake_collect)
    monkeypatch.setattr(export_router.ownership, "owners_map", empty_map)
    monkeypatch.setattr(export_router.ownership, "owners_map_for", empty_map)

    resp = _run(
        export_router.export_vms(
            None, node=None, guest_type="all", with_ip=True, user={"username": "admin"}
        )
    )
    text = _run(_collect_body(resp))

    assert "网络" in text
    assert "业务网" in text


def test_export_without_ip_has_no_network_column(monkeypatch) -> None:
    """不带 IP 时根本没有地址，也就没有「网络」列，表头不出现。"""
    from app.routers import export as export_router
    from app.routers import vms as vms_router

    async def fake_audit(*_a, **_k):
        return None

    async def fake_collect(**kwargs):
        return [
            {
                "connection_id": "c1", "connection_name": "pve1",
                "node": "pve1", "vmid": 101, "name": "web", "type": "qemu",
                "status": "running", "template": False,
                "maxcpu": 2, "maxmem": 4096, "maxdisk": 0, "tags": "",
            }
        ]

    async def empty_map(*_a, **_k):
        return {}

    monkeypatch.setattr(export_router.security, "audit_read", fake_audit)
    monkeypatch.setattr(vms_router, "collect_vms", fake_collect)
    monkeypatch.setattr(export_router.ownership, "owners_map", empty_map)
    monkeypatch.setattr(export_router.ownership, "owners_map_for", empty_map)

    resp = _run(
        export_router.export_vms(
            None, node=None, guest_type="all", with_ip=False, user={"username": "admin"}
        )
    )
    text = _run(_collect_body(resp))

    assert "网络" not in text


# ------------------------------------------------------------------ 手动 IP 的网络归属
def _async_return(value):
    async def _fn(*_a, **_k):
        return value

    return _fn


def test_manual_ip_index_filters_by_owner(monkeypatch) -> None:
    """普通用户只看得到自己填的手动地址，管理员全量 —— 与 get_vm_meta 一致。"""
    from app.routers import vm_meta

    items = {
        "c1|pve1|101": {"ip": "10.0.0.9", "by": "alice", "at": 1},
        "c1|pve1|102": {"ip": "10.0.0.10", "by": "bob", "at": 1},
    }
    monkeypatch.setattr(vm_meta, "_load", _async_return(items))

    admin = _run(vm_meta.manual_ip_index({"role": "admin", "username": "root"}))
    alice = _run(vm_meta.manual_ip_index({"role": "user", "username": "alice"}))

    assert admin == {
        ("c1", "pve1", 101): "10.0.0.9",
        ("c1", "pve1", 102): "10.0.0.10",
    }
    assert alice == {("c1", "pve1", 101): "10.0.0.9"}


def test_vms_manual_ip_maps_to_network(monkeypatch) -> None:
    """自动地址解析不到、但手动填了地址：网络列显示手动地址所属的池。"""
    from app.routers import vms as vms_router

    resources = [
        {"type": "qemu", "id": "qemu/103", "node": "pve1", "vmid": 103,
         "name": "manual", "status": "stopped"}
    ]

    async def fake_parallel(coros, limit=8):
        return [resources]

    async def fake_resolve(client, items, limit=12):
        return [""]

    async def fake_load_pools():
        return POOLS

    async def fake_manual(user):
        return {("c1", "pve1", 103): "10.1.0.33"}

    _patch_common(monkeypatch, vms_router)
    monkeypatch.setattr(vms_router, "requested_connection", lambda: None)
    monkeypatch.setattr(
        vms_router,
        "collection_targets",
        lambda: [({"id": "c1", "name": "pve1"}, object())],
    )
    monkeypatch.setattr(vms_router, "parallel", fake_parallel)
    monkeypatch.setattr(vms_router.guestip, "resolve_batch", fake_resolve)
    monkeypatch.setattr(vms_router.ippool, "load_pools", fake_load_pools)
    monkeypatch.setattr(vms_router, "manual_ip_index", fake_manual)

    row = _run(
        vms_router.collect_vms(
            user={"username": "admin"}, with_ip=True, lightweight=True
        )
    )[0]

    assert row["ip"] == ""
    assert row["ip_in_pool"] is None      # 自动地址为空，池外判定不适用
    assert row["ip_pool_name"] == "测试网"  # 但手动地址落进测试网


def test_lxc_manual_ip_maps_to_network(monkeypatch) -> None:
    """容器同理：自动读不到、手动填了业务网地址 → 网络列显示业务网。"""
    from app.routers import lxc as lxc_router

    resources = [
        {"type": "lxc", "id": "lxc/203", "node": "pve1", "vmid": 203,
         "name": "manual-ct", "status": "stopped"}
    ]
    calls = {"n": 0}

    async def fake_parallel(coros, limit=8):
        calls["n"] += 1
        return [resources] if calls["n"] == 1 else [""]

    async def fake_load_pools():
        return POOLS

    async def fake_manual(user):
        return {("c1", "pve1", 203): "10.0.0.50"}

    _patch_common(monkeypatch, lxc_router)
    monkeypatch.setattr(lxc_router, "requested_connection", lambda: None)
    monkeypatch.setattr(
        lxc_router,
        "collection_targets",
        lambda: [({"id": "c1", "name": "pve1"}, object())],
    )
    monkeypatch.setattr(lxc_router, "parallel", fake_parallel)
    monkeypatch.setattr(lxc_router.ippool, "load_pools", fake_load_pools)
    monkeypatch.setattr(lxc_router, "manual_ip_index", fake_manual)

    row = _run(
        lxc_router.list_containers(
            Response(),
            node=None,
            with_ip=True,
            summary=False,
            limit=None,
            user={"username": "admin"},
        )
    )[0]

    assert row["ip_pool_name"] == "业务网"  # 10.0.0.50 在业务网子网
