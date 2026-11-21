"""规格磁盘与模板系统盘的容量对齐。

问题背景：模板自带一块系统盘（常见 20G / 100G），而「资源规格」里的 ``disk``
是给**新机器**准备的目标容量。规格比模板小时，克隆出来的机器仍然背着模板那块
盘 —— 界面上写着 20G、实际 100G，额度还按 20G 记；一旦某条路径真去「缩盘」，
分区表与文件系统就会超出卷尾，机器直接起不来（``Boot failed: not a bootable
disk``）。所以判定必须放在**创建之前**：

* 规格 < 模板 —— 拒绝（装不下，缩盘不可行）；
* 规格 = 模板 —— 直接克隆，不需要扩容；
* 规格 > 模板 —— 允许，但要求**完整克隆**：链接克隆的系统盘挂在模板的 backing
  file 上，扩它既依赖存储实现，也可能动到模板链；完整克隆才是独立卷。

只读逻辑放这里（纯函数 + 一个开关读取），PVE 往返由调用方负责，方便单测。
"""
from __future__ import annotations

import time
from math import ceil
from typing import Any, Dict, List, Optional

from . import store
from .vmconfig import disk_size_to_gb, parse_config_disks

#: 「规格磁盘守卫」开关（settings KV）。缺省开启；值为 0/off/false/no 时回到
#: 旧行为（只提示不拦截），用于灰度与应急回滚。
GUARD_KEY = "spec_disk_guard"
#: 开关缓存时长：每次建机都读一次 DB 不值当，30 秒的陈旧度对开关来说无害。
GUARD_TTL = 30.0

#: 容器最小根盘。系统模板（tar.zst）解包后普遍在 1~3G，4G 是「能跑起来」的下限；
#: 再小会解包失败或启动即退出。规格里的 20G 起本来够用，这一项只防手填。
MIN_LXC_ROOTFS_GB = 4

#: 容器模板解包后的估算系数：压缩包 × 2.5 + 2G 余量。只用于界面提示与粗校验。
CT_UNPACK_RATIO = 2.5
CT_UNPACK_SLACK_GB = 2

_guard_cache: Optional[tuple] = None


async def guard_enabled() -> bool:
    """读一次开关，进程内缓存 30 秒。

    读库失败时按**开启**处理：DB 挂了整个面板本来也用不了，而「放行」会直接
    产出配置与卷容量不一致的机器，代价更大。
    """
    global _guard_cache
    now = time.monotonic()
    if _guard_cache and now - _guard_cache[0] < GUARD_TTL:
        return bool(_guard_cache[1])
    raw = ""
    try:
        raw = str(await store.get_setting(GUARD_KEY) or "")
    except Exception:  # noqa: BLE001 - 读不到就按默认值，不阻塞建机
        raw = ""
    enabled = raw.strip().lower() not in ("0", "off", "false", "no")
    _guard_cache = (now, enabled)
    return enabled


def _boot_order_keys(config: Dict[str, Any]) -> List[str]:
    """``boot=order=scsi0;net0`` → ``["scsi0", "net0"]``；没有则空列表。"""
    boot = str((config or {}).get("boot") or "")
    if "order=" not in boot:
        return []
    spec = boot.split("order=", 1)[1].split(",", 1)[0]
    return [piece.strip() for piece in spec.split(";") if piece.strip()]


def _as_config(config: Any) -> Dict[str, Any]:
    """只接受 dict。PVE 客户端在单测里可能是 Mock，别让 ``.items()`` 炸掉。"""
    return config if isinstance(config, dict) else {}


def boot_disk_entry(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """系统盘那一条（boot order 点名的优先，否则盘位排序第一块）。

    与 :mod:`app.reinstall` 的判定同源：只认真正的磁盘键，跳过 cloud-init 与
    光驱（``parse_config_disks`` 已经过滤掉这两类）。
    """
    data = _as_config(config)
    disks = parse_config_disks(data)
    if not disks:
        return None
    ordered = _boot_order_keys(data)
    for key in ordered:
        for disk in disks:
            if disk.get("interface") == key:
                return disk
    return disks[0]


def root_disk_gb(
    config: Dict[str, Any], fallback_bytes: Optional[Any] = None
) -> Optional[int]:
    """模板/客户机系统盘容量（整数 GB，向上取整）。

    ``size=`` 缺失时（部分存储的 config 不写这个字段）用 ``fallback_bytes``
    兜底 —— 调用方通常拿 ``/cluster/resources`` 的 ``maxdisk`` 传进来。
    两者都没有就返回 ``None``，表示「不知道」，绝不能当成 0。
    """
    disk = boot_disk_entry(config or {})
    if disk:
        gb = disk_size_to_gb(str(disk.get("size") or ""))
        if gb:
            return max(1, int(ceil(float(gb))))
    try:
        raw = int(fallback_bytes or 0)
    except (TypeError, ValueError):
        raw = 0
    if raw > 0:
        return max(1, int(ceil(raw / (1024 ** 3))))
    return None


def estimate_rootfs_gb(size_bytes: Optional[Any]) -> int:
    """容器系统模板解包后大约需要多少 G（给界面提示用）。"""
    try:
        raw = int(size_bytes or 0)
    except (TypeError, ValueError):
        raw = 0
    if raw <= 0:
        return MIN_LXC_ROOTFS_GB
    est = int(ceil(raw * CT_UNPACK_RATIO / (1024 ** 3))) + CT_UNPACK_SLACK_GB
    return max(MIN_LXC_ROOTFS_GB, est)


def check_clone_disk(
    requested_gb: Optional[int],
    required_gb: Optional[int],
    full_clone: bool,
) -> Optional[Dict[str, Any]]:
    """克隆前判定。返回 ``None`` 表示可以建；否则返回错误字典。

    同时服务两处：路由层据此抛 400，单测里直接断言字典内容。
    """
    if not requested_gb or not required_gb:
        # 规格没给盘（老前端 / 自定义部署）或模板盘读不到：交给原有流程，
        # 「不知道」不该变成「一定不行」。
        return None
    requested = int(requested_gb)
    required = int(required_gb)
    if requested < required:
        return {
            "code": "spec_disk_too_small",
            "message": (
                f"所选规格的系统盘 {requested}G 小于模板所需 {required}G，"
                "模板分区装不下，虚拟机无法正常启动。请选择不小于 "
                f"{required}G 的规格，或换一个更小的模板。"
            ),
            "required_gb": required,
            "selected_gb": requested,
        }
    if requested > required and not full_clone:
        return {
            "code": "clone_full_required",
            "message": (
                f"规格系统盘 {requested}G 大于模板的 {required}G：链接克隆的系统盘"
                "与模板共享底层卷，无法安全扩容。请改用完整克隆，或选择 "
                f"{required}G 的规格。"
            ),
            "required_gb": required,
            "selected_gb": requested,
        }
    return None
