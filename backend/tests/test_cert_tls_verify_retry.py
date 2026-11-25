"""线上生效校验要等 reload 完成。

``nginx -s reload`` 返回时旧 worker 还在优雅退出，第一次 TLS 握手可能仍落到
旧 worker 上、拿到上一张证书。修复前会把「部署成功」报成「线上校验未通过」，
站点的 ``last_error`` 也就一直挂着这条并不成立的结论。

    ../.venv/bin/python -m pytest tests/test_cert_tls_verify_retry.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import certmonitor, certs  # noqa: E402

EXPECTED = "7dd2972fc983dd53d31bd138a0f1d0437e62dcf934eef38a2ba3d26a8014fa42"
STALE = "47fb7de665084f82eefdf4f5e27a88765b91243195a2f5cb37ae59beef1e7143"


def _patch(monkeypatch, fingerprints):
    """让 inspect_tls 依次返回给定指纹（模拟旧 worker → 新 worker）。"""
    seen = {"n": 0}

    def fake_inspect_tls(host, port, **_kwargs):
        index = min(seen["n"], len(fingerprints) - 1)
        seen["n"] += 1
        return {"fingerprint": fingerprints[index], "detail": ""}

    async def fake_emit_step(*_a, **_k):
        return None

    async def fake_emit_log(*_a, **_k):
        return None

    monkeypatch.setattr(certmonitor, "inspect_tls", fake_inspect_tls)
    monkeypatch.setattr(certs.certflow, "emit_step", fake_emit_step)
    monkeypatch.setattr(certs.certflow, "emit_log", fake_emit_log)
    monkeypatch.setattr(certs, "_leaf_fingerprint", lambda _bundle: EXPECTED)
    # 退避时间改成 0，测试不必真的等 1.5 秒 × 2
    monkeypatch.setattr(certs, "TLS_VERIFY_DELAY", 0)
    return seen


def test_verify_passes_after_worker_switch(monkeypatch):
    seen = _patch(monkeypatch, [STALE, EXPECTED])
    site = {"monitor_url": "https://pvecenter.yjscloud.com"}

    ok = asyncio.run(certs._verify_deployed(site, {"privkey": "", "fullchain": ""}, ""))

    assert ok is True
    assert site["tls_verify"]["ok"] is True
    assert site["tls_verify"]["actual"].lower() == EXPECTED
    assert seen["n"] == 2


def test_verify_fails_when_certificate_never_switches(monkeypatch):
    seen = _patch(monkeypatch, [STALE])
    site = {"monitor_url": "https://pvecenter.yjscloud.com"}

    ok = asyncio.run(certs._verify_deployed(site, {"privkey": "", "fullchain": ""}, ""))

    assert ok is False
    assert site["tls_verify"]["ok"] is False
    assert site["tls_verify"]["actual"].lower() == STALE
    # 重试次数用尽才放弃（PVE 目标要等 pveproxy 重启）
    assert seen["n"] == certs.TLS_VERIFY_RETRIES


def test_verify_passes_first_time(monkeypatch):
    seen = _patch(monkeypatch, [EXPECTED])
    site = {"monitor_url": "https://pvecenter.yjscloud.com"}

    ok = asyncio.run(certs._verify_deployed(site, {"privkey": "", "fullchain": ""}, ""))

    assert ok is True
    # 第一次就一致时不该多握手
    assert seen["n"] == 1


def test_verify_skips_without_monitor_url(monkeypatch):
    async def fake_emit_step(*_a, **_k):
        return None

    monkeypatch.setattr(certs.certflow, "emit_step", fake_emit_step)
    site: dict = {}

    assert asyncio.run(certs._verify_deployed(site, {}, "")) is True
    assert site.get("tls_verify") is None
