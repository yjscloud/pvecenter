"""ostype 索引：PVE 的节点列表**不带** ostype，只能回落到每台的 config。

背景（实测 PVE 8.4 / 9.2）：``/cluster/resources``、``/nodes/{node}/qemu`` 与
``/nodes/{node}/lxc`` 都没有 ostype 字段，只有 ``vmid`` / ``name`` / 资源用量。
所以「一次节点请求换整节点 ostype」不成立 —— 容器列表整列没有系统图标的根因就
在这里。真正的来源是每台机器自己的 config（容器是 ``debian`` / ``alpine`` 这类
发行版，虚拟机是 ``l26`` / ``win11`` 大类）。

用例都是纯内存的假客户端，不碰 MySQL 与真实 PVE。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import guestostype, pvecache  # noqa: E402


class _FakeClient:
    """只回答节点列表与 config 的假客户端，并记录被问过哪些 vmid。"""

    def __init__(
        self,
        guests: Dict[str, List[Dict[str, Any]]],
        configs: Dict[Tuple[str, str, int], Dict[str, Any]],
        *,
        fail: Tuple[str, ...] = (),
    ) -> None:
        self.guests = guests
        self.configs = configs
        self.fail = set(fail)
        self.listed: List[Tuple[str, str]] = []
        self.read: List[int] = []

    async def guest_list(self, node: str, kind: str) -> List[Dict[str, Any]]:
        self.listed.append((node, kind))
        return self.guests.get(kind, [])

    async def _config(self, node: str, kind: str, vmid: int) -> Dict[str, Any]:
        self.read.append(vmid)
        if vmid in self.fail:
            raise RuntimeError("no such guest")
        return self.configs.get((node, kind, vmid), {})

    async def qemu_config(self, node: str, vmid: int) -> Dict[str, Any]:
        return await self._config(node, "qemu", vmid)

    async def lxc_config(self, node: str, vmid: int) -> Dict[str, Any]:
        return await self._config(node, "lxc", vmid)


def _reset() -> None:
    guestostype.clear()
    pvecache.invalidate()


def test_container_ostype_comes_from_config() -> None:
    """列表里没有 ostype 时读 config —— 容器的值是发行版，图标靠它。"""
    _reset()
    client = _FakeClient(
        {"lxc": [{"vmid": 103, "name": "lxc-01"}, {"vmid": 106, "name": "rest"}]},
        {
            ("pve9-2", "lxc", 103): {"ostype": "alpine"},
            ("pve9-2", "lxc", 106): {"ostype": "centos"},
        },
    )

    got = asyncio.run(guestostype.index(client, "c1", "pve9-2", "lxc"))

    assert got == {103: "alpine", 106: "centos"}
    assert sorted(client.read) == [103, 106]


def test_ostype_is_cached_between_polls() -> None:
    """第二次调用不再读 config：列表 10 秒一轮，不能让每轮都打 N 个请求。"""
    _reset()
    client = _FakeClient(
        {"qemu": [{"vmid": 100, "name": "web-01"}]},
        {("pve", "qemu", 100): {"ostype": "l26"}},
    )

    first = asyncio.run(guestostype.index(client, "c1", "pve", "qemu"))
    second = asyncio.run(guestostype.index(client, "c1", "pve", "qemu"))

    assert first == second == {100: "l26"}
    assert client.read == [100]


def test_one_unreadable_guest_does_not_blank_the_rest() -> None:
    """单台读失败只影响它自己；失败的那台不缓存，下一轮还能再试。"""
    _reset()
    client = _FakeClient(
        {"lxc": [{"vmid": 1, "name": "a"}, {"vmid": 2, "name": "b"}]},
        {("pve", "lxc", 2): {"ostype": "debian"}},
        fail=(1,),
    )

    assert asyncio.run(guestostype.index(client, "c1", "pve", "lxc")) == {2: "debian"}

    # 下一轮（整体缓存已过期）会重新问那台失败的机器
    pvecache.invalidate()
    client.fail = set()
    client.configs[("pve", "lxc", 1)] = {"ostype": "ubuntu"}
    assert asyncio.run(guestostype.index(client, "c1", "pve", "lxc")) == {
        1: "ubuntu",
        2: "debian",
    }


def test_reused_vmid_drops_the_old_ostype() -> None:
    """VMID 回收：同号容器换成另一种系统时，旧值不能贴到新容器上。"""
    _reset()
    client = _FakeClient(
        {"lxc": [{"vmid": 103, "name": "lxc-01"}]},
        {("pve", "lxc", 103): {"ostype": "alpine"}},
    )
    assert asyncio.run(guestostype.index(client, "c1", "pve", "lxc")) == {103: "alpine"}

    # 103 被删掉，列表里暂时没有它 —— 缓存必须跟着清
    client.guests = {"lxc": [{"vmid": 106, "name": "rest"}]}
    client.configs[("pve", "lxc", 106)] = {"ostype": "centos"}
    pvecache.invalidate()
    assert asyncio.run(guestostype.index(client, "c1", "pve", "lxc")) == {106: "centos"}

    # 同号的新容器是另一套系统：不能沿用 alpine
    client.guests = {"lxc": [{"vmid": 103, "name": "new"}]}
    client.configs[("pve", "lxc", 103)] = {"ostype": "centos"}
    pvecache.invalidate()
    assert asyncio.run(guestostype.index(client, "c1", "pve", "lxc")) == {103: "centos"}


def test_inline_ostype_from_list_is_used_without_extra_requests() -> None:
    """将来 PVE 若把 ostype 放进列表，就直接用 —— 不必多读一次 config。"""
    _reset()
    client = _FakeClient(
        {"qemu": [{"vmid": 100, "name": "web-01", "ostype": "win11"}]},
        {},
    )

    assert asyncio.run(guestostype.index(client, "c1", "pve", "qemu")) == {100: "win11"}
    assert client.read == []


def test_list_failure_returns_empty_instead_of_raising() -> None:
    """节点离线 / 权限不足时返回空表，让列表退回按名称判断。"""
    _reset()

    class _Broken(_FakeClient):
        async def guest_list(self, node: str, kind: str) -> List[Dict[str, Any]]:
            raise RuntimeError("node offline")

    assert asyncio.run(guestostype.index(_Broken({}, {}), "c1", "pve", "qemu")) == {}
