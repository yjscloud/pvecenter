"""任务（UPID）的归属判定：``/api/tasks``、``/api/cluster/tasks`` 与 WebSocket 推送共用。

为什么单独一个模块
------------------
任务列表有三条出口（单连接 HTTP、多连接合并 HTTP、WebSocket 推送），此前只有第一条
做了归属过滤，另外两条把全平台的 ``upid``（含节点名、vmid、发起者身份、起止时间）
原样发给了普通用户。判定逻辑复制三份迟早再次分叉，所以集中在这里
（安全审计 M-1 的收尾）。

三条通道的差别只有「连接怎么来」：

* 单连接：``guestguard.call_connection()``；
* 多连接合并：每条任务自带 ``connection_id``（``cluster._flatten_scoped`` 打的标）；
* WebSocket：与单连接相同。

规则与客户机列表同源
--------------------
* 管理员：不限制（:func:`build_scope` 返回 ``None``）；
* 区域管理员：只放行本区域连接上的任务（``security.region_in_scope``）；
* 其余角色：任务必须指向「自己名下的客户机」。没有归属记录的存量机器、以及
  非客户机级任务（``aptupdate`` / ``ceph`` / 存储任务，UPID 里没有 vmid）一律不返回
  —— 严格隔离，与 ``GET /api/vms`` 口径一致。

节点这一维有一个例外：**克隆 / 迁移任务的 UPID 节点是源节点，归属记的是目标节点**
（见 ``vms._create_from_clone``）。所以任务判定在「节点 + vmid」落空后，还会按
「同连接 + 同 vmid」再试一次（:meth:`TaskScope._allows_vmid`）。同一连接内 vmid
唯一，这一步不扩大可见范围；不放宽的话，跨节点克隆出来的机器对普通用户是永久
「待确认」，而管理员看到的是成功。

历史数据
--------
升级前记录的归属 ref 可能是 ``-:<node>:<vmid>``（当时未指定连接）。这类记录无法
判断属于哪条连接，按「通配」处理（:attr:`TaskScope.legacy`），否则升级后老用户的
任务会凭空消失。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from . import ownership, security, store

logger = logging.getLogger(__name__)

#: 归属 ref 里表示「未指定连接」的前缀（见 ``ownership.vm_ref`` 的 ``or "-"``）。
LEGACY_CONNECTION = "-"


def task_guest(upid: str) -> Optional[Tuple[str, str]]:
    """从 UPID 里解析出 ``(node, vmid)``；非客户机级任务返回 ``None``。

    UPID 形如 ``UPID:node:pid:pstart:starttime:type:id:user@realm:``。只有
    ``qm*``（虚拟机）与 ``vz*``（容器）开头的任务带客户机 id；节点级、存储级、
    集群级任务（``aptupdate``、``ceph`` 等）没有归属可判，一律不对外暴露。
    """
    parts = (upid or "").split(":")
    if len(parts) < 7:
        return None
    if not (parts[5].startswith("qm") or parts[5].startswith("vz")):
        return None
    return parts[1], parts[6]


@dataclass
class TaskScope:
    """一个用户的可见任务范围。``regions`` 非 ``None`` 表示区域管理员。"""

    owned: Dict[str, Set[Tuple[str, str]]] = field(default_factory=dict)
    legacy: Set[Tuple[str, str]] = field(default_factory=set)
    regions: Optional[List[str]] = None
    #: ``connection_id -> region`` 的请求内缓存：``store.connection_region`` 每次都要
    #: 遍历连接清单，逐条任务调用会变成 O(任务数 × 连接数)。
    _region_of: Dict[str, str] = field(default_factory=dict)

    def _region_allows(self, connection_id: str) -> bool:
        if self.regions is None:
            return True
        cid = str(connection_id or "")
        if cid not in self._region_of:
            self._region_of[cid] = store.connection_region(cid)
        # 未分区（region 为空）的连接不归任何区域管理员，与 security.region_in_scope 一致
        region = self._region_of[cid]
        return bool(region) and region in self.regions

    def allows(self, connection_id: str, upid: str) -> bool:
        guest = task_guest(upid)
        if guest is None:
            return False
        if self.allows_guest(connection_id, guest[0], guest[1]):
            return True
        # 克隆（以及迁移）在**源节点**上跑，而归属记的是**目标节点**：跨节点克隆时
        # 这条 UPID 的节点永远对不上归属里的节点，按 (节点, vmid) 精确比对必然落空。
        # 结果是普通用户在批量下发结果页读不到自己刚建好的机器，只能显示「待确认」，
        # 而管理员（范围不受限）看到的是绿色成功 —— 同一件事两种结论。
        #
        # 同一个连接内 vmid 唯一（PVE 的 vmid 是集群级唯一的），所以「同连接 + 同
        # vmid」已经足以指向同一台机器；跨连接仍然严格按 id 隔离，见
        # ``test_owner_sees_only_his_guest``。这里只放宽任务这一路，指标推送
        # （``allows_guest``）保持精确，不去动它的语义。
        return self._allows_vmid(connection_id, guest[1])

    def _allows_vmid(self, connection_id: str, vmid: Any) -> bool:
        """按 vmid 判定（不看节点）：给「任务跑在源节点、归属记在目标节点」兜底。"""
        if self.regions is not None:
            # 区域管理员在上面那次 allows_guest 里已经按连接判过了，走到这里说明
            # 连接不在本区域，不能再放宽
            return False
        wanted = str(vmid or "")
        if not wanted:
            return False
        if not str(connection_id or ""):
            if any(wanted == guest[1] for guest in self.legacy):
                return True
            return any(wanted == guest[1] for refs in self.owned.values() for guest in refs)
        return any(wanted == guest[1] for guest in self.owned.get(str(connection_id), ()))

    def allows_guest(self, connection_id: str, node: str, vmid: Any) -> bool:
        """这台客户机在不在可见范围内。

        与 :meth:`allows` 同源，只是入参不是 UPID —— 指标 WebSocket 推的是
        客户机快照（节点 + VMID），没有 UPID 可解析，但它必须走**同一套**归属
        判定，否则「HTTP 列表收紧了、实时推送还是全量」就成了绕过隔离的后门。

        ``vmid`` 用 ``Any``：调用方手里可能是 int（PVE 原始数据）也可能是 str
        （从归属 ref 里切出来的）。统一按字符串比，避免 ``100 != "100"``。
        """
        cid = str(connection_id or "")
        if not self._region_allows(cid):
            return False
        if self.regions is not None:
            return True
        guest = (str(node or ""), str(vmid or ""))
        if guest in self.legacy:
            return True
        if not cid:
            # 调用方没给出连接（单连接部署 / 未指定作用域）：按「任一连接名下」判定，
            # 与改造前的行为一致，避免老用户的任务凭空消失。
            return any(guest in refs for refs in self.owned.values())
        return guest in self.owned.get(cid, ())

    def filter(self, tasks: List[Dict[str, Any]], connection_id: str = "") -> List[Dict[str, Any]]:
        """按 ``connection_id`` 过滤任务列表；``connection_id`` 为空时取任务自带的。"""
        out: List[Dict[str, Any]] = []
        for item in tasks or []:
            cid = connection_id or str(item.get("connection_id") or "")
            if self.allows(cid, str(item.get("upid") or "")):
                out.append(item)
        return out


async def build_scope(user: Dict[str, Any]) -> Optional[TaskScope]:
    """构造用户的可见范围；管理员返回 ``None``（不限制）。"""
    if security.is_admin(user):
        return None
    if security.is_region_admin(user):
        return TaskScope(regions=list(security.user_regions(user)))

    owner = security.visible_owner(user)
    if owner is None:
        return None
    owned: Dict[str, Set[Tuple[str, str]]] = {}
    legacy: Set[Tuple[str, str]] = set()
    for kind in ownership.GUEST_KINDS:
        for ref in await ownership.list_owned(kind, owner):
            parts = str(ref).rsplit(":", 2)
            if len(parts) != 3:
                continue
            cid, node, vmid = parts
            if cid in (LEGACY_CONNECTION, ""):
                legacy.add((node, vmid))
            else:
                owned.setdefault(cid, set()).add((node, vmid))
    return TaskScope(owned=owned, legacy=legacy)
