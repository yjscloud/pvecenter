"""PBS 监控接口：连接配置（管理）+ 只读概览（查看）。

分两个权限点：

* ``pbs.manage`` —— 登记 / 修改 / 测试 PBS 只读凭据。这是**管理动作**：
  凭据本身决定面板能看到多少 PBS 数据，不能跟着 ``storage.view`` 扩散。
* ``pbs.view`` —— 看概览。PBS 没有“归属”维度（任务是仓库级的，不含客户机
  归属），所以这一项默认只给管理员，与 ``ssh.view`` / ``baseline.view`` 同类。

三态判定也在这里收口：``pve_storages`` 是「PVE 侧有没有 PBS 存储」，
``connections`` 是「有没有直连凭据」，前端据此在「未对接 / 仅间接 / 完整」之间
选择展示方式，不需要自己拼两个接口。
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import pbs, pve, security
from ..pve import ProxmoxError
from ..schemas import ConnectionPauseIn, PbsConfigIn, PbsConnectionTestIn

router = APIRouter(prefix="/api/pbs", tags=["pbs"])

MANAGE = security.require_permission("pbs.manage")
VIEW = security.require_permission("pbs.view")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


async def _pve_pbs_storages() -> List[Dict[str, Any]]:
    """PVE 侧登记了哪些 PBS 存储（用于判断「是否已对接」）。

    ``/storage`` 是数据中心级定义，含 ``type`` / ``datastore`` / ``server``；
    它需要 ``Datastore.Audit``，读不到时返回空列表 —— 前端会把这种情况当成
    「没有 PBS 存储」，所以接口另外用 ``configured`` 区分「有直连凭据」，
    不至于把权限不足误报成未对接。
    """
    out: List[Dict[str, Any]] = []
    pairs = pve.all_connection_clients()
    if not pairs:
        return out
    for profile, client in pairs:
        cid = str((profile or {}).get("id") or "")
        try:
            items = await client.storages()
        except ProxmoxError:
            continue
        for item in items or []:
            if str(item.get("type") or "").strip().lower() != "pbs":
                continue
            out.append(
                {
                    "connection_id": cid,
                    "connection_name": pve.connection_label(profile),
                    "storage": str(item.get("storage") or ""),
                    "datastore": str(item.get("datastore") or ""),
                    "server": str(item.get("server") or ""),
                    "port": item.get("port") or 8007,
                    "used_by": [],
                }
            )

    # 同一台 PBS 可能被多套 PVE 共用：按 server:port 聚合。
    # 不聚合的话，概览里同一台 PBS 会出现多次，容量看起来像翻倍。
    merged: Dict[str, Dict[str, Any]] = {}
    for item in out:
        key = f"{item['server']}:{item['port']}"
        label = item.get("connection_name") or item.get("connection_id") or ""
        if key in merged:
            merged[key]["used_by"].append(label)
            continue
        item["used_by"] = [label]
        merged[key] = item
    return list(merged.values())


@router.get("/config")
async def list_config(
    user: Dict[str, Any] = Depends(MANAGE),
) -> List[Dict[str, Any]]:
    """PBS 连接列表（密钥只回 ``token_secret_set``）。"""
    conns = await pbs.load_connections()
    return [pbs.public_connection(item) for item in conns]


@router.put("/config")
async def save_config(
    body: PbsConfigIn,
    user: Dict[str, Any] = Depends(MANAGE),
) -> List[Dict[str, Any]]:
    """整体保存连接列表。

    ``token_secret`` 留空表示**沿用原密钥**：前端从不回显密钥，编辑一条已有
    连接时不重新输入也不该把凭据清空。
    """
    existing = {str(item.get("id") or ""): item for item in await pbs.load_connections()}
    saved: List[Dict[str, Any]] = []
    for item in body.connections:
        raw = item.model_dump()
        conn_id = str(raw.get("id") or "").strip() or _new_id()
        # 与 PbsConnection 保持一致：复制来的 secret 常带尾部空白，落库前清掉
        secret = str(raw.get("token_secret") or "").strip()
        if not secret:
            secret = str((existing.get(conn_id) or {}).get("token_secret") or "")
        # 暂停状态：没传就沿用已保存的值。开关只由 pause 端点改，整份保存不该
        # 顺手把它清掉（早先 PVE 连接在这里踩过坑）。
        paused = raw.get("paused")
        if paused is None:
            paused = (existing.get(conn_id) or {}).get("paused")
        saved.append(
            {
                "id": conn_id,
                "name": str(raw.get("name") or "").strip(),
                # 区域只是分组标签：不校验、不参与连接，留空即「未分区」
                "region": str(raw.get("region") or "").strip(),
                "host": str(raw.get("host") or "").strip(),
                "port": int(raw.get("port") or 8007),
                "token_id": str(raw.get("token_id") or "").strip(),
                "token_secret": secret,
                "verify_ssl": raw.get("verify_ssl"),
                "ca_cert": str(raw.get("ca_cert") or ""),
                "node": str(raw.get("node") or "").strip(),
                "paused": bool(paused),
            }
        )
    await pbs.save_connections(saved)
    return [pbs.public_connection(item) for item in saved]


@router.post("/config/{conn_id}/pause")
async def pause_connection(
    conn_id: str,
    body: ConnectionPauseIn,
    request: Request,
    user: Dict[str, Any] = Depends(MANAGE),
) -> List[Dict[str, Any]]:
    """暂停 / 恢复某台 PBS 的接入。

    暂停后不再向这台 PBS 发任何请求（仓库容量、任务、磁盘、GC 全停），概览里
    仍保留一行并标 ``paused``：界面显示「已暂停」，在线率与告警都把它排除在外。
    配置、凭据、区域、节点名全部保留，恢复后下一轮采集自动重连。

    只改这一条记录的一个字段：整份 ``PUT /config`` 会覆盖所有连接，两个人同时
    编辑时开关状态容易被对方那份旧数据带回旧值。
    """
    conns = await pbs.load_connections()
    if not any(str(item.get("id") or "") == conn_id for item in conns):
        raise HTTPException(status_code=404, detail="PBS 连接不存在")

    updated: List[Dict[str, Any]] = []
    for item in conns:
        if str(item.get("id") or "") == conn_id:
            updated.append({**item, "paused": bool(body.paused)})
        else:
            updated.append(item)
    await pbs.save_connections(updated)

    await security.audit(
        request,
        user,
        "pbs.connection.pause" if body.paused else "pbs.connection.resume",
        target=f"pbs/{conn_id}",
        detail={"paused": bool(body.paused)},
    )
    return [pbs.public_connection(item) for item in updated]


@router.post("/config/test")
async def test_config(
    body: PbsConnectionTestIn,
    user: Dict[str, Any] = Depends(MANAGE),
) -> Dict[str, Any]:
    """连通性测试：先调 ``/version`` 验认证，再逐项探测能力（都是 GET）。

    探测项是仓库列表、首个仓库的容量、任务与节点状态 —— 每一项都对应一条独立
    ACL，一次测全才能直接告诉用户「连上了，但缺哪一项权限」。全是 GET，不会
    触发 PBS 侧的备份 / GC / verify 等重活。
    """
    raw = body.model_dump()
    conn_id = str(raw.get("id") or "").strip()
    secret = str(raw.get("token_secret") or "")
    if not secret and conn_id:
        for item in await pbs.load_connections():
            if str(item.get("id") or "") == conn_id:
                secret = str(item.get("token_secret") or "")
                break
    cfg = pbs.PbsConnection.from_dict({**raw, "token_secret": secret})
    if not cfg.configured:
        return {"ok": False, "error": "pbs_not_configured"}
    client = pbs.PbsClient(cfg)
    try:
        version = await client.version()

        # 能力探测：PBS 没有只读的 system 角色（Sys.Audit 只在 Admin 里），所以
        # 「/version 能通」不代表「任务与磁盘能看」。这里逐项探，避免用户看到
        # “连接成功”却在概览里对着一片空白。
        #
        # 注意 tasks 与 node_status 的权限是**分开**的：任务列表只需要
        # Datastore.Audit，节点状态才要 Sys.Audit。所以 `/nodes` 失败时要像
        # `_collect_one` 一样退回 localhost 继续探任务，否则会把本来能读的任务
        # 误报成不可用。
        capabilities = {
            "datastores": False,
            # 仓库容量走的是另一个端点（/admin/datastore/{store}/status），
            # 「能看到仓库」不代表「能看到容量」—— 权限或版本都可能卡在这一步，
            # 所以单独探一次，别让用户对着“连接成功”却看不到使用率。
            "datastore_status": False,
            "tasks": False,
            "node_status": False,
        }
        probe_errors: Dict[str, str] = {}
        stores: List[Dict[str, Any]] = []
        try:
            stores = [
                item for item in await client.datastores() if isinstance(item, dict)
            ]
            capabilities["datastores"] = True
        except ProxmoxError as exc:
            probe_errors["datastores"] = exc.code or f"HTTP {exc.pve_status}"

        # 列表返回空不等于没有仓库：PBS 对无权限的 token 直接回 []。
        # 用权限表把「确实没有仓库」和「缺 Datastore.Audit」分开。
        if not stores and "datastores" not in probe_errors:
            try:
                perms = await client.get("/access/permissions")
                if not pbs.has_datastore_audit(perms):
                    probe_errors["datastores"] = "no_datastore_audit"
            except ProxmoxError:
                pass

        node_name = cfg.node
        try:
            nodes = await client.nodes()
            node_name = node_name or (
                str(nodes[0].get("node") or "") if nodes else ""
            )
        except ProxmoxError:
            node_name = node_name or "localhost"

        if node_name:
            try:
                await client.node_status(node_name)
                capabilities["node_status"] = True
            except ProxmoxError:
                pass
            try:
                await client.tasks(node_name, limit=1)
                capabilities["tasks"] = True
            except ProxmoxError:
                pass

        # 容量：拿第一个仓库探一次。列表都读不到就不必再探，错误码已记在
        # capabilities["datastores"] 上。
        first_store = str(stores[0].get("store") or "") if stores else ""
        if first_store:
            try:
                await client.datastore_status(first_store)
                capabilities["datastore_status"] = True
            except ProxmoxError as exc:
                probe_errors["datastore_status"] = exc.code or f"HTTP {exc.pve_status}"
    except ProxmoxError as exc:
        # 带上 code 让前端换成人话：后端 message 是给日志看的，
        # 401 的原样文案（“HTTP 401”）对填表的人没有任何指向性。
        # 认证失败再附一份“凭据形状”诊断（不含 secret 明文），
        # 用来区分「填成账号密码」「填成 token ID」「带前缀整行粘贴」。
        result: Dict[str, Any] = {
            "ok": False,
            "error": exc.message,
            "code": exc.code or "",
        }
        if exc.code == "pbs_auth_failed":
            result["diagnostics"] = pbs.diagnose(cfg)
        return result
    finally:
        await client.aclose()
    return {
        "ok": True,
        "version": str(version.get("version") or version.get("release") or ""),
        "capabilities": capabilities,
        # 探测失败项的错误码（如 pbs_permission_denied / not_found），
        # 让界面能说「容量接口 403」而不是笼统的「不提供」
        "probe_errors": probe_errors,
    }


@router.get("/overview")
async def overview(
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """PBS 概览 + PVE 侧的对接情况。"""
    data = await pbs.collect_overview()
    data["pve_storages"] = await _pve_pbs_storages()
    return data


@router.get("/task-log")
async def task_log(
    upid: str,
    connection_id: str = "",
    node: str = "",
    user: Dict[str, Any] = Depends(VIEW),
) -> Dict[str, Any]:
    """一条 PBS 任务的日志（只读）。

    只给 ``pbs.view``：任务日志里可能出现仓库路径与客户机编号，与概览同级。
    不缓存 —— 日志本来就是「点开看当时发生了什么」，缓存会把“还在跑”的任务
    日志冻在旧状态。
    """
    conns = await pbs.load_connections()
    if not conns:
        raise HTTPException(status_code=404, detail="pbs_not_configured")
    target = None
    if connection_id:
        target = next(
            (
                item
                for item in conns
                if str(item.get("id") or "") == connection_id
            ),
            None,
        )
    if target is None and len(conns) == 1:
        target = conns[0]
    if target is None:
        # 多连接时不猜：让前端带上 connection_id，避免“日志来自另一台 PBS”
        raise HTTPException(status_code=400, detail="pbs_connection_required")

    cfg = pbs.PbsConnection.from_dict(target)
    client = pbs.PbsClient(cfg)
    try:
        lines = await client.task_log(node or cfg.node, upid)
    except ProxmoxError as exc:
        raise HTTPException(status_code=502, detail=exc.message)
    finally:
        await client.aclose()
    return {
        "lines": [
            str(item.get("t") or "")
            for item in lines
            if isinstance(item, dict)
        ]
    }
