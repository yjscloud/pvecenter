"""审批闸门：需要签发时先创建一条工作流运行，等人批准。

语义（已与使用方确认）
----------------------
**只拦签发，不拦部署。** 签发会占用 CA 配额、触发证书生效变更；部署可逆且幂等，
再拦一道只会让日常续期退化成人工操作。

行为
----
* 站点绑定了 ``workflow_id``、且本轮判定为「需要签发/续期」时，调用
  :func:`ensure_gate`：
    1. 该站点已有 ``pending`` / ``running`` 的审批实例 → 不重复建单；
    2. 否则用绑定的工作流定义建一条实例（标题「自动签发 · 域名」），停在待审批；
* 批准后由操作者推进 ``cert_apply`` 节点，执行器真实签发；
* 签发完成后证书进入 ``pending_cert_id``，自动化接管部署（不再触发闸门）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from . import certworkflow_run, certworkflow_store, certs, i18n

#: 仍算「这条闸门还没走完」的运行状态
OPEN_STATUSES = (certworkflow_run.STATUS_PENDING, certworkflow_run.STATUS_RUNNING)


async def has_open(site: Dict[str, Any]) -> bool:
    """该站点是否已有未走完的审批实例。"""
    owner = certs.site_owner(site)
    if not owner:
        return False
    site_id = str(site.get("id") or "")
    for status in OPEN_STATUSES:
        runs = await certworkflow_run.list_runs(
            owner, site_id=site_id, status=status, limit=5
        )
        if runs:
            return True
    return False


async def ensure_gate(site: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """确保该站点有一条待处理的审批实例；返回新建的 run（已有则 None）。"""
    workflow_id = str(site.get("workflow_id") or "").strip()
    owner = certs.site_owner(site)
    if not workflow_id or not owner:
        return None

    site_id = str(site.get("id") or "")
    try:
        for status in OPEN_STATUSES:
            runs = await certworkflow_run.list_runs(
                owner, site_id=site_id, status=status, limit=5
            )
            if runs:
                return None
        workflow = await certworkflow_store.get(owner, workflow_id)
        if not workflow:
            # 工作流被删了：不静默放行签发，交给调用方展示原因
            return None
        title = i18n.pick("自动签发 · ", "Auto issue · ") + str(
            site.get("domain") or ""
        )
        return await certworkflow_run.create_run(
            owner,
            workflow_id,
            str(workflow.get("name") or ""),
            workflow,
            site_id=site_id,
            site_domain=str(site.get("domain") or ""),
            title=title,
            requester=owner,
            note=i18n.pick(
                "证书需要签发，自动发起审批。批准后推进「申请证书」节点即会真实签发。",
                "The certificate needs issuance; approval requested automatically. "
                "Advance the apply node after approval to actually issue it.",
            ),
        )
    except certworkflow_run.RunError:
        return None
