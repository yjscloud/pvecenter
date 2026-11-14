"""IP 地址池占用扫描的缓存行为。

创建向导 / 批量部署 / 池管理页共用 ``GET /api/ip-pools``，以前每次请求都会把
集群里**每一台**虚拟机与容器的配置读一遍。这些用例盯的是改造后的三条硬约束：

* 同一份快照在保鲜期内复用（同一批请求只扫一次）；
* 扫描整体失败时沿用上一份快照，不能把「什么都没扫到」当成「没有地址被占用」；
* 删除客户机后，缓存里那台机器的地址立刻回到空闲表。

    ../.venv/bin/python -m pytest tests/test_ippool_cache.py -v
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

import test_pve_client as mock_mod  # noqa: E402
from app import ippool  # noqa: E402
from test_api_routes import api, auth_headers  # noqa: E402,F401

POOL = {
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


@pytest.fixture(autouse=True)
def _clean_cache(api):
    """每个用例都从冷缓存开始，结束后也别把快照留给下一条用例。"""
    api.portal.call(ippool.clear_cache)
    yield
    api.portal.call(ippool.clear_cache)


def _set_pool(api, headers) -> None:
    resp = api.put("/api/ip-pools", headers=headers, json=[POOL])
    assert resp.status_code == 200, resp.text


def _count(path_suffix: str) -> int:
    return sum(1 for _m, path, _b, _h in mock_mod.REQUESTS if path.endswith(path_suffix))


def test_second_pool_request_reuses_the_snapshot(api) -> None:
    """保鲜期内第二次取池列表：不应该再逐台读配置。"""
    headers = auth_headers(api)
    _set_pool(api, headers)
    mock_mod.REQUESTS.clear()

    first = api.get("/api/ip-pools", headers=headers)
    assert first.status_code == 200, first.text
    assert first.headers["X-IP-Pool-Snapshot"] == "miss"
    configs_after_first = _count("/config")
    resources_after_first = _count("/cluster/resources")
    assert configs_after_first >= 1  # 冷启动至少读了一台（mock 里的 VM 100）

    second = api.get("/api/ip-pools", headers=headers)
    assert second.status_code == 200, second.text
    assert second.headers["X-IP-Pool-Snapshot"] == "hit"
    assert _count("/config") == configs_after_first
    assert _count("/cluster/resources") == resources_after_first
    assert second.json()["used"] == first.json()["used"]


def test_failed_scan_keeps_previous_snapshot(api, monkeypatch) -> None:
    """整轮扫描失败（PVE 不可达）时不能把旧快照清空。"""

    async def ok_scan(previous=None):
        return {"192.168.50.100": "pve1"}, {}, {""}, True

    monkeypatch.setattr(ippool, "_scan_configs", ok_scan)
    used = api.portal.call(ippool.collect_used)
    assert "192.168.50.100" in used

    async def failed_scan(previous=None):
        return {}, {}, set(), False

    monkeypatch.setattr(ippool, "_scan_configs", failed_scan)
    snapshot = api.portal.call(ippool._refresh_scan)
    assert snapshot.used == {"192.168.50.100": "pve1"}


def test_delete_drops_cached_entry_immediately(api, monkeypatch) -> None:
    """删除客户机后，缓存里它占的地址必须马上释放。"""
    snapshot = ippool._ScanSnapshot(
        at=time.monotonic(),
        used={"192.168.50.100": "pve1"},
        observed={("", "pve1", 900): {"192.168.50.100"}},
        scanned={""},
    )
    monkeypatch.setattr(ippool, "_scan_cache", snapshot)

    api.portal.call(ippool.release_guest, "", "pve1", 900)

    used = api.portal.call(ippool.collect_used)
    assert "192.168.50.100" not in used


def test_cache_can_be_disabled_for_rollback(api, monkeypatch) -> None:
    """``IP_POOL_SCAN_CACHE=0`` 的等价开关：关掉后回到每次请求都现场扫描。"""
    headers = auth_headers(api)
    _set_pool(api, headers)
    monkeypatch.setattr(ippool, "SCAN_CACHE_ENABLED", False)
    mock_mod.REQUESTS.clear()

    first = api.get("/api/ip-pools", headers=headers)
    assert first.status_code == 200, first.text
    assert first.headers["X-IP-Pool-Snapshot"] == "miss"
    configs_after_first = _count("/config")

    second = api.get("/api/ip-pools", headers=headers)
    assert second.status_code == 200, second.text
    assert second.headers["X-IP-Pool-Snapshot"] == "miss"
    assert _count("/config") == configs_after_first * 2


def test_dhcp_create_reuses_the_snapshot(api) -> None:
    """第二台 DHCP 下发不该再为地址扫描付一次全集群配置读取。"""
    headers = auth_headers(api)
    _set_pool(api, headers)
    mock_mod.REQUESTS.clear()

    payload = {
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
    first = api.post("/api/vms", json=payload, headers=headers)
    assert first.status_code == 200, first.text
    configs_after_first = _count("/config")

    second = api.post("/api/vms", json={**payload, "name": "dhcp-vm-2"}, headers=headers)
    assert second.status_code == 200, second.text
    assert _count("/config") == configs_after_first
