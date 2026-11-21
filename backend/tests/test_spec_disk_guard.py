"""规格磁盘与模板系统盘的容量守卫。

三个必须卡住的点：

* 模板系统盘 100G、规格 20G —— 建之前就要拒，不能建出「配置写 20G、卷是 100G」
  的机器；
* 规格比模板大、却用链接克隆 —— 共享底层卷不能安全扩容，要求完整克隆；
* 扩容后必须读回确认 —— PVE 的 resize 是任务，接口成功不等于卷真的长大了。

不依赖 MySQL：store 用内存打桩，PVE 用假 client。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import diskspec  # noqa: E402
from app.pve import ProxmoxError  # noqa: E402
from app.schemas import CloneSpec, VmCreateRequest  # noqa: E402


class TestRootDisk:
    def test_reads_size_from_config(self) -> None:
        assert diskspec.root_disk_gb({"scsi0": "local-lvm:vm-9000-disk-0,size=100G"}) == 100

    def test_boot_order_wins(self) -> None:
        config = {
            "scsi0": "local-lvm:vm-9000-disk-0,size=20G",
            "scsi1": "local-lvm:vm-9000-disk-1,size=100G",
            "boot": "order=scsi1",
        }
        assert diskspec.root_disk_gb(config) == 100

    def test_cloudinit_volume_is_not_a_disk(self) -> None:
        config = {
            "ide2": "local-lvm:vm-9000-cloudinit,media=cdrom",
            "scsi0": "local-lvm:vm-9000-disk-0,size=40G",
        }
        assert diskspec.root_disk_gb(config) == 40

    def test_falls_back_to_maxdisk_bytes(self) -> None:
        assert diskspec.root_disk_gb({}, 100 * 1024**3) == 100

    def test_unknown_is_none_not_zero(self) -> None:
        """读不到必须是 None：0 会被下游当成「任何规格都放得下」。"""
        assert diskspec.root_disk_gb({}) is None
        assert diskspec.root_disk_gb({}, 0) is None


class TestCheckCloneDisk:
    def test_smaller_is_rejected(self) -> None:
        problem = diskspec.check_clone_disk(20, 100)
        assert problem is not None
        assert problem["code"] == "spec_disk_too_small"
        assert problem["required_gb"] == 100

    def test_equal_is_fine(self) -> None:
        assert diskspec.check_clone_disk(100, 100) is None

    def test_larger_is_fine_regardless_of_clone_mode(self) -> None:
        """链接克隆 / 完整克隆由用户决定，判定只看装不装得下。"""
        assert diskspec.check_clone_disk(200, 100) is None

    def test_unknown_template_or_missing_spec_passes(self) -> None:
        assert diskspec.check_clone_disk(20, None) is None
        assert diskspec.check_clone_disk(None, 100) is None


class TestEstimateRootfs:
    def test_small_template_keeps_the_floor(self) -> None:
        assert diskspec.estimate_rootfs_gb(200 * 1024**2) == diskspec.MIN_LXC_ROOTFS_GB

    def test_large_template_scales_up(self) -> None:
        assert diskspec.estimate_rootfs_gb(2 * 1024**3) == 7

    def test_unknown_size_keeps_the_floor(self) -> None:
        assert diskspec.estimate_rootfs_gb(None) == diskspec.MIN_LXC_ROOTFS_GB


class TestGuardSwitch:
    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        diskspec._guard_cache = None
        yield
        diskspec._guard_cache = None

    def _fake_store(self, monkeypatch, value: str) -> None:
        async def get_setting(key: str, default: Any = None) -> Any:
            return value

        monkeypatch.setattr(diskspec.store, "get_setting", get_setting)

    def test_defaults_to_on(self, monkeypatch) -> None:
        self._fake_store(monkeypatch, "")
        assert asyncio.run(diskspec.guard_enabled()) is True

    def test_explicit_off(self, monkeypatch) -> None:
        self._fake_store(monkeypatch, "off")
        assert asyncio.run(diskspec.guard_enabled()) is False


class _FakeCloneClient:
    """只实现克隆系统盘扩容用到的那两个方法。"""

    def __init__(self, size_gb: int, grow: bool = True) -> None:
        self.size_gb = size_gb
        self.grow = grow
        self.resized: list = []

    def _config(self) -> Dict[str, str]:
        return {
            "boot": "order=scsi0",
            "scsi0": f"local-lvm:vm-110-disk-0,size={self.size_gb}G",
        }

    async def qemu_config(self, node: str, vmid: int) -> Dict[str, str]:
        return self._config()

    async def qemu_resize(self, node: str, vmid: int, key: str, size: str) -> str:
        self.resized.append((key, size))
        if self.grow:
            self.size_gb = int(str(size).rstrip("G"))
        return "UPID:test"


class TestResizeClonedRootfs:
    def test_grows_and_reads_back(self) -> None:
        from app.routers import vms

        client = _FakeCloneClient(20)
        note, ok = asyncio.run(
            vms._resize_cloned_rootfs(client, "pve", 110, 100)
        )
        assert ok is True
        assert "20G" in note and "100G" in note
        assert client.resized == [("scsi0", "100G")]

    def test_resize_task_without_effect_is_reported(self) -> None:
        from app.routers import vms

        client = _FakeCloneClient(20, grow=False)
        note, ok = asyncio.run(
            vms._resize_cloned_rootfs(client, "pve", 110, 100)
        )
        assert ok is False
        assert "仍为" in note

    def test_linked_clone_failure_points_at_full_clone(self) -> None:
        """链接克隆扩不动时，说明里要给出可行的补救方向，而不是只说失败。"""
        from app.routers import vms

        client = _FakeCloneClient(20, grow=False)
        note, ok = asyncio.run(
            vms._resize_cloned_rootfs(
                client, "pve", 110, 100, "", linked_clone=True
            )
        )
        assert ok is False
        assert "完整克隆" in note

    def test_never_shrinks(self) -> None:
        from app.routers import vms

        client = _FakeCloneClient(100)
        note, ok = asyncio.run(
            vms._resize_cloned_rootfs(client, "pve", 110, 20)
        )
        assert ok is False, "规格小于模板：必须报未达成，不能静默当成功"
        assert client.resized == []
        assert "无法启动" in note

    def test_equal_size_is_noop(self) -> None:
        from app.routers import vms

        client = _FakeCloneClient(100)
        note, ok = asyncio.run(
            vms._resize_cloned_rootfs(client, "pve", 110, 100)
        )
        assert ok is True
        assert client.resized == []


class TestEnsureSpecDiskFits:
    def _payload(self, disk_size: int, full: bool = False) -> VmCreateRequest:
        return VmCreateRequest(
            node="pve",
            name="demo",
            clone_from=CloneSpec(
                node="pve", vmid=9000, full=full, disk_size=disk_size
            ),
        )

    def _patch(self, monkeypatch, enabled: bool, required: int) -> None:
        from app.routers import vms

        async def guard_enabled() -> bool:
            return enabled

        async def template_rootfs_gb(client: Any, node: str, vmid: int) -> int:
            return required

        monkeypatch.setattr(vms.diskspec, "guard_enabled", guard_enabled)
        monkeypatch.setattr(vms, "_template_rootfs_gb", template_rootfs_gb)

    def test_rejects_too_small_before_any_pve_write(self, monkeypatch) -> None:
        from app.routers import vms

        self._patch(monkeypatch, True, 100)
        with pytest.raises(ProxmoxError) as exc:
            asyncio.run(vms._ensure_spec_disk_fits(self._payload(20), object()))
        assert exc.value.code == "spec_disk_too_small"
        assert exc.value.status_code == 400

    def test_growth_is_allowed_with_linked_clone(self, monkeypatch) -> None:
        """链接克隆 + 规格大于模板：不拦，扩不扩得动由存储决定并如实回报。"""
        from app.routers import vms

        self._patch(monkeypatch, True, 100)
        asyncio.run(vms._ensure_spec_disk_fits(self._payload(200), object()))

    def test_growth_is_allowed_with_full_clone(self, monkeypatch) -> None:
        from app.routers import vms

        self._patch(monkeypatch, True, 100)
        asyncio.run(vms._ensure_spec_disk_fits(self._payload(200, full=True), object()))

    def test_switch_off_skips_the_guard(self, monkeypatch) -> None:
        from app.routers import vms

        self._patch(monkeypatch, False, 100)
        asyncio.run(vms._ensure_spec_disk_fits(self._payload(20), object()))
