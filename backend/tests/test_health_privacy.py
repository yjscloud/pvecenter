"""FD-04：/api/health 不得向匿名用户泄露内网拓扑与版本。

此前匿名就能读到 PVE 版本、节点数与连接状态。现在的口径：

* 匿名：``{"status": "ok", "authenticated": false}``，且**不探测 PVE**；
* 已登录：``authenticated: true``，字段与以前一致（前端登录后各处仍靠它轮询
  PVE 状态）。

``authenticated`` 这一位不是装饰：少了它，匿名载荷与「已配置但连不上」的
诊断载荷在字段上无从分辨（``pve_configured`` / ``pve_connected`` 缺省），
前端只能把「探测结果未知」误报成「PVE 连不上」。

用例不连数据库：匿名分支在鉴权层就返回 None，登录分支把 ``get_client``
换成一个「未配置」的假客户端。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient

from app import main, security
from app.routers import config


def test_anonymous_health_only_reports_status(monkeypatch: Any) -> None:
    """HTTP 层：匿名请求只拿到 status，顺带确认没有去问 PVE。"""

    def _boom() -> Any:  # pragma: no cover - 被调用即失败
        raise AssertionError("匿名健康检查不该向 PVE 发请求")

    monkeypatch.setattr(config, "get_client", _boom)

    client = TestClient(main.app)
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "authenticated": False}


def test_expired_cookie_is_marked_unauthenticated(monkeypatch: Any) -> None:
    """凭据过期时仍是匿名档，但必须**显式**标记，不能冒充诊断结果。

    access cookie 过期后 ``/health`` 不再返回完整读数（正确：它认不出用户）。
    这条路径曾经是顶部「无法连接到 Proxmox 主机」误报的来源 —— 前端把缺省的
    ``pve_connected`` 当成 false。现在靠 ``authenticated: false`` 区分，
    这条用例把「过期凭据 = 匿名档」钉住，避免以后又被读成故障。
    """

    def _boom() -> Any:  # pragma: no cover - 被调用即失败
        raise AssertionError("凭据无效时不该向 PVE 发请求")

    monkeypatch.setattr(config, "get_client", _boom)

    expired = security._encode(
        {"sub": "admin", "role": "admin", "ver": 0, "sid": "", "typ": "access"},
        datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    client = TestClient(main.app)
    response = client.get("/api/health", cookies={security.ACCESS_COOKIE: expired})

    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok", "authenticated": False}
    assert "pve_connected" not in body
    assert "pve_configured" not in body


def test_authenticated_health_keeps_diagnostics(monkeypatch: Any) -> None:
    """登录后仍是原来的完整读数（这里用「未配置连接」这一支，不必真连 PVE）。"""

    class _Conn:
        configured = False

    class _Client:
        conn = _Conn()

    monkeypatch.setattr(config, "get_client", lambda: _Client())

    result = asyncio.run(config.health(user={"username": "admin"}))

    assert result["status"] == "ok"
    assert result["authenticated"] is True
    assert result["pve_configured"] is False
    assert "pve_version" in result
    assert "node_count" in result
    assert "version" in result
