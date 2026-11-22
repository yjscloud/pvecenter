"""自助注册的「方式」与「凭证」。

四种模式互斥，同一时间只有一种生效（存 settings KV 的 ``registration_mode``）：

============  ==========================================================
``off``       关闭注册：注册接口 403，登录页不展示入口
``invite``    邀请制：必须持有一枚邀请凭证才能提交注册
``code``      注册码制：必须持有一枚注册码才能提交注册
``open``      自助注册：任何人（通过人机校验与审批）都能提交
============  ==========================================================

为什么模式存 settings 而不是环境变量：报告与运维都要求「在系统设置里切换、
切换后立即生效」。环境变量要重启面板才生效，也不该让运维为了改一个开关去
碰容器配置。环境变量 ``REGISTER_ENABLED`` 只作为**未设置时的默认值**保留，
老部署的行为不变。

凭证模型与 :mod:`app.connregister` 同一套取舍：

* 只存哈希（sha256），明文只在签发响应里出现一次；
* 原子消费：``UPDATE ... WHERE used_count<max_uses AND expires>now``，并发下
  不会超发；
* 邀请凭证与注册码是同一张表的两种 ``kind``：模式决定认哪一种，另一种即使
  没删也用不了（避免「切模式后旧码还能用」）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from typing import Any, Dict, List, Optional

from . import database, store
from .config import settings

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------- 模式
MODE_OFF = "off"
MODE_INVITE = "invite"
MODE_CODE = "code"
MODE_OPEN = "open"
MODES = (MODE_OFF, MODE_INVITE, MODE_CODE, MODE_OPEN)

#: 需要**文本凭证**的模式 → 凭证类型
#:
#: 邀请制不在其中：它只认邀请链接（见 app/invites.py），链接里带着 token，
#: 注册页不再出现「邀请码」输入框。``KIND_INVITE`` 常量与表里的历史邀请码
#: 保留（存量数据不迁移），但不再签发、也不再校验。
MODE_KIND: Dict[str, Optional[str]] = {
    MODE_OFF: None,
    MODE_INVITE: None,
    MODE_CODE: "code",
    MODE_OPEN: None,
}

#: settings KV 里的键。空值 = 没配过，回落到 ``REGISTER_ENABLED``。
SETTING_KEY = "registration_mode"

# ----------------------------------------------------------------- 凭证
KIND_INVITE = "invite"
KIND_CODE = "code"
KINDS = (KIND_INVITE, KIND_CODE)

PREFIX = {KIND_INVITE: "zpinv_", KIND_CODE: "zpreg_"}

#: 邀请凭证默认 7 天（要发出去等人点，太短等于没用）；
#: 注册码默认 24 小时（人手分发，通常当天用掉）。
DEFAULT_TTL_MINUTES = {KIND_INVITE: 7 * 1440, KIND_CODE: 24 * 60}
MAX_TTL_MINUTES = 30 * 1440

DEFAULT_MAX_USES = 1
MAX_USES_LIMIT = 1000

SCHEMA = """
CREATE TABLE IF NOT EXISTS registration_codes (
    id          VARCHAR(40)  NOT NULL,
    code_hash   CHAR(64)     NOT NULL,
    kind        VARCHAR(16)  NOT NULL DEFAULT 'code',
    note        VARCHAR(191) NOT NULL DEFAULT '',
    created_by  VARCHAR(64)  NOT NULL DEFAULT '',
    -- 新用户的角色与资源额度（JSON，字段见 app.quota 的 LIMIT_GROUPS）。
    -- 存量记录：role 由 DEFAULT 补成 viewer，quota 为 NULL（= 不额外限制）。
    role        VARCHAR(64)  NOT NULL DEFAULT 'viewer',
    quota       TEXT,
    -- 注册后是否需要管理员审批：1 = 建号为 pending，0 = 建好即可登录
    requires_approval TINYINT NOT NULL DEFAULT 1,
    created     BIGINT       NOT NULL DEFAULT 0,
    expires     BIGINT       NOT NULL DEFAULT 0,
    max_uses    INT          NOT NULL DEFAULT 1,
    used_count  INT          NOT NULL DEFAULT 0,
    used        BIGINT       NOT NULL DEFAULT 0,
    used_by     VARCHAR(191) NOT NULL DEFAULT '',
    revoked     TINYINT      NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uk_reg_code_hash (code_hash),
    KEY idx_reg_code_created (created),
    KEY idx_reg_code_kind (kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


async def init_table() -> None:
    async with database.connect() as db:
        await db.executescript(SCHEMA)
        await db.commit()
        # 老库补列：role / quota 是后加的（注册码也要能预先指定角色与额度）；
        # requires_approval 同理，存量行默认 1 —— 升级前后都要审批，不能因为
        # 升级就把老码变成免审批。quota 为 NULL = 不额外限制，按角色默认额度走。
        await database.add_columns(
            db,
            "registration_codes",
            (
                ("role", "ALTER TABLE registration_codes ADD COLUMN role VARCHAR(64) NOT NULL DEFAULT 'viewer'"),
                ("quota", "ALTER TABLE registration_codes ADD COLUMN quota TEXT"),
                ("requires_approval", "ALTER TABLE registration_codes ADD COLUMN requires_approval TINYINT NOT NULL DEFAULT 1"),
            ),
        )
        await db.commit()


# ----------------------------------------------------------------- 模式读写
def normalize_mode(value: Any) -> Optional[str]:
    """把外部输入收敛成合法模式；不认识返回 ``None``（由调用方决定报错）。"""
    text = str(value or "").strip().lower()
    return text if text in MODES else None


async def configured_mode() -> Optional[str]:
    """settings 里显式配置过的模式；没配过返回 ``None``。"""
    raw = await store.get_setting(SETTING_KEY)
    return normalize_mode(raw)


async def current_mode() -> str:
    """当前生效的注册模式。

    没在系统设置里配过时回落到环境变量：``REGISTER_ENABLED=true`` → 自助注册，
    否则关闭。这样升级前面的部署行为完全不变，升级后管理员在设置页选一次，
    之后就以设置页为准。
    """
    mode = await configured_mode()
    if mode is not None:
        return mode
    return MODE_OPEN if bool(settings.register_enabled) else MODE_OFF


async def set_mode(mode: str) -> str:
    """写入模式（调用方负责权限、step-up 与审计）。返回落库后的值。"""
    normalized = normalize_mode(mode)
    if normalized is None:
        raise ValueError("未知的注册方式")
    await store.set_setting(SETTING_KEY, normalized)
    return normalized


# ----------------------------------------------------------------- 凭证实现
def _hash(code: str) -> str:
    return hashlib.sha256(str(code or "").strip().encode("utf-8")).hexdigest()


def looks_like(code: str, kind: str) -> bool:
    prefix = PREFIX.get(kind) or ""
    return bool(prefix) and str(code or "").strip().startswith(prefix)


def normalize_kind(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    return text if text in KINDS else None


def normalize_max_uses(value: Any) -> int:
    """0 = 不限；其余收敛到 1..MAX_USES_LIMIT。"""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_USES
    if count <= 0:
        return 0
    return min(count, MAX_USES_LIMIT)


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
    """列表视图：**不含**明文（库里也没有），只给状态与剩余次数。"""
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
        status = "used" if max_uses == 1 else "exhausted"
    elif expires <= now:
        status = "expired"
    else:
        status = "active"
    return {
        "id": str(row.get("id") or ""),
        "kind": str(row.get("kind") or ""),
        "note": str(row.get("note") or ""),
        # 注册时写进新账号的角色与额度（额度为 None = 不额外限制，继承角色）
        "role": str(row.get("role") or "viewer"),
        "quota": _decode_quota(row.get("quota")),
        # 与邀请链接同一口径：默认要审批。老数据 / 手工插入的行没这一列时
        # 也按「要审批」处理，别让缺失字段变成一条免审批通道。
        "requires_approval": (
            True if row.get("requires_approval") is None
            else bool(int(row.get("requires_approval")))
        ),
        "created_by": str(row.get("created_by") or ""),
        "created": int(row.get("created") or 0),
        "expires": expires,
        "max_uses": max_uses,
        "used_count": used_count,
        "remaining": remaining,
        "used": int(row.get("used") or 0),
        "used_by": str(row.get("used_by") or ""),
        "status": status,
    }


async def create_code(
    *,
    kind: str,
    note: str = "",
    created_by: str = "",
    ttl_minutes: Optional[int] = None,
    max_uses: Any = DEFAULT_MAX_USES,
    role: str = "viewer",
    quota: Optional[Dict[str, Any]] = None,
    requires_approval: bool = True,
) -> Dict[str, Any]:
    """签发一枚凭证，返回里带**明文**（只此一次）。

    ``role`` / ``quota`` 会随凭证一起保存，注册时写进新账号 —— 注册码制也
    因此可以「提前指定角色与额度」，而不必等审批时再手动设一遍。
    ``requires_approval=False`` 时这张码建出来的账号直接是 active。
    """
    normalized = normalize_kind(kind)
    if normalized is None:
        raise ValueError("未知的凭证类型")
    ttl = int(ttl_minutes or DEFAULT_TTL_MINUTES[normalized])
    ttl = max(1, min(MAX_TTL_MINUTES, ttl))
    uses = normalize_max_uses(max_uses)
    plain = PREFIX[normalized] + secrets.token_urlsafe(24)
    now = int(time.time())
    row = {
        "id": secrets.token_hex(8),
        "code_hash": _hash(plain),
        "kind": normalized,
        "note": str(note or "")[:191],
        "created_by": str(created_by or "")[:64],
        "role": str(role or "viewer")[:64],
        # quota=None = 不额外限制（仍按角色默认额度继承），不写空对象 ——
        # 空对象会变成「显式全不限制」，把角色的额度盖掉
        "quota": json.dumps(quota, ensure_ascii=False) if quota else "",
        "requires_approval": 1 if requires_approval else 0,
        "created": now,
        "expires": now + ttl * 60,
        "max_uses": uses,
        "used_count": 0,
    }
    async with database.connect() as db:
        await db.execute(
            "INSERT INTO registration_codes"
            " (id, code_hash, kind, note, created_by, role, quota,"
            "  requires_approval, created, expires, max_uses, used_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"],
                row["code_hash"],
                row["kind"],
                row["note"],
                row["created_by"],
                row["role"],
                row["quota"],
                row["requires_approval"],
                row["created"],
                row["expires"],
                row["max_uses"],
                row["used_count"],
            ),
        )
        await db.commit()
    return {
        **public_view(row),
        "code": plain,
        "ttl_minutes": ttl,
    }


async def check(code: str, kind: str) -> Optional[Dict[str, Any]]:
    """只读判活（不消费）。不可用返回 ``None``。

    注册流程先判活再走后面的校验，是为了在「口令强度不够 / 用户名已占用」
    这类错误的场景里**不扣次数**；真正占用额度发生在建号前一刻。
    """
    if not looks_like(code, kind):
        return None
    now = int(time.time())
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM registration_codes"
                " WHERE code_hash=? AND kind=? AND revoked=0 AND expires>?"
                "   AND (max_uses=0 OR used_count<max_uses)",
                (_hash(code), kind, now),
            )
            row = await cursor.fetchone()
        return public_view(_row(row)) if row is not None else None
    except Exception:  # noqa: BLE001 - 判活失败按「不可用」处理
        logger.warning("查询注册凭证失败", exc_info=True)
        return None


async def consume(code: str, kind: str, *, used_by: str) -> Optional[Dict[str, Any]]:
    """原子占用一次额度；不可用返回 ``None``。

    条件 UPDATE 而非「先查后改」：同一枚码被并发提交时，只有
    ``used_count < max_uses`` 的那个请求拿到 ``rowcount=1``。
    """
    if not looks_like(code, kind):
        return None
    now = int(time.time())
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "UPDATE registration_codes"
                " SET used_count = used_count + 1,"
                "     used = CASE WHEN used=0 THEN ? ELSE used END,"
                "     used_by = ?"
                " WHERE code_hash=? AND kind=? AND revoked=0 AND expires>?"
                "   AND (max_uses=0 OR used_count<max_uses)",
                (now, str(used_by or "")[:191], _hash(code), kind, now),
            )
            if int(cursor.rowcount or 0) != 1:
                await db.commit()
                return None
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM registration_codes WHERE code_hash=?",
                (_hash(code),),
            )
            row = await cursor.fetchone()
        # 走 public_view：role / quota 已解析好，注册流程直接取用
        return public_view(_row(row)) if row is not None else None
    except Exception:  # noqa: BLE001
        logger.warning("消费注册凭证失败", exc_info=True)
        return None


async def release(code_id: str) -> None:
    """把刚占用的一次额度退回去（建号失败时用，别让用户白丢一次机会）。"""
    if not code_id:
        return
    try:
        async with database.connect() as db:
            await db.execute(
                "UPDATE registration_codes"
                " SET used = CASE WHEN used_count<=1 THEN 0 ELSE used END,"
                "     used_by = CASE WHEN used_count<=1 THEN '' ELSE used_by END,"
                "     used_count = used_count - 1"
                " WHERE id=? AND used_count>0 AND revoked=0",
                (str(code_id),),
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("退回注册凭证失败", exc_info=True)


async def list_codes(kind: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    try:
        async with database.connect() as db:
            if kind:
                cursor = await db.execute(
                    "SELECT * FROM registration_codes WHERE kind=?"
                    " ORDER BY created DESC LIMIT ?",
                    (kind, max(1, min(200, int(limit or 100)))),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM registration_codes ORDER BY created DESC LIMIT ?",
                    (max(1, min(200, int(limit or 100))),),
                )
            rows = await cursor.fetchall()
        return [public_view(_row(row)) for row in rows]
    except Exception:  # noqa: BLE001
        logger.warning("读取注册凭证列表失败", exc_info=True)
        return []


async def get_code(code_id: str) -> Optional[Dict[str, Any]]:
    if not code_id:
        return None
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM registration_codes WHERE id=?", (str(code_id),)
            )
            row = await cursor.fetchone()
        return public_view(_row(row)) if row is not None else None
    except Exception:  # noqa: BLE001
        logger.warning("读取注册凭证失败", exc_info=True)
        return None


async def revoke(code_id: str) -> bool:
    if not code_id:
        return False
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "UPDATE registration_codes SET revoked=1 WHERE id=?", (str(code_id),)
            )
            await db.commit()
            return int(cursor.rowcount or 0) == 1
    except Exception:  # noqa: BLE001
        logger.warning("撤销注册凭证失败", exc_info=True)
        return False


async def delete_code(code_id: str) -> bool:
    """只允许删除**已经不可能再用**的凭证（撤销 / 过期 / 次数用尽）。"""
    if not code_id:
        return False
    now = int(time.time())
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "DELETE FROM registration_codes"
                " WHERE id=? AND (revoked<>0 OR expires<=?"
                "   OR (max_uses>0 AND used_count>=max_uses))",
                (str(code_id), now),
            )
            await db.commit()
            return int(cursor.rowcount or 0) == 1
    except Exception:  # noqa: BLE001
        logger.warning("删除注册凭证失败", exc_info=True)
        return False
