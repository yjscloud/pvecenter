"""恢复备份的「目标 VMID 撞号」判定。

PVE 里虚拟机和容器**共用同一套 VMID 编号**，于是恢复目标被占时有两种后果：
同类型占用可以靠 ``force`` 覆盖，另一种类型占用则根本覆盖不了 —— PVE 只回
一句英文的 ``CT 103 already exists on node 'pve9-2'``，用户读不出该删机器还是
该换编号。这里锁住三件事：

* 另一种类型占用时，连 force 都提前拒绝（并说清原因）；
* 同类型占用时，只有显式开启 force 才放行；
* 容器归档走 LXC 端点（``ostemplate`` + ``restore=1``），不能拿去打
  ``/nodes/{node}/qemu``。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app.backup_volid import archive_guest_type, vmid_from_volid  # noqa: E402
from app.pve import ProxmoxClient, ProxmoxError  # noqa: E402
from app.routers.backups import (  # noqa: E402
    _assert_restore_target_free,
    _restore_conflict_detail,
    _restore_format_detail,
    _restore_running_detail,
)


class _FakeClient:
    """只提供预检用到的那一个读接口。"""

    def __init__(self, resources: List[Dict[str, Any]], error: Optional[Exception] = None):
        self._resources = resources
        self._error = error

    async def cluster_resources(self, rtype: Optional[str] = None) -> List[Dict[str, Any]]:
        if self._error:
            raise self._error
        return self._resources


def _run(coro):
    return asyncio.run(coro)


def _guest(
    vmid: int,
    kind: str,
    node: str = "pve9-2",
    status: str = "stopped",
) -> Dict[str, Any]:
    return {"vmid": vmid, "type": kind, "node": node, "status": status}


def test_cross_type_collision_is_refused_even_with_force() -> None:
    """VM 归档恢复进「已被 CT 占用」的 VMID：force 也覆盖不了，直接拒绝。"""
    client = _FakeClient([_guest(103, "lxc")])
    with pytest.raises(HTTPException) as exc:
        _run(_assert_restore_target_free(client, "pve9-2", 103, "qemu", force=True))
    assert exc.value.status_code == 409
    assert "容器 103" in exc.value.detail
    assert "换一个 VMID" in exc.value.detail


def test_same_type_collision_needs_explicit_force() -> None:
    client = _FakeClient([_guest(103, "qemu")])
    with pytest.raises(HTTPException) as exc:
        _run(_assert_restore_target_free(client, "pve9-2", 103, "qemu", force=False))
    assert exc.value.status_code == 409
    assert "强制覆盖" in exc.value.detail

    # 显式开启 force 后放行（由 PVE 真正做覆盖）
    _run(_assert_restore_target_free(client, "pve9-2", 103, "qemu", force=True))


def test_running_target_must_be_shut_down_first() -> None:
    """目标机器还开着：PVE 会先拷盘再拒绝（"vm is running"），必须提前拦住。"""
    client = _FakeClient([_guest(102, "qemu", status="running")])
    with pytest.raises(HTTPException) as exc:
        _run(_assert_restore_target_free(client, "pve9-2", 102, "qemu", force=True))
    assert exc.value.status_code == 409
    assert "正在运行" in exc.value.detail
    assert "关机" in exc.value.detail

    # 容器同理
    client = _FakeClient([_guest(104, "lxc", status="running")])
    with pytest.raises(HTTPException) as exc:
        _run(_assert_restore_target_free(client, "pve9-2", 104, "lxc", force=True))
    assert "容器 104 正在运行" in exc.value.detail

    # 关机后即可覆盖
    client = _FakeClient([_guest(102, "qemu", status="stopped")])
    _run(_assert_restore_target_free(client, "pve9-2", 102, "qemu", force=True))


def test_running_error_is_translated() -> None:
    """同步返回的 "vm is running" 也要翻成「先关机」的说明。"""
    detail = _restore_running_detail(
        "unable to restore VM 102 - vm is running", "pve9-2", 102
    )
    assert detail and "正在运行" in detail and "关机" in detail
    assert _restore_running_detail("storage 'x' does not exist", "pve9-2", 102) is None


def test_free_vmid_and_other_node_pass() -> None:
    client = _FakeClient([_guest(103, "lxc", node="pve9-1")])
    _run(_assert_restore_target_free(client, "pve9-2", 103, "qemu", force=False))
    _run(_assert_restore_target_free(client, "pve9-2", 104, "qemu", force=False))


def test_unreadable_resource_list_does_not_block() -> None:
    """预检读不到集群清单时放行：最终判定仍归 PVE。"""
    client = _FakeClient([], error=ProxmoxError("boom", 500))
    _run(_assert_restore_target_free(client, "pve9-2", 103, "qemu", force=False))


def test_unexpected_backup_format_is_explained() -> None:
    """PVE 把容器归档当虚拟机解析时的英文报错也要翻成人话（类型判定兜底）。"""
    detail = _restore_format_detail(
        "cannot parse volume ID - got unexpected backup format 'pbs-ct'",
        PBS_CT_VOLID,
    )
    assert detail and "刷新备份列表" in detail
    assert _restore_format_detail("some other failure", PBS_CT_VOLID) is None


def test_pve_error_message_is_translated() -> None:
    detail = _restore_conflict_detail(
        "CT 103 already exists on node 'pve9-2'", "pve9-2", 103
    )
    assert detail and "容器" in detail and "103" in detail
    # 别的失败照原样冒泡，不要被误判成撞号
    assert _restore_conflict_detail("storage 'x' does not exist", "pve9-2", 103) is None


def _client_recording(calls: List[Any]) -> ProxmoxClient:
    client = object.__new__(ProxmoxClient)

    async def fake_post(path: str, data: Optional[Dict[str, Any]] = None) -> str:
        calls.append((path, data or {}))
        return "UPID:pve9-2:0000:restore"

    client.post = fake_post  # type: ignore[method-assign]
    return client


def test_lxc_archive_uses_lxc_endpoint() -> None:
    calls: List[Any] = []
    client = _client_recording(calls)
    _run(
        client.backup_restore(
            "pve9-2",
            "local:backup/vzdump-lxc-103-2026_11_09-02_00_00.tar.zst",
            103,
            storage="local-lvm",
            force=False,
        )
    )
    path, data = calls[0]
    assert path == "/nodes/pve9-2/lxc"
    assert data["ostemplate"].startswith("local:backup/vzdump-lxc-103")
    assert data["restore"] == 1
    assert data["force"] == 0


def test_qemu_archive_uses_qemu_endpoint() -> None:
    calls: List[Any] = []
    client = _client_recording(calls)
    _run(
        client.backup_restore(
            "pve9-2",
            "local:backup/vzdump-qemu-103-2026_11_09-02_00_00.vma.zst",
            103,
            storage="local-lvm",
            force=True,
        )
    )
    path, data = calls[0]
    assert path == "/nodes/pve9-2/qemu"
    assert data["archive"].endswith(".vma.zst")
    assert "ostemplate" not in data
    assert data["force"] == 1


# ---------------------------------------------------------------- PBS 归档
# PBS 的 volid 是 ``pbs:backup/ct/104/2026_11_10T02_00_00Z``：没有 vzdump 前缀，
# 只认 ``vzdump-lxc`` 就会把容器备份发去打虚拟机端点，PVE 回
# "got unexpected backup format 'pbs-ct'"。
PBS_CT_VOLID = "pbs:backup/ct/104/2026_11_10T02_00_00Z"
PBS_VM_VOLID = "pbs:backup/vm/104/2026_11_10T02_00_00Z"


def test_pbs_container_archive_uses_lxc_endpoint() -> None:
    calls: List[Any] = []
    client = _client_recording(calls)
    _run(client.backup_restore("pve9-2", PBS_CT_VOLID, 104, storage="pbs"))
    path, data = calls[0]
    assert path == "/nodes/pve9-2/lxc"
    assert data["ostemplate"] == PBS_CT_VOLID
    assert data["restore"] == 1


def test_pbs_vm_archive_uses_qemu_endpoint() -> None:
    calls: List[Any] = []
    client = _client_recording(calls)
    _run(client.backup_restore("pve9-2", PBS_VM_VOLID, 104, storage="pbs"))
    path, data = calls[0]
    assert path == "/nodes/pve9-2/qemu"
    assert data["archive"] == PBS_VM_VOLID


def test_pbs_format_hint_when_volid_has_no_type_segment() -> None:
    """volid 拼不出类型时，靠 storage content 的 ``format`` 兜底。"""
    assert archive_guest_type("pbs:backup/104/snapshot", "pbs-ct") == "lxc"
    assert archive_guest_type("pbs:backup/104/snapshot", "pbs-vm") == "qemu"
    assert archive_guest_type("local:backup/x.tar.zst", "tar.zst") == "lxc"
    assert archive_guest_type("local:backup/x.bin") is None


def test_vmid_is_parsed_for_both_storage_kinds() -> None:
    assert vmid_from_volid("local:backup/vzdump-lxc-104-2026_11_10-02_00_00.tar.zst") == 104
    assert vmid_from_volid(PBS_VM_VOLID) == 104
    assert vmid_from_volid("local:iso/debian.iso") is None
