"""应急响应接口：虚拟机一键隔离与解除。

路径挂在 ``/api/vms/{node}/{vmid}/quarantine`` 下，与 vms 路由共用同一套归属
校验（``require_vm_access``）：普通用户只能处置自己名下的机器。

* ``GET  …/quarantine`` —— 当前隔离状态（断了几张网卡、是否保护、有哪些取证快照），
  读权限用 ``vm.view``，让被隔离机器的所有者能看到发生了什么；
* ``POST …/quarantine`` —— 一键隔离（取证快照 → 断网 → 关机 → 加保护），
  需要 ``vm.isolate`` 并且走二次确认；
* ``POST …/quarantine/release`` —— 解除隔离（恢复网卡 → 解除保护 → 可选开机）；
* ``GET  /api/isolation`` —— **已登记**的隔离清单（面板发起过的隔离动作）。

## 为什么会有一张登记表

隔离状态本身存在 PVE 侧（``link_down`` / ``protection`` / 取证快照），PVE 的
``/cluster/resources`` 既不返回 ``protection`` 也不展开 ``netX`` —— 面板若不在
自己库里记一笔，刷新页面后就只剩「选一台机器才知道它有没有被隔离」，页面上的
处置记录（断了几张网卡、快照名、操作人、时间）也一起丢了。登记只是**索引**，
单台的真实状态仍以 PVE 实时读到的为准（见 :mod:`app.quarantine_store`）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import (
    guest_osinfo,
    i18n,
    isolation,
    ownership,
    quarantine_store,
    security,
)
from ..pve import ProxmoxError
from ..schemas import QuarantineIn, QuarantineReleaseIn
from .vms import _op_connection, require_vm_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vms", tags=["isolation"])
#: 隔离清单单独一条前缀：``/vms/{node}/{vmid}`` 是「单台」语义，清单不是。
list_router = APIRouter(prefix="/api/isolation", tags=["isolation"])

ISOLATE = security.require_permission("vm.isolate")
VIEW = security.require_permission("vm.view")


def _record_owner(record: Dict[str, Any], vm_owners: Dict[str, str]) -> Optional[str]:
    """这条隔离记录归谁：先看机器归属，取不到才回落登记人。

    与「受保护备份」的可见范围同一套口径（见 routers/backups.py 的
    ``_protected_record_owner``）：管理员代用户处置时，``recorded_by`` 写的是
    管理员，而这台机器属于用户。
    """
    owner = ownership.vm_owner_from(
        vm_owners,
        record.get("node") or "",
        record.get("vmid") or 0,
        str(record.get("conn_id") or ""),
    )
    return owner or (str(record.get("recorded_by") or "") or None)


@list_router.get("", dependencies=[Depends(VIEW)])
async def list_isolated(user: Dict[str, Any] = Depends(VIEW)) -> Dict[str, Any]:
    """当前登记在案的隔离清单（最近的排前面）。

    普通用户只看得到自己机器上的 / 自己处置的那几条 —— 清单里有机器名与备注，
    不该跨用户可见。
    """
    records = await quarantine_store.list_records()
    scope = security.visible_owner(user)
    if scope is not None:
        vm_owners = await ownership.guest_owners_map()
        records = [item for item in records if _record_owner(item, vm_owners) == scope]
    items: List[Dict[str, Any]] = records
    return {"items": items, "count": len(items)}


@router.get("/{node}/{vmid}/quarantine", dependencies=[Depends(require_vm_access)])
async def quarantine_status(
    node: str,
    vmid: int,
    request: Request,
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """这台虚拟机当前的隔离状态。"""
    try:
        payload = await isolation.status(node, vmid)
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=f"读取虚拟机状态失败：{exc}") from exc
    await security.audit_read(
        request,
        user,
        "vm.quarantine.read",
        target=f"{node}/{vmid}",
        detail=(
            f"隔离中={payload['isolated']}，已断网卡 {len(payload['cut_interfaces'])} 张，"
            f"取证快照 {len(payload['evidence_snapshots'])} 个"
        ),
    )
    return payload


@router.post("/{node}/{vmid}/quarantine", dependencies=[Depends(require_vm_access)])
async def quarantine_vm(
    node: str,
    vmid: int,
    payload: QuarantineIn,
    request: Request,
    user: Dict[str, Any] = Depends(ISOLATE),
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, Any]:
    """一键隔离（先取证快照，再断网 / 关机）。逐步骤回报成败。"""
    try:
        result = await isolation.quarantine(
            node,
            vmid,
            take_snapshot=payload.snapshot,
            cut_network=payload.cut_network,
            power_action=payload.power_action,
            protect=payload.protect,
            note=payload.note,
            actor=str(user.get("username") or ""),
        )
    except (RuntimeError, ProxmoxError) as exc:
        await security.audit(
            request, user, "vm.quarantine", target=f"{node}/{vmid}",
            result="failed", detail=str(exc)[:500],
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— 兜底：任何意外都要给一句能读懂的话
        # 曾经的教训：隔离里一句模板占位符写错（TypeError），整个请求变成
        # 500「服务器内部错误」—— 用户既不知道隔离到底做没做，也没法自助。
        # 这里留全栈日志，对外给一句明确的话 + 归属（已记录，可上报）。
        logger.exception("隔离执行异常：%s/%s", node, vmid)
        await security.audit(
            request, user, "vm.quarantine", target=f"{node}/{vmid}",
            result="failed", detail=f"未预期异常：{exc}"[:500],
        )
        raise HTTPException(
            status_code=502,
            detail=i18n.pick(
                "隔离执行过程中出现未预期的错误，已记录日志；请稍后重试，或把发生时间告诉管理员",
                "An unexpected error occurred while isolating the VM. It has been "
                "logged — retry later, or tell an administrator when it happened.",
            ),
        ) from exc
    # 系统信息快照：隔离把机器关掉之后，PVE 侧只剩 ostype 可读，「版本 / 主机名 /
    # 配置」这些现场信息必须先在面板侧留一份（见 app.guest_osinfo）。
    # 放在处置之后是为了不拖慢断网：此时 config 仍然读得到，读不到也不会写坏旧值。
    await guest_osinfo.capture(
        node,
        vmid,
        conn_id=_op_connection(),
        reason="quarantine",
        actor=str(user.get("username") or ""),
    )
    await security.audit(
        request,
        user,
        "vm.quarantine",
        target=f"{node}/{vmid}",
        result="success" if result["ok"] else "failed",
        detail=(result["summary"] + ("；备注：" + payload.note if payload.note else ""))[:500],
    )
    # 登记一笔「这台机器被隔离了」：虚拟机列表的隔离标签、应急响应页的清单都读它。
    # 只有在**真的动过网络或保护**时才登记 —— 用户把两项都取消勾选时，这台机器
    # 并没有被隔离，记进来会让清单凭空多一条。
    applied = any(
        step.get("ok") and step.get("step") in ("network", "protect")
        for step in result.get("steps") or []
    )
    if applied:
        await quarantine_store.record(
            conn_id=_op_connection(),
            node=node,
            vmid=vmid,
            name=result.get("name") or "",
            snapshot=result.get("snapshot") or "",
            interfaces=result.get("interfaces") or [],
            power_action=result.get("power_action") or "none",
            note=payload.note,
            summary=result.get("summary") or "",
            state="ok" if result["ok"] else "partial",
            started_at=result.get("started_at") or 0,
            recorded_by=str(user.get("username") or ""),
        )
    return result


@router.post(
    "/{node}/{vmid}/quarantine/release", dependencies=[Depends(require_vm_access)]
)
async def release_vm(
    node: str,
    vmid: int,
    payload: QuarantineReleaseIn,
    request: Request,
    user: Dict[str, Any] = Depends(ISOLATE),
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, Any]:
    """解除隔离：恢复网卡 → 解除保护 →（可选）开机。"""
    try:
        result = await isolation.release(
            node,
            vmid,
            restore_network=payload.restore_network,
            unprotect=payload.unprotect,
            power_on=payload.power_on,
            note=payload.note,
        )
    except (RuntimeError, ProxmoxError) as exc:
        await security.audit(
            request, user, "vm.quarantine.release", target=f"{node}/{vmid}",
            result="failed", detail=str(exc)[:500],
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— 与隔离同一套兜底
        logger.exception("解除隔离异常：%s/%s", node, vmid)
        await security.audit(
            request, user, "vm.quarantine.release", target=f"{node}/{vmid}",
            result="failed", detail=f"未预期异常：{exc}"[:500],
        )
        raise HTTPException(
            status_code=502,
            detail=i18n.pick(
                "解除隔离过程中出现未预期的错误，已记录日志；请稍后重试，或把发生时间告诉管理员",
                "An unexpected error occurred while releasing the VM. It has been "
                "logged — retry later, or tell an administrator when it happened.",
            ),
        ) from exc
    await security.audit(
        request,
        user,
        "vm.quarantine.release",
        target=f"{node}/{vmid}",
        result="success" if result["ok"] else "failed",
        detail=result["summary"][:500],
    )
    # 全部步骤都成功才算「已经解除隔离」。部分失败（比如网卡恢复了但保护没解开）
    # 时保留登记 —— 那台机器仍然处于半隔离状态，清单里必须还看得见。
    if result["ok"]:
        await quarantine_store.drop(_op_connection(), node, vmid)
    # 解除隔离时的**恢复动作**：重新采集系统信息，并在备注条目缺 os 家族时用采集
    # 到的 ostype 补回去。隔离期间读不到（机器关着、快照上锁）造成的「系统信息丢了」
    # 在这里修复；采集失败则保留快照里的上一个已知值，不做任何清除。
    await guest_osinfo.capture(
        node,
        vmid,
        conn_id=_op_connection(),
        reason="release",
        actor=str(user.get("username") or ""),
        restore_os=bool(result["ok"]),
    )
    return result
