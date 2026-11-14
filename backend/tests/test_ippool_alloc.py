"""DHCP 模式的地址来源：从 IP 地址池自动分配，而不是问外部 DHCP。

这些用例盯的是「下发给 PVE 的配置」与「池列表的占用状态」两件事 —— 只测分配
函数的话，很容易出现「函数返回了地址，但创建路径没把它写进 ipconfigN」这种
静默失效；只测池列表的话，又看不出下发的到底是 DHCP 还是静态地址。

    ../.venv/bin/python -m pytest tests/test_ippool_alloc.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

import test_pve_client as mock_mod  # noqa: E402
from app import ippool  # noqa: E402
from test_api_routes import api, auth_headers  # noqa: E402,F401


POOL: Dict[str, Any] = {
    "id": "pool-1",
    "name": "客户网段",
    "bridge": "vmbr0",
    "subnet": "192.168.50.0/24",
    "gateway": "192.168.50.1",
    "start": "192.168.50.100",
    "end": "192.168.50.105",
    "nodes": [],
    "connections": [],
    "vlan": None,
    "owners": [],
}


def _set_pool(api, headers, **overrides) -> None:
    resp = api.put(
        "/api/ip-pools", headers=headers, json=[{**POOL, **overrides}]
    )
    assert resp.status_code == 200, resp.text


def _requests(path: str) -> Optional[Dict[str, Any]]:
    """mock PVE 收到的最后一笔指定路径的请求体。"""
    for method, url, body, _headers in reversed(mock_mod.REQUESTS):
        if url == path:
            return body
    return None


def _vm_payload(**overrides) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "node": "pve1",
        "name": "dhcp-vm",
        "memory": 2048,
        "cores": 2,
        "disks": [{"storage": "local-lvm", "size": 20, "interface": "scsi0"}],
        "networks": [{"bridge": "vmbr0", "model": "virtio"}],
        "cloudinit": {
            "enabled": True,
            "user": "ubuntu",
            "ip_configs": [{"ip": "dhcp"}],
        },
    }
    payload.update(overrides)
    return payload


def _ct_payload(**overrides) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "node": "pve1",
        "name": "dhcp-ct",
        "ostemplate": "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst",
        "storage": "local-lvm",
        "rootfs": 8,
        "memory": 512,
        "cores": 1,
        "networks": [{"bridge": "vmbr0", "name": "eth0", "ip": "dhcp"}],
    }
    payload.update(overrides)
    return payload


def _free_ips(api, headers) -> list[str]:
    resp = api.get("/api/ip-pools", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["pools"][0]["free"]


# ------------------------------------------------------------------ 虚拟机
def test_vm_dhcp_takes_an_address_from_the_pool(api) -> None:
    headers = auth_headers(api)
    _set_pool(api, headers)
    mock_mod.REQUESTS.clear()

    resp = api.post("/api/vms", json=_vm_payload(), headers=headers)
    assert resp.status_code == 200, resp.text

    body = _requests("/api2/json/nodes/pve1/qemu")
    assert body is not None, "创建请求没有发到 PVE"
    assert body["ipconfig0"] == "ip=192.168.50.100/24,gw=192.168.50.1"

    # 分配后立刻就是「已占用」：池列表不能再把这个地址当空闲发出去
    free = _free_ips(api, headers)
    assert "192.168.50.100" not in free
    assert "192.168.50.101" in free


def test_vm_dhcp_without_pool_stays_dhcp(api) -> None:
    """存量环境可能根本没配池：那种情况下保持原来的 DHCP 行为。"""
    headers = auth_headers(api)
    mock_mod.REQUESTS.clear()

    resp = api.post("/api/vms", json=_vm_payload(), headers=headers)
    assert resp.status_code == 200, resp.text

    body = _requests("/api2/json/nodes/pve1/qemu")
    assert body is not None
    assert body["ipconfig0"] == "ip=dhcp"


def test_vm_dhcp_reports_409_when_pool_is_exhausted(api) -> None:
    headers = auth_headers(api)
    # 只有一个地址的池：第一台拿走之后，第二台必须明确失败，而不是悄悄退回 DHCP
    _set_pool(api, headers, start="192.168.50.100", end="192.168.50.100")

    first = api.post("/api/vms", json=_vm_payload(name="one"), headers=headers)
    assert first.status_code == 200, first.text

    second = api.post("/api/vms", json=_vm_payload(name="two"), headers=headers)
    assert second.status_code == 409, second.text
    assert "地址池" in second.json()["detail"]


# ------------------------------------------------------------------ 容器
def test_container_dhcp_takes_an_address_from_the_pool(api) -> None:
    headers = auth_headers(api)
    _set_pool(api, headers)
    mock_mod.REQUESTS.clear()

    resp = api.post("/api/lxc", json=_ct_payload(), headers=headers)
    assert resp.status_code == 200, resp.text

    body = _requests("/api2/json/nodes/pve1/lxc")
    assert body is not None, "创建请求没有发到 PVE"
    assert body["net0"] == (
        "name=eth0,bridge=vmbr0,ip=192.168.50.100/24,gw=192.168.50.1"
    )


def test_container_default_nic_also_gets_a_pool_address(api) -> None:
    """请求体里没写网卡时，后端会补一张 net0；那张也得走地址池。"""
    headers = auth_headers(api)
    _set_pool(api, headers)
    mock_mod.REQUESTS.clear()

    resp = api.post("/api/lxc", json=_ct_payload(networks=[]), headers=headers)
    assert resp.status_code == 200, resp.text

    body = _requests("/api2/json/nodes/pve1/lxc")
    assert body is not None
    assert "ip=192.168.50.100/24" in body["net0"]


# ------------------------------------------------------------------ 回收
def test_delete_style_release_returns_the_address_to_the_pool(api) -> None:
    """删除路径调用 ``release_guest`` 之后，地址必须回到空闲表。"""
    headers = auth_headers(api)
    _set_pool(api, headers)

    resp = api.post("/api/vms", json=_vm_payload(), headers=headers)
    assert resp.status_code == 200, resp.text
    assert "192.168.50.100" not in _free_ips(api, headers)

    # 用 TestClient 自己的事件循环执行：数据库连接池绑定在它上面
    api.portal.call(ippool.release_guest, "", "pve1", 105)
    assert "192.168.50.100" in _free_ips(api, headers)
