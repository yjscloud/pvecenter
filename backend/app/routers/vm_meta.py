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

    **节点是可变的那一段**：机器迁移之后键就变了，按旧键写的条目会变成孤儿
    （列表里那份「系统标识 / 关联模板」就此消失）。所以凡是要「按机器」操作的
    地方都用 :func:`_same_identity` 再兜一层 —— 连接 + VMID 才是这台机器的身份
    （VMID 在一条连接内唯一）。
    """
    return f"{connection_id or ''}|{node}|{vmid}"


def _same_identity(left: str, right: str) -> bool:
    """两个条目键是不是同一台机器：只看「连接 + VMID」，忽略节点。"""
    a = str(left).rsplit("|", 2)
    b = str(right).rsplit("|", 2)
    return len(a) == 3 and len(b) == 3 and a[0] == b[0] and a[2] == b[2]


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
            tpl_name = str(tpl.get("name") or "").strip()
            # 容器的「来源模板」可以是一个**模板文件**（vztmpl），没有 VMID 也
            # 没有节点 —— 名字就是唯一标识（`ubuntu-24.04-...tar.zst`）。
            # 只认「节点 + VMID」会把这类记录整个丢掉，列表就少了模板名。
            if (tpl_node and tpl_vmid) or tpl_name:
                template = {
                    "node": tpl_node,
                    "vmid": tpl_vmid,
                    "name": tpl_name,
                }
        # 系统信息快照（隔离 / 解除隔离 / 开机时采集，见 app.guest_osinfo）。
        # 只做展示与恢复用，空字段一律丢掉。
        snap_raw = value.get("snapshot")
        snapshot: Optional[Dict[str, Any]] = None
        if isinstance(snap_raw, dict):
            snapshot = {
                str(name): item
                for name, item in snap_raw.items()
                if item not in (None, "")
            }
            if not snapshot:
                snapshot = None
        # 三个字段都空才算没有条目：只关联了模板 / 只留了快照的机器同样不该被丢掉
        if not ip and not os_key and not template and not snapshot:
            continue
        entry: Dict[str, Any] = {
            "ip": ip,
            "os": os_key,
            "by": str(value.get("by") or ""),
            "at": int(value.get("at") or 0),
        }
        if template:
            entry["template"] = template
        if snapshot:
            entry["snapshot"] = snapshot
            entry["snapshot_at"] = int(value.get("snapshot_at") or 0)
            if value.get("auto"):
                entry["auto"] = True
        result[str(key)] = entry
    return result


async def save_snapshot(
    connection_id: Optional[str],
    node: str,
    vmid: int,
    snapshot: Dict[str, Any],
    *,
    os_family: str = "",
    by: str = "",
) -> None:
    """把一次系统信息采集写进这台机器的备注条目（**只补不删**）。

    * 合并而不是替换：这次没采集到的字段不覆盖旧值（PVE 返回精简 config、某个
      字段被清空，都不该让「上一次的版本 / 主机名」跟着消失）；
    * ``os_family`` 只在条目**还没有** ``os`` 时补上 —— 人工标注永远优先；且
      ``linux`` / ``other`` 这种大类不回填（它们只进快照兜底）；
    * 记录写在「连接 + VMID」这个身份上：机器迁移过之后照样认领，并顺手把旧节点
      那一条收掉（与 :func:`save_vm_meta` 同一口径）。

    失败只记日志：它是隔离 / 开关机的附加动作，不能反过来影响主流程。
    """
    if not node or not vmid:
        return
    try:
        items = await _load()
        key = _entry_key(node, vmid, connection_id)
        entry: Dict[str, Any] = {}
        inherited = False
        for other, value in items.items():
            if other == key or _same_identity(other, key):
                entry = dict(value)
                inherited = other != key
                break
        if not entry:
            entry = {"ip": "", "os": ""}

        merged = dict(entry.get("snapshot") or {})
        for name, value in (snapshot or {}).items():
            if value not in (None, ""):
                merged[name] = value
        if not merged:
            return
        entry["snapshot"] = merged
        entry["snapshot_at"] = int(time.time())
        entry["auto"] = True
        family = str(os_family or "").strip().lower()
        # 只有**具体**家族才回填：``linux`` / ``other`` 这类大类一旦写进 ``os``，
        # 就会永远压过来源模板名带来的更具体判断（列表以 os 优先），把一台 Ubuntu
        # 显示成通用 Linux。大类只留在快照里兜底。
        if family and family not in ("linux", "other"):
            if not str(entry.get("os") or "").strip():
                entry["os"] = family
        entry["ip"] = str(entry.get("ip") or "")
        entry["by"] = str(entry.get("by") or by or "")
        entry["at"] = int(entry.get("at") or time.time())

        items[key] = entry
        # 同一台机器只留一条（迁移前后各写过一次的话）
        for other in [k for k in items if k != key and _same_identity(k, key)]:
            items.pop(other, None)
        await store.set_setting(META_KEY, json.dumps(items, ensure_ascii=False))
        logger.debug(
            "已保存系统信息快照（%s/%s，%s）", node, vmid, "认领旧条目" if inherited else "新条目"
        )
    except Exception:  # noqa: BLE001 - 附加动作，失败不该影响隔离 / 开关机
        logger.warning("保存系统信息快照失败（%s/%s）", node, vmid, exc_info=True)


async def os_of(node: str, vmid: int, connection_id: Optional[str]) -> str:
    """该机器在面板侧登记的系统家族 key；没有记录时返回空串。

    AI 租约与重装向导用它纠正 PVE 的 ``ostype``：后者对 Linux 一律记 ``l26``，
    对导入 / 手工建的机器还可能停在建机时的默认值（例如 ``win11``），而这里是
    运维在面板里确认过的标注。

    条目可能还挂在机器迁移前的节点下，所以按「连接 + VMID」再兜一次。
    """
    items = await _load()
    key = _entry_key(node, vmid, connection_id)
    item = items.get(key)
    if not item:
        for other, value in items.items():
            if _same_identity(other, key):
                item = value
                break
    return str((item or {}).get("os") or "")


async def _drop(connection_id: Optional[str], node: str, vmid: int, *, keep_ip: bool) -> None:
    """删除登记项（或只清系统标识）。失败只记日志 —— 这是善后，不是主流程。"""
    try:
        items = await _load()
        key = _entry_key(node, vmid, connection_id)
        # 同一台机器可能在多个节点下都有条目（迁移前写的 + 迁移后写的）：
        # 一次清干净，别让孤儿行在 VMID 被回收时贴到新机器上。
        targets = [other for other in items if _same_identity(other, key)]
        if not targets:
            return
        for other in targets:
            item = items.get(other) or {}
            if keep_ip and item.get("ip"):
                # 重装换了系统盘：系统标识与「来源模板」关联描述的都是旧盘，一起作废；
                # 手动 IP 与系统盘无关，留着
                kept = {**item, "os": ""}
                kept.pop("template", None)
                items[other] = kept
            else:
                items.pop(other, None)
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


async def set_template(
    connection_id: Optional[str],
    node: str,
    vmid: int,
    template: Optional[Dict[str, Any]],
    *,
    by: str = "",
) -> None:
    """把「来源模板」关联写到某台机器上（克隆体继承用）。

    只动 ``template`` 一个字段，手动 IP / 系统标识 / 快照原样保留。
    失败只记日志 —— 克隆机已经建好了，这是展示层的附加信息。
    """
    if not template or not node or vmid is None:
        return
    # 模板文件（容器用系统模板新建）没有节点与 VMID，只有一个文件名
    tpl_node = str(template.get("node") or "").strip()
    tpl_name = str(template.get("name") or "").strip()
    try:
        tpl_vmid = int(template.get("vmid") or 0)
    except (TypeError, ValueError):
        tpl_vmid = 0
    if not tpl_node and not tpl_vmid and not tpl_name:
        return
    if not tpl_node:
        tpl_node = node if tpl_vmid else ""
    try:
        items = await _load()
        key = _entry_key(node, vmid, connection_id)
        entry: Dict[str, Any] = {}
        for other, value in items.items():
            if other == key or _same_identity(other, key):
                entry = dict(value)
                break
        if not entry:
            entry = {"ip": "", "os": ""}
        entry["template"] = {
            "node": tpl_node,
            "vmid": tpl_vmid,
            "name": tpl_name,
        }
        entry["ip"] = str(entry.get("ip") or "")
        entry["os"] = str(entry.get("os") or "")
        entry["by"] = str(entry.get("by") or by or "")
        entry["at"] = int(entry.get("at") or time.time())
        items[key] = entry
        # 同一台机器只留一条（迁移前后各写过一次的话）
        for other in [k for k in items if k != key and _same_identity(k, key)]:
            items.pop(other, None)
        await store.set_setting(META_KEY, json.dumps(items, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - 附加信息，失败不影响克隆结果
        logger.warning("写入来源模板失败（%s/%s）", node, vmid, exc_info=True)


async def template_of(
    node: str,
    vmid: int,
    connection_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """该机器登记的「来源模板」；没有返回 ``None``。

    克隆时用它把源机器已经关联的模板原样带给克隆体（人工关联的、或者它自己
    克隆时继承来的都可以），这样源显示什么模板，克隆体就显示什么模板。
    """
    items = await _load()
    key = _entry_key(node, vmid, connection_id)
    item = items.get(key)
    if not item:
        for other, value in items.items():
            if _same_identity(other, key):
                item = value
                break
    tpl = (item or {}).get("template")
    return tpl if isinstance(tpl, dict) else None


async def manual_ip_index(user: Dict[str, Any]) -> Dict[Tuple[str, int], str]:
    """手动备注 IP 的索引 ``(连接, vmid) -> ip``，供列表 / 导出算「网络」。

    可见性与 :func:`get_vm_meta` 一致：管理员全量，普通用户只看自己填的 ——
    否则别人的虚拟机手动地址会顺着「网络归属」这个侧信道漏出来（虽然只漏池名，
    但推池名的输入是别人填的 IP，不能拿它当自己的数据用）。

    键刻意不含节点：机器迁移之后条目还挂在旧节点下，按节点取会让「网络」列
    丢掉那台机器的手动地址。VMID 在一条连接内唯一，同号只可能是它自己。
    """
    items = await _load()
    if user.get("role") != "admin":
        username = str(user.get("username") or "")
        items = {k: v for k, v in items.items() if v.get("by") == username}
    out: Dict[Tuple[str, int], str] = {}
    for key, value in items.items():
        try:
            conn, _node, vmid_s = str(key).rsplit("|", 2)
            out[(conn, int(vmid_s))] = str(value.get("ip") or "")
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

    # 迁移过的机器：原条目还挂在旧节点下。先认领再写，否则「只改 IP」的时候
    # 会把原来那条的系统标识 / 关联模板孤立在旧节点，等于把它们丢了。
    existing = items.get(key) or {}
    if not existing:
        for other, value in items.items():
            if _same_identity(other, key):
                existing = value
                break
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

    # 同一台机器在其它节点下的旧条目（迁移前写的）随这次保存一起收掉：
    # 界面上按「连接 + VMID」回落读取，留着两条会读到过期那一条。
    for other in [k for k in items if k != key and _same_identity(k, key)]:
        items.pop(other, None)

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
