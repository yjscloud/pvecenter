"""AI 会话：把「一次性排查」升级成能追问的对话。

为什么要单独一层
----------------
``ai.py`` 的 L2 是「一条请求跑完一次排查」，跑完即散、没有记忆。用户真正想要的
是看完结论后还能追问「那这个进程是谁起的」—— 那就需要一个**线程**把上下文留住。

线程落库而不是只留在前端：刷新页面、换设备、事后审计，都要能看到完整对话。

数据分两层
----------
* ``ai_conversations`` —— 一次会话（谁、哪台机器、产出的报告）；
* ``ai_messages``     —— 会话里每一条消息（user / assistant / tool / context）。

``ai_messages`` 与 OpenAI 的消息数组**同构**：``context`` 行的内容就是当初喂给
模型的 ``[数据]`` 段（重建上下文时映射成 user），``assistant`` 行的 ``tool_calls``
存原始 JSON。于是「把整段对话交给模型」退化成一个直接映射，不用再拼装。

首轮 = 体检并产出报告
---------------------
会话的第一轮承担原来的「排查」职责：采集证据、要求模型输出 findings JSON，
结果存进 ``ai_conversations.report``；之后的轮次是自由追问，不再重采集、不再
强制 JSON。这样「报告」与「对话」就是两个产物，各归各的入口。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from . import ai, aiplaybooks, aitools, aivm, database, hostscope, i18n

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_conversations (
    id        VARCHAR(40)  NOT NULL,
    username  VARCHAR(64)  NOT NULL DEFAULT '',
    host_id   VARCHAR(191) NOT NULL DEFAULT '',
    host_name VARCHAR(191) NOT NULL DEFAULT '',
    provider  VARCHAR(191) NOT NULL DEFAULT '',
    title     VARCHAR(191) NOT NULL DEFAULT '',
    report    MEDIUMTEXT,
    allow_exec TINYINT     NOT NULL DEFAULT 0,
    /* 主机命令的授权级别：read 只读，write 允许提议写命令。
       老数据默认 write，保持合并前的行为；新授权由统一面板显式下发。 */
    exec_access VARCHAR(8)  NOT NULL DEFAULT 'write',
    ctx_summary    MEDIUMTEXT,
    summarized_upto INT    NOT NULL DEFAULT 0,
    created   BIGINT       NOT NULL DEFAULT 0,
    updated   BIGINT       NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    KEY idx_ai_conv_user (username, updated)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ai_messages (
    id              VARCHAR(40)  NOT NULL,
    conversation_id VARCHAR(40)  NOT NULL DEFAULT '',
    seq             INT          NOT NULL DEFAULT 0,
    role            VARCHAR(16)  NOT NULL DEFAULT 'user',
    content         MEDIUMTEXT,
    tool_calls      MEDIUMTEXT,
    tool_call_id    VARCHAR(64)  NOT NULL DEFAULT '',
    tool_name       VARCHAR(64)  NOT NULL DEFAULT '',
    command         VARCHAR(1024) NOT NULL DEFAULT '',
    ok              TINYINT      DEFAULT NULL,
    created         BIGINT       NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    KEY idx_ai_msg_conv (conversation_id, seq)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

# 追问轮次比体检轮少：追问通常一两轮就能答完，多给只是让模型来回查、白烧 token。
MAX_STEPS_CHAT = 4

# 重建上下文时最多带多少条消息。历史无上限增长迟早会把 prompt 撑爆，从**最近**
# 往回取；``context``（巡检证据）单独保留，见 :func:`_context_rows`。
MAX_CONTEXT_MESSAGES = 40

# 上下文**内容**的 token 预算（按 :func:`ai._estimate_tokens` 的口径估算）。
#
# 只按条数裁是不够的：一条工具输出最多 4000 字，40 条里混几条就是上万 token，
# 而这些内容每一步模型调用都会重发一次 —— 直接体现为首字慢、每步都慢。
# ``context``（体检证据）优先保留，余下的额度留给最近的往来。
# 可在模型设置里用 ``context_tokens`` 覆盖（见 ai.USER_SETTING_KEYS 的使用方式）。
MAX_CONTEXT_TOKENS = 12000

# 重建上下文时除了证据行，额外多取几条「尾巴」：token 裁剪会再收一遍，取多几条
# 才不至于把预算浪费在「取了又删」上。
CONTEXT_TAIL_MESSAGES = MAX_CONTEXT_MESSAGES + 8

# 被裁掉的旧消息累计到这个数，才值得花一次模型调用去摘要 —— 一两条不值得。
SUMMARY_MIN_MESSAGES = 8
# 送进摘要的原文字符上限，防止一次塞太多。
SUMMARY_MAX_CHARS = 4000

# 同一会话的并发闸门：一次只允许一轮在跑。
#
# 两个标签页同时发消息会让 seq 交错、上下文错乱；而且后端在同一会话上并发跑
# 两轮，模型额度也会翻倍。锁在进程内、按 conversation_id 分开，彼此不干扰。
_locks: Dict[str, asyncio.Lock] = {}


def _conversation_lock(conversation_id: str) -> asyncio.Lock:
    """取（必要时创建）某条会话的锁。锁随会话懒建，只在该会话第一次跑时创建。"""
    lock = _locks.get(conversation_id)
    if lock is None:
        # 简单防泄漏：历史会话很多时，把已经没人持有的锁清掉再建
        if len(_locks) > 500:
            for key in [k for k, v in _locks.items() if not v.locked()]:
                _locks.pop(key, None)
        lock = _locks[conversation_id] = asyncio.Lock()
    return lock


def _evidence_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """挑出要带进上下文的证据行 —— **只留最新那一份**。

    同一会话可以反复「重新体检」，每体检一次就追加一行 ``context``。全留下的话，
    每问一句都要把历史上每一份证据重发一遍：体检三次就是三份上万字的 JSON，
    而其中只有最新那份描述的是机器**现在**的样子（旧的那份还会误导模型）。
    """
    head = [row for row in rows if str(row.get("role") or "") == "context"]
    if not head:
        return []
    # 按 seq 排序取最后一条；测试与老数据可能没有 seq，再按 id 兜底保持插入序
    head.sort(
        key=lambda row: (int(row.get("seq") or 0), str(row.get("id") or ""))
    )
    return head[-1:]


def _context_rows(
    rows: List[Dict[str, Any]], keep_recent: int = MAX_CONTEXT_MESSAGES
) -> List[Dict[str, Any]]:
    """挑出重建上下文要用的消息。

    不能简单地 ``rows[-N:]``：巡检证据是 ``context`` 行，长对话里它会被切掉 ——
    模型于是彻底忘了这台机器体检出过什么，后面的追问全靠猜。所以**最新那份**
    证据永远保留（见 :func:`_evidence_rows`），再拼上最近的 N 条；中间的旧轮次让位。
    """
    # 保留「最近的 N 条」与「最新那份证据」，其余让位。按原顺序输出：
    # 用对象身份去重，同一条消息只留一次，且不打乱时序（模型侧要求严格时序）。
    keep_recent = max(0, int(keep_recent or 0))
    evidence = _evidence_rows(rows)
    latest = id(evidence[-1]) if evidence else -1
    keep = {id(row) for row in evidence}
    for row in rows[-keep_recent:] if keep_recent else []:
        # 旧证据哪怕落在最近 N 条里也不带：它描述的不是机器现在的样子
        if str(row.get("role") or "") == "context" and id(row) != latest:
            continue
        keep.add(id(row))
    return [row for row in rows if id(row) in keep]


def _row_tokens(row: Dict[str, Any]) -> int:
    """估一条消息占多少输入 token（正文 + 工具调用参数 + 少量协议开销）。"""
    total = ai._estimate_tokens(row.get("content")) + 8
    raw = row.get("tool_calls")
    if raw:
        try:
            total += ai._estimate_tokens(
                raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            )
        except (TypeError, ValueError):
            pass
    return total


def _fit_context_tokens(
    rows: List[Dict[str, Any]], budget: int = MAX_CONTEXT_TOKENS
) -> List[Dict[str, Any]]:
    """把 :func:`_context_rows` 挑出来的消息再按 token 预算收一遍。

    ``MAX_CONTEXT_MESSAGES`` 只管条数，管不了体积：一条工具输出最多 4000 字，
    40 条里混几条就能到几万 token，而这份内容**每一步模型调用都要重发一次**。
    这里保留证据行（体检结论不能丢），其余从最近往回装，装到预算为止；至少留
    一条，免得预算配得比单条消息还小的时候把上下文清空。
    """
    if not rows:
        return rows

    head = _evidence_rows(rows)
    body = [row for row in rows if str(row.get("role") or "") != "context"]
    if budget <= 0:
        # 预算关闭：条数不限，但「旧证据」仍然不该进 prompt
        head_ids = {id(row) for row in head}
        return [
            row
            for row in rows
            if str(row.get("role") or "") != "context" or id(row) in head_ids
        ]
    used = sum(_row_tokens(row) for row in head)

    kept: List[Dict[str, Any]] = []
    for row in reversed(body):
        cost = _row_tokens(row)
        if kept and used + cost > budget:
            break
        used += cost
        kept.append(row)
    kept.reverse()
    return head + kept


async def init_table() -> None:
    async with database.connect() as db:
        await db.executescript(SCHEMA)
        columns = await database.table_columns(db, "ai_conversations")
        if "report" not in columns:
            await db.execute("ALTER TABLE ai_conversations ADD COLUMN report MEDIUMTEXT")
        # 会话级的「允许上机执行」授权：记在会话上，刷新页面后开关能回到用户
        # 上次的选择，而不是悄悄退回关闭、下一句追问就少了上机能力。
        if "allow_exec" not in columns:
            await db.execute(
                "ALTER TABLE ai_conversations ADD COLUMN allow_exec TINYINT"
                " NOT NULL DEFAULT 0"
            )
        # 授权级别：老会话已经有 allow_exec=1 的，默认 write 保持原行为；
        # 新会话由统一授权显式写 read / write，避免「只读」被旧默认值放宽。
        if "exec_access" not in columns:
            await db.execute(
                "ALTER TABLE ai_conversations ADD COLUMN exec_access VARCHAR(8)"
                " NOT NULL DEFAULT 'write'"
            )
        # 历史压缩：被裁掉的旧轮次压成要点摘要存这里；summarized_upto 记到哪一条为止
        if "ctx_summary" not in columns:
            await db.execute("ALTER TABLE ai_conversations ADD COLUMN ctx_summary MEDIUMTEXT")
        if "summarized_upto" not in columns:
            await db.execute(
                "ALTER TABLE ai_conversations ADD COLUMN summarized_upto INT"
                " NOT NULL DEFAULT 0"
            )
        # 工具行补列：这条工具调用**实际在主机上跑的命令**。参数（unit=sshd）
        # 不等于命令（journalctl -u sshd -n 60），回看历史时必须能看到后者 ——
        # 「AI 到底在我机器上敲了什么」是用户最想核对的一件事。
        msg_columns = await database.table_columns(db, "ai_messages")
        if "command" not in msg_columns:
            await db.execute(
                "ALTER TABLE ai_messages ADD COLUMN command VARCHAR(1024)"
                " NOT NULL DEFAULT ''"
            )
        await db.commit()


def new_id() -> str:
    return uuid.uuid4().hex


# ------------------------------------------------------------------ 会话读写
async def create_conversation(
    *,
    username: str,
    host_id: str,
    host_name: str,
    provider: str = "",
    title: str = "",
    allow_exec: bool = False,
    exec_access: str = "",
) -> Dict[str, Any]:
    now = int(time.time())
    level = "read" if not allow_exec else ("read" if exec_access == "read" else "write")
    conv: Dict[str, Any] = {
        "id": new_id(),
        "username": str(username or ""),
        "host_id": str(host_id or ""),
        "host_name": str(host_name or "")[:191],
        "provider": str(provider or "")[:191],
        "title": str(title or "")[:191],
        "report": None,
        "allow_exec": 1 if allow_exec else 0,
        "exec_access": level,
        "created": now,
        "updated": now,
    }
    try:
        async with database.connect() as db:
            await db.execute(
                "INSERT INTO ai_conversations (id, username, host_id, host_name, provider,"
                " title, allow_exec, exec_access, created, updated)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    conv["id"],
                    conv["username"],
                    conv["host_id"],
                    conv["host_name"],
                    conv["provider"],
                    conv["title"],
                    conv["allow_exec"],
                    conv["exec_access"],
                    conv["created"],
                    conv["updated"],
                ),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 - 落库失败不该让「发起排查」直接挂掉
        logger.warning("AI 会话创建失败", exc_info=True)
    return conv


async def set_allow_exec(
    conversation_id: str, enabled: bool, *, exec_access: str = ""
) -> bool:
    """记住这条会话最新一次的「允许上机执行」选择（真和假都写）。

    只在真的时候写、假的时候不写是不行的：用户手动关掉开关后刷新，开关会自己
    弹回打开 —— 那是往「更放权」的方向漂，不能接受。

    ``exec_access`` 不传时保持原级别；关闭授权时强制回到 read，避免下一次开启
    沿用上一次的 write。返回是否写入成功，统一授权接口据此做补偿撤销。
    """
    sets = ["allow_exec=?"]
    params: List[Any] = [1 if enabled else 0]
    if exec_access in ("read", "write"):
        sets.append("exec_access=?")
        params.append(exec_access)
    elif not enabled:
        sets.append("exec_access=?")
        params.append("read")
    params.append(str(conversation_id))
    try:
        async with database.connect() as db:
            await db.execute(
                "UPDATE ai_conversations SET " + ", ".join(sets) + " WHERE id=?",
                tuple(params),
            )
            await db.commit()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("AI 会话授权状态写入失败", exc_info=True)
        return False


async def set_context_summary(conversation_id: str, summary: str, upto_seq: int) -> None:
    """保存历史摘要，并记下「已经压到第几条」（避免下次重复压缩同一段）。"""
    try:
        async with database.connect() as db:
            await db.execute(
                "UPDATE ai_conversations SET ctx_summary=?, summarized_upto=? WHERE id=?",
                (str(summary or "")[:20000], int(upto_seq or 0), str(conversation_id)),
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("AI 会话摘要写入失败", exc_info=True)


async def get_conversation(conversation_id: str) -> Optional[Dict[str, Any]]:
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM ai_conversations WHERE id=?", (str(conversation_id),)
            )
            row = await cursor.fetchone()
    except Exception:  # noqa: BLE001
        logger.warning("读取 AI 会话失败", exc_info=True)
        return None
    return dict(row) if row else None


async def list_conversations(
    *, username: str = "", host_id: str = "", limit: int = 20, offset: int = 0
) -> Dict[str, Any]:
    """会话列表，返回 ``{total, items}``。

    不含 ``report`` 大字段 —— 列表页用不上，白读一遍徒增传输；附上每条会话的
    运行次数，便于一眼看出「这条会话查得深不深」。
    """
    where: List[str] = []
    params: List[Any] = []
    if username:
        where.append("c.username=?")
        params.append(str(username))
    if host_id:
        where.append("c.host_id=?")
        params.append(str(host_id))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    size = max(1, min(100, int(limit or 20)))
    start = max(0, int(offset or 0))
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS n FROM ai_conversations c" + clause, tuple(params)
            )
            row = await cursor.fetchone()
            total = int((dict(row).get("n") if row else 0) or 0)

            cursor = await db.execute(
                "SELECT c.id, c.username, c.host_id, c.host_name, c.provider, c.title,"
                " c.allow_exec, c.exec_access, c.created, c.updated,"
                " (SELECT COUNT(*) FROM ai_sessions s WHERE s.conversation_id=c.id) AS runs"
                " FROM ai_conversations c"
                + clause
                + " ORDER BY c.updated DESC LIMIT ? OFFSET ?",
                tuple(params + [size, start]),
            )
            items = [dict(item) for item in await cursor.fetchall()]
    except Exception:  # noqa: BLE001
        logger.warning("读取 AI 会话列表失败", exc_info=True)
        return {"total": 0, "items": []}
    # host_name 是建会话时的快照（库里那份保留不动），列表里换成**当前**名字：
    # 受管主机改过名之后，这里还显示旧名就认不出是哪台机器了。
    hostscope.apply_display_names(items, await hostscope.current_display_names())
    return {"total": total, "items": items}


async def set_report(conversation_id: str, report: Any) -> None:
    payload: Optional[str] = None
    if report is not None:
        try:
            payload = json.dumps(report, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = None
    try:
        async with database.connect() as db:
            await db.execute(
                "UPDATE ai_conversations SET report=?, updated=? WHERE id=?",
                (payload, int(time.time()), str(conversation_id)),
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("AI 会话报告写入失败", exc_info=True)


def _message_row(
    conversation_id: str,
    *,
    role: str,
    content: str = "",
    tool_calls: Any = None,
    tool_call_id: str = "",
    tool_name: str = "",
    command: str = "",
    ok: Optional[bool] = None,
    now: int = 0,
) -> Dict[str, Any]:
    """把一条协议消息规整成可落库的行（``seq`` 由 :func:`append_messages` 填）。"""
    tool_calls_json: Optional[str] = None
    if tool_calls:
        try:
            tool_calls_json = json.dumps(tool_calls, ensure_ascii=False)
        except (TypeError, ValueError):
            tool_calls_json = None
    return {
        "id": new_id(),
        "conversation_id": str(conversation_id),
        "seq": 0,
        "role": str(role or "user"),
        "content": str(content or ""),
        "tool_calls": tool_calls_json,
        "tool_call_id": str(tool_call_id or ""),
        "tool_name": str(tool_name or "")[:64],
        "command": str(command or "")[:1024],
        # 非工具消息没有成败可言，落 NULL 而不是 0 —— 「失败」和「不适用」必须能区分
        "ok": None if ok is None else (1 if ok else 0),
        "created": now or int(time.time()),
    }


async def append_messages(
    conversation_id: str, messages: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """一次事务追加一组消息（同一轮的多个工具结果）。

    原来每条消息各开一个 ``connect``：SELECT MAX(seq) + INSERT + UPDATE + commit，
    一轮十几次、一次几毫秒到几十毫秒。这里只取一次 seq、``executemany`` 一次插入、
    一次 commit —— 语义与逐条写完全一致（seq 连续、会话 updated 刷新）。
    """
    if not messages:
        return []
    now = int(time.time())
    rows = [
        _message_row(conversation_id, now=now, **item) for item in messages
    ]
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM ai_messages WHERE conversation_id=?",
                (str(conversation_id),),
            )
            fetched = await cursor.fetchone()
            first = int((dict(fetched).get("n") if fetched else 1) or 1)
            for offset, row in enumerate(rows):
                row["seq"] = first + offset
            await db.executemany(
                "INSERT INTO ai_messages (id, conversation_id, seq, role, content,"
                " tool_calls, tool_call_id, tool_name, command, ok, created)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        row["id"],
                        row["conversation_id"],
                        row["seq"],
                        row["role"],
                        row["content"],
                        row["tool_calls"],
                        row["tool_call_id"],
                        row["tool_name"],
                        row["command"],
                        row["ok"],
                        row["created"],
                    )
                    for row in rows
                ],
            )
            await db.execute(
                "UPDATE ai_conversations SET updated=? WHERE id=?",
                (now, str(conversation_id)),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 - 单条消息写失败不该中断整轮对话
        logger.warning("AI 会话消息写入失败", exc_info=True)
    return rows


async def append_message(
    conversation_id: str,
    *,
    role: str,
    content: str = "",
    tool_calls: Any = None,
    tool_call_id: str = "",
    tool_name: str = "",
    command: str = "",
    ok: Optional[bool] = None,
) -> Dict[str, Any]:
    """追加一条消息。seq 在会话内单调递增，保证回放顺序稳定。

    ``command`` 只对工具行有意义：这条调用实际在主机上执行的那条命令。内部工具
    （读体检报告、指标、变更记录）不走命令行，留空。
    """
    rows = await append_messages(
        conversation_id,
        [
            {
                "role": role,
                "content": content,
                "tool_calls": tool_calls,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "command": command,
                "ok": ok,
            }
        ],
    )
    return rows[0] if rows else _message_row(conversation_id, role=role)


async def list_messages(
    conversation_id: str, after_seq: int = 0
) -> List[Dict[str, Any]]:
    """按 seq 顺序取回一条会话的消息。

    ``after_seq`` 用于**增量刷新**：一轮结束后页面只要新追加的那几条，而不是把
    整条会话重新拉一遍（长会话那一下能明显感觉到卡顿，字节数也随轮次线性增长）。
    """
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM ai_messages WHERE conversation_id=? AND seq > ?"
                " ORDER BY seq ASC, created ASC",
                (str(conversation_id), int(after_seq or 0)),
            )
            return [dict(item) for item in await cursor.fetchall()]
    except Exception:  # noqa: BLE001
        logger.warning("读取 AI 会话消息失败", exc_info=True)
        return []


async def list_messages_between(
    conversation_id: str, after_seq: int, upto_seq: int, limit: int = 400
) -> List[Dict[str, Any]]:
    """取 seq 落在 ``(after_seq, upto_seq]`` 的消息（后台摘要用）。"""
    if upto_seq <= after_seq:
        return []
    try:
        async with database.connect() as db:
            cursor = await db.execute(
                # 证据行（context）不是「对话」，而且本身就是几千字的 JSON ——
                # 喂给摘要只会得到一段 meaningless 的复述，还白花一次模型调用
                "SELECT * FROM ai_messages WHERE conversation_id=?"
                " AND role != 'context' AND seq > ? AND seq <= ?"
                " ORDER BY seq ASC, created ASC LIMIT ?",
                (str(conversation_id), int(after_seq), int(upto_seq), int(limit)),
            )
            return [dict(item) for item in await cursor.fetchall()]
    except Exception:  # noqa: BLE001
        logger.warning("读取 AI 会话消息失败（区间）", exc_info=True)
        return []


async def list_context_window(
    conversation_id: str, tail: int = CONTEXT_TAIL_MESSAGES
) -> List[Dict[str, Any]]:
    """重建模型上下文用：证据行（``context``）+ 最近 ``tail`` 条。

    长会话每轮把整张表 ``SELECT *`` 读进内存再裁，读的量随会话长度线性增长，
    而真正会进 prompt 的只有证据行和最近几十条。渲染用的全量读取仍走
    :func:`list_messages`。
    """
    tail = max(1, int(tail or 1))
    try:
        async with database.connect() as db:
            # 只取**最新**一份证据：反复重新体检会在表里留下多份完整快照，
            # 旧的那几份既不该进 prompt，也不必从库里读出来（一份就是上万字）。
            cursor = await db.execute(
                "SELECT * FROM ai_messages WHERE conversation_id=?"
                " AND role='context' ORDER BY seq DESC, created DESC LIMIT 1",
                (str(conversation_id),),
            )
            head = [dict(item) for item in await cursor.fetchall()]
            cursor = await db.execute(
                "SELECT * FROM ai_messages WHERE conversation_id=?"
                " ORDER BY seq DESC, created DESC LIMIT ?",
                (str(conversation_id), tail),
            )
            recent = [dict(item) for item in await cursor.fetchall()]
    except Exception:  # noqa: BLE001
        logger.warning("读取 AI 会话上下文失败", exc_info=True)
        return []

    # 两次查询各取一段，按 id 去重后还原成 seq 顺序（模型侧要求严格时序）
    picked: List[Dict[str, Any]] = []
    seen: set = set()
    for row in head + list(reversed(recent)):
        key = str(row.get("id") or "")
        if key in seen:
            continue
        seen.add(key)
        picked.append(row)
    picked.sort(key=lambda row: (int(row.get("seq") or 0), str(row.get("id") or "")))
    return picked


def owns(conversation: Dict[str, Any], username: str, is_admin: bool) -> bool:
    """会话归属判定。非本人（且非管理员）一律按「不存在」处理，不泄露存在性。"""
    if is_admin:
        return True
    return str(conversation.get("username") or "") == str(username or "")


# ------------------------------------------------------------- 上下文重建
def _sanitize_model_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """保证 tool 消息与带 ``tool_calls`` 的 assistant 消息**成对**出现。

    上游对这件事严格到缺一不可，而两个方向都会落单、都会 400：

    * **调用组缺响应**：assistant 带 ``tool_calls``，但它对应的 tool 响应不齐。
      用户中途「终止」时会留下这种半截组（见 :func:`append_message` 的落库时机），
      回放时上游报「tool_calls 后面必须跟 tool 响应」。
    * **没有调用方的孤儿 tool 消息**：``tool`` 找不到它的 assistant。重建上下文时
      消息是按**条数**裁剪的（见 :func:`_context_rows` 的 ``keep_recent``），剪口完全
      可能落在工具响应行上 —— 前一行被裁掉，这条 ``tool`` 就成了对话的开头。上游
      报「Messages with role 'tool' must be a response to a preceding message with
      'tool_calls'」。它只在**对话长到触发裁剪之后**才出现，所以表现为「聊着聊着
      偶发 400」，最难查。

    两条合成一句：**一条消息只有在它的配对方也被保留时才保留**。宁可少一轮上下文，
    也不能让用户的下一句话直接报错。
    """
    out: List[Dict[str, Any]] = []
    # 已经保留下来的 assistant 声明的 tool_call id；后面的 tool 只有落在其中才留。
    # 用集合而不是「往回看一条」：完整组内的响应是就地 append 的，但如果同一轮里
    # 出现了空 id 或对不上的 id，只有这个集合能把它们认出来。
    kept_calls: set = set()
    i = 0
    total = len(messages)
    while i < total:
        msg = messages[i]
        role = str(msg.get("role") or "")

        if role == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            # 空 id 或对不上任何已保留的调用 → 孤儿，留着就是一次 400
            if call_id and call_id in kept_calls:
                out.append(msg)
            i += 1
            continue

        calls = msg.get("tool_calls") if role == "assistant" else None
        if calls:
            ids = [str(c.get("id") or "") for c in calls]
            responses: Dict[str, Dict[str, Any]] = {}
            j = i + 1
            while j < total and str(messages[j].get("role") or "") == "tool":
                responses[str(messages[j].get("tool_call_id") or "")] = messages[j]
                j += 1
            if ids and all(cid in responses for cid in ids):
                out.append(msg)
                for cid in ids:
                    out.append(responses[cid])
                kept_calls.update(ids)
            i = j  # 缺响应：整组跳过（含它的 tool 行）
            continue

        out.append(msg)
        i += 1
    return out


def to_model_messages(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把落库的消息还原成 OpenAI 的消息数组。

    ``context`` 是当初喂给模型的 ``[数据]`` 段，映射回 ``user``；``assistant``
    带 ``tool_calls`` 的原样还原；``tool`` 找回它的 ``tool_call_id``。最后统一过一遍
    :func:`_sanitize_model_messages`，挡掉半截的工具调用组。
    """
    out: List[Dict[str, Any]] = []
    for row in rows:
        role = str(row.get("role") or "")
        content = str(row.get("content") or "")
        if role in ("user", "context"):
            out.append({"role": "user", "content": content})
        elif role == "assistant":
            msg: Dict[str, Any] = {"role": "assistant", "content": content}
            raw = row.get("tool_calls")
            if raw:
                try:
                    calls = json.loads(raw)
                except (TypeError, ValueError):
                    calls = None
                if isinstance(calls, list) and calls:
                    msg["tool_calls"] = calls
            out.append(msg)
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": str(row.get("tool_call_id") or ""),
                    "content": content,
                }
            )
    return _sanitize_model_messages(out)


async def _summarize_history(
    prev_summary: str,
    rows: List[Dict[str, Any]],
    *,
    cfg: Dict[str, Any],
    provider: Dict[str, Any],
) -> str:
    """把被裁掉的旧轮次压成要点摘要（一次额外的模型调用）。

    与「直接把旧消息丢掉」相比，多花一次便宜调用换来的是：几十轮之后模型仍然
    记得「用户一路在追查 nginx 502、已经确认是后端超时」。失败就返回空串，
    调用方退化为结构化裁剪 —— 压缩是优化，不该成为对话的失败点。
    """
    lines: List[str] = []
    for row in rows:
        role = str(row.get("role") or "")
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        if role == "tool":
            label = "[工具 " + str(row.get("tool_name") or "") + "]"
        elif role == "assistant":
            label = "[助手]"
        else:
            label = "[用户]"
        lines.append(label + " " + content)
    body = "\n".join(lines)[:SUMMARY_MAX_CHARS]
    if not body.strip():
        return ""

    system = i18n.pick(
        "你负责把一段运维排查对话压缩成要点摘要，供后续追问时回忆上下文。\n"
        "必须保留：用户关心的问题、已经确认的结论、做过的关键操作及其结果、尚未解决的问题。\n"
        "不要编造、不要展开细节，用简洁的中文分条列出。",
        "Compress an ops troubleshooting conversation into a short bullet summary so "
        "later turns can recall the context.\n"
        "Keep: what the user cares about, confirmed conclusions, key actions and their "
        "results, and open questions.\n"
        "Do not invent anything or go into detail; use concise English bullets.",
    )
    parts: List[str] = []
    if prev_summary.strip():
        parts.append(
            i18n.pick("已有摘要：\n", "Existing summary:\n") + prev_summary.strip()
        )
    parts.append(i18n.pick("新增对话：\n", "New turns:\n") + body)
    # 摘要用便宜一档的模型（同一把 key、同一个 base_url，只换型号）：
    # 这一步没有用户可见产出，没必要占旗舰模型的额度
    if ai.HELPER_MODEL:
        provider = {**provider, "model": ai.HELPER_MODEL}
    try:
        reply = await ai.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": "\n\n".join(parts)},
            ],
            cfg=cfg,
            provider=provider,
            json_mode=False,
            # 摘要是副产品，别让它把预算吃掉：给个够用的上限即可
            max_tokens=min(800, int(cfg.get("max_tokens") or 800)),
        )
    except ai.AIError:
        logger.warning("会话摘要失败，退化为结构化裁剪", exc_info=True)
        return ""
    return str(reply.get("content") or "").strip()[:20000]


#: 正在后台生成摘要的会话。同一会话的连续追问不该各起一个摘要任务 —— 那会把
#: 同一段旧对话重复压两遍，白花一次模型调用。
_summary_jobs: set = set()


def _schedule_context_summary(
    conversation_id: str,
    *,
    upto_seq: int,
    cfg: Dict[str, Any],
    provider: Dict[str, Any],
) -> None:
    """把「已经被裁掉、但还没摘要」的旧消息压成摘要 —— **放到后台**。

    原先这一步就压在本轮的关键路径上（首字之前）：聊得越久，每一句追问都要多等
    一次完整的模型调用。摘要只是「记忆优化」，晚一轮生效完全够用，所以这里改成
    起一个后台任务；本轮先用现有摘要 + 「部分较早对话已省略」的提示顶上。

    失败 / 没有事件循环都只是「这轮没有新摘要」，不影响对话本身。
    """
    if upto_seq <= 0 or conversation_id in _summary_jobs:
        return
    _summary_jobs.add(conversation_id)

    async def _run() -> None:
        try:
            conv = await get_conversation(conversation_id)
            if not conv:
                return
            covered = int(conv.get("summarized_upto") or 0)
            if upto_seq <= covered:
                return
            rows = await list_messages_between(conversation_id, covered, upto_seq)
            # 攒够一定量才值得花一次模型调用（与原来同一口径）
            if len(rows) < SUMMARY_MIN_MESSAGES:
                return
            merged = await _summarize_history(
                str(conv.get("ctx_summary") or ""), rows, cfg=cfg, provider=provider
            )
            if merged:
                await set_context_summary(conversation_id, merged, upto_seq)
        except Exception:  # noqa: BLE001 - 摘要失败只是少一段记忆
            logger.warning("后台会话摘要失败", exc_info=True)
        finally:
            _summary_jobs.discard(conversation_id)

    try:
        asyncio.create_task(_run())
    except RuntimeError:  # pragma: no cover - 没有事件循环（脚本 / 单测）
        _summary_jobs.discard(conversation_id)


def _report_to_text(report: Dict[str, Any]) -> str:
    """把 findings 报告压成一句给对话流看的话（完整结论在下方报告卡里）。"""
    summary = str(report.get("summary") or "").strip()
    if summary:
        return summary
    findings = report.get("findings") or []
    if findings:
        return i18n.pick(
            f"排查完成：共 {len(findings)} 条待处理事项，详见下方报告。",
            f"Inspection finished: {len(findings)} item(s) to address — see the report below.",
        )
    return i18n.pick(
        "排查完成，未发现需要立即处理的问题。",
        "Inspection finished — nothing urgent found.",
    )


# ------------------------------------------------------------------ 跑一轮
# 一轮的两种形态。这是这里最关键的一条分界：
#
# * ``chat``    —— **默认**。自由对话，不预采集证据、不强制 JSON。模型需要什么
#   自己调工具去查（平台那批「结论类」内部工具就是为它准备的）。首轮开销只有
#   一次模型调用，用户「不点排查直接问」也能得到直接回答。
# * ``inspect`` —— 体检模板。按固定流程采集证据 + 强制输出 findings 报告。
#   它是个**可选动作**（按钮 / 定时任务），而不是进入对话的必经之路。
MODE_CHAT = "chat"
MODE_INSPECT = "inspect"


def normalise_mode(value: Any) -> str:
    """把外部传入的 mode 归一成 ``chat`` / ``inspect``（未知值一律当自由对话）。"""
    return MODE_INSPECT if str(value or "").strip().lower() == MODE_INSPECT else MODE_CHAT


async def chat_turn(
    conversation_id: str,
    user_text: str = "",
    *,
    mode: str = MODE_CHAT,
    preset: str = "",
    emit: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    allowed: Optional[Iterable[str]] = None,
    provider_id: Optional[str] = None,
    username: str = "",
    is_admin: bool = False,
    allow_exec: bool = False,
    allow_write: bool = True,
    exec_access: str = "",
    perms: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """在一条会话里跑一轮（带并发闸门）。

    ``mode`` 决定这一轮是自由对话还是体检模板（见模块里的 MODE_* 说明）；
    ``preset`` 是预案 key（见 :mod:`app.aiplaybooks`），给了就用预案的指令代替
    用户输入。

    同一条会话同一时刻只跑一轮：两个标签页同时发消息会让 seq 交错、上下文错乱，
    模型额度也会翻倍。已经在跑时直接报错让用户稍候，而不是排队 —— 排队意味着他
    按下发送后界面上什么都没有，比一句「正在处理」更让人困惑。
    """
    lock = _conversation_lock(conversation_id)
    if lock.locked():
        raise ai.AIError(i18n.tr("这条会话正在处理上一条消息，请稍候再发"))
    async with lock:
        return await _run_turn(
            conversation_id,
            user_text,
            mode=mode,
            preset=preset,
            emit=emit,
            allowed=allowed,
            provider_id=provider_id,
            username=username,
            is_admin=is_admin,
            allow_exec=allow_exec,
            allow_write=allow_write,
            exec_access=exec_access,
            perms=perms,
        )


async def _run_turn(
    conversation_id: str,
    user_text: str = "",
    *,
    mode: str = MODE_CHAT,
    preset: str = "",
    emit: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    allowed: Optional[Iterable[str]] = None,
    provider_id: Optional[str] = None,
    username: str = "",
    is_admin: bool = False,
    allow_exec: bool = False,
    allow_write: bool = True,
    exec_access: str = "",
    perms: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """真正跑一轮：自由对话，或按体检模板产出报告。"""
    mode = normalise_mode(mode)
    playbook = aiplaybooks.get_playbook(preset)
    if playbook:
        # 预案可以指定形态（默认 chat）
        mode = normalise_mode(playbook.get("mode") or mode)
    # 两者要分开：**发给模型的**是完整指令，**落库/展示的**是短标题 ——
    # 聊天记录里出现几十行操作说明既难读，也会污染后续上下文重建。
    if playbook:
        display_text = str(playbook.get("title") or "").strip()
        model_text = str(playbook.get("prompt") or display_text).strip()
    else:
        display_text = str(user_text or "").strip()
        model_text = display_text

    # 带上 username：分析窗口与单次 token 上限可以是这个人自己设的值
    # （见 ai.USER_SETTING_KEYS），不是非得跟平台默认走
    cfg = await ai.load_config(username)
    if not cfg.get("enabled"):
        raise ai.AIError(i18n.tr("AI 助手未启用，请先在设置里配置并启用大模型"))

    conv = await get_conversation(conversation_id)
    if not conv or not owns(conv, username, is_admin):
        raise ai.AIError(i18n.tr("找不到这条会话"))

    # 记住这次的授权选择：刷新页面后开关回到用户上次的设置
    await set_allow_exec(
        conversation_id,
        bool(allow_exec),
        exec_access=exec_access or ("write" if allow_write else "read"),
    )

    provider = ai.pick_provider(
        cfg,
        provider_id or str(conv.get("provider") or ""),
        username=username,
        is_admin=is_admin,
    )
    if provider is None:
        raise ai.AIError(i18n.tr("还没有可用的模型，请先在设置里添加并启用一个"))

    # 目标有两种：受管主机（SSH）或**只有一台虚拟机**（Guest Agent）。
    # 后者是为「只选一次目标」服务的：用户选一台虚拟机就够，不必再挑一台
    # 它所在的主机；没有关联受管主机时，这一轮就只做虚拟机内排查。
    host_id = str(conv.get("host_id") or "")
    lease = aivm.current_lease()
    if host_id:
        host = await ai.resolve_host(host_id, allowed)
        local, row = await ai._resolve_target(host_id)
    else:
        if lease is None:
            raise ai.AIError(
                i18n.pick(
                    "这条会话没有目标主机，也没有生效的虚拟机授权。请先选择一台"
                    "虚拟机并签发授权。",
                    "This conversation has no target host and no active VM grant. "
                    "Pick a VM and issue a grant first.",
                )
            )
        # 展示用的「目标」取票面上的虚拟机；host_id 留空，主机类工具不会下发。
        host = {
            "id": "",
            "name": str(lease.get("vm_name") or f"VM {lease.get('vmid')}"),
            "host": "",
            "address": "",
            "local": False,
        }
        local, row = False, None
    hours = int(cfg.get("hours") or 24)
    started = time.time()
    notify = ai._make_notify(emit)
    approve = ai._make_approver(notify, owner=username)

    # 只取「证据行 + 最近若干条」：整表读进来再裁，读的量随会话长度线性增长，
    # 而真正会进 prompt 的只有这一小段（见 list_context_window）。
    window = await list_context_window(conversation_id)
    context_budget = int(cfg.get("context_tokens") or MAX_CONTEXT_TOKENS)

    if mode == MODE_INSPECT:
        if not host_id:
            # 体检 / 报告依赖主机的巡检数据；只有虚拟机目标时没有可采的证据。
            raise ai.AIError(
                i18n.pick(
                    "虚拟机目标没有主机巡检数据，无法生成体检报告。请直接对话排查，"
                    "或在上方切换到一个受管主机。",
                    "A VM-only target has no host inspection data, so a report cannot "
                    "be generated. Use the chat instead, or switch to a managed host.",
                )
            )
        # 体检模板：固定流程 —— 采集证据 → 强制 findings JSON → 报告落库。
        # 它可以是会话的第一轮，也可以中途再来一次（「重新体检」）。
        await notify({"type": "stage", "stage": "collect"})
        evidence = await ai.collect_evidence(
            host_id, hours=hours, username=username, is_admin=is_admin, perms=perms
        )
        context_content = ai.evidence_user_content(host, evidence)
        # context 行落库：它是「这台机器当时的证据快照」，回看与上下文重建都要它
        await append_message(conversation_id, role="context", content=context_content)
        # 只带对话内容、丢掉**旧的**证据块：重新体检要用最新那份证据，否则每体检
        # 一次就往上下文里再堆一份完整证据，token 白白翻倍。
        prior = [
            row
            for row in _fit_context_tokens(
                _context_rows(window, MAX_CONTEXT_MESSAGES), context_budget
            )
            if str(row.get("role") or "") != "context"
        ]
        model_messages: List[Dict[str, Any]] = [
            # 体检轮也要按授权切换「能做什么」那一段：重新体检时同样可能开着授权
            {
                "role": "system",
                "content": ai.report_system_prompt(
                    allow_exec=allow_exec, allow_write=allow_write
                ),
            },
        ]
        model_messages += to_model_messages(prior)
        model_messages.append({"role": "user", "content": context_content})
        if display_text:
            await append_message(conversation_id, role="user", content=display_text)
            model_messages.append({"role": "user", "content": model_text})
        finalize_json = True
        max_steps = ai.MAX_STEPS
    else:
        # 自由对话：**不预采集证据**。模型需要什么自己调工具去查 —— 平台那批
        # 「结论类」内部工具（体检报告 / 登录失败 / 告警 / 指标 / 变更 / 关联资源）
        # 正是为这条路径准备的，首轮开销只有一次模型调用。
        if not display_text:
            raise ai.AIError(i18n.tr("消息不能为空"))
        # 先重建历史、再落库这条新消息 —— 反过来会把用户这句话塞进上下文两次。
        # 历史超长时先按条数挑（保留证据），再按 token 预算收一遍：后者才是真正
        # 决定「每一步要 prefill 多少」的那道闸。
        trimmed = _fit_context_tokens(
            _context_rows(window, MAX_CONTEXT_MESSAGES), context_budget
        )
        summary = str(conv.get("ctx_summary") or "")
        covered = int(conv.get("summarized_upto") or 0)
        # 被裁掉的旧轮次交给**后台**压成摘要，下一轮生效（原先在首字之前同步等一次
        # 完整模型调用，聊得越久每句追问就越慢）。本轮先用已有摘要 + 省略提示顶上。
        kept_body = [
            row for row in trimmed if str(row.get("role") or "") != "context"
        ]
        oldest_kept = min(
            (int(row.get("seq") or 0) for row in kept_body), default=0
        )
        if oldest_kept > covered + 1:
            _schedule_context_summary(
                conversation_id,
                upto_seq=oldest_kept - 1,
                cfg=cfg,
                provider=provider,
            )

        # 长度分档：预案（playbook）自己跑了一整套排查，用户点它就是要一份交代
        # 清楚的报告，压到自由对话那档会把「已排除什么、还剩什么不确定」全砍掉；
        # 其余按最严的那档。两档的字数上限可以由用户自己调（见 ai.USER_SETTING_KEYS），
        # cfg 已经合并过他自己设的值，这里只管把档位和字数传下去。
        system_prompt = ai.chat_system_prompt(
            MAX_STEPS_CHAT,
            length=ai.reply_length_style(
                playbook=bool(playbook),
                terse_chars=int(
                    cfg.get("reply_chars_terse") or ai.DEFAULT_REPLY_CHARS_TERSE
                ),
                deep_chars=int(
                    cfg.get("reply_chars_deep") or ai.DEFAULT_REPLY_CHARS_DEEP
                ),
            ),
            # 「能做什么」那一段按授权切换（见 ai.scope_for）：不传就等于告诉模型
            # 「你只能读」，而工具表里明明摆着提议写命令的那把 —— 它会不敢用。
            allow_exec=allow_exec,
            allow_write=allow_write,
        )
        # 下面这些内容**随轮次变化**（租约、摘要、省略提示），一律不进 system：
        # system 在 prompt 最前面，动一个字，tools + system 这段前缀就不再命中
        # 上游的 prefix cache，而它正是每一步都要重发的最贵固定块。
        notes: List[str] = []
        if not host_id and lease is not None:
            # 「只有虚拟机」的会话：基础提示词讲的仍是排查一台主机，不纠正的话
            # 模型会假装自己登录了主机，甚至编出主机命令的输出。
            notes.append(
                i18n.pick(
                    "【本轮目标】没有主机 SSH 目标，你只能通过 QEMU Guest Agent 在虚拟机"
                    f"「{lease.get('vm_name') or lease.get('vmid')}」内部操作，并读取平台内部"
                    "数据（告警 / 指标 / 变更 / 关联资源）。不要声称登录了宿主机；需要主机"
                    "级命令或终端时，请让用户先在上方选择一个受管主机。",
                    "[Scope] There is no host SSH target. You may only act inside the VM "
                    f"“{lease.get('vm_name') or lease.get('vmid')}” through the QEMU Guest "
                    "Agent, plus read platform data (alerts / metrics / changes / related "
                    "resources). Never claim to have logged in to the host; if host-level "
                    "commands or a terminal are needed, ask the user to pick a managed host "
                    "above.",
                )
            )
        if summary.strip():
            notes.append(
                i18n.pick(
                    "（更早对话的摘要：\n" + summary.strip() + "\n）",
                    "(Summary of earlier turns:\n" + summary.strip() + "\n)",
                )
            )
        if oldest_kept > covered + 1:
            # 摘要还没生成（已排到后台）：如实说明有省略，别让模型把「没提过」
            # 当成「没发生过」
            notes.append(
                i18n.pick(
                    "（注意：部分较早的对话已被省略，只保留了摘要与最近的往来。）",
                    "(Note: some earlier turns were omitted; only the summary and the "
                    "most recent messages are included.)",
                )
            )

        model_messages = [{"role": "system", "content": system_prompt}]
        model_messages += to_model_messages(trimmed)
        if notes:
            model_messages.append({"role": "system", "content": "\n\n".join(notes)})
        model_messages.append({"role": "user", "content": model_text})
        await append_message(conversation_id, role="user", content=display_text)
        finalize_json = False
        max_steps = MAX_STEPS_CHAT

    def _row(msg: Dict[str, Any]) -> Dict[str, Any]:
        """协议消息 → 落库参数（逐条与批量两条路径共用同一份映射）。"""
        return {
            "role": str(msg.get("role") or "assistant"),
            "content": str(msg.get("content") or ""),
            "tool_calls": msg.get("tool_calls"),
            "tool_call_id": str(msg.get("tool_call_id") or ""),
            "tool_name": str(msg.get("tool_name") or ""),
            "command": str(msg.get("command") or ""),
            "ok": msg.get("ok") if msg.get("role") == "tool" else None,
        }

    async def _persist(msg: Dict[str, Any]) -> None:
        """把循环里的协议消息落库（用户中途终止也能留下已发生的部分）。"""
        await append_message(conversation_id, **_row(msg))

    async def _persist_many(msgs: List[Dict[str, Any]]) -> None:
        """一轮里的多条工具结果一次事务写完（见 :func:`append_messages`）。"""
        await append_messages(conversation_id, [_row(msg) for msg in msgs])

    # 按模式裁剪工具：体检轮的证据里已经带了告警、登录失败与最近的变更
    # （见 ai.collect_evidence），再下发只会诱使模型把同样的数据重查一遍 ——
    # 一次重复调用就是一整个模型往返。裁剪规则收在 aitools 里，两条链路共用一份。
    specs = aitools.openai_tools(
        include_exec=allow_exec, include_write=allow_write, mode=mode
    )
    if not host_id:
        # 没有主机目标就不下发主机工具（包括读主机体检报告的那一个）：
        # 下发了却没有执行目标，模型只会反复调用、反复拿到失败。
        blocked = set(aitools.HOST_TOOL_MAP) | {"get_baseline_report"}
        specs = [item for item in specs if item["function"]["name"] not in blocked]
    loop = await ai.run_tool_loop(
        model_messages,
        cfg=cfg,
        provider=provider,
        specs=specs,
        host_id=host_id,
        row=row,
        local=local,
        username=username,
        is_admin=is_admin,
        notify=notify,
        approve=approve,
        started=started,
        max_steps=max_steps,
        finalize_json_mode=finalize_json,
        # 只有追问轮（自然语言）才做打字机流式；体检轮要的是整段 JSON，
        # 流出去是一屏半截的括号 —— 但它仍以流式方式请求，只回进度不回正文。
        stream_tokens=not finalize_json,
        report_progress=finalize_json,
        perms=perms,
        on_message=_persist,
        on_messages=_persist_many,
    )

    content = str(loop.get("content") or "").strip()
    report: Optional[Dict[str, Any]] = None
    if finalize_json:
        try:
            report = ai.parse_reply(content)
            assistant_text = _report_to_text(report)
        except ai.AIError:
            # 模型没按 JSON 来也不该整轮失败：降级成一段普通回复
            assistant_text = content or i18n.pick("排查完成。", "Inspection finished.")
        if report is not None:
            # 解析失败时**保留上一份报告**而不是清空 —— 一次没吐 JSON，
            # 不该把用户手里那份能看的结论抹掉
            await set_report(conversation_id, report)
    else:
        assistant_text = content or i18n.pick(
            "抱歉，这一轮我没有生成有效回复，请再问一次。",
            "Sorry, I produced no answer this round — please ask again.",
        )

    message = await append_message(
        conversation_id, role="assistant", content=assistant_text
    )
    await notify(
        {"type": "assistant", "message_id": message["id"], "content": assistant_text}
    )
    if report is not None:
        await notify({"type": "report", "report": report})
    # 不在这里发 done：结束事件由路由层统一发（它还要补上 session_id），
    # 两边都发就会出现两个 done，前端会重复处理。

    return {
        "conversation_id": conversation_id,
        "mode": mode,
        "message": message,
        "report": report,
        "usage": loop.get("usage") or {},
        "steps": loop.get("steps") or 0,
        "tool_calls": loop.get("calls") or [],
        "model": (loop.get("reply") or {}).get("model") or provider.get("model"),
        "provider": provider.get("name") or provider.get("id"),
        "host_name": host.get("name") or host_id,
        "timing": {"total_ms": int((time.time() - started) * 1000)},
    }
