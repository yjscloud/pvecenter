"""DNS-01 的 zone 探测：鉴权失败不能被说成「找不到域名」。

回归点：``_find_zone`` 会从完整记录名逐级截断去问「哪个才是托管域」。
以前所有 ``DnsError`` 都被当成「这个候选不存在」，于是一把**无效的
SecretId** 会在三次失败后变成「找不到 _acme-challenge.xxx 对应的 DNSPod
域名」—— 用户按提示去查解析，而真正要改的是密钥。

    ../.venv/bin/python -m pytest tests/test_dns_zone_lookup.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import dnsproviders  # noqa: E402
from app.dnsproviders import DnsError  # noqa: E402


def _provider(monkeypatch, behaviour):
    """behaviour(candidate) 返回 dict 或抛 DnsError。"""
    provider = dnsproviders.DnspodProvider("sid", "skey")
    seen = []

    async def fake_call(_action, payload):
        candidate = payload["Domain"]
        seen.append(candidate)
        return behaviour(candidate)

    monkeypatch.setattr(provider, "_call", fake_call)
    return provider, seen


def test_auth_failure_is_raised_immediately(monkeypatch):
    """SecretId 无效：第一次就抛，不再拿父域去重试。"""
    def behaviour(_candidate):
        raise DnsError(
            "DNSPod DescribeRecordList 失败：The SecretId is not found",
            code="AuthFailure.SecretIdNotFound",
        )

    provider, seen = _provider(monkeypatch, behaviour)

    with pytest.raises(DnsError) as excinfo:
        asyncio.run(provider._find_zone("_acme-challenge.pve.yjscloud.com"))

    assert "SecretId" in str(excinfo.value)
    assert "找不到" not in str(excinfo.value)
    assert seen == ["_acme-challenge.pve.yjscloud.com"]


def test_domain_not_exist_falls_back_to_parent(monkeypatch):
    """子域不在账号里时继续截断，命中父域即返回。"""
    def behaviour(candidate):
        if candidate == "yjscloud.com":
            return {"Domain": "yjscloud.com"}
        raise DnsError("域名不存在", code="ResourceNotFound.DomainNotExist")

    provider, seen = _provider(monkeypatch, behaviour)

    zone = asyncio.run(provider._find_zone("_acme-challenge.pve.yjscloud.com"))

    assert zone == "yjscloud.com"
    assert seen[-1] == "yjscloud.com"


def test_exhausted_candidates_keeps_last_reason(monkeypatch):
    """全都判定为不存在时，错误里要能看最后一次的真实原因。"""
    def behaviour(_candidate):
        raise DnsError("域名不存在", code="ResourceNotFound.DomainNotExist")

    provider, _seen = _provider(monkeypatch, behaviour)

    with pytest.raises(DnsError) as excinfo:
        asyncio.run(provider._find_zone("_acme-challenge.pve.yjscloud.com"))

    assert "找不到 _acme-challenge.pve.yjscloud.com 对应的 DNSPod 域名" in str(
        excinfo.value
    )
    assert "域名不存在" in str(excinfo.value)


def test_legacy_error_without_code_uses_text(monkeypatch):
    """没带 code 的旧式错误：按文案判断，只有「不存在」才继续。"""
    assert dnsproviders._zone_not_found(DnsError("domain not exist")) is True
    assert dnsproviders._zone_not_found(DnsError("InvalidParameter")) is False


def test_zone_not_found_tokens_across_providers():
    """各家「域名不存在」的写法都要认；鉴权错误不能被误判。"""
    # 域名不在账号里 → 可以继续截断父域
    assert dnsproviders._zone_not_found(
        DnsError("x", code="ResourceNotFound.DomainNotExist")
    )
    assert dnsproviders._zone_not_found(
        DnsError("x", code="InvalidDomainName.NoExist")
    )
    assert dnsproviders._zone_not_found(DnsError("x", code="NOT_FOUND"))
    assert dnsproviders._zone_not_found(DnsError("x", code="UNKNOWN_DOMAIN"))
    # DNSPod 对「传了子域而不是主域名」回 InvalidParameter.DomainInvalid，
    # 语义就是「这个候选不是托管域」，必须继续截断父域
    assert dnsproviders._zone_not_found(
        DnsError("x", code="InvalidParameter.DomainInvalid")
    )
    assert dnsproviders._zone_not_found(
        DnsError("x", code="InvalidParameterValue.DomainNotExists")
    )
    # 鉴权 / 签名错误 → 必须直接抛（``SecretIdNotFound`` 里也有 notfound）
    assert not dnsproviders._zone_not_found(
        DnsError("x", code="AuthFailure.SecretIdNotFound")
    )
    assert not dnsproviders._zone_not_found(
        DnsError("x", code="AuthFailure.SignatureFailure")
    )
    assert not dnsproviders._zone_not_found(DnsError("x", code="9109"))
