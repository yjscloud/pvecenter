"""证书生命周期事件的告警出口。

与 :mod:`alerting` 的分工：那边是"指标越界"的规则引擎（CPU 高、磁盘满），
这边是"证书生命周期"事件（即将到期 / 续期失败 / 续期成功 / 部署失败）。
两者共用同一批通道配置 —— 站内信、邮件、通用 Webhook、飞书，避免用户为了
证书再配一遍。

三条设计约束：

1. **绝不打断续期**。告警发不出去只是没通知到，证书该续还得续，所以这里
   所有异常就地吞掉并留日志，返回值只用于调试。
2. **自带冷却**。续期作业每 6 小时一轮，不过滤的话「剩余 14 天」会变成
   一天四条；冷却窗口写在站点上（``notices``），重启也不丢。
3. **按归属发**。站点是哪个用户的，消息就发给谁；管理员不重复收，避免
   一人多站点时被自己刷屏。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from . import i18n, notifications

log = logging.getLogger(__name__)

#: 事件类型。与前端 ``cert.event.*`` 的文案键一一对应。
EVENT_EXPIRING = "expiring"
EVENT_RENEW_FAILED = "renew_failed"
EVENT_RENEWED = "renewed"
EVENT_DEPLOY_FAILED = "deploy_failed"
#: 站点可用性从 up 翻到 down（及恢复）时发一次，冷却窗口内不重复
EVENT_SITE_DOWN = "site_down"
EVENT_SITE_UP = "site_up"
#: 主动轮换完成 / 证书健康度不达标
EVENT_ROTATED = "rotated"
EVENT_HEALTH_BAD = "health_bad"

#: 默认冷却（秒）。到期提醒这类"每天说一次就够"的事件用它。
COOLDOWN = 12 * 3600

_LEVELS = {
    EVENT_EXPIRING: "warning",
    EVENT_RENEW_FAILED: "error",
    EVENT_RENEWED: "info",
    EVENT_DEPLOY_FAILED: "error",
    EVENT_SITE_DOWN: "error",
    EVENT_SITE_UP: "info",
    EVENT_ROTATED: "info",
    EVENT_HEALTH_BAD: "warning",
}

#: 视为「问题已解决」的事件：进告警历史时记为 recovery，列表里不再占待办。
_RECOVERY_EVENTS = frozenset({EVENT_RENEWED, EVENT_SITE_UP, EVENT_ROTATED})


async def _record_history(
    owner: str,
    site: Dict[str, Any],
    event: str,
    title: str,
    detail: str,
    level: str,
    channels: list,
) -> None:
    """把证书事件补进统一的告警历史（``alert_history``）。

    证书告警此前只存在于证书页和站内信里：人在「监控告警」页复盘时看不到
    「昨晚 3 点续期失败了两次」。这里只补历史、不再写站内信（``inbox=False``）——
    本模块自己已经发过一条更贴合场景的消息，重复写会在铃铛里出现两条。
    """
    if not owner:
        return
    try:
        from . import alerting

        entry = {
            "username": owner,
            "rule_id": "cert:" + event,
            "rule_name": title,
            "target_type": "cert",
            "target": str(site.get("domain") or site.get("name") or ""),
            "metric": "certificate",
            "value": int(certs_days_left(site) or 0),
            "threshold": 0,
            "result": alerting.RESULT_SENT,
            "detail": detail,
            "kind": "recovery" if event in _RECOVERY_EVENTS else "alarm",
            "ts": int(time.time()),
            "site_id": str(site.get("id") or ""),
        }
        await alerting.record(
            entry, source="cert", channels=channels, inbox=False
        )
    except Exception:  # noqa: BLE001 - 历史写失败不能影响通知与续期
        log.warning("证书事件写入告警历史失败：%s", event, exc_info=True)


def certs_days_left(site: Dict[str, Any]) -> Optional[int]:
    """剩余天数（避免为了一个数字 import 整个 certs 模块造成循环依赖）。"""
    expire = int(site.get("expire_at") or 0)
    if not expire:
        return None
    return int((expire - time.time()) // 86400)


def _notices(site: Dict[str, Any]) -> Dict[str, int]:
    raw = site.get("notices")
    if not isinstance(raw, dict):
        raw = {}
        site["notices"] = raw
    return raw


def cooled(site: Dict[str, Any], event: str, ttl: int = COOLDOWN) -> bool:
    """这个站点该事件是否还在冷却窗口内。"""
    last = int(_notices(site).get(event) or 0)
    return (time.time() - last) < ttl


def mark(site: Dict[str, Any], event: str, at: Optional[int] = None) -> None:
    _notices(site)[event] = int(at or time.time())


def _feishu_card(title: str, detail: str, level: str) -> Dict[str, Any]:
    """最小的飞书卡片：标题 + 一行正文。不追求花哨，能看懂就行。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": {"error": "red", "warning": "orange"}.get(level, "blue"),
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": detail}},
        ],
    }


async def notify(
    owner: str,
    site: Dict[str, Any],
    event: str,
    title: str,
    detail: str = "",
    *,
    ttl: int = COOLDOWN,
    key: str = "",
    level: str = "",
    escalate: bool = False,
) -> bool:
    """把一条证书事件发到该站点归属者的所有已配置通道。

    ``key`` 用于「同一事件按档位分别冷却」：多级到期提醒（30/14/7/3 天）共用
    ``expiring`` 事件，但每一档要有自己的冷却键，否则 30 天那次发完后 14 天
    那次会被同一条冷却记录吃掉。不传则沿用事件名。

    ``escalate=True`` 表示「这件事已经反复发生，必须再喊一次」：绕过冷却、
    使用独立的冷却键（否则升级消息会把常规消息一起吃掉），级别升到 critical。
    连续失败 4 次后自动重试会停手，那时只有升级通知能叫人介入。

    返回 True 表示至少发出去了一个通道；False 表示冷却中或全部失败。
    """
    if not owner:
        return False
    cooldown_key = key or event
    if escalate:
        # 独立冷却键：升级消息与常规消息各自计时，互不吞并
        cooldown_key = cooldown_key + ":escalated"
        ttl = 0
    if cooled(site, cooldown_key, ttl):
        return False

    level = level or _LEVELS.get(event, "info")
    sent = False
    channels: list = []

    # 站内信永远写：它是最低成本的兜底，也不会打扰人
    try:
        await notifications.push(
            owner,
            title=title,
            body=detail,
            link="/certs?site=" + str(site.get("id") or ""),
            kind="cert",
            level=level,
        )
        sent = True
        channels.append("inbox")
    except Exception:  # noqa: BLE001 - 通知失败不能影响续期
        log.warning("证书事件站内信写入失败：%s", event, exc_info=True)

    # 其余通道按需加载：alerting 依赖较重，且证书模块不该拖慢启动
    try:
        from . import alerting

        ok, _ = await alerting.send_alert_email(owner, title, detail)
        sent = sent or ok
        if ok:
            channels.append(alerting.CHANNEL_EMAIL)
        ok, _ = await alerting.send_webhook(title, detail, {"event": event}, owner)
        sent = sent or ok
        if ok:
            channels.append(alerting.CHANNEL_WEBHOOK)
        ok, _ = await alerting.send_feishu(_feishu_card(title, detail, level), owner)
        sent = sent or ok
        if ok:
            channels.append(alerting.CHANNEL_FEISHU)
    except Exception:  # noqa: BLE001
        log.warning("证书事件外发通道失败：%s", event, exc_info=True)

    if sent:
        mark(site, cooldown_key)
        await _record_history(owner, site, event, title, detail, level, channels)
    return sent
