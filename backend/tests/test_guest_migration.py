"""迁移 / 隔离 / 快照之后，「来源模板」与面板侧记录不能丢。

机器换了节点（迁移），或者短暂从资源清单里消失（迁移上锁、隔离关机、快照冻结），
面板侧这些数据都按 ``连接|节点|VMID`` 存着 —— 节点一变就成了孤儿：

* 创建时间记录（``guest_created_record``）查不到 → 列表退回 PVE 的 ``meta.ctime``，
  而克隆 / 恢复出来的机器那份 meta 是模板带过来的（显示的是模板的时间）；
* 隔离登记查不到 → 「隔离」标签消失，删除守卫也失效；
* 审计里的克隆来源也按 ``节点/VMID`` 索引，迁移之后对不上 → 「操作系统」列变「—」。

本文件锁的就是这四条：解析要能跨节点认领、登记要按「连接 + VMID」识别、
列表清理要有宽限窗口（不能把「这一轮没看到」当成「已删除」）。

    ../.venv/bin/python -m pytest tests/test_guest_migration.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import guest_created, quarantine_store  # noqa: E402
from test_api_routes import api, auth_headers  # noqa: E402,F401

CREATED = 1789814562  # 实测本机 PVE 里一个真实 ctime


def test_resolve_claims_record_written_before_migration(monkeypatch) -> None:
    """记录写在 pve1/100，机器已经迁到 pve2 —— 详情页要认领并挪到新节点。"""
    moved: List[Any] = []

    async def fake_records(_conn: str) -> Dict[str, int]:
        return {"|pve1|100|qemu": CREATED}

    async def fake_move(conn, old_node, new_node, vmid, guest_type) -> None:
        moved.append((conn, old_node, new_node, vmid, guest_type))

    monkeypatch.setattr(guest_created, "_records_for", fake_records)
    monkeypatch.setattr(guest_created, "_move_record", fake_move)
    guest_created.clear()

    value = asyncio.run(guest_created.resolve("", "pve2", 100, "qemu", {}))

    assert value == CREATED
    assert moved == [("", "pve1", "pve2", 100, "qemu")]


def test_fill_claims_migrated_record_and_sweeps_with_grace(monkeypatch) -> None:
    """列表：认领旧节点那一行；同时清理必须带宽限窗口。

    「这一轮列表里没看到」≠「机器已被删除」：迁移 / 隔离 / 快照的瞬间都会让一台
    还活着的机器缺席一轮清单。真正的删除有显式钩子（删除接口 / 批量删除）。
    """
    moved: List[Any] = []
    sweeps: Dict[str, Any] = {}

    async def fake_records(_conn: str) -> Dict[str, int]:
        # 100 记录在旧节点；999 是一条真正过期的残留
        return {"|pve1|100|qemu": CREATED, "|pve1|999|qemu": CREATED}

    async def fake_move(conn, old_node, new_node, vmid, guest_type) -> None:
        moved.append((old_node, new_node, vmid))

    async def fake_drop(conn, node, guest_type, vmids, *, older_than) -> None:
        sweeps["args"] = (conn, node, guest_type, vmids)
        sweeps["older_than"] = older_than

    async def fake_fetch(_client, group, _key) -> None:
        return None

    monkeypatch.setattr(guest_created, "_records_for", fake_records)
    monkeypatch.setattr(guest_created, "_move_record", fake_move)
    monkeypatch.setattr(guest_created, "_drop_stale_records", fake_drop)
    monkeypatch.setattr(guest_created, "_fetch", fake_fetch)
    guest_created.clear()

    items: List[Dict[str, Any]] = [
        # 迁移后的机器（记录还在 pve1 名下）
        {"connection_id": "", "node": "pve2", "vmid": 100, "type": "qemu"},
        # 旧节点上还剩一台：只有这一轮真的看到了 pve1，清理才会作用于那个范围
        {"connection_id": "", "node": "pve1", "vmid": 101, "type": "qemu"},
    ]
    before = time.time()
    asyncio.run(guest_created.fill(None, items))  # type: ignore[arg-type]

    # 认领：pve2/100 拿到了旧节点那条记录，并把行挪了过去
    assert items[0]["created"] == CREATED
    assert moved == [("pve1", "pve2", 100)]
    # 清理只作用于「这一轮真的看到了」的那个范围（pve1），活的 VMID 是 101，
    # 于是 999 那条残留才会被清；同时只清「够老」的行
    assert sweeps["args"][:3] == ("", "pve1", "qemu")
    assert sweeps["args"][3] == [101]
    assert sweeps["older_than"] <= time.time() - guest_created.SWEEP_GRACE_SECONDS + 5
    assert sweeps["older_than"] >= before - guest_created.SWEEP_GRACE_SECONDS - 5


def test_source_template_follows_vm_to_another_node(api, monkeypatch) -> None:
    """克隆来源写在 pve1/100，机器迁到 pve2 —— 列表仍要认出来。

    审计的 ``target_guest`` 是「节点/VMID」，迁移不会改写历史记录；只按当前节点
    查，列表的「操作系统」列会突然变成「—」。
    """
    from app import security

    async def seed() -> None:
        await security.audit(
            None,
            {"username": "admin"},
            "vm.clone",
            target="pve1/9000 -> pve1/100",
            result="success",
        )

    api.portal.call(seed)

    async def fake_map():
        # 一条连接上两台节点（测试环境的假 PVE 只有 pve1）
        return ({"pve1": 1, "pve2": 1}, {"": {"pve1", "pve2"}})

    monkeypatch.setattr(guest_created, "_node_map", fake_map)

    async def resolve() -> Dict[Any, Any]:
        return await guest_created.sources_for(
            [{"connection_id": "", "node": "pve2", "vmid": 100}]
        )

    sources = api.portal.call(resolve)
    assert sources[("", "pve2", 100)] == {"node": "pve1", "vmid": 9000}


def test_container_source_follows_ct_to_another_node(api, monkeypatch) -> None:
    """容器同理：``ct.create`` 的模板文件记录留在旧节点名下。"""
    from app import security

    async def seed() -> None:
        await security.audit(
            None,
            {"username": "admin"},
            "ct.create",
            target="pve1/105",
            detail={"ostemplate": "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"},
        )

    api.portal.call(seed)

    async def fake_map():
        return ({"pve1": 1, "pve2": 1}, {"": {"pve1", "pve2"}})

    monkeypatch.setattr(guest_created, "_node_map", fake_map)

    async def resolve() -> Dict[Any, Any]:
        return await guest_created.lxc_sources_for(
            [{"connection_id": "", "node": "pve2", "vmid": 105}]
        )

    sources = api.portal.call(resolve)
    assert sources[("", "pve2", 105)] == {
        "file": "debian-12-standard_12.7-1_amd64.tar.zst"
    }


def test_quarantine_registry_is_identity_based(api) -> None:
    """隔离登记按「连接 + VMID」识别：迁移后标签与删除守卫都还在。"""

    async def run() -> None:
        await quarantine_store.record(
            conn_id="", node="pve1", vmid=100, name="demo", interfaces=["net0"]
        )
        index = await quarantine_store.index()
        # 列表打标签只看「连接 + VMID」，节点不是身份的一部分
        assert ("", 100) in index

        # 迁移之后再查：按新节点也要查得到（删除守卫靠这个拦住「删掉证据」）
        record = await quarantine_store.get("", "pve2", 100)
        assert record is not None and record["name"] == "demo"

        # 面板在迁移后重新登记：旧节点那一行必须被收掉，不能留两条
        await quarantine_store.record(
            conn_id="", node="pve2", vmid=100, name="demo", interfaces=["net0"]
        )
        rows = [r for r in await quarantine_store.list_records() if r["vmid"] == 100]
        assert [r["node"] for r in rows] == ["pve2"]

        # 解除隔离按「连接 + VMID」删：节点过期也清得掉
        await quarantine_store.drop("", "pve1", 100)
        assert ("", 100) not in await quarantine_store.index()

    api.portal.call(run)
