"""迁移报错的文案：PVE 的英文原文必须翻成「该怎么办」。

PVE 对「运行中的虚拟机 + ``online=0``」只回一句
``can't migrate running VM without --online``，这句话会一路透传到前端 toast
（见 :func:`app.pve._extract_error` 与前端 ``errorMessage``）。用户看到的是
「迁移失败 + 一句英文」，既不知道是谁在运行，也不知道该勾什么。

本文件锁两件事：

1. 已知的几句（缺 ``--online``、本地盘、目标缺存储）一定翻成可操作的中文；
2. 不认识的报错**保持原样** —— 猜错原因比给英文原文更糟，也会误导排错。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import i18n, migrateguard  # noqa: E402
from app.pve import ProxmoxError  # noqa: E402
from app.routers import lxc as lxc_router  # noqa: E402


def test_running_vm_offline_detail_is_actionable() -> None:
    """缺 --online 的提示要点明「谁在运行」与两条出路。"""
    detail = migrateguard.offline_blocked_detail(100)

    assert "100" in detail
    assert "运行" in detail
    # 两条出路：勾在线迁移 / 先关机
    assert "在线迁移" in detail
    assert "关机" in detail


def test_qemu_migrate_detail_maps_missing_online() -> None:
    """前置校验漏掉时（状态刚变、或旧脚本直调），兜底映射也要给同一份提示。"""
    exc = ProxmoxError("can't migrate running VM without --online", 400)

    detail = migrateguard.qemu_migrate_detail(exc, 100)

    assert detail is not None
    assert "100" in detail
    assert "在线迁移" in detail


def test_qemu_migrate_detail_maps_local_disk() -> None:
    exc = ProxmoxError(
        "can't migrate VM with local disks: local-lvm (use --with-local-disks)", 400
    )

    detail = migrateguard.qemu_migrate_detail(exc)

    assert detail is not None
    assert "共享存储" in detail


def test_qemu_migrate_detail_maps_missing_storage() -> None:
    exc = ProxmoxError("storage 'local-lvm' is not available on node 'pve2'", 400)

    detail = migrateguard.qemu_migrate_detail(exc)

    assert detail is not None
    assert "存储" in detail


def test_qemu_migrate_detail_keeps_unknown_errors() -> None:
    """不认识的报错返回 None，调用方原样抛 —— 不编造原因。"""
    exc = ProxmoxError("some brand new PVE failure we never saw", 500)

    assert migrateguard.qemu_migrate_detail(exc, 100) is None


def test_lxc_restart_required_detail_is_actionable() -> None:
    """运行中的容器「迁移后保持停机」本来就做不到：提示要给两条出路。"""
    detail = lxc_router._restart_required_detail()

    assert "运行" in detail
    assert "迁移完成后启动" in detail
    assert "停止容器" in detail


def test_lxc_migrate_detail_maps_missing_restart() -> None:
    """前置校验漏掉时（状态刚变、旧脚本直调），兜底映射也要给同一份提示。"""
    exc = ProxmoxError(
        "can't migrate running container without --online or --restart", 400
    )

    mapped = lxc_router._lxc_migrate_detail(exc)

    assert mapped == lxc_router._restart_required_detail()


def test_lxc_migrate_detail_keeps_unknown_errors() -> None:
    exc = ProxmoxError("some brand new LXC failure we never saw", 500)

    assert lxc_router._lxc_migrate_detail(exc) is None


def test_english_locale_gets_english_detail() -> None:
    """英文请求下不能回中文：这条字符串是直接在 toast 里显示的。"""
    with i18n.use_language("en"):
        detail = migrateguard.offline_blocked_detail(100)
        mapped = migrateguard.qemu_migrate_detail(
            ProxmoxError("can't migrate running VM without --online", 400), 100
        )
        container = lxc_router._restart_required_detail()

    assert "running" in detail
    assert not any("\u4e00" <= char <= "\u9fa5" for char in detail)
    assert mapped is not None and "running" in mapped
    # 容器的这条同样直接进 toast，英文请求下不能回中文
    assert "running" in container
    assert not any("\u4e00" <= char <= "\u9fa5" for char in container)
