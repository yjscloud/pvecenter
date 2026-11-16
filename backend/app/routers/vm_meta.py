"""虚拟机 / 容器的手动补充信息（面板侧存储）。

面板展示的「IP」有两种情况拿不到：

* **QEMU**：客户机没装或没开 Guest Agent，且网卡是 DHCP —— 没有任何地方能问到地址；
* **LXC**：容器没有 Agent 可问，只能读 `netN` 里的静态配置，DHCP 容器读出来就是 dhcp。

这时让运维手工填一个地址，列表与详情照常展示（并标出「手动」），
不必为了看一眼 IP 去开控制台。

同一份记录里还带一个 ``os``（操作系统家族 key）：列表那一列只能从**来源模板**
认出系统，手工建、ISO 装、导入进来的机器没有来源，就没法显示。这个字段由面板
侧直接写入（没有编辑接口），列表优先用它，取不到再回落到来源模板推断。

存储沿用 settings KV（与 node-notes 同一套机制），
键为 ``<connection_id 或 ''>|<node>|<vmid>``，值为
``{"ip": ..., "os": <家族 key>, "by": <填写人>, "at": <时间戳>}``。
两个字段各自独立：只填其中一个也算一条记录。

**这份数据只用于展示**：面板绝不会把它写回 PVE 的 ipconfig / net0 ——
「改了个显示值却动了机器网络」是这类功能最容易出的事故。

可见性：管理员看到全部条目；普通用户只看得到自己填的 —— 否则别人的
虚拟机地址会顺着这个接口漏出来（列表本身是按归属过滤的）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .. import guestguard, ownership, security, store

router = APIRouter(prefix="/api", tags=["vm-meta"])
META_KEY = "vm_meta"


def _entry_key(node: str, vmid: int, connection_id: Optional[str]) -> str:
    """条目键：连接 + 节点 + vmid。

    一定要带连接：两台 PVE 上完全可能同时存在 node1/100，
    不带连接会把两台不同的机器当成同一台，IP 互相覆盖。
    """
    return f"{connection_id or ''}|{node}|{vmid}"


async def _load() -> Dict[str, Dict[str, Any]]:
    raw = await store.get_setting(META_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        ip = str(value.get("ip") or "").strip()
        os_key = str(value.get("os") or "").strip().lower()
        # 两个字段都空才算没有条目：只标了系统的机器不该因为「没填 IP」被丢掉
        if not ip and not os_key:
            continue
        result[str(key)] = {
            "ip": ip,
            "os": os_key,
            "by": str(value.get("by") or ""),
            "at": int(value.get("at") or 0),
        }
    return result


async def manual_ip_index(user: Dict[str, Any]) -> Dict[Tuple[str, str, int], str]:
    """手动备注 IP 的索引 ``(连接, 节点, vmid) -> ip``，供列表 / 导出算「网络」。

    可见性与 :func:`get_vm_meta` 一致：管理员全量，普通用户只看自己填的 ——
    否则别人的虚拟机手动地址会顺着「网络归属」这个侧信道漏出来（虽然只漏池名，
    但推池名的输入是别人填的 IP，不能拿它当自己的数据用）。

    键用三元组而非原串：调用方（``collect_vms`` / ``list_containers``）拿到的
    就是这三个字段，不必再和这里的串格式耦合。
    """
    items = await _load()
    if user.get("role") != "admin":
        username = str(user.get("username") or "")
        items = {k: v for k, v in items.items() if v.get("by") == username}
    out: Dict[Tuple[str, str, int], str] = {}
    for key, value in items.items():
        try:
            conn, node, vmid_s = str(key).rsplit("|", 2)
            out[(conn, node, int(vmid_s))] = str(value.get("ip") or "")
        except ValueError:
            continue
    return out


class VmMetaInput(BaseModel):
    node: str
    vmid: int
    connection_id: Optional[str] = None
    # 留空 = 清除该条手动记录（回落到平台自动识别）
    ip: str = Field(default="", max_length=64)


@router.get("/vm-meta")
async def get_vm_meta(
    user: Dict[str, Any] = Depends(security.require_permission("vm.view")),
) -> Dict[str, Any]:
    items = await _load()
    if user.get("role") != "admin":
        username = str(user.get("username") or "")
        items = {k: v for k, v in items.items() if v.get("by") == username}
    return {"items": items}


@router.put("/vm-meta")
async def save_vm_meta(
    payload: VmMetaInput,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("vm.config")),
) -> Dict[str, Any]:
    node = payload.node.strip()
    ip = payload.ip.strip()
    # 对象级：条目是按 (连接, 节点, vmid) 落库的，随便填一个别人的号就能改写
    # 管理员看到的那台机器的地址（展示与部分判定都以它为准）。
    await guestguard.require_guest_object(
        user,
        node,
        payload.vmid,
        ownership.KIND_VM,
        connection=payload.connection_id,
    )
    items = await _load()
    key = _entry_key(node, payload.vmid, payload.connection_id)

    # 保留记录里已有的 ``os``：这个接口只管 IP，删地址不该把系统标注一起抹掉
    os_key = str((items.get(key) or {}).get("os") or "")
    if ip or os_key:
        items[key] = {
            "ip": ip,
            "os": os_key,
            "by": str(user.get("username") or ""),
            "at": int(time.time()),
        }
    else:
        items.pop(key, None)

    await store.set_setting(META_KEY, json.dumps(items, ensure_ascii=False))
    await security.audit(
        request,
        user,
        "vm_meta.save" if ip else "vm_meta.clear",
        target=f"{node}/{payload.vmid}",
        detail={
            "ip": ip,
            "os": os_key,
            "connection_id": payload.connection_id or "",
        },
    )
    return {"key": key, "item": items.get(key)}
