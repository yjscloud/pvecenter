"""证书工作流运行实例接口：发起、列表、详情、审批与逐步流转。

权限与工作流定义一致：查看要 ``cert.view``，发起 / 审批 / 流转要
``cert.manage``。运行实例按用户名隔离，管理员也不会跨用户操作别人的流程。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from .. import (
    certworkflow_exec,
    certworkflow_run,
    certworkflow_store,
    certs,
    i18n,
    security,
)

router = APIRouter(prefix="/api/cert-workflow-runs", tags=["cert-workflow-runs"])

CERT_VIEW = security.require_permission("cert.view")
CERT_MANAGE = security.require_permission("cert.manage")


class CreateRunPayload(BaseModel):
    workflow_id: str = ""
    site_id: str = ""
    title: str = ""
    note: str = ""


class TransitionPayload(BaseModel):
    action: str = ""
    note: str = ""


def _owner(user: Optional[Dict[str, Any]]) -> str:
    if not isinstance(user, dict):
        raise HTTPException(status_code=403, detail=i18n.t("workflow.error.no_owner"))
    owner = str(user.get("username") or "").strip()
    if not owner:
        raise HTTPException(status_code=403, detail=i18n.t("workflow.error.no_owner"))
    return owner


def _raise_run_error(exc: certworkflow_run.RunError) -> None:
    if exc.code == "not_found":
        raise HTTPException(
            status_code=404, detail=i18n.t("workflow.run.not_found")
        ) from exc
    if exc.code == "no_nodes":
        raise HTTPException(
            status_code=409, detail=i18n.t("workflow.run.no_nodes")
        ) from exc
    if exc.code == "status_conflict":
        raise HTTPException(
            status_code=409, detail=i18n.t("workflow.run.status_conflict")
        ) from exc
    raise HTTPException(
        status_code=400, detail=i18n.t("workflow.run.invalid_action")
    ) from exc


@router.get("")
async def list_runs(
    site_id: str = Query(default=""),
    status: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=200),
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    owner = _owner(user)
    token = status if status in certworkflow_run.RUN_STATUSES else ""
    items: List[Dict[str, Any]] = await certworkflow_run.list_runs(
        owner, site_id=site_id, status=token, limit=limit
    )
    stats = await certworkflow_run.stats_for(owner)
    return {"items": items, "count": len(items), "stats": stats}


@router.get("/{run_id}")
async def get_run(
    run_id: str,
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    run = await certworkflow_run.get_run(_owner(user), run_id)
    if run is None:
        raise HTTPException(
            status_code=404, detail=i18n.t("workflow.run.not_found")
        )
    return {"run": run}


@router.delete("/{run_id}")
async def delete_run(
    run_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    """删除一条审批实例（只允许终态）。

    待审批 / 进行中的实例还在往前走：删掉它会让正在运行的步骤失去归属，
    用户也会看到「流程凭空消失」。先把它们审批 / 取消掉再删。
    """
    owner = _owner(user)
    run = await certworkflow_run.get_run(owner, run_id)
    if run is None:
        raise HTTPException(
            status_code=404, detail=i18n.t("workflow.run.not_found")
        )
    if run.get("status") in (
        certworkflow_run.STATUS_PENDING,
        certworkflow_run.STATUS_RUNNING,
    ):
        raise HTTPException(
            status_code=409,
            detail=i18n.pick(
                "这条流程还在待审批 / 进行中，请先完成或取消后再删除",
                "This run is still pending or in progress. Finish or cancel it before deleting.",
            ),
        )
    removed = await certworkflow_run.delete_run(owner, run_id)
    if not removed:
        raise HTTPException(
            status_code=404, detail=i18n.t("workflow.run.not_found")
        )
    await security.audit(
        request,
        user,
        "cert.workflow_run.delete",
        run_id,
        "success",
        "删除审批实例 " + str(run.get("workflow_name") or ""),
    )
    return {"removed": True}


@router.post("")
async def create_run(
    payload: CreateRunPayload,
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    owner = _owner(user)
    workflow_id = str(payload.workflow_id or "").strip()
    if not workflow_id:
        raise HTTPException(
            status_code=400, detail=i18n.t("workflow.error.not_found")
        )
    workflow = await certworkflow_store.get(owner, workflow_id)
    if workflow is None:
        raise HTTPException(
            status_code=404, detail=i18n.t("workflow.error.not_found")
        )

    site_id = str(payload.site_id or "").strip()
    site_domain = ""
    if site_id:
        site = await certs.get_site(site_id, owner)
        if site is None:
            raise HTTPException(
                status_code=404, detail=i18n.t("workflow.run.site_not_found")
            )
        site_domain = str(site.get("domain") or "")

    workflow_name = str(workflow.get("name") or "")
    title = str(payload.title or "").strip()
    if not title:
        title = f"{workflow_name} · {site_domain}" if site_domain else workflow_name

    try:
        run = await certworkflow_run.create_run(
            owner,
            workflow_id,
            workflow_name,
            workflow,
            site_id=site_id,
            site_domain=site_domain,
            title=title,
            requester=owner,
            note=str(payload.note or ""),
        )
    except certworkflow_run.RunError as exc:
        _raise_run_error(exc)
        raise  # pragma: no cover - _raise_run_error 一定抛异常

    await security.audit(
        request,
        user,
        "cert.workflow.run.create",
        site_domain or workflow_name,
        "success",
        f"发起证书工作流：{title}",
    )
    return {"run": run}


@router.post("/{run_id}/transition")
async def transition_run(
    run_id: str,
    payload: TransitionPayload,
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    owner = _owner(user)
    action = str(payload.action or "").strip()
    note = str(payload.note or "")

    # 推进前**真的执行**这一步：审批通过不代表证书自己会签出来。
    # 执行失败就不推进（步骤保持 running），修好环境后再点一次即可。
    if action == certworkflow_run.ACTION_ADVANCE:
        current = await certworkflow_run.get_run(owner, run_id)
        if current is None:
            raise HTTPException(
                status_code=404, detail=i18n.t("workflow.run.not_found")
            )
        try:
            executed = await certworkflow_exec.execute_current(
                owner, current, note=note
            )
        except certs.CertError as exc:
            await security.audit(
                request,
                user,
                "cert.workflow.run.execute",
                str(current.get("site_domain") or current.get("workflow_name") or run_id),
                "failed",
                str(exc),
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if executed:
            note = (note + "\n" if note else "") + executed

    try:
        run = await certworkflow_run.transition_run(
            owner,
            run_id,
            action,
            note=note,
            operator=owner,
        )
    except certworkflow_run.RunError as exc:
        _raise_run_error(exc)
        raise  # pragma: no cover

    await security.audit(
        request,
        user,
        f"cert.workflow.run.{action or 'transition'}",
        str(run.get("site_domain") or run.get("workflow_name") or run_id),
        "success",
        f"证书工作流流转：{run.get('title') or run_id} → {run.get('status')}",
    )
    return {"run": run}
