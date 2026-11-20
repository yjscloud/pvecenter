"""列表页的客户机系统类型（ostype）索引。

## 为什么需要单独一份

列表页的「操作系统」列要靠客户机的 ostype：容器的 ostype 直接就是发行版
（``debian`` / ``ubuntu`` / ``alpine`` / ``centos``），虚拟机的是 ``l26`` /
``win11`` 这类大类（至少能分开 Windows 与 Linux）。没有它，一列图标只能退回
「按机器名猜」—— 名字叫 ``1111`` / ``rest`` / ``lxc-01`` 的机器永远认不出来。

## ostype 到底在哪

实测（PVE 8.4 / 9.2，两台）：``/cluster/resources`` **不带** ostype，
``/nodes/{node}/qemu`` 与 ``/nodes/{node}/lxc`` **也不带** —— 节点级列表里
一个 ostype 字段都没有，只有 ``vmid`` / ``name`` / 资源用量。ostype 只存在于
每台机器自己的 ``config`` 里。

所以早先「一次节点请求换整节点 ostype」的设想不成立（那时还配了个只有 mock
才会返回 ostype 的测试，把问题盖住了）。现在只能：节点列表拿 vmid 清单，再
**逐台读 config** 补齐。

## 成本与缓存

逐台读 config 是唯一的路，所以缓存必须够长：ostype 只在**重装 / 改配置**时
变化，按 :data:`ONE_TTL`（6 小时）缓存单台结果，进程内一张 dict 就够（不占
:mod:`app.pvecache` 的 512 条额度 —— 一个节点几百台机器会把它挤空）。

节点级结果仍走 :mod:`app.pvecache`（:data:`OSTYPE_TTL`）：一份 ``{vmid: ostype}``
在 2 分钟内被列表轮询反复要，没必要每次重拼。冷启动时整节点读一遍 config，
之后每轮只补「还没缓存过的新机器」。

冷启动一轮最多读 :data:`FETCH_BUDGET` 台（按「未缓存」优先），超出部分留到
下一轮 —— 与 :mod:`app.guest_created` 的创建时间预算同一个取舍：一个附加字段
不该把几百台机器的一次请求全压在一条响应里。

读失败（节点离线、权限不足、机器刚被删）不缓存，下一轮再试；确实没有
ostype 的（PVE 7 及更早建的）缓存空串，避免每轮都去问。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from . import pvecache
from .pve import parallel

#: 节点级结果（``{vmid: ostype}``）的缓存时长：列表 10 秒一轮，2 分钟足够削峰，
#: 又不会让新建的机器迟迟没有图标。
OSTYPE_TTL = 120.0

#: 单台 ostype 的缓存时长。重装 / 改配置是低频操作，6 小时后重问一次兜住
#: 「在面板之外改过」的情况。
ONE_TTL = 6 * 3600.0

#: 单节点一轮最多读多少台 config（未缓存的优先）。超出的留到下一轮。
FETCH_BUDGET = 200

#: 读 config 的并发上限。
FETCH_CONCURRENCY = 8

#: ``连接|节点|类型|VMID`` -> (过期时刻, ostype)。ostype 为空串表示「确认没有」。
_cache: Dict[str, Tuple[float, str]] = {}


def clear() -> None:
    """清空进程内缓存（测试与「连接被改动」时用）。"""
    _cache.clear()


def _one_key(cid: str, node: str, kind: str, vmid: int) -> str:
    return f"{cid}|{node}|{kind}|{vmid}"


def forget(cid: str, node: str, kind: str, vmid: int) -> None:
    """忘掉一台机器的 ostype，并让它所在节点的汇总缓存一起失效。

    重装换掉系统盘之后必须调用：否则列表最长 6 小时继续按旧系统画图标（节点级
    汇总还有 2 分钟的 :data:`OSTYPE_TTL`，所以要连它一并清）。
    """
    _cache.pop(_one_key(cid, node, kind, vmid), None)
    pvecache.invalidate(f"guests:ostype:{cid}:{node}:{kind}")


async def _read_ostype(client: Any, node: str, kind: str, vmid: int) -> Optional[str]:
    """读一台机器的 config 取 ostype。

    返回 ``None`` 表示**这次没读到**（节点离线、权限、机器刚被删）—— 不写缓存，
    下一轮还会再试。返回空串表示读到了、但 config 里确实没有 ostype（PVE 7 及
    更早建的机器），这一种要缓存住，否则每轮都会为它多打一次请求。
    """
    try:
        if kind == "lxc":
            config = await client.lxc_config(node, vmid)
        else:
            config = await client.qemu_config(node, vmid)
    except Exception:  # noqa: BLE001 - 单台失败只影响它自己
        return None
    return str((config or {}).get("ostype") or "").strip()


def _sweep(cid: str, node: str, kind: str, alive: set) -> None:
    """清掉「本轮列表里已经不存在的 vmid」的缓存。

    VMID 会被回收：一台容器删掉后同号的新容器可能是另一种系统，留着旧值会把
    图标贴到新机器上。清理范围只限这一次查询的 ``连接|节点|类型``。
    """
    prefix = f"{cid}|{node}|{kind}|"
    for key in [k for k in _cache if k.startswith(prefix)]:
        tail = key[len(prefix) :]
        if not tail.isdigit() or int(tail) not in alive:
            _cache.pop(key, None)


async def _load(client: Any, cid: str, node: str, kind: str) -> Dict[int, str]:
    """拼一份节点上的 ``{vmid: ostype}``（列表 + 逐台 config 补齐）。"""
    items: List[Dict[str, Any]] = await client.guest_list(node, kind) or []

    out: Dict[int, str] = {}
    alive: set = set()
    cold: List[int] = []
    now = time.time()
    for item in items:
        try:
            vmid = int(item.get("vmid") or 0)
        except (TypeError, ValueError):
            continue
        if not vmid:
            continue
        alive.add(vmid)

        # 将来 PVE 若把 ostype 放进列表，这里继续用现成的，不必多读一次 config
        inline = str(item.get("ostype") or "").strip()
        if inline:
            out[vmid] = inline
            continue

        hit = _cache.get(_one_key(cid, node, kind, vmid))
        if hit is not None and now < hit[0]:
            if hit[1]:
                out[vmid] = hit[1]
            continue
        cold.append(vmid)

    if cold:
        batch = cold[:FETCH_BUDGET]
        values = await parallel(
            (_read_ostype(client, node, kind, vmid) for vmid in batch),
            limit=FETCH_CONCURRENCY,
        )
        for vmid, value in zip(batch, values):
            # 非字符串 = 这次没读到（``None`` 或 parallel 用异常对象占位）：不缓存，
            # 下一轮再试。空串是「读到了但没有 ostype」，那种要缓存。
            if not isinstance(value, str):
                continue
            _cache[_one_key(cid, node, kind, vmid)] = (now + ONE_TTL, value)
            if value:
                out[vmid] = value

    _sweep(cid, node, kind, alive)
    return out


async def index(client: Any, cid: str, node: str, kind: str) -> Dict[int, str]:
    """节点上的 ``{vmid: ostype}``，带两层缓存（见模块说明）。"""
    try:
        value = await pvecache.cached(
            f"guests:ostype:{cid}:{node}:{kind}",
            OSTYPE_TTL,
            lambda: _load(client, cid, node, kind),
        )
    except Exception:  # noqa: BLE001 - 图标是附加字段，读不到就让名称兜底
        return {}
    return value if isinstance(value, dict) else {}
