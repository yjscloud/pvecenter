"""邀请链接：**邀请制**下邀请凭证的另一种载体（URL 代替文本码）。

它不是独立的注册方式：注册方式仍然是「邀请制」，注册入口仍然是 ``/register``，
链接只是把凭证装进 URL（``/register?invite=<token>``）以便转发，并顺带把
「进来是什么角色、要不要审批、能用几次、什么时候过期」写进同一条记录。

因此校验归属也清楚：链接只在邀请制下生效（关闭注册直接 403，注册码制 / 自助
注册不认链接）；链接无效与邀请码无效走同样的 400，审批走同一条队列。凭证与
链接在设置页同属「签发凭证」，清单混排 —— 它们本来就是同一份「谁能进来」的名单。

为什么 token 存**明文**，而不像 regcodes 那样只存哈希：
邀请链接是要反复复制、贴到群里、写进值班文档的长期入口，管理员事后还得能再看
一眼、再复制一次 —— 只存哈希等于「关掉弹窗就再也找不回来，只能重建」。风险由
这几点约束，而不是靠隐藏明文：

* 可设使用次数上限（0 = 不限）与有效期（0 = 永久）；
* 可随时失效（``revoked``）；
* 可要求注册后必须经管理员审批（``requires_approval``），此时账号建出来是
  pending，角色先写好但不生效；
* 库被读走时攻击者拿到的只是一个入口，仍受次数、有效期与审批限制。
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any, Dict, List, Optional

from . import database

logger = logging.getLogger(__name__)

#: 次数上限：0 = 不限，其余收敛到 1..MAX_USES_LIMIT
DEFAULT_MAX_USES = 0
MAX_USES_LIMIT = 1000

#: 有效期：0 = 永久，其余收敛到 1..MAX_TTL_DAYS
DEFAULT_TTL_DAYS = 7
MAX_TTL_DAYS = 365

SCHEMA = """
CREATE TABLE IF NOT EXISTS registration_invites (
    id              VARCHAR(40)  NOT NULL,
    token           VARCHAR(64)  NOT NULL,
    note            VARCHAR(191) NOT NULL DEFAULT '',
    role            VARCHAR(64)  NOT NULL DEFAULT 'viewer',
    requires_approval TINYINT    NOT NULL DEFAULT 1,
    max_uses        INT          NOT NULL DEFAULT 0,
    used_count      INT          NOT NULL DEFAULT 0,
    expires         BIGINT       NOT NULL DEFAULT 0,
    created         BIGINT       NOT NULL DEFAULT 0,
    created_by      VARCHAR(64)  NOT NULL DEFAULT '',
    revoked         TINYINT      NOT NULL DEFAULT 0,
    -- 新用户的资源额度（JSON，字段见 app.quota 的 LIMIT_GROUPS）。NULL = 不额外限制
    quota           TEXT,
    last_used       BIGINT       NOT NULL DEFAULT 0,
    last_used_by    VARCHAR(191) NOT NULL DEFAULT '',
    PRIMARY KEY (id),
    UNIQUE KEY uk_reg_invite_token (token),
    KEY idx_reg_invite_created (created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


async def init_table() -> None:
    async with database.connect() as db:
        await db.executescript(SCHEMA)
        await db.commit()
        # 老库补列：quota 是后加的（链接可以预先给新用户分配额度）。
        # NULL = 不额外限制，继续按角色默认额度走，存量链接无需迁移。
        await database.add_columns(
            db,
            "registration_invites",
            (("quota", "ALTER TABLE registration_invites ADD COLUMN quota TEXT"),),
        )
        await db.commit()


# ----------------------------------------------------------------- 数值收敛
def normalize_max_uses(value: Any) -> int:
    """0 = 不限；负数按 0，超过上限按上限。"""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_USES
    if count <= 0:
        return 0
    return min(count, MAX_USES_LIMIT)


def normalize_ttl_days(value: Any) -> int:
    """0 = 永久；负数按 0，超过上限按上限。"""
    try:
        days = int(value)
    except (TypeError, ValueError):
        return DEFAULT_TTL_DAYS
    if days <= 0:
        return 0
    return min(days, MAX_TTL_DAYS)


# ----------------------------------------------------------------- 视图
def _row(raw: Any) -> Dict[str, Any]:
    return dict(raw) if raw is not None else {}


def _decode_quota(raw: Any) -> Optional[Dict[str, Any]]:
    """库里的额度 JSON → 对象；空 / 坏数据按「没设置」处理。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def public_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """列表 / 详情视图。**含** token —— 这是链接，不是一次性口令（见模块头）。"""
    now = int(time.time())
    expires = int(row.get("expires") or 0)
    revoked = bool(row.get("revoked"))
    max_uses = int(row.get("max_uses") or 0)
    used_count = int(row.get("used_count") or 0)
    remaining: Optional[int] = None if max_uses == 0 else max(0, max_uses - used_count)
    exhausted = max_uses > 0 and used_count >= max_uses

    if revoked:
        status = "revoked"
    elif exhausted:
        status = "exhausted"
    elif expires and expires <= now:
        status = "expired"
    else:
        status = "active"
    return {
        "id": str(row.get("id") or ""),
        "token": str(row.get("token") or ""),
        "note": str(row.get("note") or ""),
        "role": str(row.get("role") or ""),
        # 额度：None = 不额外限制（继承角色默认额度）
        "quota": _decode_quota(row.get("quota")),
        "requires_approval": bool(row.get("requires_approval")),
        "max_uses": max_uses,
        "used_count": used_count,
        "remaining": remaining,
        "expires": expires,
        "created": int(row.get("created") or 0),
        "created_by": str(row.get("created_by") or ""),
        "revoked": revoked,
        "last_used": int(row.get("last_used") or 0),
        "last_used_by": str(row.get("last_used_by") or ""),
        "status": status,
    }


# ----------------------------------------------------------------- 写
async def create_invite(
    *,
    role: str,
    note: str = "",
    created_by: str = "",
    requires_approval: bool = True,
    max_uses: Any = DEFAULT_MAX_USES,
    ttl_days: Any = DEFAULT_TTL_DAYS,
    quota: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """新建一条邀请链接（返回里带 token，调用方拼成完整 URL）。"""
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    ttl = normalize_ttl_days(ttl_days)
    row = {
        "id": secrets.token_hex(8),
        "token": token,
        "note": str(note or "")[:191],
        "role": str(role or "viewer")[:64],
        "requires_approval": 1 if requires_approval else 0,
        # 空 = 不额外限制（继承角色），不要写空对象：那是「显式全不限制」
        "quota": json.dumps(quota, ensure_ascii=False) if quota else "",
        "max_uses": normalize_max_uses(max_uses),
        "used_count": 0,
        "expires": 0 if ttl == 0 else now + ttl * 86400,
        "created": now,
        "created_by": str(created_by or "")[:64],
        "revoked": 0,
    }
    async with database.connect() as db:
        await db.execute(
            "INSERT INTO registration_invites"
            " (id, token, note, role, requires_approval, quota, max_uses,"
            "  used_count, expires, created, created_by, revoked)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"],
                row["token"],
                row["note"],
                row["role"],
                row["requires_approval"],
                row["quota"],
                row["max_uses"],
                row["used_count"],
                row["expires"],
                row["created"],
                row["created_by"],
                row["revoked"],
            ),
        )
        await db.commit()
    return {**public_view(row), "ttl_days": ttl}


# ----------------------------------------------------------------- 读
async def list_invites(limit: int = 100) -> List[Dict[str, Any]]:
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM registration_invites ORDER BY created DESC LIMIT ?",
                (max(1, min(200, int(limit or 100))),),
            )
            rows = await cursor.fetchall()
        return [public_view(_row(row)) for row in rows]
    except Exception:  # noqa: BLE001 - 列表失败不让设置页整块报错
        logger.warning("读取邀请链接列表失败", exc_info=True)
        return []


async def get_invite(invite_id: str) -> Optional[Dict[str, Any]]:
    if not invite_id:
        return None
    async with database.connect() as db:
        cursor = await db.execute(
            "SELECT * FROM registration_invites WHERE id=?", (str(invite_id),)
        )
        row = await cursor.fetchone()
    return public_view(_row(row)) if row is not None else None


async def peek(token: str) -> Optional[Dict[str, Any]]:
    """只读判活（不消费）。注册页拿它来展示「这个链接还能不能用、给什么角色」。"""
    if not token:
        return None
    now = int(time.time())
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM registration_invites"
                " WHERE token=? AND revoked=0 AND (expires=0 OR expires>?)"
                "   AND (max_uses=0 OR used_count<max_uses)",
                (str(token), now),
            )
            row = await cursor.fetchone()
        return public_view(_row(row)) if row is not None else None
    except Exception:  # noqa: BLE001 - 判活失败按「不可用」处理
        logger.warning("查询邀请链接失败", exc_info=True)
        return None


# ----------------------------------------------------------------- 消费 / 回滚
async def consume(token: str, *, used_by: str) -> Optional[Dict[str, Any]]:
    """原子占用一次名额；不可用返回 ``None``。

    条件 UPDATE 而不是「先查后改」：同一个链接被并发提交时，只有
    ``used_count < max_uses`` 的那个请求拿到 ``rowcount=1``。
    """
    if not token:
        return None
    now = int(time.time())
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "UPDATE registration_invites"
                " SET used_count = used_count + 1, last_used = ?, last_used_by = ?"
                " WHERE token=? AND revoked=0 AND (expires=0 OR expires>?)"
                "   AND (max_uses=0 OR used_count<max_uses)",
                (now, str(used_by or "")[:191], str(token), now),
            )
            if int(cursor.rowcount or 0) != 1:
                await db.commit()
                return None
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM registration_invites WHERE token=?", (str(token),)
            )
            row = await cursor.fetchone()
        return public_view(_row(row))
    except Exception:  # noqa: BLE001
        logger.warning("消费邀请链接失败", exc_info=True)
        return None


async def release(invite_id: str) -> None:
    """把刚占用的一次退回去（建号失败时用，别让人白丢一次机会）。"""
    if not invite_id:
        return
    try:
        async with database.connect() as db:
            # SET 按从左到右依次生效：两条 CASE 必须排在递减之前，
            # 否则读到的是已经减过的 used_count（旧值 2 会被误判成「最后一次」）
            await db.execute(
                "UPDATE registration_invites"
                " SET last_used = CASE WHEN used_count<=1 THEN 0 ELSE last_used END,"
                "     last_used_by = CASE WHEN used_count<=1 THEN '' ELSE last_used_by END,"
                "     used_count = used_count - 1"
                " WHERE id=? AND used_count>0",
                (str(invite_id),),
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("退回邀请链接名额失败", exc_info=True)


# ----------------------------------------------------------------- 失效 / 删除
async def revoke(invite_id: str) -> bool:
    if not invite_id:
        return False
    async with database.connect() as db:
        cursor = await db.execute(
            "UPDATE registration_invites SET revoked=1 WHERE id=?", (str(invite_id),)
        )
        await db.commit()
        return int(cursor.rowcount or 0) == 1


async def delete_invite(invite_id: str) -> bool:
    """只允许删除**已经不可能再用**的链接（失效 / 过期 / 次数用尽）。"""
    if not invite_id:
        return False
    now = int(time.time())
    async with database.connect() as db:
        cursor = await db.execute(
            "DELETE FROM registration_invites"
            " WHERE id=? AND (revoked<>0 OR (expires>0 AND expires<=?)"
            "   OR (max_uses>0 AND used_count>=max_uses))",
            (str(invite_id), now),
        )
        await db.commit()
        return int(cursor.rowcount or 0) == 1
