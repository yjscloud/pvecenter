"""局域网 PVE 发现：新装面板「一键接入」的第一步。

为什么是扫网段，而不是 mDNS / 广播：
  * PVE 默认不广播可用的服务记录。avahi 里那条 ``_pve._tcp`` 要节点上装了
    avahi 才有，而且不少机房为了减少噪声把它关掉了；
  * 面板就装在同一个二层网络里，直接探 8006 端口是唯一**不需要在节点上预装
    任何东西**的发现方式。

探测只做两件无副作用的事：
  1. TCP 连一下 8006（快速排除绝大多数地址）；
  2. ``GET /api2/json/version``（PVE 这个接口免认证），拿到版本号才算命中。
     不发送任何凭据，因此这一步泄不泄露 token；TLS 也不校验 —— 只是认版本，
     真正的身份确认发生在下一步用账号密码登录。

只允许内网网段（RFC1918 / 链路本地），避免把这个接口变成「扫描器」。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import struct
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

#: 一次扫描最多探多少个地址。一个 /16 是 65534 个，扫完既慢又像攻击；
#: 超过上限只取前 N 个并回 ``truncated``，前端据此提示「请缩小网段」。
MAX_HOSTS = 1024

#: 并发度。太高会把交换机的 ARP 表打满（尤其家用路由），48 是稳的。
CONCURRENCY = 48

#: 单地址 TCP 超时。同网段 SYN 通常 <5ms，1.2s 已经足够宽松。
CONNECT_TIMEOUT = 1.2

#: 版本接口超时（TLS 握手 + 一次 GET）。PVE 在慢盘 / 反向代理后面偶尔要 3s 才
#: 应声，卡太死会把「能连上的机器」误判成不存在 —— 发现阶段漏报比慢更糟。
VERSION_TIMEOUT = 6.0

_SIOCGIFADDR = 0x8915
_SIOCGIFNETMASK = 0x891B


def _iface_ipv4(name: str) -> Tuple[Optional[str], Optional[str]]:
    """取网卡的 IPv4 地址与掩码（Linux ioctl，不依赖第三方库）。"""
    import fcntl

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        req = struct.pack("256s", name[:15].encode("utf-8"))
        addr = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), _SIOCGIFADDR, req)[20:24])
        mask = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), _SIOCGIFNETMASK, req)[20:24])
        return addr, mask
    except OSError:
        return None, None
    finally:
        sock.close()


def _primary_ipv4() -> Optional[str]:
    """默认路由出口地址。

    UDP ``connect`` 不发包，只是让内核选一条路由 —— 拿到的就是「本机在这个
    网络里的地址」，比解析 ``ip route`` 稳定。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 53))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def local_private_cidrs() -> List[str]:
    """本机的内网 IPv4 网段（去重、排序）。

    大网段（掩码比 /24 宽）只取本机所在的 /24：一个 /16 里通常只有一两个
    真实网段，全扫既慢又会把无关设备探一遍。
    """
    cidrs: List[str] = []
    try:
        interfaces = socket.if_nameindex()
    except OSError:
        interfaces = []

    for _, name in interfaces:
        addr, mask = _iface_ipv4(name)
        if not addr or not mask or addr.startswith("127."):
            continue
        try:
            net = ipaddress.IPv4Network(f"{addr}/{mask}", strict=False)
        except ValueError:
            continue
        if not (net.network_address.is_private or net.network_address.is_link_local):
            continue
        if net.prefixlen < 24:
            net = ipaddress.IPv4Network(f"{addr}/24", strict=False)
        cidrs.append(str(net))

    if not cidrs:
        # 拿不到网卡信息（容器 / 受限环境）时的兜底：按主地址猜一个 /24
        primary = _primary_ipv4()
        if primary and ipaddress.IPv4Address(primary).is_private:
            cidrs.append(str(ipaddress.IPv4Network(f"{primary}/24", strict=False)))

    return sorted(set(cidrs))


def parse_cidrs(raw: str) -> List[str]:
    """解析用户填写的网段（逗号 / 空格分隔），只接受内网 IPv4。

    允许写单个 IP：``192.168.1.10`` 会被当成 /32，用于「我就知道是这台」。
    """
    items: List[str] = []
    for chunk in (raw or "").replace(",", " ").split():
        text = chunk.strip()
        if not text:
            continue
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise ValueError(f"网段格式不正确：{text}") from exc
        if net.version != 4:
            raise ValueError(f"只支持 IPv4：{text}")
        if not (net.network_address.is_private or net.network_address.is_link_local):
            # 面板装在公网上也不该拿它当扫描器用
            raise ValueError(f"只允许扫描内网地址段：{text}")
        items.append(str(net))
    return items


def _hosts(cidrs: Iterable[str], limit: int) -> Tuple[List[str], bool]:
    """把网段展开成地址列表；数量超限就截断并标记。"""
    out: List[str] = []
    for cidr in cidrs:
        net = ipaddress.ip_network(cidr, strict=False)
        # /32 的 hosts() 就是那一个地址；/24 去掉网络号与广播，正好是 254 个
        for ip in net.hosts():
            if len(out) >= limit:
                return out, True
            out.append(str(ip))
    return out, False


async def _version(host: str, port: int) -> Optional[Dict[str, Any]]:
    """读 /version 确认对面确实是 PVE。"""
    url = f"https://{host}:{port}/api2/json/version"
    try:
        async with httpx.AsyncClient(
            # 只读版本信息，不带凭据：PVE 默认自签，这里校验没有意义
            verify=False,
            timeout=httpx.Timeout(VERSION_TIMEOUT, connect=VERSION_TIMEOUT),
            follow_redirects=False,
            trust_env=False,  # 内网地址不要被 HTTP_PROXY 带走
        ) as client:
            resp = await client.get(url)
            server = str(resp.headers.get("server") or "").lower()
            data: Any = {}
            if resp.status_code == 200:
                try:
                    data = (resp.json() or {}).get("data") or {}
                except ValueError:
                    data = {}
            version = str((data or {}).get("version") or "")
            # 200 + 版本号是硬证据；401 但 Server 头写着 pve-api-daemon 也算
            #（少数加固过的 PVE 会要求认证）
            if not version and "pve" not in server:
                return None
            return {
                "version": version,
                "release": str((data or {}).get("release") or ""),
                "repoid": str((data or {}).get("repoid") or ""),
            }
    except (httpx.HTTPError, OSError, ValueError):
        return None


async def _probe(host: str, port: int, sem: asyncio.Semaphore) -> Optional[Dict[str, Any]]:
    async with sem:
        started = time.monotonic()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), CONNECT_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError):
            return None
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            pass

        latency = int((time.monotonic() - started) * 1000)
        info = await _version(host, port)
        if info is None:
            # 端口开着但读不到版本：可能是不吃 ``/version`` 的加固版 PVE，也可能
            # 是别的服务占着 8006。仍然列出来（``verified=False``），让用户自己
            # 判断 —— 直接丢掉等于「明明能连却扫不到」，更难解释。
            return {
                "host": host,
                "port": port,
                "latency_ms": latency,
                "version": "",
                "release": "",
                "repoid": "",
                "verified": False,
            }
        return {"host": host, "port": port, "latency_ms": latency, "verified": True, **info}


async def scan(
    cidrs: List[str],
    port: int = 8006,
    *,
    limit: int = MAX_HOSTS,
    concurrency: int = CONCURRENCY,
) -> Dict[str, Any]:
    """扫描给定网段，返回命中的 PVE 主机。"""
    started = time.monotonic()
    hosts, truncated = _hosts(cidrs, limit)
    sem = asyncio.Semaphore(max(1, concurrency))
    results = await asyncio.gather(*(_probe(h, port, sem) for h in hosts))
    found = [item for item in results if item]
    found.sort(key=lambda item: ipaddress.IPv4Address(item["host"]))
    return {
        "cidrs": cidrs,
        "port": port,
        "scanned": len(hosts),
        "found": found,
        "truncated": truncated,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
