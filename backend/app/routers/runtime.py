"""面板运行时读数：**所有登录用户**都要用的只读视图。

**为什么单开一个前缀**（审计项：``/api/config/*`` 路由级鉴权遗漏）。

这几条读数原先挂在 ``/api/config/*`` 下，与「管理端配置」混在同一组里：控制台
外壳渲染前要知道哪些导航入口被关掉了（``/config/ui``），创建向导要预填默认
克隆方式（``/config/vm-defaults``），下单页要渲染规格卡片（``/config/specs``）。
因为它们必须对每个登录用户可用，整组 ``/api/config`` 的鉴权只能写成「登录即可」，
而同组的 ``/config/mail``、``/config/session`` 需要的却是 ``settings.manage``
—— 于是一份逐端点手写的守卫表里就漏出了 SMTP 拓扑与会话策略。

修法不是给它们单独开白名单，而是**按读者分家**：

* 这里（``/api/runtime``）：给所有人看的读数，只要求登录，只返回渲染所需字段；
* ``/api/config``：管理端配置，统一 ``settings.manage``（见 ``app/routeguard.py``）。

分家之后 ``/api/config/*`` 才能整组收成一档权限，operator / viewer 对整组
（除匿名白名单）一律 403 —— 这正是复测要的结果。

这里**只放 GET**：写入口留在 ``/api/config``（那是管理动作）。``tests/
test_route_permissions.py`` 静态钉住这一条，免得以后有人往这里加一个写接口
把「登录即可写」带进来。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from .. import defaults, security, specs, ui

router = APIRouter(prefix="/api/runtime", tags=["runtime"])


@router.get("/ui")
async def get_ui_prefs(
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """面板界面开关（被关闭的导航栏入口）。

    只要求登录，而且**必须是登录**：控制台外壳按它决定渲染哪些入口，被关掉的
    入口如果因为拿不到这份读数而回落到「全部显示」，开关就等于没生效。写入在
    ``PUT /api/config/ui``（``settings.manage``，对所有人生效）。
    """
    return await ui.get_ui_prefs()


@router.get("/vm-defaults")
async def get_vm_defaults(
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """创建向导的预填值：默认 DNS 与默认克隆方式。

    只读、无敏感信息（一个 DNS 地址 + 一个布尔），写入在
    ``PUT /api/config/vm-defaults``。
    """
    return await defaults.get_vm_defaults()


@router.get("/specs")
async def get_specs(
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """当前生效的资源规格（下单页的规格卡片）。

    「几核几 G 多大盘」本来就是给下单的人挑的，不给它读就没法下单；写入
    （整份覆盖）在 ``PUT /api/config/specs``。
    """
    return {"specs": await specs.list_specs()}


@router.get("/specs/presets")
async def get_spec_presets(
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """内置的常用规格（只读、纯静态数据，不碰库）。"""
    return {"specs": specs.presets()}
