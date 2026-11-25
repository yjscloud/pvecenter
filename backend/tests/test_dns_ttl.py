"""DNS-01 的 TXT 记录 TTL 必须落在各家允许的区间内。

统一写 60 会被「低于最小值」直接拒绝（DNSPod 免费版 600 起、华为云 300 起、
GoDaddy 600 起），报错是 ``记录的TTL值超出了限制``，不看文档根本不知道要改什么。

    ../.venv/bin/python -m pytest tests/test_dns_ttl.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import dnsproviders  # noqa: E402

NAME = "_acme-challenge.pve.yjscloud.com"


def test_dnspod_ttl_meets_minimum(monkeypatch):
    provider = dnsproviders.DnspodProvider("sid", "skey")
    captured = {}

    async def fake_find_zone(_name):
        return "yjscloud.com"

    async def fake_call(_action, payload):
        captured.update(payload)
        return {"RecordInfo": {"Id": "1"}}

    monkeypatch.setattr(provider, "_find_zone", fake_find_zone)
    monkeypatch.setattr(provider, "_call", fake_call)

    asyncio.run(provider.create_txt(NAME, "token"))

    assert int(captured["TTL"]) >= 600
    assert captured["SubDomain"] == "_acme-challenge.pve"


def test_huawei_ttl_meets_minimum(monkeypatch):
    provider = dnsproviders.HuaweiProvider("ak", "sk")
    captured = {}

    async def fake_find_zone(_name):
        return {"id": "z1", "name": "yjscloud.com."}

    async def fake_call(_method, _path, _query="", payload=None):
        captured.update(payload or {})
        return {"id": "r1"}

    monkeypatch.setattr(provider, "_find_zone", fake_find_zone)
    monkeypatch.setattr(provider, "_call", fake_call)

    asyncio.run(provider.create_txt(NAME, "token"))

    assert int(captured["ttl"]) >= 300


def test_godaddy_ttl_meets_minimum(monkeypatch):
    """GoDaddy 的 PUT 是整组覆盖，TTL 挂在每条记录上。"""
    provider = dnsproviders.GodaddyProvider("key", "secret")
    captured = {}

    async def fake_find_zone(_name):
        return "yjscloud.com"

    async def fake_read_txt(_zone, _record_name):
        return []

    async def fake_call(_method, _path, payload=None):
        captured["payload"] = payload
        return {}

    monkeypatch.setattr(provider, "_find_zone", fake_find_zone)
    monkeypatch.setattr(provider, "_read_txt", fake_read_txt)
    monkeypatch.setattr(provider, "_call", fake_call)

    asyncio.run(provider.create_txt(NAME, "token"))

    assert int(captured["payload"][0]["ttl"]) >= 600


def test_cloudflare_ttl_unchanged(monkeypatch):
    """Cloudflare 最小 60，保持小 TTL 让验证尽快生效。"""
    provider = dnsproviders.CloudflareProvider("token")
    captured = {}

    async def fake_find_zone(_name):
        return {"id": "z1", "name": "yjscloud.com"}

    async def fake_call(_method, _path, payload=None):
        captured.update(payload or {})
        return {"result": {"id": "r1"}}

    monkeypatch.setattr(provider, "_find_zone", fake_find_zone)
    monkeypatch.setattr(provider, "_call", fake_call)

    asyncio.run(provider.create_txt(NAME, "token"))

    assert int(captured["ttl"]) == 60
