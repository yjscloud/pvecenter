"""客户机迁移（同一个 PVE 集群内换节点）的两条硬性约束。

1. **目标节点必须同集群**
   ----------------------
   PVE 的迁移只能在**同一个集群**里换节点 —— 跨两套独立的 PVE 没有共享的
   ``pmxcfs`` / 集群网络，机器搬不过去。面板同时接多套 PVE 时，``/nodes`` 是
   合并返回的（每条连接各带一个 ``connection_id``），界面若直接把它当真值，
   迁移下拉里就会混进**另一套集群**的节点名；选中之后要么 PVE 回一句看不懂的
   英文错误，要么（两套集群有同名节点时）看起来"成功了"而实际一点没动。

   界面按 ``connection_id`` 收窄（见前端 ``utils/clusterNodes``），这里再在服务端
   判一次：路由直接收 ``target_node``，第三方脚本 / 旧前端同样绕不开。

2. **归属必须跟着机器走**
   --------------------
   归属 ref 是 ``<连接>:<节点>:<vmid>``，节点变了整条记录就指向一个不存在的位置
   （症状见 :func:`app.ownership.move_guest`）。迁移请求被 PVE 接受之后立刻把记录
   搬到目标节点 —— ``wait_for_task`` 那种"等任务跑完再搬"在这里不适用：整盘迁移
   可能几十分钟，把 HTTP 请求挂在那里只会让前端超时。

   搬账失败**不能**把迁移报成失败：迁移已经在 PVE 上跑起来了，这里只是面板侧的
   一本账，下一轮读取还有身份兜底（见 :func:`app.ownership.get_owner_for`）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from . import i18n, ownership
from .pve import ProxmoxError, all_connection_clients, connection_label

logger = logging.getLogger(__name__)


async def _node_names(client: Any) -> set:
    """这条连接（= 这套 PVE 集群）的节点名集合。

    读失败直接抛出：调用方马上要对同一个客户端发迁移请求，连节点清单都读不到的
    话迁移也不会成功，此时的报错比"跳过校验、让 PVE 自己报"更可读。
    """
    nodes = await client.nodes() or []
    return {
        str(item.get("node") or "").strip()
        for item in nodes
        if str(item.get("node") or "").strip()
    }


async def _owner_cluster_of(node: str) -> Optional[str]:
    """目标节点如果属于**另一条**连接，返回那条连接的展示名。

    只用于把错误信息说清楚（「它是另一套 PVE 的节点」比「不在当前集群里」有用
    得多）。探测不到就返回 ``None``，不影响拒绝本身。
    """
    for profile, client in all_connection_clients():
        try:
            if node in await _node_names(client):
                return connection_label(profile) or str(profile.get("id") or "")
        except Exception:  # noqa: BLE001 - 单条连接读不到不影响结论
            logger.debug("迁移目标校验：读取连接节点清单失败", exc_info=True)
            continue
    return None


async def ensure_target_node(
    client: Any, source_node: str, target_node: str
) -> str:
    """校验迁移目标节点；返回规整后的节点名，不合法时抛 :class:`ProxmoxError`。

    ``client`` 必须是**源机器所在连接**的客户端 —— 校验的判据是「目标节点出现
    在这套集群的节点清单里」。
    """
    source = str(source_node or "").strip()
    target = str(target_node or "").strip()
    if not target:
        raise ProxmoxError("迁移需要指定目标节点", 400)
    if not source:
        raise ProxmoxError("无法确定源节点，迁移已取消", 400)
    if target == source:
        raise ProxmoxError(f"目标节点 {target} 与当前节点相同，无法迁移", 400)

    try:
        names = await _node_names(client)
    except Exception:  # noqa: BLE001 - 见下
        # 读不到节点清单（凭据没有 Sys.Audit、PVE 正忙…）时**放行**，交给 PVE 自己判：
        # 这道校验是为了把「另一套集群的节点」这类明显错误挡在面板侧，不能反过来
        # 把原本能成功的迁移（只带 VM.* 权限的令牌）全部挡掉。
        logger.warning(
            "迁移目标校验：无法读取节点清单，跳过集群归属校验（%s）",
            source,
            exc_info=True,
        )
        return target

    if target in names:
        return target

    other = await _owner_cluster_of(target)
    if other:
        raise ProxmoxError(
            f"目标节点 {target} 属于另一套 PVE（{other}），"
            f"不能接收本集群（{source} 所在集群）上的客户机",
            400,
        )
    raise ProxmoxError(
        f"目标节点 {target} 不在当前 PVE 集群里（源节点 {source}），无法迁移",
        400,
    )


def offline_blocked_detail(vmid: Any) -> str:
    """运行中的虚拟机不能用停机方式迁移时给用户的提示。

    PVE 对 ``online=0`` 的运行中虚拟机只回一句 ``can't migrate running VM without
    --online``：既没说清楚是谁在运行，也没说该怎么办。单机端点与批量迁移共用这一
    份文案，免得两处口径不一致。
    """
    return i18n.pick(
        f"虚拟机 {vmid} 正在运行，不能以停机方式迁移。"
        "请勾选「在线迁移」（需要共享存储；本地盘需 PVE 支持本地磁盘迁移），"
        "或先关机再迁移",
        f"VM {vmid} is running, so it cannot be migrated offline. Enable "
        "“live migration” (requires shared storage; local disks need PVE local-disk "
        "migration support), or shut the VM down first.",
    )


def qemu_migrate_detail(exc: ProxmoxError, vmid: Any = None) -> Optional[str]:
    """把 QEMU 迁移的 PVE 英文原文翻成可操作的提示；不认识就返回 None（原样抛）。

    与 :func:`app.routers.lxc._lxc_migrate_detail` 同一思路：只翻**已知**的几句，
    其余保持原样 —— 猜错原因比给英文原文更糟。
    """
    text = str(exc.message or "").lower()
    if "without --online" in text:
        # 前置校验漏掉的兜底（状态刚变、或者直接调 PVE 的旧脚本）
        return offline_blocked_detail(vmid if vmid is not None else "该虚拟机")
    if "local disk" in text or "with-local-disks" in text:
        return i18n.pick(
            "虚拟机磁盘在本地存储上，在线迁移不会自动搬运本地盘。"
            "请先把磁盘迁到共享存储，或启用 PVE 的本地磁盘迁移（with-local-disks）",
            "The VM disks are on local storage and live migration cannot carry them. "
            "Move the disks to shared storage first, or enable PVE local-disk "
            "migration (with-local-disks).",
        )
    if "not available on node" in text or "no such storage" in text:
        return i18n.pick(
            "目标节点上没有虚拟机使用的存储，请先在该节点补齐存储配置",
            "The target node is missing a storage used by this VM. Add it on that "
            "node first.",
        )
    return None


async def carry_ownership(
    connection_id: str,
    source_node: str,
    target_node: str,
    vmid: Any,
    *,
    notify: bool = True,
) -> Dict[str, int]:
    """把归属记录跟到目标节点；失败只记日志（迁移已经提交，不能因此报错）。

    ``notify`` 由批量调用方传 ``False``：一批机器共用一次缓存作废更划算
    （与 :func:`app.ownership.transfer_guest` 的 ``notify`` 同一取舍）。
    """
    try:
        return await ownership.move_guest(
            connection_id,
            source_node,
            target_node,
            vmid,
            notify=notify,
        )
    except Exception:  # noqa: BLE001 - 见模块说明
        logger.warning(
            "迁移后搬动归属记录失败（%s/%s → %s）",
            source_node,
            vmid,
            target_node,
            exc_info=True,
        )
        return {}
