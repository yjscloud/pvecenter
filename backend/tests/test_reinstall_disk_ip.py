"""重装的两个「静默重置」问题：系统盘容量被模板尺寸覆盖、地址脱离地址池。

这两个问题的共同点是**不报错**：

* 100G 的机器重装完只剩 20G —— 界面显示重装成功，用户某天才发现后面的数据写不进去；
* 重装时选了 DHCP —— 客户机去问外部 DHCP，拿到的地址不在池里，而「池的占用表」
  和「页面显示的地址」从此各说各话。

所以这里不测「重装能不能跑通」，只测两个决定结果是否被悄悄改掉的决策：
容量恢复（``_restore_disk_size``）与地址来源（``_plan_network``）。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import reinstall  # noqa: E402


class DiskClient:
    """只回答「配置里的 size=」与 `qm resize`；不碰任何网络。"""

    def __init__(self, size_gb: float, *, hide_size: bool = False) -> None:
        self.size = size_gb
        self.hide_size = hide_size
        self.resized: list = []
        self.paths: list = []

    async def get(self, path: str):
        self.paths.append(path)
        if path.endswith("/config"):
            if self.hide_size:
                # 某些存储 / 老配置的 config 不带 size=，只能靠运行态接口
                return {"scsi0": "local-lvm:vm-110-disk-0"}
            return {"scsi0": f"local-lvm:vm-110-disk-0,size={self.size:g}G"}
        if path.endswith("/status/current"):
            return {"status": "stopped", "maxdisk": int(self.size * 1024**3)}
        raise AssertionError(f"不该走到这里：{path}")

    async def qemu_resize(self, node: str, vmid: int, disk: str, size: str):
        self.resized.append((node, vmid, disk, size))
        self.size = float(str(size).rstrip("G"))
        # 非 UPID：_wait 会直接返回，不需要假任务表
        return {"task": ""}


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- 系统盘容量
def test_restore_disk_size_grows_back_to_the_original():
    """模板 20G、原来 100G：必须扩回 100G，并校验通过。"""
    client = DiskClient(20)
    steps: list = []

    after = run(
        reinstall._restore_disk_size(
            client,
            node="pve",
            vmid=110,
            disk_key="scsi0",
            target_gb=100,
            record=lambda step, ok, detail="": steps.append((step, ok, detail)),
        )
    )

    assert client.resized == [("pve", 110, "scsi0", "100G")]
    assert after == 100
    # 最后一条必须是「校验通过」，而不是只报「已经扩了」
    assert ("校验系统盘容量" in steps[-1][0] or "恢复系统盘容量" in steps[-1][0])
    assert steps[-1][1] is True


def test_restore_disk_size_never_shrinks():
    """模板比原来的盘还大时保持模板尺寸 —— PVE 的卷只能增不能减。"""
    client = DiskClient(120)
    steps: list = []

    after = run(
        reinstall._restore_disk_size(
            client,
            node="pve",
            vmid=110,
            disk_key="scsi0",
            target_gb=100,
            record=lambda step, ok, detail="": steps.append((step, ok, detail)),
        )
    )

    assert client.resized == []
    assert after == 120
    assert steps[-1][1] is True


def test_disk_size_falls_back_to_maxdisk():
    """config 里没有 size= 时，退回 status/current 的 maxdisk，而不是猜一个数字。"""
    client = DiskClient(64, hide_size=True)
    assert run(reinstall._disk_gb(client, "pve", 110, "scsi0")) == 64


# ------------------------------------------------------------------- 地址池
POOL = {
    "id": "pool-a",
    "name": "业务池",
    "subnet": "10.0.0.0/24",
    "start": "10.0.0.10",
    "end": "10.0.0.100",
    "gateway": "10.0.0.1",
    "bridge": "vmbr0",
    "owners": ["*"],
}

CONFIG = {
    "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
    "ipconfig0": "ip=10.0.0.50/24,gw=10.0.0.1",
}


def _patch_pools(monkeypatch, pools, *, claim_ok: bool = True):
    async def load_pools():
        return pools

    claimed: list = []

    async def claim(allocation, *, connection_id, node, vmid):
        claimed.append(allocation.ip)
        return claim_ok

    async def allocate(*args, **kwargs):
        raise AssertionError("原地址可用时不该重新分配")

    monkeypatch.setattr(reinstall.ippool, "load_pools", load_pools)
    monkeypatch.setattr(reinstall.ippool, "claim", claim)
    monkeypatch.setattr(reinstall.ippool, "allocate", allocate)
    return claimed


def test_plan_network_reuses_pool_address_when_mode_is_dhcp(monkeypatch):
    """向导里选 DHCP，但原机器用的是池内静态地址 —— 重装要沿用，不能真去问 DHCP。"""
    claimed = _patch_pools(monkeypatch, [POOL])
    steps: list = []

    mode, ip, gateway = run(
        reinstall._plan_network(
            DiskClient(20),
            vmid=110,
            node="pve",
            connection="conn-1",
            owner_scope=None,
            config=CONFIG,
            ip_mode="dhcp",
            ip=None,
            gateway=None,
            record=lambda step, ok, detail="": steps.append((step, ok, detail)),
        )
    )

    assert (mode, ip, gateway) == ("static", "10.0.0.50/24", "10.0.0.1")
    assert claimed == ["10.0.0.50"]
    assert any("沿用" in step for step, _ok, _detail in steps)


def test_plan_network_rejects_static_address_outside_every_pool(monkeypatch):
    """手填一个池外地址必须当场拒绝：允许它等于允许制造一条池永远对不上的记录。"""
    _patch_pools(monkeypatch, [POOL])

    with pytest.raises(reinstall.ReinstallError) as excinfo:
        run(
            reinstall._plan_network(
                DiskClient(20),
                vmid=110,
                node="pve",
                connection="conn-1",
                owner_scope=None,
                config=CONFIG,
                ip_mode="static",
                ip="192.168.9.9",
                gateway="192.168.9.1",
                record=lambda *args: None,
            )
        )

    assert "地址池" in str(excinfo.value)


def test_plan_network_without_pools_keeps_caller_intent(monkeypatch):
    """存量环境一个池都没配：维持原来的参数（外部 DHCP 仍是唯一选择）。"""
    _patch_pools(monkeypatch, [])

    mode, ip, gateway = run(
        reinstall._plan_network(
            DiskClient(20),
            vmid=110,
            node="pve",
            connection="conn-1",
            owner_scope=None,
            config=CONFIG,
            ip_mode="dhcp",
            ip=None,
            gateway=None,
            record=lambda *args: None,
        )
    )

    assert (mode, ip, gateway) == ("dhcp", None, None)
