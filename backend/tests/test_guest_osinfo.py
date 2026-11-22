"""系统信息快照：隔离 / 解除隔离 / 开机时采集，且**只补不删**。

这是「隔离完解除隔离，操作系统信息丢了」的回归点：以前面板只在人工打开备注弹窗
时才写系统信息，隔离把机器关掉、快照给 VM 上锁的那些瞬间读不到 ostype，列表就退回
「按名字猜」甚至「—」，而没有任何回写路径。现在：

* 隔离、解除隔离、开机的后置步骤会主动采集一次 config 并落库；
* 采集失败（PVE 不可达、权限不足）**什么都不写**，保留上一次的值；
* 解除隔离时若条目里没有 os 家族，用采集到的 ostype 补回去（图标恢复）。

    ../.venv/bin/python -m pytest tests/test_guest_osinfo.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import guest_osinfo  # noqa: E402
from app.routers import vm_meta  # noqa: E402


class _FakeClient:
    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self.config = config if config is not None else {}
        self.error = error

    async def qemu_config(self, node: str, vmid: int) -> Dict[str, Any]:
        if self.error is not None:
            raise self.error
        return dict(self.config)

    async def lxc_config(self, node: str, vmid: int) -> Dict[str, Any]:
        if self.error is not None:
            raise self.error
        return dict(self.config)


def _patch_save(monkeypatch, calls: Dict[str, Any]):
    async def fake_save(conn, node, vmid, snapshot, *, os_family="", by=""):
        calls.update(
            conn=conn, node=node, vmid=vmid, snapshot=snapshot, os_family=os_family, by=by
        )

    async def fake_owner(*_args, **_kwargs) -> str:
        return "alice"

    monkeypatch.setattr(vm_meta, "save_snapshot", fake_save)
    monkeypatch.setattr(guest_osinfo, "_owner_of", fake_owner)


def test_capture_persists_config_and_restores_family(monkeypatch) -> None:
    calls: Dict[str, Any] = {}
    _patch_save(monkeypatch, calls)

    snapshot = asyncio.run(
        guest_osinfo.capture(
            "pve1",
            108,
            conn_id="",
            reason="release",
            restore_os=True,
            client=_FakeClient(
                {"ostype": "win11", "name": "web-01", "cores": "4", "memory": "4096"}
            ),
        )
    )

    assert snapshot is not None
    assert snapshot["family"] == "windows"
    assert snapshot["cores"] == "4"
    assert snapshot["reason"] == "release"
    # 落库时带上族系（用于「解除隔离后图标恢复」）与归属（普通用户才看得到）
    assert calls["os_family"] == "windows"
    assert calls["by"] == "alice"
    assert calls["snapshot"]["ostype"] == "win11"


def test_capture_does_not_touch_store_when_config_unreadable(monkeypatch) -> None:
    """读不到 config 就什么都不写 —— 绝不能把「这次没读到」当成「信息没了」。"""
    calls: Dict[str, Any] = {}
    _patch_save(monkeypatch, calls)

    snapshot = asyncio.run(
        guest_osinfo.capture(
            "pve1",
            108,
            reason="quarantine",
            client=_FakeClient(error=RuntimeError("no such guest")),
        )
    )

    assert snapshot is None
    assert calls == {}


def test_capture_without_restore_keeps_manual_os(monkeypatch) -> None:
    """隔离时只留快照，不动 os 家族（人工标注优先，且此时还没到恢复的时机）。"""
    calls: Dict[str, Any] = {}
    _patch_save(monkeypatch, calls)

    asyncio.run(
        guest_osinfo.capture(
            "pve1",
            108,
            reason="quarantine",
            restore_os=False,
            client=_FakeClient({"ostype": "l26", "name": "db-01"}),
        )
    )

    assert calls["os_family"] == ""
    assert calls["snapshot"]["family"] == "linux"


def test_capture_reads_lxc_hostname(monkeypatch) -> None:
    """容器的「系统信息」是 ostype + hostname + 模板卷，不是 qemu 的 name。"""
    calls: Dict[str, Any] = {}
    _patch_save(monkeypatch, calls)

    snapshot = asyncio.run(
        guest_osinfo.capture(
            "pve1",
            205,
            kind="lxc",
            reason="power.start",
            restore_os=True,
            client=_FakeClient(
                {
                    "ostype": "debian",
                    "hostname": "ct-205",
                    "ostemplate": "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst",
                    "cores": "2",
                }
            ),
        )
    )

    assert snapshot is not None
    assert snapshot["hostname"] == "ct-205"
    assert snapshot["family"] == "debian"
    assert calls["os_family"] == "debian"
    assert snapshot["ostemplate"].startswith("local:vztmpl/")


def test_family_of_is_conservative() -> None:
    """推不出家族时留空：贴错图标比不显示更糟。"""
    assert guest_osinfo.family_of("win11") == "windows"
    assert guest_osinfo.family_of("l26") == "linux"
    assert guest_osinfo.family_of("debian", "lxc") == "debian"
    assert guest_osinfo.family_of("", "qemu") == ""
    assert guest_osinfo.family_of("solaris", "qemu") == "other"
