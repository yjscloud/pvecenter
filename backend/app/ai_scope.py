"""AI 助手的「目标主机」准入判定。

为什么单独一层
--------------
助手对目标主机的处置能力比别的页面强：它会登录主机执行命令、能开一个交互式
终端，还能通过 Guest Agent 进入虚拟机内部。目标选错一台，后果不是「看错了
一页」，而是「在另一台机器上跑了命令」。因此候选清单不能按「能列出来」来
算，只能按「这一轮确实做得到」来算。

五条件为**与**关系，缺一即排除：
    1. 是虚拟机（QEMU，且登记了 node/vmid）；
    2. 已被安全纳管（在「SSH 安全 → 受管主机」里，且当前用户看得见）；
    3. 处于开机运行状态；
    4. cloud-init 配置完整（有 cloud-init 盘，且设置了登录用户）；
    5. Guest Agent 应答。

第 2 条由调用方（``baseline.targets`` + ``hostscope``）保证，本模块只判其余
四条。之所以把判定收在这里，是为了让「候选清单」与「发起排查时的准入」共用
同一份规则 —— 只在页面上过滤会留下「手工构造 host_id 仍能发起」的缝。

失败一律 fail-closed：PVE 读不到、权限不足、探测超时，都按「不满足」处理。
这不是保守过度，而是因为把这些机器放进来之后，第一次命令就会失败，用户看到
的是「AI 坏了」，而不是「这台机器现在不适合排查」。

性能：慢在哪、为什么长成这样
----------------------------
这台清单是助手页的首屏，每台机器最多三次 PVE 往返（状态 / 配置 / agent ping）。
几十台时真正拖时间的从来不是 PVE 算得慢，而是**往返次数 × 轮数**：

* **状态整批问，不逐台问** —— 60 台逐台 ``status/current`` 就是 60 次往返，
  而 ``/cluster/resources`` 一次就能拿到所有虚拟机的状态（还顺带完成探活）。
  停机 / 容器的机器因此在**零次额外往返**下判掉；在跑的机器只剩 config ∥ ping
  一轮往返。这笔账在几十台上是「每台一轮」，在几百台上是数量级的差别。
* **串行的三连问** —— 原先是 status → config → ping 一条直线，一台机器三次
  RTT。状态先问（它最便宜，而且停机 / 容器 / 不存在的机器到这里就能结束），
  拿到 running 之后 config 与 ping 并行 —— 一台跑通的机器由 3 次往返降到 2 次，
  最坏（全部超时）由 9 秒降到 6 秒。
* **每条 SQL 一次往返** —— 共享缓存原来是「每台机器一次 SELECT」，60 台就是
  60 次借连接 + 60 次往返；前端每 2 秒轮询一次，等于每秒 30 条只为读缓存。
  现在整批一次 ``IN (...)`` 查完，写回也用 ``executemany`` 合并成一轮。
* **一条 PVE 挂了，几十台机器陪着等** —— 同一连接上的机器先做一次
  ``/version`` 探活：连不上就整组判「读不到」，不再逐台等三个超时。
* **多用户 / 多 worker 叠加** —— 单次扫描的并发按台数自适应（下限 12、上限
  32）：固定 12 时 60 台要排 5 轮，每轮一个 RTT；放宽到 32 只要 2 轮。另加
  一层按连接计的并发闸门，把「同一台 PVE 上同时在飞的请求」压住，其余排队。
  后台补探与前台扫描共用一个闸门。
* **待定永远待定** —— 探测抛异常时会话不会留下任何结论，前端于是每 2 秒问
  一次、永远 pending。现在任何异常都落一条「读不到」的结论（带 TTL），
  pending 一定会收敛。

参数都在 ``Settings.ai_scan_*``（并发、超时、重试、缓存时长、显式刷新预算），
默认值就是上面这套折中；小规模环境可以调到串行，几百台的集群调到 16~24。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import database, i18n, pve
from .config import settings
from .pve import ProxmoxError

logger = logging.getLogger(__name__)

#: 单次判定里，三个上游探测各自的超时（秒）。status/config 是普通 GET；agent
#: ping 会等客户机里的服务回应。三者同档：再久就不该让整个清单陪它等。
#: 实际取值见 :func:`tuning`（``Settings.ai_scan_timeout`` 覆盖这里）。
STATUS_TIMEOUT = 3.0
CONFIG_TIMEOUT = 3.0
AGENT_TIMEOUT = 3.0

#: 同时探测的机器数（下限）。清单可能有几十台，逐个串行会把页面拖成十几秒；
#: 全放出去又会瞬间在 PVE 上打出一串请求。12 是「够快」与「不添乱」之间的
#: 折中，而实际并发按台数自适应放宽到 :data:`MAX_FANOUT`（见 :func:`fanout`）。
MAX_CONCURRENCY = 12

#: 自适应并发的上限。台数一多，固定 12 就意味着「排 5 轮」，每轮一个 RTT；
#: 放宽到 32 之后 60 台只要 2 轮，整份清单能在 1 秒内收敛。批量取状态
#: （每条连接一次 ``/cluster/resources``）之后，每台只剩 config ∥ ping 一轮
#: 往返，32 并发对 PVE 也就是 64 个只读请求 —— 与它自己的 Web UI 同量级。
MAX_FANOUT = 32

#: 同一条 PVE 连接上同时在飞的探测数。多用户同时打开页面时，各自的扫描并发
#: 会叠加；这一层按连接兜底，避免把同一台 PVE 打满（打满的表现是「全都变慢」，
#: 而不是「某一台失败」，最难查）。
CONN_CONCURRENCY = 16

#: 判定结果缓存多久。运行状态与 agent 在线状态会变，但没必要每次翻页都重探
#: 一遍；发起排查时还会再执行一次（``resolve_host``），所以这里只影响候选清单
#: 的新鲜度。太短等于没缓存，太长会让刚开机的机器半小时不出现。
CACHE_TTL = 30.0

#: 瞬时错误（超时 / 5xx / 网络）重试几次。404 与 401/403 不重试：再试一百次
#: 也是同一个答案，白白多等一个超时。
RETRIES = 1

#: 连接探活结果缓存多久（秒）。比判定缓存短得多：它只是「这条 PVE 现在通不通」，
#: 而 PVE 恢复的时间尺度是秒级。
HEALTH_TTL = 5.0

# 原因码：页面据此点名到具体主机。能够点名是因为调用方已按用户可见范围过滤，
# 明细里的机器本来就是用户能在体检页看到的，不存在额外泄露。
REASON_NOT_VM = "not_vm"
REASON_STOPPED = "stopped"
REASON_CLOUDINIT = "cloudinit"
REASON_AGENT = "agent"
REASON_UNREACHABLE = "unreachable"

REASONS = (
    REASON_NOT_VM,
    REASON_STOPPED,
    REASON_CLOUDINIT,
    REASON_AGENT,
    REASON_UNREACHABLE,
)

_REASON_LABELS: Dict[str, Tuple[str, str]] = {
    REASON_NOT_VM: (
        "没有关联到一台虚拟机（可能是容器、模板，或手工添加的主机）",
        "not linked to a VM (container, template, or a manually added host)",
    ),
    REASON_STOPPED: (
        "虚拟机没有处于开机运行状态",
        "the VM is not running",
    ),
    REASON_CLOUDINIT: (
        "cloud-init 配置不完整（缺少 cloud-init 盘或登录用户）",
        "cloud-init is not fully configured (missing cloud-init drive or login user)",
    ),
    REASON_AGENT: (
        "Guest Agent 没有响应",
        "the Guest Agent is not responding",
    ),
    REASON_UNREACHABLE: (
        "读不到这台虚拟机的状态（PVE 不可达或凭据权限不足）",
        "its VM state could not be read (PVE unreachable or insufficient permission)",
    ),
}

# (conn_id, node, vmid) -> (写入时间, 是否合格, 原因码)
_CACHE: Dict[Tuple[str, str, int], Tuple[float, bool, str]] = {}
# 同一 key 上并发进来的请求只做一次真实探测（L1 的去重）
_PROBING: Dict[Tuple[str, str, int], asyncio.Lock] = {}
# conn_id -> (探活时间, 是否连通)——同一条 PVE 挂了就别逐台等超时
_HEALTH: Dict[str, Tuple[float, bool]] = {}
# (conn_id, 上限) -> 闸门：按连接限制同时在飞的探测数
_CONN_SEMS: Dict[Tuple[str, int], asyncio.Semaphore] = {}
# 上一轮扫描的自述（前端 / 排障用）：探了几台、多久、有几条 PVE 连不上
_LAST_STATS: Dict[str, Any] = {}

#: 跨 worker 共享的探测结果。进程内缓存命中率会按 worker 数摊薄 —— 4 个 worker
#: 就是 4 次探测，每次又要 status / config / agent 三个 PVE 调用；几十台机器的
#: 候选清单首屏因此能慢十几秒。库里这张小表让所有 worker 30 秒内只探一次。
_SHARED_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_target_probes (
    cache_key VARCHAR(191) NOT NULL,
    ok        TINYINT      NOT NULL DEFAULT 0,
    reason    VARCHAR(32)  NOT NULL DEFAULT '',
    checked   BIGINT       NOT NULL DEFAULT 0,
    PRIMARY KEY (cache_key),
    KEY idx_ai_target_probes_checked (checked)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

# 表是在 lifespan 里建的。建好之前（单测 / 迁移没跑起来的旧部署）不碰库：
# 探测本身是只读加速，不该因为「缓存表还不存在」就给每次判定添一次失败查询。
_SHARED_READY = False

#: 一次 IN (...) 里最多塞多少个 key。太小浪费往返，太大超过驱动/服务器的语句
#: 长度限制（MySQL 默认 max_allowed_packet 之下，200 个 key 仍是安全的）。
_IN_CHUNK = 200


@dataclass(frozen=True)
class Tuning:
    """一次扫描的调参快照。放在 dataclass 里而不是散落的全局变量，
    是为了让「这一轮用的到底是哪套参数」在被调用方拿到手时就固定下来。"""

    concurrency: int = MAX_CONCURRENCY
    max_concurrency: int = MAX_FANOUT
    conn_concurrency: int = CONN_CONCURRENCY
    timeout: float = STATUS_TIMEOUT
    retries: int = RETRIES
    ttl: float = CACHE_TTL
    budget: float = 0.0  # 显式刷新最多等多久；0 = 不限


def _num(name: str, default: Any, *, cast: Any, floor: Any) -> Any:
    """从 settings 里取一个数；缺键 / 脏值 / 越界都退回默认值。

    ``getattr`` + 兜底是必要的：settings 是启动时从环境读的，运维手改
    ``.env`` 写成 ``"0"``、``"abc"`` 都可能；扫描链路上的参数不该因此 500。

    注意 ``0`` 是合法值（重试 0 次、预算 0 = 不限），不能走 ``or default``：
    那会把「明确关掉重试」悄悄变回默认的 1 次。
    """
    raw = getattr(settings, name, None)
    if raw is None or raw == "":
        raw = default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        value = cast(default)
    return max(floor, value) if floor is not None else value


def tuning() -> Tuning:
    """读一次当前参数。每次扫描现读：改完配置重启即生效，测试也能即时覆盖。"""
    return Tuning(
        concurrency=_num("ai_scan_concurrency", MAX_CONCURRENCY, cast=int, floor=1),
        max_concurrency=_num(
            "ai_scan_max_concurrency", MAX_FANOUT, cast=int, floor=1
        ),
        conn_concurrency=_num(
            "ai_scan_conn_concurrency", CONN_CONCURRENCY, cast=int, floor=1
        ),
        timeout=_num("ai_scan_timeout", STATUS_TIMEOUT, cast=float, floor=0.5),
        retries=_num("ai_scan_retries", RETRIES, cast=int, floor=0),
        # 下限放到 0.1 秒：调试 / 测试时「关掉缓存」是常规操作，而 0 本身
        # 会让每台机器都重新探测，等于把并发闸门直接怼在 PVE 上
        ttl=_num("ai_scan_cache_ttl", CACHE_TTL, cast=float, floor=0.1),
        budget=_num("ai_scan_budget", 0.0, cast=float, floor=0.0),
    )


def fanout(tune: Tuning, total: int) -> int:
    """这一轮实际用的并发数：按台数自适应，落在 ``[1, 台数]`` 与上限之内。

    固定并发在小规模上是浪费（12 个位置空着 10 个也快不了），在大规模上则是
    纯排队：60 台 ÷ 12 = 5 轮，每轮一个 RTT。所以这里取
    ``min(台数, max(下限, 上限))`` —— 台数少就并台数那么多，台数多就顶到
    上限。上限设成与下限相等即退回「固定并发」（见 ``ai_scan_max_concurrency``）。
    """
    if total <= 0:
        return 1
    ceiling = max(tune.concurrency, tune.max_concurrency)
    return max(1, min(total, ceiling))


async def init_table() -> None:
    global _SHARED_READY
    try:
        async with database.connect() as db:
            await db.execute(_SHARED_SCHEMA)
            await db.commit()
        _SHARED_READY = True
    except Exception:  # noqa: BLE001 - 缓存表建不出来就退回进程内缓存
        logger.warning("AI 目标探测共享缓存初始化失败，退回进程内缓存", exc_info=True)


def _shared_key(key: Tuple[str, str, int]) -> str:
    return f"{key[0]}|{key[1]}|{key[2]}"


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def _load_shared_many(
    keys: Sequence[Tuple[str, str, int]], ttl: float
) -> Dict[Tuple[str, str, int], Tuple[bool, str]]:
    """一次查询读回一批共享结论。

    原来是「每台机器一条 SELECT」：60 台 = 60 次借连接 + 60 次往返，而前端每
    2 秒就要问一次。整批 ``IN (...)`` 之后是 1 条，轮询的开销从「随台数线性」
    变成常数。读不到 / 库有问题都返回空，让调用方退回真实探测。
    """
    if not keys or not _SHARED_READY:
        return {}
    by_string = {_shared_key(key): key for key in keys}
    found: Dict[Tuple[str, str, int], Tuple[bool, str]] = {}
    now = time.time()
    try:
        async with database.connect() as db:
            for chunk in _chunks(list(by_string), _IN_CHUNK):
                placeholders = ",".join("?" for _ in chunk)
                cursor = await db.execute(
                    "SELECT cache_key, ok, reason, checked FROM ai_target_probes"
                    f" WHERE cache_key IN ({placeholders})",
                    tuple(chunk),
                )
                for row in await cursor.fetchall():
                    data = dict(row)
                    key = by_string.get(str(data.get("cache_key") or ""))
                    if key is None:
                        continue
                    if now - float(data.get("checked") or 0) < ttl:
                        found[key] = (
                            bool(data.get("ok")),
                            str(data.get("reason") or ""),
                        )
    except Exception:  # noqa: BLE001 - 缓存是加速手段，不该拖垮候选清单
        return {}
    return found


async def _save_shared_many(
    items: Sequence[Tuple[Tuple[str, str, int], Tuple[bool, str]]]
) -> None:
    """一次往返写回一批结论（``executemany`` 会合并成多值 INSERT）。"""
    if not items or not _SHARED_READY:
        return
    now = int(time.time())
    try:
        async with database.connect() as db:
            await db.executemany(
                "INSERT INTO ai_target_probes (cache_key, ok, reason, checked)"
                " VALUES (?,?,?,?) ON DUPLICATE KEY UPDATE"
                " ok=VALUES(ok), reason=VALUES(reason), checked=VALUES(checked)",
                [
                    (_shared_key(key), 1 if ok else 0, reason, now)
                    for key, (ok, reason) in items
                ],
            )
            await db.commit()
    except Exception:  # noqa: BLE001 - 写失败只影响下次命中率
        pass


async def _load_shared(key: Tuple[str, str, int]) -> Optional[Tuple[bool, str]]:
    """单台版本的共享缓存读取（直接 ``check_host`` 的路径用）。"""
    found = await _load_shared_many([key], tuning().ttl)
    return found.get(key)


async def _save_shared(key: Tuple[str, str, int], result: Tuple[bool, str]) -> None:
    await _save_shared_many([(key, result)])


def reset_cache() -> None:
    """清空判定缓存。测试与「刚改过电源状态」的显式刷新用得上。"""
    _CACHE.clear()
    _WARMING.clear()
    _HEALTH.clear()
    _CONN_SEMS.clear()
    _PROBING.clear()
    _LAST_STATS.clear()


def describe(reason: str) -> str:
    """把原因码翻成一句能直接给用户看的话。"""
    zh, en = _REASON_LABELS.get(reason, _REASON_LABELS[REASON_UNREACHABLE])
    return i18n.pick(zh, en)


def last_stats() -> Dict[str, Any]:
    """上一轮扫描的自述：探了几台、多久、几条 PVE 连不上。

    给路由层原样带进响应（``scan`` 字段），页面据此显示进度，排障时也能一眼
    看出是「机器多」还是「PVE 慢」—— 这两种慢的处理方式完全不同。
    """
    return dict(_LAST_STATS)


def _cloudinit_ready(config: Any) -> bool:
    """cloud-init 是否「配得完整」。

    只看两件事：有没有 cloud-init 盘、有没有配置登录身份。前者证明这台机器
    确实挂了 config drive（否则写 ``ciuser`` 只是往配置里塞一个没人读的键），
    后者证明 user-data 不是空的。登录身份认三种写法（``ciuser`` / ``sshkeys``
    / ``cipassword``）：手工纳管的主机可能只用密钥或只用口令，那仍然是完整的
    cloud-init 配置，不能因为没写 ``ciuser`` 就判成「没配」。

    注意这里判的是「配置完整」，不是「本次开机已经跑完」。后者的权威状态在
    客户机内（``cloud-init status``），每次列清单都进去跑一遍代价太大，而且
    agent 已经在线说明客户机至少能执行命令。真正要确认时，由具体动作自己
    在客户机内验证。
    """
    if not isinstance(config, dict):
        return False
    has_drive = any("cloudinit" in str(value).lower() for value in config.values())
    has_login = any(
        str(config.get(key) or "").strip()
        for key in ("ciuser", "sshkeys", "cipassword")
    )
    return has_drive and has_login


# ---------------------------------------------------------------- 重试
#: 值得再试一次的上游状态码。404（这台不是虚拟机）与 401/403（权限不足）是
#: **确定性的答案**，重试只会让每台被排除的机器多等一个超时。
_TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    status = int(getattr(exc, "pve_status", 0) or 0)
    return status in _TRANSIENT_STATUS


async def _call(
    factory: Any,
    *,
    timeout: float,
    retries: int,
    retry_timeout: bool = True,
) -> Any:
    """带超时 + 有限重试地执行一次 PVE 调用。

    ``retry_timeout=False`` 用于 Guest Agent 探活：agent 不应答是**客户机内的
    事实**（没装 / 没起 / 卡住），再等一个 3 秒也还是不应答，重试只是把每台
    没装 agent 的机器从 3 秒拖成 6 秒。PVE 侧的瞬时故障（5xx / 网络）仍然重试。
    """
    attempt = 0
    while True:
        try:
            return await asyncio.wait_for(factory(), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 是否需要重试由下面判断
            if (
                attempt >= retries
                or not _is_transient(exc)
                or (not retry_timeout and isinstance(exc, (asyncio.TimeoutError, TimeoutError)))
            ):
                raise
            attempt += 1
            # 抖动重试：一批机器同时失败时，别让它们在同一个瞬间一起回来
            await asyncio.sleep(0.15 * attempt + random.random() * 0.1)


def _is_connection_failure(exc: BaseException) -> bool:
    """这次失败能否归因到「整条 PVE 连接不可用」。

    只有「没有拿到 PVE 的应答」才算：超时、连不上、TLS / 代理错误（``_call``
    会把它们统一包成 502/503/504）。PVE 返回 401/403/500 说明**连接是通的**，
    只是这次请求被权限或上游问题拒了 —— 把它标成「连接读不到」正是线上那句
    「PVE 明明正常，AI 页却提示 1 条连接读不到」的来源。
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    status = int(getattr(exc, "pve_status", 0) or 0)
    return status in (502, 503, 504)


def _conn_sem(conn_id: str, limit: int) -> asyncio.Semaphore:
    """按连接取的并发闸门（进程内）。

    与「单次扫描并发」是两层，缺一都不够：前者限的是「这一次扫描有多凶」，
    后者限的是「这条 PVE 上同时在飞多少个」—— 三个人同时打开页面时，前者
    各自为政，只有后者能兜住。
    """
    key = (conn_id, limit)
    sem = _CONN_SEMS.get(key)
    if sem is None:
        sem = asyncio.Semaphore(limit)
        _CONN_SEMS[key] = sem
    return sem


async def _connection_alive(
    conn_id: str, client: Any, tune: Tuning
) -> bool:
    """这条 PVE 现在通不通（带几秒缓存）。

    一条连接挂了，它上面的机器如果逐台去试，就是「每台 3 次超时 × N 台」——
    20 台机器能把这一个请求按在地上两分钟，而这 20 台的结果完全一样。先花
    一次 ``/version`` 探活，能让「PVE 挂了」从 O(N × timeout) 变成 O(timeout)。
    """
    if not hasattr(client, "version"):
        # 老客户端 / 测试替身：没有探活接口就跳过，别把它当成「连不上」
        return True
    now = time.monotonic()
    hit = _HEALTH.get(conn_id)
    if hit and now - hit[0] < HEALTH_TTL:
        return hit[1]
    try:
        await _call(
            lambda: client.version(), timeout=tune.timeout, retries=tune.retries
        )
        alive = True
    except ProxmoxError as exc:
        # 401/403 是**令牌权限**问题，不是连接躺下：PVE 已经应答了。把它当成
        # 「连接不可用」会误报整条 PVE 读不到（实际是令牌读不了 /version）。
        status = int(getattr(exc, "pve_status", 0) or 0)
        alive = status in (401, 403)
        if alive:
            logger.info(
                "连接 %s 的令牌无权探活（%s），按可达处理，交给逐台探测判断",
                conn_id or "default",
                status,
            )
    except Exception:  # noqa: BLE001 - 超时 / 连不上 / TLS 失败才算连接不可用
        alive = False
    _HEALTH[conn_id] = (now, alive)
    if not alive:
        logger.info("AI 目标探测：连接 %s 不可达，本轮跳过其上的主机", conn_id or "default")
    return alive


async def _check_uncached(
    host: Dict[str, Any], tune: Tuning, known_status: Optional[str] = None
) -> Tuple[bool, str]:
    """真正去问 PVE。调用方负责缓存。

    ``known_status="running"`` 表示批量取状态时已经确认它在跑（见
    :func:`_batch_statuses`）：那就跳过逐个问状态的那次往返，直接问
    cloud-init 与 agent —— 每台机器由 2 次往返降到 1 次，几十台就是几十个 RTT。
    """
    try:
        vmid = int(host.get("vmid"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False, REASON_NOT_VM
    node = str(host.get("node") or "")
    if not node or vmid <= 0:
        return False, REASON_NOT_VM

    conn_id = str(host.get("conn_id") or "")
    try:
        # 受管主机行里的 conn_id 可能是「删除后重建连接」留下的旧 id；回落到
        # 当前连接，别让一台机器因为面板重新配置过而永远不可用。
        client = pve.client_for_connection(conn_id, fallback_to_active=True)
    except ProxmoxError:
        return False, REASON_UNREACHABLE
    except Exception:  # noqa: BLE001 - 连接配置坏掉也只是这一台不可用
        return False, REASON_UNREACHABLE

    # 1) 是虚拟机且在运行。qemu 接口对 LXC / 不存在的 VMID 会回 404，
    #    按「不是虚拟机」处理；其余错误（401/403/5xx/超时）按「读不到」处理。
    #    先问它是因为：它最便宜，而且停机 / 容器 / 不存在的机器到这里就能收工，
    #    不必再花两次往返。
    if known_status == "running":
        status = {"status": "running"}
    else:
        try:
            status = await _call(
                lambda: client.qemu_status(node, vmid),
                timeout=tune.timeout,
                retries=tune.retries,
            )
        except asyncio.TimeoutError:
            return False, REASON_UNREACHABLE
        except ProxmoxError as exc:
            if int(getattr(exc, "pve_status", 0) or 0) == 404:
                return False, REASON_NOT_VM
            return False, REASON_UNREACHABLE
        except Exception:  # noqa: BLE001
            return False, REASON_UNREACHABLE

    if str((status or {}).get("status") or "") != "running":
        return False, REASON_STOPPED

    # 2) + 3) cloud-init 配置与 Guest Agent 应答**并行**问。
    #    两者互不依赖：配置读的是 PVE 里的虚拟机定义，ping 等的是客户机里的服务。
    #    串行时一台跑通的机器要 3 次往返，并行后 2 次 —— 40 台机器上这是 40 个
    #    RTT 的差别，而它们本来只是「排队等一个跟彼此无关的答案」。
    #
    #    取不到配置时按「读不到」而不是「没配」：前者是权限 / 网络问题，
    #    后者是这台机器的事实，提示语不能混。
    async def _cloudinit() -> str:
        try:
            config = await _call(
                lambda: client.qemu_config(node, vmid),
                timeout=tune.timeout,
                retries=tune.retries,
            )
        except asyncio.TimeoutError:
            return REASON_UNREACHABLE
        except ProxmoxError as exc:
            if int(getattr(exc, "pve_status", 0) or 0) == 404:
                return REASON_NOT_VM
            return REASON_UNREACHABLE
        except Exception:  # noqa: BLE001
            return REASON_UNREACHABLE
        return "" if _cloudinit_ready(config) else REASON_CLOUDINIT

    async def _agent() -> str:
        # 这是「进得去」最便宜、也最直接的证据；没有它，虚拟机内取证与命令
        # 执行都无从谈起。超时不重试（见 _call 的 retry_timeout）。
        #
        # 失败原因要分开看：客户机里没装 / 没起 agent 是**这台机器**的事实，
        # 而 5xx / 连不上是 PVE 的问题。都报「Guest Agent 未响应」会让用户去
        # 客户机里白找一圈 —— 那种「按提示修了半天没修好」的最伤信任。
        try:
            await _call(
                lambda: client.qemu_agent_ping(node, vmid),
                timeout=tune.timeout,
                retries=tune.retries,
                retry_timeout=False,
            )
        except ProxmoxError as exc:
            status = int(getattr(exc, "pve_status", 0) or 0)
            if status == 0 or status in _TRANSIENT_STATUS:
                return REASON_UNREACHABLE
            return REASON_AGENT
        except (asyncio.TimeoutError, TimeoutError):
            # 等满一个超时还没应答：agent 不在线（装着但卡住，也算不在线）
            return REASON_AGENT
        except Exception:  # noqa: BLE001 - 未安装 / 未运行 / 协议不支持
            return REASON_AGENT
        return ""

    cloudinit_reason, agent_reason = await asyncio.gather(_cloudinit(), _agent())
    # 优先级：读不到 > 没配 > agent 不应答。三者同时成立时，最该让用户先处理
    # 的是「读不到」—— 它连判断依据都没有。
    for reason in (cloudinit_reason, agent_reason):
        if reason:
            return False, reason
    return True, ""


def _key(host: Dict[str, Any]) -> Optional[Tuple[str, str, int]]:
    try:
        vmid = int(host.get("vmid"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    node = str(host.get("node") or "")
    if not node or vmid <= 0:
        return None
    return str(host.get("conn_id") or ""), node, vmid


def _fresh(key: Tuple[str, str, int], ttl: float) -> Optional[Tuple[bool, str]]:
    """进程内缓存里仍然新鲜的结论（`ttl` 由调用方传入，测试可缩短）。"""
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1], hit[2]
    return None


async def check_host(
    host: Dict[str, Any],
    *,
    tune: Optional[Tuning] = None,
    collected: Optional[List[Tuple[Tuple[str, str, int], Tuple[bool, str]]]] = None,
    use_shared: bool = True,
    known_status: Optional[str] = None,
) -> Tuple[bool, str]:
    """判定一台主机是否可作为 AI 助手的目标（带短时缓存）。

    ``collected`` 非空时不单独写共享缓存，而是把结论攒进这个列表 —— 批量扫描
    最后用一次 ``executemany`` 统一写回，省掉每台一次往返。

    ``use_shared=False`` 表示「调用方已经整批读过共享缓存了」：批量扫描先做一次
    ``IN (...)`` 预读，这里若再逐台查一遍，等于把刚省掉的 N 条 SELECT 原样加
    回来 —— 那正是这条链路最贵的一笔开销。

    ``known_status`` 由批量取状态那次调用给出（见 :func:`_batch_statuses`）：
    已经知道在跑的机器不必再单独问一遍状态。
    """
    tune = tune or tuning()
    key = _key(host)
    if key is None:
        return False, REASON_NOT_VM
    hit = _fresh(key, tune.ttl)
    if hit is not None:
        return hit

    # 同一台机器同时被多个人 / 多个 worker 命中时，只做一次真实探测。
    lock = _PROBING.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            # 等锁期间别人可能已经填好了，再查一次 L1
            hit = _fresh(key, tune.ttl)
            if hit is not None:
                return hit
            # L2：别的 worker 探过的结果（库里的 TTL 用墙上时钟）
            if use_shared:
                shared = await _load_shared(key)
                if shared is not None:
                    _CACHE[key] = (time.monotonic(), shared[0], shared[1])
                    return shared
            try:
                result = await _check_uncached(host, tune, known_status=known_status)
            except Exception:  # noqa: BLE001 - 探测自己炸了也要有个结论
                # 不落结论的后果不是「少一条数据」：前端会一直看到 pending，
                # 每 2 秒回来问一次，永远问不到答案。
                logger.warning("AI 目标探测异常：%s", key, exc_info=True)
                result = (False, REASON_UNREACHABLE)
            _CACHE[key] = (time.monotonic(), result[0], result[1])
            if collected is not None:
                collected.append((key, result))
            else:
                await _save_shared(key, result)
            return result
    finally:
        # 只留「有人正在等」的锁：否则 _PROBING 会随探测过的机器数一直涨，
        # 长跑进程里就是一条稳定的内存泄漏。这里与 setdefault 的竞态只会导致
        # 一次多余的探测，不会导致错误结论。
        if not lock.locked() and _PROBING.get(key) is lock:
            _PROBING.pop(key, None)


def _classify(
    results: List[Tuple[Dict[str, Any], bool, str]]
) -> Tuple[List[Dict[str, Any]], Dict[str, int], List[Dict[str, Any]]]:
    """把判定结果拆成「可用」「按原因计数」「被排除明细」。"""
    eligible: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {"total": 0}
    details: List[Dict[str, Any]] = []
    for host, ok, reason in results:
        if ok:
            eligible.append(host)
            continue
        counts["total"] += 1
        counts[reason] = counts.get(reason, 0) + 1
        details.append(
            {
                "id": str(host.get("id") or host.get("host_id") or ""),
                "name": str(
                    host.get("name") or host.get("host") or host.get("id") or ""
                ),
                "reason": reason,
                "node": str(host.get("node") or ""),
                "vmid": host.get("vmid"),
                "conn_id": str(host.get("conn_id") or ""),
            }
        )
    return eligible, counts, details


async def _prefetch_shared(
    candidates: Sequence[Dict[str, Any]], tune: Tuning
) -> None:
    """把一批共享结论一次性读进进程内缓存。

    命中 L2 的机器整轮都不碰 PVE；没命中的留给真实探测。前端每 2 秒轮询一次，
    这一步把它从「N 条 SELECT」压成「1 条」。
    """
    keys: List[Tuple[str, str, int]] = []
    for host in candidates:
        key = _key(host)
        if key is not None and _fresh(key, tune.ttl) is None:
            keys.append(key)
    if not keys:
        return
    found = await _load_shared_many(keys, tune.ttl)
    if not found:
        return
    stamp = time.monotonic()
    for key, (ok, reason) in found.items():
        _CACHE.setdefault(key, (stamp, ok, reason))


async def _drop_dead_connections(
    candidates: Sequence[Dict[str, Any]],
    tune: Tuning,
    verdicts: Dict[Tuple[str, str, int], Tuple[bool, str]],
) -> Tuple[int, List[Tuple[str, str, int]]]:
    """同一连接上的机器先集体探活。

    返回 ``(连不上的连接数, 这些连接上被判不可用的机器)``。后者要随结论一起
    写进共享缓存 —— 否则下一轮轮询又会来探一遍同一条已经躺下的 PVE。

    只在一条连接上有 ≥2 台机器时才做：单台机器探活等于多花一次往返，没有收益。
    """
    groups: Dict[str, List[Tuple[str, str, int]]] = {}
    for host in candidates:
        key = _key(host)
        if key is None or key in verdicts:
            continue
        groups.setdefault(key[0], []).append(key)

    async def _probe(conn_id: str) -> bool:
        try:
            client = pve.client_for_connection(conn_id, fallback_to_active=True)
        except Exception:  # noqa: BLE001 - 取不到客户端按整组不可用
            return False
        if client is None:
            return False
        return await _connection_alive(conn_id, client, tune)

    # 各连接**并行**探活：串行时几条 PVE 同时躺下，每条各等一个超时，
    # 光这一步就能把首屏拖到十几秒 —— 而这几条之间毫无依赖。
    targets = [(conn_id, keys) for conn_id, keys in groups.items() if len(keys) >= 2]
    if not targets:
        return 0, []
    alive_flags = await asyncio.gather(*(_probe(conn_id) for conn_id, _ in targets))

    down = 0
    dead: List[Tuple[str, str, int]] = []
    for _entry, alive in zip(targets, alive_flags):
        if alive:
            continue
        down += 1
        for key in _entry[1]:
            verdicts[key] = (False, REASON_UNREACHABLE)
            dead.append(key)
    return down, dead


#: ``_batch_statuses`` 的「这家客户端没有批量接口」标记（老客户端 / 测试替身）
_NO_BATCH = object()


async def _batch_statuses(
    candidates: Sequence[Dict[str, Any]],
    tune: Tuning,
    verdicts: Dict[Tuple[str, str, int], Tuple[bool, str]],
) -> Tuple[
    int,
    List[Tuple[str, str, int]],
    Dict[Tuple[str, str, int], str],
    bool,
]:
    """一条连接查一次集群资源，代替「逐台问状态」。

    逐台问状态是这条链路上最没道理的一笔开销：60 台机器就要 60 次
    ``/nodes/{node}/qemu/{vmid}/status/current``，而 ``/cluster/resources``
    一次就能拿到所有虚拟机（含 lxc）的 ``status``。这次往返还顺带完成了
    探活 —— 它失败就等于整条 PVE 连不上，不必再单独打一次 ``/version``。

    拿到之后：
    * 停机 / 是容器 → 当场判死，**0 次额外往返**（原先是每台各一次 status）；
    * 在跑 → 交给 :func:`check_host` 的 ``known_status``，每台只剩 config ∥ ping
      一轮往返（原先两轮）。

    返回 ``(连不上的连接数, 判死的 key, 已知在跑的 key→状态, 是否需要退回逐台)``。
    最后一项为真表示有连接的客户端没有批量接口（老客户端 / 替身），调用方
    再走一遍原来的「探活 + 逐台问状态」，两者不会重复探测已经在 verdicts 里的机器。

    批量结果里**查不到**的 key 不做结论：凭据可能只是看不到这台机器（应当报
    「读不到」），而逐台 status 才能分辨 404（不是虚拟机）与 403（权限不足）。
    """
    groups: Dict[str, List[Tuple[str, str, int]]] = {}
    for host in candidates:
        key = _key(host)
        if key is None or key in verdicts:
            continue
        if _fresh(key, tune.ttl) is not None:
            # 已有新鲜结论（进程内、或刚才预读的共享缓存）：连批量状态都不必问，
            # 否则 TTL 之内的每次轮询都会白打一轮 PVE
            continue
        groups.setdefault(key[0], []).append(key)
    if not groups:
        return 0, [], {}, False

    def _down(conn_id: str) -> None:
        """记下「这条连接现在读不到」，几秒内的后续扫描据此直接给结论。"""
        _HEALTH[conn_id] = (time.monotonic(), False)
        logger.info(
            "AI 目标探测：连接 %s 批量取状态失败，本轮按其不可达处理",
            conn_id or "default",
        )
        return None

    async def probe(conn_id: str) -> Any:
        # 几秒内刚判定连不上的连接：直接给结论，不再花一次往返
        hit = _HEALTH.get(conn_id)
        now = time.monotonic()
        if hit and now - hit[0] < HEALTH_TTL and not hit[1]:
            return None
        try:
            client = pve.client_for_connection(conn_id, fallback_to_active=True)
        except Exception:  # noqa: BLE001 - 取不到客户端：退回逐台，不判整条连接
            return _NO_BATCH
        if client is None:
            return None
        if not hasattr(client, "cluster_resources"):
            return _NO_BATCH
        try:
            items = await _call(
                lambda: client.cluster_resources("vm"),
                timeout=tune.timeout,
                retries=tune.retries,
            )
        except ProxmoxError as exc:
            # 凭据不被允许读集群资源：那是**权限范围**的问题，不是连接断了。
            # 按「这家客户端不能批量读」处理，退回逐台的 status —— 它对只授权到
            # 某几台虚拟机的令牌照样能给出答案，而整组判死会冤枉所有机器。
            # 其余「PVE 有应答」的错误（4xx / 500）同理：退回逐台，让每台机器
            # 自己报原因；只有真正的连接层失败才记整条连接读不到。
            return _down(conn_id) if _is_connection_failure(exc) else _NO_BATCH
        except Exception as exc:  # noqa: BLE001
            return _down(conn_id) if _is_connection_failure(exc) else _NO_BATCH
        _HEALTH[conn_id] = (time.monotonic(), True)
        return items or []

    # 各连接并行：几条 PVE 同时躺下时，串行会让每条各等一个超时
    probed = await asyncio.gather(*(probe(conn_id) for conn_id in groups))

    down = 0
    dead: List[Tuple[str, str, int]] = []
    known: Dict[Tuple[str, str, int], str] = {}
    fallback = False
    for (conn_id, keys), items in zip(groups.items(), probed):
        if items is _NO_BATCH:
            fallback = True
            continue
        if items is None:
            down += 1
            for key in keys:
                verdicts[key] = (False, REASON_UNREACHABLE)
                dead.append(key)
            continue

        index: Dict[Tuple[str, int], Dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                index[(str(item.get("node") or ""), int(item.get("vmid") or 0))] = item
            except (TypeError, ValueError):
                continue

        for key in keys:
            item = index.get((key[1], key[2]))
            if item is None:
                continue  # 批量里没有：留给逐台 status 分辨 404 / 403
            if str(item.get("type") or "qemu") != "qemu":
                verdicts[key] = (False, REASON_NOT_VM)
            elif str(item.get("status") or "") != "running":
                verdicts[key] = (False, REASON_STOPPED)
            else:
                known[key] = "running"
    return down, dead, known, fallback


async def _run_scan(
    candidates: Sequence[Dict[str, Any]],
    tune: Tuning,
    *,
    budget_s: float = 0.0,
) -> Tuple[List[Tuple[Dict[str, Any], bool, str]], int, Dict[str, Any]]:
    """真正的批量扫描：批量取状态 → 预读缓存 → 限流并发探测 → 一次写回。

    返回 ``(逐台结果, 未完成台数, 统计)``。未完成的那些**不进结果集**：
    把「还没探完」当成「可用」会让用户选中它、然后第一条命令就失败。
    """
    started = time.monotonic()
    deadline = started + budget_s if budget_s > 0 else 0.0

    await _prefetch_shared(candidates, tune)

    verdicts: Dict[Tuple[str, str, int], Tuple[bool, str]] = {}
    # 一次「集群资源」把停机 / 容器 / 在跑都判掉，同时兼作探活
    down, dead_keys, known, fallback = await _batch_statuses(
        candidates, tune, verdicts
    )
    if fallback:
        # 老客户端 / 测试替身没有批量接口：退回「按连接探活 + 逐台问状态」
        more_down, more_dead = await _drop_dead_connections(candidates, tune, verdicts)
        down += more_down
        dead_keys.extend(more_dead)

    # 并发按台数自适应：60 台压在 12 个位置上就是排 5 轮，每轮一个 RTT
    limit = fanout(tune, len(candidates))
    sem = asyncio.Semaphore(limit)
    collected: List[Tuple[Tuple[str, str, int], Tuple[bool, str]]] = [
        # 「整条 PVE 连不上」同样是结论：写进缓存，下一轮轮询就不必再探一遍
        (key, (False, REASON_UNREACHABLE)) for key in dead_keys
    ]
    pending = 0

    async def one(host: Dict[str, Any]) -> Optional[Tuple[Dict[str, Any], bool, str]]:
        nonlocal pending
        key = _key(host)
        if key is None:
            return host, False, REASON_NOT_VM
        early = verdicts.get(key)
        if early is not None:
            return host, early[0], early[1]
        async with sem:
            # 排队期间可能已经超预算：与其再起一次探测，不如如实报「还没探完」
            if deadline and time.monotonic() > deadline:
                pending += 1
                return None
            async with _conn_sem(key[0], tune.conn_concurrency):
                # use_shared=False：上面已经整批预读过 L2，这里逐台再查一遍
                # 会把刚省掉的 N 条 SELECT 原样加回来
                ok, reason = await check_host(
                    host,
                    tune=tune,
                    collected=collected,
                    use_shared=False,
                    known_status=known.get(key),
                )
            return host, ok, reason

    gathered = await asyncio.gather(*(one(host) for host in candidates))
    results = [item for item in gathered if item is not None]

    # 一次 executemany 写回：批量扫描里几十台机器只花一个往返
    await _save_shared_many(collected)

    stats = {
        "total": len(candidates),
        "probed": len(results),
        "pending": pending,
        "pve_down": down,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        # 这一轮**实际**用的并发（自适应后的值），不是配置里的下限
        "concurrency": limit,
        "cache_ttl": tune.ttl,
    }
    _LAST_STATS.clear()
    _LAST_STATS.update(stats)
    return results, pending, stats


async def evaluate_hosts_with_details(
    hosts: Iterable[Dict[str, Any]],
    *,
    budget_s: float = 0.0,
) -> Tuple[List[Dict[str, Any]], Dict[str, int], List[Dict[str, Any]]]:
    """把主机清单拆成「可用」「按原因计数」「被排除明细」三部分。

    ``hosts`` 必须已经按当前用户的可见范围过滤过（调用方传的是
    ``baseline.targets(allowed)``）。因此明细里的机器名全部是用户**本来就能
    在体检页看到**的，不构成新的资产泄露 —— 这是把「只报台数」升级成「点名 +
    原因 + 修复入口」的前提。不要在没有作用域过滤的调用点上使用明细。

    ``budget_s`` 大于 0 时最多等这么久：到点先回已经探完的，剩下的计入
    ``counts['pending']``，由前端轮询补齐。大集群上「等全部探完」很容易超过
    反向代理的超时，用户看到的是一个转圈到断开的页面。

    注意这是**软上限**：到点之后不再起新的探测，但已经在飞的那几台会让它多等
    一个探测周期（最长 ≈ 2 × ``ai_scan_timeout``）。硬砍掉它们的代价是白白丢掉
    一次已经花掉的往返，而且用户在「差一点点就探完」时看到结果少了一半。
    """
    candidates = list(hosts or [])
    if not candidates:
        _LAST_STATS.clear()
        _LAST_STATS.update(
            {"total": 0, "probed": 0, "pending": 0, "pve_down": 0, "elapsed_ms": 0.0}
        )
        return [], {"total": 0}, []

    tune = tuning()
    results, pending, _stats = await _run_scan(candidates, tune, budget_s=budget_s)
    eligible, counts, details = _classify(results)
    if pending:
        counts["pending"] = pending
    return eligible, counts, details


#: 正在后台补探的 key 集合：前端每 2 秒轮询一次，不加这个每次都会新开一批探测。
_WARMING: set = set()

#: 后台探测任务的强引用。只 create_task 不存引用的话，任务可能在跑完前被 GC 回收
#: （事件循环只保留弱引用），表现成「探测偶尔不生效、要手动刷新」。
_WARM_TASKS: set = set()


def _spawn_warm(hosts: List[Dict[str, Any]]) -> None:
    task = asyncio.create_task(_warm(hosts))
    _WARM_TASKS.add(task)
    task.add_done_callback(_WARM_TASKS.discard)


async def _warm(hosts: List[Dict[str, Any]]) -> None:
    """后台把没结论的主机探一遍，写进两级缓存；失败就让它继续 pending。"""
    try:
        await evaluate_hosts_with_details(hosts)
    except Exception:  # noqa: BLE001 - 后台任务，出错只影响下次轮询
        logger.warning("AI 目标后台探测失败", exc_info=True)
    finally:
        for host in hosts:
            key = _key(host)
            if key is not None:
                _WARMING.discard(key)


def _recent_down_connections(hosts: Iterable[Dict[str, Any]]) -> int:
    """本轮候选里，**刚刚**探活失败了的 PVE 连接有几条。

    两个过滤条件都是必要的（线上反馈：PVE 明明正常却一直提示「1 条读不到」）：

    1. 只统计本轮候选主机所属的连接。``_HEALTH`` 是进程级全局表，暂停 / 已删除
       连接的旧记录原本也会被算进来，于是页面凭空多出一条「读不到」。
    2. 只认 ``HEALTH_TTL``（5 秒）内的失败，而不是 60 秒。批量取状态偶发一次
       超时就会写一条 False；如果这一轮所有主机都命中判定缓存、不再探活，这条
       False 在 60 秒内没有任何机会被成功结果覆盖 —— 用户盯着一句早已恢复的
       告警，只会去反复检查正常的 PVE。5 秒与探活缓存同寿：真有故障，下一次
       轮询会重新写 False，提示自己回来。
    """
    candidate_conns = {
        key[0] for host in hosts if (key := _key(host)) is not None
    }
    now = time.monotonic()
    return sum(
        1
        for conn_id, (stamp, alive) in _HEALTH.items()
        if conn_id in candidate_conns
        and not alive
        and now - stamp < HEALTH_TTL
    )


async def evaluate_hosts_fast(
    hosts: Iterable[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, int], List[Dict[str, Any]], int]:
    """「列表先出、状态后补」版：只读缓存，不等 PVE。

    冷启动时每台机器要真去问 PVE（批量状态 + config ∥ ping），几十台就是几十个
    往返；放在首屏请求里等，页面就转圈。这里只回**已经有结论**的部分，没结论的
    排进后台并返回 ``pending`` 台数 —— 前端拿到 pending 就隔半秒再问一次，列表
    于是「先出来、很快就补齐」。

    返回 ``(可用, 排除计数, 排除明细, 待定台数)``。待定的机器**不进可用列表**：
    放进去用户会选中它，然后第一条命令就失败 —— 那比多等两秒糟得多。
    """
    candidates = list(hosts or [])
    if not candidates:
        return [], {"total": 0}, [], 0

    tune = tuning()
    started = time.monotonic()

    # 一次 IN 查询把共享结论全部读进来（原来是每台一条 SELECT）
    await _prefetch_shared(candidates, tune)

    results: List[Tuple[Dict[str, Any], bool, str]] = []
    pending_hosts: List[Dict[str, Any]] = []
    for host in candidates:
        key = _key(host)
        if key is None:
            results.append((host, False, REASON_NOT_VM))
            continue
        hit = _fresh(key, tune.ttl)
        if hit is None:
            pending_hosts.append(host)
            continue
        results.append((host, hit[0], hit[1]))

    eligible, counts, details = _classify(results)

    if pending_hosts:
        fresh = [
            host
            for host in pending_hosts
            if _key(host) is not None and _key(host) not in _WARMING
        ]
        for host in fresh:
            key = _key(host)
            if key is not None:
                _WARMING.add(key)
        if fresh:
            # 不 await：首屏不等探测。结果进缓存，供下一次轮询读
            _spawn_warm(fresh)
        # 已经在探的也算 pending，前端据此继续轮询
        counts["pending"] = len(pending_hosts)

    # 首屏这一轮不探活（那是补探的事），但「哪条 PVE 现在连不上」要如实带出去：
    # 页面据此显示集中提示，而不是让十几台机器各报一条「状态读取失败」。
    down = _recent_down_connections(candidates)
    _LAST_STATS.clear()
    _LAST_STATS.update(
        {
            "total": len(candidates),
            "probed": len(results),
            "pending": len(pending_hosts),
            "pve_down": down,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "concurrency": fanout(tune, len(candidates)),
            "cache_ttl": tune.ttl,
        }
    )
    return eligible, counts, details, len(pending_hosts)


async def evaluate_hosts(
    hosts: Iterable[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """两元组版本：只关心「可用清单 + 原因计数」的调用方用它。"""
    eligible, counts, _ = await evaluate_hosts_with_details(hosts)
    return eligible, counts
