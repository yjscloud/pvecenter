"""PBS 只读客户端与概览采集的几条硬约定。

这些用例不依赖真实 PBS，全部用桩覆盖，盯住四件事：

* 认证头必须是 ``PBSAPIToken <tokenid>:<secret>``（照抄 PVE 的
  ``PVEAPIToken=<id>=<secret>`` 只会得到 401）；
* 配置视图**绝不回吐** ``token_secret``；
* 概览的每个子项各自容错 —— 一条探测失败不能把整块打掉；
* 任务 ``endtime == 0`` 就是“还在跑”，不能被当成失败。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import pbs  # noqa: E402
from app.pve import ProxmoxError  # noqa: E402


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


# ------------------------------------------------------------------ 基础约定
def test_auth_header_uses_pbs_token_format() -> None:
    """PBS 是 `PBSAPIToken <tokenid>:<secret>`，不是 PVE 的两个等号。"""
    client = pbs.PbsClient(
        pbs.PbsConnection.from_dict(_conn())
    )
    assert (
        client._auth_headers()["Authorization"]
        == "PBSAPIToken=root@pam!panel:secret"
    )


def test_base_url_handles_scheme_and_port() -> None:
    assert pbs.PbsConnection.from_dict(_conn()).base_url == "https://pbs.lan:8007"
    # 带协议但没端口：补默认 8007（与 PVE 连接的 base_url 行为一致）
    assert (
        pbs.PbsConnection.from_dict(_conn(host="https://10.0.0.5")).base_url
        == "https://10.0.0.5:8007"
    )
    assert (
        pbs.PbsConnection.from_dict(_conn(host="https://10.0.0.5:9000")).base_url
        == "https://10.0.0.5:9000"
    )


def test_public_view_never_leaks_secret() -> None:
    view = pbs.public_connection(_conn())
    assert "token_secret" not in view
    assert view["token_secret_set"] is True
    assert view["configured"] is True


def test_region_is_a_grouping_label_not_required() -> None:
    """区域 / 机房与 PVE 连接的 region 同一语义：纯标签，缺省为空串。"""
    assert pbs.public_connection(_conn(region="  广州机房 "))["region"] == "广州机房"
    # 老记录存的是没有 region 的 dict：不迁移，读出来就是空串（= 未分区）
    legacy = _conn()
    legacy.pop("region", None)
    assert pbs.public_connection(legacy)["region"] == ""
    assert pbs.PbsConnection.from_dict(legacy).region == ""


def test_unconfigured_connection_is_flagged() -> None:
    view = pbs.public_connection(_conn(token_secret=""))
    assert view["token_secret_set"] is False
    assert view["configured"] is False


# ------------------------------------------------------------------ 桩客户端
class _FakePbs:
    """按需返回固定数据；``fail`` / ``denied`` 指定哪一步抛 ProxmoxError。

    ``denied`` 模拟 PBS 回 403（token 没有对应 ACL），用来区分「没权限」与
    「上游出错」这两种读不到：界面要给的是不同的提示。
    """

    def __init__(
        self,
        conn: pbs.PbsConnection,
        *,
        fail: Optional[str] = None,
        denied: Optional[List[str]] = None,
        bad_nodes: Optional[List[str]] = None,
    ) -> None:
        self.conn = conn
        self.fail = fail
        self.denied = set(denied or ())
        # 模拟「节点名写错」：PBS 对不存在的节点回 400，而不是 403
        self.bad_nodes = set(bad_nodes or ())

    def _node_ok(self, node: str) -> None:
        if node in self.bad_nodes:
            raise ProxmoxError(
                f"unknown node '{node}'",
                400,
                endpoint=f"GET /nodes/{node}/status",
                pve_status=400,
            )

    def _maybe(self, step: str) -> None:
        if step in self.denied:
            raise ProxmoxError(
                f"{step} denied",
                502,
                endpoint=f"GET /{step}",
                pve_status=403,
                code="pbs_permission_denied",
            )
        if self.fail == step:
            raise ProxmoxError(f"{step} failed", 502)

    async def version(self) -> Dict[str, Any]:
        self._maybe("version")
        return {"version": "3.4.1"}

    async def nodes(self) -> List[Dict[str, Any]]:
        self._maybe("nodes")
        return [{"node": "pbs"}]

    async def node_status(self, node: str) -> Dict[str, Any]:
        self._maybe("node_status")
        self._node_ok(node)
        return {
            "uptime": 3600,
            "memory": {"total": 1000, "used": 400},
            "loadavg": [0.1, 0.2, 0.3],
            "cpuinfo": {"cpus": 4},
        }

    async def tasks(self, node: str, limit: int = 20) -> List[Dict[str, Any]]:
        self._maybe("tasks")
        self._node_ok(node)
        return [
            {
                "upid": "UPID:pbs:1:1:1:backup:100:root@pam:",
                "worker_type": "backup",
                "status": "OK",
                "starttime": 100,
                "endtime": 200,
                "worker_id": "100",
            },
            {
                "upid": "UPID:pbs:2:2:2:verify:100:root@pam:",
                "worker_type": "verify",
                "status": "",
                "starttime": 300,
                "endtime": 0,
            },
            {
                # 仓库维护类任务：不该出现在「最近任务」里
                "upid": "UPID:pbs:3:3:3:garbage_collection:100:root@pam:",
                "worker_type": "garbage_collection",
                "status": "OK",
                "starttime": 400,
                "endtime": 500,
            },
        ]

    async def datastores(self) -> List[Dict[str, Any]]:
        self._maybe("datastores")
        return [{"store": "main", "gc-schedule": "daily"}]

    async def datastore_status(self, name: str) -> Dict[str, Any]:
        self._maybe("datastore_status")
        return {"total": 1000, "used": 250, "avail": 750}

    async def disks(self, node: str) -> Any:
        self._maybe("disks")
        self._node_ok(node)
        return {
            "devices": [
                {
                    "devpath": "/dev/sda",
                    "health": "PASSED",
                    "model": "SSD",
                    "size": 256 * 1024 ** 3,
                    "type": "ssd",
                },
                {
                    "devpath": "/dev/sdb",
                    "health": "FAILED",
                    "model": "HDD",
                    "size": 4 * 1024 ** 4,
                    "type": "hdd",
                },
            ]
        }

    async def task_log(
        self, node: str, upid: str, limit: int = 200
    ) -> List[Dict[str, Any]]:
        self._maybe("task_log")
        return [{"t": "starting"}, {"t": "done"}]

    async def aclose(self) -> None:
        return None


def _patch(monkeypatch, conn=None, **fake_kwargs) -> None:
    async def fake_load():
        return [conn or _conn()]

    monkeypatch.setattr(pbs, "load_connections", fake_load)
    monkeypatch.setattr(
        pbs, "PbsClient", lambda conn: _FakePbs(conn, **fake_kwargs)
    )


# ------------------------------------------------------------------ 概览采集
def test_overview_collects_node_datastore_and_tasks(monkeypatch) -> None:
    _patch(monkeypatch)
    data = _run(pbs.collect_overview())
    assert data["configured"] is True
    row = data["connections"][0]
    assert row["online"] is True
    assert row["version"] == "3.4.1"
    assert row["node"]["cpus"] == 4
    assert row["node"]["mem_used"] == 400
    # 磁盘健康：异常盘列表与总盘数分开给，界面才能区分「全正常」与「没读到」
    assert row["node"]["disks_total"] == 2
    assert [d["device"] for d in row["node"]["disks"]] == ["/dev/sdb"]
    # 明细：悬停提示要能列出每一块盘的型号 / 容量 / 健康
    assert [d["device"] for d in row["node"]["disks_all"]] == ["/dev/sda", "/dev/sdb"]
    assert row["node"]["disks_all"][1]["health"] == "FAILED"
    # 汇总：一块健康、一块异常，容量与 SSD/HDD 构成单独给
    assert row["node"]["disks_summary"] == {
        "count": 2,
        "healthy": 1,
        "total_bytes": 256 * 1024 ** 3 + 4 * 1024 ** 4,
        "ssd": 1,
        "hdd": 1,
    }
    # GC 不在「最近任务」里，但卡片要单独显示它的状态
    assert row["node"]["gc"]["status"] == "OK"
    assert row["node"]["gc"]["running"] is False
    assert row["datastores"][0]["store"] == "main"
    assert row["datastores"][0]["used"] == 250
    assert row["datastores"][0]["capacity_error"] is None
    # endtime == 0 → 运行中；status 为空不代表失败
    verify_task = [t for t in row["tasks"] if t["type"] == "verify"][0]
    assert verify_task["running"] is True
    backup_task = [t for t in row["tasks"] if t["type"] == "backup"][0]
    assert backup_task["running"] is False
    # 「最近任务」只留备份 / 校验：GC 属于仓库维护，混进来会淹没备份状态
    assert [t["type"] for t in row["tasks"]] == ["backup", "verify"]


def test_missing_sys_audit_reports_reason_instead_of_zeros(monkeypatch) -> None:
    """缺 Sys.Audit 时内存 / 磁盘读不到，必须带出「为什么」，而不是 0。

    节点页收起状态只挂一个「—」和「读不到」，用户分不清是面板坏了还是权限
    没给 —— 所以后端把失败原因归成 ``no_permission``（403）/ ``error``（其它），
    前端据此写「缺 Sys.Audit」或通用文案。
    """
    _patch(monkeypatch, denied=["node_status", "disks"])
    row = _run(pbs.collect_overview())["connections"][0]
    assert row["online"] is True
    assert row["node"]["status_available"] is False
    assert row["node"]["status_hint"] == "no_permission"
    assert row["node"]["mem_total"] == 0
    assert row["node"]["disks_total"] == 0
    assert row["node"]["disks_hint"] == "no_permission"
    # 上游原文留给悬停提示，便于区分「权限没给」与「版本没这个端点」
    assert row["node"]["status_error"] == "node_status denied"
    # 仓库与任务走 Datastore.Audit，不受 Sys.Audit 影响
    assert row["datastores"][0]["store"] == "main"
    assert [t["type"] for t in row["tasks"]] == ["backup", "verify"]


def test_wrong_configured_node_falls_back_instead_of_blanking_metrics(
    monkeypatch,
) -> None:
    """「节点」填错（PBS 回 400）时不能把整块指标清零。

    真实事故：连接里手填了 ``pbs_backup``（其实是仓库名），PBS 对
    ``/nodes/pbs_backup/status`` 回 400，于是内存、负载、运行时长、磁盘健康、
    任务与 GC 一起消失。现在的做法是按「配置的节点 → /nodes 第一台 → localhost」
    依次回退，并记下被忽略的名字让界面解释清楚。

    这里同时模拟「``/nodes`` 也是 403」（缺 Sys.Audit 拿不到真实节点名），
    这正是线上那台 PBS 的状态。
    """
    _patch(
        monkeypatch,
        conn=_conn(node="pbs_backup"),
        bad_nodes=["pbs_backup"],
        denied=["nodes"],
    )
    row = _run(pbs.collect_overview())["connections"][0]
    assert row["node"]["name"] == "localhost"
    assert row["node"]["name_ignored"] == "pbs_backup"
    assert row["node"]["status_available"] is True
    assert row["node"]["mem_total"] == 1000
    assert row["node"]["load1"] == 0.1
    assert row["node"]["disks_total"] == 2
    assert [t["type"] for t in row["tasks"]] == ["backup", "verify"]


def test_datastore_usage_percent_and_level(monkeypatch) -> None:
    """容量四项 + 阈值级别由后端算好，前端只上色。

    90% 落在 85~95 之间 → warn；96% 越过 95 → crit。数字与级别必须自洽，
    否则会出现「条是红的、提示说正常」。
    """

    class _Full(_FakePbs):
        def __init__(self, conn, *, used: int) -> None:
            super().__init__(conn)
            self.used = used

        async def datastore_status(self, name: str) -> Dict[str, Any]:
            return {"total": 1000, "used": self.used, "avail": 1000 - self.used}

    async def fake_load():
        return [_conn()]

    monkeypatch.setattr(pbs, "load_connections", fake_load)
    monkeypatch.setattr(
        pbs, "PbsClient", lambda conn: _Full(conn, used=900)
    )
    store = _run(pbs.collect_overview())["connections"][0]["datastores"][0]
    assert store["used"] == 900
    assert store["free"] == 100
    assert store["used_percent"] == 90.0
    assert store["level"] == "warn"

    pbs.invalidate_overview()
    monkeypatch.setattr(
        pbs, "PbsClient", lambda conn: _Full(conn, used=960)
    )
    store = _run(pbs.collect_overview())["connections"][0]["datastores"][0]
    assert store["used_percent"] == 96.0
    assert store["level"] == "crit"


def test_usage_level_thresholds() -> None:
    assert pbs.usage_level(0) == "ok"
    assert pbs.usage_level(84.9) == "ok"
    assert pbs.usage_level(85) == "warn"
    assert pbs.usage_level(94.9) == "warn"
    assert pbs.usage_level(95) == "crit"


def test_version_failure_marks_offline_without_extra_reads(monkeypatch) -> None:
    _patch(monkeypatch, fail="version")
    row = _run(pbs.collect_overview())["connections"][0]
    assert row["online"] is False
    assert row["error"] and "version failed" in row["error"]
    # 连不上时不该继续列任务 / 仓库
    assert row["tasks"] == []
    assert row["datastores"] == []


def test_datastore_status_failure_keeps_row(monkeypatch) -> None:
    """老版本 PBS 没有 datastore/status：容量留 0，仓库行仍要出现。"""
    _patch(monkeypatch, fail="datastore_status")
    row = _run(pbs.collect_overview())["connections"][0]
    assert row["online"] is True
    assert row["datastores"][0]["store"] == "main"
    assert row["datastores"][0]["total"] == 0


def test_datastore_capacity_error_is_reported(monkeypatch) -> None:
    """容量读不到要留下原因：403 与 404 的处理方式完全不同，不能都显示“不提供”。"""
    _patch(monkeypatch, fail="datastore_status")
    store = _run(pbs.collect_overview())["connections"][0]["datastores"][0]
    assert store["store"] == "main"
    assert store["total"] == 0
    assert store["level"] == "unknown"
    assert store["capacity_error"] and "502" in store["capacity_error"]


def test_has_datastore_audit_paths() -> None:
    """权限必须落在 /datastore 之下；/system 上的 Datastore.* 对仓库不生效。"""
    assert pbs.has_datastore_audit({"/datastore": {"Datastore.Audit": True}})
    assert pbs.has_datastore_audit(
        {"/datastore/pbs_backup": {"Datastore.Audit": True}}
    )
    assert not pbs.has_datastore_audit({"/system": {"Datastore.Audit": True}})
    assert not pbs.has_datastore_audit({"/datastore": {"Datastore.Backup": True}})
    assert not pbs.has_datastore_audit(None)


def test_empty_datastore_list_hints_missing_permission(monkeypatch) -> None:
    """PBS 对没权限的 token 返回空数组：界面要说是权限问题，而不是“没有仓库”。"""

    class _NoPerm(_FakePbs):
        async def datastores(self) -> List[Dict[str, Any]]:
            return []

        async def get(self, path: str, params=None) -> Any:
            if path == "/access/permissions":
                # 只有 /system 的 Admin：Sys.Audit 生效，Datastore.* 对仓库不生效
                return {"/system": {"Sys.Audit": True, "Datastore.Audit": True}}
            return {}

    async def fake_load():
        return [_conn()]

    monkeypatch.setattr(pbs, "load_connections", fake_load)
    monkeypatch.setattr(pbs, "PbsClient", lambda conn: _NoPerm(conn))
    row = _run(pbs.collect_overview())["connections"][0]
    assert row["online"] is True
    assert row["datastores"] == []
    assert row["datastore_hint"] == "no_permission"


def test_task_failure_does_not_hide_datastores(monkeypatch) -> None:
    _patch(monkeypatch, fail="tasks")
    row = _run(pbs.collect_overview())["connections"][0]
    assert row["online"] is True
    assert row["tasks"] == []
    assert row["datastores"][0]["used"] == 250


def test_no_connection_returns_unconfigured(monkeypatch) -> None:
    async def empty():
        return []

    monkeypatch.setattr(pbs, "load_connections", empty)
    data = _run(pbs.collect_overview())
    assert data["configured"] is False
    assert data["connections"] == []
    # 阈值随概览一起下发：前端提示「≥85% 警戒」时用的是服务端这一组数
    assert data["thresholds"] == {"warn": 85.0, "crit": 95.0}
    assert isinstance(data["generated_at"], int)


def test_unconfigured_entry_reports_reason(monkeypatch) -> None:
    async def fake_load():
        return [_conn(token_secret="")]

    monkeypatch.setattr(pbs, "load_connections", fake_load)
    monkeypatch.setattr(
        pbs, "PbsClient", lambda conn: _FakePbs(conn)
    )
    row = _run(pbs.collect_overview())["connections"][0]
    assert row["configured"] is False
    assert row["error"] == "pbs_not_configured"


# ------------------------------------------------------------------ P1 补充项
def test_disk_alerts_only_reports_unhealthy() -> None:
    """只挑明确异常且带 health 的盘：缺字段按“未知”沉默，避免误报。"""
    out = pbs._disk_alerts(
        {
            "devices": [
                {"devpath": "/dev/sda", "health": "PASSED"},
                {"devpath": "/dev/sdb", "health": "failed", "model": "HDD"},
                {"devpath": "/dev/sdc"},
                {"devpath": "/dev/sdd", "health": "UNKNOWN"},
            ]
        }
    )
    assert [item["device"] for item in out] == ["/dev/sdb"]
    assert out[0]["health"] == "FAILED"
    # 裸数组形状（老版本）也要吃得下
    assert pbs._disk_alerts([{"devpath": "/dev/nvme0", "health": "DEGRADED"}])


def test_overview_dedupes_same_pbs(monkeypatch) -> None:
    """同一 host:port 登记两次只留一行，被合并的名称进 shared_with。"""

    async def fake_load():
        return [
            _conn(id="a", name="pbs-01"),
            _conn(id="b", name="pbs-02"),
        ]

    monkeypatch.setattr(pbs, "load_connections", fake_load)
    monkeypatch.setattr(pbs, "PbsClient", lambda conn: _FakePbs(conn))
    rows = _run(pbs.collect_overview())["connections"]
    assert len(rows) == 1
    assert rows[0]["name"] == "pbs-01"
    assert rows[0]["shared_with"] == ["pbs-02"]


def test_overview_cache_avoids_second_collection(monkeypatch) -> None:
    """20 秒内重复请求不该再打一遍 PBS（多人同时开备份页的场景）。"""
    calls = {"n": 0}

    async def fake_load():
        return [_conn()]

    class _Counting(_FakePbs):
        async def version(self) -> Dict[str, Any]:
            calls["n"] += 1
            return await super().version()

    monkeypatch.setattr(pbs, "load_connections", fake_load)
    monkeypatch.setattr(pbs, "PbsClient", lambda conn: _Counting(conn))
    first = _run(pbs.collect_overview())
    second = _run(pbs.collect_overview())
    assert calls["n"] == 1
    # 返回的是深拷贝：调用方改它不会污染缓存
    first["connections"][0]["name"] = "mutated"
    third = _run(pbs.collect_overview())
    assert third["connections"][0]["name"] == "pbs-01"
    assert second["configured"] is True


def test_save_connections_invalidates_cache(monkeypatch) -> None:
    """保存凭据后必须立刻作废缓存，否则用户会看到旧的“未配置”。"""
    calls = {"n": 0}

    async def fake_load():
        calls["n"] += 1
        return []

    monkeypatch.setattr(pbs, "load_connections", fake_load)
    _run(pbs.collect_overview())
    _run(pbs.collect_overview())
    assert calls["n"] == 1

    async def fake_save(key: str, value: str) -> None:
        return None

    monkeypatch.setattr(pbs.store, "set_setting", fake_save)
    _run(pbs.save_connections([]))
    _run(pbs.collect_overview())
    assert calls["n"] == 2


def test_secret_whitespace_is_stripped() -> None:
    """从终端复制 `generate-token` 输出常带尾部空白；带空白的 header 只会 401。"""
    cfg = pbs.PbsConnection.from_dict(_conn(token_secret="  abc-def  \n"))
    assert cfg.token_secret == "abc-def"
    header = pbs.PbsClient(cfg)._auth_headers()["Authorization"]
    assert header == "PBSAPIToken=root@pam!panel:abc-def"


# ------------------------------------------------------------ 测试连接的能力探测
def test_config_test_reports_missing_sys_audit(monkeypatch) -> None:
    """缺 Sys.Audit 只影响节点状态，任务列表只要 Datastore.Audit。

    `/nodes` 403 时旧实现会把 tasks 一起判成不可用，于是只有 DatastoreAudit
    的 token 明明能读任务却被提示“任务不可见”。这里钉住两者互不牵连：
    node_status 为 False，tasks 仍为 True，且退回 localhost 继续探。
    """
    from app.routers import pbs as pbs_router
    from app.schemas import PbsConnectionTestIn

    calls: Dict[str, List[str]] = {"tasks": [], "node_status": []}

    class _Fake:
        async def version(self) -> Dict[str, Any]:
            return {"version": "3.4.1"}

        async def datastores(self) -> List[Dict[str, Any]]:
            return [{"store": "main"}]

        async def datastore_status(self, name: str) -> Dict[str, Any]:
            return {"total": 1000, "used": 250, "avail": 750}

        async def nodes(self) -> List[Dict[str, Any]]:
            raise ProxmoxError("403 no permission", 403)

        async def node_status(self, node: str) -> Dict[str, Any]:
            calls["node_status"].append(node)
            raise ProxmoxError("403 no permission", 403)

        async def tasks(self, node: str, limit: int = 1) -> List[Dict[str, Any]]:
            calls["tasks"].append(node)
            return []

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(pbs, "PbsClient", lambda conn: _Fake())
    body = PbsConnectionTestIn(
        host="pbs.lan", token_id="root@pam!panel", token_secret="secret"
    )
    result = _run(pbs_router.test_config(body, user={"username": "admin"}))
    assert result["ok"] is True
    assert result["version"] == "3.4.1"
    assert result["capabilities"] == {
        "datastores": True,
        "datastore_status": True,
        "tasks": True,
        "node_status": False,
    }
    # 拿不到节点列表时退回 localhost，与 collect_overview 的行为一致
    assert calls["tasks"] == ["localhost"]
    assert calls["node_status"] == ["localhost"]


def test_config_test_reports_missing_datastore_audit(monkeypatch) -> None:
    """完全没有 Datastore.Audit：仓库与任务都读不到，但节点状态可能正常。"""
    from app.routers import pbs as pbs_router
    from app.schemas import PbsConnectionTestIn

    class _Fake:
        async def version(self) -> Dict[str, Any]:
            return {"version": "3.4.1"}

        async def datastores(self) -> List[Dict[str, Any]]:
            raise ProxmoxError("403 no permission", 403)

        async def nodes(self) -> List[Dict[str, Any]]:
            return [{"node": "pbs1"}]

        async def node_status(self, node: str) -> Dict[str, Any]:
            return {"uptime": 1}

        async def tasks(self, node: str, limit: int = 1) -> List[Dict[str, Any]]:
            raise ProxmoxError("403 no permission", 403)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(pbs, "PbsClient", lambda conn: _Fake())
    body = PbsConnectionTestIn(
        host="pbs.lan", token_id="root@pam!panel", token_secret="secret"
    )
    result = _run(pbs_router.test_config(body, user={"username": "admin"}))
    assert result["capabilities"] == {
        "datastores": False,
        "datastore_status": False,
        "tasks": False,
        "node_status": True,
    }
    # 列表都读不到，就没必要再探容量；错误码留在 probe_errors 里供界面展示
    assert result["probe_errors"]["datastores"] == "HTTP 403"


def test_clean_secret_removes_paste_noise() -> None:
    """`generate-token` 的输出是几行文本，复制时很容易把标签一起带进来。"""
    assert pbs.clean_secret("  abc-def  ") == "abc-def"
    assert pbs.clean_secret("Token secret: abc-def") == "abc-def"
    assert pbs.clean_secret("secret:abc-def") == "abc-def"
    assert pbs.clean_secret("pvecenter@pbs!readonly=abc-def") == "abc-def"
    assert pbs.clean_secret('"abc-def"') == "abc-def"
    assert pbs.clean_secret("") == ""


def test_diagnose_distinguishes_token_id_from_password() -> None:
    as_token_id = pbs.PbsConnection.from_dict(
        _conn(token_secret="pvecenter@pbs!readonly")
    )
    assert pbs.diagnose(as_token_id)["secret_looks_like_token_id"] is True

    uuid = "12345678-1234-1234-1234-123456789abc"
    good = pbs.PbsConnection.from_dict(_conn(token_secret=uuid))
    diag = pbs.diagnose(good)
    assert diag["secret_uuid_like"] is True
    assert diag["secret_looks_like_token_id"] is False
    # 诊断绝不回吐明文
    assert "secret" not in diag


def test_clean_secret_extracts_json_value() -> None:
    """generate-token 的输出字段叫 value（不是 secret），整段粘贴很常见。"""
    raw = (
        'Result: {\n'
        '  "tokenid": "pvecenter@pbs!readonly",\n'
        '  "value": "0a4bb8ad-4636-4c24-addd-abf6ad4a7900"\n'
        '}'
    )
    assert pbs.clean_secret(raw) == "0a4bb8ad-4636-4c24-addd-abf6ad4a7900"
    assert pbs.clean_secret('"value": "abc"') == "abc"
    assert pbs.clean_secret('{"secret":"abc"}') == "abc"


def test_clean_secret_accepts_colon_form() -> None:
    """`tokenid:secret` 整段粘贴也拆得开（PBS 自己的凭据就是这个形状）。"""
    assert (
        pbs.clean_secret("pvecenter@pbs!readonly:0a4bb8ad-4636-4c24-addd-abf6ad4a7900")
        == "0a4bb8ad-4636-4c24-addd-abf6ad4a7900"
    )
