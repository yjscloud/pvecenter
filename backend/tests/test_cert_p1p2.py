"""P1 / P2 增强的回归测试。

覆盖：多级预警档位、DNS 注册表派生、IP 证书 identifier/SAN、云部署方法注册、
腾讯云 TC3 请求形状。全部是纯逻辑，不碰网络与数据库。
"""
from __future__ import annotations

import sys
from pathlib import Path

from cryptography import x509

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app import acme, certdeployers, certs, dnsproviders  # noqa: E402


def test_expiry_stages_are_ordered() -> None:
    assert certs.EXPIRY_STAGES == (30, 14, 7, 3)


def test_dns_schema_drives_fields_and_defaults() -> None:
    assert dnsproviders.field_keys("route53") == (
        "access_key_id",
        "secret_access_key",
        "region",
    )
    assert dnsproviders.secret_keys("route53") == ("secret_access_key",)
    assert dnsproviders.schema_defaults()["route53"]["region"] == "us-east-1"
    assert dnsproviders.schema_defaults()["volcengine"]["region"] == "cn-beijing"
    assert dnsproviders.FIELD_LABELS["region"] == "cert.fieldRegion"
    # ACME 配置的默认块与注册表保持一致
    assert certs.DEFAULT_ACME["volcengine"]["region"] == "cn-beijing"


def test_ip_identifier_and_san() -> None:
    assert acme.is_ip("1.2.3.4") is True
    assert acme.is_ip("example.com") is False
    assert acme._identifier("1.2.3.4") == {"type": "ip", "value": "1.2.3.4"}
    assert acme._identifier("example.com")["type"] == "dns"
    san = acme._san("1.2.3.4")
    assert isinstance(san, x509.IPAddress)


def test_cloud_deploy_methods_registered() -> None:
    for method in ("tencent_clb", "aliyun_slb", "tencent_cdn", "aliyun_cdn"):
        assert method in certs.DEPLOY_METHODS
        assert method in certs.DEPLOY_METHOD_LABELS


def test_tc3_request_shape() -> None:
    url, headers, body = certdeployers._tc3_request(
        "AKID", "SECRET", "ssl", "2019-12-05", "UploadCertificate", {"Alias": "x"}
    )
    assert url == "https://ssl.tencentcloudapi.com"
    assert headers["X-TC-Action"] == "UploadCertificate"
    assert headers["X-TC-Version"] == "2019-12-05"
    assert "TC3-HMAC-SHA256" in headers["Authorization"]
    assert '"Alias":"x"' in body


def test_aliyun_signed_query_contains_signature() -> None:
    query = certdeployers._aliyun_signed_query(
        {"Action": "UploadServerCertificate", "Version": "2014-05-15"}, "secret"
    )
    assert "Action=UploadServerCertificate" in query
    assert "Signature=" in query
