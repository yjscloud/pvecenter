"""「测试连通性」出站目标准入（SSRF 兜底）的硬约定。

背景：``/api/config/connection/test`` 与 ``/api/pbs/config/test`` 让面板后端按
前端给的 host:port 发起请求，面板又常与 PVE / PBS / 跳板机同网段。这里钉住
四件事：

* 未登记、未白名单的目标一律拒绝（不能把前端输入当目标）；
* 已登记 / 已白名单的私网目标放行（连内网 PVE 是正常业务）；
* 回环、链路本地（含云元数据）、组播、保留地址即使登记/白名单也拒绝
  （回环例外：精确登记的 host:port 可以，本机 PVE 是真实部署形态）；
* 失败原因归一化成固定枚举，绝不回显对端正文。
"""
from __future__ import annotations

import asyncio
import ssl
from typing import Any, Dict, List

import httpx
import pytest

from app import ssrfguard
from app.pve import ProxmoxError


def _check(
    host: str,
    port: int = 8006,
    *,
    registered: Any = (),
    allow: str = "",
) -> ssrfguard.Target:
    return asyncio.run(
        ssrfguard.assert_target_allowed(
            host, port, registered=registered, allow_targets=allow
        )
    )


# ---------------------------------------------------------------- 目标收窄
def test_unregistered_private_ip_is_rejected() -> None:
    with pytest.raises(ssrfguard.TargetBlocked) as exc:
        _check("172.16.149.5", 8007)
    assert exc.value.code == ssrfguard.TARGET_NOT_ALLOWED


def test_registered_private_target_is_allowed() -> None:
    target = _check(
        "172.16.149.3",
        8006,
        registered={("172.16.149.3", 8006)},
    )
    assert target == ("172.16.149.3", 8006)


def test_registered_host_does_not_open_other_ports() -> None:
    """登记的是 8006，不能拿同一个 host 去扫 22 / 3306。"""
    with pytest.raises(ssrfguard.TargetBlocked):
        _check("172.16.149.3", 22, registered={("172.16.149.3", 8006)})


def test_allowlist_cidr_permits_private_addresses() -> None:
    assert _check("172.16.149.42", 8006, allow="172.16.149.0/24") == (
        "172.16.149.42",
        8006,
    )


def test_allowlist_hostname_is_case_insensitive() -> None:
    # 解析在部分环境里可能失败；白名单按主机名命中，不依赖这一次解析结果
    assert _check("PBS.LAN", 8007, allow="pbs.lan") == ("pbs.lan", 8007)


def test_scheme_and_embedded_port_match_saved_connection() -> None:
    """``http://127.0.0.1:8006`` 这种写法要按实际出站地址归一化。"""
    assert _check(
        "http://127.0.0.1:8006",
        8006,
        registered={("127.0.0.1", 8006)},
    ) == ("127.0.0.1", 8006)


def test_loopback_requires_exact_registration() -> None:
    """本机 PVE（127.0.0.1:8006）是真实部署形态，但只放行精确登记的那一条。"""
    with pytest.raises(ssrfguard.TargetBlocked):
        _check("127.0.0.1", 8006)
    assert _check(
        "127.0.0.1", 8006, registered={("127.0.0.1", 8006)}
    ) == ("127.0.0.1", 8006)
    with pytest.raises(ssrfguard.TargetBlocked):
        _check("127.0.0.1", 22, registered={("127.0.0.1", 8006)})


def test_cloud_metadata_address_is_always_blocked() -> None:
    """169.254.169.254 即便写进白名单也不许探 —— 没有任何 PVE/PBS 用它。"""
    with pytest.raises(ssrfguard.TargetBlocked) as exc:
        _check("169.254.169.254", 80, allow="169.254.169.254")
    assert exc.value.code == ssrfguard.ADDRESS_BLOCKED


def test_reserved_address_is_always_blocked() -> None:
    with pytest.raises(ssrfguard.TargetBlocked) as exc:
        _check("240.0.0.1", 8006, registered={("240.0.0.1", 8006)})
    assert exc.value.code == ssrfguard.ADDRESS_BLOCKED


def test_ipv6_loopback_registration_is_honoured() -> None:
    assert _check("::1", 8006, registered={("::1", 8006)}) == ("::1", 8006)


def test_dns_name_resolving_to_private_needs_registration(monkeypatch) -> None:
    """主机名先解析再判定：解析出私网地址且未登记 → 拒绝。"""
    import ipaddress

    async def fake_resolve(host: str, port: int) -> List[Any]:
        return [ipaddress.ip_address("172.16.149.3")]

    monkeypatch.setattr(ssrfguard, "_resolve", fake_resolve)
    with pytest.raises(ssrfguard.TargetBlocked):
        _check("pve.internal", 8006)
    assert _check("pve.internal", 8006, allow="172.16.149.0/24") == (
        "pve.internal",
        8006,
    )


def test_invalid_targets_are_rejected() -> None:
    for bad in ("", " ", "http://", "pve lan"):
        with pytest.raises(ssrfguard.TargetBlocked) as exc:
            _check(bad, 8006, allow="0.0.0.0/0")
        assert exc.value.code == ssrfguard.TARGET_INVALID


def test_scheme_with_path_or_userinfo_is_rejected() -> None:
    """路径 / userinfo 会让「归一化主机」和实际连接地址不一致，必须拒绝。"""
    registered = {("172.16.149.3", 8006)}
    for bad in (
        "http://172.16.149.3/x:8006",
        "https://172.16.149.3/?x=1",
        "https://user:pw@172.16.149.3:8006",
        "172.16.149.3/x",
    ):
        with pytest.raises(ssrfguard.TargetBlocked) as exc:
            _check(bad, 8006, registered=registered)
        assert exc.value.code == ssrfguard.TARGET_INVALID


def test_registered_targets_normalises_saved_rows() -> None:
    from app import ssrfguard as guard

    rows = [
        {"host": "https://172.16.149.3", "port": 8006},
        {"host": "pbs.lan", "port": 8007},
        {"host": "", "port": 8007},
        "not-a-dict",
    ]
    assert guard.registered_targets(rows, 8006) == {
        ("172.16.149.3", 8006),
        ("pbs.lan", 8007),
    }


def test_same_target_rejects_stored_secret_going_elsewhere() -> None:
    profile: Dict[str, Any] = {"host": "172.16.149.3", "port": 8006}
    assert ssrfguard.same_target("172.16.149.3", 8006, profile, 8006) is True
    assert ssrfguard.same_target("172.16.149.9", 8006, profile, 8006) is False
    assert ssrfguard.same_target("172.16.149.3", 22, profile, 8006) is False


def test_outbound_clients_do_not_follow_redirects() -> None:
    """3xx 一律不跟随：否则一次「测试」可以被跳转到私网 / 元数据地址。"""
    from app.pbs import PbsClient, PbsConnection
    from app.pve import ProxmoxClient, PveConnection

    async def check() -> None:
        pve_client = await ProxmoxClient(PveConnection(host="pve.lan"))._http()
        pbs_client = await PbsClient(PbsConnection(host="pbs.lan"))._http()
        try:
            assert pve_client.follow_redirects is False
            assert pbs_client.follow_redirects is False
        finally:
            await pve_client.aclose()
            await pbs_client.aclose()

    asyncio.run(check())


# ---------------------------------------------------------------- 失败归一化
def test_auth_failure_maps_to_enum() -> None:
    exc = ProxmoxError("HTTP 401 from /api2/json/version", 502, pve_status=401)
    code, message = ssrfguard.classify_failure(exc)
    assert code == "auth_failed"
    assert "401" not in message
    assert "/api2/json" not in message


def test_tls_handshake_failure_maps_to_not_tls() -> None:
    exc = ProxmoxError("Cannot reach https://172.16.149.3:22", 502)
    exc.__cause__ = ssl.SSLError("[SSL: WRONG_VERSION_NUMBER] wrong version number")
    code, _ = ssrfguard.classify_failure(exc)
    assert code == "not_tls"


def test_connect_error_maps_to_unreachable() -> None:
    exc = ProxmoxError("Cannot reach https://172.16.149.3:8006", 502)
    exc.__cause__ = httpx.ConnectError("connection refused")
    assert ssrfguard.classify_failure(exc)[0] == "unreachable"


def test_timeout_maps_to_timeout() -> None:
    exc = ProxmoxError("Proxmox request timed out", 504)
    assert ssrfguard.classify_failure(exc)[0] == "timeout"


def test_peer_response_body_is_never_echoed() -> None:
    """对端正文（可能含内部路径 / 页面内容）不能出现在回给前端的文案里。"""
    exc = ProxmoxError(
        "<html><body>secret internal path /admin/console</body></html>", 500
    )
    code, message = ssrfguard.classify_failure(exc)
    assert code == "http_error"
    assert "secret" not in message
    assert "/admin" not in message
