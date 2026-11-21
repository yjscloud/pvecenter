"""跨节点克隆的前置检查（``app.cloneguard``）。

这套规则决定两件事：
  * 模板列表里「别节点的模板」能不能出现在候选里（前端按同一份数据判定）；
  * 真要跨节点克隆时，是当场给一条点名存储的报错，还是丢给 PVE 在后台任务里失败。

所以这里逐条钉住：卷解析（哪些配置键算卷）、缺失 vs 本地、以及读不到配置时
必须放行（让 PVE 自己报「不存在」，而不是编一句「磁盘不在共享存储」）。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

import pytest  # noqa: E402

from app import cloneguard, pvecache  # noqa: E402
from app.pve import ProxmoxError  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_cache():
    """节点存储清单是进程级缓存：用例之间不能互相命中。"""
    pvecache.invalidate("storages:nodes:")
    yield
    pvecache.invalidate("storages:nodes:")


# ------------------------------------------------------------------ 卷解析
def test_qemu_volume_storages_cover_disks_and_efi() -> None:
    cfg = {
        "scsi0": "ceph-ssd:vm-101-disk-0,size=32G",
        "scsi1": "ceph-ssd:vm-101-disk-1,size=100G",
        "virtio2": "local-lvm:vm-101-disk-2,size=8G",
        # EFI 盘也是一个卷：落在本地存储上一样跨不过去
        "efidisk0": "local-lvm:vm-101-disk-3,size=4M",
        # 光驱 / cloud-init / 卸载下来的残留卷都不是要克隆的盘
        "ide2": "none,media=cdrom",
        "ide0": "ceph-ssd:vm-101-cloudinit,media=cdrom",
        "unused0": "local-lvm:vm-101-disk-4",
        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
        "ostype": "l26",
    }
    assert cloneguard.config_volume_storages(cfg, "qemu") == [
        "ceph-ssd",
        "local-lvm",
    ]


def test_lxc_volume_storages_include_rootfs_and_mounts() -> None:
    cfg = {
        "rootfs": "local-lvm:vm-101-disk-0,size=8G",
        "mp0": "ceph-ssd:vm-101-disk-1,size=20G",
        "mp10": "ceph-ssd:vm-101-disk-2,size=5G",
        # 空值与非法写法不该被当成卷
        "mp1": "",
        "mp2": "no-colon-here",
    }
    assert cloneguard.config_volume_storages(cfg, "lxc") == [
        "local-lvm",
        "ceph-ssd",
    ]


def test_volume_blockers_split_missing_and_local() -> None:
    storages = [
        {"storage": "ceph-ssd", "shared": 1},
        {"storage": "local-lvm", "shared": 0},
    ]
    missing, local = cloneguard._volume_blockers(
        ["ceph-ssd", "local-lvm", "nfs-old"], storages
    )
    assert missing == ["nfs-old"]
    assert local == ["local-lvm"]


def test_describe_blockers_names_storage_and_node() -> None:
    text = cloneguard.describe_blockers(["ceph-ssd"], [], "node03")
    assert "ceph-ssd" in text and "node03" in text
    # 两种原因同时出现时要都讲清楚，别只报一半
    both = cloneguard.describe_blockers(["ceph-ssd"], ["local-lvm"], "node03")
    assert "ceph-ssd" in both and "local-lvm" in both


# ------------------------------------------------------------------ 准入检查
class _FakeClient:
    """按需返回模板配置与目标节点存储清单，并记下被调用了几次。"""

    def __init__(self, cfg=None, storages=None, raise_config: bool = False):
        self.cfg = cfg or {}
        self._storages = storages if storages is not None else []
        self.raise_config = raise_config
        self.config_calls: list[tuple[str, int]] = []
        self.storage_calls: list[str] = []

    async def qemu_config(self, node: str, vmid: int):
        self.config_calls.append((node, vmid))
        if self.raise_config:
            raise ProxmoxError("config not found", status_code=500)
        return self.cfg

    async def lxc_config(self, node: str, vmid: int):
        return await self.qemu_config(node, vmid)

    async def storages(self, node: str):
        self.storage_calls.append(node)
        return self._storages


def _ensure(client, **kwargs):
    return asyncio.run(cloneguard.ensure_cross_node_clone(client, **kwargs))


def test_same_node_clone_skips_all_lookups() -> None:
    client = _FakeClient()
    _ensure(
        client, kind="qemu", source_node="node01", vmid=101, target_node="node01"
    )
    assert client.config_calls == []
    assert client.storage_calls == []


def test_cross_node_on_shared_storage_passes() -> None:
    client = _FakeClient(
        cfg={"scsi0": "ceph-ssd:vm-101-disk-0,size=32G"},
        storages=[
            {"storage": "ceph-ssd", "shared": 1},
            {"storage": "local-lvm", "shared": 0},
        ],
    )
    _ensure(
        client, kind="qemu", source_node="node01", vmid=101, target_node="node03",
        connection_id="c1",
    )
    assert client.config_calls == [("node01", 101)]
    assert client.storage_calls == ["node03"]


def test_cross_node_rejects_storage_missing_on_target() -> None:
    """模板盘在共享存储上，但目标节点没挂载 —— 这正是用户看到「模板不存在」的场景。"""
    client = _FakeClient(
        cfg={"scsi0": "ceph-ssd:vm-101-disk-0,size=32G"},
        storages=[{"storage": "local", "shared": 0}],
    )
    with pytest.raises(ProxmoxError) as excinfo:
        _ensure(
            client, kind="qemu", source_node="node01", vmid=101,
            target_node="node03", connection_id="c2",
        )
    assert excinfo.value.status_code == 400
    assert "ceph-ssd" in excinfo.value.message
    assert "node03" in excinfo.value.message


def test_cross_node_rejects_local_disk_template() -> None:
    client = _FakeClient(
        cfg={"scsi0": "local-lvm:vm-101-disk-0,size=32G"},
        storages=[{"storage": "local-lvm", "shared": 0}],
    )
    with pytest.raises(ProxmoxError) as excinfo:
        _ensure(
            client, kind="qemu", source_node="node01", vmid=101,
            target_node="node03", connection_id="c3",
        )
    assert "本地存储" in excinfo.value.message


def test_unreadable_config_does_not_block() -> None:
    """模板刚被删 / 节点离线：让 PVE 自己报，别在这里编一句存储问题。"""
    client = _FakeClient(raise_config=True)
    _ensure(
        client, kind="qemu", source_node="node01", vmid=101, target_node="node03",
        connection_id="c4",
    )
    assert client.storage_calls == []


# ------------------------------------------------- /api/templates 的 storages 字段
class _TemplateClient:
    """只够 ``list_templates`` 用：一条 cluster/resources + 按 vmid 给 config。"""

    def __init__(self, configs):
        self.configs = configs

    async def cluster_resources(self, rtype=None):
        return [
            {
                "type": "qemu",
                "node": "node01",
                "vmid": 101,
                "name": "ubuntu24",
                "template": 1,
                "status": "stopped",
            }
        ]

    async def get(self, path: str):
        for vmid, cfg in self.configs.items():
            if path.endswith(f"/{vmid}/config"):
                return cfg
        raise ProxmoxError("not found", status_code=500)


def _list_templates(client):
    from app.routers import templates as templates_router

    original = templates_router.collection_targets
    templates_router.collection_targets = lambda: [({"id": "c1", "name": "PVE"}, client)]
    try:
        return asyncio.run(
            templates_router.list_templates(guest_type="qemu", user={"username": "root"})
        )
    finally:
        templates_router.collection_targets = original


def test_template_listing_exposes_disk_storages() -> None:
    """前端靠这一项判断「目标节点挂不挂得到这块共享存储」，不能丢。"""
    pvecache.invalidate("templates:meta:")
    client = _TemplateClient(
        {101: {"ostype": "l26", "scsi0": "ceph-ssd:vm-101-disk-0,size=32G"}}
    )
    rows = _list_templates(client)
    assert len(rows) == 1
    assert rows[0]["ostype"] == "l26"
    assert rows[0]["storages"] == ["ceph-ssd"]


def test_template_listing_keeps_row_when_config_unreadable() -> None:
    """读不到 config 只丢 ostype / storages，不能让整份列表失败或报错。"""
    pvecache.invalidate("templates:meta:")
    rows = _list_templates(_TemplateClient({}))
    assert len(rows) == 1
    assert rows[0]["storages"] == []
    assert rows[0]["ostype"] == ""
