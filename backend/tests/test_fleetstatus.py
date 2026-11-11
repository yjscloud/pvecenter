"""机队口径的探测汇总：单条连接的失败不能拖垮整份结果。

``parallel`` 的契约是「异常原样返回」（见 ``app.pve.parallel``），所以
``collect`` 必须自己兜住 ``probe`` 之外的异常，否则一条结构异常的响应会让
``/api/cluster/fleet-status`` 整份 500 —— 而它的设计目标恰恰是逐条降级。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import fleetstatus  # noqa: E402
from app.pve import ProxmoxError  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _Client:
    def __init__(
        self,
        *,
        version: Optional[Dict[str, Any]] = None,
        nodes: Optional[List[Dict[str, Any]]] = None,
        ha: Optional[List[Dict[str, Any]]] = None,
        resources: Any = None,
        boom: Optional[BaseException] = None,
    ) -> None:
        self._version = version or {"version": "8.2.4"}
        self._nodes = nodes if nodes is not None else [{"node": "pve"}]
        self._ha = ha or []
        self._resources = resources if resources is not None else []
        self._boom = boom

    async def version(self) -> Dict[str, Any]:
        if self._boom is not None:
            raise self._boom
        return self._version

    async def nodes(self) -> List[Dict[str, Any]]:
        return self._nodes

    async def get(self, path: str) -> Any:
        if path.endswith("/resources"):
            return self._resources
        return self._ha


def _patch(monkeypatch, pairs) -> None:
    monkeypatch.setattr(fleetstatus, "all_connection_clients", lambda: list(pairs))
    monkeypatch.setattr(fleetstatus, "active_connection", lambda: {"id": "", "name": ""})


def _profile(cid: str = "a") -> Dict[str, Any]:
    return {"id": cid, "name": f"pve-{cid}", "host": f"{cid}.lan"}


def test_unexpected_error_degrades_to_one_offline_row(monkeypatch) -> None:
    """非 ProxmoxError（字段结构不符预期之类）：只让这一条不可达，不 500。"""
    _patch(
        monkeypatch,
        [
            (_profile("ok"), _Client()),
            (_profile("bad"), _Client(boom=RuntimeError("unexpected payload"))),
        ],
    )

    data = _run(fleetstatus.collect())

    assert data["totals"]["connections"] == 2
    assert data["totals"]["online"] == 1
    bad = [item for item in data["connections"] if item["id"] == "bad"][0]
    assert bad["ok"] is False
    assert "unexpected payload" in bad["error"]
    # 字段齐全，前端不必为降级行判空
    assert bad["node_count"] == 0
    assert bad["quorate"] is None


def test_proxmox_error_is_reported_as_message(monkeypatch) -> None:
    _patch(
        monkeypatch,
        [(_profile(), _Client(boom=ProxmoxError("Cannot reach Proxmox", status_code=502)))],
    )

    data = _run(fleetstatus.collect())

    assert data["totals"]["online"] == 0
    assert "Cannot reach Proxmox" in data["connections"][0]["error"]


def test_single_node_skips_ha_queries(monkeypatch) -> None:
    _patch(monkeypatch, [(_profile(), _Client(nodes=[{"node": "pve"}]))])

    item = _run(fleetstatus.collect())["connections"][0]

    assert item["cluster_mode"] == "standalone"
    assert item["quorate"] is None


def test_cluster_reads_quorum_and_ha_resources(monkeypatch) -> None:
    _patch(
        monkeypatch,
        [
            (
                _profile(),
                _Client(
                    nodes=[{"node": "pve1"}, {"node": "pve2"}],
                    ha=[{"type": "quorum", "quorate": 1}, {"type": "master", "node": "pve2"}],
                    resources=[{"sid": "vm:100"}],
                ),
            )
        ],
    )

    item = _run(fleetstatus.collect())["connections"][0]

    assert item["cluster_mode"] == "cluster"
    assert item["quorate"] is True
    assert item["ha_enabled"] is True
    assert item["ha_resources"] == 1


def test_host_is_only_exposed_to_managers(monkeypatch) -> None:
    _patch(monkeypatch, [(_profile(), _Client())])

    viewer = _run(fleetstatus.collect(can_manage=False))["connections"][0]
    manager = _run(fleetstatus.collect(can_manage=True))["connections"][0]

    assert "host" not in viewer
    assert manager["host"] == "a.lan"
