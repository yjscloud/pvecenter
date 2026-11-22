"""AI 助手「整机快照回滚」的结果回执。

回滚在 PVE 侧是**异步任务**：POST 返回 UPID 只代表受理。存储不支持快照、快照
已被删除、VM 卡在磁盘锁上 —— 失败都发生在任务里。所以这里锁死三件事：

1. 成功必须等到任务到达终态，且 ``pending`` 不出现；
2. 失败要带 PVE 任务日志给出的具体原因（不能只说「失败了」）；
3. 等待超时只能说「未确认」，既不能报成功，也不能报失败。

    ../.venv/bin/python -m pytest tests/test_aivm_rollback.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import aivm  # noqa: E402
from app.pve import ProxmoxError  # noqa: E402

LEASE = {
    "id": "lease-1",
    "username": "alice",
    "node": "pve1",
    "vmid": 100,
    "guest_type": "qemu",
    "snapshot": "pvecenter-ai-lease-1",
}

UPID = "UPID:pve1:1:1:1:qmrollback:100:root@pam:"


def run(coro):
    return asyncio.run(coro)


class FakeClient:
    """只实现回滚用到的那几个方法，其余一律不碰。"""

    def __init__(self) -> None:
        self.exitstatus = "OK"
        self.log: list = []
        self.submit_error: Exception | None = None
        self.wait_error: Exception | None = None
        self.waited: list = []
        self.logged: list = []

    async def qemu_snapshot_rollback(self, node, vmid, name):
        if self.submit_error is not None:
            raise self.submit_error
        return UPID

    async def wait_for_task(self, upid, node=None, timeout=0):
        self.waited.append((upid, node, timeout))
        if self.wait_error is not None:
            raise self.wait_error
        return {"status": "stopped", "exitstatus": self.exitstatus}

    async def task_log(self, upid, node=None, limit=0):
        self.logged.append(upid)
        return self.log


@pytest.fixture()
def wired(monkeypatch):
    """把回滚与数据库 / 真实 PVE 拆开，只留下被测的分支逻辑。"""
    client = FakeClient()
    records: list = []

    async def fake_bump(_lease_id):
        return 1

    async def fake_record(_lease, **kw):
        records.append(kw)
        return "action-1"

    async def fake_client(_lease):
        return client

    monkeypatch.setattr(aivm, "_bump_actions", fake_bump)
    monkeypatch.setattr(aivm, "_record", fake_record)
    monkeypatch.setattr(aivm, "_client_for", fake_client)
    return client, records


def test_snapshot_rollback_waits_for_task_and_reports_success(wired):
    client, records = wired
    result = run(aivm.rollback(LEASE, mode="snapshot"))

    assert result["ok"] is True
    assert "pending" not in result
    assert "pvecenter-ai-lease-1" in result["output"]
    # 必须等任务结束再回话，不能拿「已受理」当「已回滚」
    assert client.waited == [(UPID, "pve1", aivm.SNAPSHOT_ROLLBACK_TIMEOUT)]
    assert records[-1]["ok"] is True


def test_snapshot_rollback_warnings_still_count_as_success(wired):
    """``WARNINGS: n`` 是完成，不是失败（PVE 的常见终态）。"""
    client, _ = wired
    client.exitstatus = "WARNINGS: 1"
    result = run(aivm.rollback(LEASE, mode="snapshot"))
    assert result["ok"] is True


def test_snapshot_rollback_reports_pve_reason(wired):
    client, records = wired
    client.exitstatus = "command failed"
    client.log = [
        {"t": "starting rollback"},
        {"t": "TASK ERROR: storage 'local' does not support snapshots"},
    ]
    result = run(aivm.rollback(LEASE, mode="snapshot"))

    assert result["ok"] is False
    assert not result.get("pending")
    # 原因必须具体到「存储不支持」，而不是一句「回滚失败」
    assert "does not support snapshots" in result["output"]
    assert "回滚失败" in result["output"] or "Rollback failed" in result["output"]
    assert client.logged == [UPID]
    assert records[-1]["ok"] is False


def test_snapshot_rollback_timeout_is_unconfirmed_not_failure(wired):
    client, records = wired
    client.wait_error = ProxmoxError("Task did not finish within 900s", 504)
    result = run(aivm.rollback(LEASE, mode="snapshot"))

    assert result["ok"] is False
    assert result.get("pending") is True
    assert "未确认" in result["output"] or "unconfirmed" in result["output"]
    # 带上 UPID，用户才能去任务页自行核对
    assert UPID in result["output"]
    assert records[-1]["ok"] is False


def test_snapshot_rollback_submit_error_becomes_readable_receipt(wired):
    """PVE 直接拒掉（快照不存在 / 权限不足）时也要有可读回执，不能冒成 500。"""
    client, records = wired
    client.submit_error = ProxmoxError("snapshot 'pvecenter-ai-lease-1' does not exist", 400)
    result = run(aivm.rollback(LEASE, mode="snapshot"))

    assert result["ok"] is False
    assert "does not exist" in result["output"]
    assert records[-1]["ok"] is False


def test_snapshot_rollback_without_snapshot_is_rejected(wired):
    _, records = wired
    result = run(
        aivm.rollback({**LEASE, "snapshot": ""}, mode="snapshot")
    )
    assert result["ok"] is False
    assert "快照" in result["output"] or "snapshot" in result["output"]
    assert records[-1]["ok"] is False
