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


def test_os_annotation_survives_node_migration(monkeypatch) -> None:
    """条目还挂在迁移前的节点下，按新节点读也要读得到。

    否则「系统标识」在迁移后就没了 —— 列表退回按 ostype / 名字推断，一台被手工
    标成 Windows 的 Linux 会立刻换错徽标，重装向导的「当前系统」也会变。
    """
    _patch(
        monkeypatch,
        {"|pve1|108": {"ip": "10.0.0.5", "os": "windows", "by": "root", "at": 1}},
    )

    assert asyncio.run(vm_meta.os_of("pve2", 108, "")) == "windows"


def test_drop_entry_cleans_every_node_of_the_same_machine(monkeypatch) -> None:
    """同一台机器在旧 / 新节点各有一条（迁移前后各写一次）：一次清干净。

    只按当前节点清会留下孤儿行，VMID 被回收时贴到同号的新机器上。
    """
    saved = _patch(
        monkeypatch,
        {
            "|pve1|108": {"ip": "10.0.0.5", "os": "windows", "by": "root", "at": 1},
            "|pve2|108": {"ip": "10.0.0.5", "os": "windows", "by": "root", "at": 2},
        },
    )

    asyncio.run(vm_meta.drop_entry("", "pve2", 108))

    assert json.loads(saved[vm_meta.META_KEY]) == {}


def test_snapshot_entry_is_kept_without_ip_or_os(monkeypatch) -> None:
    """只有系统信息快照的条目不能被当成空条目丢掉。

    隔离 / 解除隔离 / 开机采集出来的那一份就是「系统信息」的最后副本 —— 它没有
    IP、也没有人工标注，但必须在 ``/vm-meta`` 里返回，否则列表与备注弹窗读不到。
    """
    async def fake_get(key: str) -> str:
        return json.dumps(
            {
                "|pve1|108": {
                    "snapshot": {
                        "ostype": "l26",
                        "family": "linux",
                        "hostname": "web-01",
                        "reason": "release",
                        "taken_at": 1,
                    },
                    "snapshot_at": 1,
                }
            }
        )

    monkeypatch.setattr(vm_meta.store, "get_setting", fake_get)

    items = asyncio.run(vm_meta._load())

    entry = items["|pve1|108"]
    assert entry["snapshot"]["ostype"] == "l26"
    assert entry["snapshot"]["hostname"] == "web-01"


def test_snapshot_merges_and_never_overwrites_manual_os(monkeypatch) -> None:
    """采集只补不删：没采集到的字段保留旧值，人工标注的 os 不被覆盖。"""
    saved = _patch(
        monkeypatch,
        {
            KEY: {
                "ip": "10.0.0.5",
                "os": "tencent",
                "by": "root",
                "at": 1,
                "snapshot": {"ostype": "l26", "hostname": "old"},
                "snapshot_at": 1,
            }
        },
    )

    asyncio.run(
        vm_meta.save_snapshot(
            "",
            "pve1",
            108,
            {"family": "linux", "reason": "release", "taken_at": 2},
            os_family="linux",
            by="root",
        )
    )

    item = json.loads(saved[vm_meta.META_KEY])[KEY]
    assert item["os"] == "tencent"  # 人工标注优先
    assert item["snapshot"]["hostname"] == "old"  # 这次没采到，保留
    assert item["snapshot"]["family"] == "linux"
    assert item["snapshot"]["reason"] == "release"
    assert item["ip"] == "10.0.0.5"


def test_snapshot_restores_missing_os(monkeypatch) -> None:
    """解除隔离：条目里没有 os 家族时，用采集到的 ostype 补回去（图标恢复）。"""
    saved = _patch(monkeypatch, {KEY: {"ip": "", "os": "", "by": "root", "at": 1}})

    asyncio.run(
        vm_meta.save_snapshot(
            "",
            "pve1",
            108,
            {"family": "windows", "ostype": "win11", "taken_at": 2},
            os_family="windows",
            by="root",
        )
    )

    item = json.loads(saved[vm_meta.META_KEY])[KEY]
    assert item["os"] == "windows"
    assert item["auto"] is True


def test_snapshot_does_not_freeze_generic_linux(monkeypatch) -> None:
    """``linux`` 这种大类不写进 os：否则模板名能判出的 Ubuntu 会被它永久压住。"""
    saved = _patch(monkeypatch, {KEY: {"ip": "", "os": "", "by": "root", "at": 1}})

    asyncio.run(
        vm_meta.save_snapshot(
            "",
            "pve1",
            108,
            {"family": "linux", "ostype": "l26", "taken_at": 2},
            os_family="linux",
            by="root",
        )
    )

    item = json.loads(saved[vm_meta.META_KEY])[KEY]
    assert item["os"] == ""
    assert item["snapshot"]["family"] == "linux"


def test_manual_ip_index_ignores_node(monkeypatch) -> None:
    """手动 IP 索引按「连接 + VMID」：机器迁移后「网络」列仍算得出池名。"""
    _patch(
        monkeypatch,
        {"|pve1|108": {"ip": "10.0.0.5", "os": "", "by": "root", "at": 1}},
    )

    index = asyncio.run(
        vm_meta.manual_ip_index({"role": "admin", "username": "root"})
    )

    assert index[("", 108)] == "10.0.0.5"
