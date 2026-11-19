"""证书流水引擎：类型化节点 + 运行实例 + 显式产物。

与 :mod:`app.certpipeline` 的分工
---------------------------------
``certpipeline`` 已经把「一个 200 行的函数」拆成了有顺序、有留痕的步骤，但步骤
序列仍然是**写死的线性列表**：谁在谁后面、谁依赖谁的产出，只存在于那个列表的
字面顺序里。渠道（腾讯云 / ACME / 手动导入）与部署目标（本地目录 / SSH / 面板
推送）一多，同一份顺序就不再成立，加一条分支只能再复制一份 ``Pipeline``。

本模块把三件事显式化：

* **节点类型（:class:`NodeSpec`）**：声明输入端口、输出端口、可产出的产物类别。
  新增渠道 = 注册一个类型，调度逻辑一行不改。
* **运行实例（:class:`Run`）**：每次执行是一条独立记录，带 id、触发器、起止时间、
  逐步状态，可按站点/用户查询。
* **产物（:class:`Artifact`）**：证书链、私钥、部署结果不再只是 ``site`` 里的几个
  键，而是带 ``kind`` / ``checksum`` / ``meta`` 的一等实体，由仓储统一落盘。

兼容策略（P0 的核心约束）
-------------------------
旧读接口依赖 ``site["last_run"]`` 的 ``[{step,status,detail,ms,at}]`` 结构和
``last_run_status`` 字段。:func:`project_last_run` 把任意 :class:`Run` 投影回这三个
字段，键名、顺序、截断长度都与 ``certpipeline.StepResult.as_dict`` 完全一致；
旧路径的结果用 :func:`run_from_legacy` 适配成 :class:`Run` 后再投影。因此
「换引擎」对前端与旧调用方是不可见的，回滚也只是切换一个开关。

本模块刻意不 import ``certs``，也不碰数据库：仓储由调用方注入
（见 :mod:`app.certflow_store`），引擎可以脱离网络与 MySQL 单测。
"""
from __future__ import annotations

import contextvars
import hashlib
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

# 节点/运行状态。与 ``cert_deploy_log.result`` 的 ok/failed 共用同一套词表，
# 前端「最后运行结果」可以原样渲染，不必再做映射。
NODE_OK = "ok"
NODE_SKIPPED = "skipped"
NODE_FAILED = "failed"
RUNNING = "running"

# 单条 detail 落库前的截断长度。与 certpipeline.DETAIL_LIMIT 保持一致：
# 两套引擎写同一个 ``last_run`` 字段，截断规则不同会让迁移前后出现假 diff。
DETAIL_LIMIT = 600

# 与 certpipeline.StepResult.as_dict 的键顺序逐一对齐 —— 等价性测试断言的是
# 整个字典相等，键顺序虽不影响 ==，但保持一致能让日志 diff 一眼看出差异。
_STEP_KEYS = ("step", "status", "detail", "ms", "at")


def _now() -> int:
    return int(time.time())


def new_run_id() -> str:
    """运行 id：时间前缀便于按时间排序，随机后缀避免同秒撞车。"""
    return "run_" + format(_now(), "x") + secrets.token_hex(4)


def _short(text: Any) -> str:
    return str(text).strip().replace("\n", " ")[:DETAIL_LIMIT]


# ------------------------------------------------------------------ 日志归因上下文
# 「这条部署日志属于哪次运行的哪一步」本来要沿着 调用栈 逐层传参：
# _step_deploy → deploy_site → _push_target → record_log 一共五六层，每层加两个
# 参数既污染签名，也总有人漏传。用 contextvar 把「当前运行 / 当前步骤」变成
# 隐式上下文，record_log 自己读，调用点一行不用改。
#
# asyncio 语义：contextvar 在同一 task 的 await 之间保持不变，``create_task`` 会
# 复制当前上下文。所以节点内部起的子任务（并发部署多目标）也能读到正确的归因，
# 不会串到别的运行上去。
_RUN_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "cert_flow_run_id", default=""
)
_NODE_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "cert_flow_node_id", default=""
)


def current_run_id() -> str:
    """当前正在执行的运行 id；不在流水线里（手工接口 / 后台清理）时为空串。"""
    return _RUN_ID.get()


def current_node_id() -> str:
    """当前正在执行的步骤 id；不在流水线里时为空串。"""
    return _NODE_ID.get()


@contextmanager
def step_scope(run_id: str = "") -> Iterator[str]:
    """把一段执行整体标记为「某次运行」，产出实际生效的运行 id。

    进入时记下运行 id 并清空步骤；退出时无论成功、失败还是取消都复位 ——
    否则归因会泄漏给同一 task 后续的无关动作（例如调度器里的下一轮 tick）。
    """
    resolved = str(run_id or "")
    run_token = _RUN_ID.set(resolved)
    node_token = _NODE_ID.set("")
    try:
        yield resolved
    finally:
        _NODE_ID.reset(node_token)
        _RUN_ID.reset(run_token)


def enter_step(node_id: str, index: int = 0) -> None:
    """切到某一步。

    直接作为 ``certpipeline.Pipeline.run(on_step=...)`` 的回调使用，所以
    ``index`` 虽然暂时不用，签名必须兼容 ``(name, index)``。
    """
    _NODE_ID.set(str(node_id or ""))


class FlowError(ValueError):
    """流水定义或运行期的配置错误。"""


class DefinitionError(FlowError):
    """定义本身不合法（未知节点类型、重复 id、依赖缺失或成环）。"""


class UnknownNodeType(DefinitionError):
    """引用了没有注册的节点类型。"""


# --------------------------------------------------------------------- 端口与类型
@dataclass(frozen=True)
class Port:
    """节点端口声明。

    P0 只做**契约记录**，不做运行期类型强制：证书流水里的产物大多是字符串
    （PEM）或字典（部署结果），过早引入运行时校验只会让新增渠道变难。端口信息
    目前用于定义校验与文档生成，后续要做「按类型连线」的编排界面时它才真正生效。
    """

    name: str
    kind: str = "any"
    required: bool = True


@dataclass
class NodeSpec:
    """一个节点类型的契约与实现。

    :param type: 类型标识（如 ``issue`` / ``deploy``），定义里按它引用
    :param handler: ``async def (ctx) -> Any``；产物用 ``ctx.artifact()`` 申报
    :param produces: 该类型可能产出的 artifact 类别，供审计与文档用
    """

    type: str
    handler: Callable[["RunContext"], Awaitable[Any]]
    inputs: Tuple[Port, ...] = ()
    outputs: Tuple[Port, ...] = ()
    produces: Tuple[str, ...] = ()
    description: str = ""

    @property
    def input_names(self) -> List[str]:
        return [p.name for p in self.inputs]

    @property
    def output_names(self) -> List[str]:
        return [p.name for p in self.outputs]


class NodeRegistry:
    """节点类型注册表。进程内单例（:data:`REGISTRY`），测试可另建实例隔离。"""

    def __init__(self) -> None:
        self._specs: Dict[str, NodeSpec] = {}

    def register(self, spec: NodeSpec) -> NodeSpec:
        if not spec.type:
            raise DefinitionError("节点类型不能为空")
        if spec.type in self._specs:
            # 静默覆盖会让「同名类型到底跑的是哪份实现」变成玄学，直接拒绝
            raise DefinitionError(f"节点类型重复注册：{spec.type}")
        if not callable(spec.handler):
            raise DefinitionError(f"节点类型 {spec.type} 缺少可调用的 handler")
        self._specs[spec.type] = spec
        return spec

    def replace(self, spec: NodeSpec) -> NodeSpec:
        """覆盖注册（热更新 / 测试用）。与 :meth:`register` 的区别是允许已存在。"""
        if not callable(spec.handler):
            raise DefinitionError(f"节点类型 {spec.type} 缺少可调用的 handler")
        self._specs[spec.type] = spec
        return spec

    def get(self, node_type: str) -> NodeSpec:
        try:
            return self._specs[node_type]
        except KeyError as exc:
            raise UnknownNodeType(f"未注册的节点类型：{node_type}") from exc

    def has(self, node_type: str) -> bool:
        return node_type in self._specs

    def types(self) -> List[str]:
        return sorted(self._specs)

    def specs(self) -> List[NodeSpec]:
        return [self._specs[t] for t in self.types()]

    def unregister(self, node_type: str) -> None:
        self._specs.pop(node_type, None)


REGISTRY = NodeRegistry()


def node_type(
    type_name: str,
    *,
    inputs: Sequence[Port] = (),
    outputs: Sequence[Port] = (),
    produces: Sequence[str] = (),
    description: str = "",
) -> Callable[[Callable[["RunContext"], Awaitable[Any]]], Callable[["RunContext"], Awaitable[Any]]]:
    """把一个 ``async def (ctx)`` 注册成节点类型。

        @certflow.node_type("issue", produces=("cert", "key"))
        async def issue(ctx):
            ...
            ctx.artifact("cert", pem, meta={"domains": [...]})
    """

    def wrap(handler: Callable[["RunContext"], Awaitable[Any]]) -> Callable[["RunContext"], Awaitable[Any]]:
        REGISTRY.register(
            NodeSpec(
                type=type_name,
                handler=handler,
                inputs=tuple(inputs),
                outputs=tuple(outputs),
                produces=tuple(produces),
                description=description,
            )
        )
        return handler

    return wrap


# ------------------------------------------------------------------------- 产物
@dataclass
class Artifact:
    """一次运行产出的东西：证书、私钥、链、部署回执、诊断报告。

    ``payload`` 允许 ``bytes``（PEM/DER 原始字节）或 ``str``（人读的结果说明）。
    checksum 在需要时才由仓储计算并落库，引擎侧只保证 payload 不被就地改写。
    """

    kind: str
    payload: Any
    node_id: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def size(self) -> int:
        if isinstance(self.payload, (bytes, bytearray)):
            return len(self.payload)
        return len(str(self.payload).encode("utf-8"))

    @property
    def checksum(self) -> str:
        if isinstance(self.payload, (bytes, bytearray)):
            data = bytes(self.payload)
        else:
            data = str(self.payload).encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "node_id": self.node_id,
            "checksum": self.checksum,
            "size": self.size,
            "meta": dict(self.meta),
        }


# --------------------------------------------------------------------- 运行实例
@dataclass
class RunStep:
    """一个节点在本次运行中的执行记录。

    ``at`` 是相对运行开始的毫秒偏移（与 certpipeline 同义），页面的时间轴靠它
    排出「哪一步卡了很久」；``ms`` 只统计真正执行了的节点，跳过恒为 0。
    """

    node_id: str
    node_type: str
    status: str
    detail: str = ""
    ms: int = 0
    at: int = 0
    error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        # 只暴露旧字段：这是 ``site["last_run"]`` 的对外契约
        return {
            "step": self.node_id,
            "status": self.status,
            "detail": self.detail,
            "ms": self.ms,
            "at": self.at,
        }

    def as_row(self) -> Dict[str, Any]:
        """落库用：比 :meth:`as_dict` 多出类型与错误原文，便于排障。"""
        row = self.as_dict()
        row["node_type"] = self.node_type
        row["error"] = self.error
        return row


@dataclass
class Run:
    """一次流水线执行。"""

    id: str
    pipeline: str
    version: int = 1
    status: str = RUNNING
    steps: List[RunStep] = field(default_factory=list)
    artifacts: List[Artifact] = field(default_factory=list)
    value: Any = None
    error: str = ""
    exc: Optional[BaseException] = None
    trigger: str = "tick"
    site_id: str = ""
    owner: str = ""
    started_at: int = 0
    finished_at: int = 0
    #: 运行期上下文（节点之间传的临时状态）。调用方据此判断「这次到底动没动过
    #: 站点」，与旧 ``Context.data`` 是同一份语义。
    state: Dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status == NODE_FAILED

    @property
    def finished(self) -> bool:
        return self.status != RUNNING

    @property
    def ran(self) -> bool:
        """有节点真正执行过（区分「本次无事可做」与「做完了」）。"""
        return any(s.status != NODE_SKIPPED for s in self.steps)

    @property
    def duration_ms(self) -> int:
        if not self.started_at:
            return 0
        end = self.finished_at or _now()
        return max(0, int(end - self.started_at) * 1000)

    def artifact(self, kind: str) -> Optional[Artifact]:
        """取最近一个指定类别的产物（后产出的覆盖先产出的）。"""
        for item in reversed(self.artifacts):
            if item.kind == kind:
                return item
        return None

    def artifact_kinds(self) -> List[str]:
        return [a.kind for a in self.artifacts]

    def as_list(self) -> List[Dict[str, Any]]:
        """与 ``certpipeline.RunResult.as_list`` 逐字段一致。"""
        return [s.as_dict() for s in self.steps]

    @property
    def keys(self) -> Tuple[str, ...]:
        return _STEP_KEYS


# ------------------------------------------------------------------------- 上下文
@dataclass
class RunContext:
    """节点之间传递状态的载体。

    ``site`` 是可变字典，节点直接改它就是改站点配置（旧 ``certpipeline`` 的
    既有语义，保留）；``data`` 放本次运行的临时状态；``artifacts`` 是本次运行
    产出的全部产物，运行结束由仓储统一落盘。
    """

    site: Dict[str, Any]
    run: Run
    data: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[Artifact] = field(default_factory=list)
    outputs: Dict[str, Any] = field(default_factory=dict)
    current: Optional["Node"] = None

    # -- 与 certpipeline.Context 对齐的手感 -------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:  # noqa: A003 - 与 dict 的手感对齐
        self.data[key] = value

    # -- 节点侧接口 ------------------------------------------------------
    def option(self, key: str, default: Any = None) -> Any:
        """读**当前节点**的 options（定义里写死的参数）。"""
        if self.current is None:
            return default
        return self.current.options.get(key, default)

    def output(self, node_id: str, default: Any = None) -> Any:
        """读依赖节点的返回值。"""
        return self.outputs.get(node_id, default)

    def artifact(self, kind: str, payload: Any, meta: Optional[Dict[str, Any]] = None) -> Artifact:
        """申报一个产物。

        产物同时挂在运行实例与上下文上：运行实例那份给仓储落盘，上下文那份让
        后续节点能用 ``ctx.lookup()`` 取到，不必再往 ``data`` 里塞。
        """
        item = Artifact(
            kind=kind,
            payload=payload,
            node_id=self.current.id if self.current is not None else "",
            meta=dict(meta or {}),
        )
        self.artifacts.append(item)
        self.run.artifacts.append(item)
        return item

    def lookup(self, kind: str) -> Optional[Artifact]:
        for item in reversed(self.artifacts):
            if item.kind == kind:
                return item
        return None


# --------------------------------------------------------------------- 定义与计划
@dataclass
class Node:
    """定义里的一个节点实例。

    :param id: 定义内唯一，投影到 ``last_run[].step``，也是日志定位锚点
    :param type: 对应 :class:`NodeSpec` 的类型标识
    :param needs: 依赖的节点 id；任一依赖未 ``ok`` 则本节点跳过
    :param when: 额外前置条件；返回 False 记 ``skipped``
    :param detail: 自定义「这一步做了什么」的说明；缺省时按返回值推断
    """

    id: str
    type: str
    title: str = ""
    options: Dict[str, Any] = field(default_factory=dict)
    needs: Tuple[str, ...] = ()
    when: Optional[Callable[[RunContext], bool]] = None
    detail: Optional[Callable[[RunContext, Any], str]] = None

    def label(self) -> str:
        return self.title or self.id


@dataclass
class Definition:
    """一份流水定义。``version`` 单调递增，回滚就是切回上一版。"""

    name: str
    nodes: List[Node]
    version: int = 1
    description: str = ""

    def node(self, node_id: str) -> Optional[Node]:
        return next((n for n in self.nodes if n.id == node_id), None)

    def validate(self, registry: Optional[NodeRegistry] = None) -> None:
        """静态校验。定义错误属于编程错误，一律抛 :class:`DefinitionError`。"""
        reg = registry or REGISTRY
        seen: Dict[str, Node] = {}
        for item in self.nodes:
            if not item.id:
                raise DefinitionError("节点 id 不能为空")
            if item.id in seen:
                raise DefinitionError(f"节点 id 重复：{item.id}")
            seen[item.id] = item
            reg.get(item.type)  # 未注册直接抛 UnknownNodeType
            for dep in item.needs:
                if dep not in seen:
                    # 依赖必须写在前面的节点上，否则多半是拼错了 id
                    raise DefinitionError(
                        f"节点 {item.id} 依赖了不存在的节点：{dep}"
                    )
        self.plan(reg)  # 复用拓扑排序做环检测

    def plan(self, registry: Optional[NodeRegistry] = None) -> List[Node]:
        """稳定拓扑排序：入度为 0 的节点保持定义里的原始顺序。

        顺序对可读性很重要 —— 「先同步、后判定」这种人读得懂的序列，不该因为
        排序算法的不确定性在两次运行里不一样。
        """
        reg = registry or REGISTRY
        by_id = {n.id: n for n in self.nodes}
        if len(by_id) != len(self.nodes):
            raise DefinitionError("节点 id 重复")

        index = {n.id: i for i, n in enumerate(self.nodes)}
        indegree: Dict[str, int] = {n.id: 0 for n in self.nodes}
        children: Dict[str, List[str]] = {n.id: [] for n in self.nodes}
        for item in self.nodes:
            reg.get(item.type)
            for dep in item.needs:
                if dep not in by_id:
                    raise DefinitionError(
                        f"节点 {item.id} 依赖了不存在的节点：{dep}"
                    )
                indegree[item.id] += 1
                children[dep].append(item.id)

        ready = sorted([nid for nid, deg in indegree.items() if deg == 0], key=index.__getitem__)
        ordered: List[Node] = []
        while ready:
            nid = ready.pop(0)
            ordered.append(by_id[nid])
            for child in children[nid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
            ready.sort(key=index.__getitem__)

        if len(ordered) != len(self.nodes):
            stuck = sorted(set(by_id) - {n.id for n in ordered})
            # 依赖成环时最需要知道的是「哪几个节点绕在一起」
            raise DefinitionError("节点依赖成环：" + ", ".join(stuck))
        return ordered

    def as_dict(self) -> Dict[str, Any]:
        """序列化（落库 / 前端展示）。``when`` 是代码回调，不入库。"""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "nodes": [
                {
                    "id": n.id,
                    "type": n.type,
                    "title": n.title,
                    "options": dict(n.options),
                    "needs": list(n.needs),
                }
                for n in self.nodes
            ],
        }


# ------------------------------------------------------------------------- 执行器
class Runner:
    """按定义执行一轮流水。

    ``sink`` 是可选的旁路记录器（见 :mod:`app.certflow_store`），P0 里它只负责
    「写下来」；忘了实现某个方法也不会影响主流程 —— 可观测性不该成为新的失败点。
    """

    def __init__(self, registry: Optional[NodeRegistry] = None) -> None:
        self.registry = registry or REGISTRY

    async def _call(self, sink: Any, method: str, *args: Any) -> None:
        if sink is None:
            return
        fn = getattr(sink, method, None)
        if fn is None:
            return
        try:
            await fn(*args)
        except Exception:  # noqa: BLE001 - 记录失败不能反过来打断签发
            pass

    async def run(
        self,
        definition: Definition,
        site: Dict[str, Any],
        *,
        trigger: str = "tick",
        run_id: str = "",
        sink: Any = None,
    ) -> Run:
        # 归因范围包住整次运行：调度器下一轮 tick 复用同一个 task，靠 step_scope
        # 的 reset 才不会把「上一次运行 / 最后一步」泄漏给无关动作。
        rid = run_id or new_run_id()
        with step_scope(rid):
            return await self._execute(
                definition, site, trigger=trigger, run_id=rid, sink=sink
            )

    async def _execute(
        self,
        definition: Definition,
        site: Dict[str, Any],
        *,
        trigger: str,
        run_id: str,
        sink: Any,
    ) -> Run:
        ordered = definition.plan(self.registry)
        run = Run(
            id=run_id or new_run_id(),
            pipeline=definition.name,
            version=definition.version,
            status=RUNNING,
            trigger=trigger,
            site_id=str(site.get("id") or ""),
            owner=str(site.get("username") or ""),
            started_at=_now(),
        )
        ctx = RunContext(site=site, run=run)
        # state 与 ctx.data 共享同一份字典：调用方读 run.state 就是读运行期上下文
        run.state = ctx.data
        base = time.perf_counter()

        def elapsed() -> int:
            return int((time.perf_counter() - base) * 1000)

        await self._call(sink, "start", run)
        status_by_id: Dict[str, str] = {}

        for item in ordered:
            # 步骤归因必须在**任何**副作用之前切好：跳过判定、前置条件求值也可能
            # 触发读配置一类的动作，那些日志同样属于这一步。
            enter_step(item.id)
            spec = self.registry.get(item.type)

            # 依赖未成功 → 本节点跟着跳过。旧实现靠 ``ctx.get("synced")`` 这种
            # 隐式开关串顺序，漏写一个条件就会在缺数据时硬跑；这里显式判定。
            blocked = next(
                (dep for dep in item.needs if status_by_id.get(dep) != NODE_OK),
                "",
            )
            if blocked:
                step = RunStep(item.id, item.type, NODE_SKIPPED, "", 0, elapsed())
                run.steps.append(step)
                status_by_id[item.id] = NODE_SKIPPED
                await self._call(sink, "step", run, step)
                continue

            if item.when is not None:
                ctx.current = item
                try:
                    wanted = bool(item.when(ctx))
                except Exception:  # 前置条件本身出错，按「不执行」处理更安全
                    wanted = False
                if not wanted:
                    step = RunStep(item.id, item.type, NODE_SKIPPED, "", 0, elapsed())
                    run.steps.append(step)
                    status_by_id[item.id] = NODE_SKIPPED
                    await self._call(sink, "step", run, step)
                    continue

            ctx.current = item
            started = time.perf_counter()
            try:
                value = await spec.handler(ctx)
            except Exception as exc:  # noqa: BLE001 - 统一收口，原样交还调用方
                cost = int((time.perf_counter() - started) * 1000)
                step = RunStep(
                    item.id, item.type, NODE_FAILED, _short(exc), cost, elapsed(), str(exc)
                )
                run.steps.append(step)
                status_by_id[item.id] = NODE_FAILED
                await self._call(sink, "step", run, step)
                run.status = NODE_FAILED
                run.error = f"{item.id}: {exc}"
                run.exc = exc
                run.value = ctx.get("result")
                run.finished_at = _now()
                await self._call(sink, "finish", run)
                return run

            cost = int((time.perf_counter() - started) * 1000)
            ctx.outputs[item.id] = value
            step = RunStep(item.id, item.type, NODE_OK, _detail(ctx, item, value), cost, elapsed())
            run.steps.append(step)
            status_by_id[item.id] = NODE_OK
            await self._call(sink, "step", run, step)

        run.status = NODE_OK
        run.value = ctx.get("result")
        run.finished_at = _now()
        await self._call(sink, "finish", run)
        return run


def _detail(ctx: RunContext, node: Node, value: Any) -> str:
    """节点没显式给说明时，把可读的返回值当成一句话。"""
    if node.detail is not None:
        try:
            return _short(node.detail(ctx, value))
        except Exception:  # 说明文案出错不该影响主流程
            return ""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        # 容器类返回值多半是内部状态，直接 str 会写出一大坨 JSON，反而没有信息量
        return ""
    return _short(value)


# --------------------------------------------------------------------- 兼容投影
def project_last_run(site: Dict[str, Any], run: Run, *, at: int = 0) -> None:
    """把运行结果投影回旧字段，替代原来手写三行的赋值。

    投影是**单向**的：新表是可观测性的真相，旧字段只是给存量前端的一个视图。
    因此任何一次投影都不会反过来覆盖运行记录。

    额外投影 ``last_run_id``：部署日志靠它归属到这次运行（见 ``cert_deploy_log``
    的 ``run_id`` 列）。旧前端不认识这个字段会直接忽略，不影响渲染。
    """
    site["last_run"] = run.as_list()
    site["last_run_id"] = run.id
    site["last_run_at"] = at or run.finished_at or run.started_at or _now()
    site["last_run_status"] = run.status


def run_from_legacy(
    result: Any,
    pipeline: str = "cert-renew",
    *,
    version: int = 1,
    trigger: str = "legacy",
    site_id: str = "",
    owner: str = "",
    run_id: str = "",
    state: Optional[Dict[str, Any]] = None,
) -> Run:
    """把 ``certpipeline.RunResult`` 适配成 :class:`Run`。

    适配层存在的意义：迁移期两条路径并行，只有投影与落库走同一个出口，
    「切换开关前后 last_run 是否一致」才是一个可自动断言的命题
    （见 ``tests/test_certflow.py::test_legacy_projection_is_identical``）。
    """
    steps: List[RunStep] = []
    for item in getattr(result, "steps", []) or []:
        steps.append(
            RunStep(
                node_id=str(getattr(item, "name", "")),
                node_type="legacy",
                status=str(getattr(item, "status", "")),
                detail=str(getattr(item, "detail", "") or ""),
                ms=int(getattr(item, "ms", 0) or 0),
                at=int(getattr(item, "at", 0) or 0),
            )
        )
    exc = getattr(result, "exc", None)
    now = _now()
    return Run(
        id=run_id or new_run_id(),
        pipeline=pipeline,
        version=version,
        status=str(getattr(result, "status", NODE_OK)),
        steps=steps,
        value=getattr(result, "value", None),
        error=str(getattr(result, "error", "") or ""),
        exc=exc,
        trigger=trigger,
        site_id=site_id,
        owner=owner,
        started_at=now,
        finished_at=now,
        state=dict(state or {}),
    )


def projection_of(run: Run) -> Dict[str, Any]:
    """不落库地算出投影结果，供等价性测试与「预览」接口用。"""
    draft: Dict[str, Any] = {}
    project_last_run(draft, run)
    return draft
