"""PBS 归档校验（verify）巡检。

## 为什么需要它

备份页能看到「最近一次备份是什么时候」，但**跑成功不等于能恢复**：vzdump 只保证
归档写完，校验（verify）才会真的把数据读一遍并比对校验和。只有 PBS 提供这项能力，
结果挂在归档条目的 ``verification`` 字段上（``ok`` / ``failed`` / ``none``）。

面板此前对这块完全沉默：verify 失败要等用户自己打开 PBS 控制台才看得到。这里的
巡检只做一件事 —— **发现 ``failed`` 就告警**，恢复后发恢复通知。

## 为什么不并进资源告警的规则表

``cpu`` / ``mem`` / ``disk`` / ``offline`` / ``backup`` 那些规则的判据是「某个对象
的使用率」，用户可以自己挑阈值。校验失败是**事件**而不是阈值：某个归档校验没过，
就是没过，没有可调的百分比。所以照 :mod:`app.backupguard` 的写法自建 ``rule_id``、
自己判冷却，复用告警的去重 / 冷却 / 飞书邮件 Webhook / 恢复通知。

## 口径

* 只看 PBS 存储：``verification`` 是 PBS 专有字段，目录型存储没有；
* 只报 ``failed``：``none`` 表示「还没校验过」，在没配置定期 verify 的环境里是常态，
  对它告警会变成噪音；
* 按归档（volid）报，不按 vmid 聚合：同一台机器可能有多份归档，能指出具体哪一份
  不可信比笼统说「这台机器的备份有问题」更有用。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from . import alerting, i18n, pve, store

logger = logging.getLogger(__name__)

RULE_ID = "backup-verify"
RULE_NAME = "备份校验巡检"
METRIC = "backup_verify"
#: 校验失败不会自己消失（要重新 verify 才可能转 ok），重复提醒不必太密；
#: 一小时一次足够提醒到人，又不至于把人吵到关掉整个来源。
COOLDOWN = 3600


async def _pbs_storages(client: pve.ProxmoxClient) -> List[Dict[str, Any]]:
    """该连接下所有 PBS 且支持 backup 内容的存储（附 ``node``）。"""
    try:
        nodes = await client.nodes() or []
    except pve.ProxmoxError as exc:
        logger.info("校验巡检：读取节点失败：%s", exc.message)
        return []
    names = [str(n.get("node") or "") for n in nodes if n.get("node")]
    if not names:
        return []
    results = await asyncio.gather(
        *(client.storages(name) for name in names), return_exceptions=True
    )
    out: List[Dict[str, Any]] = []
    for name, items in zip(names, results):
        if isinstance(items, BaseException):
            continue
        for item in items or []:
            if str(item.get("type") or "").strip().lower() != "pbs":
                continue
            if "backup" not in str(item.get("content") or ""):
                continue
            out.append({**item, "node": name})
    return out


async def scan_failures() -> List[Dict[str, Any]]:
    """扫全部连接下的 PBS 归档，返回校验失败的那些。

    单条连接 / 单个存储读不到只跳过它自己：一套 PVE 掉线不该让巡检整体停摆。
    """
    pairs = pve.all_connection_clients()
    if not pairs:
        pairs = [({}, pve.get_client())]

    failures: List[Dict[str, Any]] = []
    for profile, client in pairs:
        cid = str((profile or {}).get("id") or "")
        for storage in await _pbs_storages(client):
            node = str(storage.get("node") or "")
            name = str(storage.get("storage") or "")
            if not node or not name:
                continue
            try:
                entries = await client.storage_content(node, name, content="backup")
            except pve.ProxmoxError as exc:
                logger.info("校验巡检：读取 %s/%s 失败：%s", node, name, exc.message)
                continue
            for entry in entries or []:
                raw = entry.get("verification")
                if not isinstance(raw, dict):
                    continue
                if str(raw.get("state") or "").strip().lower() != "failed":
                    continue
                failures.append(
                    {
                        "volid": str(entry.get("volid") or ""),
                        "node": node,
                        "storage": name,
                        "connection_id": cid,
                        "vmid": entry.get("vmid"),
                        "ctime": int(entry.get("ctime") or 0),
                        "upid": str(raw.get("upid") or ""),
                    }
                )
    return failures


def _alert_text(item: Dict[str, Any]) -> str:
    return i18n.pick(
        f"PBS 归档校验失败：{item['volid']}\n"
        f"存储：{item['node']}/{item['storage']}；备份时间："
        f"{alerting._now_text(item['ctime'])}\n"
        "这份备份在 verify 时未通过校验，恢复到它可能出现数据损坏，"
        "请到 PBS 控制台查看校验日志并重新备份。",
        f"PBS verification failed: {item['volid']}\n"
        f"Storage: {item['node']}/{item['storage']}; backup time: "
        f"{alerting._now_text(item['ctime'])}\n"
        "This archive did not pass verification — restoring from it may produce "
        "corrupted data. Check the PBS verify log and take a fresh backup.",
    )


def _card(item: Dict[str, Any], at: float) -> Dict[str, Any]:
    return alerting.build_card(
        "critical",
        "🔴 " + i18n.tr("pvecenter 备份校验告警"),
        i18n.tr("PBS 归档校验失败"),
        [
            (i18n.tr("备份卷"), item["volid"]),
            (i18n.tr("存储"), f"{item['node']}/{item['storage']}"),
            (i18n.tr("备份时间"), alerting._now_text(item["ctime"])),
            (i18n.tr("校验任务"), item["upid"] or "-"),
        ],
        i18n.pick(
            f"触发时间 {alerting._now_text(at)} · 这份备份可能不可恢复，"
            "请到 PBS 控制台确认并重新备份",
            f"Triggered at {alerting._now_text(at)} · this archive may not be "
            "restorable — confirm in the PBS console and take a fresh backup",
        ),
    )


async def evaluate(owner: Optional[str] = None) -> List[Dict[str, Any]]:
    """巡检 PBS 校验失败并推送；``owner=None`` 发给第一个管理员。

    照 :func:`app.backupguard.evaluate` 的写法：自建 rule_id、自己判冷却、复用
    告警的去重 / 飞书邮件 Webhook / 恢复通知。
    """
    failures = await scan_failures()
    target_owner = owner or await store.first_admin_username() or ""
    if not target_owner:
        return []
    # 巡检在后台跑，没有请求上下文，按收件人的语言渲染
    i18n.pin_language(await alerting.resolve_language(target_owner))

    feishu = await alerting.load_feishu(target_owner)
    email_cfg = await alerting.load_alert_email(target_owner)
    webhook_cfg = await alerting.load_webhook(target_owner)
    now = time.time()

    active = await alerting.load_active()
    active = {
        key: row
        for key, row in active.items()
        if str(row.get("rule_id")) == RULE_ID
    }
    still_alarming: set = set()
    fired: List[Dict[str, Any]] = []

    for item in failures:
        volid = item["volid"]
        if not volid:
            continue
        key = alerting.alarm_key(target_owner, RULE_ID, volid)
        still_alarming.add(key)
        prev = active.get(key) or {}
        if now - float(prev.get("ts") or 0) < COOLDOWN:
            continue
        text = _alert_text(item)
        ok, detail = await alerting.dispatch(
            target_owner,
            feishu,
            email_cfg,
            i18n.tr("备份校验告警：") + volid,
            text,
            _card(item, now),
            source=alerting.SOURCE_BACKUPVERIFY,
            webhook=webhook_cfg,
        )
        state = {
            "username": target_owner,
            "rule_id": RULE_ID,
            "rule_name": RULE_NAME,
            "target_type": "host",
            "target": volid,
            "metric": METRIC,
            "value": 1.0,
            "threshold": 0.0,
            "node": item["node"],
            "ip": "",
            "vmid": item.get("vmid"),
            "ts": int(now),
            "notify_source": alerting.SOURCE_BACKUPVERIFY,
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
            source=alerting.SOURCE_BACKUPVERIFY,
            repeat=bool(prev),
            channels=alerting.channel_ids(feishu, email_cfg, webhook_cfg),
        )
        entry["text"] = text
        fired.append(entry)

    for key, row in list(active.items()):
        if key in still_alarming:
            continue
        if not await alerting.recovery_confirmed(key, row):
            continue
        volid = str(row.get("target") or "")
        card = alerting.build_recovery_card(row, None, at=now)
        text = i18n.pick(
            f"PBS 归档 {volid} 已通过校验，告警解除",
            f"PBS archive {volid} now passes verification; the alert is cleared",
        )
        ok, detail = await alerting.dispatch(
            target_owner,
            feishu,
            email_cfg,
            i18n.tr("恢复通知：") + volid,
            text,
            card,
            source=alerting.SOURCE_BACKUPVERIFY,
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
            source=alerting.SOURCE_BACKUPVERIFY,
            channels=alerting.channel_ids(feishu, email_cfg, webhook_cfg),
        )
        await alerting.clear_active(key)
        fired.append({"kind": "recovery", "target": volid, "text": text})

    return fired
