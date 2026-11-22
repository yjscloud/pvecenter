"""注册邮箱验证码：一次性数字码的签发、投递、校验与限频。

为什么要有这一层
----------------
自助注册（``open`` 模式）是完全匿名的入口：填一个邮箱就能提交。此前它只靠
人机校验 + 别名伪装挡着，邮箱是否真实、是否属于提交者，后端一无所知 ——
于是「用别人的邮箱注册」「一个人批量注册」都不需要任何成本，审批队列里也
分不清哪条是真的。

这里补上的是一件事：**提交前先证明你收得到这个邮箱的信**。验证码发到用户
填写的邮箱，只有拿到码的人才能把注册提交完。

刻意的设计取舍
--------------
* **只存哈希**。库里落 ``hmac_sha256(secret, purpose|email|code)``，6 位数字
  只有一百万种取值，明文落库等于把库读走就能反推所有人的验证码。
* **只保留最新一条**。同一邮箱再发一次，旧码立刻作废（删除），否则用户手上
  会有好几条都能用的码，而「过期时间」「错误次数」到底算哪一条会说不清。
* **错误次数用尽即销毁**。达到上限后这条码直接删掉，只能重新获取 —— 留着它
  只会让「已锁定」与「可以再试」这两种状态混在一起。
* **限频分三层**：同一邮箱每小时 N 条（默认 5）、同一 IP 每小时 10 条、同一
  邮箱两次发送之间还有冷却（默认 60 秒）。前两层挡批量，第三层挡连点。
* **过期不自动续期**。过期就是过期，用户必须重新获取（这正是「过期后的重发
  逻辑」）：重发接口本身带冷却与上限，不会变成无限续期。
* 计数落 MySQL（``rate_limits`` 表，见 :mod:`app.throttle`），多进程 / 重启
  都不会把限额清零。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from typing import Any, Dict, Optional, Tuple

from . import database, mailer, throttle
from . import store
from .config import settings

logger = logging.getLogger(__name__)

#: 目前只有注册用；留成参数是为了以后「换绑邮箱 / 敏感操作二次确认」复用同一张表。
PURPOSE_REGISTER = "register"

#: 6 位数字：够短能抄，够长不至于被穷举（配合错误次数上限与 10 分钟有效期）。
CODE_LEN = 6

#: 校验结果。刻意不用 bool：前端要分别提示「没发过」「已过期」「填错了还剩几次」
#: 「错误次数用尽，请重新获取」，这些是四种不同的下一步动作。
RESULT_OK = "ok"
RESULT_MISSING = "missing"
RESULT_EXPIRED = "expired"
RESULT_INVALID = "invalid"
RESULT_TOO_MANY = "too_many"

#: 发送结果（``issue`` 的返回）。
SEND_OK = "sent"
SEND_COOLDOWN = "cooldown"
SEND_RATE_LIMITED = "rate_limited"
SEND_MAIL_FAILED = "mail_failed"

#: 同一来源 IP 每小时的发送上限。单邮箱上限管「别把一个人打爆」，这条管
#: 「别换个邮箱接着刷」——两者用不同的桶，互不挤占。
IP_HOURLY_LIMIT = 10

#: settings KV 里的开关键。没配过（None）才回落到 ``REGISTER_EMAIL_CODE``：
#: 与注册方式同一套取舍 —— 管理员在设置页点一次就以设置页为准，环境变量只
#: 作为老部署的默认值保留，改一个开关不必进容器改配置再重启。
SETTING_KEY = "register_email_code"

SCHEMA = """
CREATE TABLE IF NOT EXISTS email_verification_codes (
    id        VARCHAR(32)  NOT NULL,
    purpose   VARCHAR(32)  NOT NULL DEFAULT 'register',
    email     VARCHAR(191) NOT NULL,
    code_hash CHAR(64)     NOT NULL,
    created   DOUBLE       NOT NULL DEFAULT 0,
    expires   DOUBLE       NOT NULL DEFAULT 0,
    attempts  INT          NOT NULL DEFAULT 0,
    used      TINYINT      NOT NULL DEFAULT 0,
    ip        VARCHAR(64)  NOT NULL DEFAULT '',
    PRIMARY KEY (id),
    KEY idx_evc_lookup (purpose, email, created),
    KEY idx_evc_expires (expires)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


async def init_table() -> None:
    async with database.connect() as db:
        await db.executescript(SCHEMA)
        await db.commit()


# --------------------------------------------------------------------- 配置
#: 有效期 / 重试上限 / 重发冷却都取 ``REGISTER_EMAIL_CODE_*`` 环境变量（见
#: app/config.py 与 .env.example）。每次现读 settings 而不是在导入时固化成常量：
#: 测试会临时改小它们来覆盖过期与重试，固化了就只能改模块属性。
def ttl_seconds() -> int:
    return max(60, int(settings.register_email_code_ttl_minutes) * 60)


async def enabled() -> bool:
    """邮箱验证码是否启用：设置页的开关优先，没配过才看 ``REGISTER_EMAIL_CODE``。

    返回 False 时自助注册退回「只要人机校验」的老行为；邀请制 / 注册码制本来
    就不看这个开关。
    """
    raw = await store.get_setting(SETTING_KEY)
    if raw is None:
        return bool(settings.register_email_code)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


async def set_enabled(value: bool) -> bool:
    """写入开关（调用方负责权限、step-up 与审计）。返回落库后的值。"""
    await store.set_setting(SETTING_KEY, "1" if value else "0")
    return bool(value)


def max_attempts() -> int:
    return max(1, int(settings.register_email_code_max_attempts))


def resend_after() -> int:
    return max(0, int(settings.register_email_code_resend_seconds))


def hourly_limit() -> int:
    return max(1, int(settings.register_email_code_hourly_limit))


def normalise_email(value: Any) -> str:
    """邮箱归一：去空白 + 小写。验证码按这个形状存与查，大小写不影响校验。"""
    return str(value or "").strip().lower()[:191]


def _hash_code(purpose: str, email: str, code: str) -> str:
    key = str(getattr(settings, "secret_key", "") or "").encode("utf-8")
    message = f"{purpose}|{email}|{code}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _new_code() -> str:
    return str(secrets.randbelow(10**CODE_LEN)).zfill(CODE_LEN)


# ------------------------------------------------------------------ 查询辅助
async def _latest_row(email: str, purpose: str) -> Optional[Dict[str, Any]]:
    """该邮箱最近一条**未使用**的验证码（无论是否过期）。"""
    async with database.connect() as db:
        cursor = await db.execute(
            "SELECT id, email, created, expires, attempts, used"
            " FROM email_verification_codes"
            " WHERE purpose = ? AND email = ? AND used = 0"
            " ORDER BY created DESC LIMIT 1",
            (purpose, email),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def _last_created(email: str, purpose: str) -> float:
    """最近一次发送时间（含已使用的行）：冷却按「发信」算，不按「有效码」算。"""
    async with database.connect() as db:
        cursor = await db.execute(
            "SELECT created FROM email_verification_codes"
            " WHERE purpose = ? AND email = ? ORDER BY created DESC LIMIT 1",
            (purpose, email),
        )
        row = await cursor.fetchone()
    return float(row["created"]) if row else 0.0


async def _drop(email: str, purpose: str) -> None:
    async with database.connect() as db:
        await db.execute(
            "DELETE FROM email_verification_codes WHERE purpose = ? AND email = ?",
            (purpose, email),
        )
        await db.commit()


# ---------------------------------------------------------------------- 签发
async def issue(
    email: str, ip: str = "", purpose: str = PURPOSE_REGISTER
) -> Tuple[str, Dict[str, Any]]:
    """签发并投递一条验证码，返回 ``(状态, 元信息)``。

    元信息里的 ``retry_after`` 是前端倒计时用的秒数：冷却 / 限频时给「还要等
    多久」，成功时给「多久后可以重发」。调用方据此拼文案，不自己算时间。
    """
    mail = normalise_email(email)
    meta: Dict[str, Any] = {
        "email": mail,
        "ttl_seconds": ttl_seconds(),
        "max_attempts": max_attempts(),
        "resend_after": resend_after(),
        "retry_after": 0,
    }
    if not mail:
        return SEND_MAIL_FAILED, meta

    now = time.time()

    # 冷却：同一邮箱两次发送之间必须隔开，压住「连点发送」这类手速攻击。
    last = await _last_created(mail, purpose)
    if last and now - last < resend_after():
        meta["retry_after"] = int(resend_after() - (now - last)) + 1
        return SEND_COOLDOWN, meta

    # 单邮箱小时上限：管理员发码也要守，否则一个地址一天能收上百封。
    allowed, retry = await throttle.allow(
        f"emailcode:email:{purpose}:{mail}", hourly_limit(), 3600
    )
    if not allowed:
        meta["retry_after"] = retry
        return SEND_RATE_LIMITED, meta

    # 来源 IP 上限：换邮箱接着刷的那批人在这里被挡住。
    allowed, retry = await throttle.allow(
        f"emailcode:ip:{purpose}:{ip or 'unknown'}", IP_HOURLY_LIMIT, 3600
    )
    if not allowed:
        meta["retry_after"] = retry
        return SEND_RATE_LIMITED, meta

    # 同一邮箱只留最新一条：旧码作废，避免「手上三条码都能用」。
    await _drop(mail, purpose)

    code = _new_code()
    row_id = secrets.token_hex(8)
    async with database.connect() as db:
        # 顺手清掉早已过期的行（含别人的），这张表只该有短命数据。
        await db.execute(
            "DELETE FROM email_verification_codes WHERE expires < ?", (now - 86400,)
        )
        await db.execute(
            "INSERT INTO email_verification_codes"
            " (id, purpose, email, code_hash, created, expires, attempts, used, ip)"
            " VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)",
            (
                row_id,
                purpose,
                mail,
                _hash_code(purpose, mail, code),
                now,
                now + ttl_seconds(),
                (ip or "")[:64],
            ),
        )
        await db.commit()

    # 参数一律按关键字传：第三位是「可输错次数」，曾经把 purpose 顶进去，
    # 于是邮件正文写成「最多可以输错 register 次」。
    subject, body, html = mailer.email_code_mail(
        code,
        minutes=max(1, ttl_seconds() // 60),
        attempts=max_attempts(),
        purpose=purpose,
    )
    ok, detail = await mailer.send_mail(mail, subject, body, html=html)
    if not ok:
        # 没发出去就别把码留在库里：用户收不到，重试还要等冷却，只会更迷惑。
        await _drop(mail, purpose)
        logger.warning("注册验证码发送失败（%s）：%s", mail, detail)
        meta["detail"] = detail
        return SEND_MAIL_FAILED, meta

    return SEND_OK, meta


# ---------------------------------------------------------------------- 校验
async def verify(
    email: str, code: str, purpose: str = PURPOSE_REGISTER
) -> Tuple[str, Dict[str, Any]]:
    """校验一条验证码，返回 ``(结果, 元信息)``。

    成功即作废（``used = 1``）：同一个码不能用来注册两个账号。失败按原因分档，
    让前端能说清「是填错了（还能试几次）」还是「得重新获取」。
    """
    mail = normalise_email(email)
    meta: Dict[str, Any] = {"remaining_attempts": 0, "ttl_seconds": ttl_seconds()}
    given = "".join(str(code or "").split())
    if not mail or not given:
        return RESULT_MISSING, meta

    row = await _latest_row(mail, purpose)
    if row is None:
        return RESULT_MISSING, meta

    if float(row["expires"] or 0) <= time.time():
        # 过期即销毁：否则下次发码前它会一直占着「最新一条」的位置。
        await _drop(mail, purpose)
        return RESULT_EXPIRED, meta

    attempts = int(row["attempts"] or 0)
    if attempts >= max_attempts():
        await _drop(mail, purpose)
        return RESULT_TOO_MANY, meta

    return await _compare_and_commit(mail, purpose, given, row, meta)


async def _compare_and_commit(
    email: str,
    purpose: str,
    given: str,
    row: Dict[str, Any],
    meta: Dict[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    """比较并落库。抽出来是为了让「成功作废」与「失败计数」只有一处实现。

    库里重新读一遍 ``code_hash`` / ``attempts``：上面那次查询之后，并发的另一个
    请求可能已经把这条码用掉了，或已经把错误次数加上去了。
    """
    row_id = str(row["id"])
    digest = _hash_code(purpose, email, given)
    async with database.connect() as db:
        cursor = await db.execute(
            "SELECT code_hash, attempts FROM email_verification_codes"
            " WHERE id = ? AND used = 0",
            (row_id,),
        )
        fresh = await cursor.fetchone()
        if fresh is None:
            # 并发下被别的请求先用掉了：等同于「没有可用的码」。
            return RESULT_MISSING, meta

        expected = str(fresh["code_hash"] or "")
        attempts = int(fresh["attempts"] or 0)
        if hmac.compare_digest(expected, digest):
            cursor = await db.execute(
                "UPDATE email_verification_codes SET used = 1"
                " WHERE id = ? AND used = 0",
                (row_id,),
            )
            await db.commit()
            if (cursor.rowcount or 0) <= 0:
                return RESULT_MISSING, meta
            meta["remaining_attempts"] = max(0, max_attempts() - attempts)
            return RESULT_OK, meta

        attempts += 1
        await db.execute(
            "UPDATE email_verification_codes SET attempts = ? WHERE id = ?",
            (attempts, row_id),
        )
        await db.commit()

    left = max(0, max_attempts() - attempts)
    meta["remaining_attempts"] = left
    if left <= 0:
        await _drop(email, purpose)
        return RESULT_TOO_MANY, meta
    return RESULT_INVALID, meta
