"""部署日志与流水线的集成：归因、粒度、读取。

三组断言：

1. **归因**——``run_id`` / ``node_id`` 从哪来（显式 > 流水线上下文 > 空串），
   以及是否真的写进了 ``cert_deploy_log`` 的两列。
2. **粒度**——部署阶段按目标逐个记录；签发 / 清理这类深层调用不用改签名也能
   自动挂到所在步骤。
3. **读取**——按步骤分组、游标增量，以及「没有运行头」（旧路径）时仍然可用。

全部纯逻辑：数据库换成假连接，CA / 部署目标全部打桩。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import certflow, certnotify, certs  # noqa: E402
from app.certpipeline import STEP_FAILED, STEP_OK, STEP_SKIPPED, Context, Pipeline, Step  # noqa: E402
from app.tencent_ssl import STATUS_ISSUED  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _site(**kw: Any) -> Dict[str, Any]:
    site = dict(certs.DEFAULT_SITE)
    site.update(
        {
            "id": "site-1",
            "name": "测试站点",
            "domain": "a.example.com",
            "username": "admin",
            "deploy_dir": "/tmp/certs",
        }
    )
    site.update(kw)
    return site


# ---------------------------------------------------------------------- 假数据库
class _FakeCursor:
    async def fetchone(self) -> None:
        return None

    async def fetchall(self) -> List[Any]:
        return []

    async def close(self) -> None:
        return None


class _FakeConn:
    def __init__(self, sink: List[Dict[str, Any]]) -> None:
        self.sink = sink

    async def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        self.sink.append({"sql": sql, "params": params})
        return _FakeCursor()

    async def commit(self) -> None:
        return None


def _patch_db(monkeypatch, sink: List[Dict[str, Any]]) -> None:
    @contextlib.asynccontextmanager
    async def fake_connect():
        yield _FakeConn(sink)

    monkeypatch.setattr(certs.database, "connect", fake_connect)


# ------------------------------------------------------------------------ 归因
def test_attribution_prefers_explicit_arguments() -> None:
    with certflow.step_scope("run_ctx"):
        certflow.enter_step("sync")
        # 显式传入优先：收尾清理这类动作可能想把日志记到「部署」而不是当前步
        assert certs.log_attribution("run_explicit", "deploy") == ("run_explicit", "deploy")
        # 空串是「明确的没有」，不会被上下文覆盖
        assert certs.log_attribution("", "") == ("", "")


def test_attribution_reads_pipeline_context() -> None:
    with certflow.step_scope("run_abc"):
        assert certs.log_attribution() == ("run_abc", "")
        certflow.enter_step("issue")
        assert certs.log_attribution() == ("run_abc", "issue")


def test_attribution_is_empty_outside_pipeline() -> None:
    certflow.enter_step("")  # 清掉可能残留的步骤
    assert certs.log_attribution() == ("", "")


def test_step_scope_does_not_leak_after_exit() -> None:
    with certflow.step_scope("run_1"):
        certflow.enter_step("deploy")
        assert certflow.current_run_id() == "run_1"
    # 调度器的下一轮 tick 复用同一个 task：退出后必须彻底复位
    assert certflow.current_run_id() == ""
    assert certflow.current_node_id() == ""


def test_record_log_persists_run_and_node(monkeypatch) -> None:
    sink: List[Dict[str, Any]] = []
    _patch_db(monkeypatch, sink)

    with certflow.step_scope("run_persist"):
        certflow.enter_step("deploy")
        _run(certs.record_log(_site(), "renew", "success", "已部署"))

    written = sink[0]["params"]
    assert written[4] == "renew"
    assert written[8] == "run_persist"  # run_id
    assert written[9] == "deploy"  # node_id


def test_record_log_without_context_writes_empty_attribution(monkeypatch) -> None:
    sink: List[Dict[str, Any]] = []
    _patch_db(monkeypatch, sink)

    _run(certs.record_log(_site(), "bind", "success", "手工绑定"))

    written = sink[0]["params"]
    assert written[8] == ""
    assert written[9] == ""


# --------------------------------------------------------------------- 步进钩子
def test_legacy_pipeline_reports_every_step_including_skipped() -> None:
    seen: List[str] = []

    async def ok(ctx: Context) -> None:
        return None

    async def boom(ctx: Context) -> None:
        raise RuntimeError("炸了")

    pipe = Pipeline(
        "demo",
        [
            Step("first", ok),
            Step("skip", ok, when=lambda ctx: False),
            Step("boom", boom),
            Step("never", ok),
        ],
    )
    _run(pipe.run(Context(site={}), on_step=lambda name, index: seen.append(f"{index}:{name}")))

    # 跳过的步骤也要回调 —— 否则「当前步骤」会停在 first，跳过期间的日志挂错地方
    assert seen == ["0:first", "1:skip", "2:boom"]


def test_runner_sets_step_context_inside_node() -> None:
    observed: List[str] = []

    async def handler(ctx: certflow.RunContext) -> None:
        observed.append(certflow.current_node_id())

    reg = certflow.NodeRegistry()
    reg.register(certflow.NodeSpec("probe", handler))
    definition = certflow.Definition(
        "demo", [certflow.Node("a", "probe"), certflow.Node("b", "probe", needs=("a",))]
    )

    _run(certflow.Runner(reg).run(definition, {}))

    assert observed == ["a", "b"]
    assert certflow.current_node_id() == ""  # 运行结束复位


def test_runner_context_is_inherited_by_child_task() -> None:
    """节点内部起的子任务要能读到归因：并发部署多目标靠的就是这个语义。"""
    seen: List[str] = []

    async def handler(ctx: certflow.RunContext) -> None:
        async def child() -> None:
            seen.append(certflow.current_run_id() + "/" + certflow.current_node_id())

        await asyncio.create_task(child())

    reg = certflow.NodeRegistry()
    reg.register(certflow.NodeSpec("probe", handler))
    definition = certflow.Definition("demo", [certflow.Node("deploy", "probe")])

    run = _run(certflow.Runner(reg).run(definition, {}))

    assert seen == [run.id + "/deploy"]


# ------------------------------------------------------------------------ 粒度
def test_deploy_logs_each_target_then_summary(monkeypatch) -> None:
    logged: List[Dict[str, Any]] = []

    async def fake_push(view: Dict[str, Any], bundle: Dict[str, Any]) -> Any:
        return ("已写入", "")

    async def fake_log(site, action, result, detail, **kw) -> None:
        logged.append(
            {
                "action": action,
                "result": result,
                "detail": detail,
                "node": certflow.current_node_id(),
            }
        )

    async def fake_update(site) -> None:
        return None

    monkeypatch.setattr(certs, "_push_target", fake_push)
    monkeypatch.setattr(certs, "record_log", fake_log)
    monkeypatch.setattr(certs, "update_site", fake_update)

    site = _site(
        ca="upload",
        targets=[
            {"id": "a", "name": "A", "method": "local", "deploy_dir": "/a"},
            {"id": "b", "name": "B", "method": "local", "deploy_dir": "/b"},
        ],
        cert_id="c1",
        acme_pem="-----BEGIN CERTIFICATE-----\nLEAF\n-----END CERTIFICATE-----",
        acme_key="-----BEGIN PRIVATE KEY-----\nKEY\n-----END PRIVATE KEY-----",
    )

    outcome = _run(certs.deploy_site(site))

    assert outcome["ok"] is True
    # 两个目标各一条 + 一条汇总：排障时要能看出「是哪个目标挂了」
    assert len(logged) == 3
    assert "目标「A」" in logged[0]["detail"]
    assert "目标「B」" in logged[1]["detail"]
    assert logged[2]["detail"].startswith("已部署到 2 个目标")
    assert all(item["action"] == "deploy" for item in logged)


def test_renew_attributes_deploy_log_to_deploy_step(monkeypatch) -> None:
    logged: List[Dict[str, Any]] = []

    async def fake_cfg(site) -> Dict[str, Any]:
        return {"renew_before_days": 15}

    async def fake_sync(site, provider=None) -> None:
        return None

    async def fake_deploy(site, cfg, action="deploy") -> Dict[str, Any]:
        return {"ok": True, "detail": "已部署"}

    async def fake_log(site, action, result, detail, **kw) -> None:
        logged.append(
            {
                "action": action,
                "result": result,
                "node": certflow.current_node_id(),
                "run": certflow.current_run_id(),
            }
        )

    async def fake_update(site) -> None:
        return None

    class _Provider:
        configured = True

        async def remove(self, cert_id: str) -> None:
            return None

    async def fake_notify(*a, **kw) -> bool:
        return True

    monkeypatch.setattr(certs, "provider_cfg_for", fake_cfg)
    monkeypatch.setattr(certs, "build_provider", lambda kind, cfg: _Provider())
    monkeypatch.setattr(certs, "sync_site_cert", fake_sync)
    monkeypatch.setattr(certs, "deploy_site", fake_deploy)
    monkeypatch.setattr(certs, "record_log", fake_log)
    monkeypatch.setattr(certs, "update_site", fake_update)
    # 只换 notify，保留 certnotify 的事件常量（deploy 步骤要按事件类型发通知）
    monkeypatch.setattr(certs.certnotify, "notify", fake_notify)

    site = _site(pending_cert_id="c-ready", cert_status=STATUS_ISSUED, auto_delete_old=False)
    run = _run(certs.renew_site(site))

    assert run.status == STEP_OK
    assert site["last_run_id"]  # 投影带上了运行 id，页面据此查日志
    deploy_logs = [item for item in logged if item["node"] == "deploy"]
    assert deploy_logs, "部署日志必须归到 deploy 步骤"
    assert all(item["run"] == site["last_run_id"] for item in deploy_logs)


# -------------------------------------------------------------- 手动动作入库
def test_manual_run_records_one_step(monkeypatch) -> None:
    async def job() -> Dict[str, Any]:
        # 动作在步骤上下文里执行，里面的 record_log 才能自动归因
        assert certflow.current_node_id() == "deploy"
        return {"detail": "已部署到 2 个目标"}

    async def fake_update(site) -> None:
        return None

    monkeypatch.setattr(certs, "update_site", fake_update)

    site = _site()
    out = _run(certs.run_manual(site, "deploy", job))

    assert out["detail"] == "已部署到 2 个目标"
    assert site["last_run_status"] == STEP_OK
    assert [step["step"] for step in site["last_run"]] == ["deploy"]
    assert site["last_run"][0]["detail"] == "已部署到 2 个目标"
    assert site["last_run_id"]  # 页面据此拉这一步的日志


def test_manual_run_records_failed_step_and_reraises(monkeypatch) -> None:
    async def job():
        raise RuntimeError("目标不可达")

    async def fake_update(site) -> None:
        return None

    monkeypatch.setattr(certs, "update_site", fake_update)

    site = _site()
    with pytest.raises(RuntimeError):
        _run(certs.run_manual(site, "deploy", job))

    # 失败也要留下一步：否则用户点完只看到一个报错弹窗，页面上什么都没有
    assert site["last_run_status"] == STEP_FAILED
    assert site["last_run"][0]["status"] == STEP_FAILED
    assert "目标不可达" in site["last_run"][0]["detail"]


def test_manual_run_attributes_inner_logs(monkeypatch) -> None:
    sink: List[Dict[str, Any]] = []
    _patch_db(monkeypatch, sink)

    async def job():
        await certs.record_log(_site(), "deploy", "success", "已写入")
        return {"detail": "ok"}

    async def fake_update(site) -> None:
        return None

    monkeypatch.setattr(certs, "update_site", fake_update)

    site = _site()
    _run(certs.run_manual(site, "deploy", job))

    # 手动动作现在也会先写运行头（cert_flow_run），所以不能再取 sink[0]：
    # 按 SQL 找 cert_deploy_log 那一行。
    written = next(
        item["params"] for item in sink if "cert_deploy_log" in item["sql"]
    )
    assert written[8] == site["last_run_id"]
    assert written[9] == "deploy"


def test_manual_run_writes_run_header_and_child_steps(monkeypatch) -> None:
    """手动动作也要进运行表：运行头 + 顶层步骤 + 子步骤（P1-a 的验收点）。"""
    sink: List[Dict[str, Any]] = []
    _patch_db(monkeypatch, sink)

    async def job():
        await certflow.emit_step(
            "deploy:t1", title="目标一", detail="已写入", parent=""
        )
        return {"detail": "ok"}

    async def fake_update(site) -> None:
        return None

    monkeypatch.setattr(certs, "update_site", fake_update)

    site = _site()
    _run(certs.run_manual(site, "deploy", job))

    headers = [item for item in sink if "INSERT INTO cert_flow_run" in item["sql"]]
    assert headers and headers[0]["params"][5] == "cert-manual"
    step_inserts = [
        item for item in sink if item["sql"].startswith("INSERT INTO cert_flow_step ")
    ]
    assert step_inserts, "手动动作的步骤必须落库"
    # 子步骤挂在顶层步骤下：parent_id == 运行里的顶层节点 id
    child = next(item["params"] for item in step_inserts if item["params"][13] == "deploy:t1")
    assert child[6] == "deploy"


# ---------------------------------------------------------- 部署子步骤 / 通知
_UPLOAD_SITE_KW = {
    "ca": "upload",
    "cert_id": "c1",
    "acme_pem": "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----",
    "acme_key": "-----BEGIN PRIVATE KEY-----\nK\n-----END PRIVATE KEY-----",
}


def test_deploy_emits_one_child_step_per_target(monkeypatch) -> None:
    """每个部署目标都是一条子步骤：三个目标挂了一个，能直接看到是哪个。"""
    sink: List[Dict[str, Any]] = []
    _patch_db(monkeypatch, sink)

    async def fake_push(view, bundle):
        return ("写入 2 个文件", "")

    async def fake_update(site) -> None:
        return None

    monkeypatch.setattr(certs, "_push_target", fake_push)
    monkeypatch.setattr(certs, "update_site", fake_update)

    site = _site(
        **_UPLOAD_SITE_KW,
        targets=[
            {"id": "primary", "method": "local", "deploy_dir": "/tmp/a"},
            {"id": "t2", "method": "local", "deploy_dir": "/tmp/b"},
        ],
    )
    _run(
        certs.run_manual(
            site, "deploy", lambda: certs.deploy_site(site, None, action="deploy")
        )
    )

    child_nodes = {
        item["params"][13]
        for item in sink
        if item["sql"].startswith("INSERT INTO cert_flow_step ") and item["params"][6]
    }
    assert {"deploy:primary", "deploy:t2"} <= child_nodes
    # 部署后还会申报一条「线上校验」子步骤：没配监控地址时是 skipped
    assert "verify" in child_nodes


def test_deploy_success_and_partial_send_notifications(monkeypatch) -> None:
    """成功 / 部分失败都要发通知：这是「部署成功也要有消息」的验收点。"""
    sink: List[Dict[str, Any]] = []
    _patch_db(monkeypatch, sink)
    events: List[str] = []

    async def fake_push(view, bundle):
        if str(view.get("deploy_dir")) == "/tmp/bad":
            raise certs.CertError("目标不可达")
        return ("写入 2 个文件", "")

    async def fake_notify(owner, site, event, *a, **kw):
        events.append(event)
        return True

    async def fake_update(site) -> None:
        return None

    monkeypatch.setattr(certs, "_push_target", fake_push)
    monkeypatch.setattr(certs, "update_site", fake_update)
    monkeypatch.setattr(certs.certnotify, "notify", fake_notify)

    good = _site(
        **_UPLOAD_SITE_KW,
        targets=[{"id": "primary", "method": "local", "deploy_dir": "/tmp/a"}],
    )
    _run(certs.deploy_site(good, None, action="deploy"))
    assert events[-1] == certnotify.EVENT_DEPLOY_OK

    mixed = _site(
        **_UPLOAD_SITE_KW,
        targets=[
            {"id": "primary", "method": "local", "deploy_dir": "/tmp/a"},
            {"id": "t2", "method": "local", "deploy_dir": "/tmp/bad"},
        ],
    )
    _run(certs.deploy_site(mixed, None, action="deploy"))
    assert events[-1] == certnotify.EVENT_DEPLOY_PARTIAL


def test_pending_sync_schedules_short_poll(monkeypatch) -> None:
    """CA 还在签发时要排短轮询：页面上的倒计时与补跑作业都靠这个时间。"""
    site = _site(pending_cert_id="p1", cert_status=0)

    async def fake_sync(site, provider=None):
        return site

    monkeypatch.setattr(certs, "sync_site_cert", fake_sync)

    ctx = Context(site=site)
    _run(certs._step_sync(ctx))
    assert site["next_attempt_at"] >= certs.now_ts() + certs.CA_POLL_SECONDS - 5


# --------------------------------------------------------------------- 重跑
def test_retry_run_refuses_steps_that_need_input(monkeypatch) -> None:
    async def fake_get_run(run_id, include_payload=False):
        return {
            "id": run_id,
            "site_id": "site-1",
            "steps": [{"node_id": "import", "status": "failed", "parent_id": ""}],
        }

    async def fake_get_site(site_id, owner=None):
        return _site()

    monkeypatch.setattr(certs.certflow_store, "get_run", fake_get_run)
    monkeypatch.setattr(certs, "get_site", fake_get_site)

    with pytest.raises(certs.CertError):
        _run(certs.retry_run("run_1"))


def test_retry_run_dispatches_failed_deploy(monkeypatch) -> None:
    async def fake_get_run(run_id, include_payload=False):
        return {
            "id": run_id,
            "site_id": "site-1",
            "steps": [
                {"node_id": "deploy", "status": "failed", "parent_id": ""},
                {"node_id": "deploy:t2", "status": "failed", "parent_id": "deploy"},
            ],
        }

    async def fake_get_site(site_id, owner=None):
        return _site()

    async def fake_run_manual(site, node_id, job, **kw):
        return {"detail": "ok"}

    monkeypatch.setattr(certs.certflow_store, "get_run", fake_get_run)
    monkeypatch.setattr(certs, "get_site", fake_get_site)
    monkeypatch.setattr(certs, "run_manual", fake_run_manual)

    out = _run(certs.retry_run("run_1"))
    assert out["step"] == "deploy"


# ------------------------------------------------------------------------ 读取
def test_run_detail_inlines_logs_into_steps(monkeypatch) -> None:
    rows = [
        {"id": 1, "node_id": "issue", "action": "apply", "result": "pending", "detail": "已提交"},
        {"id": 2, "node_id": "deploy", "action": "deploy", "result": "success", "detail": "已写入"},
        {"id": 3, "node_id": "deploy", "action": "renew", "result": "success", "detail": "完成"},
    ]

    async def fake_logs(*a, **kw) -> List[Dict[str, Any]]:
        return list(rows)

    async def fake_get_run(run_id: str) -> Dict[str, Any]:
        return {
            "id": run_id,
            "username": "admin",
            "status": STEP_OK,
            "steps": [
                {"node_id": "issue", "status": STEP_OK},
                {"node_id": "deploy", "status": STEP_OK},
            ],
            "artifacts": [],
        }

    monkeypatch.setattr(certs, "logs", fake_logs)
    monkeypatch.setattr(certs.certflow_store, "get_run", fake_get_run)

    detail = _run(certs.run_detail("run_1"))

    by_id = {step["node_id"]: step for step in detail["steps"]}
    assert [item["id"] for item in by_id["deploy"]["logs"]] == [2, 3]
    assert [item["id"] for item in by_id["issue"]["logs"]] == [1]
    assert detail["groups"]["deploy"][0]["action"] == "deploy"


def test_run_detail_without_run_head_still_groups(monkeypatch) -> None:
    """旧路径不写 cert_flow_run：日志照样要能按步骤分组。"""

    async def fake_logs(*a, **kw) -> List[Dict[str, Any]]:
        return [{"id": 7, "node_id": "sync", "action": "sync", "result": "success", "detail": ""}]

    async def fake_get_run(run_id: str) -> None:
        return None

    monkeypatch.setattr(certs, "logs", fake_logs)
    monkeypatch.setattr(certs.certflow_store, "get_run", fake_get_run)

    detail = _run(certs.run_detail("run_legacy"))

    assert detail["id"] == "run_legacy"
    assert detail["steps"] == []
    assert detail["groups"]["sync"][0]["id"] == 7


def test_run_logs_returns_advancing_cursor(monkeypatch) -> None:
    async def fake_logs(*a, **kw) -> List[Dict[str, Any]]:
        assert kw["after_id"] == 4 and kw["ascending"] is True
        return [{"id": 5, "node_id": "deploy"}, {"id": 9, "node_id": "deploy"}]

    monkeypatch.setattr(certs, "logs", fake_logs)

    fresh = _run(certs.run_logs("run_1", after_id=4))

    assert fresh["cursor"] == 9  # 下次把它当 after_id 传回来
    assert fresh["node_id"] == ""


def test_run_logs_keeps_cursor_when_nothing_new(monkeypatch) -> None:
    async def fake_logs(*a, **kw) -> List[Dict[str, Any]]:
        return []

    monkeypatch.setattr(certs, "logs", fake_logs)

    fresh = _run(certs.run_logs("run_1", after_id=11))

    assert fresh["items"] == []
    assert fresh["cursor"] == 11  # 不能把游标退回 0，否则会重复拉
