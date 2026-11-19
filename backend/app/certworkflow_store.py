"""证书工作流的持久化层。

一张表就够
----------
工作流是**用户配置**（名称、说明、节点图），没有运行实例与产物，因此不像
``certflow_store`` 那样拆成定义 / 运行 / 步骤 / 产物四张表。节点 JSON 存在
``definition`` 列里，列表页需要的节点数量单独放 ``node_count`` 列 —— 列表是
最高频的读，不值得为了一个数字把每行的 MEDIUMTEXT 都解析一遍。

名称唯一性
----------
同一用户下不允许重名：列表与下拉里靠名称区分，重名之后「编辑哪一个」就没有
确定答案。数据库上的唯一键是最后一道防线，接口层会先查一次并给出可读的
``409``；导入 / 模板创建则用 :func:`unique_name` 自动加后缀。
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any, Dict, List, Optional

from . import database
from .certworkflow import MAX_DESCRIPTION, MAX_NAME

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cert_workflow (
    id          VARCHAR(48)  NOT NULL,
    username    VARCHAR(64)  NOT NULL DEFAULT '',
    name        VARCHAR(128) NOT NULL,
    description VARCHAR(512) NOT NULL DEFAULT '',
    version     INT          NOT NULL DEFAULT 1,
    origin      VARCHAR(16)  NOT NULL DEFAULT 'editor',
    node_count  INT          NOT NULL DEFAULT 0,
    definition  MEDIUMTEXT   NOT NULL,
    created_at  BIGINT       NOT NULL DEFAULT 0,
    updated_at  BIGINT       NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uk_cert_workflow_name (username, name),
    KEY idx_cert_workflow_user (username, updated_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

_SUMMARY_COLUMNS = (
    "id, name, description, version, origin, node_count, created_at, updated_at"
)


def _now() -> int:
    return int(time.time())


def _new_id() -> str:
    return "wf_" + secrets.token_hex(8)


def _dump(definition: Dict[str, Any]) -> str:
    return json.dumps(definition, ensure_ascii=False)


def _load(raw: Any) -> Dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except Exception:  # noqa: BLE001 - 脏行不该让整个列表打不开
        logger.warning("工作流定义 JSON 解析失败，按空定义处理", exc_info=True)
        return {}
    return value if isinstance(value, dict) else {}


def _summary(row: Any) -> Dict[str, Any]:
    item = dict(row)
    item["node_count"] = int(item.get("node_count") or 0)
    return item


async def init_table() -> None:
    async with database.connect() as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def list_for(owner: str) -> List[Dict[str, Any]]:
    async with database.connect() as db:
        cursor = await db.execute(
            f"SELECT {_SUMMARY_COLUMNS} FROM cert_workflow"
            " WHERE username = ? ORDER BY updated_at DESC, name",
            (str(owner or ""),),
        )
        rows = await cursor.fetchall()
    return [_summary(row) for row in rows]


async def get(owner: str, workflow_id: str) -> Optional[Dict[str, Any]]:
    async with database.connect() as db:
        cursor = await db.execute(
            "SELECT * FROM cert_workflow WHERE username = ? AND id = ?",
            (str(owner or ""), str(workflow_id or "")),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    item = _summary(row)
    definition = _load(item.pop("definition", ""))
    item["nodes"] = definition.get("nodes") or []
    item["name"] = definition.get("name") or item.get("name") or ""
    item["description"] = definition.get("description") or ""
    return item


async def names(owner: str) -> set:
    async with database.connect() as db:
        cursor = await db.execute(
            "SELECT name FROM cert_workflow WHERE username = ?",
            (str(owner or ""),),
        )
        rows = await cursor.fetchall()
    return {str(row["name"]) for row in rows}


async def name_exists(owner: str, name: str, exclude_id: str = "") -> bool:
    sql = "SELECT id FROM cert_workflow WHERE username = ? AND name = ?"
    params: List[Any] = [str(owner or ""), str(name or "")]
    if exclude_id:
        sql += " AND id <> ?"
        params.append(str(exclude_id))
    sql += " LIMIT 1"
    async with database.connect() as db:
        cursor = await db.execute(sql, tuple(params))
        row = await cursor.fetchone()
    return row is not None


async def create(
    owner: str, definition: Dict[str, Any], origin: str = "editor"
) -> Dict[str, Any]:
    workflow_id = _new_id()
    now = _now()
    nodes = definition.get("nodes") or []
    async with database.connect() as db:
        await db.execute(
            "INSERT INTO cert_workflow"
            " (id, username, name, description, version, origin, node_count,"
            " definition, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                workflow_id,
                str(owner or ""),
                str(definition.get("name") or "")[:MAX_NAME],
                str(definition.get("description") or "")[:MAX_DESCRIPTION],
                1,
                str(origin or "editor")[:16],
                len(nodes),
                _dump(definition),
                now,
                now,
            ),
        )
        await db.commit()
    created = await get(owner, workflow_id)
    return created or {}


async def update(
    owner: str, workflow_id: str, definition: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    nodes = definition.get("nodes") or []
    async with database.connect() as db:
        cursor = await db.execute(
            "UPDATE cert_workflow SET name = ?, description = ?,"
            " node_count = ?, definition = ?, version = version + 1, updated_at = ?"
            " WHERE username = ? AND id = ?",
            (
                str(definition.get("name") or "")[:MAX_NAME],
                str(definition.get("description") or "")[:MAX_DESCRIPTION],
                len(nodes),
                _dump(definition),
                _now(),
                str(owner or ""),
                str(workflow_id or ""),
            ),
        )
        await db.commit()
    if not cursor.rowcount:
        return None
    return await get(owner, workflow_id)


async def delete(owner: str, workflow_id: str) -> bool:
    async with database.connect() as db:
        cursor = await db.execute(
            "DELETE FROM cert_workflow WHERE username = ? AND id = ?",
            (str(owner or ""), str(workflow_id or "")),
        )
        await db.commit()
    return bool(cursor.rowcount)
