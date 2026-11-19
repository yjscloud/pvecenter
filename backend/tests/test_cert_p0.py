"""P0 增强的回归测试。

覆盖四件事：新增 CA 目录与 EAB 绑定、新增 DNS 服务商适配器、证书多格式
导出（PEM / PFX）、证书库存统计分桶。全部是纯逻辑，不碰网络与数据库。
"""
from __future__ import annotations

import io
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app import acme, certs, dnsproviders  # noqa: E402


def _self_signed(domain: str = "example.com"):
    """生成一张 90 天有效期的自签证书，供导出测试用。"""
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
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode("ascii")
    return cert_pem, key_pem


def test_directories_include_major_cas() -> None:
    names = {name for name, _ in acme.DIRECTORIES}
    assert {
        "letsencrypt",
        "zerossl",
        "google",
        "sslcom",
        "actalis",
        "buypass",
    } <= names


def test_eab_payload_is_built_only_when_configured() -> None:
    configured = acme.AcmeClient(
        acme.DIRECTORY_GOOGLE,
        eab_kid="kid-1",
        eab_hmac_key="c2VjcmV0",  # base64url("secret")
    )
    payload = configured._eab_payload("https://ca.example/new-account")
    assert payload["protected"] and payload["payload"] and payload["signature"]

    absent = acme.AcmeClient(acme.DIRECTORY_GOOGLE)._eab_payload("https://ca/x")
    assert absent == {}


def test_new_dns_providers_are_registered() -> None:
    assert {"godaddy", "route53", "namecheap", "volcengine"} <= set(
        dnsproviders.PROVIDERS
    )
    assert isinstance(
        dnsproviders.build_provider("godaddy", {"api_key": "k", "api_secret": "s"}),
        dnsproviders.GodaddyProvider,
    )
    assert isinstance(
        dnsproviders.build_provider(
            "route53", {"access_key_id": "k", "secret_access_key": "s"}
        ),
        dnsproviders.Route53Provider,
    )
    assert isinstance(
        dnsproviders.build_provider(
            "namecheap", {"api_user": "u", "api_key": "k"}
        ),
        dnsproviders.NamecheapProvider,
    )
    assert isinstance(
        dnsproviders.build_provider(
            "volcengine", {"access_key": "k", "secret_key": "s"}
        ),
        dnsproviders.VolcengineProvider,
    )


def test_export_pem_and_pfx() -> None:
    cert_pem, key_pem = _self_signed()
    site = {"domain": "example.com", "acme_pem": cert_pem, "acme_key": key_pem}

    pem_out = certs.export_certificate(site, "pem")
    assert pem_out["media_type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(pem_out["content"])) as archive:
        names = archive.namelist()
    assert "example.com.crt.pem" in names
    assert "example.com.key.pem" in names

    pfx_out = certs.export_certificate(site, "pfx")
    assert pfx_out["media_type"] == "application/x-pkcs12"
    # PKCS#12 是 DER SEQUENCE，首字节 0x30
    assert pfx_out["content"][:1] == b"\x30"


def test_export_rejects_unsupported_format() -> None:
    cert_pem, key_pem = _self_signed()
    site = {"domain": "example.com", "acme_pem": cert_pem, "acme_key": key_pem}
    try:
        certs.export_certificate(site, "der")
    except certs.CertError:
        return
    raise AssertionError("unsupported format should raise CertError")


def test_inventory_stats_buckets() -> None:
    items = [
        {"provider": "acme", "label": "ACME", "status": 1, "days_left": -1},
        {"provider": "acme", "label": "ACME", "status": 1, "days_left": 3},
        {"provider": "tencent", "label": "Tencent", "status": 1, "days_left": 20},
        {"provider": "tencent", "label": "Tencent", "status": 1, "days_left": 60},
        {"provider": "upload", "label": "Upload", "status": 1, "days_left": 200},
        {"provider": "upload", "label": "Upload", "status": 0, "days_left": None},
    ]
    stats = certs.inventory_stats_from_items(items)
    assert stats["total"] == 6
    assert stats["buckets"] == {
        "expired": 1,
        "d7": 1,
        "d30": 1,
        "d90": 1,
        "d90plus": 1,
        "unknown": 1,
    }
    assert stats["by_ca"][0]["count"] == 2
