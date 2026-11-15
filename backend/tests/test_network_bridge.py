"""「换网桥」只该换网桥。

克隆与重装都要能让用户把机器接到另一套网络里（模板在 A 网桥、新机器要进 B 网桥），
而地址是静态还是 DHCP 与这件事无关 —— 两种模式共用同一块网卡。

这里锁住一个容易写错、错了又很难发现的点：换网桥**不能顺手重建整行网卡**。
整行重写会把 MAC 换成新的（客户机的 DHCP 静态租约、上游交换机的端口安全随之失效），
VLAN / 速率 / 防火墙标记也会一起丢掉。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import vmconfig  # noqa: E402


def test_override_bridge_keeps_mac_vlan_rate_and_firewall():
    config = {
        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,tag=10,firewall=1,rate=10",
        "net1": "virtio=11:22:33:44:55:66,bridge=vmbr0",
    }

    out = vmconfig.override_bridge(config, "vmbr1")

    # 只动 net0；net1 原样留在配置里（返回片段里不该出现它）
    assert set(out) == {"net0"}
    segments = out["net0"].split(",")
    assert "bridge=vmbr1" in segments
    assert "bridge=vmbr0" not in segments
    # MAC / VLAN / 速率 / 防火墙全部保留
    assert "virtio=AA:BB:CC:DD:EE:FF" in segments
    assert "tag=10" in segments
    assert "firewall=1" in segments
    assert "rate=10" in segments
    # 不能出现两个 bridge=
    assert sum(1 for s in segments if s.startswith("bridge=")) == 1


def test_override_bridge_is_address_mode_agnostic():
    """DHCP 与静态地址写的是 ipconfigN，与网卡行无关 —— 换桥结果必须完全一致。"""
    dhcp = {"net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0", "ipconfig0": "ip=dhcp"}
    static = {
        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
        "ipconfig0": "ip=10.0.0.50/24,gw=10.0.0.1",
    }

    assert vmconfig.override_bridge(dhcp, "vmbr1") == vmconfig.override_bridge(
        static, "vmbr1"
    )


def test_override_bridge_without_nic_changes_nothing():
    """模板里没有网卡时不凭空加一张 —— 客户机里也没有配套的接口配置。"""
    assert vmconfig.override_bridge({"ide0": "local:iso/x.iso"}, "vmbr1") == {}
    assert vmconfig.override_bridge({"net0": "bridge=vmbr0"}, "") == {}
