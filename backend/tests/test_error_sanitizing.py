"""对外错误只给语义码 + 固定文案，不泄露宿主机路径与 PVE 原文。

对应审计项：``GET /api/vms|/api/lxc/{node}/{vmid}`` 对不存在 / 类型不匹配的
对象返回 500 并透传 PVE 的 ``Configuration file 'nodes/.../NNN.conf' does not
exist``；``GET /api/backups/download`` 把 ``stat`` 的 ``/var/lib/vz/dump/...``
原文回给前端。两类都是 CWE-209。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List

import pytest
from fastapi import HTTPException

from app.pve import ProxmoxError
from app.routers import backups as backups_router
from app.routers import vms as vms_router


class _ResourceClient:
    def __init__(self, resources: List[dict]) -> None:
        self._resources = resources

    async def cluster_resources(self, kind: str = "vm") -> List[dict]:
        return self._resources


def _missing_exc(vmid: int = 114) -> ProxmoxError:
    return ProxmoxError(
        f"Configuration file 'nodes/pve/qemu-server/{vmid}.conf' does not exist\n",
        502,
        pve_status=500,
    )


def test_missing_guest_returns_404_without_pve_text() -> None:
    with pytest.raises(HTTPException) as err:
        asyncio.run(
            vms_router._raise_guest_read_error(
                _ResourceClient([]), "pve", 114, "qemu", _missing_exc()
            )
        )
    assert err.value.status_code == 404
    assert err.value.detail == "资源不存在"
    assert "Configuration file" not in str(err.value.detail)


def test_wrong_guest_type_returns_400() -> None:
    client = _ResourceClient([{"vmid": 114, "node": "pve", "type": "lxc"}])
    with pytest.raises(HTTPException) as err:
        asyncio.run(
            vms_router._raise_guest_read_error(client, "pve", 114, "qemu", _missing_exc())
        )
    assert err.value.status_code == 400
    assert "不是虚拟机" in err.value.detail


def test_other_pve_error_is_502_with_trace_only() -> None:
    exc = ProxmoxError(
        "stat: cannot statx '/var/lib/vz/dump/x.vma.zst': Permission denied",
        500,
        pve_status=500,
    )
    with pytest.raises(HTTPException) as err:
        asyncio.run(
            vms_router._raise_guest_read_error(
                _ResourceClient([]), "pve", 114, "qemu", exc
            )
        )
    assert err.value.status_code == 502
    assert "/var/lib/vz" not in err.value.detail
    assert "Permission denied" not in err.value.detail
    assert "trace:" in err.value.detail


def test_backup_download_hides_host_path_and_stat_output(monkeypatch) -> None:
    async def fake_find(host: str) -> Any:
        return {"id": 1}

    async def fake_run(row: Any, command: str, timeout: int = 20) -> Any:
        return (
            False,
            "stat: cannot statx '/var/lib/vz/dump/x.vma.zst': No such file or directory",
        )

    async def fake_access(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_path(client: Any, storage: str) -> str:
        return "/var/lib/vz"

    async def fake_audit(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_resolve(node: str) -> str:
        return ""

    monkeypatch.setattr(backups_router.sshremote, "find_by_address", fake_find)
    monkeypatch.setattr(backups_router.sshremote, "run_command", fake_run)
    monkeypatch.setattr(backups_router, "assert_backup_access", fake_access)
    monkeypatch.setattr(backups_router, "_dir_storage_path", fake_path)
    monkeypatch.setattr(backups_router.security, "audit", fake_audit)
    monkeypatch.setattr(backups_router, "resolve_node_connection", fake_resolve)
    monkeypatch.setattr(
        backups_router,
        "get_client",
        lambda: SimpleNamespace(conn=SimpleNamespace(host="172.16.149.3")),
    )
    monkeypatch.setattr(
        backups_router.store, "get_active_connection_id", lambda: ""
    )

    with pytest.raises(HTTPException) as err:
        asyncio.run(
            backups_router.download_backup(
                request=SimpleNamespace(headers={}),
                node="pve",
                storage="local",
                volid="local:backup/x.vma.zst",
                connection=None,
                user={"username": "admin", "role": "admin"},
            )
        )

    assert err.value.status_code == 404
    assert "/var/lib/vz" not in err.value.detail
    assert "stat:" not in err.value.detail
    assert "trace:" in err.value.detail
