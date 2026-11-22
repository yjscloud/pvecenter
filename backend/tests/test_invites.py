"""邀请链接的纯逻辑：数值收敛 + 列表状态。

不碰数据库：``check`` / ``consume`` 的并发语义靠条件 UPDATE 保证，验证它需要
真库（见 conftest 的集成用例），这里只钉住「外部输入怎么收敛」与「一条记录
对外该显示成什么状态」—— 这两件是本模块所有校验的入口。
"""
from __future__ import annotations

import time

from app import invites


def test_normalize_max_uses() -> None:
    assert invites.normalize_max_uses("3") == 3
    assert invites.normalize_max_uses(1) == 1
    # 0 / 负数都表示「不限」，不是「不能用」
    assert invites.normalize_max_uses(0) == 0
    assert invites.normalize_max_uses(-5) == 0
    # 脏输入回落到默认值，而不是抛异常把管理接口打成 500
    assert invites.normalize_max_uses("abc") == invites.DEFAULT_MAX_USES
    assert invites.normalize_max_uses(None) == invites.DEFAULT_MAX_USES
    assert invites.normalize_max_uses(10**6) == invites.MAX_USES_LIMIT


def test_normalize_ttl_days() -> None:
    assert invites.normalize_ttl_days("7") == 7
    # 0 / 负数 = 永久
    assert invites.normalize_ttl_days(0) == 0
    assert invites.normalize_ttl_days(-1) == 0
    assert invites.normalize_ttl_days("oops") == invites.DEFAULT_TTL_DAYS
    assert invites.normalize_ttl_days(10**6) == invites.MAX_TTL_DAYS


def _row(**overrides: object) -> dict:
    base = {
        "id": "abc",
        "token": "tok",
        "note": "",
        "role": "viewer",
        "requires_approval": 1,
        "max_uses": 2,
        "used_count": 1,
        "expires": 0,
        "created": 0,
        "created_by": "admin",
        "revoked": 0,
        "last_used": 0,
        "last_used_by": "",
    }
    base.update(overrides)
    return base


def test_public_view_active_with_remaining() -> None:
    view = invites.public_view(_row())
    assert view["status"] == "active"
    assert view["remaining"] == 1
    assert view["requires_approval"] is True


def test_public_view_unlimited_has_no_remaining() -> None:
    view = invites.public_view(_row(max_uses=0, used_count=9))
    assert view["status"] == "active"
    # 0 = 不限次数：remaining 用 None 表示，避免前端把它画成「剩 0 次」
    assert view["remaining"] is None


def test_public_view_exhausted() -> None:
    assert invites.public_view(_row(max_uses=2, used_count=2))["status"] == "exhausted"


def test_public_view_expired() -> None:
    past = int(time.time()) - 60
    view = invites.public_view(_row(expires=past))
    assert view["status"] == "expired"
    # 永久链接（expires=0）不能因为「0 小于 now」被判过期
    assert invites.public_view(_row(expires=0))["status"] == "active"


def test_public_view_revoked_wins() -> None:
    future = int(time.time()) + 3600
    view = invites.public_view(_row(revoked=1, expires=future))
    assert view["status"] == "revoked"
