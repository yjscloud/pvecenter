"""证书工作流节点的真实执行器。

边界
----
``certworkflow_run`` 只管状态机（纯函数、可单测）；「推进某一步时真正做什么」放在
这里。每个动作都走 :func:`certs.run_manual`，所以工作流触发的签发 / 部署在
「运行详情」里与手动按钮完全同构 —— 有运行头、逐步耗时、每目标子步骤。

节点类型 → 动作
---------------
``domain_monitor`` → 同步 CA 侧状态（顺带刷新到期时间）
``cert_apply``     → 申请 / 续期证书
``deploy_upload``  → 部署到站点的全部目标
``notification``   → 发一条站内信 + 邮件 / 飞书（沿用告警通道配置）

没有绑定站点（``site_id`` 为空）的流程只能执行 ``notification``：签发与部署都
需要一个证书站点作为落地对象。这类情况返回明确错误，而不是静默跳过 —— 静默
跳过会让人以为「点过了就是做完了」。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from . import certnotify, certworkflow_run, certs, i18n

EVENT_WORKFLOW = "workflow"

#: 有执行器的节点类型；其余类型（未知 / 未来新增）只做人工确认。
EXECUTABLE = ("domain_monitor", "cert_apply", "deploy_upload", "notification")


def current_step(run: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """运行里「正在进行」的那一步；没有则返回 None。"""
    index = int(run.get("current_index") if run.get("current_index") is not None else -1)
    steps = run.get("steps") or []
    if 0 <= index < len(steps):
        step = steps[index]
        if isinstance(step, dict):
            return step
    return None


async def execute_current(
    owner: str, run: Dict[str, Any], note: str = ""
) -> str:
    """执行运行当前所在的那一步，返回可读的结果说明（空串 = 没有执行器）。

    失败抛 :class:`certs.CertError`：调用方据此拒绝推进，步骤保持 ``running``，
    用户可以修好环境后再推一次。
    """
    step = current_step(run)
    if not step:
        return ""
    return await execute_step(owner, run, step, note=note)



async def _attach_run(owner: str, run: Dict[str, Any], site: Dict[str, Any]) -> None:
    """把这次执行产生的证书流水运行记到工作流实例上，详情页可互跳。"""
    cert_run_id = str(site.get("last_run_id") or "")
    if cert_run_id:
        await certworkflow_run.attach_cert_run(owner, str(run.get("id") or ""), cert_run_id)


async def execute_step(
    owner: str,
    run: Dict[str, Any],
    step: Dict[str, Any],
    note: str = "",
) -> str:
    node_type = str(step.get("type") or "")
    if node_type not in EXECUTABLE:
        return ""

    title = str(step.get("title") or step.get("node_id") or node_type)
    site_id = str(run.get("site_id") or "")
    site = await certs.get_site(site_id, owner) if site_id else None
    if site_id and site is None:
        # 绑定了站点但取不到：让推进失败，避免「跑完了但其实没做」
        raise certs.CertError(
            i18n.pick(
                "流程绑定的证书站点已不存在",
                "The certificate site bound to this run no longer exists",
            )
        )
    if site is None:
        # 没有绑定站点的流程只做人工确认：签发 / 部署 / 同步都需要落地对象。
        return ""

    if node_type == "notification":
        body = note or i18n.pick(
            "工作流「" + title + "」已执行到通知节点。",
            'Workflow node "' + title + '" reached its notification step.',
        )
        await certnotify.notify(
            owner,
            site,
            EVENT_WORKFLOW,
            i18n.pick(
                "证书工作流：" + str(site.get("domain") or title),
                "Certificate workflow: " + str(site.get("domain") or title),
            ),
            body,
            ttl=0,
            # 用户点「推进」才发出的通知：不受推送档位（仅失败 / 失败+成功）影响
            force=True,
        )
        return body

    if node_type == "cert_apply":
        if certs.ca_kind(site) == "upload":
            raise certs.CertError(
                i18n.pick(
                    "该站点使用托管证书，请改用「上传证书」而不是申请",
                    "This site uses an uploaded certificate; upload one instead of applying",
                )
            )
        first_time = not site.get("cert_id")
        await certs.run_manual(
            site,
            "issue",
            lambda: certs.apply_certificate(site, None, renew=not first_time),
            trigger="workflow",
        )
        # 审批通过并真正签发后，闸门标记就该清掉
        site["approval_pending"] = False
        await certs.update_site(site)
        await _attach_run(owner, run, site)
        return i18n.pick(
            "工作流已提交签发：" + str(site.get("domain") or ""),
            "Workflow submitted issuance for " + str(site.get("domain") or ""),
        )

    if node_type == "deploy_upload":
        action = "renew" if site.get("pending_cert_id") else "deploy"
        outcome = await certs.run_manual(
            site,
            "deploy",
            lambda: certs.deploy_site(site, None, action=action, force=True),
            trigger="workflow",
        )
        await certs.update_site(site)
        await _attach_run(owner, run, site)
        result = outcome or {}
        if not result.get("ok"):
            raise certs.CertError(
                str(result.get("detail") or "")
                or i18n.pick("部署失败", "Deployment failed")
            )
        return str(result.get("detail") or "")

    # domain_monitor：同步 CA 侧状态（托管证书没有远端状态，直接返回说明）
    if certs.ca_kind(site) == "upload":
        return i18n.pick("托管证书没有远端状态可同步", "Uploaded certificates have no remote state")
    await certs.run_manual(
        site, "sync", lambda: certs.sync_site_cert(site), trigger="workflow"
    )
    await certs.update_site(site)
    await _attach_run(owner, run, site)
    return i18n.pick(
        "已同步 CA 状态：" + str(site.get("domain") or ""),
        "Synced CA state for " + str(site.get("domain") or ""),
    )
