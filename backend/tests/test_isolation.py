"""应急响应「一键隔离」的回归用例。

重点覆盖两类曾经把功能整个打挂的问题：

1. **快照锁**：PVE 建快照是异步任务，任务期间 VM 带 ``snapshot`` 锁，此时改
   ``netX`` 会被拒（``VM is locked (snapshot)``）。而「先取证快照」是默认选项，
   于是每一次隔离都在断网那一步失败。
2. **错误文案炸成 500**：失败项模板是 ``"{key}（{error}）"``，而 ``i18n.t`` 的
   形参也叫 ``key``，``t(..., key=...)`` 直接 ``TypeError`` —— 一句本该可读的
   「net0（VM is locked）」变成了「服务器内部错误」。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import i18n, isolation  # noqa: E402
from app.pve import ProxmoxError  # noqa: E402
from app.routers import isolation as isolation_router  # noqa: E402
from app.schemas import QuarantineIn  # noqa: E402

BASE_CONFIG = {
    "name": "test-01",
    "net0": "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0,firewall=1",
    "net1": "virtio=AA:BB:CC:DD:EE:02,bridge=vmbr1,tag=10",
}


@pytest.fixture(autouse=True)
def _zh_language():
    i18n.pin_language("zh-CN")


class FakePve:
    """够隔离流程用的最小 PVE 客户端。

    ``after_wait_locked``：等待快照任务结束后仍然残留的锁次数（模拟备份 / 迁移
    抢占）。快照任务没结束前改配置**一律**被拒 —— 那正是线上 500 的时序。
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        after_wait_locked: int = 0,
        snapshot_upid: Optional[str] = "UPID:pve:0001:qmsnapshot:107:root@pam:",
        snapshot_exit: str = "OK",
        power_error: Optional[str] = None,
    ):
        self.config = dict(config or BASE_CONFIG)
        self.after_wait_locked = after_wait_locked
        self.snapshot_upid = snapshot_upid
        self.snapshot_exit = snapshot_exit
        self.power_error = power_error
        self.calls: List[Any] = []
        self.waited = False
        self.set_configs: List[Dict[str, Any]] = []

    async def qemu_config(self, node: str, vmid: int) -> Dict[str, Any]:
        self.calls.append(("config",))
        return dict(self.config)

    async def qemu_snapshot_create(
        self, node: str, vmid: int, name: str, description: str = ""
    ) -> Any:
        self.calls.append(("snapshot", name))
        return self.snapshot_upid

    async def wait_for_task(
        self, upid: str, node: Optional[str] = None, timeout: float = 300.0
    ) -> Dict[str, Any]:
        self.calls.append(("wait", upid))
        self.waited = True
        return {"status": "stopped", "exitstatus": self.snapshot_exit}

    async def qemu_set_config(
        self, node: str, vmid: int, data: Dict[str, Any]
    ) -> Any:
        self.calls.append(("set_config", dict(data)))
        if self.snapshot_upid and not self.waited:
            raise ProxmoxError("VM is locked (snapshot)", 500)
        if self.after_wait_locked > 0:
            self.after_wait_locked -= 1
            raise ProxmoxError("VM is locked (snapshot)", 500)
        self.set_configs.append(dict(data))
        self.config.update(
            {key: value for key, value in data.items() if str(key).startswith("net")}
        )
        return "UPID:pve:0002:qmconfig:107:root@pam:"

    async def qemu_power(self, node: str, vmid: int, action: str) -> Any:
        self.calls.append(("power", action))
        if self.power_error:
            raise ProxmoxError(self.power_error, 500)
        return "UPID:pve:0003:qmpower:107:root@pam:"


@pytest.fixture()
def fake(monkeypatch) -> FakePve:
    client = FakePve()
    monkeypatch.setattr(isolation, "get_client", lambda: client)
    # 重试真的睡 1.5 秒会把测试拖成分钟级；这里只验证「重试了」，不等真时间
    monkeypatch.setattr(isolation, "LOCK_RETRY_DELAY", 0)
    return client


def _steps_by_name(result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {item["step"]: item for item in result["steps"]}


# ---------------------------------------------------------------- i18n 形参冲突


def test_message_can_use_a_key_variable():
    """``{key}`` 模板 + ``key=`` 变量：这是当初 500 的触发点。

    ``i18n.t`` 的第一个形参曾是可被关键字覆盖的 ``key``，调用方把网卡名写成
    ``key=`` 就把形参绑走了，Python 抛 ``TypeError: t() got multiple values``。
    """
    text = i18n.t("isolation.failed_item", key="net0", error="VM is locked (snapshot)")
    assert text.startswith("net0")
    assert "VM is locked" in text


# ------------------------------------------------------------------ net 串编辑


def test_set_link_down_keeps_other_attributes():
    raw = "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0,firewall=1"
    down = isolation.set_link_down(raw, True)
    assert down.endswith("link_down=1")
    assert "bridge=vmbr0" in down and "firewall=1" in down
    assert isolation.is_link_down(down) is True
    # 再解开会把 link_down 去掉、其余原样保留
    assert isolation.set_link_down(down, False) == raw
    assert isolation.is_link_down(raw) is False


def test_set_link_down_is_idempotent():
    raw = "virtio=AA,bridge=vmbr0"
    once = isolation.set_link_down(raw, True)
    assert isolation.set_link_down(once, True).count("link_down=1") == 1


# ------------------------------------------------------------------ 一键隔离


def test_quarantine_waits_for_snapshot_before_cutting_network(fake):
    result = asyncio.run(isolation.quarantine("pve", 107))

    assert result["ok"] is True, result
    order = [call[0] for call in fake.calls]
    # 关键顺序：快照 → 等任务结束（锁释放）→ 改配置。
    # 少了中间的「等」，线上就是 500 "VM is locked (snapshot)"。
    assert order.index("snapshot") < order.index("wait") < order.index("set_config")
    assert fake.waited is True

    steps = _steps_by_name(result)
    assert steps["snapshot"]["ok"] is True
    assert steps["network"]["ok"] is True
    assert sorted(result["interfaces"]) == ["net0", "net1"]
    assert steps["power"]["ok"] is True
    assert steps["protect"]["ok"] is True
    # 断网写入的是 link_down=1，且没有丢掉 bridge/firewall
    cut = [item for item in fake.set_configs if "net0" in item]
    assert cut and "link_down=1" in cut[0]["net0"]
    assert "bridge=vmbr0" in cut[0]["net0"]
    # 保护位
    assert {"protection": 1} in fake.set_configs


def test_quarantine_retries_while_vm_is_still_locked(fake):
    """等待结束后仍被别的任务短暂占用：重试几次能过，而不是直接失败。"""
    fake.after_wait_locked = 2
    result = asyncio.run(isolation.quarantine("pve", 107))

    assert result["ok"] is True, result
    assert _steps_by_name(result)["network"]["ok"] is True


def test_quarantine_reports_lock_with_readable_message(monkeypatch):
    """一直锁着也不能 500：断网步骤失败，但整条流程照常返回结果。"""
    client = FakePve(after_wait_locked=10_000)
    monkeypatch.setattr(isolation, "get_client", lambda: client)
    monkeypatch.setattr(isolation, "LOCK_RETRY_DELAY", 0)

    result = asyncio.run(isolation.quarantine("pve", 107))

    steps = _steps_by_name(result)
    assert result["ok"] is False
    assert steps["snapshot"]["ok"] is True
    assert steps["network"]["ok"] is False
    detail = steps["network"]["detail"]
    assert "net0" in detail
    assert "任务" in detail  # friendly_error 把 locked 翻成了人话
    assert "VM is locked" in detail  # 原始错误仍保留，便于排查
    # 电源动作不受配置锁影响，照常执行；保护位同样是改配置，同样被锁挡住
    assert steps["power"]["ok"] is True
    assert steps["protect"]["ok"] is False
    assert "任务" in steps["protect"]["detail"]


def test_quarantine_continues_when_snapshot_task_fails(monkeypatch):
    client = FakePve(snapshot_exit="snapshot feature is not available")
    monkeypatch.setattr(isolation, "get_client", lambda: client)
    monkeypatch.setattr(isolation, "LOCK_RETRY_DELAY", 0)

    result = asyncio.run(isolation.quarantine("pve", 107))

    steps = _steps_by_name(result)
    assert steps["snapshot"]["ok"] is False
    assert "snapshot feature is not available" in steps["snapshot"]["detail"]
    # 取证失败不能阻止处置：网照样断
    assert steps["network"]["ok"] is True
    assert result["ok"] is False


def test_snapshot_task_warnings_are_not_failures(monkeypatch):
    """PVE 任务跑完但有告警时 exitstatus 是 "WARNINGS: n"，不该算失败。"""
    client = FakePve(snapshot_exit="WARNINGS: 1")
    monkeypatch.setattr(isolation, "get_client", lambda: client)
    monkeypatch.setattr(isolation, "LOCK_RETRY_DELAY", 0)

    result = asyncio.run(isolation.quarantine("pve", 107))

    assert _steps_by_name(result)["snapshot"]["ok"] is True
    assert result["ok"] is True


def test_quarantine_without_snapshot_does_not_wait(monkeypatch):
    client = FakePve(snapshot_upid=None)
    monkeypatch.setattr(isolation, "get_client", lambda: client)
    monkeypatch.setattr(isolation, "LOCK_RETRY_DELAY", 0)

    result = asyncio.run(isolation.quarantine("pve", 107, take_snapshot=False))

    assert result["ok"] is True
    assert not [call for call in client.calls if call[0] in ("snapshot", "wait")]


def test_quarantine_power_failure_is_reported_not_raised(fake):
    fake.power_error = "VM is locked (backup)"
    result = asyncio.run(isolation.quarantine("pve", 107))

    steps = _steps_by_name(result)
    assert steps["power"]["ok"] is False
    assert "任务" in steps["power"]["detail"]
    assert result["ok"] is False


# ------------------------------------------------------------------ 解除隔离


def test_release_restores_network_and_protection(monkeypatch):
    config = {
        "net0": "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0,link_down=1",
        "net1": "virtio=AA:BB:CC:DD:EE:02,bridge=vmbr1,link_down=1",
        "protection": 1,
    }
    client = FakePve(config, snapshot_upid=None)
    monkeypatch.setattr(isolation, "get_client", lambda: client)
    monkeypatch.setattr(isolation, "LOCK_RETRY_DELAY", 0)

    result = asyncio.run(isolation.release("pve", 107, power_on=True))

    assert result["ok"] is True, result
    assert _steps_by_name(result)["network"]["ok"] is True
    assert {"protection": 0} in client.set_configs
    assert ("power", "start") in client.calls
    restored = [item for item in client.set_configs if "net0" in item]
    assert restored and "link_down" not in restored[0]["net0"]


# ------------------------------------------------------- 路由层：不再冒 500


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/vms/pve/107/quarantine",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "server": ("test", 80),
        }
    )


def test_route_turns_unexpected_error_into_readable_message(monkeypatch):
    """兜底：任何未预期异常都返回可读提示，而不是「服务器内部错误」。"""

    async def _boom(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise TypeError("t() got multiple values for argument 'key'")

    async def _noop_audit(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(isolation_router.isolation, "quarantine", _boom)
    monkeypatch.setattr(isolation_router.security, "audit", _noop_audit)

    payload = QuarantineIn(
        snapshot=True, cut_network=True, power_action="shutdown", protect=True, note=""
    )
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            isolation_router.quarantine_vm(
                node="pve",
                vmid=107,
                payload=payload,
                request=_request(),
                user={"username": "alice"},
                _step_up={},
            )
        )

    exc = caught.value
    assert exc.status_code == 502
    assert "服务器内部错误" not in str(exc.detail)
    assert "未预期" in str(exc.detail)
