"""ACME「验证配置」：只读检查 CA 账号与 DNS 凭据。

回归点：ACME 以前 `test_disabled=True`（理由是没有「列证书」接口），结果目录
拼错、EAB 参数错、DNS 密钥失效这些问题都要等到点「申请」、等 DNS 传播完才
暴露。现在分两段验证，全程不签发、不写 TXT：

* CA：读目录 + 注册/复用账号（复用已保存的 account_key，不会重复建号）；
* DNS：用该用户的 ACME 站点域名做一次托管域探测（`DnsProvider.check_domain`）。

    ../.venv/bin/python -m pytest tests/test_cert_acme_test.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import acme, certs, dnsproviders  # noqa: E402

CFG = {
    "email": "ops@example.com",
    "directory": acme.DIRECTORY_LETSENCRYPT,
    "dns_provider": "dnspod",
    "account_key": "-----BEGIN EC PRIVATE KEY-----…",
    "eab_kid": "",
    "eab_hmac_key": "",
    "dnspod": {"secret_id": "sid", "secret_key": "sk"},
}


def _patch(monkeypatch, *, account_error: str = "", zone: str = "yjscloud.com",
           zone_error: str = "", sites=None, cfg=None):
    """装配 load_acme / AcmeClient / load_sites / DNS provider 四个桩。"""
    monkeypatch.setattr(certs, "load_acme", _async(lambda _owner: dict(cfg or CFG)))

    class FakeClient:
        def __init__(self, _directory, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def ensure_account(self):
            if account_error:
                raise acme.AcmeError(account_error)
            return "kid-1"

    monkeypatch.setattr(certs.acme_mod, "AcmeClient", FakeClient)
    monkeypatch.setattr(
        certs, "load_sites", _async(lambda: list(sites if sites is not None else [
            {"username": "alice", "ca": "acme", "domain": "pve.yjscloud.com"}
        ]))
    )

    class FakeProvider:
        name = "dnspod"

        async def check_domain(self, name):
            if zone_error:
                raise dnsproviders.DnsError(zone_error)
            return zone

    monkeypatch.setattr(
        certs.dnsproviders, "build_provider", lambda _kind, _cfg: FakeProvider()
    )


def _async(fn):
    async def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)
    return wrapper


def test_missing_email_is_rejected(monkeypatch):
    _patch(monkeypatch, cfg={**CFG, "email": ""})

    with pytest.raises(certs.CertError) as excinfo:
        asyncio.run(certs.test_connection("acme", "alice"))

    assert "邮箱" in str(excinfo.value) or "email" in str(excinfo.value).lower()


def test_account_error_is_surfaced(monkeypatch):
    """EAB / 目录错误要原样带出来，不能吞成「验证失败」。"""
    _patch(monkeypatch, account_error="EAB HMAC Key 不是合法的 base64url 字符串")

    with pytest.raises(certs.CertError) as excinfo:
        asyncio.run(certs.test_connection("acme", "alice"))

    assert "EAB" in str(excinfo.value)


def test_success_reports_ca_and_dns(monkeypatch):
    _patch(monkeypatch)

    result = asyncio.run(certs.test_connection("acme", "alice"))

    detail = result["detail"]
    assert "CA" in detail
    assert "pve.yjscloud.com" in detail
    assert "yjscloud.com" in detail


def test_dns_error_is_surfaced(monkeypatch):
    """DNS 密钥失效时，错误里要能看到真实原因（不是「找不到域名」）。"""
    _patch(
        monkeypatch,
        zone_error="DNSPod DescribeRecordList 失败：The SecretId is not found",
    )

    with pytest.raises(certs.CertError) as excinfo:
        asyncio.run(certs.test_connection("acme", "alice"))

    assert "SecretId" in str(excinfo.value)


def test_no_acme_site_only_checks_credentials(monkeypatch):
    _patch(monkeypatch, sites=[])

    result = asyncio.run(certs.test_connection("acme", "alice"))

    assert "还没有 ACME 站点" in result["detail"]


def test_no_dns_provider_skips_dns(monkeypatch):
    _patch(monkeypatch, cfg={**CFG, "dns_provider": ""})

    result = asyncio.run(certs.test_connection("acme", "alice"))

    assert "DNS" in result["detail"]
    assert "pve.yjscloud.com" not in result["detail"]
