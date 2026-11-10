"""备份归档 volid 的解析：这份归档属于哪种客户机、是哪个 VMID。

面板需要回答两个问题：「恢复出来是虚拟机还是容器」和「这是谁的机器」。
两种存储给出的 volid 形状完全不同：

* 目录型存储（dir / NFS / CIFS）—— 类型和 vmid 都在 PVE 自己的文件名里：
  ``local:backup/vzdump-qemu-100-2026_09_22-21_52_29.vma.zst``；
* PBS —— 按客户机类型分目录，只有 vmid 没有前缀：
  ``pbs:backup/ct/104/2026_11_10T02_00_00Z``。

早前各处只认 ``vzdump-lxc`` 子串，于是 PBS 上的容器归档被当成虚拟机发去了
``/nodes/{node}/qemu``，PVE 回一句 ``got unexpected backup format 'pbs-ct'``，
顺带把整份 LXC 配置按 QEMU 解析（hostname / net0 / rootfs 全部报 unknown）。
集中在这里解析一次，别让每个调用点各自猜。
"""
from __future__ import annotations

import re
from typing import Optional

#: ``vzdump-qemu-110-...`` / ``vzdump-lxc-110-...``（目录型存储的命名）
_VZDUMP_RE = re.compile(r"vzdump-(qemu|lxc)-(\d+)")
#: ``pbs:backup/ct/104/2026_11_10T02_00_00Z`` / ``pbs:backup/vm/104/...``
#: 注意 ``backup`` 前面是存储名后的冒号（``pbs:``），不是斜杠。
_PBS_RE = re.compile(r"(?:^|[:/])backup/(ct|vm)/(\d+)(?:/|$)")

#: PVE 在 storage content 里给的 ``format``。PBS 直接写客户机类型；
#: 目录型存储靠文件格式区分（容器只能是 tar 系，虚拟机是 vma / qcow2 / raw）。
_FORMAT_TYPES = {
    "pbs-ct": "lxc",
    "pbs-vm": "qemu",
    "tar": "lxc",
    "tar.zst": "lxc",
    "tar.gz": "lxc",
    "tgz": "lxc",
    "vma": "qemu",
    "vma.zst": "qemu",
    "vma.lzo": "qemu",
    "qcow2": "qemu",
    "raw": "qemu",
}


def archive_guest_type(volid: str, fmt: Optional[str] = None) -> Optional[str]:
    """归档的客户机类型：``"qemu"`` / ``"lxc"``，认不出时返回 ``None``。

    优先看 volid（最可靠，PVE 自己写的），再看 storage content 的 ``format``。
    """
    text = volid or ""
    match = _VZDUMP_RE.search(text)
    if match:
        return "lxc" if match.group(1) == "lxc" else "qemu"
    match = _PBS_RE.search(text)
    if match:
        return "lxc" if match.group(1) == "ct" else "qemu"
    kind = _FORMAT_TYPES.get((fmt or "").strip().lower())
    if kind:
        return kind
    return None


def vmid_from_volid(volid: str) -> Optional[int]:
    """从归档 volid 解析 vmid（目录型存储的 ``vzdump-<type>-<vmid>``，或 PBS 的
    ``/backup/<ct|vm>/<vmid>/``）。"""
    text = volid or ""
    match = _VZDUMP_RE.search(text)
    if match:
        return int(match.group(2))
    match = _PBS_RE.search(text)
    if match:
        return int(match.group(2))
    return None
