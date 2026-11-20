"""一键接入：局域网发现 + 只读账号/令牌自动开通。

网络部分不真连 PVE（CI 里没有），重点验证三件事：
  1. 网段解析只接受内网，格式错要能说清楚；
  2. 扫描结果的归属（哪些网段、命中几台、是否被截断、是否已在列表里）；
  3. 开通流程的顺序与落库内容 —— 建的是只读账号，默认**不**保存管理员密码。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app import pvediscover
from app.schemas import ConnectionDiscoverIn, ConnectionProvisionIn


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ 网段解析
def test_parse_cidrs_accepts_private_and_single_host() -> None:
    assert pvediscover.parse_cidrs("192.168.1.0/24, 10.0.0.5") == [
        "192.168.1.0/24",
        "10.0.0.5/32",
    ]


def test_parse_cidrs_rejects_public_range() -> None:
    """面板不是扫描器：公网网段一律拒绝。"""
    with pytest.raises(ValueError, match="内网"):
        pvediscover.parse_cidrs("8.8.8.0/24")


def test_parse_cidrs_rejects_broken_input() -> None:
    with pytest.raises(ValueError, match="格式"):
        pvediscover.parse_cidrs("192.168.1.0/33")


def test_scan_covers_both_ends_of_a_small_subnet() -> None:
    """/30 只有两个可用地址，不要扫到网络号 / 广播地址。"""
    hosts, truncated = pvediscover._hosts(["192.168.1.0/30"], 1024)
    assert hosts == ["192.168.1.1", "192.168.1.2"]
    assert truncated is False


def test_scan_truncates_oversized_subnet() -> None:
    hosts, truncated = pvediscover._hosts(["10.0.0.0/22"], 10)
    assert len(hosts) == 10
    assert truncated is True


# -------------------------------------------------------------------- 扫描
def test_scan_sorts_found_hosts(monkeypatch) -> None:
    """命中结果按 IP 升序，界面上同一网段的机器才是稳定顺序。"""

    async def fake_probe(host: str, port: int, sem) -> Dict[str, Any] | None:
        if host in {"192.168.1.20", "192.168.1.4"}:
            return {
                "host": host,
                "port": port,
                "latency_ms": 1,
                "verified": True,
                "version": "8.2.4",
                "release": "8.2",
                "repoid": "",
            }
        return None

    monkeypatch.setattr(pvediscover, "_probe", fake_probe)
    result = _run(pvediscover.scan(["192.168.1.0/27"], 8006))

    assert [item["host"] for item in result["found"]] == [
        "192.168.1.4",
        "192.168.1.20",
    ]
    assert result["scanned"] == 30
    assert result["truncated"] is False


def test_discover_endpoint_marks_known_hosts(monkeypatch) -> None:
    from app.routers import config as cfg_router

    async def fake_scan(cidrs, port):
        return {
            "cidrs": cidrs,
            "port": port,
            "scanned": 2,
            "found": [
                {
                    "host": "192.168.1.10",
                    "port": 8006,
                    "latency_ms": 2,
                    "verified": True,
                    "version": "8.2.4",
                    "release": "8.2",
                    "repoid": "",
                },
                {
                    "host": "192.168.1.11",
                    "port": 8006,
                    "latency_ms": 3,
                    "verified": True,
                    "version": "8.2.4",
                    "release": "8.2",
                    "repoid": "",
                },
            ],
            "truncated": False,
            "elapsed_ms": 12,
        }

    async def noop_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(cfg_router.pvediscover, "scan", fake_scan)
    monkeypatch.setattr(cfg_router.security, "audit", noop_audit)
    monkeypatch.setattr(
        cfg_router.store,
        "get_connections",
        lambda: [{"host": "192.168.1.10", "port": 8006}],
    )

    result = _run(
        cfg_router.discover_connections(
            ConnectionDiscoverIn(cidr="192.168.1.0/24"),
            request=None,
            user={"username": "admin"},
        )
    )
    flags = {item["host"]: item["already_added"] for item in result["found"]}
    assert flags == {"192.168.1.10": True, "192.168.1.11": False}


def test_discover_endpoint_rejects_public_cidr() -> None:
    from app.routers import config as cfg_router

    with pytest.raises(HTTPException) as excinfo:
        _run(
            cfg_router.discover_connections(
                ConnectionDiscoverIn(cidr="8.8.8.0/24"),
                request=None,
                user={"username": "admin"},
            )
        )
    assert excinfo.value.status_code == 400


# -------------------------------------------------------------------- 开通
class _FakeClient:
    """按调用顺序记录的假 PVE 客户端（管理员客户端 + 令牌复验客户端）。"""

    created: List["_FakeClient"] = []

    def __init__(self, conn) -> None:
        self.conn = conn
        self.calls: List[str] = []
        _FakeClient.created.append(self)

    async def login(self) -> None:
        self.calls.append("login")

    async def ensure_user(self, userid: str, *, comment: str = "", password: str = "") -> None:
        self.calls.append(f"ensure_user:{userid}")

    async def grant_user_role(self, userid: str, role: str = "PVEAuditor", path: str = "/") -> None:
        self.calls.append(f"grant:{userid}:{role}:{path}")

    async def create_api_token(
        self, userid: str, tokenid: str, *, privsep: bool = False, comment: str = ""
    ) -> Dict[str, str]:
        self.calls.append(f"token:{userid}:{tokenid}:privsep={int(privsep)}")
        return {"full_tokenid": f"{userid}!{tokenid}", "value": "secret-value"}

    async def version(self) -> Dict[str, Any]:
        return {"version": "8.2.4"}

    async def nodes(self) -> List[Dict[str, Any]]:
        return [{"node": "pve1"}, {"node": "pve2"}]

    async def aclose(self) -> None:
        return None


def _patch_provision_deps(monkeypatch, *, existing=None, saved=None):
    from app.routers import config as cfg_router

    existing = existing or []
    saved = saved if saved is not None else []

    async def fake_save(rows):
        saved[:] = rows

    async def fake_activate(conn_id):
        return None

    async def noop_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(cfg_router, "ProxmoxClient", _FakeClient)
    monkeypatch.setattr(cfg_router, "rebuild_client", lambda: None)
    monkeypatch.setattr(cfg_router.security, "audit", noop_audit)
    monkeypatch.setattr(cfg_router.store, "get_connections", lambda: existing)
    monkeypatch.setattr(cfg_router.store, "save_connections", fake_save)
    monkeypatch.setattr(cfg_router.store, "set_active_connection", fake_activate)
    monkeypatch.setattr(cfg_router.store, "get_active_connection_id", lambda: "")
    monkeypatch.setattr(
        cfg_router.store,
        "public_connection_config",
        lambda profile: {
            "id": profile["id"],
            "name": profile["name"],
            "host": profile["host"],
            "port": profile["port"],
        },
    )
    return cfg_router, saved


def _provision_body(**over) -> ConnectionProvisionIn:
    data: Dict[str, Any] = {
        "host": "192.168.1.10",
        "port": 8006,
        "username": "root@pam",
        "password": "pve-root-pass",
    }
    data.update(over)
    return ConnectionProvisionIn(**data)


def test_provision_creates_readonly_account_and_saves(monkeypatch) -> None:
    _FakeClient.created = []
    cfg_router, saved = _patch_provision_deps(monkeypatch)

    result = _run(
        cfg_router.provision_connection(
            _provision_body(region="广州"), request=None, user={"username": "admin"}
        )
    )

    admin = _FakeClient.created[0]
    # 顺序有意义：先登录，再建账号、授权，最后建令牌
    assert admin.calls[0] == "login"
    assert "ensure_user:pvecenter@pve" in admin.calls
    assert "grant:pvecenter@pve:PVEAuditor:/" in admin.calls
    assert any(call.startswith("token:pvecenter@pve:pvecenter-") for call in admin.calls)
    assert "privsep=0" in admin.calls[-1]

    profile = saved[-1]
    assert profile["host"] == "192.168.1.10"
    assert profile["token_id"].startswith("pvecenter@pve!pvecenter-")
    assert profile["token_secret"] == "secret-value"
    # 默认不保存管理员密码，只存只读账号的令牌
    assert "console_password" not in profile
    assert profile["region"] == "广州"

    assert result["pve_user"] == "pvecenter@pve"
    assert result["nodes"] == ["pve1", "pve2"]
    assert result["node"] == "pve1"
    assert result["version"] == "8.2.4"


def test_provision_can_store_admin_password_when_asked(monkeypatch) -> None:
    _FakeClient.created = []
    cfg_router, saved = _patch_provision_deps(monkeypatch)

    _run(
        cfg_router.provision_connection(
            _provision_body(store_admin_password=True),
            request=None,
            user={"username": "admin"},
        )
    )
    assert saved[-1]["console_user"] == "root@pam"
    assert saved[-1]["console_password"] == "pve-root-pass"


def test_provision_rejects_duplicate_endpoint(monkeypatch) -> None:
    """同一 host:port 已在列表里就不要再建一套账号 / 令牌。"""
    _FakeClient.created = []
    cfg_router, _ = _patch_provision_deps(
        monkeypatch, existing=[{"id": "x", "host": "192.168.1.10", "port": 8006}]
    )

    with pytest.raises(HTTPException) as excinfo:
        _run(
            cfg_router.provision_connection(
                _provision_body(), request=None, user={"username": "admin"}
            )
        )
    assert excinfo.value.status_code == 409
    assert _FakeClient.created == []


def test_provision_reports_bad_credentials(monkeypatch) -> None:
    """登录失败要翻成一句能照着改的中文，而不是 PVE 的英文原文。"""
    from app.pve import ProxmoxError

    cfg_router, saved = _patch_provision_deps(monkeypatch)

    class _BadLogin(_FakeClient):
        async def login(self) -> None:
            raise ProxmoxError("invalid credentials", 401, "POST /access/ticket")

    monkeypatch.setattr(cfg_router, "ProxmoxClient", _BadLogin)

    with pytest.raises(HTTPException) as excinfo:
        _run(
            cfg_router.provision_connection(
                _provision_body(), request=None, user={"username": "admin"}
            )
        )
    assert excinfo.value.status_code == 400
    assert "账号或密码不正确" in excinfo.value.detail
    assert saved == []
