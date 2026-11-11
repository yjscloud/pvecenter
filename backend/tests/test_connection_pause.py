"""PVE 连接的「暂停接入」：开关本身 + 它在采集路径上的生效点。

三件事必须成立，否则这个开关就是装饰：

* **不再采集**：``all_connection_clients`` 过滤掉暂停的连接（所有逐连接聚合
  都从那里取目标），``get_client`` 对暂停的兜底目标直接 503；
* **只改一个字段**：走专用端点，整份 PUT 不传 ``paused`` 时保持原值；
* **能恢复**：传 ``paused=false`` 就回到采集集合里。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import pve, store  # noqa: E402
from app.pve import ProxmoxError  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _conn(cid: str, **over: Any) -> Dict[str, Any]:
    base = {
        "id": cid,
        "name": f"pve-{cid}",
        "host": f"{cid}.lan",
        "port": 8006,
        "token_id": "root@pam!panel",
        "token_secret": "secret",
    }
    base.update(over)
    return base


# ------------------------------------------------------------------ 采集路径
def test_paused_connection_is_excluded_from_collection(monkeypatch) -> None:
    """暂停的连接不进采集集合：虚拟机 / 存储 / 备份 / 告警都从这一处取目标。"""
    sentinel = object()
    monkeypatch.setattr(
        store, "get_connections", lambda: [_conn("a"), _conn("b", paused=True)]
    )
    monkeypatch.setattr(store, "paused_connection_ids", lambda: ["b"])
    monkeypatch.setattr(
        pve, "_scoped_client", lambda cid: sentinel if cid == "a" else None
    )

    pairs = pve.all_connection_clients()

    assert [str(profile.get("id")) for profile, _client in pairs] == ["a"]


def test_all_paused_leaves_no_collection_target(monkeypatch) -> None:
    monkeypatch.setattr(store, "get_connections", lambda: [_conn("a", paused=True)])
    monkeypatch.setattr(store, "paused_connection_ids", lambda: ["a"])
    monkeypatch.setattr(pve, "_scoped_client", lambda cid: object())

    assert pve.all_connection_clients() == []


def test_get_client_refuses_paused_target(monkeypatch) -> None:
    """单连接路径（/health、/cluster/status）不经过聚合，必须在 get_client 挡住。"""
    monkeypatch.setattr(pve, "requested_connection", lambda: "")
    monkeypatch.setattr(pve, "_active_connection_id", lambda: "a")
    monkeypatch.setattr(store, "is_connection_paused", lambda cid: True)

    try:
        pve.get_client()
    except ProxmoxError as exc:
        assert exc.status_code == 503
        assert "暂停" in exc.message
    else:  # pragma: no cover - 返回客户端就是失败
        raise AssertionError("暂停的连接不该拿到客户端")


def test_get_client_returns_client_when_not_paused(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(pve, "requested_connection", lambda: "")
    monkeypatch.setattr(pve, "_active_connection_id", lambda: "a")
    monkeypatch.setattr(store, "is_connection_paused", lambda cid: False)
    # 命中进程内客户端缓存的快路径，避免真的去读库 + 建连
    monkeypatch.setattr(pve, "_client", sentinel)
    monkeypatch.setattr(pve, "_revision_checked_at", time.monotonic())

    assert pve.get_client() is sentinel


# ------------------------------------------------------------------ 端点
def _patch_endpoint(monkeypatch, conns: List[Dict[str, Any]], active: str = "a"):
    from app.routers import config as config_router

    saved: List[Dict[str, Any]] = []
    audits: List[Dict[str, Any]] = []

    monkeypatch.setattr(store, "get_connections", lambda: list(conns))
    monkeypatch.setattr(store, "get_active_connection_id", lambda: active)

    async def fake_save(rows):
        saved.extend(rows)

    async def fake_audit(request, user, action, **kw):
        audits.append({"action": action, **kw})

    monkeypatch.setattr(store, "save_connections", fake_save)
    monkeypatch.setattr(config_router.security, "audit", fake_audit)
    monkeypatch.setattr(config_router, "rebuild_client", lambda *a, **k: None)
    return config_router, saved, audits


def test_pause_endpoint_touches_only_that_row_and_audits(monkeypatch) -> None:
    from app.schemas import ConnectionPauseIn

    router, saved, audits = _patch_endpoint(monkeypatch, [_conn("a"), _conn("b")])

    pub = _run(
        router.pause_connection(
            "b", ConnectionPauseIn(paused=True), None, {"username": "admin"}
        )
    )

    by_id = {row["id"]: row for row in saved}
    assert by_id["b"]["paused"] is True
    assert bool(by_id["a"].get("paused")) is False
    assert audits[0]["action"] == "connection.pause"
    assert audits[0]["detail"] == {"paused": True}
    assert pub["paused"] is True
    # b 不是兜底连接：active 标记只对兜底那台为真
    assert pub["active"] is False


def test_resume_endpoint_marks_it_back_to_healthy(monkeypatch) -> None:
    from app.schemas import ConnectionPauseIn

    router, saved, audits = _patch_endpoint(
        monkeypatch, [_conn("a", paused=True)]
    )

    pub = _run(
        router.pause_connection(
            "a", ConnectionPauseIn(paused=False), None, {"username": "admin"}
        )
    )

    assert saved[0]["paused"] is False
    assert audits[0]["action"] == "connection.resume"
    assert pub["paused"] is False
    assert pub["active"] is True


def test_pausing_an_unknown_connection_is_404(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.schemas import ConnectionPauseIn

    router, _saved, _audits = _patch_endpoint(monkeypatch, [_conn("a")])

    try:
        _run(
            router.pause_connection(
                "nope", ConnectionPauseIn(paused=True), None, {"username": "admin"}
            )
        )
    except HTTPException as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("未知连接应返回 404")


def test_editing_a_connection_keeps_its_paused_flag(monkeypatch) -> None:
    """整份 PUT 不传 paused 时保持原值 —— 否则改个名字就把采集悄悄恢复了。"""
    from app.schemas import ConnectionConfigIn

    router, saved, _audits = _patch_endpoint(
        monkeypatch, [_conn("a", paused=True, name="old")], active="b"
    )

    body = ConnectionConfigIn(host="a.lan", port=8006, token_id="root@pam!panel", name="new")
    _run(router.update_connection_profile("a", body, None, {"username": "admin"}))

    assert saved[0]["name"] == "new"
    assert saved[0]["paused"] is True


def test_editing_can_resume_explicitly(monkeypatch) -> None:
    from app.schemas import ConnectionConfigIn

    router, saved, _audits = _patch_endpoint(
        monkeypatch, [_conn("a", paused=True)], active="b"
    )

    body = ConnectionConfigIn(
        host="a.lan", port=8006, token_id="root@pam!panel", paused=False
    )
    _run(router.update_connection_profile("a", body, None, {"username": "admin"}))

    assert saved[0]["paused"] is False


def test_health_reports_paused_instead_of_failure(monkeypatch) -> None:
    """/health 遇到暂停不是 500、也不是「连不上」：明确回报 pve_paused。"""
    from app.routers import config as config_router

    def boom():
        raise ProxmoxError(
            "连接已暂停接入（id=a）：在「连接配置」里恢复后才会继续采集",
            status_code=503,
            endpoint="get_client",
        )

    monkeypatch.setattr(config_router, "get_client", boom)
    monkeypatch.setattr(
        config_router.security, "has_user_permission", lambda user, perm: False
    )

    data = _run(config_router.health({"username": "admin"}))

    assert data["pve_paused"] is True
    assert data["pve_connected"] is False
    assert "暂停" in (data["error"] or "")


def test_collection_targets_does_not_fall_back_to_a_paused_host(monkeypatch) -> None:
    """全部暂停时返回空列表，而不是回退去读兜底那台（它正是被停用的机器）。"""
    monkeypatch.setattr(pve, "all_connection_clients", lambda: [])
    monkeypatch.setattr(store, "connection_ids", lambda: ["a", "b"])

    assert pve.collection_targets() == []


def test_collection_targets_falls_back_when_nothing_is_saved(monkeypatch) -> None:
    """真的一条连接都没有（老的单份配置 / 环境变量）才回退兜底客户端。"""
    sentinel = object()
    monkeypatch.setattr(pve, "all_connection_clients", lambda: [])
    monkeypatch.setattr(store, "connection_ids", lambda: [])
    monkeypatch.setattr(pve, "get_client", lambda: sentinel)

    assert pve.collection_targets() == [(None, sentinel)]
