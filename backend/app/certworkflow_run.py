"""证书工作流的运行实例：发起、审批、逐步流转。

设计边界
--------
本模块只做**状态机**：定义描述「应该做哪些步骤」，运行实例停在 ``pending``
等审批，通过后一步步推进。每一步真正做什么由 :mod:`app.certworkflow_exec`
在执行 ``advance`` 时调用 —— 签发 / 部署 / 同步 / 通知都会走 ``certs.run_manual``，
因此工作流触发的动作与手动按钮共用同一套运行日志与通知。

状态机保持纯函数（不碰网络与证书模块），执行器单独一层，两边都能独立测试。

状态机
------
``pending``  待审批：发起后停在这里，等待审批人通过或驳回；
``running``  进行中：通过审批后自动启动第 1 步，操作者逐步 ``advance``；
``succeeded`` 已完成：最后一步确认完成；
``rejected`` 已驳回：审批阶段驳回，不再流转；
``cancelled`` 已取消：待审批 / 进行中都可以取消。

步骤快照
--------
``steps`` 是 JSON 列，保存节点类型、标题和每一步的状态 / 时间 / 备注。
运行实例不引用工作流定义 —— 定义后续被编辑或删除，也不会改变已经发起的流程。
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any, Dict, List, Optional

from . import database

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cert_workflow_run (
    id            VARCHAR(48)  NOT NULL,
    username      VARCHAR(64)  NOT NULL DEFAULT '',
    workflow_id   VARCHAR(48)  NOT NULL DEFAULT '',
    workflow_name VARCHAR(128) NOT NULL DEFAULT '',
    site_id       VARCHAR(64)  NOT NULL DEFAULT '',
    site_domain   VARCHAR(255) NOT NULL DEFAULT '',
    cert_run_id   VARCHAR(48)  NOT NULL DEFAULT '',
    title         VARCHAR(255) NOT NULL DEFAULT '',
    status        VARCHAR(24)  NOT NULL DEFAULT 'pending',
    current_index INT          NOT NULL DEFAULT -1,
    steps         MEDIUMTEXT   NOT NULL,
    requester     VARCHAR(64)  NOT NULL DEFAULT '',
    approver      VARCHAR(64)  NOT NULL DEFAULT '',
    request_note  TEXT         NOT NULL,
    approval_note TEXT         NOT NULL,
    created_at    BIGINT       NOT NULL DEFAULT 0,
    updated_at    BIGINT       NOT NULL DEFAULT 0,
    started_at    BIGINT       NOT NULL DEFAULT 0,
    finished_at   BIGINT       NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    KEY idx_cert_workflow_run_user (username, updated_at DESC),
    KEY idx_cert_workflow_run_site (username, site_id, updated_at DESC),
    KEY idx_cert_workflow_run_status (username, status, updated_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"

RUN_STATUSES = (
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_REJECTED,
    STATUS_CANCELLED,
)

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_SKIPPED = "skipped"

ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
ACTION_ADVANCE = "advance"
ACTION_CANCEL = "cancel"

RUN_ACTIONS: Dict[str, List[str]] = {
    STATUS_PENDING: [ACTION_APPROVE, ACTION_REJECT, ACTION_CANCEL],
    STATUS_RUNNING: [ACTION_ADVANCE, ACTION_CANCEL],
}


class RunError(Exception):
    """带稳定错误码的流转错误，路由层负责翻译成 HTTP 状态码。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> int:
    return int(time.time())


def _new_id() -> str:
    return "run_" + secrets.token_hex(8)


def _dump_steps(steps: List[Dict[str, Any]]) -> str:
    return json.dumps(steps, ensure_ascii=False)


def _load_steps(raw: Any) -> List[Dict[str, Any]]:
    try:
        value = json.loads(str(raw or "[]"))
    except Exception:  # noqa: BLE001 - 单行脏数据不该让整个列表打不开
        logger.warning("工作流运行步骤 JSON 解析失败，按空步骤处理", exc_info=True)
        return []
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def ordered_nodes(definition: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按依赖关系排节点；同层保持定义里的原始顺序。"""
    raw_nodes = definition.get("nodes") if isinstance(definition, dict) else []
    nodes = [dict(item) for item in (raw_nodes or []) if isinstance(item, dict)]
    by_id: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for node in nodes:
        node_id = str(node.get("id") or "")
        if not node_id or node_id in by_id:
            continue
        by_id[node_id] = node
        order.append(node_id)

    indegree = {node_id: 0 for node_id in order}
    children: Dict[str, List[str]] = {node_id: [] for node_id in order}
    for node_id in order:
        needs = by_id[node_id].get("needs") or []
        for need in needs:
            need_id = str(need or "")
            if need_id in by_id and need_id != node_id:
                indegree[node_id] += 1
                children[need_id].append(node_id)

    ready = [node_id for node_id in order if indegree[node_id] == 0]
    result: List[str] = []
    while ready:
        node_id = ready.pop(0)
        result.append(node_id)
        for child in children[node_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)

    # 正常定义在保存前已经拒绝成环；若历史数据有环，剩余节点按原顺序追加，
    # 至少保证每一步都能被看到和操作，而不是整条流程没有步骤。
    if len(result) < len(order):
        seen = set(result)
        result.extend(node_id for node_id in order if node_id not in seen)
    return [by_id[node_id] for node_id in result]


def build_steps(definition: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "node_id": str(node.get("id") or ""),
            "type": str(node.get("type") or ""),
            "title": str(node.get("title") or ""),
            "status": STEP_PENDING,
            "note": "",
            "started_at": 0,
            "finished_at": 0,
        }
        for node in ordered_nodes(definition)
    ]


def _view(row: Any) -> Dict[str, Any]:
    item = dict(row)
    for key in (
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
    ):
        item[key] = int(item.get(key) or 0)
    # current_index 的 -1 是「还没有启动」，不能用 ``or 0`` 吞掉
    item["current_index"] = int(
        item.get("current_index") if item.get("current_index") is not None else -1
    )
    item["steps"] = _load_steps(item.get("steps"))
    item["actions"] = list(RUN_ACTIONS.get(str(item.get("status")), []))
    item.pop("username", None)
    return item


def _validate_action(status: str, action: str) -> None:
    if action not in (
        ACTION_APPROVE,
        ACTION_REJECT,
        ACTION_ADVANCE,
        ACTION_CANCEL,
    ):
        raise RunError("invalid_action")
    if action not in RUN_ACTIONS.get(status, []):
        raise RunError("status_conflict")


def apply_action(
    run: Dict[str, Any],
    action: str,
    *,
    note: str = "",
    operator: str = "",
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """在内存里推进状态机，返回新的运行字典（不写库，便于单元测试）。"""
    current = dict(run)
    status = str(current.get("status") or "")
    _validate_action(status, action)
    stamp = _now() if now is None else int(now)
    steps = [dict(step) for step in (current.get("steps") or [])]
    raw_index = current.get("current_index")
    current_index = int(raw_index) if raw_index is not None else -1
    message = str(note or "").strip()

    if action == ACTION_APPROVE:
        if not steps:
            raise RunError("no_nodes")
        steps[0]["status"] = STEP_RUNNING
        steps[0]["started_at"] = stamp
        current.update(
            status=STATUS_RUNNING,
            current_index=0,
            approver=operator,
            approval_note=message,
            started_at=stamp,
        )
    elif action == ACTION_REJECT:
        current.update(
            status=STATUS_REJECTED,
            approver=operator,
            approval_note=message,
            finished_at=stamp,
        )
    elif action == ACTION_CANCEL:
        if status == STATUS_RUNNING and 0 <= current_index < len(steps):
            steps[current_index]["status"] = STEP_SKIPPED
            steps[current_index]["finished_at"] = stamp
            if message:
                steps[current_index]["note"] = message
        current.update(
            status=STATUS_CANCELLED,
            finished_at=stamp,
        )
    else:  # advance
        if not (0 <= current_index < len(steps)):
            raise RunError("status_conflict")
        steps[current_index]["status"] = STEP_DONE
        steps[current_index]["finished_at"] = stamp
        if message:
            steps[current_index]["note"] = message
        if current_index + 1 >= len(steps):
            current.update(
                status=STATUS_SUCCEEDED,
                current_index=len(steps),
                finished_at=stamp,
            )
        else:
            next_index = current_index + 1
            steps[next_index]["status"] = STEP_RUNNING
            steps[next_index]["started_at"] = stamp
            current.update(current_index=next_index)

    current["steps"] = steps
    current["updated_at"] = stamp
    return current


async def attach_cert_run(owner: str, run_id: str, cert_run_id: str) -> bool:
    """把执行产生的证书流水运行 id 记到工作流实例上（详情页互跳）。"""
    if not run_id or not cert_run_id:
        return False
    async with database.connect() as db:
        cursor = await db.execute(
            "UPDATE cert_workflow_run SET cert_run_id = ?, updated_at = ?"
            " WHERE username = ? AND id = ?",
            (str(cert_run_id)[:48], _now(), str(owner or ""), str(run_id)),
        )
        await db.commit()
    return bool(cursor.rowcount)


async def init_table() -> None:
    async with database.connect() as db:
        await db.executescript(SCHEMA)
        # 旧库补列：工作流步骤与证书流水运行关联（详情页互跳用）
        columns = await database.table_columns(db, "cert_workflow_run")
        if "cert_run_id" not in columns:
            await db.execute(
                "ALTER TABLE cert_workflow_run"
                " ADD COLUMN cert_run_id VARCHAR(48) NOT NULL DEFAULT ''"
            )
        await db.commit()


async def list_runs(
    owner: str,
    *,
    site_id: str = "",
    status: str = "",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM cert_workflow_run WHERE username = ?"
    params: List[Any] = [str(owner or "")]
    if site_id:
        sql += " AND site_id = ?"
        params.append(str(site_id))
    if status:
        sql += " AND status = ?"
        params.append(str(status))
    sql += " ORDER BY updated_at DESC, created_at DESC LIMIT ?"
    params.append(max(1, min(int(limit or 100), 200)))
    async with database.connect() as db:
        cursor = await db.execute(sql, tuple(params))
        rows = await cursor.fetchall()
    return [_view(row) for row in rows]


async def delete_run(owner: str, run_id: str) -> bool:
    """删除一条审批实例（按用户名隔离）。

    只删 ``cert_workflow_run`` 这一行：它引用的证书流水运行（``cert_run_id``）
    有独立的保留策略，删审批记录不该把证书运行的日志一起带走。
    """
    async with database.connect() as db:
        cursor = await db.execute(
            "DELETE FROM cert_workflow_run WHERE username = ? AND id = ?",
            (str(owner or ""), str(run_id or "")),
        )
        await db.commit()
    return bool(cursor.rowcount)


async def delete_for_site(site_id: str) -> int:
    """删掉某个站点名下的全部审批实例（站点被删除时调用）。

    这些实例引用的是一个已经不存在的 ``site_id``：留着它们，运行列表里会一直
    挂着「一台不存在的机器的审批」，点进去还会去查证书详情（404）。
    """
    sid = str(site_id or "")
    if not sid:
        return 0
    async with database.connect() as db:
        cursor = await db.execute(
            "DELETE FROM cert_workflow_run WHERE site_id = ?", (sid,)
        )
        await db.commit()
    return cursor.rowcount or 0


async def delete_for_workflow(owner: str, workflow_id: str) -> int:
    """删掉某个工作流名下的全部审批实例（工作流定义被删除时调用）。"""
    async with database.connect() as db:
        cursor = await db.execute(
            "DELETE FROM cert_workflow_run WHERE username = ? AND workflow_id = ?",
            (str(owner or ""), str(workflow_id or "")),
        )
        await db.commit()
    return cursor.rowcount or 0


async def count_active(owner: str, workflow_id: str) -> int:
    """该工作流下还有多少「待审批 / 进行中」的实例。

    删除定义前用它拦一道：直接删会让这些实例永远停在半路（既推进不了，也
    没人能批准），用户看到的是「卡住的审批」而不是「工作流没了」。
    """
    async with database.connect() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) AS n FROM cert_workflow_run"
            " WHERE username = ? AND workflow_id = ? AND status IN (?, ?)",
            (
                str(owner or ""),
                str(workflow_id or ""),
                STATUS_PENDING,
                STATUS_RUNNING,
            ),
        )
        row = await cursor.fetchone()
    return int((row or {}).get("n") or 0)


async def stats_for(owner: str) -> Dict[str, int]:
    async with database.connect() as db:
        cursor = await db.execute(
            "SELECT status, COUNT(*) AS n FROM cert_workflow_run"
            " WHERE username = ? GROUP BY status",
            (str(owner or ""),),
        )
        rows = await cursor.fetchall()
    result = {status: 0 for status in RUN_STATUSES}
    for row in rows:
        key = str(row["status"])
        if key in result:
            result[key] = int(row["n"] or 0)
    result["total"] = sum(result.values())
    result["active"] = result[STATUS_PENDING] + result[STATUS_RUNNING]
    return result


async def get_run(owner: str, run_id: str) -> Optional[Dict[str, Any]]:
    async with database.connect() as db:
        cursor = await db.execute(
            "SELECT * FROM cert_workflow_run WHERE username = ? AND id = ?",
            (str(owner or ""), str(run_id or "")),
        )
        row = await cursor.fetchone()
    return _view(row) if row is not None else None


async def create_run(
    owner: str,
    workflow_id: str,
    workflow_name: str,
    definition: Dict[str, Any],
    *,
    site_id: str = "",
    site_domain: str = "",
    title: str = "",
    requester: str = "",
    note: str = "",
) -> Dict[str, Any]:
    steps = build_steps(definition)
    if not steps:
        raise RunError("no_nodes")
    stamp = _now()
    run_id = _new_id()
    record = {
        "id": run_id,
        "username": str(owner or ""),
        "workflow_id": str(workflow_id or ""),
        "workflow_name": str(workflow_name or "")[:128],
        "site_id": str(site_id or ""),
        "site_domain": str(site_domain or "")[:255],
        "title": (str(title or "").strip() or str(workflow_name or "")[:255])[:255],
        "status": STATUS_PENDING,
        "current_index": -1,
        "steps": _dump_steps(steps),
        "requester": str(requester or owner or "")[:64],
        "approver": "",
        "request_note": str(note or "").strip(),
        "approval_note": "",
        "created_at": stamp,
        "updated_at": stamp,
        "started_at": 0,
        "finished_at": 0,
    }
    async with database.connect() as db:
        await db.execute(
            "INSERT INTO cert_workflow_run (id, username, workflow_id, workflow_name,"
            " site_id, site_domain, title, status, current_index, steps, requester,"
            " approver, request_note, approval_note, created_at, updated_at, started_at,"
            " finished_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(
                record[key]
                for key in (
                    "id",
                    "username",
                    "workflow_id",
                    "workflow_name",
                    "site_id",
                    "site_domain",
                    "title",
                    "status",
                    "current_index",
                    "steps",
                    "requester",
                    "approver",
                    "request_note",
                    "approval_note",
                    "created_at",
                    "updated_at",
                    "started_at",
                    "finished_at",
                )
            ),
        )
        await db.commit()
    return _view(record)


async def transition_run(
    owner: str,
    run_id: str,
    action: str,
    *,
    note: str = "",
    operator: str = "",
) -> Dict[str, Any]:
    """在数据库行锁内推进状态，避免两次点击同时通过审批。"""
    async with database.connect() as db:
        cursor = await db.execute(
            "SELECT * FROM cert_workflow_run WHERE username = ? AND id = ? FOR UPDATE",
            (str(owner or ""), str(run_id or "")),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RunError("not_found")
        current = _view(row)
        updated = apply_action(current, action, note=note, operator=operator)
        await db.execute(
            "UPDATE cert_workflow_run SET status = ?, current_index = ?, steps = ?,"
            " approver = ?, approval_note = ?, updated_at = ?, started_at = ?,"
            " finished_at = ? WHERE id = ? AND username = ?",
            (
                updated.get("status"),
                int(
                    updated.get("current_index")
                    if updated.get("current_index") is not None
                    else -1
                ),
                _dump_steps(updated.get("steps") or []),
                str(updated.get("approver") or ""),
                str(updated.get("approval_note") or ""),
                int(updated.get("updated_at") or 0),
                int(updated.get("started_at") or 0),
                int(updated.get("finished_at") or 0),
                str(run_id or ""),
                str(owner or ""),
            ),
        )
        await db.commit()
    return _view(updated)
