"""FastAPI application entrypoint."""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import BASE_DIR, settings
from .pve import (
    ProxmoxError,
    error_code_headers,
    get_client,
    reset_request_connection,
    reset_request_scope,
    set_request_connection,
    set_request_scope,
)
from .routers import (
    ai as ai_router,
    alerts,
    audit,
    auth,
    backups,
    baseline,
    certs,
    # 用户可编辑的证书工作流（模板 / 导入 / 可视化编排）
    certworkflows,
    # 工作流运行实例：发起 / 审批 / 流转（与定义分开存）
    certworkflow_runs,
    cluster,
    config,
    console,
    # 数据导出：/api/export/{audit,tasks,vms,alerts} 流式吐 CSV
    export as export_router,
    feishu,
    firewall,
    frp,
    health,
    hostaudit,
    ip_pools,
    isolation,
    dashboard,
    lxc,
    # 监控历史查询（/api/metrics/*）；采样与落库在 app.metrics
    metrics as metrics_router,
    network,
    node_notes,
    node_scope,
    # PBS 概览与只读连接配置（/api/pbs/*）
    pbs as pbs_router,
    # 站内通知（顶部铃铛）与跨资源全局搜索（Ctrl+K）。
    # 通知这里必须取别名：下面还有一句 `from . import notifications`（模块），
    # 同名会把路由模块覆盖掉。
    notifications as notifications_router,
    search,
    # 与 app.portguard（巡检实现）同名，取别名避免混淆
    portguard as ports_router,
    # 每用户的界面偏好（仪表盘布局等）
    # 面板级设置（目前只有维护模式）：/api/panel/*
    panel as panel_router,
    prefs as prefs_router,
    # 统一处置（预览 → 二次确认 → 执行）；动作实现在 app.remediation
    remediation as remediation_router,
    roles,
    # 面板运行时读数（/api/runtime/*）：所有登录用户都要用的只读视图。
    # 与 /api/config/*（settings.manage）按读者分家，见 app/routeguard.py。
    runtime as runtime_router,
    # 后台作业状态与配置（/api/scheduler/*）；调度实现在 app.scheduler
    scheduler as scheduler_router,
    ssh,
    storages,
    tasks,
    templates,
    # 个人 API Token 管理（/api/tokens/*）。实现与鉴权在 app.apitokens
    tokens as tokens_router,
    # 面板自身的更新（检查新版本 / 一键更新）：与 app.update 模块同名，取别名
    update as update_router,
    users,
    vm_meta,
    # 虚拟机导入 / 导出：OVF/OVA 导入与 VMDK/QCOW2/RAW/OVA 导出
    vmtransfer as vmtransfer_router,
    # 虚拟机重装系统：用 Cloud-Init 模板重建系统盘
    reinstall as reinstall_router,
    vms,
)
from . import aichat
from . import aiaudit
from . import ai_scope
from . import aivm
# 远程终端：进程退出时要主动断开挂着的 SSH 交互会话（见 lifespan）
from . import aiterm
from . import alert_rootcause
from . import alerting
from . import accesscontrol
from . import apitokens
from . import certmonitor as cert_monitor
from . import certs as cert_manager
from . import certworkflow_run
from . import certworkflow_store
from . import connregister
# 自助注册的一次性邮箱验证码（发码 / 校验 / 限频，见 app/emailcodes）
from . import emailcodes
from . import frp
from . import invites
from . import ippool
from . import metrics as metrics_history
from . import notifications
from . import ownership
from . import panel_url
from . import password_reset
from . import prefs
from . import pvecache
from . import regcodes
from . import scheduler
from . import security
from . import site
from . import sshguard
from . import sshremote
from . import throttle
from . import trusteddevices
from . import update
# 账号有效期：到期前邮件提醒、到期关机并冻结（见 app/userexpiry.py）
from . import userexpiry
# 面板版本的唯一来源（package.json 与 CHANGELOG 跟着它走）
from . import __version__
# 与 routers.hostaudit 同名，取个别名避免混淆
from . import hostaudit as host_audit
from . import i18n
from .routers import frp as frp_router
from . import backupguard
# 面板发起过的隔离动作（虚拟机列表的「隔离」标签与应急响应清单都读它）
from . import quarantine_store
from . import buildinfo
from . import verifyguard
from . import pbsguard
from . import portguard
from . import quotaalert
from . import quorumguard
# 资源额度：按所有者分配的核数 / 内存 / 磁盘 / 快照额度（见 app/quota.py 下半部分）
from . import quota
from . import database
from . import reqmetrics
from . import guest_created
# 巡检快照：把「端口进程」「安全基线」的结果落库，读接口只读快照
from . import inspection
# 客户机 IP 落库缓存（虚拟机列表的 IP 列不再在请求内等 Guest Agent）
from . import guestip
# 巡检类阻塞调用的专用线程池，避免占满事件循环的默认 executor
from . import taskpool
from .store import (
    get_connections,
    init_db,
    purge_audit_old,
    purge_expired_tokens,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("pvecenter")

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_db()
    # 会话有效期可能被管理员改过（存 settings KV），启动时读进进程缓存：
    # 签发令牌是同步路径，不能在那儿查库（见 security.refresh_session_policy）
    await security.refresh_session_policy()
    # AI 排查助手：谁在什么时候让 AI 在哪台机器上跑了什么命令，必须可追溯
    await aiaudit.init_table()
    # AI 候选目标的跨 worker 探测缓存（多 worker 时避免把每台机器重复探 N 遍）
    await ai_scope.init_table()
    # AI 会话：把「一次性排查」升级成可追问的对话（对话线程 + 消息）
    await aichat.init_table()
    # AI 虚拟机内取证：授权租约 + 动作流水（谁、对哪台、改了什么、能不能回滚）
    await aivm.init_table()
    await alerting.init_table()
    await cert_manager.init_table()
    # 证书工作流定义（模板 / 导入 / 画布编辑）：与证书站点同库，单独一张表
    await certworkflow_store.init_table()
    # 工作流运行实例：审批与步骤快照单独存，不随定义编辑而变化
    await certworkflow_run.init_table()
    await ownership.init_table()
    # 归属代际号接进 pvecache：pvecache 是进程内缓存，改派只能清掉当前进程那份，
    # 多 worker 部署下别的进程还留着旧账。接上之后，代际号一变所有进程一起作废。
    pvecache.set_epoch_provider(ownership.generation)
    # 用户级资源额度表（roles.quota 列由上面的 init_db 负责补）
    await quota.init_table()
    # 受管主机表：本次升级给它加了「来源虚拟机」三列（node / vmid / conn_id）。
    # 迁移必须在这里跑完，不能等第一次打开「SSH 安全」页才做 —— 删除虚拟机的
    # 清理逻辑（sshremote.purge_vm_hosts）可能先于那次访问被触发，届时找不到列。
    await sshremote.init_table()
    # 受保护备份登记表（防删 / 防篡改核对）
    await backupguard.init_table()
    # 节点备注的存量键（只有节点名）固定到升级时的当前连接，避免两套集群的
    # 同名节点继续共用同一条备注（新键形如 <连接id>|<节点>）
    await node_notes.migrate_legacy_keys()
    # 面板发起的隔离动作登记表：虚拟机列表靠它打「隔离」标签，应急响应页靠它
    # 列出「现在有哪些机器被隔离」（见 app.quarantine_store）
    await quarantine_store.init_table()
    # 面板发起的建机 / 克隆 / 恢复记录（创建时间字段的准确来源，见 app.guest_created）
    await guest_created.init_table()
    # 归属表的一次性迁移：把升级前记在 KIND_VM 下的容器归属改到 KIND_CT
    # （虚拟机 / 容器权限与归属拆分）。要用到上面的 guest_created_record 判断
    # 哪些机器是容器，所以必须排在建表之后；纯读面板库，PVE 连不上也能跑。
    await ownership.migrate_guest_kinds()
    await password_reset.init_table()
    # 监控历史表：「监控历史采样」作业到点后开始往里写点
    await metrics_history.init_table()
    # 巡检快照表：端口 / 基线的结果落库，读接口不再现场 SSH（见 app.inspection）
    await inspection.init_table()
    # 客户机 IP 落库缓存：虚拟机列表读它，不再在请求内逐台问 Guest Agent
    await guestip.init_table()
    # IP 地址池的分配登记表：DHCP 自动分配在配置落盘前先把地址占住
    await ippool.init_table()
    # 站内通知表：告警发生时由 alerting.record 往里写未读消息
    await notifications.init_table()
    # 启动时清一遍「链接指向的机器已不属于收件人」的存量消息：升级前的告警规则
    # 没有按机器归属过滤，那些行会一直躺在普通用户的铃铛里（BUG-013）。失败只记日志，
    # 不影响启动；以后新产生的消息由 notifications.push_alert 在写入口拦住。
    try:
        await notifications.purge_foreign_guest_items()
    except Exception:  # noqa: BLE001 - 清理失败不能让面板起不来
        logger.warning("清理归属不符的站内消息失败，忽略", exc_info=True)
    # 个人 API Token 表（脚本 / 外部系统用的长期凭据，见 app.apitokens）
    await apitokens.init_table()
    # 信任设备表（登录两步验证的「7 天内免二次校验」，见 app.trusteddevices）
    await trusteddevices.init_table()
    # 每用户的界面偏好（仪表盘布局等，见 app.prefs）
    await prefs.init_table()
    # PVE 一键接入注册码表（脚本用它建连接，绕开 step-up，见 app.connregister）
    await connregister.init_table()
    # 自助注册的邀请凭证 / 注册码表（见 app.regcodes）
    await regcodes.init_table()
    # 邀请链接表（链接自带角色 / 审批策略 / 次数，见 app.invites）
    await invites.init_table()
    # 注册邮箱验证码表（自助注册的一步性口令，见 app.emailcodes）
    await emailcodes.init_table()
    # 清掉过期的令牌黑名单条目与早已过期的会话记录（纯残留清理，失败不影响启动）
    try:
        await purge_expired_tokens()
        await trusteddevices.purge_expired()
        await throttle.cleanup()
    except Exception:  # noqa: BLE001
        logger.exception("清理过期令牌/限流数据失败，忽略")
    # 角色定义可选择：把内置角色补齐进库并把全部角色读入内存缓存
    await security.load_roles()
    # 老版本只用 frp_config 一个 key 存整份配置；新版拆成 server + rules，
    # 启动时一次性迁移，避免第一个 GET 接口看到空数据。
    await frp.migrate_legacy()
    # 把实际解释器版本写进启动日志：面板依赖按 3.11+ 固定，排查「装到了另一个
    # Python 上」「服务跑在旧环境里」时，第一眼就能看到。
    logger.info("运行环境：Python %s", platform.python_version())
    logger.info("数据库就绪：%s", database.describe())
    # 前端产物与后端是不是同一个版本。不一致时界面可能少功能或行为不对，而它此前
    # 在界面上毫无迹象（代码已是新版、服务照跑、页面照开）。见 app.buildinfo。
    buildinfo.log_at_startup()
    logger.info("Panel listening on %s:%s", settings.host, settings.port)
    # 公网暴露是这套面板最需要被看见的前提：白名单没配就把这件事写进日志，
    # 配了就打印生效网段，避免「以为配了、其实拼错被忽略」。
    if accesscontrol.has_invalid_entries():
        logger.warning("PANEL_ALLOW_CIDRS 含非法条目，见上一条解析告警")
    if accesscontrol.configured():
        logger.info(accesscontrol.startup_hint())
    else:
        logger.warning(accesscontrol.startup_hint())
    # FORWARDED_ALLOW_IPS=* 下，任何客户端都能自称任意来源 IP：白名单会被
    # 绕过，限流与审计里的 IP 也不可信。这不是「可选优化」，是信任边界。
    if (settings.forwarded_allow_ips or "").strip() == "*":
        logger.warning(
            "FORWARDED_ALLOW_IPS=*：面板信任任意对端传来的 X-Forwarded-For，"
            "来源 IP 可被伪造（白名单 / 登录锁定 / 审计 IP 都会失真）。"
            "请改成反向代理的实际地址。"
        )
    # 关掉证书校验的连接 = 面板到 PVE 的通道可被内网中间人劫持（审计项 FD-07）。
    # 自签证书请改用「证书钉扎」（连接配置里的 ca_cert），而不是整条链路不校验。
    try:
        insecure = [
            str(c.get("name") or c.get("id") or c.get("host") or "?")
            for c in get_connections()
            if not bool(c.get("verify_ssl", settings.pve_verify_ssl))
        ]
    except Exception:  # noqa: BLE001 - 读不到配置不该拦住启动
        insecure = []
    if insecure:
        logger.warning(
            "以下 PVE 连接未校验证书（通道可被中间人劫持，API Token 可能被截获）：%s"
            "。自签证书请填「证书 PEM」做钉扎，或把 PVE 证书换成受信任 CA 签发。",
            "、".join(insecure),
        )

    # 文档端点：默认不挂载；打开时也要让人知道「它不是匿名的」
    if settings.api_docs_enabled:
        logger.warning(
            "API_DOCS_ENABLED=true：/api/docs、/api/redoc、/api/openapi.json 已挂载，"
            "访问需管理员登录。仅建议在内网/调试环境打开。"
        )
    else:
        logger.info("接口文档端点已关闭（/api/docs、/api/redoc、/api/openapi.json 均 404）")

    client = get_client()
    if client.conn.configured:
        try:
            version = await client.version()
            logger.info(
                "Connected to Proxmox VE %s at %s",
                version.get("version"),
                client.conn.base_url,
            )
        except ProxmoxError as exc:
            logger.warning("Proxmox not reachable at startup: %s", exc.message)
        except Exception:
            # An unreachable host, a TLS handshake failure, an intercepting
            # proxy — none of these may stop the panel from booting. The
            # operator has to be able to reach the UI to fix the settings,
            # so degradation must never be fatal.
            logger.exception(
                "Unexpected error while probing Proxmox at startup; "
                "continuing with the panel online"
            )
    else:
        logger.warning(
            "Proxmox connection not configured. "
            "Log in as %s and open Settings to configure it.",
            settings.admin_username,
        )

    # 后台作业统一交给调度器（见 app.scheduler）。顺序不能反：先把落库的
    # 间隔 / 启停配置恢复出来，run_forever 才会按恢复后的间隔算唤醒时刻。
    await scheduler.load_state()
    scheduler.start()
    # IP 地址池的占用快照预热：第一次打开创建向导 / 批量部署时不必现场扫全集群。
    # 放在后台跑，PVE 暂时不可达也不影响启动。
    ippool.start_warmup()
    yield

    # 停止路径**逐步打点**：这几步里任何一步卡住，systemd 都会在 TimeoutStopSec
    # （15 秒）到点后直接 SIGKILL —— 进程被硬杀，数据库连接池与远端 SSH 都来不及
    # 收口，而且日志里只会留下一句「stop-sigterm timed out」，看不出卡在哪。
    # 打点之后，最后一条日志指向的就是卡住的那一步。
    logger.info("正在停止：IP 池后台刷新")
    await ippool.stop()
    logger.info("正在停止：后台调度器")
    await scheduler.stop()
    # 打开的远程终端各挂着一条 SSH 连接：进程退出前主动收口，
    # 让远端把登录会话干净地关掉，而不是留一堆半死的连接。
    logger.info("正在停止：远程终端")
    await aiterm.close_all()
    logger.info("正在停止：HTTP 客户端")
    await client.aclose()
    logger.info("正在停止：池化 SSH 连接")
    sshremote.close_pool()
    logger.info("正在停止：巡检线程池")
    taskpool.shutdown()
    logger.info("正在停止：数据库连接池")
    await database.close_pool()
    logger.info("Shutdown complete")


app = FastAPI(
    title="pvecenter API",
    description=(
        "Web management panel for Proxmox VE 8.x / 9.x. "
        "Provides VM lifecycle, template building, networking, "
        "monitoring, backup and console access."
    ),
    version=__version__,
    lifespan=lifespan,
    # 三个文档端点一律不交给 FastAPI 自动挂载：它的内置路由没有鉴权，匿名
    # 就能拿到完整 OpenAPI（= 全量攻击地图）。需要时由下面的 _mount_api_docs
    # 注册**加过管理员鉴权**的等价路由（见 API_DOCS_ENABLED）。
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# ------------------------------------------------------------------- 限流
# 匿名/敏感入口单独一条更紧的线：登录、注册、找回密码、刷新令牌、2FA 都在这儿。
RATE_LIMIT_AUTH_PATHS = frozenset(
    {
        "/api/auth/login",
        "/api/auth/login/2fa",
        "/api/auth/register",
        "/api/auth/forgot-password",
        "/api/auth/reset-password",
        "/api/auth/reset-password/check",
        "/api/auth/refresh",
    }
)


class RateLimitMiddleware:
    """按来源 IP 的请求限流，计数落 MySQL（重启不清零、多进程共享）。

    * 只拦 ``/api/`` 请求 —— SPA 的 js/css 不走这道闸，否则加载一次页面就能把
      配额打满；
    * 健康检查例外，运维脚本不该被自己限速；
    * 来源 IP 只信「可信反代的 X-Forwarded-For」（见 security.client_ip_from_scope），
      否则客户端自己塞一个假 IP 就绕过了。

    写成原生 ASGI 的理由与 PveConnectionScopeMiddleware 相同：不进
    BaseHTTPMiddleware 的任务切换，拒绝路径也尽量短。
    """

    EXEMPT_PATHS = frozenset({"/api/health"})

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not settings.rate_limit_enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        if not path.startswith("/api/") or path in self.EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        ip = security.client_ip_from_scope(scope) or "unknown"
        if path in RATE_LIMIT_AUTH_PATHS:
            bucket = f"api-auth:ip:{ip}"
            limit = max(1, int(settings.rate_limit_auth_per_minute))
        else:
            bucket = f"api:ip:{ip}"
            limit = max(1, int(settings.rate_limit_per_minute))

        allowed, retry_after = await throttle.allow(bucket, limit, 60)
        if not allowed:
            await self._reject(send, retry_after)
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Any, retry_after: int) -> None:
        body = json.dumps({"detail": "请求过于频繁，请稍后再试"}, ensure_ascii=False)
        payload = body.encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(payload)).encode()),
                    (b"retry-after", str(max(1, retry_after)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


# 先注册限流、再注册 CORS：后注册的在外层，于是 CORS 包住限流 —— 429 响应也
# 会带上 CORS 头，跨域调试时前端能看到「请求过于频繁」而不是一个空的网络错误。
app.add_middleware(RateLimitMiddleware)


def _scope_cookies(scope: Any) -> Dict[str, str]:
    """从 ASGI scope 里解析 Cookie（只取名字和值，不关心属性）。"""
    cookies: Dict[str, str] = {}
    for name, value in scope.get("headers") or []:
        if name == b"cookie":
            for part in value.decode("latin-1").split(";"):
                key, sep, val = part.strip().partition("=")
                if sep:
                    cookies[key] = val
    return cookies


class CsrfMiddleware:
    """Cookie 认证下的 CSRF 防护（双提交令牌）。

    凭据都进 Cookie 之后，跨站请求会自动带上它们 —— 这正是 CSRF 的成因。
    ``SameSite=Lax`` 挡掉了绝大多数跨站表单/请求，但对「同站不同源」（另一台被
    拿下的子域）无能为力，所以补第二道：

    * 登录时下发一枚**非 HttpOnly** 的 ``panel_csrf``（前端 JS 要能读到）；
    * 所有改状态的方法（POST/PUT/PATCH/DELETE）必须带 ``X-CSRF-Token`` 且与
      Cookie 里的值一致 —— 别的站点读不到这枚 Cookie，也就伪造不出这个头。

    只对「靠 Cookie 认证的请求」生效：

    * 显式带 ``Authorization`` 的脚本客户端没有环境凭据（ambient credential），
      本来就不存在 CSRF，放行；
    * 没有 access cookie 的请求（登录、刷新、公开接口）也不需要校验。
    """

    SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if (scope.get("method") or "GET").upper() in self.SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        if any(name == b"authorization" for name, _ in headers):
            # 显式令牌：这条请求的身份来自请求头本身，不是浏览器自动带上的
            await self.app(scope, receive, send)
            return

        cookies = _scope_cookies(scope)
        if security.ACCESS_COOKIE not in cookies:
            await self.app(scope, receive, send)
            return

        sent = ""
        for name, value in scope.get("headers") or []:
            if name == security.CSRF_HEADER.encode():
                sent = value.decode("latin-1").strip()
                break

        expected = cookies.get(security.CSRF_COOKIE, "")
        if expected and sent and secrets.compare_digest(expected, sent):
            await self.app(scope, receive, send)
            return

        await self._reject(send)

    @staticmethod
    async def _reject(send: Any) -> None:
        body = json.dumps(
            {"detail": "请求缺少 CSRF 校验信息，请刷新页面后重试"},
            ensure_ascii=False,
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# 注册顺序：CSRF 在限流之后、CORS 之前 —— CORS 会包住两者，403/429 都带 CORS 头。
app.add_middleware(CsrfMiddleware)


class StrictCorsMiddleware:
    """CORS 的一层收口：没有回写 ``Allow-Origin`` 时也不许出现 ``Allow-Credentials``。

    Starlette 的 ``CORSMiddleware`` 会把 ``allow_credentials=True`` 放进
    ``simple_headers``，只要请求带 ``Origin`` 就无条件写回 —— 即使这个 Origin
    不在白名单里、响应里根本没有 ``Access-Control-Allow-Origin``。浏览器当下
    会拒绝，但这等于在每一个响应上埋了一句「本接口接受凭据跨域」：哪天有人为了
    排查问题把 Origin 放宽成通配，它立刻变成真实的 CORS 凭据泄露通道
    （审计项 V-06）。这里在响应出口做一次一致性校验：两条头必须同时存在。
    """

    def __init__(self, app: Any, **kwargs: Any) -> None:
        self.app = CORSMiddleware(app, **kwargs)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def send_without_orphan_credentials(message: Dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                current = list(message.get("headers") or [])
                names = {name.lower() for name, _ in current}
                if (
                    b"access-control-allow-credentials" in names
                    and b"access-control-allow-origin" not in names
                ):
                    current = [
                        (name, value)
                        for name, value in current
                        if name.lower() != b"access-control-allow-credentials"
                    ]
                    message = {**message, "headers": current}
            await send(message)

        await self.app(scope, receive, send_without_orphan_credentials)


# 未配置跨域来源时干脆不挂 CORS：同源部署（含反代）根本不需要它，而中间件
# 本身仍会改写到响应头。只有显式配置了 CORS_ORIGINS（开发 / 独立前端）才启用。
if settings.cors_origin_list:
    app.add_middleware(
        StrictCorsMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


class PveConnectionScopeMiddleware:
    """把 ``X-PVE-Connection`` / ``X-PVE-Scope`` 请求头绑定到本次请求。

    面板可以配置多台 Proxmox，绝大多数请求作用于「当前连接」；但创建虚拟机的
    向导允许临时指定另一台主机。该请求头让 :func:`get_client` 在本次请求内返回
    对应主机的客户端，从而所有读写接口都无需额外参数即可支持多主机。

    两个头强度不同（见 :mod:`app.pve` 的说明）：
      * ``X-PVE-Connection`` 是**硬指定** —— 调用点知道机器在哪条连接上；
      * ``X-PVE-Scope`` 是**软作用域** —— 顶栏「集群作用域」，会让读数据的接口
        只读那一套，但按节点 / VMID 能定位到别处时允许被覆盖。

    刻意写成原生 ASGI 中间件而不是 ``BaseHTTPMiddleware``：选择结果存放在
    contextvar 中，原生 ASGI 能让下游应用运行在同一个任务里，取值才可靠。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        conn_id = ""
        scope_id = ""
        accept_language = ""
        for key, value in scope.get("headers") or []:
            if key == b"x-pve-connection":
                conn_id = value.decode("latin-1").strip()
            elif key == b"x-pve-scope":
                scope_id = value.decode("latin-1").strip()
            elif key == b"accept-language":
                # 界面语言（见 i18n.py）：权限目录、内置角色名、FAQ 与错误消息按它返回
                accept_language = value.decode("latin-1").strip()

        i18n.set_language(accept_language)
        token = set_request_connection(conn_id)
        scope_token = set_request_scope(scope_id)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_request_scope(scope_token)
            reset_request_connection(token)


app.add_middleware(PveConnectionScopeMiddleware)


class HttpsEnforcementMiddleware:
    """``FORCE_HTTPS=1`` 时强制面板只走 HTTPS（TLS 在前面的 Nginx/Caddy 终结）。

    * 明文请求 → ``308`` 到 ``https://<Host>``，跳转目标会去掉本面板监听端口 ——
      后端自己不提供 TLS，必须跳到反代的 443；
    * HTTPS 请求 → 回响应加 HSTS（``max-age=31536000; includeSubDomains``），
      浏览器一年内对本站及其子域都只走 https。

    是否 https 只信 uvicorn 改写过的 ``scope["scheme"]``：``X-Forwarded-Proto``
    仅对 ``FORWARDED_ALLOW_IPS`` 内的对端生效，公网请求自称「我是 https」无效。
    唯一放行的是**本机回环且不带转发头**的请求（健康检查、运维脚本）—— 否则
    ``curl 127.0.0.1:8080`` 只会拿到 308。WebSocket 同样拦：VNC 控制台的
    连接串里带 JWT，明文跑等于把登录态摊在链路上。
    """

    LOOPBACK = {"127.0.0.1", "::1", "localhost"}
    # includeSubDomains：面板常挂在 panel.example.com，而同一张证书 / 同一个
    # 站点下的子域（api.example.com、console.example.com）不该还能走明文 HTTP。
    # 只写 max-age 的 HSTS 对子域不生效，等于给降级攻击留了一个旁路（BUG-007）。
    # preload：把域名提交到浏览器 HSTS 预加载列表后可覆盖「首次访问」的空白期；
    # 它只是声明，是否真的进列表取决于域名是否提交（审计项 V-05）。
    HSTS = b"max-age=31536000; includeSubDomains; preload"

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type not in ("http", "websocket") or not settings.force_https:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        peer = (scope.get("client") or ("", 0))[0]
        loopback_direct = peer in self.LOOPBACK and b"x-forwarded-proto" not in headers

        if self._is_https(scope):
            if scope_type == "http":
                send = self._with_hsts(send)
            await self.app(scope, receive, send)
            return

        if loopback_direct:
            await self.app(scope, receive, send)
            return

        location = self._redirect_target(scope, headers)
        if scope_type == "websocket":
            # 握手阶段无法 308（浏览器不会跟随），按策略违规关闭，让前端
            # 明确失败而不是把 JWT 明文发出去。
            logger.warning(
                "HTTPS 策略拦截 WebSocket：path=%s scheme=%s peer=%s"
                " x-forwarded-proto=%s",
                scope.get("path") or "-",
                scope.get("scheme"),
                peer,
                (headers.get(b"x-forwarded-proto") or b"-").decode("latin-1"),
            )
            await send({"type": "websocket.close", "code": 1008, "reason": "HTTPS required"})
            return

        await send(
            {
                "type": "http.response.start",
                "status": 308,
                "headers": [
                    (b"location", location.encode("latin-1")),
                    (b"content-length", b"0"),
                ],
            }
        )

    @staticmethod
    def _is_https(scope: Any) -> bool:
        """这次请求是否确实走加密链路。

        ``https`` 和 ``wss`` **两个 scheme 都要认**：加密的 WebSocket 在 ASGI 里的
        scheme 是 ``wss``，不是 ``https``。只认 https 的后果是**所有**浏览器
        WebSocket（实时指标、任务流、VNC 控制台）挂在反代后面时被判成明文、握手
        一律以 1008 关掉，uvicorn 记成 403 —— 而它们其实都走在 TLS 上。

        这里刻意不再自己去解析 ``X-Forwarded-Proto``：uvicorn 的 ProxyHeaders
        中间件已经按 ``FORWARDED_ALLOW_IPS`` 验过信任链并把结论写进 scheme 了。
        重复判断反而会出错 —— 那个中间件同时把 ``scope["client"]`` 改成了真实
        客户端 IP，于是「对端是不是可信反代」在这里永远为假。
        """
        return scope.get("scheme") in ("https", "wss")

    @staticmethod
    def _redirect_target(scope: Any, headers: Dict[bytes, bytes]) -> str:
        host = (headers.get(b"host") or b"").decode("latin-1")
        for port in (f":{settings.port}", ":80"):
            if port and host.endswith(port):
                host = host[: -len(port)]
                break
        if not host:
            server = scope.get("server") or ("localhost", settings.port)
            host = str(server[0])
        location = f"https://{host}{scope.get('path') or '/'}"
        query = scope.get("query_string") or b""
        if query:
            location += "?" + query.decode("latin-1")
        return location

    @staticmethod
    def _with_hsts(send: Any) -> Any:
        async def send_with_hsts(message: Dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                current = list(message.get("headers") or [])
                if not any(
                    name.lower() == b"strict-transport-security" for name, _ in current
                ):
                    current.append((b"strict-transport-security", HttpsEnforcementMiddleware.HSTS))
                    message = {**message, "headers": current}
            await send(message)

        return send_with_hsts


# 放在最后 = 最外层：先决定走不走 HTTPS，再进 CORS / 连接作用域 / 路由。
app.add_middleware(HttpsEnforcementMiddleware)


class SecurityHeadersMiddleware:
    """给所有 HTTP 响应补齐浏览器安全头（CSP / nosniff / Referrer / Permissions / 反索引）。

    为什么写在应用里而不是只交给 nginx：反代配置属于部署方，漏了就整站裸奔；
    面板自己下发，裸 8080、Docker、反代三种部署口径一致。反代那边只需**不要
    重复添加同名头**（尤其是 HSTS —— 两份会让响应里出现两条同名头）。

    CSP 分两档，别为了文档把全站放宽：

    * 普通页面 / 接口：``script-src 'self'``（Vite 产物没有内联脚本，实测可跑）；
    * ``/api/docs``、``/api/redoc``：Swagger UI / ReDoc 从 cdn.jsdelivr.net 取
      资源、且页面里有内联启动脚本，只对这两条路径放宽（它们默认还不存在）。

    ``connect-src`` 只留 ``'self'``：控制台、实时指标、任务流、VNC 全部连接
    **当前页面的同源** WebSocket（``new WebSocket(...)`` 用的是相对/同源地址），
    不需要任何跨主机权限。原先写 ``ws: wss:`` 等于允许页面脚本连接任意主机，
    与「同源」的实际需求不符，也会给被注入脚本提供外带通道（BUG-008）。
    """

    CSP = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )
    DOCS_PATHS = frozenset({"/api/docs", "/api/redoc"})
    DOCS_CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data: https://fastapi.tiangolo.com; "
        "font-src 'self' data: https://cdn.jsdelivr.net; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    )
    STATIC_HEADERS = (
        # 禁掉 MIME 嗅探：上传的 json / svg / 控制台下载文件不会被当成脚本执行
        (b"x-content-type-options", b"nosniff"),
        # 跨站跳转只带 origin：完整 URL 里可能有查询参数（例如控制台 token）
        (b"referrer-policy", b"strict-origin-when-cross-origin"),
        (
            b"permissions-policy",
            b"camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        ),
        # frame-ancestors 已能挡点击劫持，这条覆盖不支持 CSP 的老浏览器
        (b"x-frame-options", b"DENY"),
        # 跨源隔离三件套（审计项 V-05）：面板所有资源都同源，不需要任何跨源
        # 窗口引用 / 资源嵌入。COOP 切断 window.opener，COEP 要求嵌入资源
        # 显式声明 CORP，CORP 则禁止别的站点把我们的响应当图片 / 脚本加载。
        # 三者的组合同时让面板进入 crossOriginIsolated 状态。
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-embedder-policy", b"require-corp"),
        (b"cross-origin-resource-policy", b"same-origin"),
        # 整站不许索引（管理面板被搜索引擎收录只会扩大攻击面）。robots.txt
        # 只管守规矩的爬虫，这条响应头把「已经抓到了也别展示」一起覆盖；
        # 有些引擎 / 预览服务并不读 robots.txt，只认这个头。
        (b"x-robots-tag", b"noindex, nofollow"),
    )

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        csp = self.DOCS_CSP if path in self.DOCS_PATHS else self.CSP
        extra = [(b"content-security-policy", csp.encode("latin-1"))]
        extra.extend(self.STATIC_HEADERS)

        async def send_with_headers(message: Dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                # 顺手摘掉服务器商标头（审计项 FD-14）。uvicorn 自己的
                # "server: uvicorn" 是协议层加的，已在 run.py 用 server_header=False
                # 关掉；这里兜住应用/中间件可能加上的同名头，避免漏一处在响应里
                # 出现两条 server。
                current = [
                    (name, value)
                    for name, value in (message.get("headers") or [])
                    if name.lower() not in (b"server", b"x-powered-by")
                ]
                present = {name.lower() for name, _ in current}
                current.extend(
                    (name, value) for name, value in extra if name not in present
                )
                message = {**message, "headers": current}
            await send(message)

        await self.app(scope, receive, send_with_headers)


# 加在 Https 之后 = 最外层：308 跳转、403 拒绝、静态资源也都带上安全头。
app.add_middleware(SecurityHeadersMiddleware)


class PanelAccessControlMiddleware:
    """按来源 IP / CIDR 拒绝面板访问（``PANEL_ALLOW_CIDRS``）。

    这是应对「面板直接暴露公网」最有效的一道闸：来源不在白名单内时，
    连登录页、``/api/openapi.json``、VNC WebSocket 都看不到 / 连不上。

    细节：
      * 未配置白名单时完全放行（存量部署行为不变）；
      * 本机回环始终放行（反代 / 健康检查 / 容器 sidecar）；
      * ``/api/health`` 始终放行，探活不因白名单失败；
      * **WebSocket 也要拦**：只挡 HTTP 的话，VNC 控制台仍能握手；
      * 来源取 :func:`security.client_ip_from_scope`，只信可信反代的 XFF，
        客户端自己伪造的头不会换来放行。

    写成原生 ASGI：拒绝路径极短，且 WebSocket 握手也能直接 close。
    """

    # security.txt 是给外部研究员的公开渠道：把它一起挡住，等于「只允许熟人
    # 报漏洞」。内容只有联系方式，不含任何面板状态。
    EXEMPT_PATHS = frozenset({"/api/health", "/.well-known/security.txt"})
    # 被拒日志按 IP 做 60 秒去重：爆破时的日志洪水本身也是一种资源消耗
    _last_log: Dict[str, float] = {}

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if not accesscontrol.configured():
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        if scope_type == "http" and path in self.EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        ip = security.client_ip_from_scope(scope)
        if accesscontrol.is_allowed(ip):
            await self.app(scope, receive, send)
            return

        self._log_blocked(ip, path, scope_type)
        if scope_type == "websocket":
            await send(
                {
                    "type": "websocket.close",
                    "code": 1008,
                    "reason": "Source address not allowed",
                }
            )
            return

        body = json.dumps(
            {"detail": "Access denied: source address is not allowed"},
            ensure_ascii=False,
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @classmethod
    def _log_blocked(cls, ip: str, path: str, scope_type: str) -> None:
        now = time.monotonic()
        last = cls._last_log.get(ip)
        if last is not None and now - last < 60:
            return
        if len(cls._last_log) > 2048:
            cls._last_log.clear()
        cls._last_log[ip] = now
        logger.warning(
            "已拒绝非白名单来源访问面板：ip=%s type=%s path=%s"
            "（如属误拦，请检查 PANEL_ALLOW_CIDRS 与 FORWARDED_ALLOW_IPS）",
            ip or "-",
            scope_type,
            path or "-",
        )


# 再包一层 = 最外层：白名单先于 HTTPS 跳转 / CORS / 限流生效。
app.add_middleware(PanelAccessControlMiddleware)


# 计数归零 + 响应头回写（X-PVE-Calls / X-DB-Queries）。放在最外层，被前面任何
# 一道闸拦下的请求也带得上这两个头（都是 0），不会让人误以为「埋点没生效」。
app.add_middleware(reqmetrics.RequestMetricsMiddleware)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 统一为一句通用提示，字段名 / 阈值 / 校验器类型只写服务端日志。

    以前这里回的是 ``detail: [{loc, msg, type}]``：虽然去掉了 ``input``（避免
    明文口令回显，BUG-009），但 ``loc=query.limit`` + ``less_than_equal`` 仍然
    把框架（Pydantic）、字段名与阈值一起交了出去，等于把接口契约免费送给扫描器
    （审计项 CWE-209 / CWE-497）。日志里保留定位信息供运维排查，响应里只给一个
    不透明的 ``trace_id``。
    """
    trace_id = secrets.token_hex(8)
    errors = [
        {
            "loc": ".".join(str(part) for part in (error.get("loc") or [])),
            "msg": str(error.get("msg") or ""),
            "type": str(error.get("type") or ""),
        }
        for error in exc.errors()
    ]
    logger.warning(
        "请求参数校验失败 trace_id=%s %s %s errors=%s",
        trace_id,
        request.method,
        request.url.path,
        errors,
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": i18n.pick(
                "请求参数不合法，请检查后重试", "Invalid request parameters"
            ),
            "code": "validation_error",
            "trace_id": trace_id,
        },
        headers={"X-Trace-Id": trace_id},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """保持 FastAPI 默认的 JSON 形状，但 405 不再回 ``Allow``。

    RFC 9110 要求 405 带 ``Allow``，代价却是把「这条路径存在、只是方法不对」
    精确地告诉未授权访问者 —— 拿一份常见方法表逐个打一遍就能把接口存在性摸清
    （审计项 V-09）。面板的接口集合本身就是攻击地图，宁可牺牲这条自动发现
    能力：405 仍然如实表示「方法不对」，只是不再列出自以为可用的方法。
    """
    headers = dict(exc.headers or {})
    if exc.status_code == 405:
        headers.pop("Allow", None)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=headers or None,
    )


@app.exception_handler(ProxmoxError)
async def proxmox_error_handler(request: Request, exc: ProxmoxError) -> JSONResponse:
    """Translate Proxmox failures into a consistent JSON shape.

    上游状态码、内部函数名（``endpoint``）只写服务端日志：``pve_status: 404`` +
    ``endpoint: "get_client"`` 等于把「面板怎么调 PVE、错误从哪一层抛出来」交给
    调用方，是现成的代码路径侦察（审计项 CWE-209）。响应里改回不透明
    ``trace_id``，排查时按日志里的同一个 id 对上即可。
    """
    status = exc.status_code if 400 <= exc.status_code < 600 else 500
    trace_id = secrets.token_hex(8)
    logger.warning(
        "PVE 调用失败 trace_id=%s %s %s status=%s pve_status=%s endpoint=%s code=%s detail=%s",
        trace_id,
        request.method,
        request.url.path,
        status,
        exc.pve_status,
        exc.endpoint or "-",
        exc.code or "-",
        exc.message,
    )
    # 5xx 是上游内部失败：原文常带 VM 名 / 存储路径 / 锁信息，对外一律换成
    # 通用文案，细节靠 trace_id 回查日志（审计项 L-3）。4xx 是请求本身的问题
    # （未开机、权限不足、参数冲突），保留语义，用户才知道下一步该做什么。
    if status >= 500:
        detail: Any = i18n.pick(
            "上游 Proxmox 操作失败，请稍后重试（如需排查请提供 trace_id）",
            "The upstream Proxmox operation failed. Please try again later "
            "(quote the trace_id when asking for help).",
        )
    else:
        detail = exc.message
    return JSONResponse(
        status_code=status,
        content={
            "detail": detail,
            # 机器可读原因仍保留：前端靠它区分 428 的两种来源
            "code": exc.code,
            "trace_id": trace_id,
        },
        headers={"X-Trace-Id": trace_id, **(error_code_headers(exc) or {})},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """未捕获异常：日志留全栈，响应只留通用文案 + trace id。

    此前这里把 ``str(exc)`` 直接拼进 ``detail``（例如 ``KeyError: 'lxc'``、
    文件路径、SQL 片段）—— 那是最直接的一类内部信息泄露（CWE-209）。
    """
    trace_id = secrets.token_hex(8)
    logger.exception(
        "未捕获异常 trace_id=%s %s %s", trace_id, request.method, request.url.path
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": i18n.pick("服务器内部错误", "Internal server error"),
            "code": "internal_error",
            "trace_id": trace_id,
        },
        headers={"X-Trace-Id": trace_id},
    )


# ------------------------------------------------------------------ routers
app.include_router(auth.router)
# 面板配置（/api/config/*）：整组 settings.manage，路由级守卫在 routeguard.py
app.include_router(config.router)
# 运行时读数（/api/runtime/*）：登录即可读的脱敏视图（UI 开关 / 创建默认值 / 规格）
app.include_router(runtime_router.router)
app.include_router(dashboard.router)
# 监控历史：/api/metrics/history 与 /api/metrics/coverage（实时那条走 /ws/metrics）
app.include_router(metrics_router.router)
# 个人 API Token：/api/tokens（列表 / 创建 / 吊销）
app.include_router(tokens_router.router)
# 后台作业：/api/scheduler（状态 / 改间隔 / 立即执行 / 恢复默认）
app.include_router(scheduler_router.router)
# 面板自身的更新：/api/update/status|check|settings|apply（仅管理员）
app.include_router(update_router.router)
# 每用户界面偏好：/api/prefs（仪表盘布局等）
app.include_router(prefs_router.router)
# 面板级设置：/api/panel/maintenance（GET 公开，PUT 仅管理员）
app.include_router(panel_router.router)
app.include_router(cluster.router)
# 虚拟机导入 / 导出（VMware 互操作）：OVF/OVA 导入与 VMDK/QCOW2/RAW/OVA 导出。
# **必须注册在 vms.router 之前**：vms 里有 ``GET /vms/{node}/{vmid}`` 这条两段动态
# 路由，它会把 ``/vms/import/sources``、``/vms/exports/{id}`` 这种「静态前缀 + 两段」
# 的路径先匹配走（node=import、vmid=sources），接口于是返回 422 —— 逐个路由都对、
# 但永远进不来，属于最难看出来的那一类问题。静态段优先于动态段。
app.include_router(vmtransfer_router.router)
# 重装系统（路径形如 /vms/{node}/reinstall-templates 与 /vms/{node}/{vmid}/reinstall，
# 段数与 vms 的动态路由不同形，不依赖注册顺序）
app.include_router(reinstall_router.router)
app.include_router(vms.router)
# 容器与虚拟机是两套 PVE 端点，走自己的路由；归属与权限仍与 vms 共用
app.include_router(lxc.router)
app.include_router(templates.router)
app.include_router(storages.router)
# PBS 概览与只读凭据（/api/pbs/*）：与存储页互补 —— 存储页回答「PVE 眼里的
# PBS 存储」，这里回答「PBS 自己眼里的仓库与任务」
app.include_router(pbs_router.router)
app.include_router(network.router)
app.include_router(node_notes.router)
app.include_router(node_scope.router)
# 虚拟机/容器的手动 IP（面板侧补充信息，只用于展示，不回写 PVE）
app.include_router(vm_meta.router)
app.include_router(backups.router)
app.include_router(tasks.router)
app.include_router(console.router)
app.include_router(frp_router.router)
app.include_router(ip_pools.router)
app.include_router(firewall.router)
app.include_router(ssh.router)
app.include_router(baseline.router)
# AI 排查助手：只读解读巡检结果（配置仅管理员可改）
app.include_router(ai_router.router)
app.include_router(ports_router.router)
# 应急隔离挂在 /api/vms/{node}/{vmid}/quarantine，与 vms 共用归属校验
app.include_router(isolation.router)
# 隔离清单（面板登记过的隔离动作）：/api/isolation
app.include_router(isolation.list_router)
# 统一处置：/api/remediation/actions|preview|apply（预览 + 二次确认 + 审计）
app.include_router(remediation_router.router)
app.include_router(hostaudit.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(audit.router)
app.include_router(alerts.router)
app.include_router(certs.router)
app.include_router(certworkflows.router)
app.include_router(certworkflow_runs.router)
app.include_router(feishu.router)
app.include_router(health.router)
# 数据导出（审计日志 / 任务 / 资产清单 / 告警历史）
app.include_router(export_router.router)
# 站内通知（铃铛未读数 / 已读）与跨资源全局搜索
app.include_router(notifications_router.router)
app.include_router(search.router)


# ---------------------------------------------------------------- 接口文档
# 默认**不提供**任何文档端点：匿名可读的 /api/openapi.json 会把 300+ 个接口、
# 每个字段的约束、连「隐藏端点」一起交出去，等于把侦察成本降到零，而面板
# 握着 PVE 的全权管理能力（审计项 FD-03）。
#
# 需要时打开 API_DOCS_ENABLED，这里注册的替代路由仍要求「已登录 + 具备
# settings.manage（管理员）」——Swagger UI 与 openapi.json 同源，浏览器会
# 自动带上登录 cookie，管理员照样能在页面上试调；匿名访问则拿不到 spec。
#
# 注册位置必须在 /api/{rest:path}（404 兜底）与 SPA 兜底**之前**，否则会被
# 它们吃掉（FastAPI 按注册顺序匹配，先注册的赢）。
def _mount_api_docs(target: Optional[FastAPI] = None) -> None:
    """挂载需要管理员鉴权的 Swagger UI / ReDoc / OpenAPI JSON。

    ``target`` 只为测试留口：用例拿一个空 app 验证「没有凭据 = 401」，
    不去碰真实路由表。
    """
    api = target or app
    admin_only = Depends(security.require_permission("settings.manage"))

    @api.get("/api/openapi.json", include_in_schema=False, dependencies=[admin_only])
    async def openapi_spec() -> JSONResponse:
        return JSONResponse(
            api.openapi(),
            # spec 里有全部接口形状，中间层/浏览器都不该留副本
            headers={"Cache-Control": "no-store"},
        )

    @api.get("/api/docs", include_in_schema=False, dependencies=[admin_only])
    async def swagger_docs() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url="/api/openapi.json",
            title=f"{api.title} — Swagger UI",
            swagger_ui_parameters={"persistAuthorization": True},
        )

    @api.get("/api/redoc", include_in_schema=False, dependencies=[admin_only])
    async def redoc_docs() -> HTMLResponse:
        return get_redoc_html(
            openapi_url="/api/openapi.json",
            title=f"{api.title} — ReDoc",
        )


if settings.api_docs_enabled:
    _mount_api_docs()


@app.get("/api/version", tags=["meta"])
async def version(
    _user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    """面板版本（仅管理员）。

    原来登录即可读：面板版本 + 前端构建时间 + Python 运行时版本，等于把「该对
    哪个版本的公开 PoC / 依赖 CVE」直接标好，运行时版本还额外暴露解释器栈
    （审计项 CWE-497）。前端界面本来就不读这个接口（版本从 ``/api/health``
    取），所以收成与接口文档同一档：``settings.manage``。

    ``python`` 字段同时删除 —— 运维需要时看启动日志里的「运行环境」即可。
    站名/Logo 这类品牌信息仍在公开的 ``/api/config/site`` 里，登录页照常渲染。
    """
    info = await site.get_site_info()
    return {
        "name": info["name"],
        "version": __version__,
        "api": "v1",
        # 前端构建产物的版本与一致性（见 app.buildinfo）：界面据此提示「产物陈旧」
        "frontend": buildinfo.status(),
    }


# 未知的 /api/* 路径一律回 404（**所有方法**），但**先过鉴权**。
#
# 为什么非加不可：下面的 SPA 兜底是 ``GET /{full_path:path}``，它匹配**任何**路径。
# POST 到一个不存在的接口时，Starlette 于是发现「路径有匹配、方法不对」，回
# **405 Method Not Allowed** —— 前端把 detail 原样显示，用户看到「Method Not
# Allowed」，会去翻请求方法写错了没有，而真相通常是后端还跑着旧版本、这个接口
# 根本没发布。405 在这里说了假话，404 才是实话。
#
# 鉴权依赖（BUG-005）：未登录时「存在但需要登录的接口」与「根本不存在的接口」
# 必须同样回 401。否则匿名扫描器只要看 401 还是 404，就能零成本把全部 API 路径
# 枚举出来。把 ``get_current_user`` 挂在兜底路由上，未知路径也先要求身份；
# 登录之后才回 404，路由表因此只对已认证用户可见。
#
# 必须注册在**所有**真实路由之后：FastAPI 按注册顺序匹配，先注册的赢。
def _api_404_detail() -> str:
    """通用 404 文案：不回显路径 / 版本状态 / 运维动作（BUG-003）。"""
    return i18n.pick("请求的接口不存在", "Endpoint not found")


def _api_allowed_methods(path: str) -> List[str]:
    """这个路径在路由表里存在时，允许哪些方法（只用于判定 405，不回 ``Allow``）。

    兜底路由把 ``/api/*`` 的所有方法都接了，因此 Starlette 没有机会自己回
    405 —— 必须在兜底里替它算一次。只扫 ``/api`` 路由，并用预编译的正则匹配；
    未知路径才会走到这里，正常请求没有额外开销（BUG-010）。

    跳过兜底自身，避免「所有方法都允许」把 405 又吃回去。
    """
    methods: set = set()
    for route in app.router.routes:
        route_path = str(getattr(route, "path", "") or "")
        if not route_path.startswith("/api"):
            continue
        if route_path in ("/api", "/api/{rest:path}"):
            continue
        regex = getattr(route, "path_regex", None)
        if regex is None or not regex.match(path):
            continue
        for method in getattr(route, "methods", ()) or ():
            methods.add(str(method).upper())
    methods.discard("OPTIONS")
    if "GET" in methods:
        methods.add("HEAD")
    return sorted(methods)


@app.api_route(
    "/api",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
@app.api_route(
    "/api/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def api_not_found(
    request: Request,
    rest: str = "",
    _user: Optional[Dict[str, Any]] = Depends(security.get_current_user_optional),
) -> JSONResponse:
    # 未登录时**所有**未命中的 /api 路径统一 401（BUG-005）：未知路径、存在
    # 但方法不对的路径，对匿名访问者必须长得一模一样。否则 405 与 401 的差别
    # 本身就是一份「这条路径存在」的信号（审计项 V-09）。
    if _user is None:
        raise HTTPException(
            status_code=401,
            detail=i18n.pick("未登录或登录状态已失效", "Not authenticated"),
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 已登录用户才看得到 405（BUG-010）：语义是「方法不对」，而不是伪装成
    # 404 让人去翻请求方法。但**不回 Allow** —— `Allow: GET, POST` 等于把
    # 「这条路径存在、还支持哪些方法」精确地交出去。
    allowed = _api_allowed_methods(request.url.path)
    if allowed and request.method not in allowed:
        return JSONResponse(
            status_code=405,
            content={
                "detail": i18n.pick("请求方法不被允许", "Method Not Allowed"),
                "code": "method_not_allowed",
            },
        )

    # 排查所需的信息只进日志：响应体里不再回显路径、版本状态或「重启」这类
    # 运维动作（BUG-003）。扫描器拿到的统一是一个没有语义的 not_found。
    logger.info(
        "未匹配的 API 路径：%s %s（已认证用户=%s）",
        request.method,
        request.url.path,
        _user.get("username") or "-",
    )
    return JSONResponse(
        status_code=404,
        content={"detail": _api_404_detail(), "code": "not_found"},
    )


def _security_contacts(base: str, request: Request) -> List[str]:
    """把配置里的联系方式规范成 RFC 9116 的 ``Contact:`` 值。

    支持「邮箱 / mailto: / https 链接」，多个用逗号分隔；纯邮箱自动补
    ``mailto:``。没配时回落到 ``security@<面板域名>`` —— 总比让研究员只能
    去公开 issue 区提交要好（审计项 V-16）。
    """
    raw = str(getattr(settings, "security_contact", "") or "")
    items = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
    if not items:
        host = urlsplit(base).hostname or (request.url.hostname or "")
        if host:
            items = [f"mailto:security@{host}"]
    contacts: List[str] = []
    for item in items:
        if "://" in item or item.lower().startswith("mailto:"):
            contacts.append(item)
        elif "@" in item:
            contacts.append(f"mailto:{item}")
        else:
            contacts.append(item)
    return contacts


@app.get("/.well-known/security.txt", include_in_schema=False)
async def security_txt(request: Request) -> PlainTextResponse:
    """漏洞报告渠道（RFC 9116）。

    此前没有任何声明，外部研究员发现漏洞后只能猜联系方式，或者直接开公开
    issue —— 后者等于把未修复的细节先公开（审计项 V-16）。``Expires`` 按
    一年滚动生成，避免文件悄悄过期后被判定为无效；``Canonical`` 指向面板
    自己的对外地址，便于自动化工具去重。
    """
    base = (await panel_url.resolve(request)).rstrip("/")
    contacts = _security_contacts(base, request)
    expires = datetime.now(timezone.utc) + timedelta(days=365)
    lines = [f"Contact: {item}" for item in contacts]
    lines.append(f"Expires: {expires.strftime('%Y-%m-%dT%H:%M:%S.000Z')}")
    lines.append("Preferred-Languages: zh-CN, en")
    if base:
        lines.append(f"Canonical: {base}/.well-known/security.txt")
    return PlainTextResponse(
        "\n".join(lines) + "\n",
        media_type="text/plain; charset=utf-8",
        # 渠道很少变，但也不该被代理长期据旧；一小时足够。
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt() -> PlainTextResponse:
    """禁止一切搜索引擎索引。

    管理面板被收录的代价不是「多几条搜索结果」：登录页、接口路径、带查询
    参数的 URL 都会出现在别人的索引里，等于替攻击者做了资产测绘。

    用路由而不是塞一个 ``public/robots.txt``：后端没构建前端产物（``dist/``
    不存在）时也要答得出这份文件，Docker / 裸 8080 / 反代三种部署口径一致。
    原先这条路径会落到 SPA 兜底的「像静态文件就 404」，扫描器看到的是
    JSON 404（审计项：缺少 robots.txt）。

    与 :class:`SecurityHeadersMiddleware` 里的 ``X-Robots-Tag`` 双保险：
    robots.txt 只约束守规矩的爬虫，响应头则把「抓到了也别索引」一起说了。
    """
    return PlainTextResponse(
        "User-agent: *\nDisallow: /\n",
        media_type="text/plain; charset=utf-8",
        # 内容固定，一天缓存足够；改规则属于部署变更，不需要秒级生效。
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ------------------------------------------------------------ front-end UI
# 生产部署：后端直接托管前端构建产物 dist/。前端与 API 同源，既不需要
# 额外的静态服务器 / Nginx，也避免了反向代理 WebSocket 的配置——VNC
# 控制台因此可以开箱即用。
DIST_DIR = BASE_DIR.parent / "dist"


# ------------------------------------------------------- 入口页品牌注入
# 品牌名存在 settings 表里，index.html 却是构建产物：不做处理的话，浏览器先
# 渲染写死的「pvecenter」，等 React 起来、/api/config/site 回来才换成真实站点
# 名 —— 标签页肉眼可见地闪一下，图标同理（先空着，再弹出自定义 Logo）。
#
# 这里在返回入口页时把标题 / 描述 / 图标替换掉，首帧就是对的。只做字符串替换、
# 不改构建流程：dist/index.html 仍是普通静态文件，被 CDN 或独立静态服务器直接
# 托管时退化成写死的兜底值，不会把占位符泄漏给用户。

_HTML_TITLE_RE = re.compile(r"<title>.*?</title>", re.S)
_HTML_DESC_RE = re.compile(r'(<meta\s+name="description"\s+content=")[^"]*(")', re.S)
_HTML_ICON_RE = re.compile(r'<link\s+rel="icon"[^>]*>', re.S)
_HTML_LANG_RE = re.compile(r'<html\s+lang="[^"]*"', re.S)

# 站点信息也塞进 <meta>，前端首帧就用它做 React Query 的 placeholderData：
# 否则侧边栏 / 登录页的品牌名还是会先显示兜底的「pvecenter」，接口回来才换。
_HTML_META_RE = {
    key: re.compile(rf'(<meta\s+name="{key}"\s+content=")[^"]*(")', re.S)
    for key in ("site-name", "site-subtitle", "site-logo")
}

# 站点信息缓存 15 秒：入口页一被请求就要用到它，每个请求都查库（一次读三行 KV）
# 不划算；改名后最多 15 秒生效，比进程内缓存一辈子安全。
_BRAND_TTL = 15.0
_brand_cache: Dict[str, Any] = {
    "at": -1e9,
    "rev": -1,
    "name": "pvecenter",
    "subtitle": "",
    "logo": None,
}

# index.html 正文按 mtime 缓存：升级后文件变了要重读，没变就别反复读盘
_index_cache: Dict[str, Any] = {"mtime": -1.0, "text": ""}


def _index_template() -> str:
    path = DIST_DIR / "index.html"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    if _index_cache["mtime"] != mtime:
        _index_cache["text"] = path.read_text(encoding="utf-8")
        _index_cache["mtime"] = mtime
    return str(_index_cache["text"])


_BRAND_FAIL_TTL = 3.0


async def _site_branding() -> Tuple[str, str, Optional[str]]:
    """站点名 / 副标题 / logo 地址；读库失败时沿用上一次成功的值，绝不让首页 500。"""
    now = time.monotonic()
    fresh = now - float(_brand_cache["at"]) < _BRAND_TTL
    # 改名 / 换 logo 会让 revision 立刻变化，不必等缓存过期
    if fresh and _brand_cache["rev"] == site.brand_revision:
        return (
            str(_brand_cache.get("name") or "pvecenter"),
            str(_brand_cache.get("subtitle") or ""),
            _brand_cache.get("logo"),
        )

    try:
        info = await site.get_site_info()
    except Exception:  # noqa: BLE001 —— 品牌信息读不到不影响页面本身
        logger.warning("读取站点信息失败，入口页沿用上一次的品牌", exc_info=True)
        # 只压 3 秒：数据库抖一下不能让标题错一整轮缓存周期
        _brand_cache["at"] = now - (_BRAND_TTL - _BRAND_FAIL_TTL)
        return (
            str(_brand_cache.get("name") or "pvecenter"),
            str(_brand_cache.get("subtitle") or ""),
            _brand_cache.get("logo"),
        )

    name = str(info.get("name") or "pvecenter")
    subtitle = str(info.get("subtitle") or "")
    logo = info.get("logo_url") or None
    _brand_cache.update(
        at=now, rev=site.brand_revision, name=name, subtitle=subtitle, logo=logo
    )
    return name, subtitle, logo


def _request_lang(request: Request) -> str:
    """从 Accept-Language 猜标题语言，和前端 detectLang 的默认行为对齐。"""
    header = request.headers.get("accept-language") or ""
    first = header.split(",")[0].split(";")[0].strip().lower()
    return "en" if first.startswith("en") else "zh"


def _render_index(
    lang: str, name: str, logo: Optional[str], subtitle: str = ""
) -> str:
    """把入口页 HTML 里的标题 / 描述 / 语言 / 图标 / 站点信息换成当前站点值。"""
    text = _index_template()
    if not text:
        return ""

    title = f"{name} — Proxmox VE {'management panel' if lang == 'en' else '管理面板'}"
    desc = f"{name} - Proxmox VE {'cluster management panel' if lang == 'en' else '集群管理面板'}"

    text = _HTML_TITLE_RE.sub(lambda _m: f"<title>{escape(title)}</title>", text, count=1)
    text = _HTML_DESC_RE.sub(
        lambda m: f"{m.group(1)}{escape(desc)}{m.group(2)}", text, count=1
    )
    text = _HTML_LANG_RE.sub(
        '<html lang="en"' if lang == "en" else '<html lang="zh-CN"', text, count=1
    )
    if logo:
        # 整个标签重写：自定义 Logo 可能是 PNG / ICO / SVG，保留构建时的
        # type="image/svg+xml" 会声明错类型，交给浏览器按内容嗅探更稳。
        tag = f'<link rel="icon" href="{escape(logo)}">'
        if _HTML_ICON_RE.search(text):
            text = _HTML_ICON_RE.sub(lambda _m: tag, text, count=1)
        else:
            # 旧版本的构建产物里没有 icon 声明，补一个，省得升级后还要等重新构建
            text = re.sub(r"</head>", f"  {tag}\n  </head>", text, count=1)

    # 站点信息供前端做 initialData（转义后放进属性，避免站点名里的引号破坏 HTML）
    metas = {
        "site-name": name,
        "site-subtitle": subtitle,
        "site-logo": logo or "",
    }
    missing = []
    for key, value in metas.items():
        pattern = _HTML_META_RE[key]
        if pattern.search(text):
            text = pattern.sub(
                lambda m, v=escape(value): f"{m.group(1)}{v}{m.group(2)}",
                text,
                count=1,
            )
        else:
            # 升级后 dist/ 还是旧构建产物（没有 site-* 声明）——直接补上，
            # 不必等用户重新 npm run build 才能拿到首帧品牌。
            missing.append(f'  <meta name="{key}" content="{escape(value)}">')
    if missing:
        text = re.sub(
            r"</head>", "\n".join(missing) + "\n  </head>", text, count=1
        )
    return text


def _looks_like_static_file(path: str) -> bool:
    """路径是否在要一个**静态文件**（而不是前端路由）。

    SPA 兜底必须区分这两类，否则 ``/robots.txt``、``/favicon.ico``、
    ``/.well-known/security.txt`` 这类路径会拿到 200 + ``index.html`` ——
    扫描器看到「软 404」，缓存/CDN 也会把首页当成这些文件缓存起来（BUG-006）。

    判据只有两条，足够覆盖真实静态资源，又不会误伤前端路由：

    * 最后一段带扩展名（``.txt`` / ``.ico`` / ``.map`` …）；
    * 第一段以 ``.`` 开头（``/.well-known/*``、``/.env`` 等隐藏路径）。
    """
    if not path or path.endswith("/"):
        return False
    first, _, rest = path.partition("/")
    if first.startswith("."):
        return True
    last = rest.rsplit("/", 1)[-1] if rest else first
    return "." in last


if (DIST_DIR / "index.html").is_file():
    if (DIST_DIR / "assets").is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=DIST_DIR / "assets"),
            name="assets",
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str, request: Request) -> Response:
        """返回 dist/ 中的静态文件；其余路径回退到 index.html（前端路由）。

        但「像静态文件」的路径不能回退：``/robots.txt`` 之类没命中真实文件时
        必须 404，而不是把首页当 robots.txt 回给爬虫。
        """
        if full_path.startswith("api/"):
            # 未知的 API 路径保持 404 语义，不要伪装成前端页面。
            raise HTTPException(status_code=404, detail=_api_404_detail())

        candidate = (DIST_DIR / full_path).resolve()
        if full_path and candidate.is_file() and DIST_DIR in candidate.parents:
            return FileResponse(candidate)
        if _looks_like_static_file(full_path):
            logger.info("静态资源不存在，拒绝回退到首页：/%s", full_path)
            raise HTTPException(status_code=404, detail=_api_404_detail())
        # 入口页**不能**被缓存：它引用的是一串带 hash 的 assets，index.html 一旦
        # 被浏览器或代理存住，用户就一直跑旧版本 —— 前端明明重新构建过，页面上
        # 还是旧行为，看起来就像「修复没生效」。
        name, subtitle, logo = await _site_branding()
        page = _render_index(_request_lang(request), name, logo, subtitle)
        if not page:
            # 模板读不出来（权限 / 编码异常）就退回原文件，不能给个空页
            logger.warning("入口页模板不可读，回退到原始 index.html")
            return FileResponse(
                DIST_DIR / "index.html", headers={"Cache-Control": "no-store"}
            )
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

else:

    @app.get("/", include_in_schema=False)
    async def root() -> Dict[str, str]:
        info = {
            "name": "pvecenter API",
            "health": "/api/health",
            "hint": "未找到 dist/，执行 npm run build 后本服务将直接托管前端界面。",
        }
        # 文档默认关闭，这里也别把地址写死成「有」—— 那是给匿名探测指路
        if settings.api_docs_enabled:
            info["docs"] = "/api/docs"
        return info

# ============================================================== 后台作业注册
# 改造前这里是四个各自独立的 `while True` 循环 + 一堆「每 N 个 tick 跑一次」
# 的倍数常量：间隔改不了，运行状态也看不见（循环体里 `logger.exception` 吞掉
# 异常之后，界面上完全看不出某个巡检是不是早就挂了）。
#
# 现在统一交给 app.scheduler：一份作业表，每项有自己的间隔与运行状态，
# 间隔落库、改完下一个周期生效。下面这些常量只作**默认值** —— 它们的取值与
# 改造前各循环的实际周期一一对应，所以本次升级后的调度行为不变。

# 资源告警：一分钟一次，要在故障扩散之前发现
ALERT_INTERVAL = 60
# 端口/进程巡检：要跑 ss/ps + 遍历 /proc，还要逐台 SSH，比资源告警重得多，
# 5 分钟一次足够（改造前写作「每 5 个 tick」）
PORTS_INTERVAL = 5 * ALERT_INTERVAL
# 受保护备份核对：要走 PVE 逐个存储拉内容，与端口巡检同频足够
BACKUP_CHECK_INTERVAL = PORTS_INTERVAL
# 集群仲裁巡检：逐套 PVE 探测（每套 2~3 个上游请求），而且要覆盖**所有**连接，
# 不能像资源告警那样只读默认那一台。失去仲裁不会自己恢复，5 分钟查一次足够 ——
# 这条规则的意义是「别让它静悄悄过去」，不是秒级发现。
QUORUM_INTERVAL = 5 * ALERT_INTERVAL

# 额度预警：要逐人向 PVE 枚举一遍用量（同一份读数也喂给用户页那两列），比读
# cluster/resources 重一些；5 分钟一次。阈值本身是「剩余 20%」这种缓慢变化的量，
# 再密也只是反复命中同一条冷却，不会更早发现问题。
QUOTA_ALERT_INTERVAL = 5 * ALERT_INTERVAL
# 首次给足延迟：启动时连接可能还没配好，白跳一轮没有代价
QUOTA_ALERT_FIRST_DELAY = 180
# 账号有效期：提醒按天算，冻结却要尽量准时 —— 5 分钟一轮，代价只是读一遍
# users 表；到期后最多晚 5 分钟关机。首次延迟 30 秒，启动迁移跑完就查一遍。
USER_EXPIRY_INTERVAL = 300
USER_EXPIRY_FIRST_DELAY = 30
# 主机登录审计：登录历史变化慢，5 分钟汇入一次足够
HOST_AUDIT_INTERVAL = 300
# 受管主机核对：虚拟机可能在 PVE 上被直接删掉（不走面板的删除接口），
# 每 5 分钟拿一次 PVE 的虚拟机清单比对，清掉这类残留（见 sshremote.reconcile_panel_hosts）
HOST_SYNC_INTERVAL = 300
# 证书续期检查：免费证书只有 90 天，6 小时查一次足够；首次延迟 2 分钟，
# 让启动流程先跑完再去发外部网络请求
CERT_INTERVAL = 6 * 3600
CERT_FIRST_DELAY = 120
# 证书退避重试：主作业 6 小时一轮，覆盖不到 10 分钟 / 30 分钟 / 2 小时这类
# 短退避，所以单独起一个 5 分钟的轻量作业，只挑「退避到点」的站点补跑。
# 首次延迟 3 分钟，错开主作业与启动时的连接初始化。
CERT_RETRY_INTERVAL = 300
CERT_RETRY_FIRST_DELAY = 180
# 监控历史采样周期。它同时决定采样点的时间戳对齐粒度（见 metrics.align_ts），
# 所以那个函数会回头问调度器要当前生效值，两边一起变才不会出现重复点。
METRICS_INTERVAL = max(int(settings.metrics_sample_interval), 1)
# 各类保留清理：存量变化极慢，一小时一次足够
PURGE_INTERVAL = 3600
# API Token 的过期行清理：一天一次
TOKEN_PURGE_INTERVAL = 86_400
# 面板自身的版本检查：一天一次足够（Release 不会一天变两次）。首次延迟 15 分钟 ——
# 启动时先把 PVE 连接、数据库迁移、调度器都跑稳，再谈出网。间隔与首次延迟的默认值
# 放在 app/update.py 里（那里还管着「手动检查的最小间隔」等同类常量）。
UPDATE_CHECK_INTERVAL = update.CHECK_INTERVAL
UPDATE_CHECK_FIRST_DELAY = update.FIRST_DELAY
# 内网穿透看护：一分钟看一次。frpc 断掉意味着所有穿透一起断，等太久没意义；
# 一次检查只是读一个 pid 文件，代价可以忽略（拉起失败另有退避，见 app/frp.py）
FRP_WATCHDOG_INTERVAL = 60
# 首次延迟 20 秒：启动时要先跑配置迁移与数据库初始化，别抢在前面
FRP_WATCHDOG_FIRST_DELAY = 20
# 告警根因聚合：跟着告警巡检的节奏（每分钟一轮判定）走，但这只是「检查」的频率 ——
# 真正推送由告警指纹与冷却压制，同一批告警不会反复推
ROOTCAUSE_INTERVAL = 5 * ALERT_INTERVAL
ROOTCAUSE_FIRST_DELAY = 2 * ALERT_INTERVAL
# 巡检快照预热：只跑**安全基线**（端口的落库由上面的「端口进程巡检」作业顺带
# 完成，避免同一批主机每轮扫两遍 SSH）。间隔与端口巡检一致 —— 它决定「页面上的
# 数据最多旧多少」，而不是响应速度。
INSPECTION_WARM_INTERVAL = max(int(settings.inspection_warm_interval), 60)
# 首次延迟 30 秒：启动时先让数据库迁移、PVE 连接就绪，再去扫第一批
INSPECTION_WARM_FIRST_DELAY = 30
# 客户机 IP 预热：与 guestip 的进程内缓存时长对齐，让列表的 IP 列基本总能命中
GUEST_IP_WARM_INTERVAL = max(int(settings.guest_ip_warm_interval), 60)
GUEST_IP_WARM_FIRST_DELAY = 45
# 资源额度对账：快照读数是「逐台枚举 + 60 秒缓存」，本作业负责把缓存保鲜，并在
# 有人被调低额度后已经超出时落一条日志。间隔决定「读数最多旧多久」，15 分钟够用
# （创建快照时会主动作废缓存，不等它）。
QUOTA_RECONCILE_INTERVAL = 900
QUOTA_RECONCILE_FIRST_DELAY = 120
# 归属对账：把面板那本归属账和 PVE 的实际清单对齐（见 quota.reconcile_owners）。
# 它做的是**删除**，节奏不宜太急 —— 一条连接读失败就整轮跳过，所以偶尔漏一轮
# 没有代价；间隔决定「PVE 侧手工删掉一台之后，残留的归属最多留多久」。
OWNER_RECONCILE_INTERVAL = 1800
# 首次给足延迟：启动时连接可能还没配好 / PVE 还没就绪，读失败也只会白跳一轮
OWNER_RECONCILE_FIRST_DELAY = 300


def _job(
    job_id: str,
    name: str,
    group: str,
    description: str,
    run: Callable[[], Awaitable[Any]],
    interval: int,
    *,
    summarize: Optional[Callable[[Any], str]] = None,
    first_delay: Optional[int] = None,
) -> scheduler.Job:
    return scheduler.Job(
        id=job_id,
        name=name,
        group=group,
        description=description,
        run=run,
        default_interval=interval,
        summarize=summarize,
        first_delay=first_delay,
    )


def _rootcause_summary(result: Any) -> str:
    """告警根因聚合的摘要：跳过原因 / 参与聚合的归属人数 + 推送份数。"""
    if not isinstance(result, dict):
        return ""
    if result.get("skipped"):
        return str(result["skipped"])
    return (
        f"聚合 {int(result.get('owners') or 0)} 人，"
        f"推送 {int(result.get('sent') or 0)} 份"
    )


def _count(label: str) -> Callable[[Any], str]:
    """把「返回一串触发项」的作业收成一句人能读的摘要（空 = 无变化）。"""

    def render(result: Any) -> str:
        try:
            size = len(result or [])
        except TypeError:
            return ""
        return f"{label} {size} 项" if size else "无变化"

    return render


def _warm_summary(result: Any) -> str:
    """巡检快照预热 / 客户机 IP 预热的摘要：落库了多少项。"""
    if not isinstance(result, dict):
        return ""
    count = int(result.get("stored") or 0) or int(result.get("resolved") or 0)
    return f"落库 {count} 项" if count else "无变化"


def _expiry_summary(result: Any) -> str:
    """账号有效期作业的摘要：提醒了几人、冻结了几人（附带失败数）。"""
    if not isinstance(result, dict):
        return ""
    notified = int(result.get("notified") or 0)
    frozen = int(result.get("frozen") or 0)
    skipped = int(result.get("skipped") or 0)
    failed = len(result.get("errors") or [])
    parts: List[str] = []
    if notified:
        parts.append(f"提醒 {notified} 人")
    if frozen:
        parts.append(f"冻结 {frozen} 人")
    if skipped:
        parts.append(f"跳过 {skipped} 人")
    if failed:
        parts.append(f"{failed} 项失败")
    return "、".join(parts) if parts else "无变化"


def _quota_summary(result: Any) -> str:
    """资源额度对账的摘要：刷了多少台、几个人已经超出额度。"""
    if not isinstance(result, dict):
        return ""
    refreshed = int(result.get("refreshed") or 0)
    over = len(result.get("over") or [])
    if not refreshed and not over:
        return "无变化"
    return f"刷新 {refreshed} 台" + (f"，{over} 人超额" if over else "")


def _owners_summary(result: Any) -> str:
    """归属对账的摘要：清了几条、还有几台没人认领。"""
    if not isinstance(result, dict):
        return ""
    refused = str(result.get("refused") or "")
    if refused:
        return f"读数不完整，已跳过（{refused}）"
    removed = int(result.get("removed") or 0)
    orphans = int(result.get("orphans") or 0)
    if not removed and not orphans:
        return "无变化"
    parts = []
    if removed:
        parts.append(f"清理 {removed} 条残留归属")
    if orphans:
        parts.append(f"{orphans} 台无人认领")
    return "，".join(parts)


def _prune_summary(result: Any) -> str:
    """删号归属清理的摘要：清了几条、账号表读没读到。"""
    if not isinstance(result, dict):
        return ""
    refused = str(result.get("refused") or "")
    if refused:
        return f"读取账号表失败，已跳过（{refused[:40]}）"
    removed = int(result.get("removed") or 0)
    return f"清理 {removed} 条指向已删账号的归属" if removed else "无变化"


def _import_summary(result: Any) -> str:
    """主机登录审计返回的是 {imported, skipped, ...}，只挑导入条数报出来。"""
    if not isinstance(result, dict):
        return ""
    imported = int(result.get("imported") or 0)
    return f"导入 {imported} 条" if imported else "无新增"


def _sample_summary(result: Any) -> str:
    try:
        written = int(result or 0)
    except (TypeError, ValueError):
        return ""
    return f"落库 {written} 个点" if written else "本轮无数据"


def _frp_summary(result: Any) -> str:
    """内网穿透看护只回一个动作，翻成一句人话。"""
    if not isinstance(result, dict):
        return ""
    action = str(result.get("action") or "")
    if action == "restarted":
        return "检测到 frpc 已停止，已自动拉起"
    if action == "backoff":
        return f"退避中（已连续失败 {int(result.get('failures') or 0)} 次）"
    if action == "skipped":
        return "未开启自动拉起"
    if action == "running":
        return "frpc 运行中"
    return ""


async def _purge_audit() -> int:
    """审计日志保留清理（配置项在 Settings 里，作业只负责把天数传下去）。

    注意：这里用的是函数级导入，**不是** ``store.xxx``。main 早先为了精简
    ``from .store import ...`` 只导了几个符号，没导 ``store`` 模块，后台作业
    一跑就 ``NameError``（该作业此前每轮都失败，页面上显示成「审计日志保留
    清理 · 失败」）。
    """
    return await purge_audit_old(int(settings.audit_retention_days or 0))


def _purge_summary(result: Any) -> str:
    try:
        removed = int(result or 0)
    except (TypeError, ValueError):
        return ""
    return f"清理 {removed} 行" if removed else "无需清理"


def _host_sync_summary(result: Any) -> str:
    """受管主机核对：清掉了几台、有几台是「被回收后 VMID 复用」认出来的、
    又有几台本轮没判定成（连接未记 / 清单读不到 / 缺 VMID）。

    后面两类的数字必须报出来：它们在外观上与「已清理」一样，实际上机器还留在
    「端口进程」里 —— 只报 ``无变化`` 会让人误以为一切正常。
    """
    if not isinstance(result, dict):
        return ""
    removed = int(result.get("removed") or 0)
    reused = int(result.get("tombstoned") or 0)
    retried = int((result.get("retried") or {}).get("removed") or 0)
    skipped = len(result.get("skipped") or [])
    failed = len(result.get("failed") or [])
    unresolved = len(result.get("unresolved") or [])
    if not (removed or retried or skipped or failed or unresolved):
        return "无变化"
    parts = []
    if removed:
        parts.append(f"清理 {removed} 台已删除主机")
    if reused:
        parts.append(f"其中 {reused} 台因 VMID 被复用而识别")
    if retried:
        parts.append(f"补偿清理 {retried} 台")
    if failed:
        parts.append(f"{failed} 台清理失败待重试")
    if skipped:
        parts.append(f"{skipped} 台本轮无法判定")
    if unresolved:
        parts.append(f"{unresolved} 台缺 VMID 关联无法自动清理")
    return "，".join(parts)


def _cert_summary(result: Any) -> str:
    if not isinstance(result, dict):
        return _count("处理")(result)
    renewed = len(result.get("renewed") or [])
    failed = len(result.get("failed") or [])
    if failed:
        return f"续期 {renewed} 张、失败 {failed} 张"
    return f"续期 {renewed} 张" if renewed else "无需续期"


# 注册顺序 = 界面上的展示顺序，按「发现问题的先后」排：先告警与安全，
# 再监控与证书，最后是清理这类纯维护作业。
scheduler.register(
    _job(
        "alerts",
        "资源告警巡检",
        "安全巡检",
        "按阈值评估节点与虚拟机的 CPU / 内存 / 存储，命中规则即告警",
        alerting.evaluate,
        ALERT_INTERVAL,
        summarize=_count("命中"),
    )
)
# 集群仲裁：资源告警比的是「某个对象的使用率」，而且只读默认连接那一套 ——
# 非默认集群掉仲裁在那边永远不会响，所以单独一个作业逐套 PVE 检查
# （见 app.quorumguard；探测与仪表盘集群态势卡共用 app.fleetstatus）。
scheduler.register(
    _job(
        "quorum",
        "集群仲裁巡检",
        "安全巡检",
        "逐套 PVE 检查集群仲裁，失去仲裁即告警（含非默认读取的连接）",
        quorumguard.evaluate,
        QUORUM_INTERVAL,
        summarize=_count("命中"),
    )
)

# 用户额度预警：资源告警比的是「某台机器的使用率」，额度比的是「某人名下还剩
# 多少」，判据与对象都不同，放进规则表表达不了（见 app.quotaalert 的说明）。
# 通知发给**本人** —— 额度用完的后果落在那个人身上。
scheduler.register(
    _job(
        "quota_alert",
        "用户额度预警",
        "安全巡检",
        "算出谁的额度快用完或已超出，逐人走各自的告警通道",
        quotaalert.evaluate,
        QUOTA_ALERT_INTERVAL,
        summarize=_count("命中"),
        first_delay=QUOTA_ALERT_FIRST_DELAY,
    )
)
# 账号有效期：与额度预警同属「跟人有关」的巡检，放在它后面。
scheduler.register(
    _job(
        "user_expiry",
        "账号有效期巡检",
        "安全巡检",
        "到期前 7 / 3 / 1 天邮件提醒；到期后关闭该账号名下的虚拟机与容器并冻结账号",
        userexpiry.evaluate,
        USER_EXPIRY_INTERVAL,
        summarize=_expiry_summary,
        first_delay=USER_EXPIRY_FIRST_DELAY,
    )
)
scheduler.register(
    _job(
        "ssh_guard",
        "SSH 登录安全",
        "安全巡检",
        "检查本机的失败登录、爆破与异常来源，命中即告警",
        sshguard.evaluate,
        ALERT_INTERVAL,
        summarize=_count("命中"),
    )
)
scheduler.register(
    _job(
        "ssh_fleet",
        "受管主机 SSH 巡检",
        "安全巡检",
        "遍历已登记的受管主机，检查其 SSH 登录异常与策略偏移",
        sshremote.evaluate_fleet,
        ALERT_INTERVAL,
        summarize=_count("命中"),
    )
)
scheduler.register(
    _job(
        "ports",
        "端口进程巡检",
        "安全巡检",
        "扫描监听端口与关键进程，发现新增对外暴露的服务",
        portguard.evaluate,
        PORTS_INTERVAL,
        summarize=_count("命中"),
    )
)
# 安全基线快照预热：把全量体检结果落库，页面读快照而不是现场 SSH（见 app.inspection）。
# 端口那边的落库由上面那个「端口进程巡检」作业顺带完成，两者不重复扫同一批主机。
scheduler.register(
    _job(
        "inspection_warm",
        "安全基线快照预热",
        "安全巡检",
        "全量体检安全基线并落库，让页面读快照而不是现场 SSH",
        inspection.warm_once,
        INSPECTION_WARM_INTERVAL,
        summarize=_warm_summary,
        first_delay=INSPECTION_WARM_FIRST_DELAY,
    )
)
# 客户机 IP 预热：后台把 IP 落库，虚拟机列表读表，不再在请求内逐台等 Guest Agent
scheduler.register(
    _job(
        "guest_ip_warm",
        "客户机 IP 预热",
        "安全巡检",
        "后台解析客户机 IP 并落库，虚拟机列表不再在请求内等 Guest Agent",
        inspection.warm_guest_ip,
        GUEST_IP_WARM_INTERVAL,
        summarize=_warm_summary,
        first_delay=GUEST_IP_WARM_FIRST_DELAY,
    )
)
scheduler.register(
    _job(
        "backup_guard",
        "备份完整性核对",
        "安全巡检",
        "核对受保护备份是否仍然存在且未被篡改（防删 / 防篡改）",
        backupguard.evaluate,
        BACKUP_CHECK_INTERVAL,
        summarize=_count("命中"),
    )
)
# PBS 校验巡检：备份跑成功不等于能恢复，verify 未通过才是真正危险的信号。
# 与备份完整性核对同频：两者都要逐存储拉归档目录，共用同一类读取成本。
scheduler.register(
    _job(
        "backup_verify",
        "备份校验巡检",
        "安全巡检",
        "检查 PBS 归档的 verify 结果，未通过校验即告警（备份可能无法恢复）",
        verifyguard.evaluate,
        BACKUP_CHECK_INTERVAL,
        summarize=_count("命中"),
    )
)
# AI 虚拟机授权回收：每次首次写入都会在 PVE 侧打一个 pvecenter-ai-* 快照，
# 客户机里还会留一份回滚备份。不回收的话，存储会被这些「没人记得的快照」慢慢吃掉，
# 而用户在界面上看不到它们从哪来 —— 所以回收是授权机制的一部分，不是可选的收尾。
scheduler.register(
    _job(
        "ai_vm_cleanup",
        "AI 虚拟机授权回收",
        "维护",
        "回收过期授权留下的 PVE 快照与客户机回滚备份（保留时长见授权策略）",
        aivm.cleanup_expired,
        3600,
        summarize=lambda data: f"回收 {int((data or {}).get('removed') or 0)} 个",
        first_delay=600,
    )
)
# PBS 运行状态：GC 失败与磁盘健康。走 PBS 直连，没登记只读凭据时该作业安静返回，
# 不会每周目刷一条“未配置”。10 分钟一次足够 —— GC 通常一天一次，磁盘更慢。
scheduler.register(
    _job(
        "pbs_health",
        "PBS 运行状态",
        "安全巡检",
        "检查 PBS 的 GC 任务、磁盘健康与备份盘容量，异常即告警（需登记只读凭据）",
        pbsguard.evaluate,
        600,
        summarize=_count("命中"),
    )
)
# 资源额度对账：读数是逐台向 PVE 枚举出来的（没有第二份账），这里定时保鲜，
# 免得 PVE 侧手工增删过快照之后面板还按旧数字判额度
scheduler.register(
    _job(
        "quota_reconcile",
        "资源额度对账",
        "安全巡检",
        "重新枚举配了额度的用户的快照数，让读数跟上 PVE 侧的手工改动",
        quota.reconcile_snapshots,
        QUOTA_RECONCILE_INTERVAL,
        summarize=_quota_summary,
        first_delay=QUOTA_RECONCILE_FIRST_DELAY,
    )
)
# 归属对账：面板那本归属账会漂（PVE 侧手工增删的机器、被调低的额度、残留的快照行）。
# 面板自己的删除路径已经逐条清过，但逐个端点补不是办法 —— 这里兜底。
scheduler.register(
    _job(
        "owner_reconcile",
        "归属记录对账",
        "数据清理",
        "比对面板的归属账与 PVE 实际清单：清掉已消失机器的残留记录，并报出无人认领的机器",
        quota.reconcile_owners,
        OWNER_RECONCILE_INTERVAL,
        summarize=_owners_summary,
        first_delay=OWNER_RECONCILE_FIRST_DELAY,
    )
)
# 删号归属清理：比的是「归属账 vs 账号表」—— 人没了，记录也该删。删号的写点
# 已经会清（见 routers/users.delete_user），这里兜的是那一步失败或存量脏数据。
# 独立成一个作业而不是并进上面那个：对账要读 PVE，它只读两张本地表，失败面小，
# 也不该因为一条 PVE 连接读不到就整轮跳过。
scheduler.register(
    _job(
        "owner_prune",
        "删号归属清理",
        "数据清理",
        "清掉归属表里指向已不存在账号的记录（删号写点失败 / 存量数据的兜底）",
        quota.prune_deleted_owners,
        OWNER_RECONCILE_INTERVAL,
        summarize=_prune_summary,
        first_delay=OWNER_RECONCILE_FIRST_DELAY,
    )
)
scheduler.register(
    _job(
        "alert_rootcause",
        "告警根因聚合",
        "安全巡检",
        "多条告警疑似同源时聚合成一条根因报告并推送",
        alert_rootcause.run_once,
        ROOTCAUSE_INTERVAL,
        summarize=_rootcause_summary,
        first_delay=ROOTCAUSE_FIRST_DELAY,
    )
)
scheduler.register(
    _job(
        "host_audit",
        "主机登录审计导入",
        "安全巡检",
        "把各主机的 SSH 登录 / sudo 事件增量汇入面板审计日志",
        host_audit.import_events,
        HOST_AUDIT_INTERVAL,
        summarize=_import_summary,
    )
)
scheduler.register(
    _job(
        "metrics_sample",
        "监控历史采样",
        "监控与证书",
        "把节点与虚拟机的资源指标按周期采样进 metrics_history（实时那条走 WebSocket）",
        metrics_history.sample_once,
        METRICS_INTERVAL,
        summarize=_sample_summary,
    )
)
scheduler.register(
    _job(
        "certs",
        "证书状态同步与续期",
        "监控与证书",
        "同步网站证书状态并自动续期（免费证书只有 90 天）",
        cert_manager.renew_tick,
        CERT_INTERVAL,
        summarize=_cert_summary,
        first_delay=CERT_FIRST_DELAY,
    )
)
scheduler.register(
    _job(
        "cert_retry",
        "证书记录重试",
        "监控与证书",
        "对退避到点的续期失败站点补跑（10 分钟 / 30 分钟 / 2 小时 / 6 小时）",
        cert_manager.retry_tick,
        CERT_RETRY_INTERVAL,
        summarize=_cert_summary,
        first_delay=CERT_RETRY_FIRST_DELAY,
    )
)
scheduler.register(
    _job(
        "cert_monitor",
        "站点可用性探测",
        "监控与证书",
        "对开启监控的证书站点做 HTTP 探测，状态翻转时告警",
        cert_monitor.monitor_tick,
        300,
        summarize=_cert_summary,
        first_delay=240,
    )
)
scheduler.register(
    _job(
        "frp_watchdog",
        "内网穿透看护",
        "服务看护",
        "frpc 进程不在时自动拉起（仅在「自动拉起」开启时生效）",
        frp.watchdog,
        FRP_WATCHDOG_INTERVAL,
        summarize=_frp_summary,
        first_delay=FRP_WATCHDOG_FIRST_DELAY,
    )
)
scheduler.register(
    _job(
        "host_sync",
        "受管主机核对",
        "数据清理",
        "核对「面板下发」的受管主机与 PVE 上的虚拟机是否一致，清掉已删除机器留下的安全数据",
        sshremote.reconcile_panel_hosts,
        HOST_SYNC_INTERVAL,
        summarize=_host_sync_summary,
    )
)
scheduler.register(
    _job(
        "metrics_purge",
        "监控历史保留清理",
        "数据清理",
        "按 METRICS_RETENTION_DAYS 清理超期的指标采样行",
        metrics_history.purge_old,
        PURGE_INTERVAL,
        summarize=_purge_summary,
    )
)
scheduler.register(
    _job(
        "inspection_purge",
        "巡检快照保留清理",
        "数据清理",
        "清理超过保留期的巡检快照行，避免主机 / 语言变更留下残行",
        inspection.purge_old,
        PURGE_INTERVAL,
        summarize=_purge_summary,
    )
)
scheduler.register(
    _job(
        "notifications_purge",
        "站内通知保留清理",
        "数据清理",
        "清理已读且超期的站内消息",
        notifications.purge_old,
        PURGE_INTERVAL,
        summarize=_purge_summary,
    )
)
scheduler.register(
    _job(
        "audit_purge",
        "审计日志保留清理",
        "数据清理",
        "按 AUDIT_RETENTION_DAYS 清理超期的审计日志（0 = 永久保留）",
        _purge_audit,
        PURGE_INTERVAL,
        summarize=_purge_summary,
    )
)
scheduler.register(
    _job(
        "tokens_purge",
        "API Token 残留清理",
        "数据清理",
        "清理过期 / 已吊销超过 30 天的 API Token 记录",
        apitokens.purge_expired,
        TOKEN_PURGE_INTERVAL,
        summarize=_purge_summary,
    )
)
scheduler.register(
    _job(
        "trusted_devices_purge",
        "信任设备残留清理",
        "数据清理",
        "清理已过期的信任设备记录（登录两步验证的「7 天免校验」）",
        trusteddevices.purge_expired,
        TOKEN_PURGE_INTERVAL,
        summarize=_purge_summary,
    )
)
scheduler.register(
    _job(
        "update_check",
        "面板版本检查",
        "面板维护",
        "定期查 GitHub 上有没有新版本，有就给管理员发一条站内消息"
        "（默认一天一次，可在「设置 → 面板更新」里关掉自动检查）",
        update.run_check,
        UPDATE_CHECK_INTERVAL,
        # 摘要直接复用「命中 N 项 / 无变化」那套：run_check 返回发现的新版本列表
        summarize=_count("发现新版"),
        first_delay=UPDATE_CHECK_FIRST_DELAY,
    )
)

