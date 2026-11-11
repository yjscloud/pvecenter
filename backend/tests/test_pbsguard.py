"""PBS 运行状态巡检（GC 失败 / 磁盘健康 / 备份盘容量）的判定口径。

只测 ``scan_failures``：它是“什么算故障”的唯一入口；``evaluate`` 的通知流程
与 :mod:`app.verifyguard` 同构，重复测试只会增加维护成本。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import pbs, pbsguard  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _conn(**over: Any) -> Dict[str, Any]:
    base = {
        "id": "a",
        "name": "pbs-01",
        "host": "pbs.lan",
        "port": 8007,
        "token_id": "root@pam!panel",
        "token_secret": "secret",
    }
    base.update(over)
    return base


class _FakePbs:
    def __init__(
        self,
        conn: pbs.PbsConnection,
        *,
        tasks: Optional[List[Dict[str, Any]]] = None,
        disks: Any = None,
        nodes_error: bool = False,
        stores: Optional[List[str]] = None,
        status: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        self.conn = conn
        self._tasks = tasks or []
        self._disks = disks if disks is not None else {"devices": []}
        self._nodes_error = nodes_error
        self._stores = stores or []
        self._status = status or {}

    async def nodes(self) -> List[Dict[str, Any]]:
        if self._nodes_error:
            raise RuntimeError("unreachable")
        return [{"node": "pbs"}]

    async def tasks(self, node: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self._tasks

    async def disks(self, node: str) -> Any:
        return self._disks

    async def datastores(self) -> List[Dict[str, Any]]:
        return [{"store": name} for name in self._stores]

    async def datastore_status(self, name: str) -> Dict[str, Any]:
        # 缺省返回空字典：total=0 → level=unknown → 不告警（模拟读不到容量）
        return self._status.get(name, {})

    async def aclose(self) -> None:
        return None


def _patch(monkeypatch, factory) -> None:
    async def fake_load():
        return [_conn()]

    monkeypatch.setattr(pbs, "load_connections", fake_load)
    monkeypatch.setattr(pbs, "PbsClient", factory)


def _gc(status: str, starttime: int = 100, endtime: int = 200) -> Dict[str, Any]:
    return {
        "upid": f"UPID:pbs:1:1:1:garbage_collection:0:root@pam:{status}",
        "worker_type": "garbage_collection",
        "status": status,
        "starttime": starttime,
        "endtime": endtime,
    }


def test_gc_failure_is_reported(monkeypatch) -> None:
    _patch(
        monkeypatch,
        lambda conn: _FakePbs(conn, tasks=[_gc("unable to lock datastore")]),
    )
    failures = _run(pbsguard.scan_failures())
    assert len(failures) == 1
    assert failures[0]["kind"] == "gc"
    assert failures[0]["key"] == "gc:pbs.lan:pbs"


def test_successful_gc_is_silent(monkeypatch) -> None:
    _patch(monkeypatch, lambda conn: _FakePbs(conn, tasks=[_gc("OK")]))
    assert _run(pbsguard.scan_failures()) == []


def test_running_gc_is_not_a_failure(monkeypatch) -> None:
    """endtime 为 0 表示还在跑，empty status 不能当失败报出来。"""
    _patch(
        monkeypatch,
        lambda conn: _FakePbs(conn, tasks=[_gc("", starttime=300, endtime=0)]),
    )
    assert _run(pbsguard.scan_failures()) == []


def test_only_latest_gc_counts(monkeypatch) -> None:
    _patch(
        monkeypatch,
        lambda conn: _FakePbs(
            conn,
            tasks=[
                _gc("failed", starttime=100, endtime=200),
                _gc("OK", starttime=300, endtime=400),
            ],
        ),
    )
    assert _run(pbsguard.scan_failures()) == []


def test_unhealthy_disk_is_reported(monkeypatch) -> None:
    _patch(
        monkeypatch,
        lambda conn: _FakePbs(
            conn,
            disks={
                "devices": [
                    {"devpath": "/dev/sda", "health": "PASSED"},
                    {"devpath": "/dev/sdb", "health": "FAILED"},
                ]
            },
        ),
    )
    failures = _run(pbsguard.scan_failures())
    assert [item["key"] for item in failures] == ["disk:pbs.lan:/dev/sdb"]
    assert failures[0]["kind"] == "disk"


_GB = 1024 ** 3


def _capacity_pbs(used_gb: float, total_gb: float = 100.0) -> Any:
    def factory(conn: pbs.PbsConnection) -> _FakePbs:
        return _FakePbs(
            conn,
            stores=["backup"],
            status={
                "backup": {
                    "total": int(total_gb * _GB),
                    "used": int(used_gb * _GB),
                    "avail": int((total_gb - used_gb) * _GB),
                }
            },
        )

    return factory


def test_capacity_warn_is_reported(monkeypatch) -> None:
    """越过 85% 报 warn，阈值与告警页显示的百分比都取自同一处计算。"""
    _patch(monkeypatch, _capacity_pbs(used_gb=88))
    failures = _run(pbsguard.scan_failures())
    assert len(failures) == 1
    item = failures[0]
    assert item["kind"] == "capacity"
    assert item["key"] == "capacity:pbs.lan:backup"
    assert item["store"] == "backup"
    assert item["level"] == "warn"
    assert item["threshold"] == 85.0
    assert item["percent"] == 88.0


def test_capacity_crit_is_reported(monkeypatch) -> None:
    _patch(monkeypatch, _capacity_pbs(used_gb=96))
    failures = _run(pbsguard.scan_failures())
    assert [item["level"] for item in failures] == ["crit"]
    assert failures[0]["threshold"] == 95.0


def test_capacity_below_warn_is_silent(monkeypatch) -> None:
    _patch(monkeypatch, _capacity_pbs(used_gb=40))
    assert _run(pbsguard.scan_failures()) == []


def test_unknown_capacity_is_silent(monkeypatch) -> None:
    """读不到容量（403 / 404 / 老版本）时不报：叫「快满了」的告警必须是真数字。"""

    def factory(conn: pbs.PbsConnection) -> _FakePbs:
        return _FakePbs(conn, stores=["backup"], status={})

    _patch(monkeypatch, factory)
    assert _run(pbsguard.scan_failures()) == []


def test_capacity_alert_text_has_type_time_and_target() -> None:
    """告警正文三件套：异常类型、发生时间、受影响备份对象。"""
    item = {
        "kind": "capacity",
        "host": "pbs.lan",
        "node": "pbs",
        "store": "backup",
        "level": "crit",
        "percent": 96.0,
        "threshold": 95.0,
        "used": 96 * _GB,
        "total": 100 * _GB,
        "free": 4 * _GB,
    }
    text = pbsguard._alert_text(item, 1_700_000_000)
    assert "备份盘空间不足" in text
    assert "pbs.lan / backup" in text
    assert "96.0%" in text
    assert "触发时间" in text
    assert pbsguard._subject(item) == "pbs.lan / backup"


def test_unreachable_pbs_is_silent(monkeypatch) -> None:
    """连不上 PBS 属于“读取失败”，不该报成 GC/磁盘故障。"""
    _patch(monkeypatch, lambda conn: _FakePbs(conn, nodes_error=True))
    assert _run(pbsguard.scan_failures()) == []


def test_unconfigured_connection_is_skipped(monkeypatch) -> None:
    async def fake_load():
        return [_conn(token_secret="")]

    monkeypatch.setattr(pbs, "load_connections", fake_load)
    monkeypatch.setattr(pbs, "PbsClient", lambda conn: _FakePbs(conn))
    assert _run(pbsguard.scan_failures()) == []
