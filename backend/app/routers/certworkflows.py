"""证书工作流接口：列表、模板、导入、编辑与导出。

权限沿用证书模块：读要 ``cert.view``，改要 ``cert.manage``。工作流是用户配置，
按用户名隔离（管理员也不跨用户读写 —— 导入进来的私钥类参数不应因为「你是管理员」
就出现在别人的列表里）。

导入失败返回 **200 + ok=false + errors[]**，而不是 400：
一次导入往往同时有几处问题（某行 YAML 缩进错、某个节点缺必填参数），只回一句
``detail`` 会让用户改一处、再传一次、再看到下一处。结构化错误列表可以一次展示
全部问题，也便于前端把 ``path`` 标到对应节点上。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from .. import certs, certworkflow, certworkflow_run, certworkflow_store, i18n, security

router = APIRouter(prefix="/api/cert-workflows", tags=["cert-workflows"])

CERT_VIEW = security.require_permission("cert.view")
CERT_MANAGE = security.require_permission("cert.manage")

_ORIGINS = set(certworkflow.ORIGINS)
_SAFE_FILENAME = re.compile(r"[^\w\u4e00-\u9fa5.-]+")


class ImportPayload(BaseModel):
    filename: str = ""
    content: str = ""


def _owner(user: Optional[Dict[str, Any]]) -> str:
    if not isinstance(user, dict):
        raise HTTPException(status_code=403, detail=i18n.t("workflow.error.no_owner"))
    owner = str(user.get("username") or "").strip()
    if not owner:
        raise HTTPException(status_code=403, detail=i18n.t("workflow.error.no_owner"))
    return owner


def _definition_input(payload: Dict[str, Any]) -> Dict[str, Any]:
    """只把定义字段交给校验器；``origin`` 之类的接口参数不参与 unknown 警告。"""
    return {
        "name": payload.get("name"),
        "description": payload.get("description"),
        "nodes": payload.get("nodes"),
    }


def _normalize(payload: Dict[str, Any], *, allow_empty: bool = True):
    return certworkflow.normalize(
        _definition_input(payload),
        allow_empty=allow_empty,
        require_name=True,
    )


def _first_error(errors: List[Dict[str, Any]]) -> str:
    if errors:
        return str(errors[0].get("message") or "")
    return i18n.t("workflow.error.invalid")


def _origin(value: Any) -> str:
    token = str(value or "").strip()
    return token if token in _ORIGINS else certworkflow.ORIGIN_EDITOR


@router.get("")
async def list_workflows(
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    items = await certworkflow_store.list_for(_owner(user))
    return {"items": items, "count": len(items)}


@router.get("/templates")
async def list_templates(
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    """内置模板。名称与描述同时给词条键和当前语言文本，前端两种用法都支持。"""
    items = []
    for item in certworkflow.templates():
        view = dict(item)
        view["name"] = i18n.t(str(item.get("name_key") or ""))
        view["description"] = i18n.t(str(item.get("description_key") or ""))
        items.append(view)
    return {"templates": items}


@router.get("/node-types")
async def list_node_types(
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    """节点类型目录：前端据此渲染节点面板与参数表单。"""
    return {
        "types": certworkflow.node_types(),
        "max_nodes": certworkflow.MAX_NODES,
    }


@router.post("/validate")
async def validate_definition(
    payload: Dict[str, Any] = Body(...),
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    definition, errors, warnings = _normalize(payload, allow_empty=True)
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "definition": definition,
    }


@router.post("/import")
async def import_workflow(
    payload: ImportPayload,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    owner = _owner(user)
    raw, errors, fmt = certworkflow.parse_document(payload.content, payload.filename)
    if errors:
        return {"ok": False, "errors": errors, "warnings": [], "format": fmt}

    definition, errors, warnings = certworkflow.normalize(
        raw,
        filename=payload.filename,
        allow_empty=False,
        require_name=True,
    )
    if errors or definition is None:
        return {
            "ok": False,
            "errors": errors,
            "warnings": warnings,
            "format": fmt,
        }

    existing = await certworkflow_store.names(owner)
    name = certworkflow.unique_name(existing, definition["name"])
    renamed_from = ""
    if name != definition["name"]:
        renamed_from = definition["name"]
        warnings.append(
            {
                "message": i18n.t(
                    "workflow.warning.renamed", old=renamed_from, name=name
                ),
                "path": "name",
            }
        )
        definition["name"] = name

    workflow = await certworkflow_store.create(
        owner, definition, certworkflow.ORIGIN_IMPORT
    )
    return {
        "ok": True,
        "workflow": workflow,
        "warnings": warnings,
        "errors": [],
        "format": fmt,
        "renamed_from": renamed_from,
    }


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    workflow = await certworkflow_store.get(_owner(user), workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail=i18n.t("workflow.error.not_found"))
    return {"workflow": workflow}


@router.post("")
async def create_workflow(
    payload: Dict[str, Any] = Body(...),
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    owner = _owner(user)
    definition, errors, _warnings = _normalize(payload, allow_empty=True)
    if errors or definition is None:
        raise HTTPException(status_code=400, detail=_first_error(errors))

    origin = _origin(payload.get("origin"))
    if origin in (certworkflow.ORIGIN_TEMPLATE, certworkflow.ORIGIN_IMPORT):
        # 模板可被反复使用、导入文件也可能重名：自动加后缀比让用户先改名更顺手。
        name = certworkflow.unique_name(
            await certworkflow_store.names(owner), definition["name"]
        )
        definition["name"] = name
    elif await certworkflow_store.name_exists(owner, definition["name"]):
        raise HTTPException(
            status_code=409,
            detail=i18n.t("workflow.error.name_exists", name=definition["name"]),
        )

    workflow = await certworkflow_store.create(owner, definition, origin)
    return {"workflow": workflow}


@router.put("/{workflow_id}")
async def update_workflow(
    workflow_id: str,
    payload: Dict[str, Any] = Body(...),
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    owner = _owner(user)
    definition, errors, _warnings = _normalize(payload, allow_empty=True)
    if errors or definition is None:
        raise HTTPException(status_code=400, detail=_first_error(errors))

    current = await certworkflow_store.get(owner, workflow_id)
    if current is None:
        raise HTTPException(status_code=404, detail=i18n.t("workflow.error.not_found"))
    if await certworkflow_store.name_exists(owner, definition["name"], workflow_id):
        raise HTTPException(
            status_code=409,
            detail=i18n.t("workflow.error.name_exists", name=definition["name"]),
        )

    workflow = await certworkflow_store.update(owner, workflow_id, definition)
    if workflow is None:  # 并发删除
        raise HTTPException(status_code=404, detail=i18n.t("workflow.error.not_found"))
    return {"workflow": workflow}


@router.delete("/{workflow_id}")
async def delete_workflow(
    workflow_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    """删除工作流定义，并妥善处理它的引用。

    * 还有待审批 / 进行中的实例时**拒绝**（409）：直接删会让这些实例永远卡在
      半路，用户看到的是「审批卡住」而不是「工作流没了」。先把它们推进或取消；
    * 删掉定义后，解绑引用了它的站点（否则证书页仍显示「已绑定」），并清理
      它的历史审批实例（``workflow_name`` 是快照，留着也点不开）。
    """
    owner = _owner(user)
    active = await certworkflow_run.count_active(owner, workflow_id)
    if active:
        raise HTTPException(
            status_code=409,
            detail=i18n.pick(
                f"该工作流还有 {active} 个待审批 / 进行中的实例，"
                "请先处理或取消这些实例，再删除工作流",
                f"This workflow still has {active} pending or running run(s). "
                "Finish or cancel them before deleting the workflow.",
            ),
        )
    removed = await certworkflow_store.delete(owner, workflow_id)
    if not removed:
        raise HTTPException(status_code=404, detail=i18n.t("workflow.error.not_found"))
    detached = await certs.detach_workflow(owner, workflow_id)
    runs = await certworkflow_run.delete_for_workflow(owner, workflow_id)
    await security.audit(
        request,
        user,
        "cert.workflow.delete",
        workflow_id,
        "success",
        f"删除证书工作流（解绑站点 {detached}，清理实例 {runs}）",
    )
    return {"removed": True, "detached_sites": detached, "removed_runs": runs}


@router.get("/{workflow_id}/export")
async def export_workflow(
    workflow_id: str,
    format: str = Query(default="yaml"),
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Response:
    workflow = await certworkflow_store.get(_owner(user), workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail=i18n.t("workflow.error.not_found"))

    fmt = "yaml" if str(format).lower() in {"yaml", "yml"} else "json"
    text = certworkflow.dump_document(workflow, fmt)
    name = _SAFE_FILENAME.sub("-", workflow.get("name") or "workflow").strip("-")
    filename = f"{name or 'workflow'}.{fmt}"
    media = "application/x-yaml; charset=utf-8" if fmt == "yaml" else "application/json; charset=utf-8"
    return Response(
        content=text,
        media_type=media,
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"workflow.{fmt}\"; "
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )
