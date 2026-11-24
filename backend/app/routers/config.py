"""Proxmox connection configuration and health endpoints."""
from __future__ import annotations

import asyncio
import logging
import secrets
import ssl as ssl_module
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from .. import (
    # 面板版本：唯一来源在 app/__init__.py（package.json 与 CHANGELOG 跟着它走）
    __version__,
    accesscontrol,
    captcha,
    connregister,
    defaults,
    # 自助注册的邮箱验证码：开关存 settings KV（见 app/emailcodes.py）
    emailcodes,
    faq,
    fleetstatus,
    invites,
    mailer,
    onboard,
    panel_url,
    quota,
    regcodes,
    routeguard,
    security,
    site,
    specs,
    ssrfguard,
    store,
    throttle,
    ui,
)
from ..config import settings
from ..pve import (
    PveConnection,
    ProxmoxClient,
    ProxmoxError,
    client_for_connection,
    connection_from_profile,
    get_client,
    rebuild_client,
    requested_connection,
)
from ..schemas import (
    ConnectionConfigIn,
    ConnectionPauseIn,
    ConnectionRegisterCodeIn,
    ConnectionRegisterIn,
    ConnectionTestIn,
    FaqIn,
    InviteLinkIn,
    LoginCaptchaIn,
    MailConfigIn,
    MailTestIn,
    PanelUrlIn,
    RegistrationCodeIn,
    RegistrationEmailCodeIn,
    RegistrationModeIn,
    SessionPolicyIn,
    SiteInfoIn,
    UiPrefsIn,
    VmDefaultsIn,
    VmQuotaIn,
)

logger = logging.getLogger(__name__)

# 权限是**前缀级**统一的，不在这里逐个端点手写：``/api/config/*`` 整组收在
# ``settings.manage`` 一档（审计项：路由级鉴权遗漏 —— 此前 mail / session /
# specs / quota / ui 只要登录就能读，同组的 connection / diagnostics 却要
# settings.manage，于是漏出了 SMTP 拓扑与会话策略）。规则与白名单见
# ``app/routeguard.py``，由 ``tests/test_route_permissions.py`` 静态核对。
#
# 注意这条 dependencies 挂在**整个 router** 上：``/api/health``、``/api/connections``、
# ``/api/quota/*`` 也会经过它，但守卫只对命中受控前缀的路径生效（先做一次字符串
# 前缀短路，不额外查库），所以这些热点接口没有额外开销。
router = APIRouter(
    prefix="/api",
    tags=["config"],
    dependencies=[Depends(routeguard.enforce_route_permission)],
)


@router.get("/health")
async def health(
    user: Optional[Dict[str, Any]] = Depends(security.get_current_user_optional),
) -> Dict[str, Any]:
    """健康检查：匿名只答「活着」，登录后才给诊断明细。

    此前这个匿名端点会把 PVE 版本、节点数、连接状态一起吐出去（审计项
    FD-04）—— 版本号可以直接拿去对公开漏洞库做匹配，节点数说明集群规模，
    ``pve_connected`` 的翻转还能被用来探测面板配置变化。而未登录访客拿这些
    信息没有任何用处。

    所以现在分两档：
      * 匿名：回 ``{"status": "ok", "authenticated": false}``，且**不探测 PVE**
        —— 顺带堵掉「匿名请求即可让面板反复向上游发请求」这条放大路径；
      * 已登录（前端登录后的各处都在轮询它）：``authenticated: true``，字段与
        以前完全一致。
    两档用 ``authenticated`` 显式区分：少了这一位，匿名载荷与「已配置但连不上」
    在字段上无从分辨（诊断字段都缺省），前端只能误报成 PVE 故障。
    需要深度诊断用的是 ``/api/config/diagnostics``（要求 settings.manage）。
    """
    if user is None:
        # ``authenticated`` 是给前端判别用的：没有它时，一份「只有 status」的
        # 匿名载荷和「连上了但读不到 PVE」在字段上完全一样（pve_configured /
        # pve_connected 都缺省），前端只能把两者都渲染成「PVE 连不上」。
        # 匿名分支显式带上 false，登录分支带 true，前端才不会把探测结果未知
        # 当成故障（曾经的误报：access cookie 过期后 /health 静默降级成匿名，
        # 面板顶部一直挂着「无法连接到 Proxmox 主机」）。
        return {"status": "ok", "authenticated": False}

    result: Dict[str, Any] = {
        "status": "ok",
        # 已登录：下面的诊断字段才可信。见匿名分支的说明。
        "authenticated": True,
        "pve_connected": False,
        # 与 pve_connected 分两个字段是有意的：前者是「连通」，这个是「填没填过」。
        # 未配置和配了但连不上，用户要做的事完全不同（去添加 vs 去排查），
        # 只给一个 false 的话前端只能把两者都渲染成报错。
        "pve_configured": False,
        # 兜底读取的那台被「暂停接入」时为真：前端据此说「已暂停」而不是「连不上」
        "pve_paused": False,
        "version": __version__,
        "pve_version": None,
        "node_count": 0,
        "error": None,
    }

    try:
        client = get_client()
    except ProxmoxError as exc:
        # 目标连接被暂停（503）或已被删除（404）：这是可解释状态，不该 500
        result["pve_paused"] = exc.status_code == 503
        result["pve_configured"] = exc.status_code == 503
        result["error"] = exc.message
        if security.has_user_permission(user, "node.view"):
            result["fleet"] = fleetstatus.panel_fleet_cached(
                security.has_user_permission(user, "settings.manage")
            )
        return result

    if not client.conn.configured:
        result["error"] = "Proxmox 连接未配置"
        return result

    result["pve_configured"] = True

    try:
        version = await client.version()
        nodes = await client.nodes()
        result["pve_connected"] = True
        result["pve_version"] = version.get("version")
        result["node_count"] = len(nodes or [])
    except ProxmoxError as exc:
        result["error"] = exc.message
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)

    # 机队状态并入这一条：顶栏「控制台状态」原本同时轮询 /health 与
    # /cluster/fleet-status，后者逐条连接真实探活（C×(2~4) 次上游请求）。
    # 合并后顶栏每 tick 只发一条，探活由 30 秒缓存统一削峰。没有 node.view
    # 的账号拿不到这块明细（与 /cluster/fleet-status 同一道守卫）。
    if security.has_user_permission(user, "node.view"):
        # 只读缓存、过期在后台刷新：健康检查不能被一次慢探测拖住（见 fleetstatus）
        result["fleet"] = fleetstatus.panel_fleet_cached(
            security.has_user_permission(user, "settings.manage")
        )

    return result


@router.get("/config/connection")
async def get_connection(
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """当前连接（兜底连接）的完整配置。

    **只给能管理连接的人**（``settings.manage``）：即便不回传密钥明文，这份读数
    仍然带着内网地址、端口、PVE root 令牌标识、控制台账号、是否关闭证书校验与
    集群 CA 全文 —— 足以拼出一张内网拓扑，也是横向攻击最有价值的情报。普通用户
    不需要它：想认「哪台 PVE 上有哪些节点」有 ``GET /api/connections`` 的裁剪版
    （只有 id / 名称 / 区域 / 是否可达）。

    写操作本来就要 ``settings.manage`` + 二次确认；读操作此前只要求登录，导致
    普通用户能把写接口挡在门外，却照样读到同一份内容 —— 这里与写收成同一档。
    """
    # 读操作也留痕：连接配置虽然不回传密钥明文，但它描述了面板能碰哪些集群，
    # 「谁在什么时候看了这份配置」是事后追溯的关键线索。
    await security.audit_read(request, user, "config.connection.read")
    cfg = store.public_connection_config()
    # 新建连接的默认校验策略跟随后端配置 PVE_VERIFY_SSL（生产默认校验证书），
    # 前端建号/新建表单直接取这个值，免得两端各写一套默认值。
    cfg["verify_ssl_default"] = settings.pve_verify_ssl
    return cfg


@router.put("/config/connection")
async def update_connection(
    payload: ConnectionConfigIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
    # 这里能写入 PVE 的 API Token（等于集群权限）：必须二次确认身份
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, Any]:
    await store.save_connection_config(payload.model_dump(exclude_none=True))
    rebuild_client()
    await security.audit(
        request, user, "config.update", target="pve_connection",
        detail={"host": payload.host, "port": payload.port},
    )
    return store.public_connection_config()


# ================================================================= multi-PVE
# 支持保存多台 Proxmox。多条连接**一律同级**：节点 / 虚拟机 / 存储等列表接口
# 未指定连接时会遍历全部连接合并返回；只有「一次只能读一台」的接口（健康探活、
# 集群版本 / 仲裁等）才回落到这里维护的「兜底连接」。它不对外表示「主集群」，
# 设置页只把它作为「兜底读取目标」呈现。


def _find_connection(
    conns: List[Dict[str, Any]], conn_id: str
) -> Optional[Dict[str, Any]]:
    for c in conns:
        if str(c.get("id")) == conn_id:
            return c
    return None


@router.get("/connections")
async def list_connections(
    request: Request,
    user: Dict[str, Any] = Depends(
        security.require_any_permission(
            "node.view",
            "vm.view",
            "vm.create",
            "ct.view",
            "ct.create",
            "network.view",
            "storage.view",
            "template.view",
            "pbs.view",
            "task.view",
            # 防火墙页要先知道「改的是哪套集群」：集群作用域无法由 node/vmid
            # 推断，只能靠前端把连接硬指定下发（见 routers/firewall.py 的依赖）。
            # 这里只回 id + name，不泄露地址与凭据。
            "firewall.view",
            "ct.firewall.view",
        )
    ),
) -> List[Dict[str, Any]]:
    """已保存的 PVE 连接（**按权限裁剪**）。

    完整字段（主机地址 / 端口 / 令牌与控制台凭据的「是否已设置」/ 区域 / 暂停
    状态 / 是否兜底）只给管理员（``settings.manage``）。连接配置说的是「面板能
    碰哪些集群、拿什么凭据碰」，属于管理信息。

    普通用户拿到的是**最小可用视图**，只有 ``id`` 与 ``name``（见
    ``store.public_connection_brief``）：节点列表与创建向导只需要「认得出是哪
    台」。此前这里还跟着区域、暂停 / 配好状态与「当前连接」标记 —— 这些既不
    参与界面必需流程，又拼出了基础设施拓扑（哪台在用、哪台停了、分在哪个机房），
    审计项 PVE-01 因此要求收口。

    另外加了显式的权限入口：没有任何节点 / 客户机 / 建机 / 网络 / 存储 / 模板等
    需求的账号（例如只拿到 ``alert.view`` 的自定义角色）不再能读到这份清单。
    """
    await security.audit_read(request, user, "config.connections.read")
    conns = store.get_connections()
    active_id = str(store.get_active_connection().get("id") or "")
    can_manage = security.has_user_permission(user, "settings.manage")
    # 区域管理员只应看到本区域的连接：连接清单决定他能切到哪台 PVE，看到
    # 其它区域的连接等于把「另一个区域的入口」摆在他面前（普通用户不受限，
    # 他们的范围由节点授权决定）。
    conns = [
        c
        for c in conns
        if security.region_in_scope(user, str(c.get("region") or ""))
    ]
    result: List[Dict[str, Any]] = []
    for c in conns:
        if can_manage:
            pub = store.public_connection_config(c)
            pub["active"] = str(c.get("id")) == active_id
        else:
            pub = store.public_connection_brief(c)
        result.append(pub)
    return result


@router.post("/connections")
async def create_connection(
    payload: ConnectionConfigIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, Any]:
    conns = store.get_connections()
    was_empty = len(conns) == 0
    data = payload.model_dump(exclude_none=True)
    data.pop("id", None)
    name = str(data.pop("name", "") or "") or str(data.get("host") or "") or "未命名连接"
    cid = secrets.token_hex(4)
    profile: Dict[str, Any] = {"id": cid, "name": name, **data}
    conns.append(profile)
    await store.save_connections(conns)

    # 仅当这是第一条连接时自动设为当前，避免新增时抢占用户正在用的那台
    if was_empty:
        await store.set_active_connection(cid)
        rebuild_client()

    await security.audit(
        request, user, "connection.create", target=f"connection/{cid}",
        detail={"host": data.get("host"), "port": data.get("port")},
    )
    pub = store.public_connection_config(profile)
    pub["active"] = store.get_active_connection_id() == cid
    return pub


# ------------------------------------------------------------------ 一键接入
#
# 场景：机房多、PVE 集群多，一台台在浏览器里填表单不现实。脚本在 PVE 节点上以
# root 跑一次，自动建 PVE 侧令牌并注册到面板。
#
# 为什么不用 API Token 直接调 POST /connections：那条路径要求 step-up，而 step-up
# 对 API Token 无条件 403（令牌是无人值守凭据）。这里改成「一次性注册码」：
# 管理员在浏览器里签发，脚本拿它建**一条**连接，用完即废。
#
# 幂等的三档结果（脚本据此给提示）：
#   created   —— 新建；
#   unchanged —— 已注册（同集群同端点），或同集群换了节点（默认跳过）；
#   updated   —— 显式 --force 时，把已有那条改成新的端点/凭据。
# 同 host:port 但集群指纹不同 = 两台不同 PVE 抢同一个地址，属于真冲突，409。

#: 连接列表是一段 JSON，读-改-写必须串行；两个脚本同时注册会互相覆盖。
_REGISTER_LOCK = asyncio.Lock()


def _endpoint_key(host: Any, port: Any) -> Tuple[str, int]:
    return (str(host or "").strip().lower(), int(port or 8006))


def _same_cluster(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    key = str(a.get("cluster_key") or "").strip()
    return bool(key) and key == str(b.get("cluster_key") or "").strip()


def _connect_failure_detail(payload: ConnectionRegisterIn) -> str:
    """传输层失败的**唯一**文案：只随「调用方有没有带证书」变化。

    以前这里会把 TLS 原文回显出来（排障方便），代价是匿名的持码者能靠回显区分
    「端口拒绝连接 / 超时 / 不是一个 TLS 服务 / 证书不被信任」—— 这正是端口
    扫描器需要的读数。现在所有传输层失败共用同一句话，唯一的分支是调用方自己
    就知道的信息（有没有带 PEM），不再随错误类型分化。
    """
    if payload.ca_cert:
        # 已经钉扎还失败：要么 PEM 里缺签发者（节点证书是 CA 签的，不是自签），
        # 要么证书换过了（pvecm updatecerts / 换节点证书）
        return (
            "连接失败：无法与目标建立可信连接。带的证书和目标当前用的对不上"
            "（PEM 缺签发者，或证书换过？）—— 重跑接入命令会重新取整条证书链；"
            "也可以在面板里把这条连接的「证书 PEM」清空后重试"
        )
    return (
        "连接失败：无法与目标建立可信连接。请依次核对："
        "①地址与端口（PVE 默认 8006，必须 HTTPS）；"
        "②证书校验 —— pveproxy 出厂是自签证书，重跑接入命令会把本机证书带上来做钉扎，"
        "确实取不到证书时可加 PVE_ONBOARD_INSECURE=1 临时跳过校验，"
        "或在面板里给这条连接填「证书 PEM」；"
        "③面板到目标的网络与防火墙"
    )


async def _probe_registration(
    payload: ConnectionRegisterIn,
) -> Dict[str, Any]:
    """连一次 PVE：确认凭据可用，并推导**集群指纹**。

    指纹不能由脚本自报：同一集群从不同节点 IP 注册时，只有面板自己去 PVE 问
    ``/cluster/status`` 才能认出「这其实是同一个集群」。单机（无集群）用节点名。
    """
    conn = PveConnection(
        host=payload.host,
        port=payload.port,
        token_id=payload.token_id,
        token_secret=payload.token_secret,
        verify_ssl=payload.verify_ssl,
        ca_cert=payload.ca_cert,
    )
    client = ProxmoxClient(conn)
    try:
        version = await client.version()
        nodes = await client.nodes() or []
        cluster_name = ""
        try:
            status = await client.get("/cluster/status") or []
            cluster_name = next(
                (
                    str(item.get("name") or "")
                    for item in status
                    if str(item.get("type") or "") == "cluster"
                ),
                "",
            )
        except ProxmoxError:
            # 单机 / 权限不足：退回「节点名」当指纹，仍然比 host 可靠
            cluster_name = ""
    except ssl_module.SSLError as exc:
        # 传上来的 ca_cert 本身读不出来（PEM 结构坏了 / 换行被吃掉）。这条以前是
        # 裸的 500，只回一句 "no start line: cadata does not contain a certificate"；
        # 现在明确告诉对方是「证书内容有问题」，本机改动会照常回滚。
        raise HTTPException(
            status_code=400,
            detail=(
                f"证书 PEM 无法解析：{exc}。"
                "重跑接入命令会重新抓取本机证书链；也可以在面板里把这条连接的"
                "「证书 PEM」清空，让校验回到 PVE_VERIFY_SSL 策略"
            ),
        ) from exc
    except ProxmoxError as exc:
        # 失败原因走 ssrfguard 的固定文案：上游正文、内部地址与请求路径一律不
        # 回显。这条链路允许持码者指定 ``host:port``，差异化回显就是「端口扫描器」
        # 的另一半（另一半是退码，见 connregister.record_failure）。
        #
        # 传输层（拒绝连接 / 超时 / 不是 TLS / 证书不信任）**统一同一句话**，
        # 原文只进服务端日志：只要能区分，持码者就拿到了一个端口存活的读数。
        # 唯一保留差异的是 401/403 —— 那说明对面确实是 PVE 且给了应答，且是
        # 接入能否成功的必要条件（否则用户不知道该改令牌还是改地址）。
        code, text = ssrfguard.classify_failure(exc)
        logger.info(
            "注册探活失败 host=%s port=%s code=%s detail=%s",
            payload.host,
            payload.port,
            code,
            " ".join(str(exc.message or "").split())[:300],
        )
        if code in ("auth_failed", "forbidden"):
            detail = f"连接失败：{text}"
        else:
            detail = _connect_failure_detail(payload)
        raise HTTPException(status_code=400, detail=detail) from exc
    finally:
        await client.aclose()

    node_names = sorted(
        str(node.get("node") or "") for node in nodes if node.get("node")
    )
    if not node_names:
        raise HTTPException(status_code=400, detail="连接失败：PVE 没有返回任何节点")
    return {
        "cluster_key": cluster_name or f"standalone:{node_names[0]}",
        "cluster_name": cluster_name,
        "node_names": node_names,
        "version": str(version.get("version") or ""),
        "release": str(version.get("release") or ""),
        # 令牌权限不足时 PVE 不报错，只是把指标字段留空 —— 这里顺手识别出来，
        # 免得用户以为「接入成功」，实际上面板什么都看不到
        "node_metrics": any(node.get("maxmem") is not None for node in nodes),
    }


def _register_profile(
    payload: ConnectionRegisterIn,
    probe: Dict[str, Any],
    region: str,
) -> Dict[str, Any]:
    host = payload.host.strip()
    name = payload.name.strip()
    if not name and region:
        # 批量接入时脚本一般不带名字：用「机房-集群」拼一个，比一串 IP 好认
        name = f"{region}-{probe['cluster_key']}"
    return {
        "name": name or host,
        "region": region,
        "host": host,
        "port": payload.port,
        "token_id": payload.token_id,
        "token_secret": payload.token_secret,
        "verify_ssl": payload.verify_ssl,
        "ca_cert": payload.ca_cert,
        # 没显式指定默认节点时，用 PVE 报回来的第一个节点，省得用户再选一次
        "node_default": payload.node_default or probe["node_names"][0],
        "cluster_key": probe["cluster_key"],
        "imported": int(time.time()),
        **_console_fields(payload),
    }


def _console_fields(payload: ConnectionRegisterIn) -> Dict[str, Any]:
    """脚本这次带上来的控制台凭据。

    没带就返回空字典 —— 不能返回 ``{"console_user": ""}``：那会把连接里已经填好
    的控制台账号抹成空，用户下次开控制台才发现「凭据未配置」。
    """
    user = (payload.console_user or "").strip()
    if not user or not payload.console_password:
        return {}
    return {"console_user": user, "console_password": payload.console_password}


def _merge_registered(
    current: Dict[str, Any],
    payload: ConnectionRegisterIn,
    probe: Dict[str, Any],
    region: str,
) -> Dict[str, Any]:
    """把新参数合并进已有连接（--force 用）。只覆盖接入相关字段。"""
    merged = dict(current)
    merged.update(
        {
            "host": payload.host.strip(),
            "port": payload.port,
            "token_id": payload.token_id,
            "token_secret": payload.token_secret,
            "verify_ssl": payload.verify_ssl,
            "ca_cert": payload.ca_cert,
            "node_default": payload.node_default or probe["node_names"][0],
            "cluster_key": probe["cluster_key"],
            "imported": int(time.time()),
            **_console_fields(payload),
        }
    )
    if payload.name.strip():
        merged["name"] = payload.name.strip()
    if region:
        merged["region"] = region
    if not merged.get("name"):
        merged["name"] = merged.get("host") or "未命名连接"
    return merged


def _onboard_path(request: Request) -> str:
    """安装脚本的路径。

    由签发接口的路径推出来（``.../register-codes`` -> ``.../onboard.sh``），
    不写死 ``/api`` 前缀：面板挂子路径（``/pve/api``）时也能拼对。
    """
    path = request.url.path
    if path.endswith("/register-codes"):
        path = path[: -len("/register-codes")] + "/onboard.sh"
    return path


def _onboard_url(
    request: Request, base: str, code: str, *, force: bool = False
) -> str:
    params: Dict[str, str] = {"code": code}
    if force:
        params["force"] = "1"
    return f"{base.rstrip('/')}{_onboard_path(request)}?{urlencode(params)}"


def _onboard_command(url: str) -> str:
    """给用户复制的那一条命令。

    用 ``bash -c "$(curl ...)"`` 而不是 ``curl ... | bash``：后者在下载被中断时
    会让 bash 执行**半截脚本** —— 前面的命令已经改了系统，后面的却没了，留下的
    是「账号建了、注册没发」这种半成品。命令替换会等 curl 完整退出再交给 bash。
    """
    quoted = f"'{url}'"
    return f'bash -c "$(curl -fsSL {quoted})"'


@router.post("/connections/register-codes")
async def issue_register_code(
    payload: ConnectionRegisterCodeIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, Any]:
    """签发一张注册码（明文只在这次响应里出现）。"""
    row = await connregister.create_code(
        region=payload.region,
        note=payload.note,
        created_by=str(user.get("username") or ""),
        ttl_minutes=payload.ttl_minutes,
        max_uses=payload.max_uses,
    )
    await security.audit(
        request,
        user,
        "connection.register_code",
        target=f"register-code/{row['id']}",
        detail={
            "region": row.get("region") or "",
            "ttl_minutes": row.get("ttl_minutes"),
            "max_uses": row.get("max_uses"),
            # 这张码会在节点上建 root@pam 令牌（全权）与控制台账号，必须留痕
            "grants_root": True,
        },
    )
    # 脚本地址优先用「设置」里的对外地址：节点常常在另一个网段，管理员从内网
    # 访问面板时 request.base_url 会是内网 IP，节点那边反而连不上。
    base = (await panel_url.resolve(request) or str(request.base_url)).rstrip("/")
    script_url = _onboard_url(request, base, row["code"])
    return {
        "id": row["id"],
        "code": row["code"],
        "region": row["region"],
        "expires": row["expires"],
        "ttl_minutes": row["ttl_minutes"],
        "max_uses": row["max_uses"],
        "used_count": row["used_count"],
        "remaining": row["remaining"],
        "script_url": script_url,
        "command": _onboard_command(script_url),
    }


@router.get("/connections/register-codes")
async def list_register_codes(
    limit: int = Query(default=50, ge=1, le=200),
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> List[Dict[str, Any]]:
    """最近的注册码（不含明文，只给状态，便于排查「为什么码用不了」）。"""
    return await connregister.list_codes(limit)


@router.post("/connections/register-codes/{code_id}/revoke")
async def revoke_register_code(
    code_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, Any]:
    ok = await connregister.revoke(code_id)
    await security.audit(
        request,
        user,
        "connection.register_code_revoke",
        target=f"register-code/{code_id}",
        result="success" if ok else "failed",
    )
    if not ok:
        raise HTTPException(status_code=404, detail="注册码不存在")
    return {"ok": True}


@router.delete("/connections/register-codes/{code_id}")
async def delete_register_code(
    code_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """删除一条已失效的注册码（已撤销 / 已使用 / 已过期）。

    只清「历史记录」：仍在有效期内的一次性凭据必须先撤销再删，否则列表里看不见、
    对方手里却还能用。审计照样留痕 —— 删掉的行只从列表消失，不抹掉「谁删的」。
    """
    row = await connregister.get_code(code_id)
    if row is None:
        raise HTTPException(status_code=404, detail="注册码不存在")
    if row.get("status") == "active":
        raise HTTPException(
            status_code=400,
            detail="这张注册码还有可用次数：请先撤销，确认失效后再删除",
        )

    ok = await connregister.delete_code(code_id)
    await security.audit(
        request,
        user,
        "connection.register_code_delete",
        target=f"register-code/{code_id}",
        result="success" if ok else "failed",
        detail={
            "status": row.get("status"),
            "region": row.get("region"),
            "used_by": row.get("used_by"),
        },
    )
    if not ok:
        raise HTTPException(status_code=404, detail="注册码不存在或已被删除")
    return {"ok": True}


@router.get("/connections/onboard/preflight", include_in_schema=False)
async def onboard_preflight(
    code: str = Query(default="", max_length=200),
    cluster_key: str = Query(default="", max_length=200),
    host: str = Query(default="", max_length=200),
    port: int = Query(default=8006, ge=1, le=65535),
) -> Dict[str, Any]:
    """脚本动手之前问一句：这台机器 / 这个集群是不是已经接过了（不消费注册码）。

    为什么必须先问：同一个 PVE 集群共用同一份账号与令牌（``/etc/pve`` 是集群级
    配置）。在第二个节点上再跑一遍脚本，会把**共享的**令牌轮换掉，面板里那条连接
    当场失效 —— 而脚本接下来很可能还会因为别的原因失败并回滚，等于白拆了别人正在
    用的凭据。所以脚本在动 ``pveum`` 之前先来问，答案是「已接入」就原地停下。

    只回「有没有」和那条连接的名字 / 地址，不回任何凭据。
    """
    row = await connregister.peek(code)
    if row is None or row.get("status") != "active":
        return {"code_ok": False, "registered": False}

    key = (cluster_key or "").strip()
    conns = store.get_connections()
    hit: Optional[Dict[str, Any]] = None
    if key:
        # 集群指纹是权威判据：脚本从 /cluster/status 读到的集群名，面板注册时也是这么
        # 认的（单机则是 standalone:<节点名>）
        hit = next(
            (c for c in conns if str(c.get("cluster_key") or "").strip() == key),
            None,
        )
    elif host:
        # 读不出集群名（老旧节点 / 权限不足）时退回按端点判断
        endpoint = _endpoint_key(host, port)
        hit = next(
            (
                c
                for c in conns
                if _endpoint_key(c.get("host"), c.get("port")) == endpoint
            ),
            None,
        )
    if hit is None:
        return {"code_ok": True, "registered": False}

    cid = str(hit.get("id") or "")
    return {
        "code_ok": True,
        "registered": True,
        "connection_id": cid,
        "name": str(hit.get("name") or ""),
        "host": str(hit.get("host") or ""),
        "port": int(hit.get("port") or 8006),
        "cluster_key": str(hit.get("cluster_key") or ""),
        "active": store.get_active_connection_id() == cid,
    }


@router.get("/connections/onboard.sh", include_in_schema=False)
async def onboard_script(
    request: Request,
    code: str = Query(default="", max_length=200),
    force: int = Query(default=0, ge=0, le=1),
) -> PlainTextResponse:
    """把注册码渲染成一段能直接执行的安装脚本。

    这是给运维用的「一条命令」入口::

        bash -c "$(curl -fsSL 'https://panel/api/connections/onboard.sh?code=pvir_...')"

    免鉴权，凭据就是 URL 里的注册码 —— 和 ``POST /connections/register`` 同一个
    前提。判活失败时**仍然回 200 + 脚本体**：管道给 bash 的东西必须是脚本，回一
    段 JSON 只会让节点上冒出一堆语法错误，而真正的原因（码过期了）反而看不见。

    ``force=1`` 让脚本能覆盖「同集群已有连接」的端点与凭据。它只对**持码者**开放
    （持码本来就能新建一条连接，能力上相仿），但会把已有连接的地址改到本次节点，
    因此走审计：``connection.register`` 里 ``status=updated``。
    """
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    row = await connregister.peek(code)
    if row is None or row.get("status") != "active":
        if row is None:
            reason = "注册码不存在，或链接被截断"
        else:
            reason = {
                "used": "这张注册码是一次性的，已经用过了",
                "exhausted": (
                    "这张注册码的可用次数已用尽（"
                    f"{row.get('used_count')}/{row.get('max_uses')} 次）"
                ),
                "expired": "这张注册码已过期",
                "revoked": "这张注册码已被撤销",
            }.get(str(row.get("status")), "这张注册码当前不可用")
        return PlainTextResponse(onboard.error_script(reason), headers=headers)

    base = (await panel_url.resolve(request) or str(request.base_url)).rstrip("/")
    # SELF_URL 让脚本在「不是 root」时能提示用户用 sudo 重跑自己，而不必回面板
    # 再复制一次：脚本得知道自己的地址。
    self_url = _onboard_url(request, base, code.strip(), force=bool(force))
    # 权限与控制台都没有开关：脚本一律用 root@pam 令牌（全权）并一律建控制台账号。
    # 查询串能改的只有 force（改端点 / 轮换令牌），改不了授权范围。
    body = onboard.render(
        panel=base,
        code=code.strip(),
        self_url=self_url,
        force=bool(force),
    )
    return PlainTextResponse(body, headers=headers)


#: 匿名注册接口的按 IP 限速（次 / 窗口秒）。
#:
#: 这是面板上**唯一不需要登录、又会让服务端主动去连调用方指定 ``host:port``**
#: 的入口。注册码的免费退码已经封顶（``connregister.record_failure``），但一张
#: 「不限次数」的码（``max_uses=0``）仍然可以按 3 次/10 分钟的速度一直探内网 ——
#: 限速是最后一道，让扫描速度不再取决于码的类型。正常接入一次只调一两次。
REGISTER_IP_LIMIT = 10
REGISTER_IP_WINDOW = 600


@router.post("/connections/register")
async def register_connection(
    payload: ConnectionRegisterIn,
    request: Request,
) -> Dict[str, Any]:
    """脚本用注册码注册一条 PVE 连接。

    顺序是「先烧码、再探活、探活失败就退码」：探活会让面板去连脚本指定的任意
    ``host:port``，若放在消费之前，任何人无需有效注册码就能把面板当跳板去探内网。
    先消费把这道口子收窄到「持码者」，而失败退码保住了「打错 IP 不必重新签发」
    这个运维体验。

    HTTP 200 表示请求本身成立，具体结果看 ``status``；只有「同地址不同集群」
    这种真冲突才回 409。
    """
    # 0) 匿名限速：先按来源 IP 计数，再谈注册码。计在「码之前」是有意的 ——
    #    连「码无效」的请求也占额度，否则换着乱填的码就能绕过（反正会被 403，
    #    但探活发生在码校验之后，这里挡的是那之前的所有请求）。
    ip = security.client_ip(request) or "unknown"
    allowed, retry_after = await throttle.allow(
        f"connreg:ip:{ip}", REGISTER_IP_LIMIT, REGISTER_IP_WINDOW
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"注册请求过于频繁（{REGISTER_IP_WINDOW // 60} 分钟内最多 "
                f"{REGISTER_IP_LIMIT} 次），请 {retry_after} 秒后再试"
            ),
            headers={"Retry-After": str(retry_after)},
        )

    # 1) 原子占用一次额度（并发下由 used_count<max_uses 判定，超出即失败）
    used_by = f"{payload.node_name or '-'}@{security.client_ip(request)}"
    code_row = await connregister.consume(payload.code, used_by=used_by)
    if code_row is None:
        # 失败原因要分开说：次数用尽和「码过期」是两种处置方式（前者要重新生成，
        # 后者可能只是要续期或看看是不是拿错了一张码）
        info = await connregister.peek(payload.code)
        status = str((info or {}).get("status") or "")
        if status == "exhausted":
            raise HTTPException(
                status_code=403,
                detail=(
                    "该注册码的可用次数已用尽（"
                    f"{info.get('used_count')}/{info.get('max_uses')} 次），"
                    "请到面板「节点 · 连接配置 → 一键接入」重新生成"
                ),
            )
        if status == "used":
            raise HTTPException(
                status_code=403,
                detail="该注册码是一次性的，已经用过了；请重新生成",
            )
        if status == "expired":
            raise HTTPException(
                status_code=403, detail="该注册码已过期；请重新生成"
            )
        if status == "revoked":
            raise HTTPException(status_code=403, detail="该注册码已被撤销")
        raise HTTPException(
            status_code=403,
            detail="注册码无效，请在面板「节点 · 连接配置 → 一键接入」重新生成",
        )
    code_id = str(code_row.get("id") or "")

    # 控制台凭据一律接受：一键接入现在总是创建控制台账号（VNC 要账号密码换票据），
    # 签发时已经没有「是否启用控制台」这个选项可供降级。

    # 2) 验证凭据与可达性；失败把码退回，让运维改完参数直接重跑
    try:
        probe = await _probe_registration(payload)
    except Exception:  # noqa: BLE001 - 任何失败都要把码退回，让人改完参数直接重跑
        # 退码有上限：同一张码 10 分钟内最多免费失败 3 次，超出就真扣一次额度。
        # 否则持码者可以不断更换 host:port 让面板去连，把这张「一次性接入许可」
        # 变成不用登录的内网扫描额度。
        if await connregister.record_failure(code_id):
            await connregister.release(code_id)
        else:
            logger.warning(
                "注册码 %s 探活失败次数过多，本次不再退回额度", code_id
            )
        raise

    await connregister.reset_failures(code_id)

    region = str(code_row.get("region") or payload.region or "").strip()

    async with _REGISTER_LOCK:
        conns = store.get_connections()
        was_empty = len(conns) == 0
        endpoint = _endpoint_key(payload.host, payload.port)
        same_host = next(
            (
                c
                for c in conns
                if _endpoint_key(c.get("host"), c.get("port")) == endpoint
            ),
            None,
        )
        if same_host is not None and not _same_cluster(same_host, probe):
            # 真冲突：退码再报错，否则运维只改错了一个 IP 却要回面板重签发
            await connregister.release(code_id)
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "该地址已被另一条连接占用（集群指纹不同）",
                    "connection_id": str(same_host.get("id") or ""),
                    "name": str(same_host.get("name") or ""),
                    "cluster_key": str(same_host.get("cluster_key") or ""),
                },
            )

        same_cluster = next(
            (c for c in conns if _same_cluster(c, probe)),
            None,
        )

        if same_host is not None:
            target = same_host
            status = "unchanged"
            message = "该 PVE 已注册，无需重复接入"
            if payload.force_update:
                merged = _merge_registered(target, payload, probe, region)
                conns = [
                    merged if str(c.get("id")) == str(target.get("id")) else c
                    for c in conns
                ]
                await store.save_connections(conns)
                target = merged
                status = "updated"
                message = "已更新已有连接的端点与凭据"
            elif payload.console_password:
                # 脚本每次执行都会轮换控制台密码（随机生成，没人记得住），所以连接
                # 虽然已存在也要把新密码写回去 —— 否则面板还拿旧密码换票据，VNC
                # 直接 401，而用户根本不知道新密码是什么
                merged = {**target, **_console_fields(payload)}
                conns = [
                    merged if str(c.get("id")) == str(target.get("id")) else c
                    for c in conns
                ]
                await store.save_connections(conns)
                target = merged
                status = "updated"
                message = "已同步控制台账号密码"
        elif same_cluster is not None:
            target = same_cluster
            message = (
                "该集群已通过连接「"
                + str(target.get("name") or target.get("id") or "")
                + "」接入，本次跳过。需要改用本节点地址时，"
                "在安装链接末尾加 &force=1 重跑（或在面板里改该连接的地址）"
            )
            status = "unchanged"
            if payload.force_update:
                merged = _merge_registered(target, payload, probe, region)
                conns = [
                    merged if str(c.get("id")) == str(target.get("id")) else c
                    for c in conns
                ]
                await store.save_connections(conns)
                target = merged
                status = "updated"
                message = "已把该集群的接入地址更新为本次节点"
            elif payload.console_password:
                # 同集群重复执行：地址与令牌不变，但控制台密码被轮换了，得同步
                merged = {**target, **_console_fields(payload)}
                conns = [
                    merged if str(c.get("id")) == str(target.get("id")) else c
                    for c in conns
                ]
                await store.save_connections(conns)
                target = merged
                status = "updated"
                message = "已同步控制台账号密码"
        else:
            profile = {"id": secrets.token_hex(4), **_register_profile(payload, probe, region)}
            conns.append(profile)
            await store.save_connections(conns)
            target = profile
            status = "created"
            message = "注册成功"
            if was_empty:
                # 第一条连接自动生效，否则面板还停在「未配置」状态
                await store.set_active_connection(str(profile["id"]))
                rebuild_client()

        if status == "updated" and store.get_active_connection_id() == str(
            target.get("id")
        ):
            rebuild_client()

    if status == "unchanged":
        # 面板什么都没改（同集群重复接入）：把这次占用的额度退回去。否则在同一
        # 个集群的第二台节点上跑一遍，剩余次数少了一次、列表里却什么都没多。
        await connregister.release(code_id)
        message += "（本次未消耗可用次数）"
    else:
        await connregister.bind_connection(code_id, str(target.get("id") or ""))

    # 本次操作之后这张码还剩几次：字面意义上的「实时可查」，脚本与面板都以它为准
    code_after = await connregister.get_code(code_id) or {}

    detail = {
        "host": payload.host,
        "port": payload.port,
        "cluster_key": probe["cluster_key"],
        "node_name": payload.node_name,
        "code_id": str(code_row.get("id") or ""),
        "status": status,
        "ip": security.client_ip(request),
    }
    # 审计里的「操作者」写成 ``pve-import:<节点名>``：这条路径没有登录用户，
    # 但排查时必须能回答「是哪台机器、从哪个地址把自己接进来的」。
    actor = "pve-import:" + (
        payload.node_name or security.client_ip(request) or "unknown"
    )
    await store.add_audit(
        username=actor,
        action="connection.register",
        target=f"connection/{target.get('id')}",
        result="success",
        detail=detail,
        ip=security.client_ip(request),
    )

    metrics_note = ""
    if not probe["node_metrics"]:
        metrics_note = (
            "令牌可以连接，但读不到节点指标（多半是权限不足）："
            "请在 PVE 上给该令牌的所属用户授予 PVEAuditor（或等价只读）权限"
        )

    return {
        "status": status,
        "message": message + (f"；{metrics_note}" if metrics_note else ""),
        "connection_id": str(target.get("id") or ""),
        "name": str(target.get("name") or ""),
        "region": str(target.get("region") or ""),
        "host": str(target.get("host") or ""),
        "port": int(target.get("port") or 8006),
        "cluster_key": probe["cluster_key"],
        "node_count": len(probe["node_names"]),
        "node_metrics": probe["node_metrics"],
        "pve_version": probe["version"],
        "pve_release": probe["release"],
        "active": store.get_active_connection_id() == str(target.get("id")),
        "max_uses": int(code_after.get("max_uses") or 0),
        "used_count": int(code_after.get("used_count") or 0),
        "remaining": code_after.get("remaining"),
    }


@router.put("/connections/{conn_id}")
async def update_connection_profile(
    conn_id: str,
    payload: ConnectionConfigIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, Any]:
    conns = store.get_connections()
    if _find_connection(conns, conn_id) is None:
        raise HTTPException(status_code=404, detail="连接不存在")

    data = payload.model_dump(exclude_none=True)
    data.pop("id", None)
    name = str(data.pop("name", "") or "")
    # 密钥单独处理：留空表示保留原值
    data.pop("token_secret", None)
    data.pop("console_password", None)

    updated: List[Dict[str, Any]] = []
    for c in conns:
        if str(c.get("id")) != conn_id:
            updated.append(c)
            continue
        merged = {**c, **data}
        if payload.token_secret not in (None, "", "__UNCHANGED__"):
            merged["token_secret"] = payload.token_secret
        if payload.console_password not in (None, "", "__UNCHANGED__"):
            merged["console_password"] = payload.console_password
        if name:
            merged["name"] = name
        if not merged.get("name"):
            merged["name"] = merged.get("host") or "未命名连接"
        updated.append(merged)

    await store.save_connections(updated)
    if store.get_active_connection_id() == conn_id:
        rebuild_client()

    await security.audit(request, user, "connection.update", target=f"connection/{conn_id}")
    pub = store.public_connection_config(_find_connection(updated, conn_id) or {})
    pub["active"] = store.get_active_connection_id() == conn_id
    return pub


@router.delete("/connections/{conn_id}")
async def delete_connection_profile(
    conn_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, Any]:
    conns = store.get_connections()
    remaining = [c for c in conns if str(c.get("id")) != conn_id]
    if len(remaining) == len(conns):
        raise HTTPException(status_code=404, detail="连接不存在")

    await store.save_connections(remaining)
    # 删除的是当前连接时，自动切到剩余的第一条
    if store.get_active_connection_id() == conn_id:
        new_active = str(remaining[0].get("id")) if remaining else ""
        await store.set_active_connection(new_active)
        rebuild_client()

    await security.audit(request, user, "connection.delete", target=f"connection/{conn_id}")
    return {"deleted": conn_id, "remaining": len(remaining)}


@router.post("/connections/{conn_id}/activate")
async def activate_connection(
    conn_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """把某条连接设为「当前连接」，全站立即切到它。"""
    conns = store.get_connections()
    if _find_connection(conns, conn_id) is None:
        raise HTTPException(status_code=404, detail="连接不存在")

    await store.set_active_connection(conn_id)
    rebuild_client()
    await security.audit(request, user, "connection.activate", target=f"connection/{conn_id}")
    return {"active": conn_id}


@router.post("/connections/{conn_id}/pause")
async def pause_connection(
    conn_id: str,
    payload: ConnectionPauseIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """暂停 / 恢复某条 PVE 连接的接入。

    暂停期间的语义（与界面文案一一对应）：

    * **不再采集**：节点列表、fleet 状态、连接探测、告警巡检一律跳过这台机器
      （``pve.all_connection_clients`` 过滤，``get_client`` 对它是 503）；
    * **不算异常**：它不会出现在逐连接的探测里、不会被计成离线，也不会触发
      离线 / 阈值 / 仲裁类告警；
    * **数据保留**：只是不再写入新数据，已有的指标历史、告警记录、受保护备份
      登记全部照旧；恢复后下一轮轮询自动重连，不需要重新填写凭据。

    只改 ``paused`` 一个字段：编辑连接时提交的是整份配置，开关状态不该被一次
    无关的保存覆盖，也不该因为两个人同时编辑而互相打架。
    """
    conns = store.get_connections()
    if _find_connection(conns, conn_id) is None:
        raise HTTPException(status_code=404, detail="连接不存在")

    updated: List[Dict[str, Any]] = [
        {**c, "paused": bool(payload.paused)} if str(c.get("id")) == conn_id else c
        for c in conns
    ]
    await store.save_connections(updated)
    # 兜底客户端缓存里可能还留着这台机器的连接对象，重建一次让它立即让位
    rebuild_client()

    await security.audit(
        request,
        user,
        "connection.pause" if payload.paused else "connection.resume",
        target=f"connection/{conn_id}",
        detail={"paused": bool(payload.paused)},
    )
    pub = store.public_connection_config(_find_connection(updated, conn_id) or {})
    pub["active"] = store.get_active_connection_id() == conn_id
    return pub


@router.post("/connections/{conn_id}/repair-permissions")
async def repair_connection_permissions(
    conn_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """修复连接的 PVE 令牌权限。

    要解决的问题：在 PVE 界面新建令牌时「特权分离」默认勾选，这种令牌的有效权限
    为空，PVE 会隐藏节点 CPU / 内存 / 磁盘指标、特权接口一律 403 —— 表现就是
    「面板读不到这台机器」。修它是一次 ACL 变更，而零权限的令牌不能给自己授权，
    所以借用连接里保存的账号密码换票据来改。

    借用谁很关键：改 ACL 需要 ``Permissions.Modify``。旧版一键接入建的账号是
    ``pvecenter@pve``（CenterPanel 角色，刻意不含 Permissions.Modify）与
    ``pvecenter-console@pve``（只有 VM.Audit + VM.Console），拿它们的凭据去改
    ACL 只会得到 ``Permission check failed (/, Permissions.Modify)`` —— 不是按钮
    坏了，而是这条路根本走不通。所以先看能不能修：能修就修，修不了给准话。
    """
    conns = store.get_connections()
    profile = _find_connection(conns, conn_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="连接不存在")

    conn = connection_from_profile(profile)
    if not conn.token_id:
        raise HTTPException(status_code=400, detail="该连接未填写 API Token ID")

    token_owner = (conn.token_id.split("!")[0] or "").strip()

    # 1) 先用令牌本身探一次：root@pam 的全权令牌压根没什么可修的
    privileges: List[str] = []
    has_modify = False
    node_metrics = False
    probe = ProxmoxClient(conn)
    try:
        perms = await probe.access_permissions()
        for path, privs in (perms or {}).items():
            for priv in privs or []:
                privileges.append(f"{priv}@{path}")
                if priv == "Permissions.Modify" and path == "/":
                    has_modify = True
        nodes = await probe.nodes() or []
        node_metrics = bool(nodes) and any(n.get("maxmem") is not None for n in nodes)
    except ProxmoxError as exc:
        # 零权限令牌读 /access/permissions 也会 403，这正是要修的形态，继续往下走
        logger.info("探测 %s 的令牌权限失败：%s", conn.token_id, exc.message)
    finally:
        await probe.aclose()

    if has_modify:
        return {
            "ok": True,
            "already_ok": True,
            "token_id": conn.token_id,
            "role": "",
            "effective_privileges": sorted(set(privileges)),
            "node_metrics": node_metrics,
        }

    # 2) 只有 root@pam 的账号密码能改 ACL；受限账号去改只会 403
    console_user = (conn.console_user or "").strip()
    if console_user != "root@pam" or not conn.console_password:
        if token_owner == "root@pam":
            detail = (
                "令牌属于 root@pam，但创建时勾选了「特权分离」，它不继承 root 的权限，"
                "而 PVE 不支持事后取消这一项。重新执行一次一键接入链接即可：新令牌以"
                " --privsep 0 创建，权限一次到位。"
            )
        else:
            detail = (
                f"这条连接用的是 {token_owner or '受限账号'}，它没有「权限管理」"
                "（Permissions.Modify），而 PVE 不允许账号给自己授权，所以没法就地修。"
                "请到「一键接入」重新生成链接并在节点上执行一次：面板账号会换成"
                " root@pam 的全权令牌。"
            )
        raise HTTPException(status_code=400, detail=detail)

    # 3) 用 root@pam 换票据，给令牌在 / 上授予 PVEAdmin
    admin = ProxmoxClient(conn)
    try:
        await admin.grant_token_role(conn.token_id, role="PVEAdmin", path="/")
    except ProxmoxError as exc:
        raise HTTPException(
            status_code=400, detail=f"授予权限失败：{exc.message}"
        ) from exc
    finally:
        await admin.aclose()

    # 4) 复验：改用令牌本身再读一次权限与节点指标，确认真的生效
    verify = ProxmoxClient(conn)
    privileges = []
    node_metrics = False
    try:
        perms = await verify.access_permissions()
        for path, privs in (perms or {}).items():
            privileges.extend(f"{priv}@{path}" for priv in privs or [])
        nodes = await verify.nodes() or []
        node_metrics = bool(nodes) and any(n.get("maxmem") is not None for n in nodes)
    except ProxmoxError as exc:
        raise HTTPException(
            status_code=400, detail=f"已提交授权，但复验失败：{exc.message}"
        ) from exc
    finally:
        await verify.aclose()

    if not privileges:
        raise HTTPException(
            status_code=400,
            detail=(
                "已提交授权，但令牌仍无有效权限。请确认控制台账号本身具备"
                "「权限管理」权限（通常是 root@pam）。"
            ),
        )

    await security.audit(
        request, user, "connection.repair", target=f"connection/{conn_id}",
        detail={"token_id": conn.token_id, "role": "PVEAdmin"},
    )

    return {
        "ok": True,
        "already_ok": False,
        "token_id": conn.token_id,
        "role": "PVEAdmin",
        "effective_privileges": sorted(set(privileges)),
        "node_metrics": node_metrics,
    }


@router.get("/config/diagnostics")
async def diagnostics(
    connection_id: str = Query(default=""),
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """Probe the configured credential and report what the panel can actually do.

    This exists because of a Proxmox behaviour that is easy to misread as a
    panel bug. An API token created with "privilege separation" (the default
    in the web UI) starts with *no* permissions: privileged reads answer 403,
    and some endpoints — ``/storage`` in particular — answer 200 with an empty
    list rather than an error. The panel then honestly renders an empty
    dashboard, and the operator concludes the panel is broken.

    So we ask Proxmox directly for the credential's effective privileges and
    turn the gaps into concrete remediation commands.

    **必须说明探测的是哪一条连接。** 多连接部署里 ``get_client()`` 在未指定
    连接时只读「兜底连接」（``store.get_active_connection_id()``，通常是第一条
    保存的连接）。于是刚新增完一台节点、在它的表单上点「环境自检」，报告里
    回的却是旧连接的 ``host``（例如新增 172.16.149.42，报告显示
    ``https://172.16.149.3:8006``）—— 看起来像节点 IP 存错了，实际是自检
    打错了主机。``?connection_id=`` 或 ``X-PVE-Connection`` 指定后，凭据与
    地址都取自那一条连接，报告的 ``connection_id`` / ``host`` 才说得清归属。
    """
    # 目标连接：显式参数 → 请求头 / 顶栏作用域 → 兜底连接。
    # 与 pve.get_client() 同一套解析口径，报告里的 connection_id 就是真正被
    # 探测的那条，不会出现「报告写 A、实际打 B」。
    conn_id = (connection_id or "").strip() or requested_connection()
    profiles = store.get_connections()
    if conn_id:
        profile: Optional[Dict[str, Any]] = next(
            (c for c in profiles if str(c.get("id") or "") == conn_id), None
        )
        # 存的是已删除的 id（老页面标签页 / 顶栏作用域残留）：直接说清楚，
        # 别让它退化成一次「连不上」的 502。
        if profile is None:
            raise HTTPException(status_code=404, detail="连接不存在")
    else:
        conn_id = str(store.get_active_connection_id() or "")
        profile = next(
            (c for c in profiles if str(c.get("id") or "") == conn_id), None
        )

    # 按 id 取客户端：命中的是这条连接自己的 host / port / 令牌，
    # 而不是全局那个「兜底」客户端（见 pve.client_for_connection）。
    client = client_for_connection(conn_id)

    checks: List[Dict[str, Any]] = []

    def add(key: str, label: str, status: str, detail: str, hint: str = "") -> None:
        checks.append(
            {"key": key, "label": label, "status": status, "detail": detail, "hint": hint}
        )

    result: Dict[str, Any] = {
        "ok": False,
        "reachable": False,
        # 报告回显「这次探测的是哪条连接」：host 只说明打到哪台 PVE，
        # 同一台 PVE 也可能被登记成多条连接（端口 / 令牌不同）。
        "connection_id": conn_id,
        "connection_name": str(
            (profile or {}).get("name") or (profile or {}).get("host") or ""
        ),
        # host 由 PveConnection.base_url 拼出：协议前缀 https:// + 该连接的
        # host:port（未显式给协议时补 https，端口缺省 8006）——
        # 取值来源就是上面这条连接的 profile，不再来自全局兜底客户端。
        "host": client.conn.base_url,
        "token_id": client.conn.token_id,
        "pve_version": None,
        "effective_privileges": [],
        "checks": checks,
        "remediation": [],
    }

    if not client.conn.configured:
        add("configured", "连接配置", "fail", "尚未填写主机或 API Token")
        result["remediation"].append("在「Proxmox 连接配置」中填写主机地址与 API Token")
        result["status"] = "fail"
        return result

    # --- 1. reachability ---------------------------------------------------
    try:
        version = await client.version()
        result["reachable"] = True
        result["pve_version"] = version.get("version")
        add(
            "version",
            "API 可达",
            "ok",
            f"Proxmox VE {version.get('version')}（{version.get('release')}）",
        )
    except ProxmoxError as exc:
        add("version", "API 可达", "fail", exc.message)
        result["remediation"].append(
            "检查主机地址、8006 端口连通性与 Token Secret；"
            "若集群使用自签名证书，可关闭「校验证书」"
        )
        result["status"] = "fail"
        return result

    # --- 2. effective privileges ------------------------------------------
    privileges: List[str] = []
    no_privileges = False
    try:
        perms = await client.access_permissions()
        for path, privs in (perms or {}).items():
            for priv in privs or []:
                privileges.append(f"{priv}@{path}")
        no_privileges = not privileges
    except ProxmoxError as exc:
        add("privileges", "权限查询", "warn", f"无法读取 /access/permissions：{exc.message}")

    result["effective_privileges"] = sorted(set(privileges))

    if no_privileges:
        add(
            "privileges",
            "令牌有效权限",
            "fail",
            "该令牌没有任何有效权限（/access/permissions 返回空）",
            "令牌创建时很可能勾选了「特权分离（Privilege Separation）」。"
            "特权分离的令牌默认零权限，必须显式授予 ACL 才能使用。",
        )
        result["remediation"].append(
            f"pveum acl modify / --tokens '{client.conn.token_id}' --roles PVEAdmin"
        )
        result["remediation"].append(
            "或在 Proxmox 界面「数据中心 → 权限 → 添加 → API 令牌权限」中授予角色"
        )
        add(
            "storage",
            "存储可见性",
            "warn",
            "未做进一步检测：无权限时该接口会返回空列表而非报错",
        )
        add("vms", "虚拟机读取", "warn", "未做进一步检测：无权限时结果同样为空")
        result["checks"] = checks
        result["status"] = "fail"
        return result

    # --- 3. node visibility ------------------------------------------------
    nodes: List[Dict[str, Any]] = []
    try:
        nodes = await client.nodes() or []
        if nodes:
            names = "、".join(n.get("node", "?") for n in nodes)
            add("nodes", "节点可见", "ok", f"{len(nodes)} 个节点：{names}")
        else:
            add(
                "nodes",
                "节点可见",
                "fail",
                "看不到任何节点，需在 / 或 /nodes 上授予 Sys.Audit",
            )
            result["remediation"].append(
                f"pveum acl modify / --tokens '{client.conn.token_id}' --roles PVEAuditor"
            )
    except ProxmoxError as exc:
        add("nodes", "节点可见", "fail", exc.message)

    # A node with no metrics is the signature of a missing Sys.Audit.
    if nodes and all(n.get("maxmem") is None for n in nodes):
        add(
            "node_metrics",
            "节点资源指标",
            "fail",
            "/nodes 未返回 CPU / 内存 / 磁盘数据（字段被权限过滤）",
            "实时监控与资源大盘依赖 Sys.Audit",
        )
        result["remediation"].append(
            f"pveum acl modify / --tokens '{client.conn.token_id}' --roles PVEAuditor"
        )
    elif nodes:
        add("node_metrics", "节点资源指标", "ok", "CPU / 内存 / 磁盘数据可读")

    # --- 4. storage --------------------------------------------------------
    try:
        storages = await client.storages() or []
        if storages:
            add("storage", "存储可见", "ok", f"{len(storages)} 个存储")
        else:
            add(
                "storage",
                "存储可见",
                "warn",
                "/storage 返回空列表。若集群确实配置了存储，通常是缺少 Datastore.Audit 权限",
                "创建虚拟机与导入镜像都需要选择存储",
            )
            result["remediation"].append(
                f"pveum acl modify / --tokens '{client.conn.token_id}' --roles PVEDatastoreUser"
            )
    except ProxmoxError as exc:
        add("storage", "存储可见", "fail", exc.message)

    # --- 5. guests ---------------------------------------------------------
    try:
        guests = await client.cluster_resources("vm") or []
        running = sum(1 for g in guests if g.get("status") == "running")
        add(
            "guests",
            "虚拟机可见",
            "ok",
            f"{len(guests)} 台虚拟机（运行中 {running} 台）",
        )
    except ProxmoxError as exc:
        add("guests", "虚拟机可见", "fail", exc.message)

    # --- 6. console credentials -------------------------------------------
    if client.conn.console_user and client.conn.console_password:
        add("console", "控制台凭据", "ok", f"已配置账号 {client.conn.console_user}")
    else:
        add(
            "console",
            "控制台凭据",
            "warn",
            "未配置。Proxmox 不接受 API Token 打开 VNC 控制台",
            "如需浏览器内控制台，请额外填写一个 Proxmox 账号密码",
        )

    # Overall verdict. "warn" still means the panel is usable — a missing
    # optional console account, for example, does not block anything — while
    # "fail" means core functionality is unavailable.
    worst = "ok"
    for check in checks:
        if check["status"] == "fail":
            worst = "fail"
            break
        if check["status"] == "warn":
            worst = "warn"

    result["ok"] = worst != "fail"
    result["status"] = worst
    return result


@router.post("/config/connection/test")
async def test_connection(
    payload: ConnectionTestIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """Try a candidate connection without persisting it.

    An empty ``token_secret`` means "use the stored one", which lets the user
    re-test a saved config without retyping the secret.

    **「存的那一份」必须是这条连接的**：多连接部署里不能再无条件读
    ``store.get_connection_config()``（那是「兜底连接」，通常是第一条保存的
    连接）。编辑第二条连接时留空 secret，拿到的会是第一条的密钥，于是报
    「Proxmox 拒绝了该凭据」—— 而凭据其实是好的。连接取自
    ``connection_id``（请求体）或 ``X-PVE-Connection`` 头，两者都没有时才
    回落到兜底连接（旧行为，供单连接部署与脚本使用）。

    两条硬约束（SSRF / 凭据外带的兜底）：

    * **目标必须可信**：只允许已保存连接里登记的 host:port，或运维在
      ``CONNECTION_TEST_ALLOW_TARGETS`` 里显式放行的地址。面板与 PVE / PBS /
      跳板机同网段，放任前端指定 host 就等于给了一台内网扫描器；
    * **复用的密钥不能跟着任意目标走**：留空 secret 时，目标必须就是那条已保存
      连接本身，否则等于把面板持有的 Token 发到调用方指定的主机上。
    """
    profiles = store.get_connections()
    # .env 里的 PVE_HOST 也是「已配置的连接」（见 pve._load_connection 的兜底），
    # 没有落库连接时同样允许测试它。
    registered = ssrfguard.registered_targets(profiles, 8006)
    if settings.pve_host:
        registered |= ssrfguard.registered_targets(
            [{"host": settings.pve_host, "port": settings.pve_port}], 8006
        )

    try:
        await ssrfguard.assert_target_allowed(
            payload.host,
            payload.port,
            registered=registered,
        )
    except ssrfguard.TargetBlocked as exc:
        await security.audit(
            request, user, "config.test", target=payload.host,
            result="blocked", detail={"code": exc.code},
        )
        raise HTTPException(status_code=400, detail=exc.message) from exc

    secret = payload.token_secret
    if not secret:
        conn_id = (payload.connection_id or "").strip() or requested_connection()
        if conn_id:
            profile: Optional[Dict[str, Any]] = next(
                (
                    c
                    for c in profiles
                    if str(c.get("id") or "") == conn_id
                ),
                None,
            )
            # 指向已删除的连接：说清楚，别退化成「凭据被拒绝」
            if profile is None:
                raise HTTPException(status_code=404, detail="连接不存在")
        else:
            profile = store.get_connection_config()
        if profile and not ssrfguard.same_target(
            payload.host, payload.port, profile, 8006
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "留空 Token Secret 时，目标必须是该连接已保存的 host:port"
                    "（避免把已存密钥发送到别的地址）。请重新填写 Secret，"
                    "或先保存这条连接再测试。"
                ),
            )
        secret = str((profile or {}).get("token_secret") or "")

    if not secret:
        raise HTTPException(status_code=400, detail="缺少 API Token Secret")

    conn = PveConnection(
        host=payload.host,
        port=payload.port,
        token_id=payload.token_id,
        token_secret=secret,
        verify_ssl=payload.verify_ssl,
        ca_cert=payload.ca_cert,
    )
    client = ProxmoxClient(conn)

    try:
        version = await client.version()
        nodes = await client.nodes()
    except ProxmoxError as exc:
        # 只回归一化后的枚举与固定文案：对端响应正文 / 请求路径一律不外带，
        # 否则「连不上 / 不是 TLS / 认证失败」的差异本身就能当扫描器用。
        code, message = ssrfguard.classify_failure(exc)
        # 诊断细节只进服务端日志（压成单行，避免对端正文里的换行污染日志），
        # 不回给前端。
        logger.warning(
            "连接测试失败（%s）：%s", code, " ".join(str(exc.message).split())[:300]
        )
        await security.audit(
            request, user, "config.test", target=payload.host,
            result="failed", detail={"code": code},
        )
        raise HTTPException(status_code=400, detail=message) from exc
    finally:
        await client.aclose()

    await security.audit(
        request, user, "config.test", target=payload.host, result="success",
        detail={"version": version.get("version")},
    )

    return {
        "ok": True,
        "version": version.get("version"),
        "release": version.get("release"),
        "repoid": version.get("repoid"),
        "nodes": [
            {"name": n.get("node"), "status": n.get("status"), "type": n.get("type")}
            for n in (nodes or [])
        ],
    }


# 读取在 ``GET /api/runtime/vm-defaults``（创建向导向所有用户预填，不能要求
# 管理权限）；这里只剩写入口，整个 ``/api/config`` 前缀统一 ``settings.manage``。
@router.put("/config/vm-defaults")
async def put_vm_defaults(
    payload: VmDefaultsIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """保存默认 DNS 与默认克隆方式。

    DNS 留空 = 不干预，创建/克隆时完全继承 DHCP / RA 下发的 DNS；
    full_clone 只是各创建入口预选的初值（False 链接克隆 / True 完整克隆），
    用户在快速部署、自定义部署、模板页克隆里都可以单独改。
    """
    saved = await defaults.set_vm_defaults(payload.dns, payload.full_clone)
    await security.audit(
        request,
        user,
        "config.vm_defaults",
        target="vm-defaults",
        detail={
            "dns": saved["dns"] or "(空)",
            "full_clone": "完整克隆" if saved["full_clone"] else "链接克隆",
        },
    )
    return saved


# ==================================================== 资源规格（管理员定义）
# 用户下单时挑「几核几 G 多大盘」，规格由管理员在设置页维护。
#
# 读在 ``GET /api/runtime/specs``（还有 ``/specs/presets``）：下单的人得能挑，
# 所以那条只要求登录；写留在这里 —— ``/api/config/*`` 整组 ``settings.manage``。
@router.put("/config/specs")
async def put_specs(
    payload: List[Dict[str, Any]],
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """整份覆盖保存（前端本地编辑完一次性提交，与 IP 池同一契约）。

    不合法的条目会被逐条丢掉而不是整份拒绝（见 :func:`app.specs.normalise`），
    返回值就是真正落库的那份，前端据此回显。
    """
    saved = await specs.save_specs(payload)
    await security.audit(
        request,
        user,
        "config.specs",
        target="resource-specs",
        detail={"count": len(saved), "names": [s["name"] for s in saved][:8]},
    )
    return {"specs": saved}


# ============================================================ 登录验证方式
# 三档：关闭 / 图形验证码 / 拖动滑块。存在 settings KV 表，改完立即生效，
# 不需要重启（登录接口每次现读，见 captcha.get_mode）。
#
# 读与写都在 ``settings.manage`` 这一档（路由级守卫，见 app/routeguard.py）：
# 它不是凭据，但「登录门槛现在关掉了」本身就是攻击者最想确认的一条情报 ——
# 只有打算登录的人才需要读它，而这个人应该是管理员。
# 写入还要**过二次确认** —— 把验证码关掉等于降低整个面板的登录门槛，和改连接
# 凭据属于同一类操作，不该只凭一个在线的会话就能完成。


@router.get("/config/login-captcha")
async def get_login_captcha(
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, str]:
    return {"mode": await captcha.get_mode()}


@router.put("/config/login-captcha")
async def put_login_captcha(
    payload: LoginCaptchaIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, str]:
    try:
        mode = await captcha.set_mode(payload.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await security.audit(
        request,
        user,
        "config.login_captcha",
        target="login-captcha",
        detail={"mode": mode, "label": captcha.MODE_LABEL.get(mode, mode)},
    )
    return {"mode": mode}


# ============================================================ 注册方式
# 四种模式互斥（关闭 / 邀请制 / 注册码制 / 自助），存 settings KV，改完下一
# 次注册请求就生效，不用重启。整组读写都在 ``/api/config`` 前缀下，路由守卫
# 已经统一收进 ``settings.manage``；写操作再加 step-up，并落审计 —— 换注册
# 方式等于放开一个匿名写入口，必须留痕。


@router.get("/config/registration")
async def get_registration(
    _user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """注册方式 + 凭证列表（不含明文，库里也没有）。"""
    mode = await regcodes.current_mode()
    configured = await regcodes.configured_mode()
    return {
        "mode": mode,
        "requires": regcodes.MODE_KIND.get(mode) or "",
        # 是否在设置页显式选过；null 表示仍跟着 REGISTER_ENABLED 走
        "configured": configured,
        "modes": list(regcodes.MODES),
        # 自助注册的邮箱验证码开关：只对 open 模式生效，但设置页要能提前看到它
        "email_code_enabled": await emailcodes.enabled(),
        "codes": await regcodes.list_codes(),
        "limits": {
            "default_ttl_minutes": regcodes.DEFAULT_TTL_MINUTES,
            "default_max_uses": regcodes.DEFAULT_MAX_USES,
            "max_ttl_minutes": regcodes.MAX_TTL_MINUTES,
            "max_uses": regcodes.MAX_USES_LIMIT,
        },
    }


@router.put("/config/registration/mode")
async def put_registration_mode(
    payload: RegistrationModeIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, Any]:
    mode = regcodes.normalize_mode(payload.mode)
    if mode is None:
        raise HTTPException(status_code=400, detail="未知的注册方式")
    previous = await regcodes.current_mode()
    await regcodes.set_mode(mode)
    if previous != mode:
        logger.info("注册方式已切换：%s → %s（%s）", previous, mode, user.get("username"))
    await security.audit(
        request,
        user,
        "config.registration_mode",
        target="registration",
        detail={"from": previous, "to": mode},
    )
    return {"mode": mode, "requires": regcodes.MODE_KIND.get(mode) or ""}


@router.put("/config/registration/email-code")
async def put_registration_email_code(
    payload: RegistrationEmailCodeIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, Any]:
    """自助注册的邮箱验证码开关。

    单独一个接口、只改这一个字段：整份「保存配置」会覆盖所有字段，两个管理员
    同时编辑时，后保存的那份会把对方刚改的开关带回旧值。

    关掉之后自助注册退回「只要人机校验」的老行为 —— 邮件服务没配好、或者不想
    让注册依赖 SMTP 的部署可以走这条路，但代价是邮箱不再被验证（批量注册与
    「用别人的邮箱注册」重新变得没有成本），确认弹窗里已经把这句话讲明白。
    """
    previous = await emailcodes.enabled()
    enabled = await emailcodes.set_enabled(bool(payload.enabled))
    if previous != enabled:
        logger.info(
            "注册邮箱验证码：%s → %s（%s）", previous, enabled, user.get("username")
        )
    await security.audit(
        request,
        user,
        "config.registration_email_code",
        target="registration",
        detail={"from": previous, "to": enabled},
    )
    return {"email_code_enabled": enabled}


@router.post("/config/registration/codes")
async def post_registration_code(
    payload: RegistrationCodeIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, Any]:
    """签发注册码。**明文只在这个响应里出现一次**。

    邀请制已只保留邀请链接，所以这里不再接受 ``kind=invite``：存量邀请码
    仍在表里（不迁移），但既不能新发，也不会被注册流程校验。
    """
    kind = regcodes.normalize_kind(payload.kind)
    if kind is None:
        raise HTTPException(status_code=400, detail="未知的凭证类型")
    if kind == regcodes.KIND_INVITE:
        raise HTTPException(
            status_code=400,
            detail="邀请制已改为仅支持邀请链接，请在签发凭证里选择邀请链接",
        )
    # 注册码也能提前指定角色与额度：注册那一刻写进新账号，不必等审批再设一遍
    role_id = str(payload.role or "viewer").strip()
    role = await store.get_role(role_id)
    if not role:
        raise HTTPException(status_code=400, detail=f"角色 {role_id} 不存在")
    await security.ensure_can_assign_role(user, role_id)
    limits = quota.provided_limits(payload.quota)
    row = await regcodes.create_code(
        kind=kind,
        note=payload.note,
        created_by=str(user.get("username") or ""),
        ttl_minutes=payload.ttl_minutes,
        max_uses=payload.max_uses,
        role=role_id,
        quota=limits,
        requires_approval=payload.requires_approval,
    )
    await security.audit(
        request,
        user,
        "config.registration_code_create",
        target=str(row.get("id") or ""),
        detail={
            "kind": kind,
            "note": row.get("note"),
            "expires": row.get("expires"),
            "max_uses": row.get("max_uses"),
            "role": role_id,
            "quota": bool(limits),
            "requires_approval": payload.requires_approval,
        },
    )
    return {**row, "role_name": str(role.get("name") or role_id)}


@router.post("/config/registration/codes/{code_id}/revoke")
async def revoke_registration_code(
    code_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, Any]:
    row = await regcodes.get_code(code_id)
    if row is None:
        raise HTTPException(status_code=404, detail="凭证不存在")
    if not await regcodes.revoke(code_id):
        raise HTTPException(status_code=404, detail="凭证不存在")
    await security.audit(
        request,
        user,
        "config.registration_code_revoke",
        target=code_id,
        detail={"kind": row.get("kind"), "note": row.get("note")},
    )
    return {"id": code_id, "revoked": True}


@router.delete("/config/registration/codes/{code_id}")
async def delete_registration_code(
    code_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, Any]:
    """删除凭证：只允许删「已不可能再用」的（撤销 / 过期 / 次数用尽）。"""
    row = await regcodes.get_code(code_id)
    if row is None:
        raise HTTPException(status_code=404, detail="凭证不存在")
    if not await regcodes.delete_code(code_id):
        raise HTTPException(
            status_code=400, detail="仍然有效的凭证不能删除，请先撤销"
        )
    await security.audit(
        request,
        user,
        "config.registration_code_delete",
        target=code_id,
        detail={"kind": row.get("kind"), "note": row.get("note")},
    )
    return {"id": code_id, "deleted": True}


# ============================================================ 邀请链接
# 链接属于「邀请制」：它是邀请凭证的另一种载体（URL 代替文本码），可附角色与
# 审批策略。模式仍是邀请制，注册入口仍是 /register，校验与审批走同一套规则。
#
# 分发出去的是 URL（``/register?invite=<token>``），所以 api 回包里直接给
# ``path``，前端拿 ``window.location.origin`` 一拼即可，不必自己知道参数名。


def _invite_path(token: str) -> str:
    return f"/register?invite={quote(str(token or ''))}"


async def _role_name(role_id: str) -> str:
    """角色名缓存不了（角色随时可改），但这张表一次只查一条，够用。"""
    role = await store.get_role(str(role_id or "")) or {}
    return str(role.get("name") or role_id or "")


@router.get("/config/invites")
async def list_invite_links(
    _user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """邀请链接列表（含 token：链接需要能被再次复制，见 app/invites.py 模块头）。"""
    rows = await invites.list_invites()
    roles = {str(r.get("id")): str(r.get("name") or r.get("id")) for r in await store.list_roles()}
    for row in rows:
        row["role_name"] = roles.get(row["role"], row["role"])
        row["path"] = _invite_path(row["token"])
    return {
        "invites": rows,
        "limits": {
            "max_uses": invites.MAX_USES_LIMIT,
            "max_ttl_days": invites.MAX_TTL_DAYS,
            "default_ttl_days": invites.DEFAULT_TTL_DAYS,
        },
    }


@router.post("/config/invites")
async def create_invite_link(
    payload: InviteLinkIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
    # 建链接 = 预先决定一批新账号的角色，与「审批」是同一量级的授权行为
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, Any]:
    role_id = str(payload.role or "viewer").strip()
    role = await store.get_role(role_id)
    if not role:
        raise HTTPException(status_code=400, detail=f"角色 {role_id} 不存在")
    # 授予链边界：不能造一个「进来就比自己权限高」的入口
    await security.ensure_can_assign_role(user, role_id)

    limits = quota.provided_limits(payload.quota)
    row = await invites.create_invite(
        role=role_id,
        note=payload.note,
        created_by=str(user.get("username") or ""),
        requires_approval=payload.requires_approval,
        max_uses=payload.max_uses,
        ttl_days=payload.ttl_days,
        quota=limits,
    )
    await security.audit(
        request,
        user,
        "config.invite_create",
        target=str(row.get("id") or ""),
        detail={
            "role": role_id,
            "requires_approval": bool(payload.requires_approval),
            "max_uses": row.get("max_uses"),
            "expires": row.get("expires"),
            "note": row.get("note"),
            "quota": bool(limits),
        },
    )
    return {**row, "role_name": str(role.get("name") or role_id), "path": _invite_path(row["token"])}


@router.post("/config/invites/{invite_id}/revoke")
async def revoke_invite_link(
    invite_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, Any]:
    row = await invites.get_invite(invite_id)
    if row is None:
        raise HTTPException(status_code=404, detail="邀请链接不存在")
    if not await invites.revoke(invite_id):
        raise HTTPException(status_code=404, detail="邀请链接不存在")
    await security.audit(
        request,
        user,
        "config.invite_revoke",
        target=invite_id,
        detail={"note": row.get("note"), "used_count": row.get("used_count")},
    )
    return {"id": invite_id, "revoked": True}


@router.delete("/config/invites/{invite_id}")
async def delete_invite_link(
    invite_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, Any]:
    """删除：只允许删「已不可能再用」的（失效 / 过期 / 次数用尽）。"""
    row = await invites.get_invite(invite_id)
    if row is None:
        raise HTTPException(status_code=404, detail="邀请链接不存在")
    if not await invites.delete_invite(invite_id):
        raise HTTPException(status_code=400, detail="仍然有效的邀请链接不能删除，请先失效")
    await security.audit(
        request,
        user,
        "config.invite_delete",
        target=invite_id,
        detail={"note": row.get("note"), "used_count": row.get("used_count")},
    )
    return {"id": invite_id, "deleted": True}


# ============================================================ 会话有效期
# 管理员可调：访问令牌多久过期、会话（refresh token）保留几天。
#
# 存 settings KV，改完**立即生效**、不用重启 —— security.py 里有一份进程内
# 缓存（签发令牌是同步函数，不能在那里查库），写入路径负责刷新它。
#
# 读**不再**「登录即可」：这份读数把会话窗口、上下限与出厂默认值一并回给调用方，
# 等于告诉对方「偷到的 cookie 还能用多久、多久不动会被踢」。只有设置页用它，
# 所以整组收进 ``settings.manage``（路由级守卫，见 app/routeguard.py）。
# 写入还要二次确认：把有效期调成 30 天等于放松整个面板的会话保障。


@router.get("/config/session")
async def get_session_policy(
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    return {
        "access_minutes": security.session_access_minutes(),
        "refresh_days": security.session_refresh_days(),
        "limits": {
            "access_min": security.SESSION_ACCESS_MIN,
            "access_max": security.SESSION_ACCESS_MAX,
            "refresh_min": security.SESSION_REFRESH_MIN,
            "refresh_max": security.SESSION_REFRESH_MAX,
        },
        # 环境变量里的出厂默认值：界面上标一句「默认 720 分钟」，
        # 让管理员知道自己是改过还是没改过
        "defaults": {
            "access_minutes": int(settings.access_token_expire_minutes),
            "refresh_days": int(settings.refresh_token_expire_days),
        },
    }


@router.put("/config/session")
async def put_session_policy(
    payload: SessionPolicyIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, int]:
    await store.set_setting("session_access_minutes", str(payload.access_minutes))
    await store.set_setting("session_refresh_days", str(payload.refresh_days))
    await security.refresh_session_policy()

    await security.audit(
        request,
        user,
        "config.session",
        target="session-policy",
        detail={
            "access_minutes": payload.access_minutes,
            "refresh_days": payload.refresh_days,
        },
    )
    return {
        "access_minutes": security.session_access_minutes(),
        "refresh_days": security.session_refresh_days(),
    }


# ============================================================ 面板界面开关
# 即「设置 → 导航栏功能开关」（见 app/ui.py）：逐项关闭导航栏里的入口。
#
# 读在 ``GET /api/runtime/ui``：控制台外壳在渲染前就得拿到它，只给管理员读的
# 话，被关掉的入口对其他用户依然可见 —— 所以那条只要求登录。写留在这里：
# 这个开关对所有用户生效，属于管理动作（``settings.manage``）。


@router.put("/config/ui")
async def put_ui_prefs(
    payload: UiPrefsIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """保存面板界面开关（被关闭的导航栏入口）。保存后立即对所有人生效。"""
    saved = await ui.set_ui_prefs(nav_disabled=payload.nav_disabled)
    await security.audit(
        request,
        user,
        "config.ui_prefs",
        target="ui-prefs",
        # 关掉了哪些入口是这次改动的全部内容，审计日志里必须能看出来
        detail={"nav_disabled": saved["nav_disabled"]},
    )
    return saved


# =============================================================== site info
# 站点信息（品牌名 / 副标题 / 版权）。登录页与产品官网都要在「未登录」状态下
# 展示品牌名，所以这里刻意不做鉴权：读取只返回可公开的品牌文案，不含任何
# 连接信息或凭据。写入仍然要求 settings.manage。
#
# **它是 routeguard 的匿名白名单成员**（``routeguard.PUBLIC_ROUTES``），
# 和 Logo / 登录页背景图 / FAQ 同一类。往这个前缀下再加端点时不要顺手把它当
# 例子 —— 白名单是按「方法 + 完整路径」精确匹配的，新端点默认走 settings.manage。


@router.get("/config/site")
async def get_site() -> Dict[str, Any]:
    """站点信息。无需登录：登录页 / 落地页的标题都依赖它。"""
    return await site.get_site_info()


@router.put("/config/site")
async def put_site(
    payload: SiteInfoIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """保存站点信息。品牌三项留空恢复默认值，备案号与友链留空则不展示。"""
    saved = await site.set_site_info(
        name=payload.name,
        subtitle=payload.subtitle,
        copyright_text=payload.copyright,
        icp=payload.icp,
        links=[link.model_dump() for link in payload.links],
    )
    await security.audit(
        request,
        user,
        "config.site_info",
        target="site-info",
        detail={
            "name": saved["name"],
            "subtitle": saved["subtitle"],
            "icp": saved["icp"] or "(空)",
            # 只记条数：审计日志没必要存一长串外部地址
            "links": len(saved["links"]),
        },
    )
    return saved


# ================================================================ 常见问题
# 与站点信息同样刻意不鉴权：产品官网在未登录状态下就要渲染 FAQ。
# 读取只返回问题与答案，不含任何连接信息或凭据。


@router.get("/config/faq")
async def get_faq() -> List[Dict[str, str]]:
    """常见问题。无需登录：产品官网的 FAQ 区块依赖它。"""
    return await faq.get_faqs()


@router.put("/config/faq")
async def put_faq(
    payload: FaqIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> List[Dict[str, str]]:
    """保存常见问题。传空列表 = 关闭官网上的 FAQ 区块。"""
    saved = await faq.set_faqs([item.model_dump() for item in payload.items])
    await security.audit(
        request,
        user,
        "config.faq",
        target="site-faq",
        detail={"count": len(saved)},
    )
    return saved


@router.delete("/config/faq")
async def delete_faq(
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> List[Dict[str, str]]:
    """恢复内置的默认问题（删掉配置记录，回到「从未配置」状态）。"""
    restored = await faq.reset_faqs()
    await security.audit(request, user, "config.faq.reset", target="site-faq")
    return restored


@router.get("/config/site/logo", response_class=FileResponse)
async def get_site_logo() -> FileResponse:
    """自定义 Logo 图片。无需登录：登录页与落地页都要显示它。"""
    meta = await site.get_logo_meta()
    if not meta:
        raise HTTPException(status_code=404, detail="未设置自定义 Logo")
    return FileResponse(
        meta["path"],
        media_type=meta["mime"],
        headers={
            # 该接口与面板同源。若直接打开 SVG，里面的脚本会以面板身份执行，
            # 所以用严格 CSP + sandbox 把它压成纯图片；放在 <img> 里不受影响。
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; sandbox"
            ),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "public, max-age=3600",
        },
    )


@router.post("/config/site/logo")
async def upload_site_logo(
    request: Request,
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """上传 / 替换站点 Logo（PNG / JPG / WebP / GIF / SVG / ICO，≤ 512 KB）。"""
    # 多读 1 字节用于判断超限，避免把超大文件整个读进内存
    data = await file.read(site.MAX_LOGO_BYTES + 1)
    try:
        meta = await site.save_logo(data, file.content_type or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await security.audit(
        request,
        user,
        "config.site_logo",
        target="site-logo",
        detail={
            "filename": meta["filename"],
            "size": meta["size"],
            "mime": meta["mime"],
        },
    )
    return await site.get_site_info()


@router.delete("/config/site/logo")
async def delete_site_logo(
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """移除自定义 Logo，界面回落到内置图标。"""
    await site.delete_logo()
    await security.audit(request, user, "config.site_logo.delete", target="site-logo")
    return await site.get_site_info()


# ------------------------------------------------------------ 登录页背景图
# 与 Logo 同一套约定：读取公开（登录 / 注册 / 找回密码页都要用），
# 上传与删除要求 settings.manage。


@router.get("/config/site/login-bg", response_class=FileResponse)
async def get_site_login_bg() -> FileResponse:
    """登录页背景图。无需登录：未登录状态下就要展示。"""
    meta = await site.get_login_bg_meta()
    if not meta:
        raise HTTPException(status_code=404, detail="未设置自定义登录页背景")
    return FileResponse(
        meta["path"],
        media_type=meta["mime"],
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "public, max-age=3600",
        },
    )


@router.post("/config/site/login-bg")
async def upload_site_login_bg(
    request: Request,
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """上传 / 替换登录页背景图（PNG / JPG / WebP / GIF，≤ 4 MB）。"""
    data = await file.read(site.MAX_LOGIN_BG_BYTES + 1)
    try:
        meta = await site.save_login_bg(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await security.audit(
        request,
        user,
        "config.site_login_bg",
        target="site-login-bg",
        detail={
            "filename": meta["filename"],
            "size": meta["size"],
            "mime": meta["mime"],
        },
    )
    return await site.get_site_info()


@router.delete("/config/site/login-bg")
async def delete_site_login_bg(
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """移除自定义背景图，登录页回落到内置插画。"""
    await site.delete_login_bg()
    await security.audit(
        request, user, "config.site_login_bg.delete", target="site-login-bg"
    )
    return await site.get_site_info()


# --------------------------------------------------------------- 邮件通知
# SMTP 配置是**纯管理信息**：主机与端口说清了出网方向，发件账号与管理员收件人
# 直接就是钓鱼的目标清单，``verify_ssl`` 还说明了这条路能不能被中间人利用。
# 此前它只要登录就能读（同组的 connection / diagnostics 却要 settings.manage），
# 是审计项点名的泄露点之一。现在整组 ``/api/config`` 统一 ``settings.manage``：
# 非管理员一律 403，连「有没有配过邮件」都不告诉它。
@router.get("/config/mail")
async def get_mail_config(
    request: Request,
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """邮件配置。SMTP 密码只回 ``password_set``，明文永不出库。

    读权限与写同一档（``settings.manage``，由路由级守卫施加）：这句话的重点是
    「不回明文」而不是「谁都能读」—— 主机 / 端口 / 发件人 / 收件人已经足够
    支撑一轮钓鱼或一次 SMTP 侧的攻击面探测。
    """
    await security.audit_read(request, user, "config.mail.read")
    return mailer.public_mail(await mailer.load_mail())


@router.put("/config/mail")
async def update_mail_config(
    payload: MailConfigIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
    # SMTP 口令 = 「以面板名义发信」的凭据（可用于钓鱼）：改它要二次确认
    _step_up: Dict[str, Any] = Depends(security.require_step_up()),
) -> Dict[str, Any]:
    try:
        saved = await mailer.save_mail(payload.model_dump(exclude_none=True))
    except mailer.MailConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await security.audit(
        request, user, "config.mail", target="mail_settings",
        detail={
            "enabled": saved["enabled"],
            "host": saved["host"],
            "port": saved["port"],
            "tls": saved["tls"],
            "verify_ssl": saved["verify_ssl"],
            "password_changed": bool(payload.password),
        },
    )
    return mailer.public_mail(saved)


# ------------------------------------------------------------- 面板全局地址
# 邮件里拼「重置密码 / 审批结果」链接时用的对外域名。不配则回落到请求 Host
# —— 挂在反向代理后面时那个 Host 常是 localhost，收件人根本打不开。
#
# 读也收在 ``settings.manage``：这条读数是「面板在外网叫什么」，配上连接配置
# 就能拼出完整的访问入口；而除了设置页，没有别的地方需要它（登录页的对外地址
# 由请求 Host 自己决定）。


@router.get("/config/panel-url")
async def get_panel_url(
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, str]:
    """当前配置的面板全局地址；空串 = 未配置（回落到请求 Host）。"""
    return {"url": await panel_url.get_base_url()}


@router.put("/config/panel-url")
async def put_panel_url(
    payload: PanelUrlIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, str]:
    """保存面板全局地址。留空 = 清除配置，回落到请求 Host。"""
    try:
        saved = await panel_url.set_base_url(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await security.audit(
        request, user, "config.panel_url", target="panel-url",
        detail={"url": saved or "(未配置，回落请求 Host)"},
    )
    return {"url": saved}


@router.get("/config/security-posture")
async def get_security_posture(
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """公网暴露加固的当前状态（只读）。

    这些开关都来自环境变量：安全边界不适合做成「点一下就能关掉」的界面开关，
    而且白名单一旦在界面里填错，保存的那一刻就会把人锁在门外。这里只把
    「现在到底生效了什么」摊开给管理员看，改配置仍然走 `.env` + 重启。

    ``admins_without_totp`` 让管理员一眼看到还有谁没绑 2FA —— 公网面板上
    这等同于一个只靠口令守着的 root 入口。
    """
    admins: List[Dict[str, Any]] = []
    for row in await store.list_users():
        if str(row.get("role") or "") == "admin" or "*" in security.effective_permissions(row):
            if row.get("enabled", True) and str(
                row.get("status") or store.STATUS_ACTIVE
            ) == store.STATUS_ACTIVE:
                admins.append(row)

    return {
        "allow_configured": accesscontrol.configured(),
        "allow_cidrs": accesscontrol.allowed_networks(),
        "allow_invalid": accesscontrol.has_invalid_entries(),
        "forwarded_allow_ips": settings.forwarded_allow_ips,
        "trusts_any_proxy": (settings.forwarded_allow_ips or "").strip() == "*",
        "force_https": settings.force_https,
        "totp_required_roles": list(settings.totp_required_role_list),
        "admins_without_totp": [
            str(row.get("username") or "")
            for row in admins
            if not row.get("totp_enabled")
        ],
        "login_ip_alert": settings.login_ip_alert,
        "panel_url": await panel_url.get_base_url(),
    }


@router.get("/config/vm-quota")
async def get_vm_quota(
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """虚拟机下发总配额（**管理端回显**，全局容量上限）。

    ``quota`` 为 ``null`` 表示不限制。读权限与写同一档（``settings.manage``，
    路由级守卫）：这是**全平台**护栏，连同「已用多少台 / 还剩多少」一起，
    拼起来就是集群规模。普通用户看自己那份用 ``GET /api/quota/me``（按人分配
    的额度与名下用量，口径不同，别混用）。
    """
    return await quota.usage(user, "vm")


@router.get("/config/lxc-quota")
async def get_lxc_quota(
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """容器下发总配额（管理端回显）。与虚拟机额度**互相独立**（互不占用）。

    单独给一条路径而不是复用 ``?kind=``：设置页两份读数各拉自己那份，
    显式路径比查询参数少一层「传错了就静默拿错额度」的风险。
    """
    return await quota.usage(user, "lxc")


@router.put("/config/vm-quota")
async def put_vm_quota(
    payload: VmQuotaIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """保存总配额。填 0 = 普通用户完全不能下发；留空 = 不限制。

    校验规则只在 :func:`app.quota.set_quota` 里写一份（负数拒绝、``None`` 删除
    配置），这里只负责把 ``ValueError`` 翻成 400 —— 别在路由里另抄一段阈值判断，
    否则两处迟早不一致。
    """
    try:
        saved = await quota.set_quota("vm", payload.quota)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await security.audit(
        request,
        user,
        "config.vm_quota",
        target="vm-quota",
        detail={"quota": "不限制" if saved is None else saved},
    )
    return await quota.usage(user, "vm")


@router.put("/config/lxc-quota")
async def put_lxc_quota(
    payload: VmQuotaIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """保存容器下发额度。填 0 = 普通用户完全不能建容器；留空 = 不限制。

    校验与虚拟机那份同一处（:func:`app.quota.set_quota`），只是记的键不同。
    """
    try:
        saved = await quota.set_quota("lxc", payload.quota)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await security.audit(
        request,
        user,
        "config.lxc_quota",
        target="lxc-quota",
        detail={"quota": "不限制" if saved is None else saved},
    )
    return await quota.usage(user, "lxc")


@router.get("/quota/me")
async def my_quota(
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """我自己的资源额度与名下用量。

    只要求登录：这是**自己的**读数，创建向导与列表页都要用它 —— 普通用户同样
    应该知道「我还能建几台 / 还能用几核」。别人的读数在管理端
    ``GET /api/users/{username}/quota``。

    与上面两条全局额度（``/config/vm-quota`` 等）的区别：那些是**集群护栏**
    （全集群台数、所有人共享），这里是**按人分配**的资源额度。
    """
    return await quota.resource_report(user)


@router.get("/quota/overview")
async def quota_overview(
    user: Dict[str, Any] = Depends(security.require_permission("users.view")),
) -> Dict[str, Any]:
    """**所有用户**的额度与用量一览（管理端）。

    额度这个功能唯一需要管理员持续做的决策是「要不要给谁放宽」，而此前没有任何
    一个地方支撑它 —— 只能逐个打开用户编辑器看。这个接口把读数取一次、按归属
    分桶算全，代价是 O(用户数 + 机器数) 而不是「每人一次 PVE 请求」。

    口径与用户侧那份（``/quota/me``）完全共用。``error`` 非空表示集群读数不全，
    用量会偏小 —— 界面必须把它显示出来，不能当成「大家都还没用」。
    """
    return await quota.overview()


@router.post("/config/mail/test")
async def test_mail_config(
    payload: MailTestIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """发一封测试邮件。收件人留空时发给「管理员收件人」。"""
    # HTML 走 mailer.wrap_html：移动端的排版（宽度自适应、长文本可折行）
    # 只在那一处维护，测试邮件不另起一套。
    sent_at = time.strftime('%Y-%m-%d %H:%M:%S')
    subject = "[测试] 面板邮件通知配置成功"
    body = (
        "如果你收到这封邮件，说明面板的 SMTP 配置可用。\n\n"
        f"发件时间：{sent_at}\n"
        "这封邮件由管理员的「测试发送」按钮触发。"
    )
    html = mailer.wrap_html(
        "邮件通知配置成功",
        [
            "如果你收到这封邮件，说明面板的 SMTP 配置可用。",
            f"发件时间：{sent_at}",
        ],
        "由管理员的「测试发送」按钮触发。",
    )

    target = (payload.to or "").strip()
    if payload.dry_run:
        # 演练：把「能不能发、会发给谁」算清楚，但不碰 SMTP。空 body、脚本
        # 误触发、批量重放都不会再产生真实投递（审计项 V-14）。
        cfg = await mailer.load_mail()
        recipients = (
            mailer.recipients_of(target)
            if target
            else await mailer.admin_recipients()
        )
        if not cfg.get("enabled"):
            ok, detail = False, "邮件通知未启用"
        elif not cfg.get("host"):
            ok, detail = False, "未配置 SMTP 服务器"
        elif not (cfg.get("sender") or cfg.get("username")):
            ok, detail = False, "需要填写发件人地址（或 SMTP 账号）"
        elif not recipients:
            ok, detail = False, "没有可用的管理员收件地址"
        else:
            ok, detail = True, "演练模式：将发往 " + ", ".join(recipients) + "，未实际投递"
    elif target:
        ok, detail = await mailer.send_mail(target, subject, body, html=html)
    else:
        ok, detail = await mailer.send_to_admins(subject, body, html=html)

    await security.audit(
        request, user, "config.mail.test", target="mail_settings",
        result="success" if ok else "failed",
        detail=("演练：" if payload.dry_run else "") + detail,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=detail)
    return {"ok": True, "detail": detail, "dry_run": payload.dry_run}
