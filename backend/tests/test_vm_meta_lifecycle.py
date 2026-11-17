"""面板备注（手动 IP / 系统标识）的生命周期。

VMID 会被 PVE 回收：机器删掉后，同号的新机器会拿到同一个 key。备注不清掉的话，
一台刚下发的 TencentOS 会顶着上一台 Windows 机器的徽标 —— 这就是本文件的回归点。

    ../.venv/bin/python -m pytest tests/test_vm_meta_lifecycle.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app.routers import vm_meta  # noqa: E402

KEY = "|pve1|108"


def _patch(monkeypatch, items: dict) -> dict:
    """把存储换成内存 dict，返回它 —— drop 之后直接看写回去的值。"""
    saved: dict = {}

    async def fake_load() -> dict:
        return {k: dict(v) for k, v in items.items()}

    async def fake_set(key: str, value: str) -> None:
        saved[key] = value

    monkeypatch.setattr(vm_meta, "_load", fake_load)
    monkeypatch.setattr(vm_meta.store, "set_setting", fake_set)
    return saved


def test_drop_entry_removes_ip_and_os(monkeypatch) -> None:
    """删机器 → 备注整条消失，同号的新机器从「自动识别」开始。"""
    saved = _patch(
        monkeypatch,
        {KEY: {"ip": "10.0.0.5", "os": "windows", "by": "root", "at": 1}},
    )

    asyncio.run(vm_meta.drop_entry("", "pve1", 108))

    assert json.loads(saved[vm_meta.META_KEY]) == {}


def test_drop_os_keeps_manual_ip(monkeypatch) -> None:
    """重装换了系统盘 → 系统标识作废，手动 IP 仍留着（它与系统盘无关）。"""
    saved = _patch(
        monkeypatch,
        {KEY: {"ip": "10.0.0.5", "os": "windows", "by": "root", "at": 1}},
    )

    asyncio.run(vm_meta.drop_os("", "pve1", 108))

    item = json.loads(saved[vm_meta.META_KEY])[KEY]
    assert item["ip"] == "10.0.0.5"
    assert item["os"] == ""


def test_drop_os_without_ip_removes_entry(monkeypatch) -> None:
    """只标了系统、没填地址的记录，清掉系统标识后就不该再留一条空记录。"""
    saved = _patch(
        monkeypatch,
        {KEY: {"ip": "", "os": "windows", "by": "root", "at": 1}},
    )

    asyncio.run(vm_meta.drop_os("", "pve1", 108))

    assert json.loads(saved[vm_meta.META_KEY]) == {}


def test_drop_unknown_entry_is_noop(monkeypatch) -> None:
    """没有登记过就不写库（省一次 settings 往返，也避免无谓的写放大）。"""
    saved = _patch(monkeypatch, {})

    asyncio.run(vm_meta.drop_entry("", "pve1", 999))

    assert saved == {}
