"""PVE 节点「一键接入」注册码。

为什么需要它：新建连接要求 step-up 二次确认，而 step-up 对 API Token 一律 403
（见 :func:`app.security.require_step_up`）—— 无人值守脚本拿不到。注册码把「管理员
在浏览器里的一次授权」换成一张**短期、一次性**的凭据：脚本拿着它只能建**一条**
连接，其余能力一概没有，也无法用来改配置或读数据。

三条边界：

* 只存哈希（sha256），明文只在签发响应里出现一次 —— 与 :mod:`app.apitokens` 同一取舍；
* 原子消费：``UPDATE ... WHERE used=0 AND expires>now``，并发下只有一个请求能成功；
* 与连接列表解耦：注册码只负责「准许接入」，写哪条连接由
  :func:`app.routers.config.register_connection` 决定（含按集群判重）。

表很小（几条到几十条），过期行不主动清理：列表接口按 ``created`` 倒序取，
发码频率是人手级，不必为它加一个清理任务。
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import Any, Dict, List, Optional

from . import database

logger = logging.getLogger(__name__)

#: 注册码前缀。与 API Token 的 ``zp_`` 区分开，日志/截图上肉眼可辨。
PREFIX = "pvir_"

#: 注册码默认寿命与上限。批量接入时通常几分钟内就用掉；给到 24 小时是上限，
#: 再长就等于一张长期有效的「建连接许可证」，不如直接发 Token。
DEFAULT_TTL_MINUTES = 30
MAX_TTL_MINUTES = 1440

#: 默认可用次数（1 = 一次性，与多用途之前的语义完全一致）。
DEFAULT_MAX_USES = 1
#: ``max_uses = 0`` 表示不限次数。上限只是防手滑：上万次的码已经等于长期凭据，
#: 想长期自动化应该发 API Token。
MAX_USES_LIMIT = 1000

#: ``console_enabled`` / ``read_only`` 是历史列：一键接入已改成「一律 root@pam 全权
#: + 一律带控制台」，签发时不再有这两个选项，列也不再参与任何判断。保留它们是为了
#: 老库不必重建表（本模块的迁移策略只加列不删列），新行写入时用默认值。
SCHEMA = """
CREATE TABLE IF NOT EXISTS connection_register_codes (
    id            VARCHAR(40)  NOT NULL,
    code_hash     CHAR(64)     NOT NULL,
    region        VARCHAR(64)  NOT NULL DEFAULT '',
    note          VARCHAR(191) NOT NULL DEFAULT '',
    created_by    VARCHAR(64)  NOT NULL DEFAULT '',
    created       BIGINT       NOT NULL DEFAULT 0,
    expires       BIGINT       NOT NULL DEFAULT 0,
    max_uses      INT          NOT NULL DEFAULT 1,
    used_count    INT          NOT NULL DEFAULT 0,
    console_enabled TINYINT    NOT NULL DEFAULT 0,
    read_only     TINYINT      NOT NULL DEFAULT 0,
    used          BIGINT       NOT NULL DEFAULT 0,
    used_by       VARCHAR(191) NOT NULL DEFAULT '',
    connection_id VARCHAR(40)  NOT NULL DEFAULT '',
    revoked       TINYINT      NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uk_conn_reg_hash (code_hash),
    KEY idx_conn_reg_created (created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


async def init_table() -> None:
    async with database.connect() as db:
        await db.executescript(SCHEMA)
        await db.commit()

        # 旧库补列：多用途之前没有 max_uses / used_count。补完要把「已经用过」
        # 的行回填成 used_count=1 —— 否则 max_uses 默认 1 而 used_count 还是 0，
        # 那些早就用掉的码会突然变回「可用 1 次」。
        columns = await database.table_columns(db, "connection_register_codes")
        if "max_uses" not in columns:
            await db.execute(
                "ALTER TABLE connection_register_codes"
                " ADD COLUMN max_uses INT NOT NULL DEFAULT 1"
            )
        if "used_count" not in columns:
            await db.execute(
                "ALTER TABLE connection_register_codes"
                " ADD COLUMN used_count INT NOT NULL DEFAULT 0"
            )
        # 两个历史列：现在签发不再有「控制台 / 只读」选项，补列只为老库结构一致，
        # 值不再被读取
        if "console_enabled" not in columns:
            await db.execute(
                "ALTER TABLE connection_register_codes"
                " ADD COLUMN console_enabled TINYINT NOT NULL DEFAULT 0"
            )
        if "read_only" not in columns:
            await db.execute(
                "ALTER TABLE connection_register_codes"
                " ADD COLUMN read_only TINYINT NOT NULL DEFAULT 0"
            )
        await db.execute(
            "UPDATE connection_register_codes SET used_count=1"
            " WHERE used<>0 AND used_count=0"
        )
        await db.commit()


def _hash(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def looks_like_code(value: str) -> bool:
    return str(value or "").strip().startswith(PREFIX)


def _row(raw: Any) -> Dict[str, Any]:
    return dict(raw) if raw is not None else {}


def normalize_max_uses(value: Any) -> int:
    """把界面/接口传来的次数收敛到合法区间：0 = 不限，其余 1..MAX_USES_LIMIT。"""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_USES
    if count <= 0:
        return 0
    return min(count, MAX_USES_LIMIT)


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    """列表视图：**不含**明文码（库里也没有），只给状态与剩余次数。"""
    now = int(time.time())
    expires = int(row.get("expires") or 0)
    used = int(row.get("used") or 0)
    revoked = bool(row.get("revoked"))
    max_uses = int(row.get("max_uses") or 0)
    # 旧行没有 used_count 时按「用过就是 1 次」算，别让它们显示成 0/1
    used_count = int(row.get("used_count") or (1 if used else 0))
    remaining: Optional[int] = None if max_uses == 0 else max(0, max_uses - used_count)
    exhausted = max_uses > 0 and used_count >= max_uses

    if revoked:
        status = "revoked"
    elif exhausted and max_uses == 1:
        # 一次性码用掉之后仍叫 used：这是升级前的既有状态名，前端与日志都在用
        status = "used"
    elif exhausted:
        status = "exhausted"
    elif expires <= now:
        status = "expired"
    else:
        status = "active"
    return {
        "id": str(row.get("id") or ""),
        "region": str(row.get("region") or ""),
        "note": str(row.get("note") or ""),
        "created_by": str(row.get("created_by") or ""),
        "created": int(row.get("created") or 0),
        "expires": expires,
        "max_uses": max_uses,
        "used_count": used_count,
        "remaining": remaining,
        "used": used,
        "used_by": str(row.get("used_by") or ""),
        "connection_id": str(row.get("connection_id") or ""),
        "status": status,
    }


async def create_code(
    *,
    region: str,
    note: str,
    created_by: str,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    max_uses: int = DEFAULT_MAX_USES,
) -> Dict[str, Any]:
    """签发一张注册码。返回里带**明文**（只此一次）。

    权限与控制台都没有开关了：脚本一律给节点上的 ``root@pam`` 建令牌（面板拿到
    的是 root 全权），并一律创建控制台账号（PVE 的 VNC 要账号密码换票据，令牌
    换不了）。这两个历史列留着不写，让老库照旧工作。
    """
    ttl = max(1, min(MAX_TTL_MINUTES, int(ttl_minutes or DEFAULT_TTL_MINUTES)))
    uses = normalize_max_uses(max_uses)
    plain = PREFIX + secrets.token_urlsafe(32)
    now = int(time.time())
    row = {
        "id": secrets.token_hex(8),
        "code_hash": _hash(plain),
        "region": str(region or "")[:64],
        "note": str(note or "")[:191],
        "created_by": str(created_by or "")[:64],
        "created": now,
        "expires": now + ttl * 60,
        "max_uses": uses,
        "used_count": 0,
    }
    async with database.connect() as db:
        await db.execute(
            "INSERT INTO connection_register_codes"
            " (id, code_hash, region, note, created_by, created, expires,"
            "  max_uses, used_count)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                row["id"],
                row["code_hash"],
                row["region"],
                row["note"],
                row["created_by"],
                row["created"],
                row["expires"],
                row["max_uses"],
                row["used_count"],
            ),
        )
        await db.commit()
    return {
        **row,
        "code": plain,
        "ttl_minutes": ttl,
        "remaining": None if uses == 0 else uses,
    }


async def peek(code: str) -> Optional[Dict[str, Any]]:
    """查注册码状态（**不消费**），不存在返回 ``None``。

    用途只有一个：生成安装脚本前先判活。用户拿着一条已经过期 / 用过的链接去节点
    上跑，脚本会在第 6 步才失败 —— 前面几步已经建了账号和令牌，虽然会回滚，但
    白白折腾一趟。提前判活后，面板直接回一段「这条链接已失效」的脚本。
    """
    if not looks_like_code(code):
        return None
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM connection_register_codes WHERE code_hash=?",
                (_hash(code),),
            )
            row = await cursor.fetchone()
        return _public(_row(row)) if row is not None else None
    except Exception:  # noqa: BLE001 - 判活失败按「不可用」处理，不影响注册本身
        logger.warning("查询注册码失败", exc_info=True)
        return None


async def consume(code: str, *, used_by: str) -> Optional[Dict[str, Any]]:
    """原子占用一次使用额度；不可用（不存在/过期/次数用尽/已撤销）返回 ``None``。

    用一条带条件的 ``UPDATE`` 判定，而不是「先查后改」：多台节点同时拿同一张码
    注册时，只有 ``used_count < max_uses`` 的那些请求能拿到 ``rowcount=1``，
    第 N+1 个自然失败 —— 不需要额外加锁。

    ``used`` 仍表示「第一次使用的时间」（0 = 从未用过），列表与旧前端都按它判断
    「这张码有没有被动过」。
    """
    if not looks_like_code(code):
        return None
    now = int(time.time())
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "UPDATE connection_register_codes"
                " SET used_count = used_count + 1,"
                "     used = CASE WHEN used=0 THEN ? ELSE used END,"
                "     used_by = ?"
                " WHERE code_hash=? AND revoked=0 AND expires>?"
                "   AND (max_uses=0 OR used_count<max_uses)",
                (now, str(used_by or "")[:191], _hash(code), now),
            )
            if int(cursor.rowcount or 0) != 1:
                await db.commit()
                return None
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM connection_register_codes WHERE code_hash=?",
                (_hash(code),),
            )
            row = await cursor.fetchone()
        return _row(row)
    except Exception:  # noqa: BLE001 - 注册链路的失败要由调用方给出明确提示
        logger.warning("消费注册码失败", exc_info=True)
        return None


async def release(code_id: str) -> None:
    """把刚占用的一次额度退回去（次数 -1）。

    两种情况走这里：

    * 探活失败 / 地址冲突 —— 码不该因为运维打错一个 IP 就少一次；
    * 重复接入（``unchanged``）—— 面板没做任何改动，自然不该扣次数。不然在同一
      个集群的第二台节点上跑一遍，剩余次数会莫名其妙少一，而列表里什么都没多。

    只用「次数减到 0 才清掉 used / used_by」的条件写法，避免覆盖掉真实的使用记录。
    """
    if not code_id:
        return
    try:
        async with database.connect() as db:
            await db.execute(
                "UPDATE connection_register_codes"
                # MySQL 的 SET 按书写顺序生效：先算 used/used_by（用的是旧
                # used_count），再减 used_count，语义就不依赖求值顺序。
                " SET used = CASE WHEN used_count<=1 THEN 0 ELSE used END,"
                "     used_by = CASE WHEN used_count<=1 THEN '' ELSE used_by END,"
                "     used_count = used_count - 1"
                " WHERE id=? AND used_count>0 AND revoked=0",
                (str(code_id),),
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("退回注册码失败", exc_info=True)


async def bind_connection(code_id: str, connection_id: str) -> None:
    """把注册码和它建出来的连接关联起来（列表里能看到「这张码接的是哪台」）。"""
    if not code_id:
        return
    try:
        async with database.connect() as db:
            await db.execute(
                "UPDATE connection_register_codes SET connection_id=? WHERE id=?",
                (str(connection_id or "")[:40], str(code_id)),
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("回填注册码对应连接失败", exc_info=True)


async def list_codes(limit: int = 50) -> List[Dict[str, Any]]:
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM connection_register_codes"
                " ORDER BY created DESC LIMIT ?",
                (max(1, min(200, int(limit or 50))),),
            )
            rows = await cursor.fetchall()
        return [_public(_row(row)) for row in rows]
    except Exception:  # noqa: BLE001
        logger.warning("读取注册码列表失败", exc_info=True)
        return []


async def get_code(code_id: str) -> Optional[Dict[str, Any]]:
    """按 id 取一条（不含明文），给「删除前判状态」用。"""
    if not code_id:
        return None
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM connection_register_codes WHERE id=?",
                (str(code_id),),
            )
            row = await cursor.fetchone()
        return _public(_row(row)) if row is not None else None
    except Exception:  # noqa: BLE001
        logger.warning("读取注册码失败", exc_info=True)
        return None


async def delete_code(code_id: str) -> bool:
    """删除一条**已经不可能再用**的注册码（已撤销 / 次数用尽 / 已过期）。

    还剩可用次数的码不能删：删掉它等于给了「撤销」一条后门 —— 码从列表里消失了，
    可对方手里的链接照样能建连接，看起来却像「已经处理过」。要清理就先撤销。

    ``connection_id`` 指向的连接不受影响：删的是凭据记录，不是接入结果。
    """
    if not code_id:
        return False
    now = int(time.time())
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "DELETE FROM connection_register_codes"
                # 「已失效」的判定和 _public 的 status 必须一致：撤销 / 过期 /
                # 次数用尽。只看 used 会把「5 次用掉 2 次、仍在有效期」的码也划进来。
                " WHERE id=? AND (revoked<>0 OR expires<=?"
                "   OR (max_uses>0 AND used_count>=max_uses))",
                (str(code_id), now),
            )
            await db.commit()
            return int(cursor.rowcount or 0) == 1
    except Exception:  # noqa: BLE001
        logger.warning("删除注册码失败", exc_info=True)
        return False


async def revoke(code_id: str) -> bool:
    """作废一张码的剩余额度。

    以前只允许撤销「还没用过的」码（``used=0``），因为那时一次性码用过就等于废了。
    支持多用途后，一张用了 1/3 次、还有 2 次额度的码同样需要能作废 —— 否则「重新
    生成一条接着用」时旧码会一直留着，两条并行谁也说不清用哪条。
    """
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "UPDATE connection_register_codes SET revoked=1 WHERE id=?",
                (str(code_id),),
            )
            await db.commit()
            return int(cursor.rowcount or 0) == 1
    except Exception:  # noqa: BLE001
        logger.warning("撤销注册码失败", exc_info=True)
        return False
