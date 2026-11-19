"""证书流水引擎（certflow）的回归测试。

三组断言，对应 P0 的三条验收线：

1. **引擎语义**：依赖排序、跳过、失败短路、产物收集、定义校验。
2. **投影兼容**：任意运行都能投影回旧的 ``last_run`` 三字段，格式与
   ``certpipeline`` 逐字段一致。
3. **迁移等价性**：七种站点场景下，开关开与关产生的外部调用序列、投影结果
   完全相同 —— 这是「能一键切回去」的前提，也是这次重构唯一不可让步的约束。

全部纯逻辑：``SqlSink`` 打桩，CA / 腾讯云 / 数据库全部换假实现。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import certflow, certflow_store, certnotify, certs  # noqa: E402
from app.certpipeline import STEP_FAILED, STEP_OK, STEP_SKIPPED, Context, Pipeline, Step  # noqa: E402
from app.tencent_ssl import STATUS_FAILED, STATUS_ISSUED, STATUS_PENDING  # noqa: E402

# 固定「远期」时间戳：每次调 time.time() 会让两次运行的剩余天数在临界点上不一致
_FAR = int(time.time()) + 60 * 86400
_SOON = int(time.time()) + 3 * 86400


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


class _FakeProvider:
    configured = True

    def __init__(self) -> None:
        self.deleted: List[str] = []

    async def remove(self, cert_id: str) -> None:
        self.deleted.append(cert_id)


def _patch_io(monkeypatch, calls: List[str]) -> None:
    """把流水线里所有会碰外部/库的动作替换成打点（与 test_cert_pipeline 同构）。"""

    async def fake_sync(site: Dict[str, Any], provider: Any = None) -> None:
        calls.append("sync")

    async def fake_update(site: Dict[str, Any]) -> None:
        calls.append("update")

    async def fake_log(site: Dict[str, Any], action: str, result: str, detail: str) -> None:
        calls.append(f"log:{action}:{result}")

    async def fake_apply(site: Dict[str, Any], cfg: Any, *, renew: bool = False) -> Dict[str, Any]:
        calls.append(f"apply:renew={renew}")
        return {"cert_id": "new"}

    async def fake_deploy(site: Dict[str, Any], cfg: Any, action: str = "deploy") -> Dict[str, Any]:
        calls.append(f"deploy:{action}")
        return {"ok": True, "detail": "已部署"}

    async def fake_cfg_for(site: Dict[str, Any]) -> Dict[str, Any]:
        calls.append("cfg")
        return {"renew_before_days": 15}

    async def fake_notify(owner, site, event, title, detail="", **kw):
        calls.append(f"notify:{event}")
        return True

    monkeypatch.setattr(certs, "sync_site_cert", fake_sync)
    monkeypatch.setattr(certs, "update_site", fake_update)
    monkeypatch.setattr(certs, "record_log", fake_log)
    monkeypatch.setattr(certs, "apply_certificate", fake_apply)
    monkeypatch.setattr(certs, "deploy_site", fake_deploy)
    monkeypatch.setattr(certs, "provider_cfg_for", fake_cfg_for)
    monkeypatch.setattr(certs, "build_provider", lambda kind, cfg: _FakeProvider())
    monkeypatch.setattr(certnotify, "notify", fake_notify)


# ------------------------------------------------------------------ 引擎语义
def test_definition_orders_by_dependency_not_declaration() -> None:
    reg = certflow.NodeRegistry()

    async def noop(ctx: certflow.RunContext) -> None:
        return None

    for name in ("a", "b", "c"):
        reg.register(certflow.NodeSpec(type=name, handler=noop))

    # 反转书写顺序：排序必须按 needs 算，而不是按列表位置
    definition = certflow.Definition(
        "demo",
        [
            certflow.Node("c", "c", needs=("b",)),
            certflow.Node("b", "b", needs=("a",)),
            certflow.Node("a", "a"),
        ],
    )
    assert [n.id for n in definition.plan(reg)] == ["a", "b", "c"]


def test_definition_rejects_cycle() -> None:
    reg = certflow.NodeRegistry()

    async def noop(ctx: certflow.RunContext) -> None:
        return None

    for name in ("a", "b"):
        reg.register(certflow.NodeSpec(type=name, handler=noop))

    definition = certflow.Definition(
        "demo",
        [certflow.Node("a", "a", needs=("b",)), certflow.Node("b", "b", needs=("a",))],
    )
    with pytest.raises(certflow.DefinitionError):
        definition.plan(reg)


def test_definition_rejects_missing_dependency_and_unknown_type() -> None:
    definition = certflow.Definition("demo", [certflow.Node("a", "a", needs=("ghost",))])
    with pytest.raises(certflow.DefinitionError):
        definition.validate(certflow.REGISTRY)  # ghost 不存在

    unknown = certflow.Definition("demo", [certflow.Node("a", "no-such-type")])
    with pytest.raises(certflow.UnknownNodeType):
        unknown.validate(certflow.REGISTRY)


def test_duplicate_node_type_is_rejected() -> None:
    reg = certflow.NodeRegistry()

    async def noop(ctx: certflow.RunContext) -> None:
        return None

    reg.register(certflow.NodeSpec(type="x", handler=noop))
    with pytest.raises(certflow.DefinitionError):
        reg.register(certflow.NodeSpec(type="x", handler=noop))


def test_runner_short_circuits_and_keeps_original_exception() -> None:
    seen: List[str] = []

    async def first(ctx: certflow.RunContext) -> str:
        seen.append("first")
        return "one"

    async def boom(ctx: certflow.RunContext) -> None:
        seen.append("boom")
        raise RuntimeError("炸了")

    async def never(ctx: certflow.RunContext) -> None:
        seen.append("never")

    reg = certflow.NodeRegistry()
    reg.register(certflow.NodeSpec("first", first))
    reg.register(certflow.NodeSpec("boom", boom))
    reg.register(certflow.NodeSpec("never", never))
    definition = certflow.Definition(
        "demo",
        [
            certflow.Node("first", "first"),
            certflow.Node("boom", "boom", needs=("first",)),
            certflow.Node("never", "never", needs=("boom",)),
        ],
    )

    run = _run(certflow.Runner(reg).run(definition, {}))

    assert seen == ["first", "boom"]  # 失败之后再无节点执行
    assert [s.status for s in run.steps] == [certflow.NODE_OK, certflow.NODE_FAILED]
    assert run.status == certflow.NODE_FAILED and run.failed
    assert isinstance(run.exc, RuntimeError)  # 原始异常留给调用方按类型分流
    assert run.error.startswith("boom:")
    assert run.steps[0].detail == "one"


def test_runner_skips_when_dependency_not_ok() -> None:
    """依赖没跑成功，后续节点必须跳过，而不是带着半份上下文硬跑。"""
    seen: List[str] = []

    async def noop(ctx: certflow.RunContext) -> None:
        seen.append("noop")

    reg = certflow.NodeRegistry()
    reg.register(certflow.NodeSpec("noop", noop))
    definition = certflow.Definition(
        "demo",
        [
            certflow.Node("head", "noop", when=lambda ctx: False),
            certflow.Node("body", "noop", needs=("head",)),
        ],
    )

    run = _run(certflow.Runner(reg).run(definition, {}))

    assert seen == []  # 依赖被跳过 → 下游连 handler 都不进
    assert [s.status for s in run.steps] == [certflow.NODE_SKIPPED, certflow.NODE_SKIPPED]
    assert run.ran is False
    assert run.status == certflow.NODE_OK


def test_runner_collects_artifacts_with_checksum() -> None:
    async def issue(ctx: certflow.RunContext) -> None:
        ctx.artifact("cert", b"-----BEGIN CERTIFICATE-----", meta={"domain": "a.example.com"})
        ctx.artifact("key", b"private")

    reg = certflow.NodeRegistry()
    reg.register(certflow.NodeSpec("issue", issue, produces=("cert", "key")))
    definition = certflow.Definition("demo", [certflow.Node("issue", "issue")])

    run = _run(certflow.Runner(reg).run(definition, {}))

    assert run.artifact_kinds() == ["cert", "key"]
    cert = run.artifact("cert")
    assert cert is not None
    assert cert.size == len(b"-----BEGIN CERTIFICATE-----")
    assert len(cert.checksum) == 64
    assert cert.meta["domain"] == "a.example.com"
    assert cert.node_id == "issue"
    # 同一 payload 的指纹稳定，内容一变指纹就变 —— 产物表靠它做去重与比对
    assert certflow.Artifact("cert", b"x").checksum == certflow.Artifact("cert", b"x").checksum
    assert certflow.Artifact("cert", b"x").checksum != certflow.Artifact("cert", b"y").checksum


def test_runner_uses_custom_detail_and_ignores_container_value() -> None:
    async def gives_dict(ctx: certflow.RunContext) -> Dict[str, Any]:
        return {"noisy": True}

    async def empty(ctx: certflow.RunContext) -> None:
        return None

    reg = certflow.NodeRegistry()
    reg.register(certflow.NodeSpec("dict", gives_dict))
    reg.register(certflow.NodeSpec("none", empty))
    definition = certflow.Definition("demo", [certflow.Node("d", "dict"), certflow.Node("n", "none")])

    run = _run(certflow.Runner(reg).run(definition, {}))

    # 容器返回值不往 detail 里塞 JSON：默认留空，避免把 last_run 撑爆
    assert run.steps[0].detail == ""
    assert run.steps[1].detail == ""


# -------------------------------------------------------------------- 投影兼容
def test_projection_writes_the_three_legacy_fields() -> None:
    run = certflow.Run(
        id="run_x",
        pipeline="cert-renew",
        status=certflow.NODE_OK,
        steps=[certflow.RunStep("guard", "guard", certflow.NODE_OK, "可以执行", 3, 1)],
        started_at=1000,
        finished_at=1002,
    )
    site: Dict[str, Any] = {}

    certflow.project_last_run(site, run)

    assert site["last_run_at"] == 1002
    assert site["last_run_status"] == certflow.NODE_OK
    assert site["last_run"] == [
        {"step": "guard", "status": certflow.NODE_OK, "detail": "可以执行", "ms": 3, "at": 1}
    ]


def test_run_from_legacy_preserves_every_step_field() -> None:
    async def ok(ctx: Context) -> str:
        return "done"

    async def boom(ctx: Context) -> None:
        raise RuntimeError("炸了")

    legacy = _run(
        Pipeline(
            "cert-renew",
            [Step("first", ok, detail=lambda ctx, value: value), Step("boom", boom)],
        ).run(Context(site={}))
    )
    flow = certflow.run_from_legacy(legacy, "cert-renew")

    assert flow.status == legacy.status
    assert flow.error == legacy.error
    assert isinstance(flow.exc, RuntimeError)
    assert flow.as_list() == legacy.as_list()


def test_flow_definition_tracks_the_legacy_pipeline() -> None:
    """节点 id 与旧步骤名必须一一对应。

    这条是防漂移的闸：以后往 ``RENEW_FLOW`` 里加了节点却忘了同步旧 pipeline，
    或者名字对不上，等价性测试会失败，而不是等到线上发现 last_run 少了一行。
    """
    assert [n.id for n in certs.RENEW_FLOW.plan()] == [
        s.name for s in certs._RENEW_PIPELINE.steps
    ]
    data = certs.RENEW_FLOW.as_dict()
    assert data["name"] == "cert-renew"
    assert "when" not in data["nodes"][0]  # 回调不入库
    guard = next(n for n in certs.RENEW_FLOW.nodes if n.id == "guard")
    assert guard.title == "退避检查"


def test_sink_encoding_respects_payload_flag() -> None:
    artifact = certflow.Artifact("key", b"secret")

    assert certflow_store._encode(artifact, False) == {"encoding": "none", "payload": None}

    stored = certflow_store._encode(artifact, True)
    assert stored["encoding"] == "base64"
    import base64

    assert base64.b64decode(stored["payload"]) == b"secret"

    text = certflow_store._encode(certflow.Artifact("deploy", "ok"), True)
    assert text == {"encoding": "text", "payload": "ok"}


# ------------------------------------------------------------------ 迁移等价性
SCENARIOS = [
    ("无证书", {}),
    ("签发中", {"pending_cert_id": "c-pending", "cert_status": STATUS_PENDING[0]}),
    ("已签发", {"pending_cert_id": "c-ready", "cert_status": STATUS_ISSUED}),
    ("申请失败", {"pending_cert_id": "c-bad", "cert_status": STATUS_FAILED[0]}),
    ("到期需续期", {"cert_id": "c-old", "expire_at": _SOON, "auto_renew": True}),
    ("到期未开续期", {"cert_id": "c-old", "expire_at": _SOON, "auto_renew": False}),
    ("未到期", {"cert_id": "c-old", "expire_at": _FAR}),
    ("已关闭到期提醒", {"cert_id": "c-old", "expire_at": _SOON, "notify_expiring": False}),
]


def _projection(site: Dict[str, Any]) -> List[tuple]:
    """去掉 ms/at —— 它们是时钟读数，两次运行本就不可能相同。"""
    return [(s["step"], s["status"], s["detail"]) for s in site["last_run"]]


def _set_enabled(monkeypatch, enabled: bool, *, silence_sink: bool = True) -> None:
    monkeypatch.setattr(certs.settings, "certflow_enabled", enabled)
    monkeypatch.setattr(certs.settings, "certflow_store_payload", False)
    if silence_sink:
        # 旁路仓储在等价性测试里没有意义：要比较的是编排，不是落库。
        # 需要观察 sink 的用例传 silence_sink=False 并自己打桩。
        monkeypatch.setattr(certs.certflow_store, "SqlSink", lambda *a, **kw: None)


@pytest.mark.parametrize("label,kwargs", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_legacy_and_flow_are_equivalent(monkeypatch, label, kwargs) -> None:
    legacy_calls: List[str] = []
    _patch_io(monkeypatch, legacy_calls)
    _set_enabled(monkeypatch, False)
    legacy_site = _site(**kwargs)
    legacy_run = _run(certs.renew_site(legacy_site))

    flow_calls: List[str] = []
    _patch_io(monkeypatch, flow_calls)
    _set_enabled(monkeypatch, True)
    flow_site = _site(**kwargs)
    flow_run = _run(certs.renew_site(flow_site))

    # 1) 对外副作用完全一致（调了哪些接口、什么顺序）
    assert flow_calls == legacy_calls
    # 2) 投影结果逐字段一致（步骤名、状态、人话说明）
    assert _projection(flow_site) == _projection(legacy_site)
    # 3) 返回给上层的动作与状态一致
    assert flow_run.value == legacy_run.value
    assert flow_run.status == legacy_run.status
    assert flow_run.exc is None and legacy_run.exc is None
    # 4) 站点是否落库的判定一致
    assert ("update" in flow_calls) == ("update" in legacy_calls)


def test_renew_site_disabled_never_touches_flow_tables(monkeypatch) -> None:
    """开关关闭 = 一条新表都不写，回滚不需要担心半新半旧的数据。"""
    touched: List[str] = []

    class _Trap:
        def __init__(self, *a, **kw):
            touched.append("constructed")

    monkeypatch.setattr(certs.certflow_store, "SqlSink", _Trap)
    _patch_io(monkeypatch, [])
    _set_enabled(monkeypatch, False)

    _run(certs.renew_site(_site(pending_cert_id="c-ready", cert_status=STATUS_ISSUED)))

    assert touched == []


def test_renew_site_enabled_streams_a_complete_run(monkeypatch) -> None:
    """开关打开时，sink 应收到 start → 逐步 step → finish 的完整回放。"""
    events: List[str] = []

    class _Recording:
        def __init__(self, site, store_payload=False):
            events.append(f"constructed:payload={store_payload}")

        async def start(self, run):
            events.append(f"start:{run.id}:{run.pipeline}:v{run.version}")

        async def step(self, run, step):
            events.append(f"step:{step.node_id}:{step.status}")

        async def finish(self, run):
            events.append(f"finish:{run.status}")

    monkeypatch.setattr(certs.certflow_store, "SqlSink", _Recording)
    _patch_io(monkeypatch, [])
    _set_enabled(monkeypatch, True, silence_sink=False)

    site = _site(pending_cert_id="c-ready", cert_status=STATUS_ISSUED)
    run = _run(certs.renew_site(site, trigger="manual"))

    assert events[0] == "constructed:payload=False"
    assert events[1].startswith("start:run_")
    assert "step:issue:skipped" in events  # 已签发分支不重新申请
    assert "step:deploy:ok" in events
    assert events[-1] == f"finish:{STEP_OK}"
    # 投影仍然写在站点上：新表只增不减，读接口照旧
    assert site["last_run_status"] == STEP_OK
    assert run.status == STEP_OK


def test_renew_site_enabled_keeps_backoff_and_error_semantics(monkeypatch) -> None:
    """新引擎同样要把 CA 异常原样带出来，``_run_sites`` 的分流逻辑才不用改。"""

    async def boom(site: Dict[str, Any], provider: Any = None) -> None:
        raise RuntimeError("接口抖动")

    monkeypatch.setattr(certs, "sync_site_cert", boom)
    monkeypatch.setattr(certs, "update_site", _fake_update)
    monkeypatch.setattr(certs, "provider_cfg_for", _fake_cfg)
    monkeypatch.setattr(certs, "build_provider", lambda kind, cfg: _FakeProvider())
    monkeypatch.setattr(certs.certflow_store, "SqlSink", lambda *a, **kw: None)
    _set_enabled(monkeypatch, True)

    site = _site(pending_cert_id="c-pending", cert_status=STATUS_PENDING[0])
    run = _run(certs.renew_site(site))

    assert run.status == STEP_FAILED
    assert isinstance(run.exc, RuntimeError)
    assert site["last_run_status"] == STEP_FAILED
    assert [s["step"] for s in site["last_run"]][-1] == "sync"  # 卡在同步这一步


async def _fake_update(site: Dict[str, Any]) -> None:
    return None


async def _fake_cfg(site: Dict[str, Any]) -> Dict[str, Any]:
    return {"renew_before_days": 15}
