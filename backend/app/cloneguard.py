"""跨节点克隆的前置检查：模板盘必须全在共享存储上，且目标节点挂得到。

为什么需要这个模块
------------------
PVE 的 ``POST /nodes/{node}/qemu/{vmid}/clone`` 支持 ``target``（把克隆体放到
另一台节点），但有前提：**源模板的磁盘得在共享存储上** —— 否则链接克隆引用的
还是模板所在节点的那块卷，目标节点看不到，任务在后台失败，前端只拿到一句
``clone failed``，看不出是「存储没挂到这台节点」。

面板原先这条路上没有任何前置判定：
  * ``/api/templates`` 只回 ``maxdisk``（容量），**不回盘在哪块存储**，前端
    根本没法判断能不能跨节点；
  * 创建向导索性把候选锁死在同节点（``tpl.node === 目标节点``），于是共享存储
    上的模板永远跨不过去 —— 表现就是「模板明明在共享存储上，却提示目标节点
    没有模板」。

判定逻辑只有一条：把模板配置里的每个卷取出来，看它所在的存储「是不是共享的」
以及「目标节点挂没挂」。两者都成立才允许跨节点；否则抛一条**点名存储**的报错，
而不是让 PVE 在任务里失败。
"""

from typing import Any, Dict, List, Optional

from . import pvecache
from .formatters import pve_flag
from .pve import ProxmoxError

#: 节点存储清单的缓存时长（秒）。
#:
#: 「某块共享存储有没有挂到这台节点」是 PVE 侧的配置，改动靠人工，几秒内不会
#: 反复变。缓存 60 秒刚好抵掉「列模板 / 打开向导时每台模板各查一次」的重复请求，
#: 又不至于让刚挂上的存储在面板里一分钟都看不见。
NODE_STORAGE_TTL = 60.0

#: 参与「共享判定」的配置键：虚拟机的磁盘总线 + EFI/TPM 盘。
#:
#: EFI 盘也要算：它同样是一个卷，落在本地存储上时跨节点克隆一样引不到；
#: ``unused*`` 是卸载下来的残留卷，不参与克隆，必须排除。
_VOLUME_KEY = ("scsi", "virtio", "sata", "ide")


def config_volume_storages(cfg: Dict[str, Any], kind: str = "qemu") -> List[str]:
    """配置里的卷分别在哪块存储上（去重、保持出现顺序）。

    容器的系统盘是 ``rootfs``，形态与虚拟机不同（``local-lvm:vm-101-disk-0``），
    单独处理；``mp0..`` 之类的挂载点同样会被克隆，一并算进去。
    """
    if not isinstance(cfg, dict):
        return []

    keys: List[str] = []
    if kind == "lxc":
        keys.append("rootfs")
        keys.extend(
            key
            for key in cfg
            if isinstance(key, str) and key.startswith("mp") and key[2:].isdigit()
        )
    else:
        keys.extend(
            key
            for key in cfg
            if isinstance(key, str)
            and (
                (key.startswith(_VOLUME_KEY) and key.rstrip("0123456789").isalpha())
                or key in ("efidisk0", "tpmstate0")
            )
        )

    out: List[str] = []
    for key in keys:
        value = cfg.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        # 光驱与 cloud-init 盘不是真实卷：前者是 ISO，后者由 PVE 生成
        low = value.lower()
        if "media=cdrom" in low or "cloudinit" in low:
            continue
        head = value.split(",", 1)[0]
        if ":" not in head:
            continue
        name = head.split(":", 1)[0].strip()
        if name and name not in out:
            out.append(name)
    return out


async def node_storages(
    client: Any, node: str, connection_id: str = ""
) -> List[Dict[str, Any]]:
    """节点上挂着的存储（带 ``shared`` / ``active`` 标记），60 秒缓存。"""
    if not node:
        return []

    async def _load() -> List[Dict[str, Any]]:
        return await client.storages(node) or []

    try:
        value = await pvecache.cached(
            f"storages:nodes:{connection_id or '-'}:{node}",
            NODE_STORAGE_TTL,
            _load,
        )
    except ProxmoxError:
        # 读不到存储清单不阻断业务：交给 PVE 自己判，报错再由 explain_clone_error 补话
        return []
    return value if isinstance(value, list) else []


def _volume_blockers(
    volumes: List[str], storages: List[Dict[str, Any]]
) -> tuple[List[str], List[str]]:
    """把「缺失」与「非共享」分开：两者的修法完全不同，不能合并成一句话。

    * ``missing`` —— 目标节点上根本没有这块存储：要去 PVE 里把存储加到该节点
      （数据中心 → 存储 → 编辑 → 节点）；
    * ``local`` —— 存储挂得到，但是本地盘：只能迁模板，或改用同节点克隆。
    """
    by_name = {
        str(item.get("storage") or ""): item
        for item in storages or []
        if isinstance(item, dict) and item.get("storage")
    }
    missing: List[str] = []
    local: List[str] = []
    for name in volumes:
        info = by_name.get(name)
        if info is None:
            missing.append(name)
        elif pve_flag(info.get("shared")) != 1:
            local.append(name)
    return missing, local


def describe_blockers(missing: List[str], local: List[str], target_node: str) -> str:
    """把检查结果写成一条可操作的报错（点名存储与节点）。

    这里刻意只说「存储」而不说「共享存储」：缺失的那块也可能是台本地存储（只是
    没在这台节点上定义），把它叫成共享存储会把用户引向错误的方向。
    """
    parts: List[str] = []
    if missing:
        parts.append(
            f"存储 {', '.join(missing)} 没有挂载到节点 {target_node}"
            f"（在 PVE 的「数据中心 → 存储 → 编辑 → 节点」里给它勾上这台节点）"
        )
    if local:
        parts.append(
            f"磁盘所在的存储 {', '.join(local)} 是本地存储，不能跨节点克隆"
            f"（把模板盘迁到共享存储，或改用节点内克隆）"
        )
    return (
        f"无法把模板克隆到节点 {target_node}："
        + "；".join(parts)
        + "。"
    )


async def ensure_cross_node_clone(
    client: Any,
    *,
    kind: str,
    source_node: str,
    vmid: int,
    target_node: Optional[str],
    connection_id: str = "",
) -> None:
    """跨节点克隆的准入检查；只在真的跨节点时才查（同节点直接放行）。

    查不到模板配置时**放行**：这台机器可能刚好被删，此时让 PVE 回它自己的
    报错（``does not exist``）比在这里编一句「磁盘不在共享存储」准确得多。
    """
    target = str(target_node or "")
    if not target or not source_node or target == source_node:
        return

    try:
        if kind == "lxc":
            cfg = await client.lxc_config(source_node, vmid) or {}
        else:
            cfg = await client.qemu_config(source_node, vmid) or {}
    except ProxmoxError:
        return

    volumes = config_volume_storages(cfg, kind)
    if not volumes:
        return

    storages = await node_storages(client, target, connection_id)
    if not storages:
        # 存储清单读不到（节点离线 / 权限不足）：不在这里下结论
        return

    missing, local = _volume_blockers(volumes, storages)
    if missing or local:
        raise ProxmoxError(
            describe_blockers(missing, local, target),
            status_code=400,
            endpoint="clone",
        )
