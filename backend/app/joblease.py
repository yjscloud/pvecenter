"""后台作业的跨实例租约。

为什么需要
----------
调度器是「进程内注册 + 进程内计时」的：单实例部署时没问题，但一旦跑两个
副本（滚动升级、双机热备、docker compose scale=2），两个进程会同时到点、
同时执行同一个作业。对证书续期来说，后果是重复签发（消耗 CA 配额、触发
速率限制）、重复部署（多目标轮流写）、以及两份旧快照互相覆盖状态。

实现
----
MySQL 里一张 ``job_lease`` 表，每行一个作业。抢锁是一条原子的
``INSERT ... ON DUPLICATE KEY UPDATE``：只有租约过期（或本进程已持有）
时才把自己写成 owner，随后回读 owner 判断是否抢到。租约带 TTL，进程
崩溃后最多等一个 TTL 就能被别的实例接手，不需要人工清理。

数据库不可用时**不阻断作业**：记一次警告后按「单机模式」继续跑。这保证了
测试环境与尚未配库的启动流程不被租约拖住 —— 代价是这种情况下没有跨实例
保护，与改造前的行为一致。
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from typing import Optional

from . import database

logger = logging.getLogger(__name__)

TABLE = "job_lease"

#: 本进程的身份：主机名 + pid + 随机后缀。重启后 pid 可能复用，随机后缀保证
#: 不会把「上一个进程遗留的租约」误认成自己持有的。
OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  job_id VARCHAR(64) NOT NULL,
  owner VARCHAR(160) NOT NULL,
  lease_until BIGINT NOT NULL,
  PRIMARY KEY (job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_ensure_lock = asyncio.Lock()
_ensured = False
_warned = False


def _table_ready() -> bool:
    return _ensured


async def ensure_table() -> None:
    global _ensured
    if _ensured:
        return
    async with _ensure_lock:
        if _ensured:
            return
        async with database.connect() as db:
            await db.execute(CREATE_SQL)
            await db.commit()
        _ensured = True


async def acquire(job_id: str, ttl: int) -> bool:
    """尝试为 ``job_id`` 抢一个 ``ttl`` 秒的租约。"""
    global _ensured, _warned
    try:
        await ensure_table()
        now = int(time.time())
        until = now + max(int(ttl), 60)
        async with database.connect() as db:
            await db.execute(
                f"INSERT INTO {TABLE} (job_id, owner, lease_until) VALUES (?, ?, ?) "
                "ON DUPLICATE KEY UPDATE "
                "owner = IF(lease_until < ?, VALUES(owner), owner), "
                "lease_until = IF(lease_until < ?, VALUES(lease_until), lease_until)",
                (job_id, OWNER, until, now, now),
            )
            await db.commit()
            cursor = await db.execute(
                f"SELECT owner FROM {TABLE} WHERE job_id = ?", (job_id,)
            )
            row = await cursor.fetchone()
        return bool(row and str(row[0]) == OWNER)
    except Exception:  # noqa: BLE001 - 租约不可用时退回单机行为
        # 表可能被重建/迁移过：下次重新走一遍 ensure_table
        _ensured = False
        if not _warned:
            _warned = True
            logger.warning(
                "作业租约不可用，按单机模式继续（多副本部署时请检查数据库）",
                exc_info=True,
            )
        return True


async def release(job_id: str) -> None:
    """主动释放租约（作业结束）。失败只记日志，TTL 会兜底。"""
    if not _table_ready():
        return
    try:
        async with database.connect() as db:
            await db.execute(
                f"UPDATE {TABLE} SET lease_until = 0 WHERE job_id = ? AND owner = ?",
                (job_id, OWNER),
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.debug("释放作业租约失败：%s", job_id, exc_info=True)


async def peek(job_id: str) -> Optional[dict]:
    """查看当前租约（排障用）。"""
    if not _table_ready():
        return None
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                f"SELECT owner, lease_until FROM {TABLE} WHERE job_id = ?", (job_id,)
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "owner": str(row[0]),
            "lease_until": int(row[1]),
            "held_by_me": str(row[0]) == OWNER,
        }
    except Exception:  # noqa: BLE001
        return None
