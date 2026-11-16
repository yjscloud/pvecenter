"""存储能力：某个操作在当前存储上到底能不能做。

为什么要有这一层
----------------
「能不能快照」不是面板决定的，而是 PVE 的**存储插件**决定的：ZFS、LVM-thin、
Ceph RBD、Btrfs 会为每个卷建快照，而 dir / NFS / SMB / LVM（厚置备）/ iSCSI /
CephFS / GlusterFS 根本没有快照这个动作。PVE 自己要到真正执行时才检查，报出来的
是一句 ``storage 'local' does not support snapshots``：

* 用户已经把对话框填完、点了确定才被拒；
* 那句话既没说「你这台机器的盘到底在哪块存储上」，也没说「那能用什么」；
* 容器还可能有多块盘（rootfs + mpN），PVE 只报第一块挡路的。

所以这里把判断提到**操作之前**：先算出容器的每一块盘落在哪种存储上、支不支持
快照，再列出当前环境里确实可用（存在、启用、在线）的快照存储。前端据此把
「创建快照」按钮直接置灰并给出替代方案，后端在 POST 里再拦一次（防止绕过界面
或能力数据过期）。

判断口径
--------
* 支持：``zfspool`` / ``zfs``（ZFS over iSCSI）/ ``lvmthin`` / ``rbd`` / ``btrfs``；
* 不支持：类型已知但不在上表（dir、nfs、cifs、lvm、iscsi、cephfs、glusterfs、
  pbs …）→ **阻止**；
* 无法确定：存储不在节点存储列表里，或列表读不到（PVE 抖动 / 共享存储未挂载）
  → **放行**并标记 ``unknown``。宁可让 PVE 自己报错，也不要因为一次读不到存储
  清单就把用户挡在门外 —— 假阴性（说不能做）比假阳性（做了再说失败）更烦人。

容器快照会覆盖**全部卷**，所以任意一块盘在不支持的存储上都算不支持：只检查
rootfs 会在「系统盘在 ZFS、数据盘挂在 dir」的机器上放过一次注定失败的请求。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import i18n
from .pve import ProxmoxError

#: 支持快照的存储插件。`zfs` 是「ZFS over iSCSI」，底层同样是 ZFS 数据集，
#: PVE 的插件也实现了 snapshot 特性 —— 不能因为它不叫 zfspool 就误判成不支持。
SNAPSHOT_CAPABLE_TYPES: Tuple[str, ...] = (
    "zfspool",
    "zfs",
    "lvmthin",
    "rbd",
    "btrfs",
)

#: 类型 → 展示名。刻意不做翻译：这些是 PVE 的技术名词（LVM-thin / Ceph RBD），
#: 中英文界面里都应原样出现，翻译反而让人对不上 PVE 界面。
STORAGE_TYPE_LABELS: Dict[str, str] = {
    "zfspool": "ZFS",
    "zfs": "ZFS over iSCSI",
    "lvmthin": "LVM-thin",
    "rbd": "Ceph RBD",
    "btrfs": "Btrfs",
    "lvm": "LVM (thick)",
    "dir": "dir",
    "nfs": "NFS",
    "cifs": "SMB/CIFS",
    "iscsi": "iSCSI",
    "iscsidirect": "iSCSI (direct)",
    "cephfs": "CephFS",
    "glusterfs": "GlusterFS",
    "pbs": "Proxmox Backup Server",
}

#: 卷键：`rootfs` 与 `mpN`。`unusedN` 刻意排除 —— 它不参与快照，算进来会让
#: 一台「系统盘在 ZFS、另有一块退役盘挂在 dir」的容器被无谓地拦下。
_ROOTFS_KEY = "rootfs"


def storage_type_label(storage_type: Optional[str]) -> str:
    """存储类型 → 展示名；认不出时原样返回（空则 ``unknown``）。"""
    raw = str(storage_type or "").strip().lower()
    if not raw:
        return "unknown"
    return STORAGE_TYPE_LABELS.get(raw, raw)


def supports_snapshots(storage_type: Optional[str]) -> bool:
    """类型是否支持快照。空类型返回 False —— 调用方要先用 :func:`volume_type`
    区分「已知不支持」与「不知道」，不能拿这个函数当唯一判据。"""
    return str(storage_type or "").strip().lower() in SNAPSHOT_CAPABLE_TYPES


def _flag(value: Any, default: bool = True) -> bool:
    """PVE 的布尔字段混着 0/1、'0'/'1' 与 true/false，统一成 bool。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def volume_storage(value: Any) -> str:
    """从卷配置里取存储名：``local-lvm:vm-102-disk-0,size=8G`` → ``local-lvm``。

    没有冒号的（``none``、空串）不是有效卷，返回空串由调用方跳过。
    """
    text = str(value or "").strip()
    if not text or ":" not in text:
        return ""
    return text.split(":", 1)[0].strip()


def snapshot_volume_keys(cfg: Dict[str, Any]) -> List[str]:
    """容器里参与快照的卷键（rootfs + mpN），按 key 排序。"""
    keys = [key for key in (cfg or {}) if key == _ROOTFS_KEY]
    mps = [
        key
        for key in (cfg or {})
        if key.startswith("mp") and key[2:].isdigit()
    ]
    return keys + sorted(mps, key=lambda k: int(k[2:]))


def evaluate_snapshot_capability(
    cfg: Dict[str, Any], storages: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """纯函数：容器配置 + 节点存储清单 → 快照能力。

    拆成纯函数是为了能直接对「ZFS 系统盘 + dir 数据盘」「存储清单读不到」这类
    组合做单测，不必起 PVE。
    """
    by_name = {
        str(item.get("storage") or ""): item
        for item in (storages or [])
        if item.get("storage")
    }

    volumes: List[Dict[str, Any]] = []
    for key in snapshot_volume_keys(cfg):
        storage = volume_storage(cfg.get(key))
        if not storage:
            continue
        info = by_name.get(storage)
        stype = str((info or {}).get("type") or "").strip()
        volumes.append(
            {
                "key": key,
                # 键名照抄 PVE 配置，前端自己把 rootfs 翻成「系统盘」
                "storage": storage,
                "type": stype,
                "type_label": storage_type_label(stype),
                "storage_known": bool(info),
                # None = 无法确定（存储不在列表里 / 列表读不到）
                "supports_snapshot": supports_snapshots(stype) if stype else None,
                "active": _flag((info or {}).get("active")),
                "shared": _flag((info or {}).get("shared"), default=False),
            }
        )

    blocking = [item for item in volumes if item["supports_snapshot"] is False]
    unknown = [item for item in volumes if item["supports_snapshot"] is None]

    # 当前环境里**真的能用**的快照存储：类型对、已启用、在线。
    available = [
        {
            "storage": name,
            "type": stype,
            "type_label": storage_type_label(stype),
            "shared": _flag(info.get("shared"), default=False),
        }
        for name, info in by_name.items()
        for stype in [str(info.get("type") or "").strip()]
        if supports_snapshots(stype)
        and _flag(info.get("active"))
        and _flag(info.get("enabled"))
    ]
    available.sort(key=lambda item: (item["storage"].lower(), item["type"]))

    return {
        "supported": not blocking,
        "volumes": volumes,
        "blocking": blocking,
        "unknown": unknown,
        "supported_types": list(SNAPSHOT_CAPABLE_TYPES),
        "supported_type_labels": [
            storage_type_label(item) for item in SNAPSHOT_CAPABLE_TYPES
        ],
        "available_storages": available,
    }


async def snapshot_capability(client: Any, node: str, vmid: int) -> Dict[str, Any]:
    """读容器配置 + 节点存储清单，算出快照能力。

    只吞「读存储清单」的失败：读不到就按类型未知处理（放行 + 标记），因为
    快照本身仍可能成功。读容器配置的失败必须往上抛 —— 连盘在哪都不知道，
    继续提交只会得到一句更难懂的 PVE 报错。
    """
    cfg = await client.lxc_config(node, vmid)
    try:
        storages = await client.storages(node)
    except ProxmoxError:
        storages = []
    return evaluate_snapshot_capability(cfg, storages)


def _describe_volumes(items: List[Dict[str, Any]]) -> str:
    """``local（dir）、data-nfs（NFS）``，同一存储只列一次。"""
    seen: List[str] = []
    for item in items:
        text = f'{item["storage"]}（{item["type_label"]}）'
        if text not in seen:
            seen.append(text)
    return "、".join(seen)


def blocked_message(capability: Dict[str, Any]) -> str:
    """给用户看的拒绝原因：当前存储 + 支持的类型 + 当前环境可用的存储。

    这段话同时用于 HTTP 409 的 detail 与批量操作的单台结果 —— 两处都要能独立
    读懂，所以不依赖前端再拼一遍。
    """
    volumes = _describe_volumes(capability.get("blocking") or [])
    types = " / ".join(capability.get("supported_type_labels") or [])
    available = capability.get("available_storages") or []
    if available:
        return i18n.t(
            "storage.snapshot_blocked",
            volumes=volumes,
            types=types,
            available="、".join(
                f'{item["storage"]}（{item["type_label"]}）' for item in available
            ),
        )
    return i18n.t(
        "storage.snapshot_blocked_no_target", volumes=volumes, types=types
    )
