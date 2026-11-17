"""一轮 AI 请求的分段耗时埋点。

为什么单独放一个模块：这些数字分布在**三条链路**上 —— 路由层（排队、首字），
Agent 循环（模型调用 / 工具执行），会话层（消息落库）。再让每条链路自己往返回体
里塞字段，等于把审计结构和业务逻辑绑在一起；用 contextvar 则可以让任何一层
「就近记一笔」，最后由路由层统一收口、落库、写一条结构化日志。

contextvar 随 asyncio task 传播：worker 是独立 task，在 worker 内部 ``start()``，
里面 await 的调用（含 run_tool_loop）拿到的就是同一份字典。并发的其它会话互不干扰。

**这不是分布式追踪**，只是定位本机瓶颈用的秒表：进程重启即丢，跨 worker 要按
``session_id`` 聚合日志。希望它常驻的话，把 ``snapshot()`` 的结果接进
OpenTelemetry / Prometheus 即可（字段已经是可直接打点的数值）。
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

# 各字段单位统一为毫秒（token 数、步数除外），缺省为 0，方便直接求和 / 打点
_ZERO: Dict[str, Any] = {
    "queue_ms": 0.0,       # 在闸门里排队等了多久
    "ttfb_ms": 0.0,        # 开始执行 → 首个 token 事件（用户感受到的「首字」）
    "llm_ms": 0.0,         # 模型调用总耗时（含流式收尾）
    "tool_ms": 0.0,        # 工具执行总耗时
    "db_ms": 0.0,          # 消息 / 报告落库耗时
    "llm_calls": 0,        # 模型调用次数（含摘要等辅助调用）
    "stream_fallback": 0,  # 上游不支持流式、退回非流式的次数
    "steps": 0,            # 工具循环走了几步（= 几次「模型 → 工具」往返）
    "tokens_in": 0,        # 本轮输入 token（上游回传，缺省估算）
    "tokens_out": 0,       # 本轮输出 token
}

_current: ContextVar[Optional[Dict[str, Any]]] = ContextVar("aiperf", default=None)


def start() -> Dict[str, Any]:
    """开始记录一轮；返回的对象可直接改动，但请用 :func:`add` / :func:`set_once`。"""
    metrics: Dict[str, Any] = dict(_ZERO)
    _current.set(metrics)
    return metrics


def current() -> Optional[Dict[str, Any]]:
    return _current.get()


def add(name: str, ms: float) -> None:
    metrics = _current.get()
    if metrics is not None:
        metrics[name] = round(float(metrics.get(name, 0)) + ms, 1)


def bump(name: str, n: int = 1) -> None:
    metrics = _current.get()
    if metrics is not None:
        metrics[name] = int(metrics.get(name, 0)) + n


def set_once(name: str, ms: float) -> None:
    """只记第一次（首字、排队完成这类「一次性」时间点）。"""
    metrics = _current.get()
    if metrics is not None and not metrics.get(name):
        metrics[name] = round(float(ms), 1)


def set_value(name: str, value: Any) -> None:
    """盖一个非耗时字段（步数、token 数这类计数，不该被四舍五入成毫秒）。"""
    metrics = _current.get()
    if metrics is not None:
        metrics[name] = value


@contextmanager
def timing(name: str) -> Iterator[None]:
    """``with aiperf.timing('llm_ms'):`` —— 异常时也会记下已经耗掉的时间。"""
    started = time.perf_counter()
    try:
        yield
    finally:
        add(name, (time.perf_counter() - started) * 1000)


def snapshot() -> Dict[str, Any]:
    metrics = _current.get()
    return dict(metrics) if metrics is not None else dict(_ZERO)


def log_session(session_id: str, *, status: str, total_ms: float) -> Dict[str, Any]:
    """收口：返回快照并写一条可被日志系统检索的 JSON（前缀 ``ai_perf``）。"""
    metrics = snapshot()
    metrics["total_ms"] = round(total_ms, 1)
    payload = {"session_id": session_id, "status": status, **metrics}
    # 用 json.dumps 而不是 f-string：字段里没有用户内容，但结构化日志要能被
    # 采集系统直接解析（Loki / ELK 里按 llm_ms 排序找慢请求）
    logger.info("ai_perf %s", json.dumps(payload, ensure_ascii=False))
    return metrics
