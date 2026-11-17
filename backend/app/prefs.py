"""按用户保存的界面偏好（每用户一份 JSON）。

为什么要落服务端
----------------
仪表盘的布局、卡片显隐这类偏好如果只放浏览器 localStorage，换个浏览器、换台
机器、清一次缓存就全没了。面板的用户体系本来就是服务端的，偏好跟着账号走才
符合预期。

与 ``settings`` 表的区别
------------------------
``settings`` 是**全局**键值（PVE 连接、站点信息、邮件配置……），键名是功能名。
这里是**每用户**键值，主键是 ``(username, key)``。两者语义不同（一个是「面板
怎么配」，一个是「这个人怎么看界面」），混在一张表里迟早会出现「谁的偏好被
别人覆盖了」这类问题。

键名为什么要白名单
------------------
不是为了防攻击（值有大小上限，且只能读写自己的），而是为了让「这张表里可能
出现什么」是可枚举的：将来要做偏好导出、清理或迁移时，白名单是那份清单。
新增一个偏好 = 在 :data:`ALLOWED_KEYS` 里加一项，顺带逼着思考它的结构。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from . import database

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_prefs (
    username VARCHAR(64) NOT NULL,
    `key`    VARCHAR(64) NOT NULL,
    value    TEXT        NOT NULL,
    updated  DOUBLE      NOT NULL,
    PRIMARY KEY (username, `key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

# 允许保存的偏好键。
PREF_DASHBOARD_LAYOUT = "dashboard_layout"
# 界面语言：浏览器里那份 localStorage 决定「这台设备立刻显示哪种语言」，
# 服务端这份决定「发给这个人的通知邮件用哪种语言」。两者刻意各存一份 ——
# 同一账号在公司电脑看英文、在家看中文是合理的。
PREF_LANGUAGE = "language"
# 顶栏「集群作用域」：这个人默认在看哪一套 PVE（值是连接 id，空串 = 全部集群）。
#
# 与语言同理，浏览器那份 localStorage 是**本设备的热副本**（请求拦截器与启动校验
# 需要同步读取），服务端这份决定「换台设备/换个浏览器打开时恢复到哪一套」。
# 存服务端还有一层意义：原先「读哪一台」是 settings 里的全局指针，管理员甲切一下
# 管理员乙的读数立刻跟着变；改成按用户之后，各人的视角互不干扰。
PREF_CLUSTER_SCOPE = "cluster_scope"
# 虚拟机 / 容器的「置顶」与「加星」：
#   {"pinned": ["<conn>:<node>/<vmid>", ...], "starred": [...]}
# pinned 是**有序**数组，顺序即置顶顺序（先置顶的排在前）；starred 只表示归属。
#
# 与 dashboard_layout 的 order/hidden 完全无关 —— 那边是 widget id，上限 60，
# 这里是几百台机器。存服务端而不是 localStorage，是因为「收藏了哪几台」属于
# 账号级认知，换台机器打开也该还在。
PREF_GUEST_PINS = "guest_pins"
ALLOWED_KEYS = frozenset(
    {PREF_DASHBOARD_LAYOUT, PREF_LANGUAGE, PREF_CLUSTER_SCOPE, PREF_GUEST_PINS}
)

# 单个偏好的体积上限。布局就是个 widget id 数组，几 KB 顶天；留 64 KB 是给
# 未来的偏好（比如保存的筛选条件组合）留余量，同时挡住把这张表当文件存的用法。
MAX_VALUE_BYTES = 64 * 1024


async def init_table() -> None:
    async with database.connect() as db:
        await db.executescript(SCHEMA)
        await db.commit()


def is_allowed(key: str) -> bool:
    return key in ALLOWED_KEYS


def _decode(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        # 存坏了就当没存过：一个坏掉的偏好不该让整个仪表盘打不开
        logger.warning("用户偏好解析失败，按未设置处理")
        return default


async def get(username: str, key: str, default: Any = None) -> Any:
    async with database.connect() as db:
        cursor = await db.execute(
            "SELECT value FROM user_prefs WHERE username = ? AND `key` = ?",
            (username, key),
        )
        row = await cursor.fetchone()
    if not row:
        return default
    return _decode(row[0], default)


async def all_for(username: str) -> Dict[str, Any]:
    """该用户的全部偏好。没设置过的键不会出现在结果里（由调用方决定默认值）。"""
    async with database.connect() as db:
        cursor = await db.execute(
            "SELECT `key`, value FROM user_prefs WHERE username = ?",
            (username,),
        )
        rows = await cursor.fetchall()
    out: Dict[str, Any] = {}
    for row in rows:
        key = str(row[0])
        if not is_allowed(key):
            # 白名单缩小时留下的老数据：不回传，也不主动删（回滚版本还能用）
            continue
        out[key] = _decode(row[1], None)
    return out


async def set(  # noqa: A001 - 与项目里其它模块的 set_setting 风格一致
    username: str, key: str, value: Any
) -> Any:
    """保存一条偏好（覆盖写）。返回存进去的值。"""
    payload = json.dumps(value, ensure_ascii=False)
    if len(payload.encode("utf-8")) > MAX_VALUE_BYTES:
        raise ValueError(f"偏好内容过大（上限 {MAX_VALUE_BYTES // 1024} KB）")

    async with database.connect() as db:
        await db.execute(
            "INSERT INTO user_prefs (username, `key`, value, updated)"
            " VALUES (?, ?, ?, ?)"
            " ON DUPLICATE KEY UPDATE value = VALUES(value), updated = VALUES(updated)",
            (username, key, payload, time.time()),
        )
        await db.commit()
    return value


async def delete(username: str, key: str) -> bool:
    async with database.connect() as db:
        cursor = await db.execute(
            "DELETE FROM user_prefs WHERE username = ? AND `key` = ?",
            (username, key),
        )
        await db.commit()
        return int(cursor.rowcount or 0) > 0


# ---------------------------------------------------------------------------
# 机器回收后的置顶 / 收藏清理
#
# guest_pins 里的键是 ``<连接 id>:<节点>/<vmid>``（与前端 useGuestPins 一致），
# 里面**没有**任何能区分「第几代」机器的信息。PVE 分配新机器时用的是最小空闲
# VMID，所以一台机器被删掉、下一台机器拿到同一个号之后，旧键会原样命中新机器，
# 表现为「刚下发的新机器莫名其妙带着星标和置顶」。
#
# 结论：机器被回收时必须把它的键从所有用户偏好里删掉；建机时再兜底删一次，
# 覆盖「在面板外删的机器」这种走不到删除接口的情况。
# ---------------------------------------------------------------------------


def guest_pin_refs(connection_id: str, node: str, vmid: Any) -> List[str]:
    """一台机器在 guest_pins 里可能出现的全部写法。

    正常写法是 ``<连接 id>:<节点>/<vmid>``；空连接、历史上不带连接前缀、
    以及早期把缺省连接写成 ``-`` 的几种变体一并列出。多删一条不存在的键没有
    副作用，漏删一条就会让同号的新机器继承旧状态，权衡很明确。
    """
    conn = str(connection_id or "").strip()
    node = str(node or "").strip()
    vm = str(vmid).strip()
    refs = [
        f"{conn}:{node}/{vm}",
        f":{node}/{vm}",
        f"-:{node}/{vm}",
        f"{node}/{vm}",
    ]
    return list(dict.fromkeys(refs))


def _drop_pin_refs(pins: Dict[str, Any], refs: Set[str]) -> Tuple[Dict[str, Any], bool]:
    """从一份 guest_pins 里摘掉这些键，返回 (新值, 是否改动)。"""
    result = dict(pins)
    changed = False
    for field in ("pinned", "starred"):
        values = pins.get(field)
        if not isinstance(values, list):
            continue
        kept = [item for item in values if not (isinstance(item, str) and item in refs)]
        if len(kept) != len(values):
            changed = True
            result[field] = kept
    return result, changed


async def forget_guest_refs(connection_id: str, node: str, vmid: Any) -> int:
    """把一台机器从**所有用户**的置顶 / 收藏里移除，返回被改动的用户数。

    失败只记日志、不抛给调用方：资源已经删掉了，清理偏好是善后工作，
    不该反过来把「删除成功」变成 500。残留的键会在下一次建机兜底清理时消失。
    """
    # 注意别写成 set(...)：本模块有一个同名的 set() 用于保存偏好，会被遮蔽
    refs = {ref for ref in guest_pin_refs(connection_id, node, vmid)}
    changed = 0
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "SELECT username, value FROM user_prefs"
                " WHERE `key` = ? FOR UPDATE",
                (PREF_GUEST_PINS,),
            )
            rows = await cursor.fetchall()
            for row in rows:
                stored = _decode(row[1], None)
                if not isinstance(stored, dict):
                    continue
                trimmed, hit = _drop_pin_refs(stored, refs)
                if not hit:
                    continue
                await db.execute(
                    "UPDATE user_prefs SET value = ?, updated = ?"
                    " WHERE username = ? AND `key` = ?",
                    (
                        json.dumps(trimmed, ensure_ascii=False),
                        time.time(),
                        row[0],
                        PREF_GUEST_PINS,
                    ),
                )
                changed += 1
            await db.commit()
    except Exception:  # noqa: BLE001 - 见上面的说明：清理失败不阻断删除
        logger.warning(
            "清理 %s/%s 的置顶与收藏失败", node, vmid, exc_info=True
        )
        return 0
    if changed:
        logger.info(
            "%s/%s 已回收，清掉了 %d 个用户的置顶/收藏", node, vmid, changed
        )
    return changed
