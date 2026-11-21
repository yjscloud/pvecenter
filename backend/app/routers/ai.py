"""AI 排查助手的接口。

* ``GET  /api/ai/config``      —— 读配置（密钥只回「配没配」与打码值）
* ``PUT  /api/ai/config``      —— 保存配置
* ``POST /api/ai/config/test`` —— 连通性测试（可用未保存的表单值直接测）
* ``GET  /api/ai/targets``     —— 可排查的主机清单 + 助手可用状态
* ``POST /api/ai/inspect``     —— 跑一次排查

权限上刻意分成两档：

* **排查**只读 —— 读的是平台已经采集好的巡检结果，不连目标主机、不改任何
  配置，因此沿用 ``baseline.view``：能看体检报告的人就能让 AI 解读一遍；
* **配置**是全局动作（所有用户共用同一份模型配置和密钥），只有管理员能碰。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse

from .. import (
    ai,
    ai_approval,
    aichat,
    aiaudit,
    aiperf,
    aiplaybooks,
    ai_scope,
    aiterm,
    aivm,
    aivm_diagd,
    baseline,
    guestguard,
    hostscope,
    i18n,
    ownership,
    security,
    sshremote,
    store,
)
from ..config import settings
from . import vm_meta as vm_meta_router

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["ai"])

VIEW = security.require_permission("baseline.view")
# AI 的模型 / Key 是面板自身的配置，归「系统设置」那一档（原先是 require_admin()，
# 也就等于 users.manage —— 勾了用户管理的人顺带拿到了 AI 配置权）
ADMIN = security.require_permission("settings.manage")

def _env_limit(name: str, default: int) -> int:
    """读并发上限环境变量，非法值/越界一律回落到默认值（不能因为写错就崩）。"""
    try:
        value = int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default
    return max(1, min(128, value))


# 同时进行的排查上限。一次 L2 排查要 SSH 到目标主机跑多轮命令、再调七八次模型；
# 多用户并发时既可能压垮被排查的机器，也可能瞬间打爆模型额度（花的是配 key 那位的钱）。
# 超出上限的排在后面等，不直接拒绝 —— 排队比失败更符合预期。
#
# 数值做成环境变量：不同 provider 的限速差一个数量级（本地 Ollama 能跑几十路，
# 企业版 key 可能只给 5 RPM），写死一个数总有一边不对。改这里不用改代码：
#   PVECENTER_AI_CONCURRENCY        全局总闸（默认 16）
#   PVECENTER_AI_LANE_CONCURRENCY   单个 provider 子闸（默认 6）
MAX_CONCURRENT_TOTAL = _env_limit("PVECENTER_AI_CONCURRENCY", 16)
#: 每个 provider 一条子闸：真正稀缺的是那把共用的 API key，不是面板的线程。
MAX_CONCURRENT_LANE = _env_limit("PVECENTER_AI_LANE_CONCURRENCY", 6)
#: 兼容旧名字：体检链路历史上按「同时排查数」命名，语义等同 lane 闸。
MAX_CONCURRENT_INSPECTIONS = MAX_CONCURRENT_LANE
#: 排队时对「每一次前面还有多久」的兜底粗估（秒）。有本 provider 的真实历史
#: 耗时就用实测均值，没有（刚启动 / 这个 key 第一次用）才退回它。
QUEUE_ETA_SECONDS = 45

_GLOBAL_SEM: Optional[asyncio.Semaphore] = None
_LANE_SEMS: Dict[str, asyncio.Semaphore] = {}
_LANE_WAITING: Dict[str, int] = {}
#: 每个 provider 最近 N 轮的实际耗时（毫秒）。只留窗口，避免某一次异常慢
#: 把 ETA 永久带偏；排队提示是「量级」，不是承诺。
_LANE_DURATIONS: Dict[str, deque] = {}
_ETA_WINDOW = 20


def _global_semaphore() -> asyncio.Semaphore:
    """全局总闸。懒建是为了不绑定到 import 时可能还不存在的事件循环。"""
    global _GLOBAL_SEM
    if _GLOBAL_SEM is None:
        _GLOBAL_SEM = asyncio.Semaphore(MAX_CONCURRENT_TOTAL)
    return _GLOBAL_SEM


def _inspect_semaphore() -> asyncio.Semaphore:
    """兼容旧的调用点（配置连通性测试等）：它们只关心「全局有没有满」。"""
    return _global_semaphore()


def _lane_for(provider_id: str) -> str:
    """并发分组按**模型配置**而不是按用户：真正稀缺的是那把共用的 API key。"""
    return str(provider_id or "default")


def _lane_semaphore(lane: str) -> asyncio.Semaphore:
    sem = _LANE_SEMS.get(lane)
    if sem is None:
        sem = asyncio.Semaphore(MAX_CONCURRENT_LANE)
        _LANE_SEMS[lane] = sem
    return sem


@asynccontextmanager
async def _turn_lane(lane: str) -> AsyncIterator[None]:
    """先过全局总闸、再过 provider 子闸。两层都是「排队不拒绝」。"""
    async with _global_semaphore():
        async with _lane_semaphore(lane):
            yield


def _queued_event(lane: str = "default") -> Dict[str, Any]:
    """进入队列时下发的位置与等待估计。

    ``ahead`` 是同一 provider 队列里排在前面的人数（不含正在跑的那几轮）；
    ``eta_s`` 用该 provider 最近若干轮的实际耗时均值推算，第一次用这个 key 时
    才退回固定粗估值。
    """
    ahead = _LANE_WAITING.get(lane, 0)
    _LANE_WAITING[lane] = ahead + 1
    durations = list(_LANE_DURATIONS.get(lane, ()))
    if durations:
        average_s = sum(durations) / len(durations) / 1000
        # 排队 + 自己这一轮，按最近实测均值估；夹一下，避免极端值吓得人不敢用
        eta = max(5, min(300, int(average_s * (ahead + 1))))
    else:
        eta = (ahead + 1) * QUEUE_ETA_SECONDS
    return {
        "type": "stage",
        "stage": "queued",
        "ahead": ahead,
        "eta_s": eta,
    }


def _leave_queue(lane: str = "default") -> None:
    _LANE_WAITING[lane] = max(0, _LANE_WAITING.get(lane, 0) - 1)


def _record_duration(lane: str, ms: float) -> None:
    """记录一轮**实际执行**耗时（不含排队），供下一次排队估计使用。"""
    bucket = _LANE_DURATIONS.setdefault(lane, deque(maxlen=_ETA_WINDOW))
    bucket.append(max(0.0, float(ms)))


@router.get("/config")
async def get_config(user: Dict[str, Any] = Depends(ADMIN)) -> Dict[str, Any]:
    cfg = await ai.load_config()
    # 厂商预设跟着配置一起下发：配置面板一打开就能用，省一次往返
    return {**ai.public_config(cfg), "presets": ai.provider_presets()}


@router.put("/config")
async def put_config(
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(ADMIN),
) -> Dict[str, Any]:
    """保存配置。``api_key`` 留空表示沿用旧密钥（与腾讯云那套约定一致）。"""
    saved = await ai.save_config(payload)
    await security.audit(
        request,
        user,
        "ai.config",
        "ai",
        "success",
        "更新 AI 助手配置（" + str(len(saved.get("providers") or [])) + " 个模型）",
    )
    return saved


@router.post("/config/test")
async def test_config(
    payload: Dict[str, Any],
    user: Dict[str, Any] = Depends(ADMIN),
) -> Dict[str, Any]:
    """连通性测试。

    允许直接拿**还没保存**的表单值来测（地址填错时不该逼用户先存一遍再发现），
    密钥留空则回退到已保存的那一份。
    """
    item = payload.get("provider") if isinstance(payload.get("provider"), dict) else payload
    prov = ai.normalise_provider(item or {})

    if not prov.get("api_key"):
        cfg = await ai.load_config()
        for old in cfg.get("providers") or []:
            if old.get("id") == prov.get("id"):
                prov["api_key"] = old.get("api_key") or ""
                break

    try:
        return await ai.test_provider(prov)
    except ai.AIError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _scan_inline(candidate_count: int) -> bool:
    """候选台数是否少到值得「当场探完再回」。

    阈值在 ``Settings.ai_scan_inline_max``：台数少时探测只要几百毫秒，等它比
    多等一个轮询周期划算；台数多时先出列表更值钱。脏值 / 0 一律按「不当场探」
    处理 —— 那正是加这个阈值之前的行为，最坏只是慢，不会把首屏挂死在探测上。
    """
    try:
        limit = int(getattr(settings, "ai_scan_inline_max", 0) or 0)
    except (TypeError, ValueError):
        limit = 0
    return candidate_count <= max(0, limit)


@router.get("/targets")
async def targets(
    request: Request, user: Dict[str, Any] = Depends(VIEW)
) -> Dict[str, Any]:
    """可排查的主机清单，附带助手当前的可用状态。

    状态一起回给前端，是为了让页面能区分「还没配模型」和「配好了可以排查」
    两种情形，而不是让用户点一下才被告知没配置。

    候选按五条件与运算筛过（运行中、已安全纳管、Guest Agent 正常、cloud-init
    正常、是虚拟机），并带回被排除的原因计数；规则见 ``ai_scope``。

    **候选很少时当场探完**：探测本身只有两次 PVE 往返，而「先回列表、后台补探」
    要等前端下一次轮询才有结论 —— 两三台机器上，那一个轮询周期才是用户看到的
    「慢」。阈值见 ``Settings.ai_scan_inline_max``（默认 8，设 0 = 一律不当场探）。

    **候选很多时默认不等探测**：每台机器两次 PVE 往返，几十台就能把首屏拖到
    十几秒。先回已经有结论的，没结论的排后台，``pending_total`` 大于 0 时前端
    隔两秒再问一次，于是「列表先出来、状态逐渐补齐」。``?refresh=1`` 则等全部
    探测完成（页面上「重新检测」这类显式动作、脚本与测试用）；它同样受
    ``ai_scan_budget`` 上限约束，到点先回已探完的。
    """
    allowed = await hostscope.allowed_host_ids(user)
    cfg = await ai.load_config()
    # 必须带上身份：模型是严格隔离的，"我到底有没有可用的模型"因人而异。
    # 漏传身份会按匿名算（谁都不许），于是明明配好了也显示「还没有配置大模型」，
    # 而下面的模型下拉却是满的 —— 自相矛盾的提示比没有提示更糟。
    provider = ai.active_provider(
        cfg,
        username=str(user.get("username") or ""),
        is_admin=_is_admin(user),
    )
    # 候选必须是「这一轮确实做得到」的机器：五条件为与关系，任一不满足就排除
    # （详见 ai_scope 模块头）。这里不放宽到「先列出来、点进去再报错」——
    # 用户会以为那台机器能查，而实际上第一条命令就会失败。
    candidates = ai.ai_targets(await baseline.targets(allowed))
    wait = str(request.query_params.get("refresh") or "") in ("1", "true", "yes")
    pending = 0
    # 台数很少时**当场探完**再回：探测本身只有两次往返（几百毫秒），而「先出
    # 列表、后台补探」这条要等前端下一次轮询才有结论 —— 两三台机器上，那一个
    # 轮询周期才是用户看到的「慢」。台数一多就反过来了：先出列表更值钱。
    inline = _scan_inline(len(candidates))
    if wait or inline:
        # 显式刷新 / 当场探也要有上限：全量探测动辄一两分钟，等于把请求挂在
        # 反向代理的超时上 —— 用户看到的是转圈到断开，不是「还没探完」。
        # 到点先回已探完的，pending 交给前端轮询补齐（它本来就会这么做）。
        hosts, excluded, excluded_items = await ai_scope.evaluate_hosts_with_details(
            candidates, budget_s=settings.ai_scan_budget
        )
        pending = int(excluded.get("pending") or 0)
    else:
        (
            hosts,
            excluded,
            excluded_items,
            pending,
        ) = await ai_scope.evaluate_hosts_fast(candidates)
    return {
        # 还有几台没探完：大于 0 时前端继续轮询，列表先出、状态后补
        "pending_total": pending,
        # 这一轮扫描的自述（探了几台 / 多久 / 几条 PVE 连不上）：页面上用来
        # 显示进度，排障时用来区分「机器多」和「PVE 慢」—— 两者处理方式不同。
        "scan": ai_scope.last_stats(),
        # 刻意**不含面板本机**（ai.ai_targets 负责剔除）：本机是跑着这个面板、
        # 握着全部受管凭据的那台机器，而助手会上机执行命令、还能开终端。
        # 本机的安全状况另有自己那几页的本机作用域，不必从这条更放权的路进来。
        "hosts": hosts,
        # 计数用于一句话概括，items 用于点名到具体机器 + 原因 + 修复入口。
        # 明细只来自上一步已按 allowed 过滤后的主机，因此不越出用户的可见范围。
        "excluded": {**excluded, "items": excluded_items},
        "status": {
            "enabled": bool(cfg.get("enabled")),
            "ready": bool(cfg.get("enabled") and provider),
            "provider": (provider.get("name") or provider.get("model") or "") if provider else "",
            "provider_kind": (provider.get("kind") or "") if provider else "",
        },
    }


def _is_admin(user: Dict[str, Any]) -> bool:
    return str(user.get("role") or "") == "admin"


def _exec_access(payload: Dict[str, Any], allow_exec: bool) -> str:
    """解析主机命令授权级别。

    老客户端只传 ``allow_exec``：按 write 处理，保持合并前的「可以提议写命令」。
    新客户端显式传 ``exec_access``；未授权时一律 read，避免残留级别被误用。
    """
    if not allow_exec:
        return "read"
    level = str(payload.get("exec_access") or "").strip().lower()
    return "read" if level == "read" else "write"


def _lease_scope_matches(
    lease: Dict[str, Any], *, fs_rw: List[str], units: List[str]
) -> bool:
    """级别之外，可写目录与可重启服务也必须一致才允许复用旧票。"""
    return set(lease.get("fs_rw") or []) == set(fs_rw) and set(
        lease.get("units") or []
    ) == set(units)


async def _active_lease_from_payload(
    username: str, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """按统一授权 payload 里的目标信息找一张生效中的租约。"""
    node = str(payload.get("node") or "")
    try:
        vmid = int(payload.get("vmid") or 0)
    except (TypeError, ValueError):
        return None
    if not node or vmid <= 0:
        return None
    return await aivm.active_lease_for(
        username, str(payload.get("connection_id") or ""), node, vmid
    )


@router.get("/models")
async def models(user: Dict[str, Any] = Depends(VIEW)) -> Dict[str, Any]:
    """已接入的模型清单（不含密钥），**所有能排查的用户都能看**。

    放在这个权限档是有意的：用户需要知道平台接了哪些模型，才能判断该不该
    指望 AI 帮上忙。管理员那份配置（含各家 Base URL）仍然只有管理员能读。
    """
    return ai.public_models(
        await ai.load_config(),
        username=str(user.get("username") or ""),
        is_admin=_is_admin(user),
    )


@router.get("/capabilities")
async def capabilities(user: Dict[str, Any] = Depends(VIEW)) -> Dict[str, Any]:
    """这个助手能做什么：工具名 + 描述 + 步数上限。

    按当前用户的生效权限过滤（没有 ``ai.exec`` 就没有「改动系统的命令」，
    没有 ``ssh.view`` 就没有 SSH 登录分析）；命令白名单 / 参数黑名单这类
    判定规则留在服务端，不下发前端（V2-04）。
    """
    return ai.capabilities(security.effective_permissions(user))


@router.get("/my-providers")
async def my_providers(user: Dict[str, Any] = Depends(VIEW)) -> Dict[str, Any]:
    """我自己的模型（外加平台共享的，只读）。

    普通用户靠这个入口维护自己的模型 —— 用自己的 key、花自己的额度，也不会
    看见别人配了什么。管理员请用 ``/config``，那份能看见并管理全平台的。
    """
    # 带上用户名：分析窗口与单次 token 上限要回他**自己**设的值，而不是平台默认
    cfg = await ai.load_config(str(user.get("username") or ""))
    return {
        **ai.user_config(cfg, str(user.get("username") or "")),
        "presets": ai.provider_presets(),
        # 个人不能建「全局共享」：那会把自己的凭据推给所有人用
        "can_share": False,
    }


@router.put("/my-providers")
async def save_my_providers(
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """保存自己的模型。归属由后端强制盖上，前端传什么都不作数。"""
    username = str(user.get("username") or "")
    result = await ai.save_user_providers(username, payload)
    await security.audit(
        request,
        user,
        "ai.providers.save",
        "ai_config",
        "success",
        f"保存个人模型 {len(result.get('providers') or [])} 项",
    )
    return result


@router.get("/sessions")
async def sessions(
    limit: int = 20,
    offset: int = 0,
    host_id: str = "",
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """排查记录（工具调用明细的入口）。

    普通用户只看到自己的记录；管理员能看全部 —— 这是"平台代用户在他的服务器上
    执行过命令"的账，谁都能查自己那份，管理员能查所有。
    """
    username = "" if _is_admin(user) else str(user.get("username") or "")
    return await aiaudit.list_sessions(
        limit=limit, offset=offset, username=username, host_id=host_id
    )


@router.get("/sessions/{session_id}")
async def session_detail(
    session_id: str,
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """单次排查的详情：会话信息 + 每一步工具调用。"""
    record = await aiaudit.get_session(session_id)
    if not record:
        raise HTTPException(status_code=404, detail=i18n.tr("找不到这条排查记录"))
    # 越权检查：普通用户只能看自己的
    if not _is_admin(user) and str(record.get("username") or "") != str(
        user.get("username") or ""
    ):
        raise HTTPException(status_code=403, detail=i18n.tr("找不到这条排查记录"))
    return {"session": record, "calls": await aiaudit.session_calls(session_id)}


@router.get("/usage")
async def usage(
    days: int = 7,
    host_id: str = "",
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """AI 用量统计（token 消耗）：窗口内的总量 + 按模型 / 按天明细。

    归属口径与排查记录页一致 —— 管理员看全平台，普通用户只看自己的。消耗是
    花在谁头上的成本，与「记录页能翻到谁」必须是同一个答案。
    """
    username = "" if _is_admin(user) else str(user.get("username") or "")
    return await aiaudit.usage_stats(username=username, host_id=host_id, days=days)


@router.post("/inspect")
async def inspect(
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """跑一次排查。

    最坏要等一轮远端 SSH 体检 + 一次模型调用（十几秒到一分钟），所以前端
    需要自己显示进度，不要依赖这个接口做流式输出 —— L1 先把结论跑通，
    流式留给 L2 的 Agent 循环。
    """
    host_id = str(payload.get("host_id") or "local")
    provider_id = str(payload.get("provider_id") or "")
    allowed = await hostscope.allowed_host_ids(user)

    try:
        async with _inspect_semaphore():
            result = await ai.inspect(
                host_id,
                allowed,
                provider_id=provider_id,
                username=str(user.get("username") or ""),
                is_admin=_is_admin(user),
                perms=security.effective_permissions(user),
            )
    except ai.AIError as exc:
        # 越权、没配模型、上游报错都在这里 —— 都是可预期的业务错误，回 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await security.audit(
        request,
        user,
        "ai.inspect",
        result.get("host_name") or host_id,
        "success",
        "AI 排查 "
        + str(result.get("host_name") or host_id)
        + "："
        + str(len((result.get("result") or {}).get("findings") or []))
        + " 条结论，"
        + str((result.get("usage") or {}).get("total_tokens") or 0)
        + " tokens",
    )
    return result


@router.post("/inspect/stream")
async def inspect_stream(
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(VIEW),
) -> StreamingResponse:
    """L2：带工具调用的排查，边跑边推过程。

    为什么不用 ``EventSource``：它只能发 GET，而我们要传 body（host_id）、
    还要能带 CSRF 之外的凭据 —— 用 POST + 流式响应，前端拿 ``fetch`` 读
    ``ReadableStream`` 就行。

    事件类型（每行一个 ``data:`` JSON）：

    * ``stage``      —— 阶段切换（collect）；
    * ``thinking``   —— 第 N 轮开始；
    * ``tool_start`` —— 要调某个工具了（工具名 + 参数）；
    * ``tool_done``  —— 工具返回（成功与否 + 输出预览 + 耗时）；
    * ``done``       —— 最终结果（结构与 L1 一致，前端共用渲染）；
    * ``error``      —— 可预期的失败（没配模型、越权、上游报错）。

    过程事件是给用户「看得见」用的：一次排查要十几秒到一分钟，没有实时反馈
    用户会以为页面卡死。
    """
    host_id = str(payload.get("host_id") or "local")
    provider_id = str(payload.get("provider_id") or "")
    # 授权执行：用户勾了「授权 AI 在目标主机执行命令」，且本人有 ai.exec 权限。
    # 权限位与授权要同时成立 —— 勾选框不是授权本身，它只是一次显式确认。
    allow_exec = bool(payload.get("allow_exec"))
    if allow_exec and not security.has_user_permission(user, "ai.exec"):
        raise HTTPException(
            status_code=403,
            detail=i18n.tr("没有「授权 AI 上机执行」的权限"),
        )
    exec_access = _exec_access(payload, allow_exec)
    allow_write = allow_exec and exec_access == "write"
    allowed = await hostscope.allowed_host_ids(user)
    username = str(user.get("username") or "")
    # 权限清单一并带进 AI 层：SSH 登录分析这类**全局**数据要据此决定给不给
    # （见 ai.collect_evidence 的说明）—— 排查本身的门槛比 SSH 安全页低。
    perms = security.effective_permissions(user)
    queue: "asyncio.Queue[Any]" = asyncio.Queue()

    # 首字延迟从「请求进入端点」算起，包含排队时间 —— 这才是用户实际等到的时长
    perf_started = time.perf_counter()
    last_disconnect_check = 0.0

    async def emit(event: Dict[str, Any]) -> None:
        # 用户关了页面或点了「终止」：连接已断，再往下跑只是白烧 token。
        # is_disconnected() 是一次 ASGI receive 轮询：流式下每 token 都做，等于
        # 每个字都多一次往返。降到 500ms 一次；结束类事件立即检查，用户点
        # 「终止」最迟半秒生效。抛出去让 agent 循环停下（见 StopInspection）。
        nonlocal last_disconnect_check
        kind = str(event.get("type") or "")
        if kind in ("done", "error", "aborted", "approval"):
            check_now = True
        else:
            check_now = time.monotonic() - last_disconnect_check >= 0.5
        if check_now:
            last_disconnect_check = time.monotonic()
            if await request.is_disconnected():
                raise ai.StopInspection()
        # 首个 token（或报告模式的第一条生成进度）进 SSE 队列的时刻 = 用户感知的「首字」
        if kind in ("token", "report_progress"):
            aiperf.set_once("ttfb_ms", (time.perf_counter() - perf_started) * 1000)
        await queue.put(event)

    def _finish_perf(session: str, status: str) -> Dict[str, Any]:
        """收口计时：写 ai_perf 日志并返回快照，交给 finish_session 落库。"""
        return aiperf.log_session(
            session,
            status=status,
            total_ms=(time.perf_counter() - perf_started) * 1000,
        )

    async def worker() -> None:
        session_id = ""
        started = time.time()
        host_name = host_id
        lane = _lane_for(provider_id)
        sem = _lane_semaphore(lane)
        reserved = False
        # 分段秒表随 worker 这个 task 传播：emit / Agent 循环 / 会话层记的是同一份
        aiperf.start()
        queued_at = 0.0
        acquired_at = 0.0
        try:
            # 并发到顶时先告诉前端「排队中 + 前面还有几次」，否则用户对着转圈会
            # 以为页面卡死，或者不知道是「再等几秒」还是「先干别的去」
            if sem.locked() or _global_semaphore().locked():
                reserved = True
                queued_at = time.perf_counter()
                await queue.put(_queued_event(lane))
            async with _turn_lane(lane):
                acquired_at = time.perf_counter()
                if queued_at:
                    aiperf.add("queue_ms", (acquired_at - queued_at) * 1000)
                host = await ai.resolve_host(host_id, allowed)
                host_name = str(host.get("name") or host_id)

                session_id = await aiaudit.open_session(
                    username=username,
                    host_id=host_id,
                    host_name=host_name,
                    provider="",
                    mode="agent",
                )
                result = await ai.inspect_agent(
                    host_id,
                    allowed,
                    emit=emit,
                    provider_id=provider_id,
                    username=username,
                    is_admin=_is_admin(user),
                    allow_exec=allow_exec,
                    allow_write=allow_write,
                    perms=perms,
                )

                calls = result.get("tool_calls") or []
                await aiaudit.record_calls(session_id, calls)
                await aiaudit.finish_session(
                    session_id,
                    status="done",
                    model=str(result.get("model") or ""),
                    steps=int(result.get("steps") or 0),
                    tool_calls=len(calls),
                    usage=result.get("usage") or {},
                    duration_ms=int((time.time() - started) * 1000),
                    # 结论也落库：否则回看记录只有「跑了什么」，没有「结论是什么」
                    result=result.get("result"),
                    metrics=_finish_perf(session_id, "done"),
                )
                await queue.put({"type": "done", "result": result, "session_id": session_id})
                await security.audit(
                    request,
                    user,
                    "ai.inspect",
                    host_name,
                    "success",
                    "AI 排查（工具调用"
                    + ("，已授权上机执行" if allow_exec else "")
                    + "）"
                    + host_name
                    + "："
                    + str(len(calls))
                    + " 次工具调用，"
                    + str(len((result.get("result") or {}).get("findings") or []))
                    + " 条结论",
                )
        except ai.StopInspection:
            # 用户终止：不算失败。但会话必须收口 —— 否则排查记录里会留一条
            # 永远「进行中」的记录，事后分不清是真卡死了还是人为停掉的。
            if session_id:
                await aiaudit.finish_session(
                    session_id,
                    status="cancelled",
                    metrics=_finish_perf(session_id, "cancelled"),
                )
            await queue.put({"type": "aborted"})
        except ai.AIError as exc:
            if session_id:
                await aiaudit.finish_session(
                    session_id,
                    status="failed",
                    error=str(exc),
                    metrics=_finish_perf(session_id, "failed"),
                )
            await queue.put({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 兜底：异常也要让前端收到结束信号
            logger.exception("AI 排查异常")
            if session_id:
                await aiaudit.finish_session(
                    session_id,
                    status="failed",
                    error=str(exc)[:300],
                    metrics=_finish_perf(session_id, "failed"),
                )
            await queue.put({"type": "error", "message": str(exc)[:300]})
        finally:
            if acquired_at:
                # 只统计真正跑起来的耗时，排队时间不混进去 —— 否则别人排队久了
                # 会把这个 provider 的 ETA 估计整体带偏
                _record_duration(lane, (time.perf_counter() - acquired_at) * 1000)
            if reserved:
                _leave_queue(lane)
            await queue.put(None)  # 结束哨兵

    async def gen():
        task = asyncio.create_task(worker())
        try:
            while True:
                try:
                    # 心跳：上游卡住 / 长工具执行时 SSE 也要有字节流出来，否则
                    # nginx / 浏览器 / 前端的读超时会把它当成断线（用户只看到
                    # 一直转圈，却没有任何错误提示）。15 秒远小于这些超时。
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if event is None:
                    break
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # 面板前面有 nginx：不关缓冲的话事件会被攒着一起发，流式就没意义了
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/approval")
async def resolve_approval(
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """批准 / 拒绝 AI 提议的一条写命令（逐条确认）。

    与排查是两条连接：排查挂在 SSE 上等，这里用一次普通 POST 把它唤醒。
    权限与授权一致（``ai.exec``）—— 能授权上机执行的人才谈得上逐条批准。
    """
    approval_id = str(payload.get("approval_id") or "")
    approved = bool(payload.get("approved"))
    if not approval_id:
        raise HTTPException(status_code=400, detail=i18n.tr("缺少 approval_id"))

    # 审批必须由**发起人**拍板。原实现只校验 ai.exec，于是任何持权者拿到
    # （或猜到）别人的 approval_id 就能替别人批准一条写命令 —— 而 approval_id
    # 是推送里明文下发的，这不是理论风险。
    owner = ai_approval.owner_of(approval_id)
    username = str(user.get("username") or "")
    if owner and owner != username and not _is_admin(user):
        raise HTTPException(
            status_code=403, detail=i18n.tr("只能批准自己发起的操作")
        )

    # 宿主机命令的审批要 ai.exec；虚拟机内动作的审批要 ai.vm.write —— 两者不能
    # 互相顶替，否则「只能进虚拟机改配置」的人也能替宿主机写命令拍板。
    if not (
        security.has_user_permission(user, "ai.exec")
        or security.has_user_permission(user, "ai.vm.write")
    ):
        raise HTTPException(
            status_code=403,
            detail=i18n.tr("没有「授权 AI 上机执行」的权限"),
        )

    verdict = ai_approval.resolve(
        approval_id, approved, by=str(user.get("username") or "")
    )
    hit = bool(verdict.get("hit"))
    # 一次「点了批准却什么也没发生」最难查的地方就在这里：HTTP 一律 200，
    # 真正决定成败的是 hit（这个 id 还在不在等待队列里）。留一行日志，
    # 事后能区分「没批到」与「批到了但执行失败」。
    logger.info(
        "审批请求：id=%s approved=%s hit=%s by=%s",
        approval_id,
        approved,
        hit,
        username,
    )
    # 高危动作可能需要两个人：第一票只是记账，界面上要显示「1/2」，
    # 审计里也要留下「谁在第几步批的」——不然事后看不出是谁放行的。
    tally = (
        f"（{verdict.get('count')}/{verdict.get('required')}）"
        if hit and not verdict.get("done")
        else ""
    )
    await security.audit(
        request,
        user,
        "ai.approval",
        target=approval_id,
        result="success" if hit else "failed",
        detail=("批准" if approved else "拒绝")
        + tally
        + ("执行写命令" if hit else "（请求已失效或超时）"),
    )
    return {"ok": hit, **verdict}


# ------------------------------------------------------- 虚拟机内排查（租约）
# 授权不是「打开一个开关」，而是「给某台虚拟机签发一张限时票据」：范围（目录 /
# 可重启的服务）、时长、动作数上限都写在票面上，过期或撤销即失效。这样才谈得上
# 「显式授权」，而不是给 AI 一把常开的钥匙。

VM_READ = "ai.vm.read"
VM_WRITE = "ai.vm.write"


async def _require_own_lease(lease_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
    """取一张属于调用者的租约。越权按 404 处理，不确认它是否存在。"""
    lease = await aivm.get_lease(lease_id)
    username = str(user.get("username") or "")
    if not lease or (lease.get("username") != username and not _is_admin(user)):
        raise HTTPException(status_code=404, detail=i18n.tr("找不到这条授权"))
    return lease


@router.get("/vm/targets")
async def vm_targets(user: Dict[str, Any] = Depends(VIEW)) -> Dict[str, Any]:
    """可授权的虚拟机清单（当前连接，排除模板）。"""
    if not security.has_user_permission(user, VM_READ):
        raise HTTPException(
            status_code=403, detail=i18n.tr("没有「虚拟机内取证」的权限")
        )
    items = await aivm.list_targets()
    # 只列自己管得到的机器：普通用户看不到别人的机器名，否则授权面板成了资产清单
    visible = []
    for item in items:
        try:
            await guestguard.require_guest_object(
                user,
                item["node"],
                item["vmid"],
                ownership.KIND_CT if item["guest_type"] == "lxc" else ownership.KIND_VM,
            )
        except HTTPException:
            continue
        visible.append(item)
    return {"items": visible}


@router.get("/vm/agent")
async def vm_agent(
    node: str, vmid: int, user: Dict[str, Any] = Depends(VIEW)
) -> Dict[str, Any]:
    """选中机器后确认 Guest Agent 是否应答（= 能不能纳入安全管控）。

    单独一个轻量接口：签发前探测、失败时给出原因，比「签完发现每条命令都
    超时」有用得多。只 ping，不执行任何命令。
    """
    if not security.has_user_permission(user, VM_READ):
        raise HTTPException(
            status_code=403, detail=i18n.tr("没有「虚拟机内取证」的权限")
        )
    if not node or vmid <= 0:
        raise HTTPException(status_code=400, detail=i18n.tr("缺少虚拟机标识"))
    await guestguard.require_guest_object(user, node, vmid, ownership.KIND_VM)
    return {"ok": await aivm.probe_agent(node, vmid)}


@router.get("/vm/leases")
async def vm_lease_list(user: Dict[str, Any] = Depends(VIEW)) -> Dict[str, Any]:
    """最近的授权记录：管理员看全部，其他人只看自己的。"""
    if not (
        security.has_user_permission(user, VM_READ)
        or security.has_user_permission(user, VM_WRITE)
    ):
        raise HTTPException(
            status_code=403, detail=i18n.tr("没有「虚拟机内取证」的权限")
        )
    username = "" if _is_admin(user) else str(user.get("username") or "")
    items = await aivm.list_leases(username)
    return {"items": [aivm.public_lease(item) for item in items]}


@router.post("/vm/leases")
async def vm_lease_create(
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """签发一张租约。写能力需要 ``ai.vm.write``，只读只需要 ``ai.vm.read``。"""
    username = str(user.get("username") or "")
    node = str(payload.get("node") or "")
    try:
        vmid = int(payload.get("vmid") or 0)
    except (TypeError, ValueError):
        vmid = 0
    if not node or vmid <= 0:
        raise HTTPException(status_code=400, detail=i18n.tr("缺少虚拟机标识"))

    fs_rw = [str(item) for item in (payload.get("fs_rw") or [])]
    units = [str(item) for item in (payload.get("units") or [])]
    # 授权级别由调用方显式选择（read / write / ops）。旧客户端没传时按原来的语义
    # 推断：给了目录或服务就是受限变更，否则只读 —— 不能默认成 ops。
    access = str(payload.get("access") or "").lower()
    if access not in (aivm.ACCESS_READ, aivm.ACCESS_WRITE, aivm.ACCESS_OPS):
        access = aivm.ACCESS_WRITE if (fs_rw or units) else aivm.ACCESS_READ
    wants_write = access != aivm.ACCESS_READ

    if not security.has_user_permission(user, VM_READ):
        raise HTTPException(
            status_code=403, detail=i18n.tr("没有「虚拟机内取证」的权限")
        )
    if wants_write and not security.has_user_permission(user, VM_WRITE):
        raise HTTPException(
            status_code=403, detail=i18n.tr("没有「虚拟机内变更」的权限")
        )

    guest_type = "lxc" if str(payload.get("guest_type") or "") == "lxc" else "qemu"
    if guest_type == "lxc":
        # 容器的执行要走节点侧 pct，写入更没有 Guest Agent 的文件接口可用（回滚
        # 也就无从谈起）。与其发一张注定执行失败的票据，不如当场说清楚。
        raise HTTPException(
            status_code=400,
            detail=i18n.tr("容器暂不支持 AI 内联排查，请对虚拟机授权"),
        )
    kind = ownership.KIND_CT if guest_type == "lxc" else ownership.KIND_VM
    connection_id = str(payload.get("connection_id") or "")
    await guestguard.require_guest_object(
        user, node, vmid, kind, connection=connection_id or None
    )

    # 同一台机器上已有生效租约就复用：反复点「授权」不该刷出一堆票据，
    # 否则撤销时用户不知道该撤哪一张。
    existing = await aivm.active_lease_for(username, connection_id, node, vmid)
    if existing:
        # 级别或范围不同的票不能「复用」：用户点的是「运维授权」，拿回一张只读票，
        # 表现就是「授权了还是什么都不能做」；范围不同更危险 —— 用户以为新加了
        # /etc，实际拿回的是旧票的 /tmp。两种情况都必须显式撤销旧票再签新票。
        current = str(existing.get("access") or aivm.ACCESS_WRITE)
        if current != access or not _lease_scope_matches(
            existing, fs_rw=fs_rw, units=units
        ):
            raise HTTPException(
                status_code=409,
                detail=i18n.pick(
                    f"这台机器已有一张「{aivm.ACCESS_LABELS.get(current, current)}」级别的授权，"
                    "范围和本次选择不一致。请先在「生效中的授权」列表里撤销它，再签发新授权",
                    f"This VM already has a “{aivm.ACCESS_LABELS_EN.get(current, current)}” "
                    "grant whose scope differs from this request. Revoke it in the "
                    "“Active grants” list first, then issue the new one.",
                ),
            )
        return {"lease": aivm.public_lease(existing), "reused": True}

    # 纳管的前提是 Guest Agent 在线：没有它连不上客户机，票签出来也是死票。
    # 这一点在签发时再确认一次 —— 列表加载与点「签发」之间机器可能已经停了。
    if not await aivm.probe_agent(node, vmid):
        raise HTTPException(
            status_code=400,
            detail=i18n.pick(
                "这台虚拟机未运行，或没有响应 Guest Agent。请确认客户机已安装并"
                "启动 qemu-guest-agent、虚拟机配置中 agent 已启用；未经纳管的机器"
                "不能签发授权。",
                "This VM is not running or is not answering the QEMU Guest Agent. "
                "Make sure qemu-guest-agent is installed and running inside the guest, "
                "and agent is enabled in the VM config. Unmanaged VMs cannot be granted.",
            ),
        )

    try:
        minutes = int(payload.get("minutes") or aivm.DEFAULT_MINUTES)
    except (TypeError, ValueError):
        minutes = aivm.DEFAULT_MINUTES
    try:
        actions_max = int(payload.get("actions_max") or aivm.DEFAULT_ACTIONS)
    except (TypeError, ValueError):
        actions_max = aivm.DEFAULT_ACTIONS

    # 客户机是 Linux 还是 Windows、带了什么标签 —— 这两项决定路径语义、命令白名单
    # 与策略（prod 标签禁写）。取不到就按 Linux + 无标签处理（见 describe_guest）。
    info = await aivm.describe_guest(node, vmid, guest_type)
    # 面板侧的人工标注优先：导入 / 手工建的机器常把 PVE 的 ostype 停在 win*，
    # 运维在「备注」里确认过的系统比它可信。只映射大类 —— 发行版细节不影响
    # 这里的路径语义与命令白名单。
    meta_os = await vm_meta_router.os_of(node, vmid, connection_id or None)
    if meta_os:
        info["os_type"] = "windows" if meta_os == "windows" else "linux"
    # 沙箱模式在签发时探测一次并固化：中途装上/卸掉 diagd 不该让同一次排查的
    # 隔离等级悄悄变化。
    probe = {
        "id": "",
        "node": node,
        "vmid": vmid,
        "guest_type": guest_type,
        "os_type": info["os_type"],
        "mode": "compat",
    }
    mode = await aivm.detect_mode(probe)

    try:
        lease = await aivm.create_lease(
            username=username,
            node=node,
            vmid=vmid,
            vm_name=str(payload.get("vm_name") or "") or info["name"],
            connection_id=connection_id,
            guest_type=guest_type,
            fs_ro=[str(item) for item in (payload.get("fs_ro") or [])] or None,
            fs_rw=fs_rw,
            units=units,
            risk_max=aivm.RISK_CONFIG if wants_write else aivm.RISK_READ,
            minutes=minutes,
            actions_max=actions_max,
            reason=str(payload.get("reason") or ""),
            os_type=info["os_type"],
            tags=info["tags"],
            mode=mode,
            access=access,
        )
    except aivm.ToolDenied as exc:
        # 策略拒绝（例如 prod 标签禁写）：4xx 而不是 500 —— 这是规则，不是故障
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 可写/运维级别的票先打快照：运维授权下 AI 是跑包管理器的，文件级备份覆盖不到。
    # 快照失败不阻挡授权（存储可能不支持），票面上如实留空。
    if access != aivm.ACCESS_READ:
        await aivm.prepare_snapshot(lease)

    await security.audit(
        request,
        user,
        "ai.vm.grant",
        target=f"{node}/{vmid}",
        result="success",
        detail=(
            f"授权 AI 进入虚拟机 {lease['vm_name'] or vmid}："
            f"{minutes} 分钟，级别 {aivm.ACCESS_LABELS.get(access, access)}，"
            + (
                "整机（装包 / 改配置 / 重启服务，逐条审批）"
                if access == aivm.ACCESS_OPS
                else ("可写 " + "、".join(lease["fs_rw"]) if lease["fs_rw"] else "仅只读")
                + ("；可重启 " + "、".join(lease["units"]) if lease["units"] else "")
            )
        ),
    )
    return {"lease": aivm.public_lease(lease), "reused": False}


@router.get("/vm/leases/{lease_id}")
async def vm_lease_detail(
    lease_id: str, user: Dict[str, Any] = Depends(VIEW)
) -> Dict[str, Any]:
    """租约详情 + 完整动作流水（谁授权、做了什么、是否被审批、结果如何）。"""
    lease = await _require_own_lease(lease_id, user)
    return {
        "lease": aivm.public_lease(lease),
        "actions": await aivm.list_actions(lease_id),
    }


@router.post("/vm/leases/{lease_id}/revoke")
async def vm_lease_revoke(
    lease_id: str,
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """撤销授权，并尝试终止客户机内仍在执行的命令。"""
    lease = await _require_own_lease(lease_id, user)
    outcome = await aivm.revoke_lease(
        lease_id, reason=str(payload.get("reason") or "")
    )
    await security.audit(
        request,
        user,
        "ai.vm.revoke",
        target=f"{lease.get('node')}/{lease.get('vmid')}",
        result="success",
        detail=f"撤销 AI 虚拟机授权（{lease.get('vm_name') or lease.get('vmid')}）",
    )
    refreshed = await aivm.get_lease(lease_id)
    return {
        "lease": aivm.public_lease(refreshed or lease),
        # 兼容模式下没有终止开关，前端要如实告诉用户「那条命令会自己跑完」
        "kill": outcome.get("kill") or {},
    }


@router.post("/authorization/grant")
async def authorization_grant(
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """统一授权：一次确认同时处理主机命令与虚拟机票据。

    主机是会话级布尔值，虚拟机是限时票据；两者生命周期不同，但用户决定只做一次。
    这里先校验、再签票、最后落主机标志；主机标志写失败时补偿撤销刚新建的票，
    避免留下「有票但页面不知道」的半成品。
    """
    username = str(user.get("username") or "")
    is_admin = _is_admin(user)
    want_host = bool(payload.get("host"))
    want_vm = bool(payload.get("vm"))
    exec_access = _exec_access(payload, want_host)
    host_id = str(payload.get("host_id") or "")
    vm_payload = payload.get("vm_grant")
    if not isinstance(vm_payload, dict):
        vm_payload = {}

    if want_host:
        if not security.has_user_permission(user, "ai.exec"):
            raise HTTPException(
                status_code=403,
                detail=i18n.tr("没有「授权 AI 上机执行」的权限"),
            )
        if not host_id:
            raise HTTPException(status_code=400, detail=i18n.tr("缺少目标主机"))
        try:
            await ai.resolve_host(host_id, await hostscope.allowed_host_ids(user))
        except ai.AIError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    conversation_id = str(payload.get("conversation_id") or "")
    if conversation_id:
        conv = await aichat.get_conversation(conversation_id)
        if not conv or not aichat.owns(conv, username, is_admin):
            raise HTTPException(status_code=404, detail=i18n.tr("找不到这条会话"))
    else:
        host_name = str(payload.get("host_name") or host_id)
        if not host_name:
            raise HTTPException(
                status_code=400, detail=i18n.tr("缺少目标主机或虚拟机")
            )
        conv = await aichat.create_conversation(
            username=username,
            host_id=host_id,
            host_name=host_name,
            allow_exec=want_host,
            exec_access=exec_access,
        )
        conversation_id = str(conv.get("id") or "")

    lease: Optional[Dict[str, Any]] = None
    reused = False
    replaced: Optional[Dict[str, Any]] = None
    revoked: Optional[Dict[str, Any]] = None
    warnings: List[str] = []

    def _remember_kill(outcome: Dict[str, Any]) -> None:
        kill = outcome.get("kill") or {}
        if kill and not kill.get("ok") and kill.get("output"):
            warnings.append(str(kill.get("output")))

    if want_vm:
        if not vm_payload:
            raise HTTPException(status_code=400, detail=i18n.tr("缺少虚拟机授权参数"))
        try:
            result = await vm_lease_create(vm_payload, request, user)
        except HTTPException as exc:
            # 已有不同级别 / 不同范围的票：统一面板已经让用户选过「撤销并重签」，
            # 这里按一次确认执行，不再把 409 丢回页面让用户点第二次。
            if exc.status_code == 409 and bool(payload.get("replace_existing")):
                existing = await _active_lease_from_payload(username, vm_payload)
                if existing:
                    _remember_kill(
                        await aivm.revoke_lease(
                            str(existing["id"]),
                            reason=i18n.tr("统一授权调整级别 / 范围，撤销旧票据"),
                        )
                    )
                    refreshed = await aivm.get_lease(str(existing["id"]))
                    replaced = aivm.public_lease(refreshed or existing)
                result = await vm_lease_create(vm_payload, request, user)
            else:
                raise
        lease = result.get("lease")
        reused = bool(result.get("reused"))
    elif vm_payload:
        # 用户在新面板里取消了虚拟机通道：撤销该目标上仍生效的票，避免「页面
        # 显示未授权、AI 还能拿旧 lease_id 进去」。
        existing = await _active_lease_from_payload(username, vm_payload)
        if existing:
            _remember_kill(
                await aivm.revoke_lease(
                    str(existing["id"]),
                    reason=i18n.tr("统一授权调整为不启用虚拟机通道"),
                )
            )
            refreshed = await aivm.get_lease(str(existing["id"]))
            revoked = aivm.public_lease(refreshed or existing)

    if not await aichat.set_allow_exec(
        conversation_id, want_host, exec_access=exec_access
    ):
        # 主机标志落库失败：把刚新建的票撤掉，保持两条通道一致；复用旧票不能撤。
        if lease and not reused:
            await aivm.revoke_lease(
                str(lease.get("id") or ""),
                reason=i18n.tr("主机授权落库失败，回滚新建票据"),
            )
        raise HTTPException(
            status_code=500, detail=i18n.tr("授权状态保存失败，请重试")
        )

    channels = []
    if want_host:
        channels.append("主机命令(" + exec_access + ")")
    if want_vm and lease:
        channels.append("虚拟机内(" + str(lease.get("access") or "read") + ")")
    if not want_vm and revoked:
        channels.append("撤销虚拟机内")
    detail = "统一授权：" + ("、".join(channels) if channels else "仅更新状态")
    await security.audit(
        request,
        user,
        "ai.authorization.grant",
        target=conversation_id,
        result="success",
        detail=detail,
    )
    refreshed_conv = await aichat.get_conversation(conversation_id)
    return {
        "conversation": refreshed_conv or conv,
        "host": {
            "authorized": want_host,
            "scope": "session",
            "access": exec_access,
        },
        "lease": lease,
        "reused": reused,
        "replaced": replaced,
        "revoked": revoked,
        "warnings": warnings,
    }


@router.post("/authorization/renew")
async def authorization_renew(
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """续期虚拟机票据：只延长时间，不改变级别、范围与已用动作数。"""
    lease_id = str(payload.get("lease_id") or "")
    if not lease_id:
        raise HTTPException(status_code=400, detail=i18n.tr("缺少授权标识"))
    lease = await _require_own_lease(lease_id, user)
    try:
        minutes = int(payload.get("minutes") or aivm.DEFAULT_MINUTES)
    except (TypeError, ValueError):
        minutes = aivm.DEFAULT_MINUTES
    renewed = await aivm.renew_lease(
        lease_id,
        minutes=minutes,
        username=str(user.get("username") or ""),
        is_admin=_is_admin(user),
    )
    if renewed is None:
        raise HTTPException(
            status_code=409,
            detail=i18n.tr("这条虚拟机授权已过期或已撤销，请重新授权"),
        )
    await security.audit(
        request,
        user,
        "ai.authorization.renew",
        target=f"{lease.get('node')}/{lease.get('vmid')}",
        result="success",
        detail=f"续期 AI 虚拟机授权（{lease.get('vm_name') or lease.get('vmid')}）",
    )
    return {"lease": aivm.public_lease(renewed)}


@router.post("/authorization/revoke")
async def authorization_revoke(
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """撤销统一授权：主机、虚拟机可分别撤销，也可一次撤销全部。"""
    want_host = bool(payload.get("host"))
    want_vm = bool(payload.get("vm"))
    conversation_id = str(payload.get("conversation_id") or "")
    host_updated = True
    if want_host and conversation_id:
        conv = await aichat.get_conversation(conversation_id)
        if not conv or not aichat.owns(
            conv, str(user.get("username") or ""), _is_admin(user)
        ):
            raise HTTPException(status_code=404, detail=i18n.tr("找不到这条会话"))
        host_updated = await aichat.set_allow_exec(conversation_id, False)

    lease: Optional[Dict[str, Any]] = None
    kill: Dict[str, Any] = {}
    if want_vm:
        lease_id = str(payload.get("lease_id") or "")
        if not lease_id:
            vm_payload = payload.get("vm_grant")
            if not isinstance(vm_payload, dict):
                vm_payload = payload
            existing = await _active_lease_from_payload(
                str(user.get("username") or ""), vm_payload
            )
            lease_id = str(existing.get("id") or "") if existing else ""
        if lease_id:
            current = await _require_own_lease(lease_id, user)
            outcome = await aivm.revoke_lease(
                lease_id,
                reason=str(payload.get("reason") or i18n.tr("用户撤销统一授权")),
            )
            kill = outcome.get("kill") or {}
            refreshed = await aivm.get_lease(lease_id)
            lease = aivm.public_lease(refreshed or current)

    await security.audit(
        request,
        user,
        "ai.authorization.revoke",
        target=conversation_id or (lease_id if want_vm else ""),
        result="success" if host_updated else "partial",
        detail="撤销统一授权："
        + ("主机命令" if want_host else "")
        + ("、" if want_host and want_vm else "")
        + ("虚拟机内" if want_vm else ""),
    )
    return {"host_updated": host_updated, "lease": lease, "kill": kill}


@router.post("/vm/leases/{lease_id}/kill")
async def vm_lease_kill(
    lease_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """立即终止该授权在客户机内仍在运行的进程（不撤销授权）。

    单独一个动作是有必要的：排查到一半发现某条 ``journalctl -f`` 还在刷，
    用户想停掉的只是那一条，不是整张票。
    """
    lease = await _require_own_lease(lease_id, user)
    result = await aivm.kill_lease(lease)
    await security.audit(
        request,
        user,
        "ai.vm.kill",
        target=f"{lease.get('node')}/{lease.get('vmid')}",
        result="success" if result.get("ok") else "failed",
        detail="终止客户机内运行中的命令：" + str(result.get("output") or "")[:150],
    )
    return result


@router.get("/vm/diagd")
async def vm_diagd_script(user: Dict[str, Any] = Depends(VIEW)) -> Dict[str, Any]:
    """客户机内受限执行器的安装脚本。

    为什么把脚本源码交给前端而不是让用户去翻仓库：这东西必须和面板**同一版本**
    才有意义（参数格式、kill 语义都在变），跟着面板一起下发最省事。
    """
    if not security.has_user_permission(user, VM_READ):
        raise HTTPException(
            status_code=403, detail=i18n.tr("没有「虚拟机内取证」的权限")
        )
    return {
        "version": aivm_diagd.VERSION,
        "script": aivm_diagd.SCRIPT,
        "hint": aivm_diagd.INSTALL_HINT,
    }


@router.get("/vm/policy")
async def vm_policy_get(user: Dict[str, Any] = Depends(VIEW)) -> Dict[str, Any]:
    """虚拟机授权策略（谁能被写、要不要双人批、快照留多久）。"""
    return {"policy": await aivm.policy(), "defaults": aivm.DEFAULT_POLICY}


@router.put("/vm/policy")
async def vm_policy_put(
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """改策略（仅管理员）。**非法值一律拒掉**而不是夹到边界：

    这几项直接决定「生产机器能不能被写」，静默纠正会让人以为配置生效了，
    实际却是另一个值 —— 这种事发生在安全开关上最危险。
    """
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail=i18n.tr("只有管理员能改策略"))

    current = await aivm.policy()
    merged = dict(current)

    if "max_minutes" in payload:
        try:
            value = int(payload["max_minutes"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=i18n.tr("授权时长必须是整数"))
        if not 5 <= value <= 480:
            raise HTTPException(
                status_code=400, detail=i18n.tr("授权时长需在 5–480 分钟之间")
            )
        merged["max_minutes"] = value

    for key in ("double_approve", "auto_snapshot"):
        if key in payload:
            merged[key] = bool(payload[key])

    # 「允许运维授权」是这张策略里最宽的一档开关：关掉之后连管理员也不能再签发
    # 整机 shell 的票。已有票据不受影响（级别在签发时就固化在票面上）。
    if "allow_ops" in payload:
        merged["allow_ops"] = bool(payload["allow_ops"])

    if "keep_snapshot_hours" in payload:
        try:
            hours = int(payload["keep_snapshot_hours"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=i18n.tr("快照保留时长必须是整数"))
        if not 1 <= hours <= 24 * 30:
            raise HTTPException(
                status_code=400, detail=i18n.tr("快照保留时长需在 1–720 小时之间")
            )
        merged["keep_snapshot_hours"] = hours

    if "deny_write_tags" in payload:
        raw = payload["deny_write_tags"]
        if not isinstance(raw, list):
            raise HTTPException(status_code=400, detail=i18n.tr("禁写标签必须是列表"))
        tags: List[str] = []
        for item in raw:
            tag = str(item or "").strip()
            if not tag:
                continue
            if len(tag) > 32:
                raise HTTPException(status_code=400, detail=i18n.tr("标签过长（上限 32）"))
            if tag not in tags:
                tags.append(tag)
        merged["deny_write_tags"] = tags

    await store.set_setting(aivm.POLICY_KEY, json.dumps(merged, ensure_ascii=False))
    await security.audit(
        request,
        user,
        "ai.vm.policy",
        target="ai_vm_policy",
        result="success",
        detail="更新虚拟机授权策略：" + json.dumps(merged, ensure_ascii=False)[:300],
    )
    return {"policy": merged}


@router.post("/vm/leases/{lease_id}/rollback")
async def vm_lease_rollback(
    lease_id: str,
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """回滚：按动作还原单个文件，或整机回到授权前打的快照。"""
    if not security.has_user_permission(user, VM_WRITE):
        raise HTTPException(
            status_code=403, detail=i18n.tr("没有「虚拟机内变更」的权限")
        )
    lease = await _require_own_lease(lease_id, user)
    if lease.get("status") != "active":
        # 到期后不给回滚是刻意的：租约的有效期就是「这段时间内的改动由我负责」。
        raise HTTPException(
            status_code=400, detail=i18n.tr("这条授权已失效，不能再回滚")
        )
    mode = "snapshot" if str(payload.get("mode") or "") == "snapshot" else "file"
    result = await aivm.rollback(
        lease, action_id=str(payload.get("action_id") or ""), mode=mode
    )
    await security.audit(
        request,
        user,
        "ai.vm.rollback",
        target=f"{lease.get('node')}/{lease.get('vmid')}",
        result="success" if result.get("ok") else "failed",
        detail=(
            f"回滚 {'快照' if mode == 'snapshot' else '文件'}："
            + str(payload.get("action_id") or "全部")
        ),
    )
    return result


# ------------------------------------------------------------------ 会话式对话
#
# 与上面 `/inspect/stream`（一次性排查）的分工：这里把「排查」放进一条**会话线程**
# 里，首轮体检产出报告、之后可以继续追问。线程落库，刷新或换设备都能接着聊。

@router.get("/playbooks")
async def playbooks(user: Dict[str, Any] = Depends(VIEW)) -> Dict[str, Any]:
    """排障预案清单（可插拔的提问模板）。

    只读、不含任何主机信息，因此与「能排查」同权限档即可。前端拿它渲染空会话
    的快捷入口 —— 想加一套新套路，改 ``aiplaybooks.py`` 就行，前端不用动。

    **只下发 key / 标题 / 一句提示**：发给模型的指令（``prompt``）由后端按
    key 拼接（见 ``aichat``），前端不需要也拿不到。把整段排查指令交给浏览器，
    等于把「助手会怎么查、查哪些路径 / 命令」提前交底（V2-04）。
    """
    return {"items": aiplaybooks.list_playbooks()}


@router.get("/conversations")
async def list_conversations(
    host_id: str = "",
    limit: int = 20,
    offset: int = 0,
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """我的会话列表（管理员看全部）。用于「继续上次的对话」与记录页的会话视图。"""
    username = "" if _is_admin(user) else str(user.get("username") or "")
    return await aichat.list_conversations(
        username=username, host_id=host_id, limit=limit, offset=offset
    )


@router.post("/conversations")
async def create_conversation(
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """开一条新会话。

    越权在动模型之前就挡住：``resolve_host`` 会确认这台主机在该用户的可见范围内，
    不合法直接 400 —— 不能等采集完证据才发现「你没权限看这台机器」。
    """
    host_id = str(payload.get("host_id") or "")
    allowed = await hostscope.allowed_host_ids(user)
    if host_id:
        try:
            host = await ai.resolve_host(host_id, allowed)
        except ai.AIError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        # 纯虚拟机目标：没有主机，会话靠后续的虚拟机租约工作。名称只需要用于
        # 记录与会话列表展示，真正的执行目标由票面决定。
        vm_name = str(payload.get("vm_name") or "").strip()
        if not vm_name:
            raise HTTPException(
                status_code=400, detail=i18n.tr("缺少目标主机或虚拟机")
            )
        host = {"id": "", "name": vm_name}

    # 会话级的「允许上机执行」授权：与逐轮 payload 同一道权限门
    allow_exec = bool(payload.get("allow_exec"))
    if allow_exec and not security.has_user_permission(user, "ai.exec"):
        raise HTTPException(
            status_code=403,
            detail=i18n.tr("没有「授权 AI 上机执行」的权限"),
        )

    conv = await aichat.create_conversation(
        username=str(user.get("username") or ""),
        host_id=host_id,
        host_name=str(host.get("name") or host_id),
        provider=str(payload.get("provider_id") or ""),
        allow_exec=allow_exec,
        exec_access=_exec_access(payload, allow_exec),
    )
    await security.audit(
        request,
        user,
        "ai.conversation",
        target=conv["id"],
        result="success",
        detail="新建 AI 会话：" + str(host.get("name") or host_id),
    )
    return conv


@router.get("/conversations/{conversation_id}")
async def conversation_detail(
    conversation_id: str,
    since_seq: int = Query(default=0, ge=0),
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """会话详情：元信息 + 消息 + 最近一次运行的元信息。

    ``since_seq`` 只返回 seq 更大的消息 —— 一轮结束后页面只需要新追加的那几条，
    不必把整条会话重新拉一遍（长会话那一下会被用户直接感觉到）。
    越权按 404 处理（而不是 403）—— 不向非本人确认这条会话是否存在。
    """
    conv = await aichat.get_conversation(conversation_id)
    if not conv or not aichat.owns(
        conv, str(user.get("username") or ""), _is_admin(user)
    ):
        raise HTTPException(status_code=404, detail=i18n.tr("找不到这条会话"))
    # 会话上存的是建会话时的主机名快照（库里保留），展示用**当前**名字 ——
    # 受管主机改过名之后，报告卡上那行主机名还显示旧名就认不出了。
    hostscope.apply_display_names([conv], await hostscope.current_display_names())
    return {
        "conversation": conv,
        "messages": await aichat.list_messages(
            conversation_id, after_seq=int(since_seq or 0)
        ),
        # 报告页要显示「模型 / 耗时 / token / 工具数」，这些记在运行上
        "session": await aiaudit.latest_session_for_conversation(conversation_id),
    }


@router.post("/conversations/{conversation_id}/exec-authorization")
async def set_exec_authorization(
    conversation_id: str,
    payload: Dict[str, Any],
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """同步会话的主机授权状态（不跑模型、不写审计）。

    用于两处：切换主机自动接续旧会话时**关闭**旧授权；统一面板已经写过的状态
    在页面恢复时对齐。真正的授权动作走 ``/authorization/grant`` 并写审计。
    """
    conv = await aichat.get_conversation(conversation_id)
    if not conv or not aichat.owns(
        conv, str(user.get("username") or ""), _is_admin(user)
    ):
        raise HTTPException(status_code=404, detail=i18n.tr("找不到这条会话"))
    enabled = bool(payload.get("enabled"))
    if enabled and not security.has_user_permission(user, "ai.exec"):
        raise HTTPException(
            status_code=403,
            detail=i18n.tr("没有「授权 AI 上机执行」的权限"),
        )
    access = _exec_access(payload, enabled)
    if not await aichat.set_allow_exec(
        conversation_id, enabled, exec_access=access
    ):
        raise HTTPException(
            status_code=500, detail=i18n.tr("授权状态保存失败，请重试")
        )
    return {
        "conversation_id": conversation_id,
        "allow_exec": 1 if enabled else 0,
        "exec_access": access,
    }


@router.post("/conversations/{conversation_id}/messages/stream")
async def conversation_stream(
    conversation_id: str,
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(VIEW),
) -> StreamingResponse:
    """在一条会话里发一条消息，边跑边推过程（SSE）。

    事件类型与 `/inspect/stream` 基本一致，另外多两个：
    ``assistant``（本轮回复文本）与 ``report``（首轮产出的 findings 报告）。
    """
    conv = await aichat.get_conversation(conversation_id)
    username = str(user.get("username") or "")
    is_admin = _is_admin(user)
    # 与一次性排查同一条口径：权限清单要带进 AI 层（整轮体检会采集全局证据）
    perms = security.effective_permissions(user)
    if not conv or not aichat.owns(conv, username, is_admin):
        raise HTTPException(status_code=404, detail=i18n.tr("找不到这条会话"))

    allow_exec = bool(payload.get("allow_exec"))
    if allow_exec and not security.has_user_permission(user, "ai.exec"):
        raise HTTPException(
            status_code=403,
            detail=i18n.tr("没有「授权 AI 上机执行」的权限"),
        )
    exec_access = _exec_access(payload, allow_exec)
    allow_write = allow_exec and exec_access == "write"

    # 虚拟机授权：带上租约 id 才会启用进虚拟机的那几个工具。租约必须属于本人、
    # 未过期、未撤销；权限位在创建租约时校验过，这里再查一次 —— 权限可能在
    # 会话存续期间被管理员回收。
    lease = None
    lease_id = str(payload.get("vm_lease") or "")
    if lease_id:
        if not security.has_user_permission(user, "ai.vm.read"):
            raise HTTPException(
                status_code=403, detail=i18n.tr("没有「虚拟机内取证」的权限")
            )
        lease = await aivm.resolve_lease(
            lease_id, username=username, is_admin=is_admin
        )
        if lease is None:
            raise HTTPException(
                status_code=403,
                detail=i18n.tr("这条虚拟机授权已失效，请重新授权"),
            )

    text = str(payload.get("text") or "")
    provider_id = str(payload.get("provider_id") or "")
    # chat（默认，自由对话）/ inspect（体检模板，产出报告）。未知值一律当自由对话。
    mode = aichat.normalise_mode(payload.get("mode"))
    # 预案 key：给了就用预案的指令代替用户输入（见 aiplaybooks）
    preset = str(payload.get("preset") or "")
    allowed = await hostscope.allowed_host_ids(user)
    queue: "asyncio.Queue[Any]" = asyncio.Queue()

    perf_started = time.perf_counter()
    last_disconnect_check = 0.0

    async def emit(event: Dict[str, Any]) -> None:
        # 同上：断连检查降频到 500ms，结束类事件仍立即检查（否则「终止」会延迟）
        nonlocal last_disconnect_check
        kind = str(event.get("type") or "")
        if kind in ("done", "error", "aborted", "approval"):
            check_now = True
        else:
            check_now = time.monotonic() - last_disconnect_check >= 0.5
        if check_now:
            last_disconnect_check = time.monotonic()
            if await request.is_disconnected():
                raise ai.StopInspection()
        if kind in ("token", "report_progress"):
            aiperf.set_once("ttfb_ms", (time.perf_counter() - perf_started) * 1000)
        await queue.put(event)

    def _finish_perf(session: str, status: str) -> Dict[str, Any]:
        return aiperf.log_session(
            session,
            status=status,
            total_ms=(time.perf_counter() - perf_started) * 1000,
        )

    async def worker() -> None:
        session_id = ""
        started = time.time()
        lane = _lane_for(provider_id)
        sem = _lane_semaphore(lane)
        reserved = False
        aiperf.start()
        queued_at = 0.0
        acquired_at = 0.0
        try:
            if sem.locked() or _global_semaphore().locked():
                reserved = True
                queued_at = time.perf_counter()
                await queue.put(_queued_event(lane))
            async with _turn_lane(lane):
                acquired_at = time.perf_counter()
                if queued_at:
                    aiperf.add("queue_ms", (acquired_at - queued_at) * 1000)
                session_id = await aiaudit.open_session(
                    username=username,
                    host_id=str(conv.get("host_id") or ""),
                    host_name=str(conv.get("host_name") or ""),
                    provider="",
                    mode="chat",
                    conversation_id=conversation_id,
                )
                # 租约只在这一轮对话期间生效（contextvar）。放 worker 内部而不是
                # 端点函数体：worker 是独立 task，在它外面设置不会传进来。
                with aivm.scope(lease):
                    result = await aichat.chat_turn(
                        conversation_id,
                        text,
                        mode=mode,
                        preset=preset,
                        emit=emit,
                        allowed=allowed,
                        provider_id=provider_id,
                        username=username,
                        is_admin=is_admin,
                        allow_exec=allow_exec,
                        allow_write=allow_write,
                        exec_access=exec_access,
                        perms=perms,
                    )
                calls = result.get("tool_calls") or []
                await aiaudit.record_calls(session_id, calls)
                await aiaudit.finish_session(
                    session_id,
                    status="done",
                    model=str(result.get("model") or ""),
                    steps=int(result.get("steps") or 0),
                    tool_calls=len(calls),
                    usage=result.get("usage") or {},
                    duration_ms=int((time.time() - started) * 1000),
                    result=result.get("report"),
                    metrics=_finish_perf(session_id, "done"),
                )
                await queue.put(
                    {
                        "type": "done",
                        "conversation_id": conversation_id,
                        "session_id": session_id,
                    }
                )
                await security.audit(
                    request,
                    user,
                    "ai.chat",
                    target=str(conv.get("host_name") or conversation_id),
                    result="success",
                    detail="AI 对话（"
                    + ("体检" if result.get("mode") == aichat.MODE_INSPECT else "追问")
                    + "）："
                    + str(len(calls))
                    + " 次工具调用",
                )
        except ai.StopInspection:
            # 用户终止不算失败，但会话必须收口，否则记录里会留一条永远「进行中」
            if session_id:
                await aiaudit.finish_session(
                    session_id,
                    status="cancelled",
                    metrics=_finish_perf(session_id, "cancelled"),
                )
            await queue.put({"type": "aborted"})
        except ai.AIError as exc:
            if session_id:
                await aiaudit.finish_session(
                    session_id,
                    status="failed",
                    error=str(exc),
                    metrics=_finish_perf(session_id, "failed"),
                )
            await queue.put({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 兜底：异常也要让前端收到结束信号
            logger.exception("AI 会话异常")
            if session_id:
                await aiaudit.finish_session(
                    session_id,
                    status="failed",
                    error=str(exc)[:300],
                    metrics=_finish_perf(session_id, "failed"),
                )
            await queue.put({"type": "error", "message": str(exc)[:300]})
        finally:
            if acquired_at:
                _record_duration(lane, (time.perf_counter() - acquired_at) * 1000)
            if reserved:
                _leave_queue(lane)
            await queue.put(None)  # 结束哨兵

    async def gen():
        task = asyncio.create_task(worker())
        try:
            while True:
                try:
                    # 心跳：上游卡住 / 长工具执行时 SSE 也要有字节流出来，否则
                    # nginx / 浏览器 / 前端的读超时会把它当成断线（用户只看到
                    # 一直转圈，却没有任何错误提示）。15 秒远小于这些超时。
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if event is None:
                    break
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # 面板前面有 nginx：不关缓冲的话事件会被攒着一起发，流式就没意义了
            "X-Accel-Buffering": "no",
        },
    )


# ------------------------------------------------------------------ 远程终端
#
# 左侧那个终端的后端：浏览器里的 xterm.js ↔ 这条 WebSocket ↔ paramiko 的交互式
# shell。与上面几条排查接口最大的不同是 —— 它给的是一条**能敲任意命令**的通道。
#
# 因此门开得比只读排查紧得多：
# * 单列一个 ``ai.terminal`` 权限（默认仅管理员）——「能看 AI 的结论」和
#   「能亲手在这台机器上敲命令」不是一回事；
# * 归属校验沿用受管主机那一套（``hostscope.assert_host_access``），
#   别人的主机连不上；
# * 面板本机（``local``）不开放：那是面板自己所在的那台机器，走 SSH 受管
#   主机之外的路子登录更合适；
# * 打开与关闭各写一条审计。
#
# 终端里的按键由远端 shell 自己解释，不逐条进 ``ai_tool_calls``（那是 AI 的账）；
# 用户自己在机器上做过什么由该机自己的 audit / history 负责 —— 与 SSH 直连的
# 语义一致，不必也不该由面板替它记账。

TERMINAL_PERMISSION = "ai.terminal"


async def _reject_ws(websocket: WebSocket, code: int, reason: str) -> None:
    """**握手前**拒绝（来源校验失败 / 凭据无效）：不 accept，直接关。

    这两种失败发生在身份确认之前，原因只能写进服务端日志 —— 对一个还没通过
    认证的调用方说「这台主机不属于当前用户」本身就是信息泄露。

    与 console.py 同样的理由记日志：uvicorn 对「accept 之前关闭」一律记成 403，
    光看访问日志分不清是来源校验、凭据无效还是权限不足。
    """
    logger.info(
        "AI 终端握手被拒 %s：code=%s 原因=%s Origin=%s Host=%s 有Cookie=%s",
        websocket.url.path,
        code,
        reason,
        websocket.headers.get("origin") or "-",
        websocket.headers.get("host") or "-",
        bool(websocket.cookies.get(security.ACCESS_COOKIE)),
    )
    await websocket.close(code=code, reason=reason)


async def _fail_after_auth(websocket: WebSocket, code: int, reason: str) -> None:
    """**已认证之后**的失败：先 accept，把原因发给前端，再关闭。

    为什么不能像 ``_reject_ws`` 那样直接关：accept 之前关闭时，uvicorn 只回一个
    HTTP 403，浏览器侧 ``onerror`` 拿到的是没有任何内容的通用失败 ——「这台机器
    SSH 连不上」和「你没有权限」在界面上长得一模一样，用户只能看到「连接失败」
    三个字，无从下手。这几条失败都发生在身份已经验证过之后，把原因告诉本人
    不泄露任何东西。

    关闭码按语义分：策略类（权限 / 归属 / 缺参）用 1008，上游（SSH 连不上）
    用 1011。前端主要读那条 error 帧，码只是给日志看的。
    """
    logger.info(
        "AI 终端失败（已认证）%s：code=%s 原因=%s", websocket.url.path, code, reason
    )
    try:
        await websocket.accept()
        await websocket.send_text(
            json.dumps({"type": "error", "message": reason}, ensure_ascii=False)
        )
    except Exception:  # noqa: BLE001 - 对端已经跑了也要继续走关闭
        logger.debug("AI 终端错误帧发送失败", exc_info=True)
    try:
        await websocket.close(code=code, reason=reason[:120])
    except Exception:  # noqa: BLE001
        logger.debug("AI 终端关闭失败", exc_info=True)


async def _ws_identity(websocket: WebSocket, token: str) -> Dict[str, Any]:
    """WebSocket 侧的鉴权：握手令牌 → 用户（含用户级权限覆盖）。

    与 HTTP 侧只有令牌来源不同（WS 没法带 Authorization 头，走 ``?token=`` 或
    同源 HttpOnly Cookie）。身份**从库里重新读**而不是只看 JWT 里的 role：
    自定义角色与用户级权限覆盖都记在用户行上，只看 token 会让「被单独授权过的
    人」进不来、而「权限已被收回的人」照样进得来。
    """
    value = security.ws_token(websocket, token)
    if not value:
        raise PermissionError(i18n.t("error.no_credentials"))
    try:
        payload = security.decode_token(value, expected_typ="access")
        await security.ensure_session_active(payload)
    except HTTPException as exc:
        raise PermissionError(str(exc.detail)) from exc

    username = str(payload.get("sub") or "")
    user = await store.get_user(username) if username else None
    if not user:
        raise PermissionError(i18n.tr("无效的认证凭据"))
    if not user.get("enabled", True) or str(
        user.get("status") or store.STATUS_ACTIVE
    ) != store.STATUS_ACTIVE:
        raise PermissionError(
            i18n.pick("账号不可用", "The account is not usable")
        )
    return {
        "username": username,
        "role": str(user.get("role") or "viewer"),
        "permissions_override": user.get("permissions_override"),
    }


async def _terminal_pump(session: aiterm.TerminalSession, websocket: WebSocket) -> None:
    """两个方向各一个任务：浏览器 → 远端 shell，远端 → 浏览器。

    浏览器发来的每一帧都是 JSON：``input`` 是按键（原样送给远端，控制序列也在
    里面），``resize`` 让远端按新的列宽折行，``ping`` 只用来把会话标记成活跃。
    远端回来的输出走会话队列 —— AI 的镜像也进同一个队列，所以「谁输出的」在
    时间顺序上天然对齐。
    """

    async def browser_to_host() -> None:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except ValueError:
                message = None
            if not isinstance(message, dict):
                # 容错：非 JSON 的一帧按纯输入处理（手工用 wscat 调试时方便）
                await session.write(raw)
                continue
            kind = str(message.get("type") or "")
            if kind == "input":
                await session.write(str(message.get("data") or ""))
            elif kind == "resize":
                await session.resize(
                    int(message.get("cols") or session.cols),
                    int(message.get("rows") or session.rows),
                )
            elif kind == "ping":
                session.touch()

    async def host_to_browser() -> None:
        while True:
            event = await session.out.get()
            if event is None:
                return
            await websocket.send_text(json.dumps(event, ensure_ascii=False))
            if event.get("type") == aiterm.EXIT_EVENT:
                return

    async def watchdog() -> None:
        """空闲 / 生命周期看门狗。

        浏览器关掉、断网、机器休眠时服务端不会立刻收到 close 帧 —— 没有这道
        兜底，那条 SSH 连接会一直挂着（远端还会留着这个登录会话）。
        """
        while True:
            await asyncio.sleep(30)
            if session.expired():
                session.notice(
                    i18n.pick(
                        "终端空闲过久，已自动断开。刷新页面可重新连接。",
                        "The terminal was idle for too long and has been closed. "
                        "Reload the page to reconnect.",
                    )
                )
                session.finish()
                return

    tasks = [
        asyncio.create_task(browser_to_host()),
        asyncio.create_task(host_to_browser()),
        asyncio.create_task(watchdog()),
    ]
    try:
        # 任一方向结束（远端退出 / 浏览器关页面 / 超时）就整体收尾
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.get("/terminal/preflight")
async def terminal_preflight(
    host_id: str = "",
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """终端连接前的自检：把「为什么连不上」用**普通 HTTP 请求**问清楚。

    为什么需要它：WebSocket 握手失败时，浏览器的 ``onerror`` / ``onclose``
    里**拿不到任何东西** —— 没有状态码、没有原因。后端拒绝了是它，中间的反向
    代理没转发 ``Upgrade`` 也是它，界面上长得完全一样。

    这个接口把**后端这一侧**的前置条件逐条查一遍（权限 / 主机归属 / SSH 可达），
    返回 ``reason`` 就是第一处不满足的原因。于是：

    * 自检失败 → 原因明确，界面直接显示；
    * 自检通过、WebSocket 仍然连不上 → 后端已经确认没问题，剩下的**只可能**
      是反向代理没转发 WebSocket，界面就能给出那句该改的配置。

    先探 TCP，再做一次**完整的 SSH 建连 + 认证**后立即断开。只探端口会把
    「网络通、但用户名 / 口令 / 私钥 / sshd 配置不对」误报成通过，用户随后在
    WebSocket 里只看到一句 ``Authentication failed``。这里复用与终端完全相同的
    ``sshremote._connect``，把真正原因放进普通 HTTP 响应（只读，不执行命令）。
    """
    checks: List[Dict[str, Any]] = []

    def add(key: str, ok: bool, detail: str = "") -> None:
        checks.append({"key": key, "ok": ok, "detail": detail})

    if not security.has_user_permission(user, TERMINAL_PERMISSION):
        add("permission", False)
        return {
            "ok": False,
            "reason": i18n.pick(
                "权限不足：需要「远程终端」权限",
                "Permission denied: the “Remote terminal” permission is required",
            ),
            "checks": checks,
        }
    add("permission", True)

    if not host_id:
        add("host", False)
        return {
            "ok": False,
            "reason": i18n.pick(
                "没有选择受管主机（面板本机不支持在此打开终端）",
                "No managed host selected (the panel host itself is not exposed here)",
            ),
            "checks": checks,
        }

    try:
        row = await hostscope.assert_host_access(user, host_id)
    except HTTPException as exc:
        add("host", False, str(exc.detail))
        return {"ok": False, "reason": str(exc.detail), "checks": checks}
    add("host", True, str(row.get("host") or ""))

    address = str(row.get("host") or "")
    port = int(row.get("port") or 22)
    reachable = await _tcp_probe(address, port)
    add(
        "ssh",
        reachable,
        "" if reachable else f"{address}:{port}",
    )
    if not reachable:
        return {
            "ok": False,
            "reason": i18n.pick(
                f"连不上 {address}:{port} —— 面板所在的机器无法访问该地址的 SSH 端口",
                f"Cannot reach {address}:{port} — no SSH connectivity from the panel host",
            ),
            "checks": checks,
        }

    # 端口通只说明「网络到了」。认证失败（用户名 / 口令 / 私钥 / sshd 禁口令 /
    # 面板公钥没下发）必须在这里说清楚，否则用户会在 WebSocket 里只看到
    # 一句没有任何指向性的 Authentication failed。
    try:
        auth_ok, auth_detail = await asyncio.wait_for(
            asyncio.to_thread(sshremote.verify_auth, row), timeout=30.0
        )
    except asyncio.TimeoutError:
        auth_ok, auth_detail = False, i18n.pick(
            "SSH 握手超时（端口能连上，但 30 秒内没有完成认证）",
            "SSH handshake timed out (port reachable, but authentication did not "
            "finish within 30 seconds)",
        )
    add("auth", auth_ok, auth_detail)
    if not auth_ok:
        return {
            "ok": False,
            "reason": i18n.pick("SSH 认证未通过：", "SSH authentication failed: ")
            + auth_detail,
            "checks": checks,
        }

    return {"ok": True, "reason": "", "checks": checks}


async def _tcp_probe(address: str, port: int, timeout: float = 3.0) -> bool:
    """探一下 TCP 端口通不通。

    只回答「网络能不能到」；认证 / 指纹 / sshd 配置由调用方随后的
    ``sshremote.verify_auth`` 回答。两步分开是为了让错误提示能区分
    「网络不通」与「连上了但认证不过」。
    """
    if not address:
        return False
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port), timeout=timeout
        )
    except Exception:  # noqa: BLE001 - 超时 / 拒绝 / DNS 失败都算不可达
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # noqa: BLE001
        pass
    del reader
    return True


@router.websocket("/terminal/ws")
async def terminal_ws(
    websocket: WebSocket,
    host_id: str = Query(default=""),
    token: str = Query(default=""),
    cols: int = Query(default=80, ge=1, le=1000),
    rows: int = Query(default=24, ge=1, le=1000),
) -> None:
    """受管主机的交互式终端（页面左侧那个窗口）。

    URL 形如 ``/api/ai/terminal/ws?host_id=<id>&cols=120&rows=30``；浏览器不带
    令牌（在 HttpOnly Cookie 里），脚本仍可 ``?token=``。
    """
    if not security.ws_origin_allowed(websocket):
        await _reject_ws(websocket, 4403, i18n.tr("来源校验失败"))
        return

    try:
        user = await _ws_identity(websocket, token)
    except PermissionError as exc:
        await _reject_ws(websocket, 4401, str(exc))
        return

    # WS 里没有 Request 对象，审计的 IP 从 ASGI scope 取（与 HTTP 侧同一个口径）。
    # 终端是「一条能敲任意命令的通道」，因此**被拒绝的尝试也要留痕** ——
    # 「谁反复试图在一台不属于他的机器上开终端」正是审计该回答的问题。
    wip = security.client_ip_from_scope(websocket.scope)

    if not security.has_user_permission(user, TERMINAL_PERMISSION):
        await store.add_audit(
            username=user["username"],
            action="ai.terminal.open",
            target=host_id,
            result="failed",
            detail="权限不足：需要 ai.terminal",
            ip=wip,
        )
        await _fail_after_auth(
            websocket,
            1008,
            i18n.pick(
                "权限不足：需要「远程终端」权限",
                "Permission denied: the “Remote terminal” permission is required",
            ),
        )
        return
    if not host_id:
        await _fail_after_auth(
            websocket, 1008, i18n.pick("缺少 host_id", "Missing host_id")
        )
        return

    try:
        row = await hostscope.assert_host_access(user, host_id)
    except HTTPException as exc:
        await store.add_audit(
            username=user["username"],
            action="ai.terminal.open",
            target=host_id,
            result="failed",
            detail=str(exc.detail)[:200],
            ip=wip,
        )
        await _fail_after_auth(websocket, 1008, str(exc.detail))
        return

    host_name = str(row.get("name") or row.get("host") or host_id)
    try:
        session = await aiterm.open_session(
            username=user["username"],
            host_id=host_id,
            row=row,
            host_name=host_name,
            cols=cols,
            rows=rows,
        )
    except Exception as exc:  # noqa: BLE001 - 连不上要让用户看到原因，不是静默断开
        logger.warning("AI 终端打开失败：host=%s %s", host_id, exc)
        await store.add_audit(
            username=user["username"],
            action="ai.terminal.open",
            target=host_name,
            result="failed",
            detail=str(exc)[:200],
            ip=wip,
        )
        await _fail_after_auth(websocket, 1011, str(exc)[:200])
        return

    await websocket.accept()
    await store.add_audit(
        username=user["username"],
        action="ai.terminal.open",
        target=host_name,
        result="success",
        detail=f"打开远程终端 {host_name}（{cols}x{rows}）",
        ip=wip,
    )

    try:
        await _terminal_pump(session, websocket)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - 收尾要照做，但异常不能抛给 ASGI
        logger.debug("AI 终端异常收尾", exc_info=True)
    finally:
        aiterm.close_session(session)
        await store.add_audit(
            username=user["username"],
            action="ai.terminal.close",
            target=host_name,
            result="success",
            detail=f"关闭远程终端 {host_name}",
            ip=wip,
        )
