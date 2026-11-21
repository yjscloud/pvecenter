"""出站「测试连通性」目标校验（SSRF 兜底）。

背景：``POST /api/config/connection/test`` 与 ``POST /api/pbs/config/test`` 会按
请求体里的 ``host`` / ``port`` 让**面板后端**发起出站请求，并把失败原因回给前端。
面板通常与 PVE / PBS / 跳板机处在同一内网段，于是这两个接口天生等价于
「以面板为跳板的内网探测与读取原语」：任意 host + 任意 port + 差异化回显 =
一台端口扫描器。

本模块只做两件事：

1. **收窄目标**（:func:`assert_target_allowed`）。目标必须命中「已保存连接」
   的 ``host:port``，或命中运维显式配置的 ``CONNECTION_TEST_ALLOW_TARGETS``
   白名单；两者都没有就拒绝。前端因此只能在已知目标里挑，不能决定目标。
   主机名要先解析，再对**每一个**解析结果做地址检查：链路本地（含云元数据
   169.254.169.254）、组播、保留、未指定地址一律拒绝；RFC1918 私网与回环只在
   「已登记 / 已白名单」时允许 —— 面板连内网 PVE、甚至与 PVE 同机部署都是
   正常业务，不能一刀切。
2. **归一化失败原因**（:func:`classify_failure`）。上游异常被翻译成固定的枚举
   与固定文案，路由层直接回给前端。**绝不拼接对端响应正文与内部路径** ——
   那正是「差异化回显」的另一半。

``follow_redirects`` 由 PVE / PBS 客户端统一关闭，3xx 不会被跟随到私网；
本模块不替代那一层，只负责「请求发出之前」的准入。
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import ssl
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit

import httpx

from .config import settings

logger = logging.getLogger(__name__)

#: 归一化后的 (host, port)；host 已去 scheme / 方括号 / 末尾点并转小写
Target = Tuple[str, int]

#: 目标未登记、也不在白名单里
TARGET_NOT_ALLOWED = "target_not_allowed"
#: 已登记 / 已白名单，但解析出的地址仍属于禁止探测的保留网段
ADDRESS_BLOCKED = "address_blocked"
TARGET_INVALID = "target_invalid"

# RFC1918 + CGNAT + IPv6 ULA。**不要**用 ``ipaddress.is_private``：它把文档
# 网段、基准测试网段、保留段也算成 private，会把合法目标一起误伤。
_PRIVATE_NETS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "fc00::/7",
    )
)
_LOOPBACK_NETS = tuple(
    ipaddress.ip_network(net) for net in ("127.0.0.0/8", "::1/128")
)
_LINK_LOCAL_NETS = tuple(
    ipaddress.ip_network(net) for net in ("169.254.0.0/16", "fe80::/10")
)

#: DNS 解析超时：校验本身不能被一个卡住的解析拖死
_RESOLVE_TIMEOUT = 3.0

_NOT_ALLOWED_HINT = (
    "目标地址不在允许范围内（target_not_allowed）："
    "只能测试「已保存连接」中登记的 host:port。"
    "新增连接请先保存再测试；确实需要在保存前测试时，"
    "由管理员把地址加入 CONNECTION_TEST_ALLOW_TARGETS（逗号分隔的 IP / CIDR / 主机名）"
)


class TargetBlocked(Exception):
    """目标未通过出站准入。``code`` 供路由层转成稳定的机器可读错误码。"""

    def __init__(self, message: str, code: str = TARGET_NOT_ALLOWED) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


def _in_any(addr: Any, nets: Sequence[Any]) -> bool:
    """``addr`` 是否落在任一网段里（版本不同直接视为不匹配）。"""
    return any(addr.version == net.version and addr in net for net in nets)


def parse_ip(text: str) -> Optional[Any]:
    """把字符串解析成 IP；不是 IP 返回 ``None``（不抛异常）。"""
    try:
        return ipaddress.ip_address(str(text or "").strip().strip("[]"))
    except ValueError:
        return None


def split_host_port(raw: str, default_port: int = 0) -> Target:
    """把用户填的目标归一化成 ``(host, port)``。

    与 :class:`app.pve.PveConnection` / :class:`app.pbs.PbsConnection` 的
    ``base_url`` 保持同一套解析：允许裸主机名，也允许测试与隧道场景下的
    ``http://127.0.0.1:8006`` 写法 —— 冒号后的端口一旦写死就以它为准。
    """
    text = str(raw or "").strip()
    if not text:
        return "", int(default_port or 0)

    host = ""
    port = int(default_port or 0)
    try:
        parsed = urlsplit(text if "://" in text else f"//{text}")
        host = parsed.hostname or ""
        if parsed.port:
            port = int(parsed.port)
    except ValueError:
        # 端口不是数字 / IPv6 写法怪异：退回原文，至少能继续做地址检查
        host = text

    host = host.strip().strip("[]").rstrip(".").lower()
    return host, port


def host_key(host: str) -> str:
    """比较用的主机键：IP 走规范写法，主机名小写去尾点。"""
    ip = parse_ip(host)
    if ip is not None:
        return str(ip)
    return str(host or "").strip().strip("[]").rstrip(".").lower()


def registered_targets(
    connections: Iterable[Any], default_port: int
) -> Set[Target]:
    """从连接表生成「可信目标」集合（host:port）。

    连接表里的 host 可能带 scheme / 内嵌端口（测试与隧道场景），这里统一按
    实际出站地址归一化；端口缺失时用该模块的默认端口兜底。
    """
    out: Set[Target] = set()
    for item in connections or ():
        if not isinstance(item, dict):
            continue
        try:
            fallback = int(item.get("port") or default_port)
        except (TypeError, ValueError):
            fallback = int(default_port)
        host, port = split_host_port(str(item.get("host") or ""), fallback)
        if not host:
            continue
        out.add((host_key(host), port))
    return out


def same_target(
    raw_host: str, port: int, profile: Dict[str, Any], default_port: int
) -> bool:
    """请求体的目标是否**就是**这条已保存连接。

    用于「留空 secret 时复用已存密钥」这一路径：不校验的话，调用方可以要求
    面板把保存的 Token 发到任意主机上 —— 那已经不是 SSRF，而是凭据外带。
    """
    if not isinstance(profile, dict):
        return False
    try:
        fallback = int(profile.get("port") or default_port)
    except (TypeError, ValueError):
        fallback = int(default_port)
    saved_host, saved_port = split_host_port(str(profile.get("host") or ""), fallback)
    want_host, want_port = split_host_port(raw_host, port)
    return bool(saved_host) and saved_host == want_host and saved_port == want_port


def parse_allow_targets(raw: str) -> Tuple[Set[str], List[Any]]:
    """解析 ``CONNECTION_TEST_ALLOW_TARGETS``：返回（精确主机集合，网段列表）。

    支持逗号 / 分号 / 空白分隔；条目可以是 IP、CIDR 或主机名。主机名按大小写
    不敏感比较，IP 用规范写法（``::1`` 与 ``0:0:...:1`` 视为同一个）。
    """
    hosts: Set[str] = set()
    nets: List[Any] = []
    for token in re.split(r"[,;\s]+", str(raw or "")):
        token = token.strip().rstrip(".")
        if not token:
            continue
        if "/" in token:
            try:
                nets.append(ipaddress.ip_network(token, strict=False))
            except ValueError:
                logger.warning("CONNECTION_TEST_ALLOW_TARGETS 里的网段无效：%s", token)
            continue
        hosts.add(host_key(token))
    return hosts, nets


def _classify(addr: Any) -> str:
    """地址分类。顺序重要：元数据地址优先于「私网」被识别为链路本地。"""
    if addr.is_unspecified:
        return "unspecified"
    if addr.is_multicast:
        return "multicast"
    if _in_any(addr, _LOOPBACK_NETS):
        return "loopback"
    if _in_any(addr, _LINK_LOCAL_NETS):
        return "link_local"
    if _in_any(addr, _PRIVATE_NETS):
        return "private"
    if addr.is_reserved:
        return "reserved"
    return "public"


async def _resolve(host: str, port: int) -> List[Any]:
    """解析主机名。失败 / 超时按「解析不到」处理，由调用方决定是否放行。"""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=_RESOLVE_TIMEOUT,
        )
    except (socket.gaierror, OSError, asyncio.TimeoutError, UnicodeError, ValueError):
        return []

    found: List[Any] = []
    for info in infos:
        sockaddr = info[4] if len(info) > 4 else ()
        if not sockaddr:
            continue
        addr = parse_ip(str(sockaddr[0]).split("%")[0])
        if addr is None or addr in found:
            continue
        found.append(addr)
    return found


async def assert_target_allowed(
    host: str,
    port: int,
    *,
    registered: Iterable[Target] = (),
    allow_targets: Optional[str] = None,
) -> Target:
    """校验一次「测试连通性」的目标；不允许时抛 :class:`TargetBlocked`。

    ``registered`` 用 :func:`registered_targets` 从对应连接表生成；
    ``allow_targets`` 留空时读 ``CONNECTION_TEST_ALLOW_TARGETS``。
    """
    raw_text = str(host or "").strip()
    target_host, target_port = split_host_port(raw_text, port)
    if not target_host:
        raise TargetBlocked("目标地址不能为空", TARGET_INVALID)
    if not 1 <= int(target_port) <= 65535:
        raise TargetBlocked("目标端口必须在 1 到 65535 之间", TARGET_INVALID)
    if any(ch.isspace() for ch in target_host) or any(
        ch in target_host for ch in ("/", "\\", "?", "#", "@")
    ):
        raise TargetBlocked("目标地址格式不正确", TARGET_INVALID)

    # 带 scheme 时只接受 ``scheme://host[:port]``。路径 / 查询 / userinfo 都会
    # 让「归一化后的主机」与客户端实际连接的地址不是同一个（例如
    # ``172.16.149.3/x:8006`` 会绕到 443），必须挡在准入层。
    if raw_text.startswith(("http://", "https://")):
        parsed = urlsplit(raw_text)
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise TargetBlocked("目标地址不能带路径 / 查询串 / 用户信息", TARGET_INVALID)
    elif any(ch in raw_text for ch in ("/", "\\", "?", "#", "@")):
        raise TargetBlocked("目标地址格式不正确", TARGET_INVALID)

    raw_allow = (
        settings.connection_test_allow_targets
        if allow_targets is None
        else allow_targets
    )
    allow_hosts, allow_nets = parse_allow_targets(raw_allow)
    registry = set(registered or ())

    key = host_key(target_host)
    by_registry = (key, int(target_port)) in registry

    literal = parse_ip(target_host)
    if literal is not None:
        addresses = [literal]
    else:
        addresses = await _resolve(target_host, int(target_port))

    by_allow_host = key in allow_hosts
    by_allow_net = any(
        ip.version == net.version and ip in net
        for ip in addresses
        for net in allow_nets
    )

    if not (by_registry or by_allow_host or by_allow_net):
        raise TargetBlocked(_NOT_ALLOWED_HINT, TARGET_NOT_ALLOWED)

    # 地址级兜底：链路本地（含云元数据）、组播、保留、未指定地址**始终**拒绝，
    # 即使它出现在白名单里 —— 这些地址没有一个是 PVE / PBS 的正常监听地址。
    for addr in addresses:
        kind = _classify(addr)
        if kind in ("link_local", "multicast", "unspecified", "reserved"):
            raise TargetBlocked(
                f"目标地址属于禁止探测的保留网段（address_blocked）：{addr}",
                ADDRESS_BLOCKED,
            )

    return target_host, int(target_port)


# ------------------------------------------------------------------ 失败归一化
_FAILURE_TEXT = {
    "auth_failed": (
        "目标拒绝了该凭据（auth_failed）：请核对 Token ID 与 Secret；"
        "若确认无误，检查令牌是否已被删除 / 重置，或是否勾选了「特权分离」"
        "（特权分离的令牌默认零权限）"
    ),
    "forbidden": "凭据权限不足（forbidden）：请为该令牌补齐所需的 ACL 权限",
    "timeout": "连接超时（timeout）：目标在限定时间内没有响应",
    "not_tls": "目标端口不是 HTTPS/TLS 服务（not_tls）：请确认端口填写正确",
    "unreachable": "无法连接目标地址（unreachable）：请检查地址、端口与网络可达性",
    "proxy": "出站请求被代理拦截（proxy）：请检查面板的 HTTP(S)_PROXY 配置",
    "http_error": (
        "目标返回了非预期状态（http_error）：请确认该地址确实是 PVE / PBS 的 API"
    ),
    "protocol_error": (
        "目标响应不是可识别的 API 格式（protocol_error）：请确认地址与端口"
    ),
}

#: 只匹配明确的 TLS 错误特征。不要用裸 "ssl" / "tls"：主机名里带这几个字母
#: （例如 pvetls.example.com）会被误判成「不是 TLS 服务」。
_TLS_MARKS = (
    "wrong version number",
    "certificate verify failed",
    "unknown protocol",
    "tls handshake",
    "handshake failure",
    "sslv3",
    "ssl:",
    "_ssl",
    "record layer failure",
    "eof occurred in violation of protocol",
)


def _cause_chain(exc: BaseException) -> List[BaseException]:
    seen: Set[int] = set()
    chain: List[BaseException] = []
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen and len(chain) < 8:
        seen.add(id(cur))
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    return chain


def _mentions_tls(exc: BaseException) -> bool:
    for item in _cause_chain(exc):
        if isinstance(item, ssl.SSLError):
            return True
        text = str(item).lower()
        if any(mark in text for mark in _TLS_MARKS):
            return True
    return False


def classify_failure(exc: BaseException) -> Tuple[str, str]:
    """把上游异常归一化成 ``(code, 面向用户的固定文案)``。

    ``code`` 取 ``auth_failed / forbidden / timeout / not_tls / unreachable /
    proxy / http_error / protocol_error``。文案里**不含**对端响应正文、请求
    路径或面板内部地址。
    """
    status = getattr(exc, "pve_status", None)
    if status == 401:
        return "auth_failed", _FAILURE_TEXT["auth_failed"]
    if status == 403:
        return "forbidden", _FAILURE_TEXT["forbidden"]

    chain = _cause_chain(exc)
    if any(
        isinstance(item, (httpx.TimeoutException, asyncio.TimeoutError))
        for item in chain
    ):
        return "timeout", _FAILURE_TEXT["timeout"]
    if "timed out" in " ".join(str(item) for item in chain).lower():
        return "timeout", _FAILURE_TEXT["timeout"]
    if any(isinstance(item, httpx.ProxyError) for item in chain):
        return "proxy", _FAILURE_TEXT["proxy"]
    if _mentions_tls(exc):
        return "not_tls", _FAILURE_TEXT["not_tls"]
    if any(isinstance(item, httpx.TransportError) for item in chain):
        return "unreachable", _FAILURE_TEXT["unreachable"]
    # 502 但没有底层传输异常：连上了，对端吐的不是可识别的 API（例如把
    # 8006 填成了某个 Web 站点）—— 这属于协议层问题，不是不可达。
    if status == 502:
        return "protocol_error", _FAILURE_TEXT["protocol_error"]
    if isinstance(status, int) and status >= 400:
        return "http_error", _FAILURE_TEXT["http_error"]
    return "protocol_error", _FAILURE_TEXT["protocol_error"]


def failure_message(code: str) -> str:
    """取固定文案；未知 code 回落到通用文案。"""
    return _FAILURE_TEXT.get(code, _FAILURE_TEXT["protocol_error"])
