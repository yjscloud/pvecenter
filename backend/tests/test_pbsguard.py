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

from app import alerting, pbs, pbsguard  # noqa: E402


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


# ------------------------------------------------------------------ 暂停接入


def test_paused_connection_is_not_probed(monkeypatch) -> None:
    """暂停接入 = 采集停了：与 pbs._collect_all 同口径，连请求都不该发。"""

    async def fake_load():
        return [_conn(paused=True)]

    def boom(conn: pbs.PbsConnection) -> Any:
        raise AssertionError("暂停的连接不该被探测")

    monkeypatch.setattr(pbs, "load_connections", fake_load)
    monkeypatch.setattr(pbs, "PbsClient", boom)
    assert _run(pbsguard.scan_failures()) == []


def test_paused_hosts_are_returned_to_the_caller(monkeypatch) -> None:
    """暂停名单要单独带出来：否则 evaluate 会把「停用」当「恢复」发通知。"""

    async def fake_load():
        return [_conn(paused=True)]

    monkeypatch.setattr(pbs, "load_connections", fake_load)
    failures, paused = _run(pbsguard.scan())
    assert failures == []
    assert paused == {"pbs.lan"}


def test_paused_host_with_a_live_twin_is_not_suppressed(monkeypatch) -> None:
    """同一台 PBS 登记两条、只暂停一条时，另一条还在采集，不能压制它的告警。"""

    async def fake_load():
        return [_conn(id="a", paused=True), _conn(id="b")]

    monkeypatch.setattr(pbs, "load_connections", fake_load)
    monkeypatch.setattr(pbs, "PbsClient", lambda conn: _FakePbs(conn))
    failures, paused = _run(pbsguard.scan())
    assert failures == []
    assert paused == set()


def test_subject_host_handles_every_subject_shape() -> None:
    assert pbsguard._subject_host("pbs.lan / backup") == "pbs.lan"
    assert pbsguard._subject_host("pbs.lan / /dev/sdb") == "pbs.lan"
    assert pbsguard._subject_host("pbs.lan") == "pbs.lan"


class _Stub:
    """把 evaluate 要碰的告警侧函数记下来，跑完断言「发了什么、清了什么」。"""

    def __init__(self, active: Optional[Dict[str, Any]] = None) -> None:
        self.active: Dict[str, Any] = dict(active or {})
        self.dispatched: List[Dict[str, Any]] = []
        self.marked: List[str] = []
        self.cleared: List[str] = []
        self.recover_calls: List[str] = []

    async def load_active(self) -> Dict[str, Any]:
        return dict(self.active)

    async def dispatch(self, owner, feishu, email_cfg, title, text, card, **kw):
        self.dispatched.append({"title": title, "text": text, "source": kw.get("source")})
        return True, "sent"

    async def record(self, entry, *, source="", repeat=False, channels=None):
        return None

    async def mark_active(self, key, row):
        self.marked.append(key)
        self.active[key] = dict(row, alarm_key=key)

    async def clear_active(self, key):
        self.cleared.append(key)
        self.active.pop(key, None)

    async def recovery_confirmed(self, key, row):
        self.recover_calls.append(key)
        return True

    async def load_feishu(self, owner=""):
        return {"enabled": True}

    async def load_alert_email(self, owner=""):
        return {"enabled": False}

    async def load_webhook(self, owner=""):
        return {"enabled": False}

    async def resolve_language(self, owner):
        return "zh-CN"


def _active_capacity_alarm() -> Dict[str, Any]:
    key = alerting.alarm_key("admin", pbsguard.RULE_ID, "capacity:pbs.lan:backup")
    return {
        key: {
            "username": "admin",
            "rule_id": pbsguard.RULE_ID,
            "rule_name": pbsguard.RULE_NAME,
            "target_type": "host",
            "target": "pbs.lan / backup",
            "metric": "pbs_capacity",
            "value": 96.0,
            "threshold": 95.0,
            "node": "pbs",
            "ts": 1,
        }
    }


def _patch_evaluate(monkeypatch, stub: _Stub, paused: bool) -> None:
    async def fake_load():
        return [_conn(paused=paused)]

    async def admin():
        return "admin"

    monkeypatch.setattr(pbs, "load_connections", fake_load)
    monkeypatch.setattr(pbsguard.pbs, "PbsClient", lambda conn: _FakePbs(conn))
    monkeypatch.setattr(pbsguard.store, "first_admin_username", admin)
    for name in (
        "load_active",
        "dispatch",
        "record",
        "mark_active",
        "clear_active",
        "recovery_confirmed",
        "load_feishu",
        "load_alert_email",
        "load_webhook",
        "resolve_language",
    ):
        monkeypatch.setattr(pbsguard.alerting, name, getattr(stub, name))


def test_paused_connection_does_not_send_fake_recovery(monkeypatch) -> None:
    """暂停 ≠ 恢复：既不发「已恢复」，也不清活动告警（先清等于让它静默消失）。"""
    stub = _Stub(_active_capacity_alarm())
    _patch_evaluate(monkeypatch, stub, paused=True)

    assert _run(pbsguard.evaluate()) == []
    assert stub.dispatched == []
    assert stub.recover_calls == []
    assert stub.cleared == []
    assert stub.active  # 活动告警原样留着，等恢复接入后重新判定


def test_unpaused_connection_still_sends_recovery(monkeypatch) -> None:
    """对照组：没暂停时容量已回落，恢复通知照发，证明上面的静默只针对暂停。"""
    stub = _Stub(_active_capacity_alarm())
    _patch_evaluate(monkeypatch, stub, paused=False)

    fired = _run(pbsguard.evaluate())
    assert [item["kind"] for item in fired] == ["recovery"]
    assert stub.cleared
    assert not stub.active
