"""AI 助手目标主机的五条件与运算。

候选清单与发起入口共用 ``ai_scope``：这里钉住「缺一即排除」，避免以后为了
「让下拉里多一台」而放宽成或运算，也避免把「读不到」误报成「没配置」。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import ai_scope  # noqa: E402
from app.pve import ProxmoxError  # noqa: E402


def _config(*, drive: bool = True, user: str = "root") -> Dict[str, Any]:
    config: Dict[str, Any] = {"name": "web1", "cores": 2}
    if drive:
        config["ide2"] = "local:100/vm-100-cloudinit.qcow2"
    if user:
        config["ciuser"] = user
    return config


class FakeClient:
    def __init__(
        self,
        *,
        status: str = "running",
        config: Optional[Dict[str, Any]] = None,
        agent_ok: bool = True,
        status_error: Optional[Exception] = None,
        config_error: Optional[Exception] = None,
    ) -> None:
        self.status = status
        self.config = config if config is not None else _config()
        self.agent_ok = agent_ok
        self.status_error = status_error
        self.config_error = config_error
        self.calls = {"status": 0, "config": 0, "ping": 0}

    async def qemu_status(self, node: str, vmid: int) -> Dict[str, Any]:
        self.calls["status"] += 1
        if self.status_error:
            raise self.status_error
        return {"status": self.status}

    async def qemu_config(self, node: str, vmid: int) -> Dict[str, Any]:
        self.calls["config"] += 1
        if self.config_error:
            raise self.config_error
        return self.config

    async def qemu_agent_ping(self, node: str, vmid: int) -> Dict[str, Any]:
        self.calls["ping"] += 1
        if not self.agent_ok:
            raise TimeoutError("agent did not answer")
        return {}


HOST = {
    "id": "h1",
    "name": "web1",
    "node": "pve1",
    "vmid": 100,
    "conn_id": "c1",
}


@pytest.fixture(autouse=True)
def clear_cache():
    ai_scope.reset_cache()
    yield
    ai_scope.reset_cache()


def _patch_client(monkeypatch, client) -> None:
    monkeypatch.setattr(
        ai_scope.pve,
        "client_for_connection",
        lambda conn_id, **_kwargs: client,
    )


def test_all_conditions_pass(monkeypatch) -> None:
    client = FakeClient()
    _patch_client(monkeypatch, client)
    ok, reason = asyncio.run(ai_scope.check_host(HOST))
    assert (ok, reason) == (True, "")
    assert client.calls == {"status": 1, "config": 1, "ping": 1}


def test_host_without_vm_is_excluded(monkeypatch) -> None:
    _patch_client(monkeypatch, FakeClient())
    ok, reason = asyncio.run(ai_scope.check_host({**HOST, "vmid": None}))
    assert (ok, reason) == (False, ai_scope.REASON_NOT_VM)


def test_stopped_vm_is_excluded(monkeypatch) -> None:
    _patch_client(monkeypatch, FakeClient(status="stopped"))
    ok, reason = asyncio.run(ai_scope.check_host(HOST))
    assert (ok, reason) == (False, ai_scope.REASON_STOPPED)


def test_paused_vm_is_excluded(monkeypatch) -> None:
    """paused 不是「运行中」：Guest Agent 可能应答，但业务已停。"""
    _patch_client(monkeypatch, FakeClient(status="paused"))
    assert asyncio.run(ai_scope.check_host(HOST)) == (False, ai_scope.REASON_STOPPED)


@pytest.mark.parametrize(
    "config",
    [
        _config(drive=False),
        _config(user=""),
        {},
    ],
)
def test_incomplete_cloudinit_is_excluded(monkeypatch, config) -> None:
    _patch_client(monkeypatch, FakeClient(config=config))
    ok, reason = asyncio.run(ai_scope.check_host(HOST))
    assert (ok, reason) == (False, ai_scope.REASON_CLOUDINIT)


def test_cloudinit_accepts_key_only_login(monkeypatch) -> None:
    """没有 ciuser、但通过 sshkeys 下发登录凭证，也算 cloud-init 配置完整。"""
    _patch_client(
        monkeypatch,
        FakeClient(config=_config(user="", drive=True) | {"sshkeys": "ssh-ed25519%20AAAA"}),
    )
    assert asyncio.run(ai_scope.check_host(HOST)) == (True, "")


def test_missing_agent_is_excluded(monkeypatch) -> None:
    _patch_client(monkeypatch, FakeClient(agent_ok=False))
    ok, reason = asyncio.run(ai_scope.check_host(HOST))
    assert (ok, reason) == (False, ai_scope.REASON_AGENT)


def test_404_means_not_a_vm(monkeypatch) -> None:
    _patch_client(
        monkeypatch,
        FakeClient(status_error=ProxmoxError("no such VM", pve_status=404)),
    )
    assert asyncio.run(ai_scope.check_host(HOST)) == (False, ai_scope.REASON_NOT_VM)


def test_permission_error_means_unreachable(monkeypatch) -> None:
    """权限不足是「读不到」，不能显示成「这台机器没配 cloud-init」。"""
    _patch_client(
        monkeypatch,
        FakeClient(status_error=ProxmoxError("denied", pve_status=403)),
    )
    assert asyncio.run(ai_scope.check_host(HOST)) == (False, ai_scope.REASON_UNREACHABLE)


def test_result_is_cached(monkeypatch) -> None:
    client = FakeClient()
    _patch_client(monkeypatch, client)

    async def twice():
        first = await ai_scope.check_host(HOST)
        second = await ai_scope.check_host(HOST)
        return first, second

    assert asyncio.run(twice()) == ((True, ""), (True, ""))
    assert client.calls == {"status": 1, "config": 1, "ping": 1}


def test_fast_mode_does_not_wait_for_probes(monkeypatch) -> None:
    """首屏不等 PVE：没缓存的主机记成 pending，探测排到后台。"""
    client = FakeClient()
    _patch_client(monkeypatch, client)
    eligible, counts, details, pending = asyncio.run(
        ai_scope.evaluate_hosts_fast([HOST])
    )
    assert eligible == []
    assert pending == 1
    assert counts.get("pending") == 1
    assert client.calls == {"status": 0, "config": 0, "ping": 0}


def test_fast_mode_serves_cached_verdicts(monkeypatch) -> None:
    _patch_client(monkeypatch, FakeClient())
    assert asyncio.run(ai_scope.check_host(HOST)) == (True, "")
    eligible, _, _, pending = asyncio.run(ai_scope.evaluate_hosts_fast([HOST]))
    assert [item["id"] for item in eligible] == ["h1"]
    assert pending == 0


def test_fast_mode_reports_excluded_from_cache(monkeypatch) -> None:
    _patch_client(monkeypatch, FakeClient(status="stopped"))
    asyncio.run(ai_scope.check_host(HOST))
    eligible, _, details, pending = asyncio.run(ai_scope.evaluate_hosts_fast([HOST]))
    assert eligible == []
    assert details[0]["reason"] == ai_scope.REASON_STOPPED
    assert pending == 0


def test_permission_denied_is_not_a_down_connection() -> None:
    """401/403 说明 PVE 已经应答，只是令牌权限不够 —— 不能报「连接读不到」。"""
    class DeniedClient:
        async def version(self) -> Dict[str, Any]:
            raise ProxmoxError("Permission check failed", 502, pve_status=403)

    ai_scope.reset_cache()
    assert (
        asyncio.run(
            ai_scope._connection_alive("c1", DeniedClient(), ai_scope.tuning())
        )
        is True
    )

    assert ai_scope._is_connection_failure(ProxmoxError("denied", 502, pve_status=403)) is False
    assert ai_scope._is_connection_failure(ProxmoxError("boom", 500)) is False
    assert ai_scope._is_connection_failure(ProxmoxError("unreachable", 502)) is True
    assert ai_scope._is_connection_failure(asyncio.TimeoutError()) is True


def test_missing_connection_falls_back_only_for_internal_aggregation(monkeypatch) -> None:
    """受管主机行里的旧 conn_id 不该让整条 PVE 被判故障；请求头路径仍严格 404。"""
    from app import pve

    sentinel = object()
    monkeypatch.setattr(pve, "_scoped_client", lambda _cid: None)
    monkeypatch.setattr(pve, "get_client", lambda: sentinel)

    assert pve.client_for_connection("deleted", fallback_to_active=True) is sentinel
    with pytest.raises(ProxmoxError):
        pve.client_for_connection("deleted")


def test_pve_down_ignores_unrelated_and_stale_connections() -> None:
    """「1 条读不到」只报本轮候选、且刚刚失败过的连接。

    现场问题：PVE 明明正常，AI 页却长期挂着一条告警 —— 因为进程级 ``_HEALTH``
    里躺着别的连接（或一分钟前的偶发超时）的失败记录。
    """
    import time

    now = time.monotonic()
    ai_scope._HEALTH["other-conn"] = (now, False)
    ai_scope._HEALTH["c1"] = (now - 60, False)
    assert ai_scope._recent_down_connections([HOST]) == 0

    ai_scope._HEALTH["c1"] = (now, False)
    assert ai_scope._recent_down_connections([HOST]) == 1


def test_resolve_host_rejects_ineligible_machine(monkeypatch) -> None:
    """候选清单与发起入口共用规则：绕过页面直接传 host_id 也进不去。"""
    from app import ai

    async def fake_targets(allowed):
        return [HOST]

    monkeypatch.setattr(ai.baseline, "targets", fake_targets)
    _patch_client(monkeypatch, FakeClient(status="stopped"))

    with pytest.raises(ai.AIError) as rejected:
        asyncio.run(ai.resolve_host("h1", None))
    message = str(rejected.value)
    assert "不满足 AI 排查条件" in message
    assert "开机运行状态" in message


def test_evaluate_counts_reasons_and_keeps_order(monkeypatch) -> None:
    clients = {
        "ok": FakeClient(),
        "stopped": FakeClient(status="stopped"),
        "agent": FakeClient(agent_ok=False),
    }
    monkeypatch.setattr(
        ai_scope.pve,
        "client_for_connection",
        lambda conn_id, **_kwargs: clients[conn_id],
    )
    hosts = [
        {**HOST, "id": "ok", "conn_id": "ok"},
        {**HOST, "id": "stopped", "conn_id": "stopped"},
        {**HOST, "id": "agent", "conn_id": "agent"},
        {**HOST, "id": "notvm", "conn_id": "ok", "vmid": None},
    ]
    eligible, counts = asyncio.run(ai_scope.evaluate_hosts(hosts))
    assert [item["id"] for item in eligible] == ["ok"]
    assert counts == {
        "total": 3,
        ai_scope.REASON_STOPPED: 1,
        ai_scope.REASON_AGENT: 1,
        ai_scope.REASON_NOT_VM: 1,
    }
