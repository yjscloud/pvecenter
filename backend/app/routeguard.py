"""路由前缀 → 所需权限 的统一守卫。

**为什么要有这一层**（审计项：``/api/config/*`` 路由级鉴权遗漏）：

``/api/config`` 下的端点此前是**逐个**在参数里写 ``Depends(require_permission(...))``
的，于是同一组接口出现了两种档位：``/config/connection``、``/config/diagnostics``、
``/config/security-posture`` 要 ``settings.manage``，而 ``/config/mail``、
``/config/session``、``/config/specs``、``/config/vm-quota``、``/config/ui``
只要**登录** —— 后者把管理员收件邮箱、SMTP 主机与端口、会话策略、配额与默认
规格交给了任何一个 operator。逐端点装饰的漏法是结构性的：新增一条路由时
「忘了写装饰器」不会报错，只会静默退化成「登录即可读」。

所以这里改成**前缀级**声明：一条请求属于哪个前缀，就按 :data:`ROUTE_PERMISSIONS`
判定，而不是看函数签名里有没有记得写 ``Depends``。守卫挂在 router 上（见
``app/routers/config.py``），``tests/test_route_permissions.py`` 再对着真实路由表
做静态核对 —— 落在受控前缀下却没人给它加守卫时，用例直接红。

**关于权限键。** 审计报告建议的 ``config.view`` / ``config.manage`` 与既有的
``settings.manage`` 是同一件事（见 ``security.PERMISSION_CATALOG`` 里
「系统设置 → 管理：面板自身的配置」）。这里**不新增两个同义的键**：那会让管理员
必须同时勾选两个名字不同、含义相同的项，漏勾一个就是「勾了不生效」——
正是本仓库在别处反复避免的那类问题。读写都收在 ``settings.manage`` 这一档：
能改面板配置的人当然也能看它，而没有任何 ``settings.manage`` 的账号（operator /
viewer）对该组接口一律 403。

**为什么保留公开白名单。** ``/config/site``、``/config/faq``、Logo 与登录页背景图
是**登录页 / 官网**的渲染素材，未登录就要能取；它们只含品牌文案与图片，不含
连接信息、凭据或内网地址（见各端点自己的说明）。白名单按 ``(方法, 完整路径)``
精确匹配，不用通配 —— 通配迟早会把同前缀下新增的管理端点一起放出去。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Optional, Tuple

from fastapi import HTTPException, Request, status

from . import i18n, security


@dataclass(frozen=True)
class RouteRule:
    """一条「前缀 → 权限」声明。

    ``methods`` 为空表示该前缀下**所有方法**同一档（``/api/config`` 就是这样：
    读与写都是 ``settings.manage``）。需要区分时再逐方法拆条目。
    """

    prefix: str
    permission: str
    methods: FrozenSet[str] = field(default_factory=frozenset)


#: 受守卫前缀 → 所需权限。**新增受控前缀只改这里**，不要在端点里另写一份。
#:
#: 顺序无关紧要（匹配时取最长前缀），但保持「由严到松」读起来更清楚。
ROUTE_PERMISSIONS: Tuple[RouteRule, ...] = (
    # 面板自身配置：连接 / 邮件 / 会话策略 / 配额 / 规格 / UI / 站点 / FAQ 写入口。
    # 读与写同一档：这些读数拼起来就是一张内网 + 账号拓扑（SMTP 主机、管理员
    # 收件人、PVE 地址、会话窗口、配额上限）。
    RouteRule("/api/config", "settings.manage"),
)

#: 匿名可读的端点：``(方法, 完整路径)`` 精确匹配。
#:
#: 只有登录页 / 官网渲染必需的品牌与图片素材在此列。往里加条目等于公开一个
#: 端点，评审时应当被单独质疑一次。
PUBLIC_ROUTES: FrozenSet[Tuple[str, str]] = frozenset(
    {
        ("GET", "/api/config/site"),
        ("GET", "/api/config/faq"),
        # Logo 与登录页背景图：FileResponse，未登录就要显示
        ("GET", "/api/config/site/logo"),
        ("GET", "/api/config/site/login-bg"),
    }
)

#: 受守卫的路径前缀（供静态校验与快速短路用）
GUARDED_PREFIXES: Tuple[str, ...] = tuple(rule.prefix for rule in ROUTE_PERMISSIONS)


def _method(method: str) -> str:
    """HEAD 与 GET 同权（HEAD 不返回正文，但能用来探测端点是否存在）。"""
    upper = str(method or "").upper()
    return "GET" if upper == "HEAD" else upper


def _path(path: str) -> str:
    """去掉尾部斜杠：``/api/config/mail/`` 与 ``/api/config/mail`` 是同一个端点。"""
    trimmed = str(path or "")
    if len(trimmed) > 1 and trimmed.endswith("/"):
        return trimmed.rstrip("/") or "/"
    return trimmed


def is_public(method: str, path: str) -> bool:
    """该「方法 + 路径」是否在匿名白名单里。"""
    return (_method(method), _path(path)) in PUBLIC_ROUTES


def rule_for(method: str, path: str) -> Optional[RouteRule]:
    """命中哪条规则；``None`` = 这个前缀不归守卫管。

    匹配要卡在**路径分隔符边界**上：``/api/configx`` 不是 ``/api/config`` 的
    子路径，否则给相邻前缀留了个绕过口。
    """
    wanted = _path(path)
    verb = _method(method)
    best: Optional[RouteRule] = None
    for rule in ROUTE_PERMISSIONS:
        prefix = _path(rule.prefix)
        if wanted != prefix and not wanted.startswith(prefix + "/"):
            continue
        if rule.methods and verb not in rule.methods:
            continue
        if best is None or len(prefix) > len(_path(best.prefix)):
            best = rule
    return best


async def enforce_route_permission(request: Request) -> None:
    """路由级守卫：按 :data:`ROUTE_PERMISSIONS` 判定当前请求所需权限。

    挂在 ``/api/config`` 所在 router 的 ``dependencies`` 上，所以**每条**落在
    受控前缀下的端点都会经过它，与端点自己写了什么无关。

    鉴权刻意**懒做**：只有命中受控前缀才去解析凭据。这条依赖会跟着 router 上
    的每一条路由执行（包括 ``/api/health``、``/api/connections`` 这些同 router
    的热点接口），不能给它们平白多加一次会话校验 / 数据库查询。
    """
    verb = _method(request.method)
    # 预检请求不带凭据，拦了等于让浏览器把所有跨域请求判成失败
    if verb == "OPTIONS":
        return
    path = request.url.path
    if not path.startswith(GUARDED_PREFIXES):
        return
    if is_public(verb, path):
        return
    rule = rule_for(verb, path)
    if rule is None:
        return

    # 两条都走 security 的同一套凭据解析（Authorization 头优先，其次 HttpOnly
    # cookie）；optional 版本只用于「有没有登录」，失败原因交给强制版本原样抛：
    # 401 的文案、维护期的 503 都保持与端点自己的依赖一致。
    token = await security.oauth2_scheme(request)
    user = await security.get_current_user_optional(request, token)
    if user is None:
        await security.get_current_user(request, token)
        return  # pragma: no cover - get_current_user 在无凭据时必抛

    if rule.permission and not security.has_user_permission(user, rule.permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=i18n.t(
                "error.permission_denied",
                permission=rule.permission,
                path=path,
            ),
        )
