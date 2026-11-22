"""建规则时「目标地址」下拉的数据来源。

普通用户的选项 = 自己名下的虚机 / 容器（**同时也是允许范围**）。管理员不受白名单
限制，下拉只是「手边有哪些地址可以点」的快捷入口 = 全平台虚机 / 容器；这份清单要
逐条连接读 PVE，所以只在弹窗打开时按需拉（``/policy?with_targets=1``），不跟着
页面一起算。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import frpguard, guestip, pve  # noqa: E402
from test_api_routes import api, auth_headers  # noqa: E402,F401


class _FakeClient:
    """只实现 cluster_resources：返回这台 PVE 上的客户机清单。"""

    def __init__(self, rows: List[Dict[str, Any]], *, fail: bool = False) -> None:
        self._rows = rows
        self._fail = fail

    async def cluster_resources(self, rtype: str | None = None):  # noqa: ARG002
        if self._fail:
            raise RuntimeError("connection refused")
        return self._rows


def _guest(node: str, vmid: int, name: str, kind: str = "qemu") -> Dict[str, Any]:
    return {"node": node, "vmid": vmid, "name": name, "type": kind, "status": "running"}


class TestAllGuestTargets:
    """管理员看到的预设清单：全平台，且不按归属过滤。"""

    def _patch(self, monkeypatch, connections, ips: Dict[str, str]):
        monkeypatch.setattr(pve, "all_connection_clients", lambda: connections)

        async def _resolve(_client, guest, **_kw) -> str:
            return ips.get(str(guest.get("vmid")), "")

        monkeypatch.setattr(guestip, "resolve", _resolve)

    def test_lists_every_guest_regardless_of_owner(self, monkeypatch) -> None:
        connections = [
            ({"id": "c1"}, _FakeClient([_guest("pve1", 101, "web"), _guest("pve1", 102, "db")])),
            ({"id": "c2"}, _FakeClient([_guest("pve2", 201, "ct", "lxc")])),
        ]
        self._patch(monkeypatch, connections, {"101": "10.0.0.11", "102": "10.0.0.12", "201": "10.0.1.21"})

        targets = asyncio.run(frpguard.all_guest_targets())

        assert [item["ip"] for item in targets] == ["10.0.0.11", "10.0.0.12", "10.0.1.21"]
        assert [item["name"] for item in targets] == ["web", "db", "ct"]
        # 对外形状与普通用户那份一致：不带内部字段
        assert set(targets[0]) == {"ip", "name", "vmid", "node", "type"}

    def test_unreachable_connection_is_skipped(self, monkeypatch) -> None:
        connections = [
            ({"id": "c1"}, _FakeClient([_guest("pve1", 101, "web")])),
            ({"id": "c2"}, _FakeClient([], fail=True)),
        ]
        self._patch(monkeypatch, connections, {"101": "10.0.0.11"})

        targets = asyncio.run(frpguard.all_guest_targets())

        assert [item["ip"] for item in targets] == ["10.0.0.11"]

    def test_unresolved_ip_is_dropped_and_duplicates_collapse(self, monkeypatch) -> None:
        connections = [
            (
                {"id": "c1"},
                _FakeClient(
                    [
                        _guest("pve1", 101, "web"),
                        _guest("pve1", 102, "no-ip"),
                        _guest("pve1", 103, "same-ip"),
                    ]
                ),
            )
        ]
        self._patch(monkeypatch, connections, {"101": "10.0.0.11", "103": "10.0.0.11"})

        targets = asyncio.run(frpguard.all_guest_targets())

        assert [(item["vmid"], item["ip"]) for item in targets] == [(101, "10.0.0.11")]


class TestPolicyTargetsOnDemand:
    """管理员的目标清单按需拉：默认不算，带 with_targets=1 才算。"""

    def _stub(self, monkeypatch) -> None:
        async def _targets():
            return [
                {
                    "ip": "10.0.0.11",
                    "name": "web",
                    "vmid": 101,
                    "node": "pve1",
                    "type": "qemu",
                }
            ]

        monkeypatch.setattr(frpguard, "all_guest_targets", _targets)

    def test_admin_policy_skips_the_pve_scan_by_default(self, api, monkeypatch) -> None:
        self._stub(monkeypatch)

        body = api.get("/api/frp/policy", headers=auth_headers(api)).json()

        assert body["restricted"] is False
        assert body["targets"] == []
        assert body["own_ips"] == []

    def test_admin_can_ask_for_the_preset_list(self, api, monkeypatch) -> None:
        self._stub(monkeypatch)

        body = api.get(
            "/api/frp/policy",
            params={"with_targets": "1"},
            headers=auth_headers(api),
        ).json()

        assert body["restricted"] is False
        assert [item["ip"] for item in body["targets"]] == ["10.0.0.11"]
        assert body["own_ips"] == ["10.0.0.11"]

    def test_ordinary_user_still_gets_own_targets_without_the_flag(
        self, api, monkeypatch
    ) -> None:
        from test_api_routes import user_with_permissions

        entry = {
            "ip": "10.0.0.25",
            "name": "mine",
            "vmid": 105,
            "node": "pve",
            "type": "qemu",
        }

        async def _own(_username: str):
            return [entry]

        def _boom():
            raise AssertionError("普通用户不该走全平台清单")

        monkeypatch.setattr(frpguard, "own_guest_targets", _own)
        monkeypatch.setattr(frpguard, "all_guest_targets", _boom)

        headers = user_with_permissions(api, ["frp.view"], "frp-owner9")
        body = api.get("/api/frp/policy", headers=headers).json()

        assert body["restricted"] is True
        assert body["targets"] == [entry]
