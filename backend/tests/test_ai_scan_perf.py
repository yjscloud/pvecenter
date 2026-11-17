"""AI 目标扫描的性能回归（往返次数 / 并发上限 / 重试策略 / 缓存批量读写）。

这一组用例钉住的都是「悄悄退回去就没人发现」的优化：

1. 一台跑通的机器只花 **2 次** PVE 往返（status → config∥ping），不是 3 次；
2. 并发上限真的生效（``ai_scan_concurrency``），不是「看起来并发」；
3. 一条 PVE 躺下时，整组只探活一次，不逐台等超时；
4. 重试只发生在**瞬时错误**上：404 / 403 / agent 超时都不重试；
5. 共享缓存整批读、整批写：N 台机器 = 1 条 SELECT + 1 次 executemany；
6. 预算到点如实报 pending，pending 不会永远挂着。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import ai_scope  # noqa: E402
from app.config import settings  # noqa: E402
from app.pve import ProxmoxError  # noqa: E402


def _config(*, drive: bool = True, user: str = "root") -> Dict[str, Any]:
    config: Dict[str, Any] = {"name": "web1", "cores": 2}
    if drive:
        config["ide2"] = "local:100/vm-100-cloudinit.qcow2"
    if user:
        config["ciuser"] = user
    return config


class SlowClient:
    """可控延迟 / 可控失败的 PVE 替身。

    并发峰值只按 ``qemu_status`` 统计：一台主机每次探测恰好问一次 status，
    而 config 与 ping 是同一次探测里并行的两个调用 —— 把它们也算进「并发」
    会把「2 台机器同时在探」误读成 4。
    """

    def __init__(
        self,
        *,
        status: str = "running",
        config: Optional[Dict[str, Any]] = None,
        agent_ok: bool = True,
        agent_error: Optional[Exception] = None,
        status_error: Optional[Exception] = None,
        status_error_once: bool = False,
        config_error: Optional[Exception] = None,
        status_delay: float = 0.0,
        config_delay: float = 0.0,
        agent_delay: float = 0.0,
        version_error: Optional[Exception] = None,
    ) -> None:
        self.status = status
        self.config = config if config is not None else _config()
        self.agent_ok = agent_ok
        self.agent_error = agent_error
        self.status_error = status_error
        self.status_error_once = status_error_once
        self.config_error = config_error
        self.status_delay = status_delay
        self.config_delay = config_delay
        self.agent_delay = agent_delay
        self.version_error = version_error
        self.calls = {"version": 0, "status": 0, "config": 0, "ping": 0}
        self.in_flight = 0
        self.max_in_flight = 0

    async def version(self) -> Dict[str, Any]:
        self.calls["version"] += 1
        if self.version_error:
            raise self.version_error
        return {"version": "8.2"}

    async def qemu_status(self, node: str, vmid: int) -> Dict[str, Any]:
        self.calls["status"] += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.status_delay:
                await asyncio.sleep(self.status_delay)
            else:
                await asyncio.sleep(0)
            if self.status_error:
                # 「只错一次」用于验证重试：第二次调用正常返回
                if not (self.status_error_once and self.calls["status"] > 1):
                    raise self.status_error
            return {"status": self.status}
        finally:
            self.in_flight -= 1

    async def qemu_config(self, node: str, vmid: int) -> Dict[str, Any]:
        self.calls["config"] += 1
        if self.config_delay:
            await asyncio.sleep(self.config_delay)
        if self.config_error:
            raise self.config_error
        return self.config

    async def qemu_agent_ping(self, node: str, vmid: int) -> Dict[str, Any]:
        self.calls["ping"] += 1
        if self.agent_delay:
            await asyncio.sleep(self.agent_delay)
        if self.agent_error:
            raise self.agent_error
        if not self.agent_ok:
            raise TimeoutError("agent did not answer")
        return {}


class BatchClient(SlowClient):
    """多了 ``/cluster/resources`` 的替身：批量取状态是本轮提速的核心。

    ``statuses`` = 这台 PVE 上「看得到的虚拟机」及其状态（``{vmid: status}``）；
    ``types`` 用来把某些 vmid 标成 lxc；``hidden`` 模拟「凭据看不到这台机器」
    —— 批量结果里没有它，应当退回逐台 status 再分辨 404 / 403。
    """

    def __init__(
        self,
        *,
        statuses: Optional[Dict[int, str]] = None,
        types: Optional[Dict[int, str]] = None,
        hidden: Optional[set] = None,
        resources_error: Optional[Exception] = None,
        resources_delay: float = 0.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.statuses = dict(statuses or {})
        self.types = dict(types or {})
        self.hidden = set(hidden or ())
        self.resources_error = resources_error
        self.resources_delay = resources_delay
        self.calls["resources"] = 0
        # 批量之后每台机器只剩 config ∥ ping 两个调用，并发按它们统计
        self.probe_in_flight = 0
        self.max_probe_in_flight = 0

    async def _tracked(self, name: str, delay: float) -> None:
        self.calls[name] += 1
        self.probe_in_flight += 1
        self.max_probe_in_flight = max(self.max_probe_in_flight, self.probe_in_flight)
        try:
            if delay:
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(0)
        finally:
            self.probe_in_flight -= 1

    async def cluster_resources(self, rtype: Optional[str] = None) -> List[Dict[str, Any]]:
        self.calls["resources"] += 1
        if self.resources_delay:
            await asyncio.sleep(self.resources_delay)
        if self.resources_error:
            raise self.resources_error
        return [
            {
                "node": "pve1",
                "vmid": vmid,
                "type": self.types.get(vmid, "qemu"),
                "status": status,
            }
            for vmid, status in self.statuses.items()
            if vmid not in self.hidden
        ]

    async def qemu_config(self, node: str, vmid: int) -> Dict[str, Any]:
        await self._tracked("config", self.config_delay)
        if self.config_error:
            raise self.config_error
        return self.config

    async def qemu_agent_ping(self, node: str, vmid: int) -> Dict[str, Any]:
        await self._tracked("ping", self.agent_delay)
        if self.agent_error:
            raise self.agent_error
        if not self.agent_ok:
            raise TimeoutError("agent did not answer")
        return {}


def _host(idx: int = 1, *, conn: str = "c1") -> Dict[str, Any]:
    return {
        "id": f"h{idx}",
        "name": f"web{idx}",
        "node": "pve1",
        "vmid": 100 + idx,
        "conn_id": conn,
    }


@pytest.fixture(autouse=True)
def clear_cache():
    ai_scope.reset_cache()
    yield
    ai_scope.reset_cache()


def _patch(monkeypatch, clients) -> None:
    """clients: 单个客户端，或 {conn_id: client}"""
    if isinstance(clients, dict):
        monkeypatch.setattr(
            ai_scope.pve, "client_for_connection", lambda conn_id: clients[conn_id]
        )
    else:
        monkeypatch.setattr(
            ai_scope.pve, "client_for_connection", lambda conn_id: clients
        )


# ------------------------------------------------------------------ 往返次数
def test_running_host_uses_two_round_trips(monkeypatch) -> None:
    """status 之后 config 与 ping 并行：一台跑通的机器是 2 次往返，不是 3 次。"""
    client = SlowClient(config_delay=0.08, agent_delay=0.08)
    _patch(monkeypatch, client)

    started = time.monotonic()
    ok, reason = asyncio.run(ai_scope.check_host(_host()))
    elapsed = time.monotonic() - started

    assert (ok, reason) == (True, "")
    assert client.calls == {"version": 0, "status": 1, "config": 1, "ping": 1}
    # 串行至少 0.16s；并行约 0.08s。留足余量，只判「明显并行」
    assert elapsed < 0.14, f"config/ping 没有并行：{elapsed:.3f}s"


def test_stopped_host_does_not_ask_config_or_agent(monkeypatch) -> None:
    """停机在 status 就能判死，不该再花两次往返去问配置与 agent。"""
    client = SlowClient(status="stopped")
    _patch(monkeypatch, client)
    assert asyncio.run(ai_scope.check_host(_host())) == (False, ai_scope.REASON_STOPPED)
    assert client.calls == {"version": 0, "status": 1, "config": 0, "ping": 0}


# ------------------------------------------------------- 批量取状态（1 次/连接）
def test_batch_status_replaces_per_host_status(monkeypatch) -> None:
    """N 台机器只问一次集群资源：不再逐台问 status，每台只剩一轮 config ∥ ping。"""
    client = BatchClient(
        statuses={100 + i: "running" for i in range(1, 7)},
        config_delay=0.05,
        agent_delay=0.05,
    )
    _patch(monkeypatch, client)
    hosts = [_host(i) for i in range(1, 7)]

    started = time.monotonic()
    eligible, _counts, _details = asyncio.run(
        ai_scope.evaluate_hosts_with_details(hosts)
    )
    elapsed = time.monotonic() - started

    assert len(eligible) == 6
    assert client.calls["resources"] == 1, "批量状态没有做到「一条连接一次」"
    assert client.calls["status"] == 0, "批量之后还在逐台问状态"
    assert client.calls["config"] == 6 and client.calls["ping"] == 6
    # 6 台一轮就够（每台一次往返）。串行会是 6 × 0.05s = 0.3s 以上
    assert elapsed < 0.22, f"没有并行到一轮往返：{elapsed:.3f}s"


def test_batch_status_settles_stopped_and_containers_for_free(monkeypatch) -> None:
    """停机 / 容器在批量结果里就能判死，不该再为它们花任何一次往返。"""
    client = BatchClient(statuses={101: "stopped", 102: "running"}, types={102: "lxc"})
    _patch(monkeypatch, client)

    eligible, counts, _details = asyncio.run(
        ai_scope.evaluate_hosts_with_details([_host(1), _host(2)])
    )

    assert eligible == []
    assert counts[ai_scope.REASON_STOPPED] == 1
    assert counts[ai_scope.REASON_NOT_VM] == 1
    assert client.calls["config"] == 0 and client.calls["ping"] == 0


def test_batch_miss_falls_back_to_single_status(monkeypatch) -> None:
    """批量里看不到的机器不能直接判「不是虚拟机」——那可能只是凭据看不到。

    退回逐台 status，才能分辨 404（确实不是虚拟机）与 403（权限不足）。
    """
    client = BatchClient(statuses={}, hidden={101})
    _patch(monkeypatch, client)

    eligible, _counts, _details = asyncio.run(
        ai_scope.evaluate_hosts_with_details([_host(1)])
    )
    assert client.calls["resources"] == 1
    assert client.calls["status"] == 1
    assert len(eligible) == 1

    # 逐台 status 报权限不足时按「读不到」处理，而不是「不是虚拟机」
    ai_scope.reset_cache()
    denied = BatchClient(
        statuses={}, status_error=ProxmoxError("denied", pve_status=403)
    )
    _patch(monkeypatch, denied)
    _eligible, counts, _details = asyncio.run(
        ai_scope.evaluate_hosts_with_details([_host(1)])
    )
    assert counts[ai_scope.REASON_UNREACHABLE] == 1


def test_batch_failure_marks_whole_connection_down(monkeypatch) -> None:
    """整条 PVE 读不到：一次失败就够了，不必再逐台等超时、也不必再单独探活。"""
    client = BatchClient(
        statuses={101: "running", 102: "running"},
        resources_error=RuntimeError("connection refused"),
    )
    _patch(monkeypatch, client)
    hosts = [_host(i) for i in range(1, 5)]

    eligible, counts, _details = asyncio.run(
        ai_scope.evaluate_hosts_with_details(hosts)
    )

    assert eligible == []
    assert counts[ai_scope.REASON_UNREACHABLE] == 4
    assert ai_scope.last_stats()["pve_down"] == 1
    assert client.calls["status"] == 0
    assert client.calls["version"] == 0

    # 几秒内不再为这条已经躺下的连接花任何往返（下一轮轮询最常见）
    asyncio.run(ai_scope.evaluate_hosts_with_details([_host(9)]))
    assert client.calls["resources"] == 1


def test_batch_permission_error_falls_back_to_single_status(monkeypatch) -> None:
    """凭据只授权到某几台虚拟机时，集群资源可能 403 —— 那不是「PVE 不可达」。

    整组判死会冤枉所有机器（它们逐台是读得到的），所以退回逐台 status。
    """
    client = BatchClient(
        statuses={}, resources_error=ProxmoxError("denied", pve_status=403)
    )
    _patch(monkeypatch, client)

    eligible, _counts, _details = asyncio.run(
        ai_scope.evaluate_hosts_with_details([_host(1)])
    )

    assert len(eligible) == 1
    assert ai_scope.last_stats()["pve_down"] == 0
    assert client.calls["status"] == 1


# ------------------------------------------------------------ 自适应并发（台数）
def test_fanout_scales_with_host_count(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_scan_concurrency", 12)
    monkeypatch.setattr(settings, "ai_scan_max_concurrency", 32)
    tune = ai_scope.tuning()

    assert ai_scope.fanout(tune, 0) == 1
    assert ai_scope.fanout(tune, 2) == 2
    assert ai_scope.fanout(tune, 12) == 12
    assert ai_scope.fanout(tune, 60) == 32
    # 上限不低于下限：把上限设得比下限还小，仍是固定并发（不会更慢也不会更凶）
    monkeypatch.setattr(settings, "ai_scan_max_concurrency", 3)
    assert ai_scope.fanout(ai_scope.tuning(), 60) == 12


def test_many_hosts_are_probed_in_a_few_waves(monkeypatch) -> None:
    """40 台机器：并发自适应到 32，两轮就探完 —— 不是 40 ÷ 12 的四轮。"""
    monkeypatch.setattr(settings, "ai_scan_concurrency", 12)
    monkeypatch.setattr(settings, "ai_scan_max_concurrency", 32)
    monkeypatch.setattr(settings, "ai_scan_conn_concurrency", 64)
    client = BatchClient(
        statuses={100 + i: "running" for i in range(1, 41)},
        config_delay=0.05,
        agent_delay=0.05,
    )
    _patch(monkeypatch, client)
    hosts = [_host(i) for i in range(1, 41)]

    started = time.monotonic()
    eligible, _counts, _details = asyncio.run(
        ai_scope.evaluate_hosts_with_details(hosts)
    )
    elapsed = time.monotonic() - started

    assert len(eligible) == 40
    assert ai_scope.last_stats()["concurrency"] == 32
    # 一轮 32 台、一轮 8 台 ≈ 0.1s；按 12 并发要 4 轮 ≈ 0.2s，串行要 2s
    assert client.max_probe_in_flight >= 20, f"并发没放大：{client.max_probe_in_flight}"
    assert elapsed < 0.35, f"台数一多就又排成队了：{elapsed:.3f}s"


# ------------------------------------------------------------------ 并发上限
def test_concurrency_setting_caps_parallel_probes(monkeypatch) -> None:
    """并发是自适应的（下限 → 上限），所以「限住」要同时钉下限与上限。"""
    monkeypatch.setattr(settings, "ai_scan_concurrency", 2)
    monkeypatch.setattr(settings, "ai_scan_max_concurrency", 2)
    client = SlowClient(status_delay=0.02)
    _patch(monkeypatch, client)
    hosts = [_host(i, conn=f"c{i}") for i in range(1, 7)]

    asyncio.run(ai_scope.evaluate_hosts_with_details(hosts))

    assert client.max_in_flight <= 2, f"并发没被限住：{client.max_in_flight}"
    assert client.calls["status"] == 6


def test_connection_gate_caps_single_pve(monkeypatch) -> None:
    """同一条 PVE 上同时在飞的数量受 ai_scan_conn_concurrency 约束。"""
    monkeypatch.setattr(settings, "ai_scan_concurrency", 8)
    monkeypatch.setattr(settings, "ai_scan_conn_concurrency", 2)
    client = SlowClient(status_delay=0.02)
    _patch(monkeypatch, client)
    hosts = [_host(i) for i in range(1, 7)]  # 同一条连接

    asyncio.run(ai_scope.evaluate_hosts_with_details(hosts))

    assert client.max_in_flight <= 2, f"单条 PVE 上并发超了：{client.max_in_flight}"


# ------------------------------------------------------------------ 连不上的 PVE
def test_dead_connection_is_probed_once(monkeypatch) -> None:
    """一条 PVE 连不上：整组只探活一次，不逐台等三个超时。"""
    client = SlowClient(version_error=RuntimeError("connection refused"))
    _patch(monkeypatch, client)
    hosts = [_host(i) for i in range(1, 5)]

    eligible, counts, _details = asyncio.run(
        ai_scope.evaluate_hosts_with_details(hosts)
    )

    assert eligible == []
    assert client.calls["version"] == 1
    assert client.calls["status"] == 0
    assert counts[ai_scope.REASON_UNREACHABLE] == 4
    assert ai_scope.last_stats()["pve_down"] == 1


def test_single_host_skips_health_preflight(monkeypatch) -> None:
    """只有一台机器时不探活：多一次往返、零收益。"""
    client = SlowClient()
    _patch(monkeypatch, client)
    asyncio.run(ai_scope.evaluate_hosts_with_details([_host()]))
    assert client.calls["version"] == 0


# ------------------------------------------------------------------ 重试策略
def test_transient_timeout_is_retried(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_scan_retries", 1)
    client = SlowClient(status_error=TimeoutError("pve slow"), status_error_once=True)
    _patch(monkeypatch, client)

    assert asyncio.run(ai_scope.check_host(_host())) == (True, "")
    assert client.calls["status"] == 2


def test_404_is_not_retried(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_scan_retries", 3)
    client = SlowClient(status_error=ProxmoxError("no such VM", pve_status=404))
    _patch(monkeypatch, client)

    assert asyncio.run(ai_scope.check_host(_host())) == (False, ai_scope.REASON_NOT_VM)
    assert client.calls["status"] == 1


def test_permission_error_is_not_retried(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_scan_retries", 3)
    client = SlowClient(status_error=ProxmoxError("denied", pve_status=403))
    _patch(monkeypatch, client)

    assert asyncio.run(ai_scope.check_host(_host())) == (
        False,
        ai_scope.REASON_UNREACHABLE,
    )
    assert client.calls["status"] == 1


def test_agent_timeout_is_not_retried(monkeypatch) -> None:
    """agent 不应答是客户机里的事实，重试只把 3 秒拖成 6 秒。"""
    monkeypatch.setattr(settings, "ai_scan_retries", 3)
    client = SlowClient(agent_ok=False)
    _patch(monkeypatch, client)

    assert asyncio.run(ai_scope.check_host(_host())) == (False, ai_scope.REASON_AGENT)
    assert client.calls["ping"] == 1


def test_agent_pve_error_is_reported_as_unreachable(monkeypatch) -> None:
    """PVE 打嗝导致的 ping 失败不能报成「Guest Agent 未响应」——
    那会让用户去客户机里白找一圈。"""
    client = SlowClient(agent_error=ProxmoxError("bad gateway", pve_status=502))
    _patch(monkeypatch, client)

    assert asyncio.run(ai_scope.check_host(_host())) == (
        False,
        ai_scope.REASON_UNREACHABLE,
    )


def test_disabled_retry_means_single_attempt(monkeypatch) -> None:
    """``ai_scan_retries=0`` 是合法配置，不能被「0 视同缺省」吞掉。"""
    monkeypatch.setattr(settings, "ai_scan_retries", 0)
    client = SlowClient(status_error=TimeoutError("pve slow"))
    _patch(monkeypatch, client)

    assert asyncio.run(ai_scope.check_host(_host())) == (
        False,
        ai_scope.REASON_UNREACHABLE,
    )
    assert client.calls["status"] == 1


# ------------------------------------------------------------------ 缓存批量读写
def test_shared_cache_is_read_and_written_in_batches(monkeypatch) -> None:
    """N 台机器 = 1 次批量读 + 1 次批量写，而不是 N 次。"""
    reads: List[int] = []
    writes: List[List[Any]] = []

    async def fake_load(keys, ttl):
        reads.append(len(keys))
        return {}

    async def fake_save(items):
        writes.append(list(items))

    monkeypatch.setattr(ai_scope, "_load_shared_many", fake_load)
    monkeypatch.setattr(ai_scope, "_save_shared_many", fake_save)

    client = SlowClient()
    _patch(monkeypatch, client)
    hosts = [_host(i, conn=f"c{i}") for i in range(1, 6)]

    asyncio.run(ai_scope.evaluate_hosts_with_details(hosts))

    assert reads == [5], f"共享缓存没有整批读：{reads}"
    assert len(writes) == 1 and len(writes[0]) == 5, f"共享缓存没有整批写：{writes}"


def test_second_scan_within_ttl_makes_no_pve_calls(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_scan_cache_ttl", 30.0)
    client = SlowClient()
    _patch(monkeypatch, client)
    hosts = [_host(1), _host(2)]

    async def twice():
        await ai_scope.evaluate_hosts_with_details(hosts)
        before = dict(client.calls)
        await ai_scope.evaluate_hosts_with_details(hosts)
        return before

    before = asyncio.run(twice())
    assert before == client.calls, "TTL 之内的第二轮又去打了 PVE"


def test_cached_scan_skips_batch_status(monkeypatch) -> None:
    """TTL 之内连批量状态都不问：轮询不该每两秒白打一次 PVE。"""
    monkeypatch.setattr(settings, "ai_scan_cache_ttl", 30.0)
    client = BatchClient(statuses={101: "running", 102: "running"})
    _patch(monkeypatch, client)
    hosts = [_host(1), _host(2)]

    async def twice():
        await ai_scope.evaluate_hosts_with_details(hosts)
        await ai_scope.evaluate_hosts_with_details(hosts)
        # 首屏那条路也只读缓存：不该再问一次批量状态
        _eligible, _counts, _details, pending = await ai_scope.evaluate_hosts_fast(hosts)
        assert pending == 0

    asyncio.run(twice())

    assert client.calls["resources"] == 1, "有结论的机器又被打了一轮批量状态"


def test_cache_ttl_setting_is_honoured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_scan_cache_ttl", 0.1)
    client = SlowClient()
    _patch(monkeypatch, client)

    async def probe_twice():
        await ai_scope.check_host(_host())
        await asyncio.sleep(0.15)
        await ai_scope.check_host(_host())

    asyncio.run(probe_twice())
    assert client.calls["status"] == 2


# ------------------------------------------------------------------ 预算 / pending
def test_budget_returns_partial_results(monkeypatch) -> None:
    """预算到点先回已探完的，剩下的如实报 pending。"""
    # 1/1 = 串行：预算要能看出「排到后面的没轮到」，就得让它们真的排队
    monkeypatch.setattr(settings, "ai_scan_concurrency", 1)
    monkeypatch.setattr(settings, "ai_scan_max_concurrency", 1)
    client = SlowClient(status_delay=0.1, config_delay=0.1, agent_delay=0.1)
    _patch(monkeypatch, client)
    hosts = [_host(i, conn=f"c{i}") for i in range(1, 5)]

    eligible, counts, _details = asyncio.run(
        ai_scope.evaluate_hosts_with_details(hosts, budget_s=0.05)
    )

    assert len(eligible) == 1
    assert counts.get("pending") == 3


def test_probe_failure_still_settles(monkeypatch) -> None:
    """探测自己炸了也要落结论，否则前端会永远 pending、每 2 秒问一次。"""

    async def boom(host, tune):
        raise RuntimeError("boom")

    monkeypatch.setattr(ai_scope, "_check_uncached", boom)
    _patch(monkeypatch, SlowClient())

    async def twice():
        first = await ai_scope.check_host(_host())
        second = await ai_scope.check_host(_host())
        return first, second

    assert asyncio.run(twice()) == (
        (False, ai_scope.REASON_UNREACHABLE),
        (False, ai_scope.REASON_UNREACHABLE),
    )


def test_fast_mode_reads_cache_once_for_all_hosts(monkeypatch) -> None:
    """首屏轮询：整批一次读缓存，不随台数线性增长。"""
    reads: List[int] = []

    async def fake_load(keys, ttl):
        reads.append(len(keys))
        return {}

    monkeypatch.setattr(ai_scope, "_load_shared_many", fake_load)
    hosts = [_host(i, conn=f"c{i}") for i in range(1, 6)]

    eligible, counts, _details, pending = asyncio.run(
        ai_scope.evaluate_hosts_fast(hosts)
    )

    assert eligible == []
    assert pending == 5
    # 后台补探会再读一次，但**每次都是整批**——永远不该出现「1 台 1 条」
    assert reads and set(reads) == {5}, f"出现了逐台读缓存：{reads}"


def test_inline_threshold_decides_whether_to_wait(monkeypatch) -> None:
    """候选很少时当场探完，多时先出列表 —— 这条线的取舍不能悄悄改回去。

    「先出列表、后台补探」要等前端下一次轮询才有结论，2 台机器上那一个轮询
    周期（约 2 秒）就是用户看到的「慢」，而探测本身只要几百毫秒。
    """
    from app.routers.ai import _scan_inline

    monkeypatch.setattr(settings, "ai_scan_inline_max", 8)
    assert _scan_inline(0)
    assert _scan_inline(2)
    assert _scan_inline(8)
    assert not _scan_inline(9)

    # 0 = 一律不当场探（大集群 / 反代超时很紧的部署可以这么关掉）
    monkeypatch.setattr(settings, "ai_scan_inline_max", 0)
    assert not _scan_inline(1)

    # 脏值退回「不当场探」：最坏只是慢，不能把首屏挂死在探测上
    monkeypatch.setattr(settings, "ai_scan_inline_max", "abc")
    assert not _scan_inline(1)
    monkeypatch.setattr(settings, "ai_scan_inline_max", None)
    assert not _scan_inline(1)


def test_tuning_falls_back_on_bad_values(monkeypatch) -> None:
    """运维把 .env 写成 0 / 负数 / 字符串，不该让扫描 500。"""
    monkeypatch.setattr(settings, "ai_scan_concurrency", 0)
    monkeypatch.setattr(settings, "ai_scan_timeout", "abc")
    monkeypatch.setattr(settings, "ai_scan_retries", -3)

    tune = ai_scope.tuning()
    assert tune.concurrency >= 1
    assert tune.timeout >= 0.5
    assert tune.retries == 0
