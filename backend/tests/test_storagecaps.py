"""快照存储能力的判定口径（纯函数，不需要 PVE / 数据库）。

这类逻辑看着简单，但三种「说不清」的情况最容易出错，而且都不是能在界面上
一眼看出来的：

* 容器有**多块盘**（rootfs + mpN）：只查系统盘会放过「系统盘在 ZFS、数据盘挂在
  dir」的机器，PVE 会在复制到一半时失败；
* 存储**不在节点列表里**（刚被移除 / 共享存储未挂载）：不能当成「不支持」，
  否则一次 PVE 抖动就会把本来能建快照的用户挡在门外；
* 类型是**已知但不支持**（dir / NFS / 厚置备 LVM）：必须拦，并且要说清楚能用什么。

    ../.venv/bin/python -m pytest tests/test_storagecaps.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

import asyncio  # noqa: E402

from app import storagecaps  # noqa: E402
from app.pve import ProxmoxError  # noqa: E402


def _storage(name: str, stype: str, *, active: int = 1, shared: int = 0):
    return {
        "storage": name,
        "type": stype,
        "active": active,
        "shared": shared,
        "content": "rootdir,images",
    }


def test_zfs_rootfs_is_supported():
    cap = storagecaps.evaluate_snapshot_capability(
        {"rootfs": "local-zfs:subvol-102-disk-0,size=8G"},
        [_storage("local-zfs", "zfspool")],
    )

    assert cap["supported"] is True
    assert cap["blocking"] == []
    assert cap["unknown"] == []
    assert cap["volumes"][0]["type"] == "zfspool"
    assert cap["volumes"][0]["supports_snapshot"] is True
    assert cap["volumes"][0]["type_label"] == "ZFS"


def test_dir_rootfs_is_blocked_and_lists_alternatives():
    cap = storagecaps.evaluate_snapshot_capability(
        {"rootfs": "local:subvol-102-disk-0,size=8G"},
        [
            _storage("local", "dir"),
            _storage("local-zfs", "zfspool"),
            # 类型支持但当前离线：不该出现在「可用」清单里
            _storage("ceph-pool", "rbd", active=0),
        ],
    )

    assert cap["supported"] is False
    assert [item["storage"] for item in cap["blocking"]] == ["local"]
    assert cap["blocking"][0]["type_label"] == "dir"
    assert [item["storage"] for item in cap["available_storages"]] == ["local-zfs"]
    assert "LVM-thin" in cap["supported_type_labels"]


def test_any_volume_on_unsupported_storage_blocks():
    """系统盘在 ZFS、数据盘在 dir：整台机器仍然不能快照。"""
    cap = storagecaps.evaluate_snapshot_capability(
        {
            "rootfs": "local-zfs:subvol-102-disk-0,size=8G",
            "mp0": "backup-nfs:102/vm-102-disk-1.raw,mp=/data,size=32G",
            "unused0": "local:102/vm-102-disk-9.raw",
        },
        [
            _storage("local-zfs", "zfspool"),
            _storage("backup-nfs", "nfs"),
            _storage("local", "dir"),
        ],
    )

    assert cap["supported"] is False
    assert [item["key"] for item in cap["blocking"]] == ["mp0"]
    # unusedN 不参与快照，不能算进来，否则这台机器会被多余地拦一次
    assert {item["key"] for item in cap["volumes"]} == {"rootfs", "mp0"}


def test_unknown_storage_is_allowed_but_flagged():
    """存储清单读不到（或存储不在列表里）：放行，但标记为「无法确定」。"""
    cap = storagecaps.evaluate_snapshot_capability(
        {"rootfs": "gone-storage:subvol-102-disk-0,size=8G"},
        [],
    )

    assert cap["supported"] is True
    assert cap["blocking"] == []
    assert [item["storage"] for item in cap["unknown"]] == ["gone-storage"]
    assert cap["unknown"][0]["supports_snapshot"] is None
    assert cap["available_storages"] == []


def test_message_includes_storage_type_and_available_targets():
    cap = storagecaps.evaluate_snapshot_capability(
        {"rootfs": "local:subvol-102-disk-0,size=8G"},
        [_storage("local", "dir"), _storage("local-zfs", "zfspool")],
    )

    message = storagecaps.blocked_message(cap)
    assert "local（dir）" in message
    assert "ZFS" in message
    assert "local-zfs（ZFS）" in message


def test_message_without_any_target_offers_adding_storage():
    cap = storagecaps.evaluate_snapshot_capability(
        {"rootfs": "local:subvol-102-disk-0,size=8G"},
        [_storage("local", "dir")],
    )

    message = storagecaps.blocked_message(cap)
    assert "local（dir）" in message
    # 没有可用目标时不能给一份空清单，要明确「先去加存储」
    assert "没有可用的快照存储" in message


def test_zfs_over_iscsi_counts_as_snapshot_capable():
    """`zfs` 是 ZFS over iSCSI：底层仍是 ZFS 数据集，不能因为名字不同就误判。"""
    cap = storagecaps.evaluate_snapshot_capability(
        {"rootfs": "zfs-iscsi:subvol-102-disk-0,size=8G"},
        [_storage("zfs-iscsi", "zfs")],
    )

    assert cap["supported"] is True
    assert cap["volumes"][0]["type_label"] == "ZFS over iSCSI"


class _FakeClient:
    """只实现能力判定要用的两个读接口。"""

    def __init__(self, cfg, storages=None, storages_error=None):
        self._cfg = cfg
        self._storages = storages or []
        self._storages_error = storages_error

    async def lxc_config(self, node, vmid):
        return self._cfg

    async def storages(self, node):
        if self._storages_error is not None:
            raise self._storages_error
        return self._storages


def test_storage_list_failure_does_not_block():
    """读存储清单失败（PVE 抖动 / 共享存储未挂载）不能把用户挡在门外。"""
    client = _FakeClient(
        {"rootfs": "local:subvol-102-disk-0,size=8G"},
        storages_error=ProxmoxError("storage list unavailable", 503),
    )

    cap = asyncio.run(storagecaps.snapshot_capability(client, "pve", 102))

    assert cap["supported"] is True
    assert [item["storage"] for item in cap["unknown"]] == ["local"]


def test_config_read_failure_propagates():
    """连盘在哪都不知道时不能装作没事：让调用方按 PVE 的错误处理。"""

    class _BrokenClient(_FakeClient):
        async def lxc_config(self, node, vmid):
            raise ProxmoxError("configuration file not found", 500)

    try:
        asyncio.run(
            storagecaps.snapshot_capability(_BrokenClient({}), "pve", 999)
        )
    except ProxmoxError as exc:
        assert exc.status_code == 500
    else:  # pragma: no cover - 只为失败信息更清楚
        raise AssertionError("config read failure must propagate")
