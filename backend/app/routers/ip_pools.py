"""IP 地址池的接口层。

池的读取、占用判定、归属规则与自动分配都在 :mod:`app.ippool`；这里只保留
HTTP 出入参、权限与保存时的越权防护。下游（虚拟机 / 容器的创建路径）直接用
``app.ippool``，不绕这一层。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request, Response

from .. import ippool, security, store
from ..schemas import IpPool

router = APIRouter(prefix="/api", tags=["ip-pools"])

# 常量随业务逻辑一起搬到了 app.ippool；这里保留同名引用，旧调用方不受影响。
POOLS_KEY = ippool.POOLS_KEY
MAX_FREE = ippool.MAX_FREE
ALL_USERS = ippool.ALL_USERS


def _owners_of(pool: Dict[str, Any]) -> List[str]:
    """兼容读法的薄包装（保存路径与列表出参共用）。"""
    return ippool.owners_of(pool)


def _legacy_pool_id(pool: Dict[str, Any]) -> str:
    return ippool.legacy_pool_id(pool)


# 旧名字的薄包装：池的业务逻辑搬去了 app.ippool，但这一层曾经是这些函数唯一
# 的所在（脚本 / 手工排查可能按旧路径引用），保留下来零成本。
_load = ippool.load_pools
_collect_used = ippool.collect_used
_pool_free = ippool.pool_free
_visible_to = ippool.visible_to
_merge_for_scope = ippool.merge_for_scope


@router.get("/ip-pools")
async def get_ip_pools(
    response: Response,
    user: Dict[str, Any] = Depends(security.require_permission("network.view")),
) -> Dict[str, Any]:
    """返回可见的池，附带各自的空闲地址列表与全量已占用地址。

    普通用户只拿得到**分给自己**的池（名单里有他本人，或名单是「全部用户」）：
    池是「给谁取地址」的资源，把别人的池列进候选，选中就是往不属于自己的网段里
    塞地址。管理员不受限，公共池（名单为空）也只有管理员看得到。

    ``used`` 里既有 PVE 配置扫出来的静态地址，也有面板刚分配出去、配置还没
    落盘的登记地址 —— 后者见 :mod:`app.ippool` 的「分配登记」一节。
    """
    pools = await ippool.load_pools()
    scope = security.visible_owner(user)
    if scope is not None:
        pools = [p for p in pools if ippool.visible_to(p, scope)]
    used, meta = await ippool.collect_used_meta()
    # 诊断响应头：排查「回填慢」时先看这两个值 —— miss + 大 scan_ms 说明慢在
    # 集群扫描，hit 说明扫描已 amortize，慢点在别处（网络 / 前端渲染）。
    response.headers["X-IP-Pool-Snapshot"] = "hit" if meta["cache_hit"] else "miss"
    response.headers["X-IP-Pool-Scan-Ms"] = str(meta["scan_ms"])
    response.headers["X-IP-Pool-Snapshot-Age-Ms"] = str(meta["age_ms"])
    response.headers["X-IP-Pool-Guests"] = str(meta["guests"])
    out: List[Dict[str, Any]] = []
    for pool in pools:
        free = ippool.pool_free(pool, used)
        # 统一按 owners 出参：单值时代的 owner 只在这里兼容读取，不再往外发 ——
        # 两套字段并存的话，迟早有人读到过时的那一份
        item = {
            **pool,
            "owners": _owners_of(pool),
            "free": free,
            "free_count": len(free),
        }
        item.pop("owner", None)
        out.append(item)
    return {"pools": out, "used": sorted(used.keys())}


@router.put("/ip-pools")
async def save_ip_pools(
    payload: List[IpPool],
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("network.manage")),
) -> Dict[str, Any]:
    """保存地址池。管理员整表覆盖；其他人只能改「分配给自己的」那几行。

    为什么非管理员不能整表覆盖：这个接口以前是无条件 ``set_setting``，任何拿到
    ``network.manage`` 的人都能改别人的 ``owners``（或填 ``["*"]``）、还能靠
    「提交里不含它」把别人的池删掉 —— 一条完整的横向提权链。现在按 id 合并：

    * 提交的行必须已存在、且原本就分给自己（新增一律拒绝，池是管理员分配的资源）；
    * ``owners`` 必须与旧值完全一致（能用，不能转赠）；
    * 没提交的池：自己可见的视为删除，**看不见的原样保留** —— 否则普通用户一保存
      就会把别人的池整片覆盖掉。

    管理员路径保持整表覆盖，行为与以前完全一致。
    """
    submitted: List[Dict[str, Any]] = []
    for pool in payload:
        item = pool.model_dump()
        # 归属用户按用户名存，顺手去掉界面可能带进来的空白与重复项，免得出现
        # 「test01 」这种看不见的差异把匹配搞丢。
        # ``or item.get("owner")`` 是给还没刷新的旧页面兜底：旧页面只发单值 owner，
        # 忽略它就等于把池改回公共池（普通用户那边这些池会直接消失）。
        item["owners"] = ippool.clean_owners(item.get("owners") or item.get("owner"))
        item.pop("owner", None)
        if not str(item.get("id") or "").strip():
            item["id"] = _legacy_pool_id(item)
        submitted.append(item)

    scope = security.visible_owner(user)
    if scope is None:
        data = submitted
    else:
        data = ippool.merge_for_scope(submitted, await ippool.load_pools(), scope)

    await store.set_setting(ippool.POOLS_KEY, json.dumps(data, ensure_ascii=False))
    await security.audit(
        request, user, "ip_pools.save", target="ip_pools",
        detail={"count": len(data), "scope": scope or "*"},
    )
    return {"pools": data}
