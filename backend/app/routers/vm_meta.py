"""虚拟机 / 容器的手动补充信息（面板侧存储）。

面板展示的「IP」有两种情况拿不到：

* **QEMU**：客户机没装或没开 Guest Agent，且网卡是 DHCP —— 没有任何地方能问到地址；
* **LXC**：容器没有 Agent 可问，只能读 `netN` 里的静态配置，DHCP 容器读出来就是 dhcp。

这时让运维手工填一个地址，列表与详情照常展示（并标出「手动」），
不必为了看一眼 IP 去开控制台。

同一份记录里还带一个 ``os``（操作系统家族 key）：列表那一列只能从**来源模板**
认出系统，手工建、ISO 装、导入进来的机器没有来源，就没法显示。这个字段可以在
「备注」弹窗里手工纠正（例如 PVE 的 ostype 停在 ``win*``、实际跑的是 TencentOS），
列表优先用它，取不到再回落到来源模板推断。

``os`` 只影响标识与策略口径，**不会写回 PVE 的 ostype**：改 PVE 的 ostype 会连带
改变虚拟机的固件/磁盘参数（Windows 预设会锁 SATA、E1000），不是一次展示修正
该做的事。要让 PVE 侧也一致，应在停机后按官方方式修改 ``ostype``。

同一个 KV 里还记一条可选的 ``template``（来源模板的 节点 / vmid / 名字）。机器若是
在面板之外建出来的（恢复备份、PVE 界面克隆、装 ISO），审计里没有克隆记录，列表就
认不出系统；备注里手工**关联一张模板**相当于补上这条线索 —— 「操作系统」列、模板名
回填与重装向导的「当前系统」都以它为准。名字只是存档：模板被改名或删除后仍能显示
「当初关联的是哪一张」。

存储沿用 settings KV（与 node-notes 同一套机制），
键为 ``<connection_id 或 ''>|<node>|<vmid>``，值为
``{"ip": ..., "os": <家族 key>, "template": {"node", "vmid", "name"}, "by", "at"}``。
三个字段各自独立：只填其中一个也算一条记录。

**这份数据只用于展示**：面板绝不会把它写回 PVE 的 ipconfig / net0 ——
「改了个显示值却动了机器网络」是这类功能最容易出的事故。

可见性：管理员看到全部条目；普通用户只看得到自己填的 —— 否则别人的
虚拟机地址会顺着这个接口漏出来（列表本身是按归属过滤的）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import guestguard, i18n, ownership, security, store

router = APIRouter(prefix="/api", tags=["vm-meta"])
META_KEY = "vm_meta"

logger = logging.getLogger(__name__)


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
        # 关联的来源模板：节点 + vmid 是定位依据，名字只是存档（模板被改名 / 删除后
        # 仍要能显示「当初关联的是哪一张」）。缺定位信息的旧数据一律当作没有。
        tpl = value.get("template")
        template: Optional[Dict[str, Any]] = None
        if isinstance(tpl, dict):
            tpl_node = str(tpl.get("node") or "").strip()
            try:
                tpl_vmid = int(tpl.get("vmid") or 0)
            except (TypeError, ValueError):
                tpl_vmid = 0
            if tpl_node and tpl_vmid:
                template = {
                    "node": tpl_node,
                    "vmid": tpl_vmid,
                    "name": str(tpl.get("name") or "").strip(),
                }
        # 三个字段都空才算没有条目：只关联了模板的机器同样不该被丢掉
        if not ip and not os_key and not template:
            continue
        entry: Dict[str, Any] = {
            "ip": ip,
            "os": os_key,
            "by": str(value.get("by") or ""),
            "at": int(value.get("at") or 0),
        }
        if template:
            entry["template"] = template
        result[str(key)] = entry
    return result


async def os_of(node: str, vmid: int, connection_id: Optional[str]) -> str:
    """该机器在面板侧登记的系统家族 key；没有记录时返回空串。

    AI 租约与重装向导用它纠正 PVE 的 ``ostype``：后者对 Linux 一律记 ``l26``，
    对导入 / 手工建的机器还可能停在建机时的默认值（例如 ``win11``），而这里是
    运维在面板里确认过的标注。
    """
    item = (await _load()).get(_entry_key(node, vmid, connection_id)) or {}
    return str(item.get("os") or "")


async def _drop(connection_id: Optional[str], node: str, vmid: int, *, keep_ip: bool) -> None:
    """删除登记项（或只清系统标识）。失败只记日志 —— 这是善后，不是主流程。"""
    try:
        items = await _load()
        key = _entry_key(node, vmid, connection_id)
        item = items.get(key)
        if not item:
            return
        if keep_ip and item.get("ip"):
            # 重装换了系统盘：系统标识与「来源模板」关联都描述的是旧盘，一起作废
            kept = {**item, "os": ""}
            kept.pop("template", None)
            items[key] = kept
        else:
            items.pop(key, None)
        await store.set_setting(META_KEY, json.dumps(items, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - 删机器 / 重装不能因为清备注失败而回滚
        logger.warning("清理面板备注失败（%s/%s）", node, vmid, exc_info=True)


async def drop_entry(connection_id: Optional[str], node: str, vmid: int) -> None:
    """机器被删除时清掉它在本模块的登记（手动 IP + 系统标识）。

    键里只有「连接 / 节点 / VMID」，而 PVE 会把空闲的 VMID 重新分配给新机器。
    不清的话，同号的新机器会继承上一台的手动地址，最刺眼的是系统标识 ——
    刚下发的 TencentOS 会顶着上一台 Windows 机器的 logo。删除接口里创建时间、
    IP 缓存、地址池、归属、置顶都按这个口径清过一遍，这里是漏掉的一处。
    """
    await _drop(connection_id, node, vmid, keep_ip=False)


async def drop_os(connection_id: Optional[str], node: str, vmid: int) -> None:
    """系统盘被换掉（重装）之后清掉系统标识与来源模板关联，但保留手动 IP。

    重装换了模板，原来那句「这台是 Windows」和「来源是 Kylin-V10」都不再成立 ——
    而它们优先级最高，不清的话列表徽标会一直停在旧系统。手动 IP 与系统盘无关，
    重装后运维多半还要靠它定位机器，所以只清 ``os`` 与 ``template``。
    """
    await _drop(connection_id, node, vmid, keep_ip=True)


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
    # 客户机类型：容器的归属记在 ct 下、权限也是 ct.config，不能一律按虚拟机处理
    # （否则只有容器权限的账号会被 403 挡住，或者反过来改到别人的容器上）。
    type: str = Field(default="qemu", pattern=r"^(qemu|lxc)$")
    # 留空 = 清除该条手动记录（回落到平台自动识别）
    ip: str = Field(default="", max_length=64)
    # 系统家族 key（见 utils/osFamily）。
    # ``None`` = 不改（只传 ip 的调用方不会把已有标注抹掉）；空串 = 清除标注。
    os: Optional[str] = Field(default=None, max_length=16, pattern=r"^[a-z0-9]*$")
    # 手工关联的「来源模板」（面板侧记录，不写回 PVE）。
    # ``template_node`` 三态与 ``os`` 一致：None = 不改；空串 = 解除关联；
    # 节点 + vmid 表示关联到集群里的某张模板，``template_name`` 只作存档展示。
    template_node: Optional[str] = Field(default=None, max_length=64)
    template_vmid: Optional[int] = None
    template_name: Optional[str] = Field(default=None, max_length=128)


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
    user: Dict[str, Any] = Depends(
        security.require_any_permission("vm.config", "ct.config")
    ),
) -> Dict[str, Any]:
    node = payload.node.strip()
    ip = payload.ip.strip()
    kind = ownership.kind_for_type(payload.type)
    # 容器与虚拟机是两把钥匙：入口按类型摆，这里再按类型收一次 —— 只有
    # vm.config 的账号不该能改容器的备注，反之亦然。
    need = "ct.config" if kind == ownership.KIND_CT else "vm.config"
    if not security.has_user_permission(user, need):
        raise HTTPException(
            status_code=403,
            detail=i18n.t(
                "error.permission_denied_role",
                permission=need,
                role=user["role"],
            ),
        )
    # 对象级：条目是按 (连接, 节点, vmid) 落库的，随便填一个别人的号就能改写
    # 管理员看到的那台机器的地址（展示与部分判定都以它为准）。
    await guestguard.require_guest_object(
        user,
        node,
        payload.vmid,
        kind,
        connection=payload.connection_id,
    )
    items = await _load()
    key = _entry_key(node, payload.vmid, payload.connection_id)

    existing = items.get(key) or {}
    # 没传 ``os`` 时保留记录里已有的值：删地址不该把系统标注一起抹掉
    os_key = str(existing.get("os") or "")
    if payload.os is not None:
        os_key = payload.os.strip().lower()

    # 来源模板同样是三态：None = 保留原关联；空串 = 解除；节点 + vmid = 关联。
    # 只给了节点不给 vmid（或反之）当作解除 —— 半条关联没法定位模板，留着只会让
    # 列表去解析一个不存在的对象。
    template: Optional[Dict[str, Any]] = existing.get("template")
    if payload.template_node is not None:
        tpl_node = payload.template_node.strip()
        if tpl_node and payload.template_vmid:
            template = {
                "node": tpl_node,
                "vmid": int(payload.template_vmid),
                "name": str(payload.template_name or "").strip(),
            }
        else:
            template = None

    if ip or os_key or template:
        entry: Dict[str, Any] = {
            "ip": ip,
            "os": os_key,
            "by": str(user.get("username") or ""),
            "at": int(time.time()),
        }
        if template:
            entry["template"] = template
        items[key] = entry
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
            # 关联的是哪一张模板要能追溯：这条记录会改变列表徽标与重装推荐
            "template": (
                f"{template['node']}/{template['vmid']}" if template else ""
            ),
            "connection_id": payload.connection_id or "",
        },
    )
    return {"key": key, "item": items.get(key)}
