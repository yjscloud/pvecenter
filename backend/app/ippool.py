"""IP 地址池：读取、占用判定与**自动分配**。

池是一段「可分配给客户机的静态地址」，存在 ``settings.ip_pools`` 里（见
``routers/ip_pools.py`` 的接口层）。这里收的是两条路径共用的业务规则：

* 哪些池算「落在同一个二层网络里」（网桥 / VLAN / 节点 / PVE 连接）；
* 哪些地址已经被占用 —— **扫描 PVE 配置**加上**面板的分配登记**；
* 从池里挑一个空闲地址，并在分配后立刻把它标成已占用。

为什么需要「分配登记」而不只是扫配置：扫描只认**已经写进 PVE 的静态 IP**。
创建请求里地址刚挑出来、机器还没建出来（或者建机任务还在跑）的那一小段时间
里，配置里什么都没有，紧跟着的第二个创建请求会把同一个地址再分一次。登记表
``ip_allocation`` 就是这段窗口里的占位；等配置里真的出现了这个地址，扫描结果
接管占用判定，登记行随即被清掉（见 :func:`_merge_reservations`）。这样：

* 分配后立刻可见为「已占用」（池列表不会再把它当空闲地址发出去）；
* 机器删除 / 网卡地址改掉之后，扫描不再看到它，地址自动回到池里；
* 创建失败时由创建方显式释放（:func:`release_guest`），最多也只占一个保鲜期。

**没有匹配的池时保持 ``ip=dhcp``**：这是为了兼容没有配置地址池的存量环境 ——
那种环境下本来就只有外部 DHCP 可用。但只要存在**匹配的池**，DHCP 语义就变成
「从池里自动取一个」，池满会直接报错，**不会**悄悄退回外部 DHCP。

**占用扫描带快照缓存**：算「哪些地址已被占用」需要对每台客户机各发一个 PVE
配置请求（集群 N 台就是 N 次往返），而创建向导、批量部署、池管理页、改网卡
下拉都共用同一个 ``GET /api/ip-pools``。不做缓存时，打开一次表单就要扫一遍
全集群，PVE 慢的时候「空闲地址回填」会等上好几秒。现在扫描结果按
:data:`SCAN_TTL_SECONDS` 保鲜：窗口内直接复用；过期后先返回旧快照、后台重扫
（stale-while-revalidate）；超过 :data:`SCAN_MAX_AGE_SECONDS` 才同步重扫。
分配登记表**每次实时读库**，所以缓存不会让「刚分配出去的地址」重新变成空闲。
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import database, store
from .pve import parallel

logger = logging.getLogger(__name__)

POOLS_KEY = "ip_pools"
#: 单个池最多返回的空闲地址数，避免超大网段拖垮前端。
MAX_FREE = 512

#: 归属用户名单里的这一项表示「全部用户」：任何登录用户都能看到并使用这个池。
#:
#: 用显式的 ``"*"`` 而不是「名单为空」—— 空名单那一档是**公共池（仅管理员）**，
#: 两者必须能区分开，否则升级之后老数据里那些管理员自用的池会对所有用户可见。
ALL_USERS = "*"

#: 分配登记在「PVE 里还看不到这台机器」时的保鲜期（秒）。
#:
#: 正常创建只需要几秒到几十秒；超过这个时长还没在 PVE 里看到对应配置，说明
#: 创建失败 / 面板中途重启了，登记行不该永久占着地址。
RESERVATION_TTL_SECONDS = 15 * 60.0

#: 自动分配用的串行锁：同一进程内的并发创建不能同时看到同一份空闲表。
_ALLOCATE_LOCK = asyncio.Lock()

_ALLOC_SCHEMA = """
CREATE TABLE IF NOT EXISTS ip_allocation (
    ip            VARCHAR(64) NOT NULL,
    connection_id VARCHAR(64) NOT NULL DEFAULT '',
    node          VARCHAR(64) NOT NULL DEFAULT '',
    vmid          INT         NOT NULL DEFAULT 0,
    pool_id       VARCHAR(64) NOT NULL DEFAULT '',
    allocated_at  BIGINT      NOT NULL DEFAULT 0,
    PRIMARY KEY (ip),
    KEY idx_ip_allocation_guest (connection_id, node, vmid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


# ------------------------------------------------------------------ 异常
class PoolExhausted(Exception):
    """有匹配的池，但池里没有可用地址。

    调用方应把它转成 409：这是「资源不够」，不是参数写错了；更重要的是**不能**
    静默退回外部 DHCP —— 用户选 DHCP 就是希望由面板从池里发地址。
    """


# ------------------------------------------------------------------ 归属
def clean_owners(raw: Any) -> List[str]:
    """规范化归属用户名单：去空白、去空项、去重，并把 ``"*"`` 收敛成它自己。

    容错比"严格"重要：这一串是用户手选出来的名字，且保存走的是「整份池覆盖写」，
    读的时候一旦因为一个多余空格匹配不上，用户侧看到的就是"池不见了"。
    """
    if isinstance(raw, str):
        # 单个用户名（或空串）也当作名单：旧数据就是这种形状
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for name in raw:
        text = str(name or "").strip()
        if text and text not in out:
            out.append(text)
    # 勾了「全部用户」就不再需要具体名单：名单在这里没有额外信息，留着反而让人
    # 以为以后还要逐个维护
    return [ALL_USERS] if ALL_USERS in out else out


def owners_of(pool: Dict[str, Any]) -> List[str]:
    """读出池的归属用户名单。

    老数据只有单值 ``owner``（用户名或空串），在这里包成名单 —— 全链路上只有这
    一处需要认识旧字段，其余代码一律只看 ``owners``。

    ``owners`` 为空名单时同样回落到 ``owner``：保存路径是 ``owners or owner``
    （见 ``routers/ip_pools.save_ip_pools``），读的时候也照这个优先级来，否则一行
    同时带着 ``owners: []`` 和 ``owner: "alice"`` 的数据会被读成公共池 —— 归属在
    管理员毫不知情的情况下被撤销。新代码写下的行不会出现这种组合（保存时会把
    ``owner`` 丢掉），所以这条回落只会帮到迁移到一半 / 手工改过的数据。
    """
    owners = pool.get("owners")
    if isinstance(owners, list) and owners:
        return clean_owners(owners)
    return clean_owners(pool.get("owner"))


def visible_to(pool: Dict[str, Any], username: str) -> bool:
    """普通用户能不能看到这个池：名单里有他本人，或者名单是「全部用户」。

    空名单（公共池）对普通用户不可见 —— 与单归属时代一致。
    """
    owners = owners_of(pool)
    return ALL_USERS in owners or username in owners


def legacy_pool_id(pool: Dict[str, Any]) -> str:
    """给没有 ``id`` 的老数据生成一个稳定 id。

    保存改成「按 id 合并」之后，没有 id 的行必须能认出「还是同一个池」。按内容
    生成而不是按下标：池的顺序会变（页面上可增删），下标一变就会被当成两个池，
    合并时旧的那个会被当成「没提交」而非「已更新」。
    """
    raw = "|".join(
        str(pool.get(k) or "") for k in ("bridge", "subnet", "start", "end", "vlan")
    )
    return "pool-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------ 读取池
async def load_pools() -> List[Dict[str, Any]]:
    raw = await store.get_setting(POOLS_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        # 老数据没有 id：读的时候就补上（不落盘，下次保存时自然带回去）
        if not str(row.get("id") or "").strip():
            row = {**row, "id": legacy_pool_id(row)}
        out.append(row)
    return out


def merge_for_scope(
    submitted: List[Dict[str, Any]], existing: List[Dict[str, Any]], scope: str
) -> List[Dict[str, Any]]:
    """普通用户的保存：按 id 合并，只允许改「分配给自己的」那几行。

    * 提交的行必须已存在且原本就分给自己（新增一律拒绝 —— 池是管理员分配的资源）；
    * ``owners`` 必须与旧值完全一致（能用，不能转赠）；
    * 没提交的池：自己可见的视为删除，**看不见的原样保留** —— 否则一次保存就会把
      别人的池整片覆盖掉。

    管理员不走这里（整表覆盖，行为与改造前一致）。
    """
    from fastapi import HTTPException

    by_id = {str(p.get("id") or ""): p for p in existing}
    visible = {pid for pid, p in by_id.items() if visible_to(p, scope)}
    seen: Set[str] = set()
    merged: List[Dict[str, Any]] = []
    for item in submitted:
        pid = str(item.get("id") or "")
        old = by_id.get(pid)
        if old is None:
            raise HTTPException(
                status_code=403, detail="只能修改分配给自己的地址池，不能新建"
            )
        if pid not in visible:
            raise HTTPException(status_code=403, detail="该地址池不属于当前用户")
        if owners_of(old) != item["owners"]:
            raise HTTPException(status_code=403, detail="不能修改地址池的归属用户")
        seen.add(pid)
        merged.append(item)
    merged.extend(
        p for pid, p in by_id.items() if pid not in seen and pid not in visible
    )
    return merged


# ------------------------------------------------------------------ 占用
def _ip_only(value: str) -> str:
    return str(value or "").split("/")[0].strip()


def config_ips(cfg: Dict[str, Any]) -> List[str]:
    """从一份 VM / 容器配置里取出所有静态 IP。

    虚拟机看 cloud-init 的 ``ipconfigN``，容器看 ``netN`` 的 ``ip=`` —— 两者是同一套
    ``key=a,b;ip=<地址>`` 语法，所以一个函数就够。``ip=dhcp`` 这类不是具体地址，
    直接跳过（拿不到地址就没法把它从池里剔掉）。
    """
    ips: List[str] = []
    for key, val in (cfg or {}).items():
        if not isinstance(val, str):
            continue
        if not (key.startswith("ipconfig") or key.startswith("net")):
            continue
        for seg in val.split(","):
            seg = seg.strip()
            if not seg.startswith("ip="):
                continue
            ip = _ip_only(seg[3:])
            if not ip:
                continue
            if ip.lower() in ("dhcp", "auto", "manual", "none"):
                continue
            if ip.startswith("127."):
                continue
            ips.append(ip)
    return ips


def _conn(conn_id: Any) -> str:
    """连接标识归一：空 / 缺省一律视作空串（池是全局的，不按连接分）。"""
    return str(conn_id or "")


def reservation_key(conn_id: Any, node: Any, vmid: Any) -> Optional[Tuple[str, str, int]]:
    """分配登记表的「客户机」键；节点 / VMID 缺失时返回 ``None``。"""
    if not node or vmid in (None, ""):
        return None
    try:
        number = int(vmid)
    except (TypeError, ValueError):
        return None
    return (_conn(conn_id), str(node), number)


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_flag(name: str, default: bool = True) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


#: 扫描缓存总开关。出问题时设 ``IP_POOL_SCAN_CACHE=0`` 重启即回到「每次请求都
#: 现场扫全集群」的旧行为，不必回滚代码 —— 见交付说明里的回滚方案。
SCAN_CACHE_ENABLED = _env_flag("IP_POOL_SCAN_CACHE", True)

#: 扫描结果的保鲜期（秒）。窗口内重复请求直接命中内存快照，不再逐台问 PVE。
#:
#: 为什么是 10 秒：池的占用集合变化很慢（只在建机 / 改网卡 / 删机时动），而创建
#: 向导、批量部署、详情的池下拉共用同一个 ``/api/ip-pools``。10 秒足以把同一次
#: 操作里的多次请求合并成一次扫描，又不会让「刚删掉的机器」在界面上赖太久。
SCAN_TTL_SECONDS = _env_float("IP_POOL_SCAN_TTL_SECONDS", 10.0)

#: 快照的最长可用年龄（秒）。超过这个年龄就**同步**重扫一次，不再拿旧数据
#: 去分配地址 —— 这是数据一致性的兜底，防止 PVE 长时间不可达时一直用陈旧快照。
SCAN_MAX_AGE_SECONDS = max(
    _env_float("IP_POOL_SCAN_MAX_AGE_SECONDS", 60.0), SCAN_TTL_SECONDS
)

#: 单台客户机配置的读取上限（秒）。这个值不能再放大：一台没装 / 没跑
#: Guest Agent 的机器不会拖慢这里（这里只读配置），但 PVE 返回慢时会拖住整批。
SCAN_GUEST_TIMEOUT_SECONDS = 3.0

#: 同时读取多少台客户机的配置。12 是「一次几秒内扫完一个中等集群」与
#: 「别把 PVE 的 API worker 打满」之间的折中。
SCAN_CONCURRENCY = 12


@dataclass
class _ScanSnapshot:
    """一次集群配置扫描的结果（带时间戳，供缓存复用）。"""

    at: float
    used: Dict[str, str]
    observed: Dict[Tuple[str, str, int], Set[str]]
    scanned: Set[str]
    #: 这次扫描本身花了多少毫秒（只用于观测 / 响应头，不参与判定）
    duration_ms: float = 0.0


#: 进程内快照。多 worker 部署时每个 worker 各有一份，但真正保证不重复分配的
#: 是 ``ip_allocation`` 登记表（跨进程共享），快照只负责「别每次都全量扫」。
_scan_cache: Optional[_ScanSnapshot] = None
_scan_lock = asyncio.Lock()
_scan_seq = 0
_refresh_task: Optional[asyncio.Task] = None
_warm_task: Optional[asyncio.Task] = None


async def _scan_configs(
    previous: Optional[_ScanSnapshot] = None,
) -> Tuple[Dict[str, str], Dict[Tuple[str, str, int], Set[str]], Set[str], bool]:
    """扫描**所有虚拟机与容器**的静态 IP。

    返回四样东西：

    * ``used``：``{ip: 资源标识}``（值与改造前一致，只用于提示「谁占的」）；
    * ``observed``：``{(连接, 节点, VMID): {ip, ...}}``，用来判断一条分配登记是否
      已经落进了配置；
    * ``scanned_connections``：本轮**成功读到**的连接 id。读失败的连接不能据此
      判断「机器不存在」，否则会把登记行误删；
    * ``ok``：本轮是否至少读到一条连接（或压根没有配连接）。``False`` 表示整轮
      扫描失败，调用方应沿用上一份快照，而不是把「什么都没扫到」当成「没有任何
      地址被占用」—— 后者会让池把全部地址都发出去。

    判定依据是**配置**而不是运行状态：运行中、关机、待机、刚创建还没起来的机器，
    配置里的地址都照样算数 —— 只有它被删除、或者那个 IP 被改掉了，这个地址才会
    重新回到池子里。这就是「回收了才能被重新分配」。

    四个容易漏的地方：

    * **只扫当前连接**：地址池是**全局**的（存在 settings 里，不按 PVE 分），所以
      占用判定必须遍历**每一条**已保存的连接。曾经这里用的是 ``get_client()`` ——
      只看当前那一条，于是另一台 PVE 上的机器占着的地址会被当成空闲再分配出去一次。
      面板是「多台 PVE 合并展示」的，池却只有一个，不对齐就会重复分配。
    * **容器**：LXC 的静态 IP 写在 ``netN`` 的 ``ip=`` 里，语法与 VM 的 ipconfigN
      相同。只扫 qemu 的话，容器占着的地址会被当成空闲再分配一次 —— 而两个设备
      拿到同一个 IP 的后果是互相抢网络。
    * **模板**：模板在 ``cluster_resources`` 里也是 ``type=qemu``，而且通常带着
      cloud-init 配置。不排除的话，一个池里会被模板的配置占掉一批地址；模板往往
      长期存在，那批地址等于永久废了。
    * **回收站**：PVE 回收站里的机器不在 cluster resources 中，它配置的地址因此会
      回到池子里。这符合直觉 —— 那台机器已经不在用了。

    ``previous`` 是上一份快照。单台配置 / 单条连接读失败时，从它里面把对应条目
    原样搬过来 —— 临时故障不能让一个仍在使用的地址变回空闲。
    """
    # 延迟 import：pve 会连带一串模块，放在这里避免启动期循环依赖
    from .pve import collection_targets

    used: Dict[str, str] = {}
    observed: Dict[Tuple[str, str, int], Set[str]] = {}
    scanned: Set[str] = set()
    # 用 collection_targets 而不是 all_connection_clients：后者只看「已保存的连接」，
    # 而老库 / 单连接部署下可能一条都没存（走的是兜底客户端）。那种情况下扫描会
    # 得到空集合，池里的地址会被当成全部空闲 —— 一分配就撞车。
    # 面板还没配 PVE 时 get_client 会抛 503：池的配置页仍应能打开，按「扫不到」
    # 处理（此时也不可能有机器占地址）。
    try:
        targets = collection_targets()
    except Exception:  # noqa: BLE001
        logger.debug("无法枚举 PVE 连接，跳过 IP 占用扫描", exc_info=True)
        return used, observed, scanned, False
    if not targets:
        # 没有配置任何连接：空集合就是正确答案（不是失败），别让调用方一直重试
        return used, observed, scanned, True

    for profile, client in targets:
        conn_id = str((profile or {}).get("id") or "")
        label = str((profile or {}).get("name") or conn_id or "default")
        try:
            resources = await client.cluster_resources("vm")
        except Exception:  # noqa: BLE001 - 一条连接读不到不影响其它主机
            _carry_over_connection(previous, conn_id, label, used, observed, scanned)
            continue
        scanned.add(conn_id)

        guests: List[Any] = []
        for item in resources or []:
            kind = str(item.get("type") or "")
            if kind not in ("qemu", "lxc"):
                continue
            # 模板不占用地址：它是用来克隆的源，不是在网络里跑的一台机器
            if int(item.get("template") or 0) == 1:
                continue
            node = item.get("node")
            vmid = item.get("vmid")
            if not node or not vmid:
                continue
            guests.append((kind, str(node), int(vmid)))

        async def one(target: Any, _client: Any = client) -> Optional[List[str]]:
            kind, node, vmid = target
            fetch = _client.qemu_config if kind == "qemu" else _client.lxc_config
            try:
                cfg = await asyncio.wait_for(
                    fetch(node, vmid), timeout=SCAN_GUEST_TIMEOUT_SECONDS
                )
            except Exception:  # noqa: BLE001 - 单台读不到不影响整体
                # 读不到 ≠ 没有地址：返回 None，下面从上一份快照里搬旧值，
                # 免得把「暂时读不到」误判成「配置里已经没有这个地址」。
                return None
            return config_ips(cfg)

        results = await parallel((one(t) for t in guests), limit=SCAN_CONCURRENCY)
        for target, res in zip(guests, results):
            _kind, node, vmid = target
            key = (conn_id, node, vmid)
            if not isinstance(res, list):
                # 单台读取失败：沿用上一份快照里这台机器的地址（如果有）
                if previous is not None and key in previous.observed:
                    res = sorted(previous.observed[key])
                else:
                    continue
            observed[key] = set(res)
            for ip in res:
                # 值记的是「谁占的」。同一个地址被多台机器占用时把来源都留下来 ——
                # 那是**已经发生的冲突**，值得让人知道，而不是被 setdefault 悄悄吞掉。
                if ip in used:
                    used[ip] = f"{used[ip]},{label}"
                else:
                    used[ip] = label
    return used, observed, scanned, bool(scanned)


def _carry_over_connection(
    previous: Optional[_ScanSnapshot],
    conn_id: str,
    label: str,
    used: Dict[str, str],
    observed: Dict[Tuple[str, str, int], Set[str]],
    scanned: Set[str],
) -> None:
    """一条连接整体读失败时，把上一份快照里属于它的条目原样搬过来。"""
    if previous is None or conn_id not in previous.scanned:
        return
    for key, ips in previous.observed.items():
        if key[0] != conn_id:
            continue
        observed[key] = set(ips)
        for ip in ips:
            used.setdefault(ip, previous.used.get(ip, label))
    scanned.add(conn_id)


async def _refresh_scan() -> _ScanSnapshot:
    """同步重扫一次（单飞：并发调用只会真正扫一次）。"""
    global _scan_cache, _scan_seq

    seen = _scan_seq
    async with _scan_lock:
        # 等锁期间别的请求已经扫完了：直接用那份，别重复扫
        if _scan_seq > seen and _scan_cache is not None:
            return _scan_cache

        started = time.monotonic()
        previous = _scan_cache
        used, observed, scanned, ok = await _scan_configs(previous)
        if not ok:
            logger.warning("IP 占用扫描失败（PVE 不可达？），沿用上一次结果")
            if previous is not None:
                return previous
            # 第一次就失败：返回空快照但**不写缓存**，下次请求会再试
            return _ScanSnapshot(started, {}, {}, set())

        at = time.monotonic()
        snap = _ScanSnapshot(at, used, observed, scanned, (at - started) * 1000)
        _scan_cache = snap
        _scan_seq += 1
        logger.debug(
            "IP 占用扫描完成：%d 台客户机，用时 %.0f ms",
            len(observed),
            snap.duration_ms,
        )
        return snap


def _schedule_refresh() -> None:
    """后台刷新（不阻塞当前请求）。同一时刻只允许一个刷新任务。"""
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 不在事件循环里（比如同步测试）
        return
    _refresh_task = loop.create_task(_background_refresh())


async def _background_refresh() -> None:
    try:
        await _refresh_scan()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 后台任务失败不能冒泡到请求
        logger.warning("后台刷新 IP 占用快照失败，保留旧快照", exc_info=True)


async def _scan_snapshot(max_age: float = SCAN_MAX_AGE_SECONDS) -> _ScanSnapshot:
    """取一份扫描快照。

    * 新鲜（``SCAN_TTL_SECONDS`` 内）：直接返回；
    * 过期但未超过 ``max_age``：**先返回旧的**，同时后台重扫（stale-while-revalidate）；
    * 超过 ``max_age`` 或还没有快照：同步重扫，宁可慢也不能拿太旧的数据去分配。
    """
    if not SCAN_CACHE_ENABLED:
        return await _refresh_scan()
    snap = _scan_cache
    now = time.monotonic()
    if snap is not None:
        age = now - snap.at
        if age < SCAN_TTL_SECONDS:
            return snap
        if age <= max_age:
            _schedule_refresh()
            return snap
    return await _refresh_scan()


def clear_cache() -> None:
    """丢掉进程内快照（测试 / 手动排障用）。"""
    global _scan_cache, _scan_seq
    _scan_cache = None
    _scan_seq = 0


def _forget_cached_guest(conn_id: Any, node: Any, vmid: Any) -> None:
    """从缓存快照里摘掉一台客户机（删除后地址应立即回到空闲表）。

    只删这台机器自己的地址：同一个 IP 若还被别的机器配置着（已经发生的冲突），
    必须继续算占用。分配登记表那边由 :func:`release_guest` 单独清。
    """
    global _scan_cache
    snap = _scan_cache
    if snap is None:
        return
    key = reservation_key(conn_id, node, vmid)
    if key is None:
        return
    ips = snap.observed.pop(key, None)
    if not ips:
        return
    still_used: Set[str] = set()
    for other in snap.observed.values():
        still_used.update(other)
    for ip in ips:
        if ip not in still_used:
            snap.used.pop(ip, None)


async def warm() -> None:
    """启动预热：有池时先把占用快照扫一遍，让第一次打开创建向导就能拿到结果。"""
    if not SCAN_CACHE_ENABLED:
        return
    try:
        if not await load_pools():
            return
        await _refresh_scan()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 预热失败不影响面板启动
        logger.warning("预热 IP 地址池快照失败，稍后由首次请求重试", exc_info=True)


def start_warmup() -> None:
    """在后台跑一次 :func:`warm`（不阻塞启动流程）。"""
    global _warm_task
    if _warm_task is not None and not _warm_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _warm_task = loop.create_task(warm())


async def stop() -> None:
    """取消预热 / 后台刷新任务（进程退出时调用）。"""
    global _refresh_task, _warm_task
    tasks = [task for task in (_warm_task, _refresh_task) if task is not None]
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _warm_task = None
    _refresh_task = None


async def init_table() -> None:
    """建分配登记表（应用启动时调用）。"""
    async with database.connect() as db:
        await db.executescript(_ALLOC_SCHEMA)
        await db.commit()


async def _load_reservations() -> List[Dict[str, Any]]:
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "SELECT ip, connection_id, node, vmid, pool_id, allocated_at "
                "FROM ip_allocation"
            )
            rows = await cursor.fetchall()
    except Exception:  # noqa: BLE001 - 登记表读不到不该让池列表 / 创建失败
        logger.debug("读取 ip_allocation 失败", exc_info=True)
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(dict(row))
        else:
            out.append({key: row[key] for key in row.keys()})
    return out


async def _delete_reservations(ips: Iterable[str]) -> None:
    values = [str(ip) for ip in ips if ip]
    if not values:
        return
    try:
        marks = ", ".join("?" for _ in values)
        async with database.connect() as db:
            await db.execute(
                f"DELETE FROM ip_allocation WHERE ip IN ({marks})", tuple(values)
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.debug("清理 ip_allocation 失败", exc_info=True)


async def _reserve(
    allocation: "Allocation", *, connection_id: str, node: str, vmid: int
) -> bool:
    """登记一个地址的占用。``False`` = 这个地址已经被别人抢先登记了。"""
    try:
        async with database.connect() as db:
            await db.execute(
                "INSERT INTO ip_allocation "
                "(ip, connection_id, node, vmid, pool_id, allocated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    allocation.ip,
                    _conn(connection_id),
                    str(node),
                    int(vmid),
                    allocation.pool_id,
                    int(time.time()),
                ),
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001
        # 唯一键冲突 = 另一个请求 / 进程抢先登记了同一个地址，必须让调用方重试；
        # 其它错误（表还没建好、连接抖动）按「登记没成功但分配照旧」处理，
        # 否则数据库一个抖动就会让所有下发失败，而地址仍会写进 PVE 配置、
        # 下一轮扫描照样能算占用。
        if "Duplicate" in exc.__class__.__name__ or "Integrity" in exc.__class__.__name__:
            logger.info("IP %s 已被抢先登记", allocation.ip)
            return False
        logger.warning("登记 IP %s 失败（继续分配）", allocation.ip, exc_info=True)
    return True


async def _merge_reservations(
    used: Dict[str, str],
    observed: Dict[Tuple[str, str, int], Set[str]],
    scanned: Set[str],
) -> None:
    """把分配登记并入占用表，并顺手做一次回收。

    三种情形：

    1. **配置里已经出现这个地址** —— 扫描已经算它占用了，登记行完成使命，删掉；
    2. **机器在、但配置里没有这个地址** —— 可能是地址刚被改掉，也可能是克隆体
       还带着模板的旧配置（覆盖要等克隆任务跑完）。保鲜期内继续算占用，过期才
       释放；
    3. **PVE 里还看不到这台机器** —— 创建进行中，保鲜期内继续算占用；过期则释放
       （创建失败 / 面板中途重启留下的残行）。

    连接本轮没读到（``scanned`` 里没有）时不判断「机器不存在」—— 那可能只是
    PVE 暂时不可达，误删会把一台仍然在用的地址发出去。
    """
    rows = await _load_reservations()
    if not rows:
        return
    now = time.time()
    stale: List[str] = []
    for row in rows:
        ip = str(row.get("ip") or "")
        if not ip:
            continue
        conn_id = _conn(row.get("connection_id"))
        key = reservation_key(conn_id, row.get("node"), row.get("vmid"))
        if conn_id not in scanned:
            used.setdefault(ip, "panel")
            continue
        try:
            allocated_at = int(row.get("allocated_at") or 0)
        except (TypeError, ValueError):
            allocated_at = 0
        fresh = now - allocated_at < RESERVATION_TTL_SECONDS
        if key is not None and key in observed:
            if ip in observed[key]:
                # 扫描已经接管；登记行不必再留（配置里就是它，占用不会丢）
                used.setdefault(ip, "panel")
                stale.append(ip)
            elif fresh:
                # 配置里暂时还没有这个地址：**不能立刻释放** —— 克隆体可能先带着
                # 模板的旧配置出现在 cluster resources 里，覆盖配置要等克隆任务
                # 跑完（大磁盘要几分钟）。这段窗口里登记行还必须继续占着地址。
                used.setdefault(ip, "panel")
            else:
                stale.append(ip)
            continue
        if fresh:
            used.setdefault(ip, "panel")
        else:
            stale.append(ip)
    await _delete_reservations(stale)


async def collect_used_meta(
    max_age: float = SCAN_MAX_AGE_SECONDS,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """``({ip: 占用者}, 扫描元信息)``。

    快照只缓存「集群里已经写好的静态地址」；分配登记每次都重新读库，所以刚分配
    出去、配置还没落盘的地址在缓存窗口内依然算占用。

    元信息给接口层打诊断响应头用：``cache_hit`` 为 True 表示这次请求没有现场扫
    集群（命中了快照或走了 stale-while-revalidate），``scan_ms`` 是那份快照当初
    扫描花的时间。只看这两个值就能判断「慢」是慢在扫描还是别处。
    """
    seq_before = _scan_seq
    snap = await _scan_snapshot(max_age=max_age)
    used = dict(snap.used)
    await _merge_reservations(used, snap.observed, snap.scanned)
    meta = {
        "cache_hit": _scan_seq == seq_before,
        "scan_ms": round(snap.duration_ms, 1),
        "age_ms": round((time.monotonic() - snap.at) * 1000, 1),
        "guests": len(snap.observed),
    }
    return used, meta


async def collect_used(max_age: float = SCAN_MAX_AGE_SECONDS) -> Dict[str, str]:
    """``{ip: 占用者}``：PVE 配置扫描（带快照） + 面板分配登记（每次实时）。

    ``max_age`` 决定「快照最多有多旧还可以用」：池列表展示可以放宽到
    ``SCAN_MAX_AGE_SECONDS``，而 :func:`allocate` 用更严格的 ``SCAN_TTL_SECONDS``
    —— 发地址这件事宁可多扫一次，也不要拿十几秒前的空闲表去撞一个刚被外部
    改上去的静态地址。
    """
    used, _meta = await collect_used_meta(max_age=max_age)
    return used


async def release_guest(connection_id: Any, node: Any, vmid: Any) -> None:
    """释放一台客户机登记过（可能）的地址。

    删除机器时调用；创建失败时也调用 —— 否则那条登记要等保鲜期结束才松开，
    池里会白少一个地址。同时把缓存快照里这台机器的条目摘掉，让删掉的地址
    在下一次请求里就能重新分配，而不必等快照过期。
    """
    try:
        async with database.connect() as db:
            await db.execute(
                "DELETE FROM ip_allocation "
                "WHERE connection_id = ? AND node = ? AND vmid = ?",
                (_conn(connection_id), str(node or ""), int(vmid)),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 - 释放失败不该阻断删除本身
        logger.debug("释放 %s/%s 的 IP 登记失败", node, vmid, exc_info=True)
    _forget_cached_guest(connection_id, node, vmid)


# ------------------------------------------------------------------ 分配
@dataclass(frozen=True)
class PoolScope:
    """一张网卡要落在哪个二层网络里。"""

    bridge: str = ""
    vlan: Optional[int] = None


@dataclass(frozen=True)
class Allocation:
    ip: str
    cidr: str
    gateway: str
    pool_id: str
    pool_name: str


def pool_free(pool: Dict[str, Any], used: Dict[str, str]) -> List[str]:
    """列出池内空闲地址（排除网络号、广播地址与网关）。"""
    subnet = str(pool.get("subnet") or "").strip()
    start = str(pool.get("start") or "").strip()
    end = str(pool.get("end") or "").strip()
    try:
        net = ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return []
    if net.version != 4:
        return []

    try:
        lo = ipaddress.ip_address(start) if start else net.network_address + 1
        hi = ipaddress.ip_address(end) if end else net.broadcast_address - 1
    except ValueError:
        return []
    if int(lo) > int(hi):
        return []

    gateway = _ip_only(str(pool.get("gateway") or ""))
    free: List[str] = []
    cur = int(lo)
    last = int(hi)
    while cur <= last and len(free) < MAX_FREE:
        addr = ipaddress.ip_address(cur)
        s = str(addr)
        if (
            addr != net.network_address
            and addr != net.broadcast_address
            and s not in used
            and s != gateway
        ):
            free.append(s)
        cur += 1
    return free


def pool_fits(
    pool: Dict[str, Any],
    scope: PoolScope,
    *,
    node: str,
    connection_id: str,
) -> bool:
    """这个池能不能用在这张网卡 / 这台节点 / 这条连接上。

    判定规则与前端 ``utils/ippool.poolFits`` 保持同一套「空 = 不限」：

    * 池写了网桥就必须与网卡一致；
    * 池写了 VLAN 就必须与网卡一致 —— 自动分配**不会**替用户改网卡的 tag，
      把带 tag 的池地址填到未打标签的网卡上会直接不通；
    * 池的 ``nodes`` / ``connections`` 非空时必须包含目标。
    """
    bridge = str(scope.bridge or "").strip()
    pool_bridge = str(pool.get("bridge") or "").strip()
    if bridge and pool_bridge and pool_bridge != bridge:
        return False

    pool_vlan = pool.get("vlan")
    if pool_vlan is not None:
        # 老数据 / 手工改过的行里 tag 可能是字符串，统一成整数再比
        try:
            pool_vlan = int(pool_vlan)
        except (TypeError, ValueError):
            return False
        if pool_vlan != scope.vlan:
            return False

    conn = str(connection_id or "").strip()
    conns = pool.get("connections") or []
    if conn and isinstance(conns, list) and conns and conn not in conns:
        return False

    node_name = str(node or "").strip()
    nodes = pool.get("nodes") or []
    if node_name and isinstance(nodes, list) and nodes and node_name not in nodes:
        return False

    return True


def _rank(pool: Dict[str, Any]) -> Tuple[int, int]:
    """越具体的池越优先：先网桥明确的，再 VLAN 明确的。"""
    return (
        0 if str(pool.get("bridge") or "").strip() else 1,
        0 if pool.get("vlan") is not None else 1,
    )


def _candidates(
    pools: Sequence[Dict[str, Any]],
    scope: PoolScope,
    *,
    node: str,
    connection_id: str,
    owner_scope: Optional[str],
) -> List[Dict[str, Any]]:
    out = []
    for pool in pools:
        if owner_scope is not None and not visible_to(pool, owner_scope):
            continue
        if pool_fits(pool, scope, node=node, connection_id=connection_id):
            out.append(pool)
    out.sort(key=_rank)
    return out


async def allocate(
    scopes: Sequence[PoolScope],
    *,
    owner_scope: Optional[str],
    node: str,
    connection_id: str,
    vmid: int,
    guest_type: str,
) -> List[Optional[Allocation]]:
    """为每张网卡分配一个池内空闲地址（返回与 ``scopes`` 等长的列表）。

    * 列表里出现 ``None``：这张网卡没有匹配的池，调用方保持 ``ip=dhcp``；
    * 抛 :class:`PoolExhausted`：**有**匹配的池但都满了。此时不会登记任何一个
      地址，调用方可以安全地放弃本次创建。

    分配与登记在同一把进程内锁里完成：两笔并发创建不会拿到同一个地址。
    """
    empty: List[Optional[Allocation]] = [None for _ in scopes]
    pools = await load_pools()
    if not pools or not scopes:
        return empty

    async with _ALLOCATE_LOCK:
        # 发地址比展示更在意一致性：快照超过保鲜期就同步重扫，不复用过期结果
        used = await collect_used(max_age=SCAN_TTL_SECONDS)
        picks: List[Optional[Allocation]] = []
        exhausted: List[str] = []
        for scope in scopes:
            candidates = _candidates(
                pools,
                scope,
                node=node,
                connection_id=connection_id,
                owner_scope=owner_scope,
            )
            chosen: Optional[Allocation] = None
            for pool in candidates:
                free = pool_free(pool, used)
                if not free:
                    continue
                ip = free[0]
                try:
                    prefix = str(
                        ipaddress.ip_network(
                            str(pool.get("subnet") or ""), strict=False
                        ).prefixlen
                    )
                except ValueError:
                    prefix = "24"
                chosen = Allocation(
                    ip=ip,
                    cidr=f"{ip}/{prefix}",
                    gateway=_ip_only(str(pool.get("gateway") or "")),
                    pool_id=str(pool.get("id") or ""),
                    pool_name=str(pool.get("name") or ""),
                )
                # 同一批里的多张网卡不能拿到同一个地址
                used[ip] = f"pending:{guest_type}/{node}/{vmid}"
                break
            if chosen is None and candidates:
                exhausted.append(
                    "、".join(str(p.get("name") or p.get("id") or "未命名") for p in candidates)
                )
            picks.append(chosen)

        if exhausted:
            raise PoolExhausted(
                "IP 地址池没有可用地址（"
                + "；".join(dict.fromkeys(exhausted))
                + "）。请释放地址或调整地址池范围后重试"
            )

        reserved: List[Allocation] = []
        for pick in picks:
            if pick is None:
                continue
            ok = await _reserve(
                pick, connection_id=connection_id, node=node, vmid=vmid
            )
            if not ok:
                # 另一个进程刚好抢先登记了同一个地址：把这次已登记的都退掉，
                # 让调用方重试（重试会重新扫描，拿到的是下一个空闲地址）。
                for item in reserved:
                    await _delete_reservations([item.ip])
                raise PoolExhausted(
                    f"地址 {pick.ip} 刚被占用，请重试"
                )
            reserved.append(pick)
        return picks
