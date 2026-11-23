"""备份防删 / 防篡改检测（防勒索）。

## 先说清楚这东西**不是**什么

真正的「不可变备份」（WORM）必须由**存储侧**保证：PBS 的 retention/immutability、
S3 Object Lock、或者只读挂载的文件系统。**只靠 PVE API 做不到**，我也不打算假装
做到了。所以这里实现的是「**登记 + 核对 + 面板层禁止删除**」三位一体的检测型防护：

1. **登记**：把某个备份卷登记为「受保护」，同时尽力给 PVE 卷打上 ``protected``
   旗标（能拦住 PVE 的 prune 与常规删除，但 root 仍可清掉）；
2. **面板层拦截**：面板自己的删除接口会拒删受保护备份 —— 勒索软件即使拿到了
   面板的管理员会话，「先删备份再加密」这条路也走不通，必须先在界面上解除保护
   （两次明确操作）；
3. **核对与告警**：定期把登记过的备份与 PVE 实际内容对一遍，**不见了**或
   **元数据变了**（大小 / 创建时间）就发告警 —— 这是「备份被动过」时唯一的信号。

## 关于「指纹」的一个诚实说明

PVE 的 storage content **不返回内容校验和**（``verification`` 只有 PBS 存储才有）。
所以 ``meta_fingerprint`` 取的是 ``volid|size|ctime`` 的哈希：它能发现**删除、
替换、元数据变化**，**不能**发现存储层的静默位翻转（bit rot）。要后者请接入 PBS。
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Dict, List, Optional

from . import alerting, database, i18n, store

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS protected_backups (
    connection       VARCHAR(64)  NOT NULL DEFAULT '',
    volid            VARCHAR(255) NOT NULL,
    node             VARCHAR(128) NOT NULL DEFAULT '',
    storage          VARCHAR(128) NOT NULL DEFAULT '',
    vmid             VARCHAR(32),
    size             BIGINT       NOT NULL DEFAULT 0,
    ctime            BIGINT       NOT NULL DEFAULT 0,
    meta_fingerprint VARCHAR(64)  NOT NULL DEFAULT '',
    note             VARCHAR(255) NOT NULL DEFAULT '',
    created          DOUBLE       NOT NULL DEFAULT 0,
    created_by       VARCHAR(64)  NOT NULL DEFAULT '',
    last_seen        DOUBLE       NOT NULL DEFAULT 0,
    state            VARCHAR(16)  NOT NULL DEFAULT 'ok',
    state_detail     VARCHAR(500) NOT NULL DEFAULT '',
    pve_protected    TINYINT      NOT NULL DEFAULT 0,
    PRIMARY KEY (connection, volid),
    KEY idx_protected_state (state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

#: 主键的列顺序（迁移判断用）。
_KEY_COLUMNS = ("connection", "volid")


async def init_table() -> None:
    async with database.connect() as db:
        await db.executescript(SCHEMA)
        # 旧库迁移：早期主键只有 ``volid``。而 volid 只含「存储名 + 文件名」，
        # 两套 PVE 上同名存储、同号机器、备份时间落在同一秒时会是同一个字符串 ——
        # 于是共用一条保护记录：is_protected 误判、核对状态互相覆盖。
        # 补 connection 列后把主键换成复合键，旧记录一律归到「空连接」
        # （= 当时用的是当前连接），行为与升级前一致。
        await database.add_columns(
            db,
            "protected_backups",
            [
                (
                    "connection",
                    "ALTER TABLE protected_backups "
                    "ADD COLUMN connection VARCHAR(64) NOT NULL DEFAULT ''",
                )
            ],
        )
        cursor = await db.execute(
            "SELECT COLUMN_NAME FROM information_schema.key_column_usage"
            " WHERE table_schema = DATABASE()"
            " AND table_name = 'protected_backups'"
            " AND constraint_name = 'PRIMARY'"
            " ORDER BY ordinal_position"
        )
        rows = await cursor.fetchall()
        key_columns = tuple(str(row["COLUMN_NAME"]) for row in rows)
        if key_columns != _KEY_COLUMNS:
            await db.execute(
                "ALTER TABLE protected_backups DROP PRIMARY KEY,"
                " ADD PRIMARY KEY (connection, volid)"
            )
        await db.commit()


def meta_fingerprint(volid: str, size: Any, ctime: Any) -> str:
    """``volid|size|ctime`` 的哈希。

    只覆盖元数据 —— PVE 拿不到内容校验和，所以这里**不承诺**能发现静默损坏。
    """
    raw = f"{volid}|{int(size or 0)}|{int(ctime or 0)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    return {
        "connection": str(row[0] or ""),
        "volid": str(row[1]),
        "node": row[2] or "",
        "storage": row[3] or "",
        "vmid": row[4] or "",
        "size": int(row[5] or 0),
        "ctime": int(row[6] or 0),
        "meta_fingerprint": row[7] or "",
        "note": row[8] or "",
        "created": float(row[9] or 0),
        "created_by": row[10] or "",
        "last_seen": float(row[11] or 0),
        "state": row[12] or "ok",
        "state_detail": row[13] or "",
        "pve_protected": bool(row[14]),
    }


_COLUMNS = (
    "connection, volid, node, storage, vmid, size, ctime, meta_fingerprint, note, "
    "created, created_by, last_seen, state, state_detail, pve_protected"
)


async def get_record(
    volid: str, connection_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """按卷读登记记录。

    ``connection_id`` 为空 / ``None`` 时**不限定连接**：删除拦截走这条路，
    volid 本身不含连接信息，漏判等于放行了受保护备份（宁严不漏）。需要精确
    定位（解除保护、核对）时再显式传连接。
    """
    if connection_id:
        sql = (
            f"SELECT {_COLUMNS} FROM protected_backups"
            " WHERE volid = ? AND connection = ?"
        )
        params: tuple = (volid, connection_id)
    else:
        sql = (
            f"SELECT {_COLUMNS} FROM protected_backups"
            " WHERE volid = ? ORDER BY created DESC LIMIT 1"
        )
        params = (volid,)
    async with database.connect() as db:
        cursor = await db.execute(sql, params)
        row = await cursor.fetchone()
    return _row(row) if row else None


async def list_records(connection_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """全部受保护备份；``connection_id`` 非空时只列那一套集群。"""
    async with database.connect() as db:
        if connection_id:
            cursor = await db.execute(
                f"SELECT {_COLUMNS} FROM protected_backups"
                " WHERE connection = ? ORDER BY created DESC",
                (connection_id,),
            )
        else:
            cursor = await db.execute(
                f"SELECT {_COLUMNS} FROM protected_backups ORDER BY created DESC"
            )
        rows = await cursor.fetchall()
    return [_row(row) for row in rows]


async def is_protected(volid: str, connection_id: Optional[str] = None) -> bool:
    """面板层删除拦截用：登记过就算受保护（不看当前核对状态）。"""
    return await get_record(volid, connection_id) is not None


async def protect(
    node: str,
    storage: str,
    volid: str,
    *,
    vmid: Optional[Any] = None,
    note: str = "",
    username: str = "",
    connection_id: str = "",
) -> Dict[str, Any]:
    """登记为受保护备份，并尽力给 PVE 卷打上 ``protected`` 旗标。

    读一次 PVE 侧的大小与创建时间作为基线指纹；读不到（卷刚消失 / 权限不足）也照
    登记，只是状态标成 ``unknown`` —— 核对时会把差异报出来，比默默放过好。

    ``connection_id`` 是这条备份所在的 PVE：与 volid 一起构成主键。两套集群上
    同名存储 + 同号机器 + 同一秒的备份会得到同一个 volid，只按 volid 存会互相
    覆盖（一处解除保护，另一处的防删保护也没了）。
    """
    from .pve import ProxmoxError, client_for_connection, get_client

    size = 0
    ctime = 0
    pve_protected = False
    detail = ""
    client = None
    try:
        client = client_for_connection(connection_id) if connection_id else get_client()
        items = await client.storage_content(node, storage, content="backup")
        found = next((item for item in items if str(item.get("volid")) == volid), None)
        if found is None:
            return {
                "ok": False,
                "volid": volid,
                "detail": f"在 {node}/{storage} 上找不到备份卷 {volid}，未登记",
            }
        size = int(found.get("size") or 0)
        ctime = int(found.get("ctime") or 0)
        pve_protected = bool(found.get("protected"))
    except ProxmoxError as exc:
        detail = f"读取备份信息失败（仍登记，核对时再确认）：{exc}"

    if not pve_protected and client is not None:
        try:
            await client.storage_content_set_protected(node, storage, volid, True)
            pve_protected = True
        except Exception as exc:  # noqa: BLE001 - 旧版 PVE 不支持该字段是常态
            detail = (detail + "；" if detail else "") + f"PVE 未接受保护旗标：{exc}"

    record = {
        "connection": str(connection_id or ""),
        "volid": volid,
        "node": node,
        "storage": storage,
        "vmid": str(vmid) if vmid else "",
        "size": size,
        "ctime": ctime,
        "meta_fingerprint": meta_fingerprint(volid, size, ctime),
        "note": note[:255],
        "created": time.time(),
        "created_by": username,
        "last_seen": time.time(),
        "state": "ok",
        "state_detail": detail[:500],
        "pve_protected": pve_protected,
    }
    async with database.connect() as db:
        await db.execute(
            database.upsert_sql(
                "protected_backups",
                [
                    "connection", "volid", "node", "storage", "vmid", "size",
                    "ctime", "meta_fingerprint", "note", "created", "created_by",
                    "last_seen", "state", "state_detail", "pve_protected",
                ],
                ["connection", "volid"],
                [
                    "node", "storage", "vmid", "size", "ctime", "meta_fingerprint",
                    "note", "created_by", "last_seen", "state", "state_detail",
                    "pve_protected",
                ],
            ),
            (
                record["connection"], record["volid"], record["node"],
                record["storage"], record["vmid"], record["size"], record["ctime"],
                record["meta_fingerprint"], record["note"], record["created"],
                record["created_by"], record["last_seen"], record["state"],
                record["state_detail"], 1 if record["pve_protected"] else 0,
            ),
        )
        await db.commit()
    return {
        "ok": True,
        "record": record,
        "detail": (
            "已登记为受保护备份"
            + ("（PVE 侧已打上保护旗标）" if pve_protected else "（PVE 侧未打上保护旗标）")
            + (f"；{detail}" if detail else "")
        ),
    }


async def unprotect(
    volid: str,
    *,
    release_pve_flag: bool = True,
    connection_id: Optional[str] = None,
) -> Dict[str, Any]:
    """解除登记（并尽力解除 PVE 侧的保护旗标）。

    ``connection_id`` 为空时按 volid 找到那条记录，再用**记录里的连接**精确
    删除：老前端不带连接也能用；两套集群有同 volid 的记录时至少只动一条。
    """
    from .pve import ProxmoxError, client_for_connection, get_client

    record = await get_record(volid, connection_id)
    if not record:
        return {"ok": False, "volid": volid, "detail": "该备份不在受保护清单里"}
    conn = str(record.get("connection") or "")
    if release_pve_flag and record["pve_protected"]:
        try:
            client = client_for_connection(conn) if conn else get_client()
            await client.storage_content_set_protected(
                record["node"], record["storage"], volid, False
            )
        except (ProxmoxError, Exception) as exc:  # noqa: BLE001
            logger.info("解除 PVE 保护旗标失败 %s：%s", volid, exc)
    async with database.connect() as db:
        await db.execute(
            "DELETE FROM protected_backups WHERE volid = ? AND connection = ?",
            (volid, conn),
        )
        await db.commit()
    return {"ok": True, "volid": volid, "detail": "已从受保护清单移除"}


async def reconcile() -> Dict[str, Any]:
    """把登记过的备份与 PVE 实际内容核对一遍，刷新状态。

    按 ``(连接, node, storage)`` 分组只拉一次存储内容，避免逐个卷去查；
    分组必须带连接 —— 同名节点 + 同名存储在两套 PVE 上是两份独立数据。
    """
    from .pve import ProxmoxError, client_for_connection, get_client

    records = await list_records()
    if not records:
        return {"checked": 0, "ok": 0, "missing": 0, "changed": 0, "unknown": 0, "items": []}

    groups: Dict[Any, List[Dict[str, Any]]] = {}
    for record in records:
        groups.setdefault(
            (record.get("connection") or "", record["node"], record["storage"]), []
        ).append(record)

    items: List[Dict[str, Any]] = []
    now = time.time()
    for (conn_id, node, storage), group in groups.items():
        index: Dict[str, Dict[str, Any]] = {}
        error = ""
        try:
            client = client_for_connection(conn_id) if conn_id else get_client()
            for entry in await client.storage_content(node, storage, content="backup"):
                index[str(entry.get("volid"))] = entry
        except ProxmoxError as exc:
            error = str(exc)
        for record in group:
            volid = record["volid"]
            if error:
                state, detail = "unknown", f"无法读取 {node}/{storage}：{error}"
            elif volid not in index:
                state, detail = "missing", "备份卷已不存在（被删除或存储不可达）"
            else:
                entry = index[volid]
                fresh = meta_fingerprint(volid, entry.get("size"), entry.get("ctime"))
                if fresh != record["meta_fingerprint"]:
                    state = "changed"
                    detail = (
                        f"备份元数据与登记时不一致（登记 size={record['size']} "
                        f"ctime={record['ctime']}，现在 size={entry.get('size')} "
                        f"ctime={entry.get('ctime')}）"
                    )
                else:
                    state, detail = "ok", "与登记信息一致"
            async with database.connect() as db:
                await db.execute(
                    "UPDATE protected_backups SET last_seen = ?, state = ?, state_detail = ? "
                    "WHERE volid = ? AND connection = ?",
                    (now, state, detail[:500], volid, conn_id),
                )
                await db.commit()
            items.append(
                {
                    "connection": conn_id,
                    "volid": volid,
                    "state": state,
                    "detail": detail,
                }
            )

    counts = {key: sum(1 for item in items if item["state"] == key) for key in ("ok", "missing", "changed", "unknown")}
    return {"checked": len(items), **counts, "items": items}


def _alert_text(record: Dict[str, Any]) -> str:
    if record["state"] == "missing":
        return i18n.pick(
            f"受保护备份不见了：{record['volid']}\n"
            f"存储：{record['node']}/{record['storage']}；登记时间："
            f"{alerting._now_text(record['created'])}；登记人：{record['created_by'] or '-'}\n"
            "这可能是勒索软件在加密前先删备份，请立刻确认存储侧发生了什么。",
            f"A protected backup is gone: {record['volid']}\n"
            f"Storage: {record['node']}/{record['storage']}; registered at: "
            f"{alerting._now_text(record['created'])}; by: "
            f"{record['created_by'] or '-'}\n"
            "Ransomware often deletes backups before encrypting — check what "
            "happened on the storage side right away.",
        )
    return i18n.pick(
        f"受保护备份被改动：{record['volid']}\n"
        f"存储：{record['node']}/{record['storage']}\n原因：{record['state_detail']}",
        f"A protected backup was modified: {record['volid']}\n"
        f"Storage: {record['node']}/{record['storage']}\n"
        f"Reason: {record['state_detail']}",
    )


def _card(record: Dict[str, Any], at: float) -> Dict[str, Any]:
    missing = record["state"] == "missing"
    return alerting.build_card(
        "critical" if missing else "warning",
        ("🔴 " if missing else "🟠 ") + i18n.tr("pvecenter 备份防护告警"),
        i18n.tr("受保护备份丢失") if missing else i18n.tr("受保护备份异常"),
        [
            (i18n.tr("备份卷"), record["volid"]),
            (i18n.tr("存储"), f"{record['node']}/{record['storage']}"),
            (
                i18n.tr("状态"),
                "**" + i18n.tr("已丢失") + "**"
                if missing
                else "**" + i18n.tr("元数据变化") + "**",
            ),
            (i18n.tr("原因"), record["state_detail"] or "-"),
            (
                i18n.tr("登记人 / 时间"),
                f"{record['created_by'] or '-'} · {alerting._now_text(record['created'])}",
            ),
        ],
        i18n.pick(
            f"触发时间 {alerting._now_text(at)} · 面板层已禁止删除受保护备份，"
            "但存储侧仍可能被绕过，请人工确认",
            f"Triggered at {alerting._now_text(at)} · the panel forbids deleting "
            "protected backups, but the storage layer can still be bypassed — "
            "confirm it manually",
        ),
    )


async def evaluate(owner: Optional[str] = None) -> List[Dict[str, Any]]:
    """核对受保护备份并推送异常；``owner=None`` 发给第一个管理员。

    照 :func:`app.sshguard.evaluate` 的写法：自建 rule_id、自己判冷却、复用告警的
    去重 / 飞书邮件 / 恢复通知。
    """
    result = await reconcile()
    # 键带连接：两套集群的同一个 volid 是两条**独立**记录，按 volid 合并会让
    # 一条恢复把另一条的告警也清掉。
    records = {
        f"{item.get('connection') or ''}|{item['volid']}": item
        for item in result["items"]
    }
    target_owner = owner or await store.first_admin_username() or ""
    if not target_owner:
        return []
    # 按收件人的语言渲染（巡检是后台跑的，没有请求上下文，见 alerting.resolve_language）
    i18n.pin_language(await alerting.resolve_language(target_owner))

    feishu = await alerting.load_feishu(target_owner)
    email_cfg = await alerting.load_alert_email(target_owner)
    # 显式读一份 Webhook：dispatch 不传时会自己读，但历史里的渠道要按「实际
    # 传给 dispatch 的那份配置」算（见 alerting.channel_ids），必须握在手上。
    webhook_cfg = await alerting.load_webhook(target_owner)
    now = time.time()
    # 备份丢失/被改动是「状态型」异常：有变化才有意义，冷却用 10 分钟
    cooldown = 600

    active = await alerting.load_active()
    active = {
        key: row for key, row in active.items() if str(row.get("rule_id")) == "backup-protect"
    }
    still_alarming: set = set()
    fired: List[Dict[str, Any]] = []

    for record_key, record in records.items():
        volid = str(record["volid"])
        if record["state"] not in ("missing", "changed"):
            continue
        key = alerting.alarm_key(target_owner, "backup-protect", record_key)
        still_alarming.add(key)
        # prev 非空 = 上一轮就在告警中，这一轮只是重复提醒（不写新历史）
        prev = active.get(key) or {}
        last = float(prev.get("ts") or 0)
        if now - last < cooldown:
            continue
        text = _alert_text(record)
        ok, detail = await alerting.dispatch(
            target_owner,
            feishu,
            email_cfg,
            i18n.tr("备份防护告警：") + volid,
            text,
            _card(record, now),
            source=alerting.SOURCE_BACKUPGUARD,
            webhook=webhook_cfg,
        )
        state = {
            "username": target_owner,
            "rule_id": "backup-protect",
            "rule_name": "受保护备份核对",
            "target_type": "host",
            "target": volid,
            "connection": record.get("connection") or "",
            "metric": "backup_integrity",
            "value": 1.0 if record["state"] == "missing" else 0.5,
            "threshold": 0.0,
            "node": record["node"],
            "ip": "",
            "vmid": record["vmid"] or None,
            "ts": int(now),
            "notify_source": alerting.SOURCE_BACKUPGUARD,
        }
        await alerting.mark_active(key, state)
        active[key] = dict(state, alarm_key=key)
        entry = {**state, "result": "sent" if ok else "failed", "detail": detail, "kind": "alarm"}
        await alerting.record(
            entry,
            source=alerting.SOURCE_BACKUPGUARD,
            repeat=bool(prev),
            channels=alerting.channel_ids(feishu, email_cfg, webhook_cfg),
        )
        entry["text"] = text
        fired.append(entry)

    for key, row in list(active.items()):
        if key in still_alarming:
            continue
        volid = str(row.get("target") or "")
        current = records.get(f"{row.get('connection') or ''}|{volid}")
        if current and current["state"] == "unknown":
            continue  # 读不到存储时不要误报「已恢复」
        if not await alerting.recovery_confirmed(key, row):
            continue  # 还没到「连续 N 轮可核对」的恢复门槛
        card = alerting.build_recovery_card(row, None, at=now)
        text = i18n.pick(
            f"受保护备份 {volid} 已恢复可核对状态，告警解除",
            f"Protected backup {volid} can be verified again; the alert is cleared",
        )
        ok, detail = await alerting.dispatch(
            target_owner,
            feishu,
            email_cfg,
            i18n.tr("恢复通知：") + volid,
            text,
            card,
            source=alerting.SOURCE_BACKUPGUARD,
            webhook=webhook_cfg,
        )
        await alerting.record(
            {
                "username": row.get("username") or target_owner,
                "rule_id": row.get("rule_id"),
                "rule_name": row.get("rule_name"),
                "target_type": "host",
                "target": volid,
                "metric": row.get("metric"),
                "value": 0.0,
                "threshold": row.get("threshold"),
                "result": "sent" if ok else "failed",
                "detail": detail,
                "kind": "recovery",
            },
            source=alerting.SOURCE_BACKUPGUARD,
            channels=alerting.channel_ids(feishu, email_cfg, webhook_cfg),
        )
        await alerting.clear_active(key)
        fired.append({"kind": "recovery", "target": volid, "text": text})

    return fired
