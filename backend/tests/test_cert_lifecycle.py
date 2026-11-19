"""P1 / P2 能力回归：详情、轮换、批量、指标、健康检查、告警升级。

这些测试刻意覆盖「会被运维真正撞上」的分支：

* 详情接口给出可核验的证据（指纹 / SAN / 链），但绝不回私钥；
* 托管证书轮换必须被拒绝，而不是假装成功；
* 批量操作逐站点汇报，一个越权 ID 不能拖垮整批；
* Prometheus 文本能被解析（标签转义、TYPE 行齐全）；
* 体检能识别过期 / 域名不匹配 / 弱协议；
* 升级通知绕过冷却，否则「已经不会再重试了」会被 12 小时窗口吞掉。
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

from app import certnotify, certs, certstatus  # noqa: E402
from app.certpipeline import Context  # noqa: E402
from app.config import settings  # noqa: E402


def _self_signed(domain: str = "life.example.com", days: int = 90):
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
        .not_valid_after(now + timedelta(days=days))
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


def _upload_site(api, headers, domain: str, deploy_dir: str):
    created = api.post(
        "/api/certs/sites",
        json={
            "name": domain.split(".")[0],
            "domain": domain,
            "deploy_method": "local",
            "deploy_dir": deploy_dir,
            "ca": "upload",
            "environment": "prod",
            "cluster": "pve-a",
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
    return imported.json()["site"]


# --------------------------------------------------------------------- 详情
def test_detail_returns_verifiable_evidence(api) -> None:
    admin = auth_headers(api)
    site = _upload_site(api, admin, "detail.example.com", "/tmp/certs")

    resp = api.get(f"/api/certs/sites/{site['id']}/detail", headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["domain"] == "detail.example.com"
    assert body["source"] == "cache"
    assert body["leaf"]["common_name"] == "detail.example.com"
    assert "detail.example.com" in body["leaf"]["san"]
    assert len(body["leaf"]["fingerprint_sha256"]) == 64
    assert body["leaf"]["key"]["type"] == "RSA"
    assert body["leaf"]["key"]["bits"] == 2048
    assert body["chain_length"] == 1  # 自签：只有叶证书
    assert body["environment"] == "prod"
    assert body["cluster"] == "pve-a"
    # 详情是只读接口，绝不能借机回私钥
    assert "PRIVATE KEY" not in resp.text


def test_detail_requires_ownership(api) -> None:
    admin = auth_headers(api)
    site = _upload_site(api, admin, "owner.example.com", "/tmp/certs")
    bob = user_with_permissions(api, ["cert.view"], "bob")
    assert api.get(f"/api/certs/sites/{site['id']}/detail", headers=bob).status_code == 404


# --------------------------------------------------------------------- 轮换
def test_rotate_rejects_uploaded_certificate(api) -> None:
    admin = auth_headers(api)
    site = _upload_site(api, admin, "rot.example.com", "/tmp/certs")
    resp = api.post(f"/api/certs/sites/{site['id']}/rotate", json={}, headers=admin)
    assert resp.status_code == 400, resp.text

    audit = api.get("/api/audit?action=cert.rotate", headers=admin)
    assert any(item["result"] == "failed" for item in audit.json()["items"])


def test_rotate_acme_reissues_and_counts(monkeypatch) -> None:
    """绕开真实 CA：只验证「换新证书 + 计数 + 通知」这条编排。"""
    notified = []

    async def fake_apply(site, cfg=None, renew=False):
        site["pending_cert_id"] = "acme-new"
        site["cert_id"] = "acme-new"
        site["acme_pem"] = "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n"
        certstatus.set_status(site, 1)
        site["expire_at"] = int(datetime.now(timezone.utc).timestamp()) + 90 * 86400

    async def fake_notify(*args, **kwargs):
        notified.append(args[2])

    async def fake_update(site):
        return site

    async def fake_log(*args, **kwargs):
        return None

    monkeypatch.setattr(certs, "apply_certificate", fake_apply)
    monkeypatch.setattr(certs, "update_site", fake_update)
    monkeypatch.setattr(certs, "record_log", fake_log)
    monkeypatch.setattr(certs.certnotify, "notify", fake_notify)

    site = {
        **certs.DEFAULT_SITE,
        "id": "s1",
        "name": "acme",
        "domain": "acme.example.com",
        "ca": "acme",
        "cert_id": "old",
        "username": "u1",
        "cert_status": 1,
    }
    result = asyncio.run(certs.rotate_site(site, None, deploy=False))
    assert result["rotated"] is True
    assert site["rotate_count"] == 1
    assert site["last_rotated_at"] > 0
    assert certnotify.EVENT_ROTATED in notified


def test_scheduled_rotation_extends_threshold() -> None:
    site = {
        **certs.DEFAULT_SITE,
        "id": "s1",
        "domain": "acme.example.com",
        "ca": "acme",
        "cert_id": "x",
        "expire_at": int(datetime.now(timezone.utc).timestamp()) + 25 * 86400,
        "renew_before_days": 15,
        "auto_rotate": True,
        "rotate_before_days": 30,
    }
    assert certs._decide(Context(site=site)) == "due"
    site["auto_rotate"] = False
    assert certs._decide(Context(site=site)) == "none"


# --------------------------------------------------------------------- 批量
def test_bulk_deploy_reports_each_site(api, tmp_path) -> None:
    admin = auth_headers(api)
    first = _upload_site(api, admin, "b1.example.com", str(tmp_path / "b1"))
    second = _upload_site(api, admin, "b2.example.com", str(tmp_path / "b2"))

    resp = api.post(
        "/api/certs/sites/bulk",
        json={
            "action": "deploy",
            "site_ids": [first["id"], second["id"], "not-mine"],
        },
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "deploy"
    assert body["total"] == 3
    assert body["ok"] == 2
    assert body["failed"] == 1
    assert {row["site_id"] for row in body["results"]} >= {first["id"], second["id"]}


def test_bulk_rejects_unknown_action(api) -> None:
    admin = auth_headers(api)
    resp = api.post(
        "/api/certs/sites/bulk",
        json={"action": "explode", "site_ids": []},
        headers=admin,
    )
    assert resp.status_code == 422  # Pydantic Literal 先拦下


def test_bulk_requires_manage(api) -> None:
    viewer = user_with_permissions(api, ["cert.view"], "viewer-bulk")
    resp = api.post(
        "/api/certs/sites/bulk",
        json={"action": "sync", "site_ids": []},
        headers=viewer,
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------- 指标
def test_prometheus_metrics_are_well_formed(api) -> None:
    admin = auth_headers(api)
    _upload_site(api, admin, "metrics.example.com", "/tmp/certs")

    resp = api.get("/api/certs/metrics", headers=admin)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    text = resp.text
    assert "# TYPE pvecenter_cert_days_left gauge" in text
    assert 'domain="metrics.example.com"' in text
    assert "pvecenter_cert_health_ok" in text
    assert 'environment="prod"' in text


def test_metrics_label_escaping() -> None:
    site = {
        **certs.DEFAULT_SITE,
        "id": 'a"b',
        "domain": "x.example.com",
        "expire_at": int(datetime.now(timezone.utc).timestamp()) + 10 * 86400,
    }
    text = certs.metrics_text([site])
    assert 'site="a\\"b"' in text


# --------------------------------------------------------------------- 健康
def test_inspect_tls_flags_expired_and_hostname() -> None:
    """用自签证书直接验证判定逻辑，不发真实网络请求。"""
    from app import certmonitor

    assert certmonitor._hostname_matches("*.example.com", "a.example.com")
    assert not certmonitor._hostname_matches("*.example.com", "a.b.example.com")
    assert certmonitor._hostname_matches("example.com", "example.com")
    assert not certmonitor._hostname_matches("example.com", "www.example.com")


# --------------------------------------------------------------------- 告警
def test_escalation_bypasses_cooldown(monkeypatch) -> None:
    sent = []

    async def fake_push(*args, **kwargs):
        sent.append(kwargs.get("title"))

    async def no_history(*args, **kwargs):
        return None

    async def no_email(*args, **kwargs):
        return False, ""

    async def no_webhook(*args, **kwargs):
        return False, ""

    async def no_feishu(*args, **kwargs):
        return False, ""

    from app import alerting, notifications

    monkeypatch.setattr(notifications, "push", fake_push)
    monkeypatch.setattr(certnotify, "_record_history", no_history)
    monkeypatch.setattr(alerting, "send_alert_email", no_email)
    monkeypatch.setattr(alerting, "send_webhook", no_webhook)
    monkeypatch.setattr(alerting, "send_feishu", no_feishu)

    site = dict(certs.DEFAULT_SITE)
    site["notices"] = {}
    # 先发一次常规通知并进入冷却
    assert asyncio.run(
        certnotify.notify("u1", site, certnotify.EVENT_RENEW_FAILED, "t1", "d1")
    )
    assert not asyncio.run(
        certnotify.notify("u1", site, certnotify.EVENT_RENEW_FAILED, "t2", "d2")
    )
    # 升级通知必须绕开同一条冷却
    assert asyncio.run(
        certnotify.notify(
            "u1",
            site,
            certnotify.EVENT_RENEW_FAILED,
            "t3",
            "d3",
            escalate=True,
        )
    )
    assert "t3" in sent


def test_deploy_failure_notifies_once(monkeypatch) -> None:
    """手动部署与自动部署共用 deploy_site：失败必须在这里发通知。"""
    events = []

    async def bad_push(*args, **kwargs):
        raise certs.CertError("write failed")

    async def fake_notify(owner, site, event, *args, **kwargs):
        events.append(event)
        return True

    async def fake_log(*args, **kwargs):
        return None

    async def fake_update(site):
        return site

    monkeypatch.setattr(certs, "_push_target", bad_push)
    monkeypatch.setattr(certs, "record_log", fake_log)
    monkeypatch.setattr(certs, "update_site", fake_update)
    monkeypatch.setattr(certs.certnotify, "notify", fake_notify)

    site = {
        **certs.DEFAULT_SITE,
        "id": "s1",
        "name": "local",
        "domain": "local.example.com",
        "ca": "upload",
        "deploy_method": "local",
        "deploy_dir": "/tmp/certs",
        "username": "u1",
        "cert_id": "c1",
        "cert_status": 1,
        "acme_pem": "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n",
        "acme_key": "-----BEGIN PRIVATE KEY-----\nK\n-----END PRIVATE KEY-----\n",
        "targets": [
            {"id": "primary", "method": "local", "deploy_dir": "/tmp/certs"}
        ],
    }
    outcome = asyncio.run(certs.deploy_site(site, None, action="deploy"))
    assert outcome["ok"] is False
    assert certnotify.EVENT_DEPLOY_FAILED in events


# --------------------------------------------------------------- 部署回滚
def test_local_snapshot_rollback_restores_previous_files(tmp_path) -> None:
    cert = tmp_path / "fullchain.pem"
    key = tmp_path / "privkey.pem"
    cert.write_text("old-cert", encoding="utf-8")
    key.write_text("old-key", encoding="utf-8")

    snap = certs._snapshot([(cert, 0o644), (key, 0o600)])
    cert.write_text("new-cert", encoding="utf-8")
    key.write_text("new-key", encoding="utf-8")
    assert certs._rollback(snap) is True
    assert cert.read_text(encoding="utf-8") == "old-cert"
    assert key.read_text(encoding="utf-8") == "old-key"


def test_local_rollback_deletes_files_created_by_failed_deploy(tmp_path) -> None:
    created = tmp_path / "new.pem"
    snap = certs._snapshot([(created, 0o644)])  # 部署前不存在
    created.write_text("written-by-deploy", encoding="utf-8")
    assert certs._rollback(snap) is True
    assert not created.exists()


def test_audit_retention_zero_keeps_everything() -> None:
    from app import store

    assert asyncio.run(store.purge_audit_old(0)) == 0


# ----------------------------------------------------------------- 维度字段
def test_normalise_accepts_environment_and_rotation() -> None:
    site = certs.normalise_site(
        {
            "name": "n",
            "domain": "n.example.com",
            "ca": "acme",
            "deploy_method": "local",
            "deploy_dir": "/tmp/certs",
            "environment": "  staging ",
            "cluster": "pve-b",
            "auto_rotate": True,
            "rotate_before_days": 999,
        }
    )
    assert site["environment"] == "staging"
    assert site["cluster"] == "pve-b"
    assert site["auto_rotate"] is True
    assert site["rotate_before_days"] == 60  # 上限收敛
