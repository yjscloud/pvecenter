"""证书自动化闭环：建站即签发、异步短轮询、工作流真实执行。

这些用例都不碰网络：CA 客户端、部署通道、通知与数据库写入全部打桩，只验证
「什么时候该动作、动作之后站点字段变成什么」。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import certflow, certnotify, certs, certworkflow_exec  # noqa: E402
from app.certpipeline import Context  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _site(**kw: Any) -> Dict[str, Any]:
    site = dict(certs.DEFAULT_SITE)
    site.update(
        {
            "id": "site-auto",
            "name": "自动化",
            "domain": "auto.example.com",
            "username": "admin",
            "ca": "tencent",
        }
    )
    site.update(kw)
    return site


# ------------------------------------------------------------------ 建站即签发
def test_new_site_enters_pipeline_and_is_due_for_issue() -> None:
    site = _site(auto_renew=True)
    ctx = Context(site=site)
    assert certs._has_cert(ctx) is True
    assert certs._decide(ctx) == "due"


def test_upload_site_without_cert_stays_out_of_pipeline() -> None:
    site = _site(ca="upload", auto_renew=True)
    ctx = Context(site=site)
    assert certs._has_cert(ctx) is False


def test_new_site_with_auto_renew_off_gets_notice_only() -> None:
    site = _site(auto_renew=False)
    ctx = Context(site=site)
    assert certs._decide(ctx) == "notice"


def test_auto_issue_runs_pipeline_for_empty_site(monkeypatch) -> None:
    calls: List[str] = []

    async def fake_get_site(site_id, owner=None):
        return _site(id=site_id)

    async def fake_renew_site(site, trigger="tick"):
        calls.append(trigger)
        return SimpleNamespace(status="ok", value=None, exc=None)

    monkeypatch.setattr(certs, "get_site", fake_get_site)
    monkeypatch.setattr(certs, "renew_site", fake_renew_site)

    _run(certs.auto_issue("site-auto", "admin"))
    assert calls == ["create"]


def test_auto_issue_skips_site_with_cert(monkeypatch) -> None:
    calls: List[str] = []

    async def fake_get_site(site_id, owner=None):
        return _site(cert_id="c1")

    async def fake_renew_site(site, trigger="tick"):
        calls.append(trigger)

    monkeypatch.setattr(certs, "get_site", fake_get_site)
    monkeypatch.setattr(certs, "renew_site", fake_renew_site)

    _run(certs.auto_issue("site-auto", "admin"))
    assert calls == []


# ------------------------------------------------------ 异步签发排短轮询
def test_apply_schedules_short_poll_for_async_ca(monkeypatch) -> None:
    class _Provider:
        configured = True

        async def apply(self, domains, renew_from="", alias=""):
            return SimpleNamespace(
                cert_id="new-1", status=0, expire_at=0, pem="", key=""
            )

    async def fake_update(site) -> None:
        return None

    async def fake_record(*a, **kw) -> None:
        return None

    async def fake_notify(*a, **kw) -> bool:
        return True

    monkeypatch.setattr(certs, "build_provider", lambda kind, cfg: _Provider())
    monkeypatch.setattr(certs, "update_site", fake_update)
    monkeypatch.setattr(certs, "record_log", fake_record)
    monkeypatch.setattr(certs.certnotify, "notify", fake_notify)

    site = _site()
    before = certs.now_ts()
    _run(certs.apply_certificate(site, {"secret_id": "x"}, renew=False))

    assert site["pending_cert_id"] == "new-1"
    assert site["wait_started_at"] >= before
    # 5 分钟一轮的补跑作业据此提前接手，签出后立刻部署
    assert site["next_attempt_at"] >= before + certs.CA_POLL_SECONDS


# ------------------------------------------------------------ 工作流执行器
@pytest.mark.parametrize(
    ("node_type", "expected_step"),
    [
        ("cert_apply", "issue"),
        ("deploy_upload", "deploy"),
        ("domain_monitor", "sync"),
    ],
)
def test_workflow_node_executes_real_action(monkeypatch, node_type, expected_step) -> None:
    calls: List[str] = []

    async def fake_get_site(site_id, owner=None):
        return _site(id=site_id, cert_id="c1")

    async def fake_run_manual(site, node_id, job, trigger="tick"):
        calls.append(node_id)
        return {"ok": True, "detail": "done"}

    async def fake_update(site) -> None:
        return None

    monkeypatch.setattr(certs, "get_site", fake_get_site)
    monkeypatch.setattr(certs, "run_manual", fake_run_manual)
    monkeypatch.setattr(certs, "update_site", fake_update)

    run = {"site_id": "site-auto", "current_index": 0}
    step = {"node_id": "n1", "type": node_type, "title": "步骤"}
    out = _run(certworkflow_exec.execute_step("admin", run, step))
    assert calls == [expected_step]
    assert out


def test_workflow_deploy_failure_raises(monkeypatch) -> None:
    async def fake_get_site(site_id, owner=None):
        return _site(id=site_id, cert_id="c1")

    async def fake_run_manual(site, node_id, job, trigger="tick"):
        return {"ok": False, "detail": "目标不可达"}

    async def fake_update(site) -> None:
        return None

    monkeypatch.setattr(certs, "get_site", fake_get_site)
    monkeypatch.setattr(certs, "run_manual", fake_run_manual)
    monkeypatch.setattr(certs, "update_site", fake_update)

    run = {"site_id": "site-auto", "current_index": 0}
    step = {"node_id": "n2", "type": "deploy_upload", "title": "部署"}
    with pytest.raises(certs.CertError):
        _run(certworkflow_exec.execute_step("admin", run, step))


def test_workflow_without_site_only_confirms(monkeypatch) -> None:
    """没有绑定站点的流程只做人工确认，不执行任何证书动作。"""
    run = {"site_id": "", "current_index": 0}
    step = {"node_id": "n3", "type": "cert_apply", "title": "签发"}
    assert _run(certworkflow_exec.execute_step("admin", run, step)) == ""


def test_workflow_stale_site_reference_raises(monkeypatch) -> None:
    async def fake_get_site(site_id, owner=None):
        return None

    monkeypatch.setattr(certs, "get_site", fake_get_site)

    run = {"site_id": "gone", "current_index": 0}
    step = {"node_id": "n6", "type": "cert_apply", "title": "签发"}
    with pytest.raises(certs.CertError):
        _run(certworkflow_exec.execute_step("admin", run, step))


def test_workflow_notification_uses_alert_channels(monkeypatch) -> None:
    sent: List[str] = []

    async def fake_get_site(site_id, owner=None):
        return _site(id=site_id)

    async def fake_notify(owner, site, event, title, detail="", **kw):
        sent.append(event)
        return True

    monkeypatch.setattr(certs, "get_site", fake_get_site)
    monkeypatch.setattr(certs.certnotify, "notify", fake_notify)

    run = {"site_id": "site-auto", "current_index": 0}
    step = {"node_id": "n4", "type": "notification", "title": "通知"}
    out = _run(certworkflow_exec.execute_step("admin", run, step))
    assert sent == [certworkflow_exec.EVENT_WORKFLOW]
    assert out


def test_unknown_node_type_is_not_executed() -> None:
    run = {"site_id": "site-auto", "current_index": 0}
    step = {"node_id": "n5", "type": "manual_check", "title": "人工确认"}
    assert _run(certworkflow_exec.execute_step("admin", run, step)) == ""


# --------------------------------------------------- 部署幂等 / 线上生效校验
_PEM = "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----"
_KEY = "-----BEGIN PRIVATE KEY-----\nK\n-----END PRIVATE KEY-----"
# deploy_site 用 bundle["fullchain"]（pem.strip() + "\n"）算指纹
_CERT_FP = hashlib.sha256((_PEM.strip() + "\n").encode("utf-8")).hexdigest()[:16]


def _upload_site(**kw: Any) -> Dict[str, Any]:
    return _site(ca="upload", cert_id="c1", acme_pem=_PEM, acme_key=_KEY, **kw)


def _deploy_stubs(monkeypatch):
    pushed: List[str] = []
    notified: List[str] = []

    async def fake_push(view, bundle):
        pushed.append(str(view.get("deploy_dir")))
        return ("写入 2 个文件", "")

    async def fake_update(site) -> None:
        return None

    async def fake_notify(owner, site, event, *a, **kw):
        notified.append(event)
        return True

    async def fake_record(*a, **kw) -> None:
        return None

    monkeypatch.setattr(certs, "_push_target", fake_push)
    monkeypatch.setattr(certs, "update_site", fake_update)
    monkeypatch.setattr(certs, "record_log", fake_record)
    monkeypatch.setattr(certs.certnotify, "notify", fake_notify)
    return pushed, notified


def test_deploy_skips_unchanged_target_unless_forced(monkeypatch) -> None:
    pushed, _ = _deploy_stubs(monkeypatch)
    site = _upload_site(
        targets=[
            {
                "id": "primary",
                "method": "local",
                "deploy_dir": "/tmp/same",
                "deployed_fingerprint": _CERT_FP,
            },
            {"id": "t2", "method": "local", "deploy_dir": "/tmp/new"},
        ]
    )

    outcome = _run(certs.deploy_site(site, None, action="renew"))
    assert outcome["ok"] is True
    assert pushed == ["/tmp/new"]  # 指纹一致的被跳过
    skipped = next(item for item in outcome["targets"] if item["id"] == "primary")
    assert skipped["written"] is False

    pushed.clear()
    _run(certs.deploy_site(site, None, action="deploy", force=True))
    assert pushed == ["/tmp/same", "/tmp/new"]  # 用户显式部署必定真的写一次


def test_deploy_verifies_live_fingerprint(monkeypatch) -> None:
    _, notified = _deploy_stubs(monkeypatch)
    from app import certmonitor

    monkeypatch.setattr(certs, "_leaf_fingerprint", lambda bundle: "aaaa")
    monkeypatch.setattr(
        certmonitor,
        "inspect_tls",
        lambda host, port=443: {"fingerprint": "bbbb", "detail": ""},
    )

    site = _upload_site(
        monitor_url="https://example.com",
        targets=[{"id": "primary", "method": "local", "deploy_dir": "/tmp/a"}],
    )
    _run(certs.deploy_site(site, None, action="deploy", force=True))

    assert site["tls_verify"]["ok"] is False
    assert certnotify.EVENT_VERIFY_MISMATCH in notified

    # 线上指纹与部署一致时不再报警
    monkeypatch.setattr(
        certmonitor,
        "inspect_tls",
        lambda host, port=443: {"fingerprint": "aaaa", "detail": ""},
    )
    notified.clear()
    site2 = _upload_site(
        monitor_url="https://example.com",
        targets=[{"id": "primary", "method": "local", "deploy_dir": "/tmp/a"}],
    )
    _run(certs.deploy_site(site2, None, action="deploy", force=True))
    assert site2["tls_verify"]["ok"] is True
    assert certnotify.EVENT_VERIFY_MISMATCH not in notified


def test_verify_skipped_without_monitor_url(monkeypatch) -> None:
    _, notified = _deploy_stubs(monkeypatch)
    site = _upload_site(
        targets=[{"id": "primary", "method": "local", "deploy_dir": "/tmp/a"}]
    )
    _run(certs.deploy_site(site, None, action="deploy", force=True))
    assert site.get("tls_verify") == {}
    assert certnotify.EVENT_VERIFY_MISMATCH not in notified


# ---------------------------------------------------- 通知投递结果进步骤日志
def test_notify_logs_delivery_channels(monkeypatch) -> None:
    logs: List[str] = []

    async def fake_emit(text, *, level="info", node_id=""):
        logs.append(text)

    async def fake_push(*a, **kw):
        return None

    async def fake_history(*a, **kw):
        return None

    from app import alerting

    async def mail_ok(owner, title, detail):
        return False, "smtp down"

    async def hook_ok(title, detail, extra, owner, cfg=None):
        return True, ""

    async def feishu_ok(card, owner):
        return False, "bad webhook"

    async def load_webhook_ok(_owner):
        # 只有「已启用且填了地址」的渠道才会真的去投递
        return {"enabled": True, "webhook": "https://example.com/hook"}

    monkeypatch.setattr(certflow, "emit_log", fake_emit)
    monkeypatch.setattr(certnotify.notifications, "push", fake_push)
    monkeypatch.setattr(certnotify, "_record_history", fake_history)
    monkeypatch.setattr(alerting, "send_alert_email", mail_ok)
    monkeypatch.setattr(alerting, "send_webhook", hook_ok)
    monkeypatch.setattr(alerting, "send_feishu", feishu_ok)
    monkeypatch.setattr(alerting, "load_webhook", load_webhook_ok)

    ok = _run(certnotify.notify("admin", _site(), "deploy_ok", "标题", "正文", ttl=0))

    assert ok is True
    assert logs, "投递结果必须写进步骤日志"
    assert "inbox" in logs[0] and "webhook" in logs[0]
    assert "email" in logs[0] and "feishu" in logs[0]


# ---------------------------------------------------------- SSE 步骤签名
def test_steps_signature_detects_status_change() -> None:
    from app.routers.certs import _steps_signature

    running = _steps_signature(
        [{"node_id": "issue", "status": "running", "ms": 0, "attempt": 0, "next_at": 0}]
    )
    failed = _steps_signature(
        [{"node_id": "issue", "status": "failed", "ms": 120, "attempt": 1, "next_at": 0}]
    )
    waiting = _steps_signature(
        [{"node_id": "wait-ca", "status": "waiting", "ms": 0, "attempt": 1, "next_at": 100}]
    )
    assert running != failed != waiting
    assert running == _steps_signature(
        [{"node_id": "issue", "status": "running", "ms": 0, "attempt": 0, "next_at": 0}]
    )


# ---------------------------------------------------------------- 审批闸门
def test_site_with_workflow_routes_to_gate() -> None:
    site = _site(workflow_id="wf-1", auto_renew=True)
    ctx = Context(site=site)
    assert certs._decide(ctx) == "gate"
    # 没绑定工作流的仍然直接签发
    assert certs._decide(Context(site=_site())) == "due"


def test_gate_creates_approval_and_marks_pending(monkeypatch) -> None:
    from app import certworkflow_gate

    async def fake_ensure(site):
        return {"id": "run-1", "title": "自动签发 · auto.example.com"}

    async def fake_notify(*a, **kw):
        return True

    monkeypatch.setattr(certworkflow_gate, "ensure_gate", fake_ensure)
    monkeypatch.setattr(certs.certnotify, "notify", fake_notify)

    site = _site(workflow_id="wf-1")
    ctx = Context(site=site)
    detail = _run(certs._step_gate(ctx))

    assert site["approval_pending"] is True
    assert ctx.get("result")["action"] == "gate"
    assert "自动签发" in detail


def test_gate_rejected_backs_off_one_hour(monkeypatch) -> None:
    from app import certworkflow_gate

    async def fake_ensure(site):
        return None

    async def fake_open(site):
        return False

    monkeypatch.setattr(certworkflow_gate, "ensure_gate", fake_ensure)
    monkeypatch.setattr(certworkflow_gate, "has_open", fake_open)

    site = _site(workflow_id="wf-1")
    _run(certs._step_gate(Context(site=site)))

    assert site["approval_pending"] is False
    assert site["next_attempt_at"] >= certs.now_ts() + 3500


# ------------------------------------------------------------ 同步分级
def test_sync_interval_by_days_left() -> None:
    assert certs._sync_interval(None) == 12 * 3600
    assert certs._sync_interval(60) == 12 * 3600
    assert certs._sync_interval(20) == 3600
    assert certs._sync_interval(3) == 900


def test_prepare_schedules_sync_and_honours_window(monkeypatch) -> None:
    site = _site(ca="upload")
    ctx = Context(site=site)
    _run(certs._step_prepare(ctx))
    assert ctx.get("sync_due") is True
    assert site["next_sync_at"] > certs.now_ts()

    # 窗口内不再同步：到期时间沿用上次结论
    site2 = _site(ca="upload", next_sync_at=certs.now_ts() + 3600)
    ctx2 = Context(site=site2)
    _run(certs._step_prepare(ctx2))
    assert ctx2.get("sync_due") is False

    # 等待签发的站点必须每轮都查
    site3 = _site(ca="upload", pending_cert_id="p1", next_sync_at=certs.now_ts() + 3600)
    ctx3 = Context(site=site3)
    _run(certs._step_prepare(ctx3))
    assert ctx3.get("sync_due") is True


# ------------------------------------------------------------ 生产目标确认
def test_prod_target_waits_without_confirmation(monkeypatch) -> None:
    pushed, notified = _deploy_stubs(monkeypatch)
    site = _upload_site(
        prod_auto_deploy=False,
        targets=[
            {"id": "pve1", "method": "pve", "agent_node": "pve"},
            {"id": "local1", "method": "local", "deploy_dir": "/tmp/a"},
        ],
    )

    outcome = _run(certs.deploy_site(site, None, action="renew"))
    assert outcome["ok"] is False
    assert pushed == ["/tmp/a"]  # 生产目标没写，本地目标照常
    assert site["deploy_blocked"] is True
    assert site["next_attempt_at"] == 0  # 不安排自动重试
    assert certnotify.EVENT_DEPLOY_HELD in notified

    pushed.clear()
    _run(certs.deploy_site(site, None, action="deploy", force=True))
    assert "/tmp/a" in pushed  # 人工部署会强制写


def test_prod_target_allowed_by_default(monkeypatch) -> None:
    pushed, notified = _deploy_stubs(monkeypatch)
    site = _upload_site(
        targets=[{"id": "pve1", "method": "pve", "agent_node": "pve"}]
    )
    outcome = _run(certs.deploy_site(site, None, action="renew"))
    assert outcome["ok"] is True
    assert site["deploy_blocked"] is False
    assert certnotify.EVENT_DEPLOY_HELD not in notified


# ------------------------------------------------------------ 视图派生字段
def test_stage_derivation() -> None:
    from app.routers.certs import _stage_of

    cfg = {"renew_before_days": 15}
    now = certs.now_ts()

    stage, action, _ = _stage_of(
        _site(cert_id="c1", expire_at=now + 60 * 86400), cfg
    )
    assert (stage, action) == ("valid", "sync")

    stage, action, _ = _stage_of(
        _site(cert_id="c1", expire_at=now + 5 * 86400), cfg
    )
    assert (stage, action) == ("expiring", "renew")

    stage, action, _ = _stage_of(_site(), cfg)
    assert (stage, action) == ("empty", "issue")

    stage, action, _ = _stage_of(_site(pending_cert_id="p1", cert_status=0), cfg)
    assert (stage, action) == ("issuing", "wait_ca")

    stage, action, _ = _stage_of(_site(approval_pending=True), cfg)
    assert (stage, action) == ("waiting_approval", "approve")

    stage, action, _ = _stage_of(_site(deploy_blocked=True), cfg)
    assert (stage, action) == ("waiting_confirm", "confirm")

    stage, action, _ = _stage_of(_site(cert_id="c1", cert_status=10), cfg)
    assert (stage, action) == ("revoked", "none")


# ------------------------------------------------- 工作流运行关联证书流水
def test_workflow_execution_attaches_cert_run(monkeypatch) -> None:
    from app import certworkflow_run

    attached: List[tuple] = []

    async def fake_get_site(site_id, owner=None):
        return _site(id=site_id, cert_id="c1")

    async def fake_run_manual(site, node_id, job, trigger="tick"):
        site["last_run_id"] = "run-xyz"
        return {"ok": True, "detail": "done"}

    async def fake_update(site) -> None:
        return None

    async def fake_attach(owner, run_id, cert_run_id):
        attached.append((owner, run_id, cert_run_id))
        return True

    monkeypatch.setattr(certs, "get_site", fake_get_site)
    monkeypatch.setattr(certs, "run_manual", fake_run_manual)
    monkeypatch.setattr(certs, "update_site", fake_update)
    monkeypatch.setattr(certworkflow_run, "attach_cert_run", fake_attach)

    run = {"id": "wf-run-1", "site_id": "site-auto", "current_index": 0}
    step = {"node_id": "n1", "type": "cert_apply", "title": "签发"}
    _run(certworkflow_exec.execute_step("admin", run, step))

    assert attached == [("admin", "wf-run-1", "run-xyz")]


# ------------------------------------------------------ 单飞租约 / 待办队列
def test_busy_lease_skips_run(monkeypatch) -> None:
    async def fake_acquire(site_id, holder, ttl=600):
        return False

    async def fake_release(site_id, holder) -> None:
        return None

    async def fake_renew(site, trigger="tick"):
        raise AssertionError("抢不到租约时不该跑流水线")

    monkeypatch.setattr(certs, "acquire_lease", fake_acquire)
    monkeypatch.setattr(certs, "release_lease", fake_release)
    monkeypatch.setattr(certs, "renew_site", fake_renew)

    run = _run(certs._renew_with_lease(_site(id="busy-1"), "tick"))
    assert run.error == "busy"


def test_deploy_failure_enqueues_retry_job(monkeypatch) -> None:
    pushed, _ = _deploy_stubs(monkeypatch)
    jobs: List[tuple] = []

    async def fake_enqueue(site_id, action="tick", not_before=0):
        jobs.append((site_id, action, int(not_before)))
        return True

    monkeypatch.setattr(certs, "enqueue_job", fake_enqueue)
    site = _upload_site(
        id="site-job",
        targets=[{"id": "t1", "method": "local", "deploy_dir": "/tmp/bad"}],
    )

    async def fake_push(view, bundle):
        raise certs.CertError("目标不可达")

    monkeypatch.setattr(certs, "_push_target", fake_push)
    _run(certs.deploy_site(site, None, action="renew"))

    assert jobs and jobs[0][1] == "deploy"
    assert jobs[0][2] > certs.now_ts()
    target = next(t for t in site["targets"] if t["id"] == "t1")
    assert target["next_retry_at"] > certs.now_ts()


def test_target_in_backoff_is_deferred_not_failed(monkeypatch) -> None:
    pushed, notified = _deploy_stubs(monkeypatch)
    jobs: List[tuple] = []

    async def fake_enqueue(site_id, action="tick", not_before=0):
        jobs.append((site_id, action, int(not_before)))
        return True

    monkeypatch.setattr(certs, "enqueue_job", fake_enqueue)
    later = certs.now_ts() + 600
    site = _upload_site(
        id="site-defer",
        targets=[
            {
                "id": "t1",
                "method": "local",
                "deploy_dir": "/tmp/a",
                "next_retry_at": later,
                "deploy_attempts": 1,
                "last_error": "上次失败",
            }
        ],
    )

    outcome = _run(certs.deploy_site(site, None, action="renew"))
    assert outcome["ok"] is False
    assert pushed == []  # 退避窗口内不写
    assert jobs and jobs[0][2] == later
    assert "退避" in str(site.get("last_error") or "")
    assert certnotify.EVENT_DEPLOY_FAILED not in notified  # 不是新故障，不重复喊


def test_notify_mode_persists_to_settings(monkeypatch) -> None:
    from app import store as app_store

    box: Dict[str, str] = {}

    async def fake_get(key: str):
        return box.get(key)

    async def fake_set(key: str, value: str):
        box[key] = value

    monkeypatch.setattr(app_store, "get_setting", fake_get)
    monkeypatch.setattr(app_store, "set_setting", fake_set)
    certnotify._MODE_CACHE.update({"at": 0, "mode": ""})

    assert _run(certnotify.load_mode()) == "failures_success"  # 默认档
    assert _run(certnotify.save_mode("all")) == "all"
    assert box[certnotify.MODE_KEY] == "all"
    certnotify._MODE_CACHE.update({"at": 0, "mode": ""})
    assert _run(certnotify.load_mode()) == "all"

    with pytest.raises(ValueError):
        _run(certnotify.save_mode("everything"))
