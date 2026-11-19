"""证书管理 P0 加固的回归测试。

覆盖四件事：

* **私钥出口**：站点列表 / 详情只回「是否已配置」，绝不回私钥与证书链正文；
* **导出权限**：``cert.view`` 不再够用，导出需要 ``cert.export``，且必须留审计；
  PFX 口令只能走 POST body；
* **吊销闭环**：原因码白名单、本地私钥销毁、``cert_id`` 清空、CA 侧回查结果、
  审计落库；
* **根密钥轮换**：``SECRET_KEY_PREVIOUS`` 能解开旧密文，rewrap 后不再依赖旧密钥；
* **状态守卫**：已吊销不能在没有新证书材料时回到活动状态；
* **调度租约**：抢不到租约时只记 skip，不算失败。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from test_api_routes import api, auth_headers, user_with_permissions  # noqa: E402,F401

from app import certs, certstatus, crypto, scheduler  # noqa: E402
from app.config import settings  # noqa: E402


def _self_signed(domain: str = "p0.example.com"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=90))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode("ascii")
    return cert_pem, key_pem


def _upload_site(api, headers, domain: str = "p0.example.com"):
    """建一个托管站点并导入一对真实的自签证书 / 私钥。"""
    created = api.post(
        "/api/certs/sites",
        json={
            "name": "p0",
            "domain": domain,
            "deploy_method": "local",
            "deploy_dir": "/tmp/certs",
            "ca": "upload",
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    site = created.json()["site"]
    pem, key = _self_signed(domain)
    imported = api.post(
        f"/api/certs/sites/{site['id']}/certificate",
        json={"pem": pem, "key": key},
        headers=headers,
    )
    assert imported.status_code == 200, imported.text
    return site, pem, key


# --------------------------------------------------------------------- 私钥出口
def test_site_view_never_returns_private_key(api) -> None:
    admin = auth_headers(api)
    site, _pem, key = _upload_site(api, admin)

    listed = api.get("/api/certs", headers=admin)
    assert listed.status_code == 200
    # 整个响应体里都不应出现私钥正文（列表、展开详情、库存都不行）
    assert "PRIVATE KEY" not in listed.text
    assert key.strip().splitlines()[1][:20] not in listed.text

    row = next(item for item in listed.json()["sites"] if item["id"] == site["id"])
    assert row["acme_key"] == ""
    assert row["acme_pem"] == ""
    assert row["acme_key_set"] is True
    assert row["acme_pem_set"] is True


def test_import_response_also_hides_private_key(api) -> None:
    """导入接口的返回同样走站点视图，不能顺手把刚上传的私钥回显。"""
    admin = auth_headers(api)
    created = api.post(
        "/api/certs/sites",
        json={
            "name": "p0",
            "domain": "echo.example.com",
            "deploy_method": "local",
            "deploy_dir": "/tmp/certs",
            "ca": "upload",
        },
        headers=admin,
    ).json()["site"]
    pem, key = _self_signed("echo.example.com")
    resp = api.post(
        f"/api/certs/sites/{created['id']}/certificate",
        json={"pem": pem, "key": key},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert "PRIVATE KEY" not in resp.text
    assert resp.json()["site"]["acme_key_set"] is True


# --------------------------------------------------------------------- 导出权限
def test_export_requires_cert_export_permission(api) -> None:
    carol = user_with_permissions(api, ["cert.view", "cert.manage"], "carol")
    site, _pem, _key = _upload_site(api, carol)

    # 有查看 + 管理，但没有 cert.export：导出必须被拒
    denied = api.post(
        f"/api/certs/sites/{site['id']}/export", json={"format": "pem"}, headers=carol
    )
    assert denied.status_code == 403, denied.text
    assert (
        api.get(f"/api/certs/sites/{site['id']}/download", headers=carol).status_code
        == 403
    )


def test_admin_export_is_audited_and_pfx_not_in_url(api) -> None:
    admin = auth_headers(api)
    site, _pem, _key = _upload_site(api, admin)

    # GET 只允许 PEM：PFX 口令不能出现在 URL 里
    assert (
        api.get(
            f"/api/certs/sites/{site['id']}/download?format=pfx&password=secret",
            headers=admin,
        ).status_code
        == 400
    )

    resp = api.post(
        f"/api/certs/sites/{site['id']}/export",
        json={"format": "pfx", "password": "pfx-pass"},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"

    audit = api.get("/api/audit?action=cert.export", headers=admin)
    assert audit.status_code == 200, audit.text
    assert any(
        item["action"] == "cert.export" and item["result"] == "success"
        for item in audit.json()["items"]
    )


# --------------------------------------------------------------------- 吊销闭环
def test_revoke_wipes_local_key_and_audits(api) -> None:
    admin = auth_headers(api)
    site, _pem, _key = _upload_site(api, admin, "revoke.example.com")

    revoked = api.post(f"/api/certs/sites/{site['id']}/revoke", json={}, headers=admin)
    assert revoked.status_code == 200, revoked.text
    body = revoked.json()
    assert body["revoked"] is True
    assert body["verified"] is True  # 本地托管：销毁本地材料即完成
    assert body["cert_id"]

    fresh = api.get("/api/certs", headers=admin).json()["sites"]
    row = next(item for item in fresh if item["id"] == site["id"])
    assert row["cert_status"] == 10
    assert row["cert_id"] == ""
    assert row["acme_key"] == "" and row["acme_pem"] == ""
    assert row["acme_key_set"] is False
    assert row["revoked_cert_id"] == body["cert_id"]
    assert row["revoked_at"] > 0
    assert row["auto_renew"] is False

    audit = api.get("/api/audit?action=cert.revoke", headers=admin)
    assert any(item["result"] == "success" for item in audit.json()["items"])


def test_revoke_rejects_unknown_reason(api) -> None:
    admin = auth_headers(api)
    site, _pem, _key = _upload_site(api, admin, "reason.example.com")
    resp = api.post(
        f"/api/certs/sites/{site['id']}/revoke", json={"reason": 2}, headers=admin
    )
    assert resp.status_code == 400, resp.text
    # 失败也要留痕
    audit = api.get("/api/audit?action=cert.revoke", headers=admin)
    assert any(item["result"] == "failed" for item in audit.json()["items"])


# --------------------------------------------------------------- 根密钥轮换
def test_previous_secret_key_can_still_decrypt(monkeypatch) -> None:
    old_key = "A" * 40
    new_key = "B" * 40
    monkeypatch.setattr(settings, "secret_key", old_key)
    monkeypatch.setattr(settings, "secret_key_previous", "")
    token = crypto.encrypt("super-secret-value")
    assert crypto.decrypt(token) == "super-secret-value"

    # 换根密钥但保留旧值：仍能解开，并标记为需要重写
    monkeypatch.setattr(settings, "secret_key", new_key)
    monkeypatch.setattr(settings, "secret_key_previous", old_key)
    assert crypto.decrypt(token) == "super-secret-value"
    assert crypto.needs_rewrap(token) is True

    rewrapped = crypto.rewrap(token)
    assert crypto.decrypt(rewrapped) == "super-secret-value"
    assert crypto.needs_rewrap(rewrapped) is False

    # 清掉旧密钥后，新密文仍然可用，旧密文失效（符合轮换预期）
    monkeypatch.setattr(settings, "secret_key_previous", "")
    assert crypto.decrypt(rewrapped) == "super-secret-value"
    assert crypto.decrypt(token) == ""


# --------------------------------------------------- 云厂商吊销后的 CA 侧回查
class _FakeProvider:
    def __init__(self, info=None, error: bool = False) -> None:
        self.info = info
        self.error = error

    async def describe(self, cert_id):
        if self.error:
            raise RuntimeError("network down")
        return self.info


class _FakeInfo:
    def __init__(self, status: int) -> None:
        self.status = status


def test_cloud_revoke_verify_treats_missing_cert_as_done() -> None:
    assert asyncio.run(certs._verify_cloud_revoke(_FakeProvider(None), "cid")) is True


def test_cloud_revoke_verify_accepts_revoked_or_expired_state() -> None:
    assert asyncio.run(certs._verify_cloud_revoke(_FakeProvider(_FakeInfo(10)), "cid")) is True
    assert asyncio.run(certs._verify_cloud_revoke(_FakeProvider(_FakeInfo(3)), "cid")) is True


def test_cloud_revoke_verify_flags_still_issued_cert() -> None:
    assert asyncio.run(certs._verify_cloud_revoke(_FakeProvider(_FakeInfo(1)), "cid")) is False


def test_cloud_revoke_verify_is_not_confirmed_on_error() -> None:
    """回查失败只能是「未确认」，不能谎报成功。"""
    assert asyncio.run(certs._verify_cloud_revoke(_FakeProvider(error=True), "cid")) is False


def test_rewrap_endpoint_covers_legacy_tencent_key(api) -> None:
    """腾讯云密钥用的是独立存储 key，轮换时必须一起覆盖。"""
    admin = auth_headers(api)
    saved = api.put(
        "/api/certs/tencent",
        json={"secret_id": "AKIDrewrap", "secret_key": "sk-rewrap"},
        headers=admin,
    )
    assert saved.status_code == 200, saved.text

    resp = api.post("/api/certs/keys/rewrap", headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["rewrapped"]["providers"] >= 1

    view = api.get("/api/certs", headers=admin).json()
    assert view["secret_set"] is True
    assert view["tencent"]["secret_id"] == "AKIDrewrap"


def test_rewrap_endpoint_is_admin_only(api) -> None:
    bob = user_with_permissions(api, ["cert.view", "cert.manage"], "bob")
    assert api.post("/api/certs/keys/rewrap", headers=bob).status_code == 403


# --------------------------------------------------------------------- 状态守卫
def test_revoked_cannot_revive_without_new_certificate() -> None:
    site = {"cert_status": 10, "cert_id": "", "pending_cert_id": ""}
    try:
        certstatus.set_status(site, 1)
    except certstatus.StatusTransitionError:
        pass
    else:  # pragma: no cover - 守卫失效时给出明确失败
        raise AssertionError("已吊销的站点在没有新证书材料时不应回到已签发")

    site["pending_cert_id"] = "acme-new"
    certstatus.set_status(site, 1)
    assert site["cert_status"] == 1
    assert site["status_prev"] == 10


def test_same_status_does_not_overwrite_changed_at() -> None:
    site = {"cert_status": 0}
    certstatus.set_status(site, 1, changed_at=100)
    certstatus.set_status(site, 1, changed_at=200)
    assert site["status_changed_at"] == 100


# --------------------------------------------------------------------- 调度租约
def test_scheduler_skips_when_lease_is_held(monkeypatch) -> None:
    calls = []

    async def _run():
        calls.append(1)
        return "done"

    async def _deny(*args, **kwargs):
        return False

    monkeypatch.setattr(scheduler.joblease, "acquire", _deny)
    job = scheduler.Job(
        id="lease_test_denied",
        name="租约测试",
        group="测试",
        description="",
        run=_run,
        default_interval=60,
    )
    state = scheduler.JobState(job=job, interval=60)
    asyncio.run(scheduler._execute(state))

    assert calls == []  # 没有真正执行
    assert state.skipped == 1
    assert state.failures == 0
    assert state.last_status == "never"


def test_scheduler_runs_and_releases_lease(monkeypatch) -> None:
    async def _allow(*args, **kwargs):
        return True

    released = []

    async def _release(job_id):
        released.append(job_id)

    async def _run():
        return "ok"

    monkeypatch.setattr(scheduler.joblease, "acquire", _allow)
    monkeypatch.setattr(scheduler.joblease, "release", _release)
    job = scheduler.Job(
        id="lease_test_allow",
        name="租约测试",
        group="测试",
        description="",
        run=_run,
        default_interval=60,
    )
    state = scheduler.JobState(job=job, interval=60)
    asyncio.run(scheduler._execute(state))

    assert state.last_status == "ok"
    assert state.runs == 1
    assert released == ["lease_test_allow"]
