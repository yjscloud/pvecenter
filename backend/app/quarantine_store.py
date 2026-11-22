"""隔离登记表：面板执行过的「虚拟机隔离」在库里留一份账。

## 为什么需要这张表

隔离状态本身是**写在 PVE 侧**的（``netX.link_down=1`` + ``protection=1`` +
``quarantine-*`` 取证快照，见 :mod:`app.isolation`），面板不保存副本。但 PVE 的
``/cluster/resources`` 不返回 ``protection``，也不展开 ``netX`` —— 于是：

* 虚拟机列表没法给被隔离的机器打标记（要判断就得逐台读 config，N 台 N 次请求）；
* 应急响应页在刷新后只剩一个空的「隔离状态」，看不出「现在全平台有哪些机器被
  隔离」，处置记录（断了几张网卡 / 取证快照叫什么 / 谁在什么时候做的）也跟着丢了。

这张表补的就是这两件事：隔离成功时记一条，解除隔离 / 删除虚拟机时删掉一条。
它**不是**隔离状态的权威来源 —— 单台的真实状态仍以 PVE 实时读到的为准
（:func:`app.isolation.status`），这里只是「面板登记过的隔离动作」。

## 一致性

* 登记 / 删除都走 best-effort：记不上账不该让隔离这种应急操作失败；
* 表很小（一台被隔离的机器一行），列表请求一次全取，不做 N 次查询；
* 主键是 ``连接 + 节点 + VMID``：多台 PVE 上可以有同名节点 + 同号虚拟机
  （与 :mod:`app.guest_created` 同一个理由）。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import database

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS quarantine_record (
    conn_id      VARCHAR(64)  NOT NULL DEFAULT '',
    node         VARCHAR(128) NOT NULL DEFAULT '',
    vmid         INT          NOT NULL DEFAULT 0,
    name         VARCHAR(255) NOT NULL DEFAULT '',
    snapshot     VARCHAR(128) NOT NULL DEFAULT '',
    interfaces   TEXT,
    power_action VARCHAR(16)  NOT NULL DEFAULT 'none',
    note         VARCHAR(500) NOT NULL DEFAULT '',
    summary      VARCHAR(500) NOT NULL DEFAULT '',
    state        VARCHAR(16)  NOT NULL DEFAULT 'ok',
    started_at   BIGINT       NOT NULL DEFAULT 0,
    recorded_at  DOUBLE       NOT NULL DEFAULT 0,
    recorded_by  VARCHAR(64)  NOT NULL DEFAULT '',
    PRIMARY KEY (conn_id, node, vmid),
    KEY idx_quarantine_record_node (node)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

_COLUMNS = (
    "conn_id, node, vmid, name, snapshot, interfaces, power_action, note, "
    "summary, state, started_at, recorded_at, recorded_by"
)


async def init_table() -> None:
    async with database.connect() as db:
        await db.executescript(SCHEMA)


def _interfaces(value: Any) -> List[str]:
    """``interfaces`` 列存 JSON 数组；读不出来就当空列表（不是致命错误）。"""
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if not value:
        return []
    try:
        data = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]


def _row(row: Any) -> Dict[str, Any]:
    return {
        "conn_id": str(row["conn_id"] or ""),
        "node": str(row["node"] or ""),
        "vmid": int(row["vmid"] or 0),
        "name": str(row["name"] or ""),
        "snapshot": str(row["snapshot"] or ""),
        "interfaces": _interfaces(row["interfaces"]),
        "power_action": str(row["power_action"] or "none"),
        "note": str(row["note"] or ""),
        "summary": str(row["summary"] or ""),
        "state": str(row["state"] or "ok"),
        "started_at": int(row["started_at"] or 0),
        "recorded_at": float(row["recorded_at"] or 0),
        "recorded_by": str(row["recorded_by"] or ""),
    }


def key(conn_id: Any, node: Any, vmid: Any) -> Tuple[str, str, int]:
    try:
        number = int(vmid or 0)
    except (TypeError, ValueError):
        number = 0
    return (str(conn_id or ""), str(node or ""), number)


async def record(
    *,
    conn_id: Any,
    node: Any,
    vmid: Any,
    name: str = "",
    snapshot: str = "",
    interfaces: Optional[Iterable[Any]] = None,
    power_action: str = "none",
    note: str = "",
    summary: str = "",
    state: str = "ok",
    started_at: Any = 0,
    recorded_by: str = "",
) -> None:
    """登记一条隔离记录（同一台机器重复隔离时覆盖）。

    落库失败只记日志：隔离动作本身已经在 PVE 上做完了，不能因为写账失败
    反过来让用户以为隔离没生效。
    """
    conn, host, number = key(conn_id, node, vmid)
    if not host or number <= 0:
        return
    try:
        started = int(started_at or 0) or int(time.time())
    except (TypeError, ValueError):
        started = int(time.time())
    payload = {
        "conn_id": conn,
        "node": host,
        "vmid": number,
        "name": str(name or "")[:255],
        "snapshot": str(snapshot or "")[:128],
        "interfaces": json.dumps(
            [str(item) for item in (interfaces or [])], ensure_ascii=False
        ),
        "power_action": str(power_action or "none")[:16],
        "note": str(note or "")[:500],
        "summary": str(summary or "")[:500],
        "state": str(state or "ok")[:16],
        "started_at": started,
        "recorded_at": time.time(),
        "recorded_by": str(recorded_by or "")[:64],
    }
    columns = [column.strip() for column in _COLUMNS.split(",")]
    try:
        async with database.connect() as db:
            # 先清掉同一台机器在**其它节点**下的旧行：VMID 在一条连接内唯一，
            # 同号只可能是它自己迁移走了。留着旧行，列表会在两个节点上都打
            # 「隔离」标签，解除隔离后也清不干净。
            await db.execute(
                "DELETE FROM quarantine_record "
                "WHERE conn_id = ? AND vmid = ? AND node <> ?",
                (conn, number, host),
            )
            await db.execute(
                "REPLACE INTO quarantine_record (" + _COLUMNS + ") VALUES ("
                + ", ".join("?" for _ in columns)
                + ")",
                tuple(payload[column] for column in columns),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 - 记账失败不影响隔离本身
        logger.warning("记录隔离状态失败（%s/%s）", host, number, exc_info=True)


async def drop(conn_id: Any, node: Any, vmid: Any) -> None:
    """删掉一条记录（解除隔离 / 虚拟机被删除时调）。

    按 ``连接 + VMID`` 删，不按节点：机器迁移过之后行还挂在旧节点下，只按当前
    节点删会留下孤儿行 —— 那是「解除隔离后标签还在」的来源。
    """
    conn, host, number = key(conn_id, node, vmid)
    try:
        async with database.connect() as db:
            await db.execute(
                "DELETE FROM quarantine_record WHERE conn_id = ? AND vmid = ?",
                (conn, number),
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.debug("删除隔离记录失败（%s/%s）", host, number, exc_info=True)


async def get(conn_id: Any, node: Any, vmid: Any) -> Optional[Dict[str, Any]]:
    conn, host, number = key(conn_id, node, vmid)
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                # 按「连接 + VMID」查，不看节点：机器迁移过之后行还挂在旧节点下，
                # 按当前节点查会让删除守卫认不出「这台还在隔离中」。
                "SELECT " + _COLUMNS + " FROM quarantine_record "
                "WHERE conn_id = ? AND vmid = ?",
                (conn, number),
            )
            row = await cursor.fetchone()
    except Exception:  # noqa: BLE001 - 读不到就当没有登记
        logger.debug("读取隔离记录失败（%s/%s）", host, number, exc_info=True)
        return None
    return _row(row) if row else None


async def list_records() -> List[Dict[str, Any]]:
    """全部登记记录，最近隔离的排前面。"""
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "SELECT " + _COLUMNS + " FROM quarantine_record "
                "ORDER BY started_at DESC"
            )
            rows = await cursor.fetchall()
    except Exception:  # noqa: BLE001 - 读不到就退化成一页「没有隔离记录」
        logger.debug("读取隔离记录失败", exc_info=True)
        return []
    return [_row(row) for row in rows or []]


async def index() -> Dict[Tuple[str, int], str]:
    """``{(连接, VMID): 登记的节点}``：虚拟机列表打标记用，一次查完。

    键刻意不含节点：VMID 在一条连接（= 一个 PVE 集群）内唯一，节点只是位置。
    机器迁移之后登记行里仍是旧节点，按「连接 + VMID」匹配才不会丢掉隔离标签。
    """
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "SELECT conn_id, node, vmid FROM quarantine_record"
            )
            rows = await cursor.fetchall()
    except Exception:  # noqa: BLE001 - 附加字段，读不到就当没有隔离
        logger.debug("读取隔离记录键失败", exc_info=True)
        return {}
    return {
        (str(row["conn_id"] or ""), int(row["vmid"] or 0)): str(row["node"] or "")
        for row in rows or []
    }
