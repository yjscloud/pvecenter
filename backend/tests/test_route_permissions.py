"""路由级权限守卫自检：``/api/config/*`` 不许再靠「逐端点记得写 Depends」。

审计项的背景：这一组接口原先只在处理函数参数里逐个写权限依赖，于是同组内出现
两种档位 —— ``/config/connection``、``/config/diagnostics``、
``/config/security-posture`` 要 ``settings.manage``，而 ``/config/mail``、
``/config/session``、``/config/specs``、``/config/vm-quota``、``/config/ui``
只要登录。operator 因此读到了 SMTP 主机与端口、发件账号、管理员收件人、
会话策略与配额上限。

修复分两步，两步都由本用例钉住（**不查库、不发请求**，只对着路由表断言）：

1. ``app/routeguard.py`` 里有一张「前缀 → 权限」表，守卫挂在 router 的
   ``dependencies`` 上 —— 所以新增端点落在受控前缀下会**自动**被覆盖；
2. 万一以后有人把守卫从 router 上摘掉、或把某个端点挪出受控 router，本用例会红。

第 3 条守的是另一次回归：为了「把 /api/config 整组收成一档」而把创建向导要用的
读数一起关死。那几条读数已经挪到 ``/api/runtime/*``，本用例顺便确认它们仍然
只要求登录、且**只能读**（写入口必须留在 /api/config）。
"""
from __future__ import annotations

from typing import Any, Iterator, Set

import pytest
from fastapi.routing import APIRoute

from app import main, routeguard, security

#: 运行时读视图前缀：登录即可，但只读
RUNTIME_PREFIX = "/api/runtime"


def _iter_api_routes() -> Iterator[APIRoute]:
    for route in main.app.routes:
        if isinstance(route, APIRoute):
            yield route


def _under(path: str, prefix: str) -> bool:
    """路径是否在 ``prefix`` 之下（卡分隔符边界：/api/configx 不算）。"""
    return path == prefix or path.startswith(prefix + "/")


def _dependency_calls(route: APIRoute) -> Set[Any]:
    """路由依赖树里出现的全部可调用对象（含子依赖）。"""
    calls: Set[Any] = set()
    stack = list(getattr(getattr(route, "dependant", None), "dependencies", []))
    seen = set()
    while stack:
        dep = stack.pop()
        if id(dep) in seen:
            continue
        seen.add(id(dep))
        if dep.call is not None:
            calls.add(dep.call)
        stack.extend(getattr(dep, "dependencies", []) or [])
    return calls


def _guarded(route: APIRoute) -> bool:
    return routeguard.enforce_route_permission in _dependency_calls(route)


def _requires_login(route: APIRoute) -> bool:
    return security.get_current_user in _dependency_calls(route)


def _config_routes() -> list:
    return [
        route
        for route in _iter_api_routes()
        if _under(route.path, "/api/config")
    ]


def test_config_group_exists() -> None:
    """先证明真的扫到了东西：正则 / 路由遍历失效时下面的断言会假绿。"""
    paths = {route.path for route in _config_routes()}
    assert "/api/config/mail" in paths
    assert len(paths) >= 6, paths


def test_every_config_route_is_guarded() -> None:
    """``/api/config`` 下**每一条**路由都带着前缀守卫（匿名白名单除外）。

    这条是审计修复的核心断言：守卫属于 router，不属于哪个函数记得写
    ``Depends``。新增一条 ``/api/config/xxx`` 时它会自动被覆盖；若有人把
    dependencies 摘掉、或把端点挂到别的 router 上，这里立刻红。
    """
    for route in _config_routes():
        for method in sorted(route.methods or set()):
            if routeguard.is_public(method, route.path):
                # 白名单端点也必须**在**受控前缀里被「识别」出来，只是放行
                continue
            assert _guarded(route), (
                f"{method} {route.path} 没有路由级权限守卫 —— "
                "app/routers/config.py 的 router.dependencies 被摘掉了？"
            )
            assert routeguard.rule_for(method, route.path) is not None, (
                f"{method} {route.path} 落在受控前缀下，但 routeguard.ROUTE_PERMISSIONS "
                "里没有对应规则"
            )
            # 守卫只加权限判定，认证仍由端点自己声明；两者缺一都算漏
            assert _requires_login(route), f"{method} {route.path} 缺少登录依赖"


def test_public_whitelist_is_exact_and_alive() -> None:
    """白名单是「方法 + 完整路径」精确匹配，且每条都对应真实存在的路由。

    精确匹配是关键：用通配（``/api/config/site*``）的话，以后在同一前缀下新增
    的管理端点会被一起放出去。第二条断言防「路径改名后白名单还留着」——
    留着不会立刻造成泄露，但它会让下一个人以为那个路径是公开的。
    """
    existing = {(method, route.path) for route in _iter_api_routes() for method in route.methods}
    for method, path in routeguard.PUBLIC_ROUTES:
        assert method == "GET", (method, path)
        assert (method, path) in existing, f"白名单里的 {method} {path} 已经不存在了"
        # 白名单只对**这一个方法**生效：同一路径的写入口仍然要权限
        for other in ("POST", "PUT", "PATCH", "DELETE"):
            assert not routeguard.is_public(other, path), (other, path)


def test_public_whitelist_contains_only_branding_and_assets() -> None:
    """白名单不许扩张到「带配置含义」的路径。"""
    allowed = {
        "/api/config/site",
        "/api/config/faq",
        "/api/config/site/logo",
        "/api/config/site/login-bg",
    }
    assert {path for _method, path in routeguard.PUBLIC_ROUTES} == allowed, (
        "匿名白名单变了。登录页 / 官网需要的品牌与图片素材是唯一的例外；"
        "任何带配置含义的路径（mail / session / quota / connection …）都不该进这里"
    )


def test_runtime_views_are_login_only_and_read_only() -> None:
    """``/api/runtime/*``：所有登录用户可读，但**只能读**。

    这几条读数（UI 开关 / 创建默认值 / 规格）是创建向导与下单页的输入，
    所以不能要 ``settings.manage``；但它们也不该出现写入口 —— 写是管理动作，
    留在 ``/api/config``。
    """
    routes = [
        route for route in _iter_api_routes() if _under(route.path, RUNTIME_PREFIX)
    ]
    assert routes, "没有找到 /api/runtime/* 路由"
    for route in routes:
        assert route.methods and route.methods <= {"GET", "HEAD"}, (
            f"{route.path} 允许 {sorted(route.methods)} —— "
            "运行时读视图只放只读方法，写入口请留在 /api/config"
        )
        assert _requires_login(route), f"{route.path} 没有登录依赖"


def test_runtime_prefix_is_not_shadowed_by_config_guard() -> None:
    """两条前缀互不重叠：``/api/runtime`` 不该被 config 组的规则命中。"""
    for path in (
        "/api/runtime/ui",
        "/api/runtime/specs",
        "/api/runtime/specs/presets",
        "/api/runtime/vm-defaults",
    ):
        assert routeguard.rule_for("GET", path) is None, path
        assert not routeguard.is_public("GET", path), path


# --------------------------------------------------------------- 表本身的性质


@pytest.mark.parametrize(
    "method,path,expected",
    [
        # 整组配置：读与写同一档
        ("GET", "/api/config/mail", "settings.manage"),
        ("GET", "/api/config/session", "settings.manage"),
        ("GET", "/api/config/vm-quota", "settings.manage"),
        ("PUT", "/api/config/mail", "settings.manage"),
        ("PUT", "/api/config/specs", "settings.manage"),
        ("DELETE", "/api/config/faq", "settings.manage"),
        # 相邻前缀不能被当成子路径
        ("GET", "/api/configx", None),
        ("GET", "/api/connections", None),
        ("GET", "/api/health", None),
        # 尾部斜杠与 HEAD 都归一
        ("GET", "/api/config/mail/", "settings.manage"),
        ("HEAD", "/api/config/mail", "settings.manage"),
    ],
)
def test_rule_matching(method: str, path: str, expected) -> None:
    rule = routeguard.rule_for(method, path)
    if expected is None:
        assert rule is None, (method, path, rule)
    else:
        assert rule is not None and rule.permission == expected, (method, path, rule)
