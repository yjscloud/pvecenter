"""证书流水的持久化层：定义版本、运行实例、步骤明细、产物。

四张表的职责边界
----------------
* ``cert_flow_definition``：**可回滚的流水定义**。代码里的内置定义在启动时按
  ``(username, name, version)`` upsert 一条；要回滚就把 ``enabled`` 切到旧版本，
  不必改代码重新发版。
* ``cert_flow_run``：一次运行一行。状态、起止时间、错误原文，是「这张证书哪天
  被谁用哪版流水续的」的唯一答案。
* ``cert_flow_step``：运行内每步一行，含节点类型与原始错误。一次运行最多十来行，
  按 ``run_id`` 直查即可，不做分页。
* ``cert_flow_artifact``：产物索引（kind / checksum / size / meta）。

为什么默认不落 payload
----------------------
证书私钥本来就以明文存在站点 JSON 里（``acme_key``），但这不代表新表应该再存
一份：产物表的价值在「有哪些产物、指纹是多少、来自哪个节点」，正文由站点配置
与部署目标持有。需要留档时用 ``store_payload=True`` 显式打开。

与旧结构的关系
--------------
``site["last_run"]`` 仍然是读接口的真相来源（投影见 ``certflow.project_last_run``）。
本模块只写不读，属于**旁路**：表写失败只记日志，绝不让一次签发失败。这样默认
关闭开关时行为与改动前完全一致，回滚就是关掉 ``CERTFLOW_ENABLED``。
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Dict, List, Optional

from . import database
from .certflow import Artifact, Definition, Run, RunStep

logger = logging.getLogger(__name__)


def _now() -> int:
    """本地取时间戳：刻意不从 certflow 借私有函数，仓储层保持可独立替换。"""
    return int(time.time())

SCHEMA = """
CREATE TABLE IF NOT EXISTS cert_flow_definition (
    id          BIGINT       NOT NULL AUTO_INCREMENT,
    username    VARCHAR(64)  NOT NULL DEFAULT '',
    name        VARCHAR(128) NOT NULL,
    version     INT          NOT NULL DEFAULT 1,
    description VARCHAR(255) NOT NULL DEFAULT '',
    definition  MEDIUMTEXT   NOT NULL,
    enabled     TINYINT      NOT NULL DEFAULT 1,
    updated_at  BIGINT       NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uk_cert_flow_definition (username, name, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS cert_flow_run (
    id          VARCHAR(48)  NOT NULL,
    username    VARCHAR(64)  NOT NULL DEFAULT '',
    site_id     VARCHAR(64)  NOT NULL DEFAULT '',
    site_name   VARCHAR(128) NOT NULL DEFAULT '',
    domain      VARCHAR(255) NOT NULL DEFAULT '',
    pipeline    VARCHAR(128) NOT NULL DEFAULT '',
    version     INT          NOT NULL DEFAULT 1,
    trigger_by  VARCHAR(32)  NOT NULL DEFAULT '',
    status      VARCHAR(16)  NOT NULL DEFAULT '',
    error       TEXT,
    value_json  MEDIUMTEXT,
    started_at  BIGINT       NOT NULL DEFAULT 0,
    finished_at BIGINT       NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    KEY idx_cert_flow_run_site (username, site_id, started_at DESC),
    KEY idx_cert_flow_run_time (started_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS cert_flow_step (
    id        BIGINT      NOT NULL AUTO_INCREMENT,
    run_id    VARCHAR(48) NOT NULL,
    seq       INT         NOT NULL DEFAULT 0,
    node_id   VARCHAR(64) NOT NULL DEFAULT '',
    node_type VARCHAR(48) NOT NULL DEFAULT '',
    status    VARCHAR(16) NOT NULL DEFAULT '',
    detail    TEXT,
    error     TEXT,
    ms        INT         NOT NULL DEFAULT 0,
    at_ms     INT         NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    KEY idx_cert_flow_step_run (run_id, seq)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS cert_flow_artifact (
    id         BIGINT      NOT NULL AUTO_INCREMENT,
    run_id     VARCHAR(48) NOT NULL,
    node_id    VARCHAR(64) NOT NULL DEFAULT '',
    kind       VARCHAR(32) NOT NULL,
    checksum   CHAR(64)    NOT NULL DEFAULT '',
    size       INT         NOT NULL DEFAULT 0,
    meta_json  TEXT,
    encoding   VARCHAR(16) NOT NULL DEFAULT 'none',
    payload    LONGTEXT,
    created_at BIGINT      NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    KEY idx_cert_flow_artifact_run (run_id, kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

#: 运行记录的保留条数。每次 tick 每个站点最多一条，保留 500 条够回看几个月，
#: 又不至于把 MySQL 撑成日志仓库；超出部分在写完新记录后顺带删掉。
KEEP_RUNS = 500
#: 每多少次运行做一轮清理（进程内计数，多 worker 各自计数不影响正确性，
#: 最坏情况是清得勤一点）。
PRUNE_EVERY = 200

_finishes = 0


async def init_table() -> None:
    async with database.connect() as db:
        await db.executescript(SCHEMA)
        await db.commit()


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - 无法序列化的值不该拖垮落库
        return ""


def _encode(artifact: Artifact, store_payload: bool) -> Dict[str, Any]:
    if not store_payload:
        return {"encoding": "none", "payload": None}
    if isinstance(artifact.payload, (bytes, bytearray)):
        return {
            "encoding": "base64",
            "payload": base64.b64encode(bytes(artifact.payload)).decode("ascii"),
        }
    return {"encoding": "text", "payload": str(artifact.payload)}


# ------------------------------------------------------------------ 定义（版本）
async def save_definition(definition: Definition, owner: str = "") -> None:
    """按 ``(owner, name, version)`` upsert 一条定义。

    新版本写入时**不动**旧版本的 ``enabled``：回滚靠显式切换，而不是写入时
    顺手把历史版本关掉 —— 后者会让「回滚到上一版」需要重新启用一次。
    """
    async with database.connect() as db:
        await db.execute(
            "INSERT INTO cert_flow_definition"
            " (username, name, version, description, definition, enabled, updated_at)"
            " VALUES (?,?,?,?,?,1,?)"
            " ON DUPLICATE KEY UPDATE"
            " description=VALUES(description), definition=VALUES(definition),"
            " updated_at=VALUES(updated_at)",
            (
                str(owner or ""),
                definition.name,
                int(definition.version),
                definition.description[:255],
                _json(definition.as_dict()),
                _now(),
            ),
        )
        await db.commit()


async def set_definition_enabled(
    name: str, version: int, enabled: bool, owner: str = ""
) -> bool:
    """启用/停用某个版本的流水定义 —— 一键回滚的入口。"""
    async with database.connect() as db:
        cursor = await db.execute(
            "UPDATE cert_flow_definition SET enabled = ?, updated_at = ?"
            " WHERE username = ? AND name = ? AND version = ?",
            (1 if enabled else 0, _now(), str(owner or ""), name, int(version)),
        )
        await db.commit()
    return bool(cursor.rowcount)


async def definitions(owner: str = "", enabled_only: bool = False) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM cert_flow_definition WHERE username = ?"
    params: List[Any] = [str(owner or "")]
    if enabled_only:
        sql += " AND enabled = 1"
    sql += " ORDER BY name, version DESC"
    async with database.connect() as db:
        cursor = await db.execute(sql, tuple(params))
        rows = await cursor.fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["definition"] = json.loads(item.get("definition") or "{}")
        except Exception:  # noqa: BLE001 - 脏数据不影响列表其它行
            item["definition"] = {}
        result.append(item)
    return result


# ------------------------------------------------------------------ 运行（旁路写）
class SqlSink:
    """把运行过程写进四张表。所有写失败只记日志，绝不向上抛。

    Runner 对 sink 的调用本身已经吞异常（见 ``certflow.Runner._call``），这里再加
    一层是给**直接调用**本类的场景兜底。
    """

    def __init__(self, site: Dict[str, Any], store_payload: bool = False) -> None:
        self.site = site or {}
        self.store_payload = store_payload
        # 步骤序号自己数：Runner 可能在运行结束后才回放 steps，用 len(run.steps)
        # 会让每一条都拿到同一个序号。
        self._seq: Dict[str, int] = {}

    async def start(self, run: Run) -> None:
        try:
            async with database.connect() as db:
                await db.execute(
                    "INSERT INTO cert_flow_run"
                    " (id, username, site_id, site_name, domain, pipeline, version,"
                    " trigger_by, status, error, value_json, started_at, finished_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run.id,
                        str(self.site.get("username") or run.owner or ""),
                        str(self.site.get("id") or run.site_id or ""),
                        str(self.site.get("name") or "")[:128],
                        str(self.site.get("domain") or "")[:255],
                        run.pipeline,
                        int(run.version),
                        run.trigger[:32],
                        run.status,
                        "",
                        "",
                        int(run.started_at),
                        int(run.finished_at),
                    ),
                )
                await db.commit()
        except Exception:  # noqa: BLE001 - 可观测性不能变成新的失败点
            logger.debug("写入证书流水运行记录失败", exc_info=True)

    async def step(self, run: Run, step: RunStep) -> None:
        try:
            seq = self._seq.get(run.id, 0) + 1
            self._seq[run.id] = seq
            async with database.connect() as db:
                await db.execute(
                    "INSERT INTO cert_flow_step"
                    " (run_id, seq, node_id, node_type, status, detail, error, ms, at_ms)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        run.id,
                        seq,
                        step.node_id,
                        step.node_type,
                        step.status,
                        step.detail,
                        step.error,
                        int(step.ms),
                        int(step.at),
                    ),
                )
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.debug("写入证书流水步骤失败", exc_info=True)

    async def finish(self, run: Run) -> None:
        try:
            async with database.connect() as db:
                await db.execute(
                    "UPDATE cert_flow_run SET status=?, error=?, value_json=?,"
                    " finished_at=? WHERE id=?",
                    (
                        run.status,
                        run.error[:2000],
                        _json(run.value),
                        int(run.finished_at or _now()),
                        run.id,
                    ),
                )
                for artifact in run.artifacts:
                    encoded = _encode(artifact, self.store_payload)
                    await db.execute(
                        "INSERT INTO cert_flow_artifact"
                        " (run_id, node_id, kind, checksum, size, meta_json,"
                        " encoding, payload, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            run.id,
                            artifact.node_id[:64],
                            artifact.kind[:32],
                            artifact.checksum,
                            int(artifact.size),
                            _json(artifact.meta),
                            encoded["encoding"],
                            encoded["payload"],
                            _now(),
                        ),
                    )
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.debug("写入证书流水产物失败", exc_info=True)
        # 清理不是每次运行都值得做：站点多的时候一轮就是几百条 run，
        # 每条都扫一遍 id 列表的代价远高于「晚几百条才清掉」。
        global _finishes
        _finishes += 1
        if _finishes % PRUNE_EVERY == 0:
            await prune(keep=KEEP_RUNS)


# ---------------------------------------------------------------------- 读取
async def list_runs(
    owner: Optional[str] = None, site_id: str = "", limit: int = 50
) -> List[Dict[str, Any]]:
    """运行列表。``owner=None`` 表示管理员视角（跨用户）。"""
    clauses: List[str] = []
    params: List[Any] = []
    if owner is not None:
        clauses.append("username = ?")
        params.append(str(owner))
    if site_id:
        clauses.append("site_id = ?")
        params.append(str(site_id))
    sql = "SELECT * FROM cert_flow_run"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(max(1, min(500, int(limit))))
    async with database.connect() as db:
        cursor = await db.execute(sql, tuple(params))
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_run(run_id: str, include_payload: bool = False) -> Optional[Dict[str, Any]]:
    """运行详情：运行头 + 步骤 + 产物索引（默认不含产物正文）。"""
    async with database.connect() as db:
        cursor = await db.execute("SELECT * FROM cert_flow_run WHERE id = ?", (run_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        head = dict(row)
        cursor = await db.execute(
            "SELECT * FROM cert_flow_step WHERE run_id = ? ORDER BY seq", (run_id,)
        )
        steps = [dict(r) for r in await cursor.fetchall()]
        columns = "id, run_id, node_id, kind, checksum, size, meta_json, encoding, created_at"
        if include_payload:
            columns += ", payload"
        cursor = await db.execute(
            f"SELECT {columns} FROM cert_flow_artifact WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        artifacts = []
        for item in await cursor.fetchall():
            record = dict(item)
            try:
                record["meta"] = json.loads(record.pop("meta_json") or "{}")
            except Exception:  # noqa: BLE001
                record["meta"] = {}
            artifacts.append(record)
    head["steps"] = steps
    head["artifacts"] = artifacts
    return head


async def prune(keep: int = KEEP_RUNS) -> int:
    """只保留最近 ``keep`` 条运行，连同步骤与产物一起清掉。

    先查出要删的 id 再逐表删：MySQL 不允许在子查询里引用正在删除的同一张表，
    这个写法绕开了那条限制，也让每次删除的行数可控。
    """
    keep = max(20, int(keep))
    async with database.connect() as db:
        # 表大小由本函数自身维持在 keep 附近，所以一次取回 id 是可控的；
        # 用 LIMIT ... OFFSET 反而在 offset 上做无用扫描。
        cursor = await db.execute(
            "SELECT id FROM cert_flow_run ORDER BY started_at DESC", ()
        )
        stale = [str(r["id"]) for r in (await cursor.fetchall())[keep:]]
        if not stale:
            return 0
        marks = ",".join("?" for _ in stale)
        await db.execute(f"DELETE FROM cert_flow_step WHERE run_id IN ({marks})", tuple(stale))
        await db.execute(
            f"DELETE FROM cert_flow_artifact WHERE run_id IN ({marks})", tuple(stale)
        )
        cursor = await db.execute(
            f"DELETE FROM cert_flow_run WHERE id IN ({marks})", tuple(stale)
        )
        await db.commit()
    return cursor.rowcount or 0
