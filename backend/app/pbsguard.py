"""PBS 运行状态巡检：GC 失败与磁盘健康异常。

## 为什么单独一个巡检，而不是并进 verifyguard

verifyguard 走的是 **PVE 代理**（storage content 的 ``verification`` 字段），
不需要任何凭据；这里走 **PBS 直连**（任务列表与磁盘清单），只有登记了只读
token 才跑得起来。两者的前置条件不同，混在一起会出现“没配凭据就整块静默”
这种难解释的状态 —— 分开之后，各自的启用条件一目了然。

## 只报三类事

* **GC 失败**：PBS 的 ``garbage_collection`` 任务最近一次不是 ``OK``。GC 卡住
  时磁盘不会立刻爆，但下一次备份可能因空间不足失败，属于“早发现早处理”。
* **磁盘健康**：PBS 自己报 ``health`` 不是 ``PASSED`` 的盘（只看健康判定，
  不读温度、坏道计数之类的 SMART 细节）。
* **备份盘容量**：datastore 使用率越过 85% / 95%。GC 失败说的是“空间迟早
  出事”，这里说的是“已经快出事”—— 两条分开报，处理动作也不同（查日志
  还是清理 / 扩容）。

不报的东西（有意）：

* 维护模式 —— 用户自己设的，告警只会打扰；
* 订阅 / 版本提示 —— 商业信息，不是运行故障；
* 备份任务失败 —— PVE 侧 ``backup`` 规则已经覆盖，重复告警等于噪音。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from . import alerting, i18n, pbs, store

logger = logging.getLogger(__name__)

RULE_ID = "pbs-health"
RULE_NAME = "PBS 运行状态"
METRIC = "pbs_health"
#: GC 失败与磁盘异常都不会在几分钟内反复变化；6 小时提醒一次足够，
#: 也不至于让一条“盘快坏了”被埋在重复消息里。
COOLDOWN = 6 * 3600


async def scan_failures() -> List[Dict[str, Any]]:
    """扫全部 PBS 连接，返回 GC 失败与磁盘异常。"""
    conns = await pbs.load_connections()
    failures: List[Dict[str, Any]] = []

    async def _one(conn: Dict[str, Any]) -> List[Dict[str, Any]]:
        cfg = pbs.PbsConnection.from_dict(conn)
        if not cfg.configured:
            return []
        out: List[Dict[str, Any]] = []
        client = pbs.PbsClient(cfg)
        try:
            try:
                nodes = await client.nodes()
            except Exception as exc:  # noqa: BLE001 - 单条连接失败不影响其它
                logger.info("PBS 巡检：读取节点失败（%s）：%s", cfg.host, exc)
                return []
            node = cfg.node or (
                str(nodes[0].get("node") or "") if nodes else ""
            )
            if not node:
                return []

            # ---- GC：只看最近一次「已结束」的回收任务 ----
            try:
                tasks = await client.tasks(node, limit=50)
                gc_tasks = [
                    item
                    for item in tasks
                    if isinstance(item, dict)
                    and str(item.get("worker_type") or item.get("type") or "")
                    == "garbage_collection"
                ]
                if gc_tasks:
                    latest = max(
                        gc_tasks, key=lambda item: int(item.get("starttime") or 0)
                    )
                    ended = int(latest.get("endtime") or 0) > 0
                    status = str(latest.get("status") or "").strip()
                    if ended and status.upper() != "OK":
                        out.append(
                            {
                                "key": f"gc:{cfg.host}:{node}",
                                "kind": "gc",
                                "host": cfg.host,
                                "node": node,
                                "detail": status or "-",
                                "starttime": int(latest.get("starttime") or 0),
                            }
                        )
            except Exception as exc:  # noqa: BLE001
                logger.info("PBS 巡检：读取任务失败（%s）：%s", cfg.host, exc)

            # ---- 磁盘健康 ----
            try:
                disks = pbs._disk_alerts(await client.disks(node))
            except Exception as exc:  # noqa: BLE001
                logger.info("PBS 巡检：读取磁盘失败（%s）：%s", cfg.host, exc)
                disks = []
            for disk in disks:
                out.append(
                    {
                        "key": f"disk:{cfg.host}:{disk['device']}",
                        "kind": "disk",
                        "host": cfg.host,
                        "node": node,
                        "detail": disk["health"],
                        "device": disk["device"],
                        "model": disk["model"],
                        "serial": disk["serial"],
                    }
                )

            # ---- 备份盘容量：只有越过 warn / crit 才报 ----
            # 正常盘和读不到容量的盘（total=0 / 403 / 404）都静默：容量告警
            # 一旦开始为“读不到”发消息，就会盖住真正的“快满了”。
            try:
                stores = [
                    item
                    for item in await client.datastores()
                    if isinstance(item, dict)
                ]
                rows = await asyncio.gather(
                    *(pbs.datastore_row(client, item) for item in stores)
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("PBS 巡检：读取仓库失败（%s）：%s", cfg.host, exc)
                rows = []
            for row in rows:
                level = str(row.get("level") or "")
                if level not in ("warn", "crit"):
                    continue
                out.append(
                    {
                        "key": f"capacity:{cfg.host}:{row['store']}",
                        "kind": "capacity",
                        "host": cfg.host,
                        "node": node,
                        "store": row["store"],
                        "level": level,
                        "percent": row["used_percent"],
                        "threshold": (
                            pbs.PBS_USAGE_CRIT
                            if level == "crit"
                            else pbs.PBS_USAGE_WARN
                        ),
                        "used": row["used"],
                        "total": row["total"],
                        "free": row["free"],
                    }
                )
            return out
        finally:
            await client.aclose()

    groups = await asyncio.gather(
        *(_one(conn) for conn in conns), return_exceptions=True
    )
    for group in groups:
        if isinstance(group, BaseException):
            logger.info("PBS 巡检：一条连接整体失败：%s", group)
            continue
        failures.extend(group)
    return failures


def _gb(value: Any) -> str:
    """字节数 → 人类可读；算不出来时给 ``-``，别在正文里留空档。"""
    return alerting._capacity(value) or "-"


def _subject(item: Dict[str, Any]) -> str:
    """受影响对象：容量报「PBS / 仓库」，磁盘报「PBS / 设备」，GC 报「PBS」。"""
    if item["kind"] == "capacity":
        return f"{item['host']} / {item['store']}"
    if item["kind"] == "disk":
        return f"{item['host']} / {item['device']}"
    return item["host"]


def _alert_text(item: Dict[str, Any], at: Optional[float] = None) -> str:
    """告警正文：异常类型、发生时间、受影响对象三件必须齐全。"""
    if item["kind"] == "capacity":
        body = i18n.pick(
            f"PBS 备份盘空间不足：{item['host']} / {item['store']}\n"
            f"使用率 {item['percent']}%（阈值 {item['threshold']}%）；"
            f"已用 {_gb(item['used'])} / 总容量 {_gb(item['total'])}，"
            f"剩余 {_gb(item['free'])}\n"
            "空间不足会让下一次备份直接失败，请先清理过期备份或扩容。",
            f"PBS datastore is running low: {item['host']} / {item['store']}\n"
            f"Usage {item['percent']}% (threshold {item['threshold']}%); "
            f"used {_gb(item['used'])} of {_gb(item['total'])}, "
            f"{_gb(item['free'])} free\n"
            "When it fills up the next backup fails — prune old backups or "
            "grow the datastore.",
        )
    elif item["kind"] == "gc":
        body = i18n.pick(
            f"PBS 垃圾回收（GC）失败：{item['host']} / {item['node']}\n"
            f"任务状态：{item['detail']}\n"
            "GC 卡住时空间不会立刻回收，下一次备份可能因容量不足失败，"
            "请到 PBS 控制台查看该任务日志。",
            f"PBS garbage collection failed: {item['host']} / {item['node']}\n"
            f"Task status: {item['detail']}\n"
            "When GC is stuck, space is not reclaimed and the next backup may "
            "fail on capacity — check the task log in the PBS console.",
        )
    else:
        body = i18n.pick(
            f"PBS 磁盘健康异常：{item['host']} / {item['device']}\n"
            f"健康状态：{item['detail']}；型号：{item['model'] or '-'}；"
            f"序列号：{item['serial'] or '-'}\n"
            "这块盘可能即将故障，请尽快更换并确认备份可恢复。",
            f"PBS disk health problem: {item['host']} / {item['device']}\n"
            f"Health: {item['detail']}; model: {item['model'] or '-'}; "
            f"serial: {item['serial'] or '-'}\n"
            "This disk may fail soon — replace it and verify that backups restore.",
        )
    if at is not None:
        body += i18n.pick(
            f"\n触发时间：{alerting._now_text(at)}",
            f"\nTriggered at {alerting._now_text(at)}",
        )
    return body


def _card(item: Dict[str, Any], at: float) -> Dict[str, Any]:
    if item["kind"] == "capacity":
        crit = item["level"] == "crit"
        return alerting.build_card(
            "critical" if crit else "warning",
            ("🔴 " if crit else "🟠 ") + i18n.tr("pvecenter PBS 运行告警"),
            i18n.pick("备份盘空间不足", "Datastore running low"),
            [
                (i18n.tr("PBS"), item["host"]),
                (i18n.pick("仓库", "Datastore"), item["store"]),
                (i18n.pick("使用率", "Usage"), f"{item['percent']}%"),
                (i18n.pick("阈值", "Threshold"), f"{item['threshold']}%"),
                (
                    i18n.pick("已用 / 总容量", "Used / total"),
                    f"{_gb(item['used'])} / {_gb(item['total'])}",
                ),
                (i18n.pick("剩余", "Free"), _gb(item["free"])),
            ],
            i18n.pick(
                f"触发时间 {alerting._now_text(at)} · "
                "空间不足会让下一次备份直接失败",
                f"Triggered at {alerting._now_text(at)} · a full datastore "
                "fails the next backup",
            ),
        )
    if item["kind"] == "gc":
        return alerting.build_card(
            "warning",
            "🟠 " + i18n.tr("pvecenter PBS 运行告警"),
            i18n.tr("垃圾回收失败"),
            [
                (i18n.tr("PBS"), item["host"]),
                (i18n.tr("节点"), item["node"]),
                (i18n.tr("任务状态"), item["detail"]),
            ],
            i18n.pick(
                f"触发时间 {alerting._now_text(at)} · GC 失败会让仓库空间持续增长",
                f"Triggered at {alerting._now_text(at)} · failed GC keeps the "
                "repository growing",
            ),
        )
    return alerting.build_card(
        "critical",
        "🔴 " + i18n.tr("pvecenter PBS 运行告警"),
        i18n.tr("磁盘健康异常"),
        [
            (i18n.tr("PBS"), item["host"]),
            (i18n.tr("磁盘"), item.get("device") or "-"),
            (i18n.pick("健康状态", "Health"), item["detail"]),
            (i18n.tr("型号 / 序列号"), f"{item.get('model') or '-'} / {item.get('serial') or '-'}"),
        ],
        i18n.pick(
            f"触发时间 {alerting._now_text(at)} · 请尽快更换磁盘并确认备份可恢复",
            f"Triggered at {alerting._now_text(at)} · replace the disk and verify "
            "that backups restore",
        ),
    )


async def evaluate(owner: Optional[str] = None) -> List[Dict[str, Any]]:
    """巡检 GC 失败、磁盘健康与备份盘容量；``owner=None`` 发给第一个管理员。"""
    failures = await scan_failures()
    target_owner = owner or await store.first_admin_username() or ""
    if not target_owner:
        return []
    i18n.pin_language(await alerting.resolve_language(target_owner))

    feishu = await alerting.load_feishu(target_owner)
    email_cfg = await alerting.load_alert_email(target_owner)
    webhook_cfg = await alerting.load_webhook(target_owner)
    now = time.time()

    active = {
        key: row
        for key, row in (await alerting.load_active()).items()
        if str(row.get("rule_id")) == RULE_ID
    }
    still: set = set()
    fired: List[Dict[str, Any]] = []

    for item in failures:
        key = alerting.alarm_key(target_owner, RULE_ID, item["key"])
        still.add(key)
        prev = active.get(key) or {}
        if now - float(prev.get("ts") or 0) < COOLDOWN:
            continue
        text = _alert_text(item, now)
        ok, detail = await alerting.dispatch(
            target_owner,
            feishu,
            email_cfg,
            i18n.tr("PBS 运行告警：") + _subject(item),
            text,
            _card(item, now),
            source=alerting.SOURCE_PBSGUARD,
            webhook=webhook_cfg,
        )
        state = {
            "username": target_owner,
            "rule_id": RULE_ID,
            "rule_name": RULE_NAME,
            "target_type": "host",
            # 存人话而不是 alarm_key：活动告警与恢复通知都直接显示这个字段
            "target": _subject(item),
            # 容量与 GC/磁盘分开记指标：告警页按指标筛选时能一眼分出
            # 「快满了」和「已经坏了」，两者的处理动作不一样。
            "metric": "pbs_capacity" if item["kind"] == "capacity" else METRIC,
            # 容量项带真实百分比，告警页才会显示「92.3% / 阈值 85%」；
            # GC / 磁盘是事件型，没有百分比语义，保持 1.0 / 0.0。
            "value": float(item["percent"]) if item["kind"] == "capacity" else 1.0,
            "threshold": (
                float(item["threshold"]) if item["kind"] == "capacity" else 0.0
            ),
            "node": item["node"],
            "ip": "",
            "vmid": None,
            "ts": int(now),
            "notify_source": alerting.SOURCE_PBSGUARD,
        }
        await alerting.mark_active(key, state)
        active[key] = dict(state, alarm_key=key)
        entry = {
            **state,
            "result": "sent" if ok else "failed",
            "detail": detail,
            "kind": "alarm",
        }
        await alerting.record(
            entry,
            source=alerting.SOURCE_PBSGUARD,
            repeat=bool(prev),
            channels=alerting.channel_ids(feishu, email_cfg, webhook_cfg),
        )
        entry["text"] = text
        fired.append(entry)

    for key, row in list(active.items()):
        if key in still:
            continue
        if not await alerting.recovery_confirmed(key, row):
            continue
        target = str(row.get("target") or "")
        card = alerting.build_recovery_card(row, None, at=now)
        text = i18n.pick(
            f"PBS 运行状态已恢复：{target}",
            f"PBS is healthy again: {target}",
        )
        ok, detail = await alerting.dispatch(
            target_owner,
            feishu,
            email_cfg,
            i18n.tr("恢复通知：") + target,
            text,
            card,
            source=alerting.SOURCE_PBSGUARD,
            webhook=webhook_cfg,
        )
        await alerting.record(
            {
                "username": row.get("username") or target_owner,
                "rule_id": row.get("rule_id"),
                "rule_name": row.get("rule_name"),
                "target_type": "host",
                "target": target,
                "metric": row.get("metric"),
                "value": 0.0,
                "threshold": row.get("threshold"),
                "result": "sent" if ok else "failed",
                "detail": detail,
                "kind": "recovery",
            },
            source=alerting.SOURCE_PBSGUARD,
            channels=alerting.channel_ids(feishu, email_cfg, webhook_cfg),
        )
        await alerting.clear_active(key)
        fired.append({"kind": "recovery", "target": target})

    return fired
