"""系统信息快照：在隔离 / 解除隔离 / 开机这些节点把客户机的系统信息留一份。

## 为什么要单独做这件事

列表与详情里的「操作系统」不是 PVE 给的字段，是面板**拼**出来的三类证据：

1. ``vm_meta.os`` —— 运维在「备注」里确认过的系统家族（面板侧持久化）；
2. 来源模板 —— 从审计里的克隆 / 重装 / 建机记录推出来；
3. PVE 的 ``ostype`` —— 逐台读 config 才知道。

这三条都有「读不到的瞬间」：应急隔离会把机器关掉、取证快照会给 VM 上锁、
迁移 / 备份期间 config 可能短暂不可读；而这类读失败**没有任何回写路径** ——
列表退回「按机器名猜」，猜不出就是「—」。用户看到的就是「隔离完解除隔离，
系统信息没了」，而且重启也回不来，因为它本来就没被存下来过。

所以这里在**关键时刻主动采集一次**并存进 ``vm_meta`` 同一条记录：

* 隔离时 —— 处置刚做完，config 一定还读得到，是最后一份可信现场；
* 解除隔离时 —— 采集成功就顺手把缺失的 ``os`` 家族补回去（恢复逻辑）；
* 开机 / 重启后 —— 系统盘没换过，重新确认一次（重装后由重装流程负责作废）。

## 只补不删

任何一步采集失败（PVE 不可达、权限不足、机器已删）都**不写任何东西**：保留上一个
已知值，绝不能因为「这次没读到」把已有信息抹掉 —— 那正是这个 bug 的形状。
合并时同样按字段补：新采集到的空字段不覆盖旧值。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from .pve import ProxmoxError, get_client

logger = logging.getLogger(__name__)

#: 采集哪些 config 字段。
#:
#: 只取「换机器也不会变、又能说明这台机器是什么系统」的那几项：系统类型、名字、
#: CPU / 内存规模。磁盘与网络是易变的，不属于「系统信息」。
_QEMU_FIELDS = ("ostype", "name", "cores", "sockets", "memory")
_LXC_FIELDS = ("ostype", "hostname", "arch", "cores", "memory", "ostemplate")

#: 容器 ostype 就直接是发行版名，映射到面板的家族 key（与前端 osFamily.ts 同一份口径）。
_LXC_FAMILY = {
    "almalinux": "alma",
    "alma": "alma",
    "opensuse": "suse",
    "suse": "suse",
    "tencentos": "tencent",
    "redhat": "redhat",
    "rhel": "redhat",
    "oracle": "redhat",
    "alios": "alios",
    "opencloudos": "opencloudos",
    "redflag": "redflag",
}


def family_of(ostype: Any, kind: str = "qemu") -> str:
    """由 PVE 的 ``ostype`` 推出面板的家族 key；推不出就返回空串。

    只做「有把握」的判断：``win*`` 一定是 Windows，QEMU 的 ``l26`` 是 Linux 大类；
    容器的 ostype 本身就是发行版名。其它一律留空 —— 猜错会把图标贴到错误的系统上，
    比不显示更糟。
    """
    text = str(ostype or "").strip().lower()
    if not text:
        return ""
    if text.startswith("win"):
        return "windows"
    if kind == "lxc":
        if text in _LXC_FAMILY:
            return _LXC_FAMILY[text]
        # 容器 ostype 与前端家族 key 同名（debian / ubuntu / alpine / centos …）
        return text
    if text in ("l26", "l24", "2.6", "kvm64", "x86_64"):
        return "linux"
    if text in ("solaris", "other"):
        return "other"
    return ""


async def _read_config(
    client: Any, node: str, vmid: int, kind: str
) -> Optional[Dict[str, Any]]:
    """读一次 config；读不到返回 ``None``（调用方据此**放弃写入**）。"""
    try:
        if kind == "lxc":
            return await client.lxc_config(node, vmid) or {}
        return await client.qemu_config(node, vmid) or {}
    except ProxmoxError as exc:
        logger.debug("采集系统信息失败（%s/%s）：%s", node, vmid, exc)
        return None
    except Exception:  # noqa: BLE001 - 采集是附加动作，任何异常都只是「这次没有」
        logger.debug("采集系统信息异常（%s/%s）", node, vmid, exc_info=True)
        return None


def _snapshot_of(config: Dict[str, Any], kind: str, reason: str) -> Dict[str, Any]:
    fields = _LXC_FIELDS if kind == "lxc" else _QEMU_FIELDS
    snapshot: Dict[str, Any] = {}
    for name in fields:
        value = config.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            snapshot[name] = text
    family = family_of(config.get("ostype") or (config.get("hostname") if kind == "lxc" else ""), kind)
    if family:
        snapshot["family"] = family
    snapshot["kind"] = kind
    snapshot["taken_at"] = int(time.time())
    if reason:
        snapshot["reason"] = reason
    return snapshot


async def _owner_of(conn_id: Any, node: str, vmid: int, actor: str) -> str:
    """这条快照记在谁名下：优先机器归属，其次操作人。

    ``/vm-meta`` 对普通用户按 ``by`` 过滤（见 routers/vm_meta.get_vm_meta），
    管理员代用户处置时若写成管理员名，VM 所有者反而看不到这条快照 —— 系统信息
    又一次「丢了」。所以先按归属解析。
    """
    try:
        from . import ownership

        owners = await ownership.guest_owners_map()
        owner = ownership.vm_owner_from(owners, node, vmid, str(conn_id or ""))
        if owner:
            return owner
    except Exception:  # noqa: BLE001 - 归属拿不到就退回操作人
        logger.debug("解析机器归属失败（%s/%s）", node, vmid, exc_info=True)
    return actor


async def capture(
    node: str,
    vmid: int,
    *,
    kind: str = "qemu",
    conn_id: Any = "",
    reason: str = "",
    actor: str = "",
    restore_os: bool = False,
    client: Any = None,
) -> Optional[Dict[str, Any]]:
    """采集一次系统信息并落库；返回快照，采集失败返回 ``None``。

    ``restore_os`` 为真时，若这条记录没有 ``os`` 家族，用采集到的 ostype 补上 ——
    解除隔离 / 重启后「图标回不来」的修复点就在这。
    """
    if not node or not vmid:
        return None
    api = client or get_client()
    config = await _read_config(api, node, int(vmid), kind)
    if config is None:
        # 读不到就什么都不写：宁可显示上一次的值，也不能把已有信息当成「已清除」
        return None
    snapshot = _snapshot_of(config, kind, reason)
    if not snapshot:
        return None
    by = await _owner_of(conn_id, node, int(vmid), actor)
    # 延迟导入：vm_meta 是路由模块，导入链反过来到不了这里
    try:
        from .routers.vm_meta import save_snapshot

        await save_snapshot(
            conn_id,
            node,
            int(vmid),
            snapshot,
            os_family=snapshot.get("family") if restore_os else "",
            by=by,
        )
    except Exception:  # noqa: BLE001 - 落库失败不该影响隔离 / 开关机
        logger.warning("保存系统信息快照失败（%s/%s）", node, vmid, exc_info=True)
        return None
    return snapshot
