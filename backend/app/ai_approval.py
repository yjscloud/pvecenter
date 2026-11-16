"""AI 写命令的逐条审批：把「等用户点批准」做成一个可跨请求唤醒的等待点。

为什么单独一个模块
------------------
Agent 循环是**一条流式连接里的 await**，而用户的批准来自**另一个 HTTP 请求**；
两者靠一个进程内的 Future 桥接。放全局状态是必要的（两个请求要能碰到同一个
Future），但键是每次提议唯一的 ``approval_id``，彼此互不干扰。

等待有超时（见 :data:`APPROVAL_TIMEOUT`）：用户不在电脑前时不该让排查一直挂着 ——
超时按「拒绝」处理，模型会收到说明并换方案。超时长度必须小于
``ai.AGENT_TOTAL_TIMEOUT``，否则等待还没结束、循环就因总超时收口了，
用户点了批准也白点。

**双人审批**（``required=2``）是为高危动作准备的：一台生产机器的重启、一次
``/etc`` 覆盖，本该有两个人都同意。计票规则有三条不能含糊：

* **同一人不能投两票**（``voters`` 去重）—— 否则「双人」只是一个多余的点击；
* **一票否决立即生效** —— 拒绝不需要凑够人数，安全侧永远偏向拒绝；
* **批准要凑够 ``required`` 才放行** —— 少一票就是不放行，没有「先跑着看」。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# 单次审批的等待上限（秒）
APPROVAL_TIMEOUT = 120.0

_pending: Dict[str, "asyncio.Future[bool]"] = {}
#: 每个审批的**发起人**。审批接口据此拒绝「拿别人的 approval_id 替别人拍板」。
#: 只在等待期间有效（等待结束即清理），不留历史。
_owners: Dict[str, str] = {}
#: 已投「批准」票的人（去重）。双人审批靠它计票。
_votes: Dict[str, List[str]] = {}
#: 需要多少人批准（默认 1）。
_required: Dict[str, int] = {}


def prepare(
    approval_id: str, owner: str = "", required: int = 1
) -> "asyncio.Future[bool]":
    """先登记等待点，再去推送审批卡。

    顺序不能反：先 notify 再注册的话，用户在推送到达的瞬间就点「批准」，
    这个批准会撞上一个还不存在的 Future 而被丢弃 —— 表现为「点了没反应」。
    """
    loop = asyncio.get_running_loop()
    future: "asyncio.Future[bool]" = loop.create_future()
    _pending[approval_id] = future
    _votes[approval_id] = []
    _required[approval_id] = max(1, int(required or 1))
    if owner:
        _owners[approval_id] = owner
    return future


async def wait_for_approval(
    approval_id: str,
    timeout: float = APPROVAL_TIMEOUT,
    owner: str = "",
    required: int = 1,
) -> bool:
    """挂起等用户决定；超时 / 连接被取消一律返回 False（视作拒绝）。"""
    future = _pending.get(approval_id) or prepare(approval_id, owner, required)
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return False
    finally:
        _pending.pop(approval_id, None)
        _owners.pop(approval_id, None)
        _votes.pop(approval_id, None)
        _required.pop(approval_id, None)


def owner_of(approval_id: str) -> str:
    """这个审批是谁发起的（空串表示老接口发的、没有归属信息）。"""
    return _owners.get(approval_id, "")


def required_of(approval_id: str) -> int:
    """这个审批需要多少人批准（0 表示这个 id 已经不在等待队列里了）。"""
    return _required.get(approval_id, 0) if approval_id in _pending else 0


def voters_of(approval_id: str) -> List[str]:
    """已经批准过的人（用于展示「1/2 已批准」）。"""
    return list(_votes.get(approval_id, []))


def resolve(
    approval_id: str, approved: bool, by: str = ""
) -> Dict[str, Any]:
    """由审批接口调用：投一票 / 唤醒等待中的排查。

    返回 ``{hit, done, approved, count, required, voters}``：

    * ``hit``  —— 这个 id 是否还挂在等待队列里（false = 已失效或超时）；
    * ``done`` —— 等待是否已经收口（双人审批下，第一票不算）；
    * ``count``/``required`` —— 用于界面显示「1/2」。

    注意它**不一定**结束等待：双人审批下第一票只是记账。
    """
    future = _pending.get(approval_id)
    if future is None or future.done():
        return {
            "hit": False,
            "done": False,
            "approved": False,
            "count": 0,
            "required": 0,
            "voters": [],
        }

    needed = _required.get(approval_id, 1)
    voters = _votes.setdefault(approval_id, [])

    # 拒绝是单方面的：一票否决，不必凑人。安全侧永远偏向拒绝。
    if not approved:
        future.set_result(False)
        return {
            "hit": True,
            "done": True,
            "approved": False,
            "count": len(voters),
            "required": needed,
            "voters": list(voters),
        }

    # 同一人重复点击不能算两票，否则「双人」就是自欺欺人。
    if by and by in voters:
        return {
            "hit": True,
            "done": False,
            "approved": False,
            "count": len(voters),
            "required": needed,
            "voters": list(voters),
        }
    voters.append(by or f"anonymous-{len(voters) + 1}")

    if len(voters) >= needed:
        future.set_result(True)
        return {
            "hit": True,
            "done": True,
            "approved": True,
            "count": len(voters),
            "required": needed,
            "voters": list(voters),
        }

    return {
        "hit": True,
        "done": False,
        "approved": False,
        "count": len(voters),
        "required": needed,
        "voters": list(voters),
    }


def pending_count() -> int:
    """当前有几个在等批准的请求（诊断 / 测试用）。"""
    return len(_pending)
