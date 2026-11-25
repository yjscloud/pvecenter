"""站点可用性监控：HTTP 探测 + 证书链到期检查。

与「证书到期」是两件事：证书可能有效但站点已经 502，也可能站点正常但证书
三天后过期。这里每次探测都同时记录「HTTP 是否可用」和「对端证书还剩几天」，
状态翻转时才发通知（每 5 分钟一条没人受得了）。

刻意不复用 :mod:`alerting`：那套是「指标越界 + 规则匹配」，为一个 URL 的
200/500 写规则太重；这里只需要「上次是什么、这次是什么」。
"""
from __future__ import annotations

import asyncio
import socket
import ssl
import time
from typing import Any, Dict, List, Optional

import httpx

from . import certnotify, certs, i18n, ssrfguard

#: 单次探测超时。站点慢不该拖住 5 分钟一轮的作业。
TIMEOUT = 10.0
#: 单轮最多检查多少个站点，避免一条坏链路占满窗口
BATCH = 20


#: 视为「弱协议」的版本：TLS 1.2 以下都该淘汰（PCI DSS 早已不接受）。
WEAK_PROTOCOLS = ("SSLv2", "SSLv3", "TLSv1", "TLSv1.1")
#: 剩余天数低于该值时健康度标黄（与告警分档解耦：这里只是体检结论）。
HEALTH_WARN_DAYS = 14


def _hostname_matches(pattern: str, host: str) -> bool:
    """证书里的名字能否覆盖 ``host``（支持单层 ``*.`` 通配）。"""
    name = str(pattern or "").strip().lower().rstrip(".")
    target = str(host or "").strip().lower().rstrip(".")
    if not name or not target:
        return False
    if name == target:
        return True
    if name.startswith("*."):
        suffix = name[1:]  # ".example.com"
        if not target.endswith(suffix):
            return False
        head = target[: -len(suffix)]
        # 通配只覆盖一层：a.example.com 可以，a.b.example.com 不行
        return bool(head) and "." not in head
    return False


def inspect_tls(host: str, port: int = 443) -> Dict[str, Any]:
    """对端 TLS 体检：剩余天数、协议版本、主机名匹配、链是否被信任、密钥强度。

    与 :func:`_cert_days_left` 的区别：那个只回答「还有几天」，本函数回答
    「这条链到底能不能用」。做法是两次握手：

    1. **宽松连接**（不校验）读叶证书，拿 SAN / 有效期 / 密钥参数；
    2. **严格连接**（系统信任库 + 校验主机名）判断链是否完整、名字是否匹配。

    严格握手失败不等于站点坏了（自签、私有 CA 很常见），所以只作为
    ``trusted`` 字段与体检结论，不参与 up/down 判定。
    """
    result: Dict[str, Any] = {
        "ok": False,
        "days_left": None,
        "protocol": "",
        "issuer": "",
        "subject": "",
        "serial": "",
        "fingerprint": "",
        "san": [],
        "key_type": "",
        "key_bits": 0,
        "signature_hash": "",
        "hostname_ok": False,
        "trusted": False,
        "issues": [],
        "detail": "",
    }
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
                result["protocol"] = str(tls.version() or "")
        if not der:
            result["detail"] = "no certificate"
            return result

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, ed25519, ed448, rsa
        from cryptography.x509.oid import NameOID

        cert = x509.load_der_x509_certificate(der)
        expire = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
        result["days_left"] = int((expire.timestamp() - time.time()) // 86400)
        result["issuer"] = cert.issuer.rfc4514_string()
        result["subject"] = cert.subject.rfc4514_string()
        result["serial"] = format(cert.serial_number, "x")
        result["fingerprint"] = cert.fingerprint(hashes.SHA256()).hex()
        result["signature_hash"] = str(cert.signature_hash_algorithm.name or "")
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            result["san"] = list(san.value.get_values_for_type(x509.DNSName))
        except x509.ExtensionNotFound:
            result["san"] = []

        public_key = cert.public_key()
        if isinstance(public_key, rsa.RSAPublicKey):
            result["key_type"] = "RSA"
            result["key_bits"] = int(public_key.key_size)
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            result["key_type"] = "EC"
            result["key_bits"] = int(public_key.curve.key_size)
        elif isinstance(public_key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
            result["key_type"] = "Ed25519" if isinstance(public_key, ed25519.Ed25519PublicKey) else "Ed448"

        names = result["san"] or [
            str(attr.value)
            for attr in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        ]
        result["hostname_ok"] = any(_hostname_matches(name, host) for name in names)

        # 严格握手：链完整 + 系统信任 + 主机名匹配
        try:
            strict = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=5) as raw:
                with strict.wrap_socket(raw, server_hostname=host) as tls:
                    tls.getpeercert()
            result["trusted"] = True
        except ssl.SSLCertVerificationError as exc:
            result["trusted"] = False
            message = str(getattr(exc, "verify_message", "") or exc)
            if "hostname" in message.lower() or "match" in message.lower():
                result["hostname_ok"] = False
        except Exception:  # noqa: BLE001 - 严格握手失败只影响 trusted
            result["trusted"] = False

        issues: list = []
        days = result["days_left"]
        if days is not None and days < 0:
            issues.append("expired")
        elif days is not None and days <= HEALTH_WARN_DAYS:
            issues.append("expiring")
        if result["protocol"] in WEAK_PROTOCOLS:
            issues.append("weak_protocol")
        if not result["hostname_ok"]:
            issues.append("hostname_mismatch")
        if not result["trusted"]:
            issues.append("chain_untrusted")
        if result["key_type"] == "RSA" and 0 < result["key_bits"] < 2048:
            issues.append("weak_key")
        if result["signature_hash"].lower() in ("md5", "sha1"):
            issues.append("weak_signature")
        result["issues"] = issues

        if "expired" in issues or "hostname_mismatch" in issues:
            result["ok"] = False
        elif issues:
            result["ok"] = True  # 可用但有隐患：由页面对 issues 标黄
        else:
            result["ok"] = True
        result["detail"] = ", ".join(issues)
        return result
    except Exception as exc:  # noqa: BLE001 - 体检失败就是「读不到」，不冒充健康
        result["detail"] = str(exc)
        return result


def _cert_days_left(host: str, port: int = 443) -> Optional[int]:
    """读对端证书的剩余天数；连不上或没有证书返回 None。"""
    try:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
        if not der:
            return None
        from cryptography import x509

        cert = x509.load_der_x509_certificate(der)
        expire = getattr(cert, "not_valid_after_utc", None)
        stamp = expire.timestamp() if expire is not None else cert.not_valid_after.timestamp()
        return int((stamp - time.time()) // 86400)
    except Exception:  # noqa: BLE001 - 探测失败就是「读不到」，不算监控失败
        return None


def _url_host(url: str) -> tuple:
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else "https://" + url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return str(parsed.hostname or ""), int(port)


async def probe(url: str) -> Dict[str, Any]:
    """探测一个 URL，返回状态、耗时与对端证书剩余天数。"""
    target = str(url or "").strip()
    if not target:
        return {"status": "", "code": 0, "latency_ms": 0, "error": "", "cert_days": None}
    if "://" not in target:
        target = "https://" + target
    # 站点监控地址由用户填写：内网 / 回环一律拒绝（后台会反复探测，比一次性
    # 请求更适合做内网测绘）。内网站点确有需要时由管理员加进出站白名单。
    try:
        await ssrfguard.assert_url_allowed(target)
    except ssrfguard.TargetBlocked as exc:
        return {
            "status": "down",
            "code": 0,
            "latency_ms": 0,
            "error": str(exc)[:200],
            "cert_days": None,
        }
    started = time.perf_counter()
    status, code, error = "down", 0, ""
    try:
        # 恢复证书校验：关掉它等于让这条出站通道可被中间人利用。自签站点会报
        # 证书错误，需要把 CA 装进面板的信任链，而不是把校验关掉。
        async with httpx.AsyncClient(
            timeout=TIMEOUT, verify=True, follow_redirects=True
        ) as client:
            response = await client.get(target)
        code = int(response.status_code)
        if code >= 500:
            status, error = "down", f"HTTP {code}"
        else:
            status = "up"
    except Exception as exc:  # noqa: BLE001 - 任何异常都算 down，原因留给页面
        error = str(exc)[:200]
    latency = int((time.perf_counter() - started) * 1000)

    host, port = _url_host(target)
    cert_days = await asyncio.to_thread(_cert_days_left, host, port) if host else None
    return {
        "status": status,
        "code": code,
        "latency_ms": latency,
        "error": error,
        "cert_days": cert_days,
    }


async def check_site(site: Dict[str, Any]) -> Dict[str, Any]:
    """探测一个站点并写回状态；状态翻转时发一次通知。"""
    url = str(site.get("monitor_url") or "").strip()
    if not url:
        return {}
    previous = str(site.get("monitor_status") or "")
    result = await probe(url)

    site["monitor_status"] = result["status"]
    site["monitor_checked_at"] = int(time.time())
    site["monitor_latency_ms"] = result["latency_ms"]
    site["monitor_error"] = result["error"]
    if result["cert_days"] is not None:
        # 对端证书剩余天数直接落到 expire_at：页面上的「到期」与监控一致
        site["expire_at"] = int(time.time()) + int(result["cert_days"]) * 86400
        site["last_check_at"] = int(time.time())

    # TLS 体检：只在 https（或没写 scheme，按 https 处理）时做。它是两次额外
    # 握手，放在线程里跑，别阻塞每 5 分钟一轮的作业。
    health: Dict[str, Any] = {}
    host, port = _url_host(url)
    # 只有 https 才做 TLS 体检：对 http（80 端口）握手只会得到一个
    # 「wrong version number」，把它记成健康问题等于误报。
    scheme = "https" if "://" not in url else url.split("://", 1)[0].lower()
    if host and scheme == "https":
        try:
            health = await asyncio.to_thread(inspect_tls, host, port)
        except Exception:  # noqa: BLE001 - 体检失败只是没有结论
            health = {}
    if health:
        previous_health = str(site.get("health_status") or "")
        issues = [str(item) for item in health.get("issues") or []]
        site["health_checked_at"] = int(time.time())
        site["tls_protocol"] = str(health.get("protocol") or "")
        site["tls_trusted"] = bool(health.get("trusted"))
        site["tls_hostname_ok"] = bool(health.get("hostname_ok"))
        site["health_issues"] = issues
        site["health_detail"] = str(health.get("detail") or "")
        if health.get("days_left") is None:
            site["health_status"] = "unknown"
        elif not health.get("ok"):
            site["health_status"] = "bad"
        elif issues:
            site["health_status"] = "warn"
        else:
            site["health_status"] = "ok"
        if health.get("issuer"):
            site["cert_issuer"] = str(health["issuer"])
        if health.get("fingerprint"):
            site["cert_fingerprint"] = str(health["fingerprint"])
        if health.get("serial"):
            site["cert_serial"] = str(health["serial"])

        # 只在「刚变坏」时喊一次：每 5 分钟重复发一条没人会看
        if site["health_status"] == "bad" and previous_health != "bad":
            await certnotify.notify(
                certs.site_owner(site),
                site,
                certnotify.EVENT_HEALTH_BAD,
                i18n.pick(
                    "证书健康检查不通过：" + str(site.get("domain") or url),
                    "Certificate health check failed: " + str(site.get("domain") or url),
                ),
                i18n.pick(
                    "问题：" + "、".join(issues) + "。证书可能无法被浏览器正常校验。",
                    "Issues: " + ", ".join(issues) + ". Clients may reject this certificate.",
                ),
            )

    name = str(site.get("name") or site.get("domain") or url)
    if result["status"] == "down" and previous != "down":
        await certnotify.notify(
            certs.site_owner(site),
            site,
            certnotify.EVENT_SITE_DOWN,
            i18n.pick("站点不可用：" + url, "Site unreachable: " + url),
            i18n.pick(
                "站点「" + name + "」探测失败：" + (result["error"] or "未知错误"),
                f'Site "{name}" probe failed: {result["error"] or "unknown error"}',
            ),
        )
    elif result["status"] == "up" and previous == "down":
        await certnotify.notify(
            certs.site_owner(site),
            site,
            certnotify.EVENT_SITE_UP,
            i18n.pick("站点已恢复：" + url, "Site recovered: " + url),
            i18n.pick(
                "站点「" + name + "」已恢复，响应 " + str(result["latency_ms"]) + " ms",
                f'Site "{name}" is back, responded in {result["latency_ms"]} ms',
            ),
        )

    return {
        "site": name,
        "status": result["status"],
        "latency_ms": result["latency_ms"],
        "cert_days": result["cert_days"],
        "detail": result["error"] or f"HTTP {result['code']}",
    }


async def monitor_tick() -> List[Dict[str, Any]]:
    """定时作业：逐站点探测（只挑开了监控且有 URL 的启用站点）。"""
    sites = [
        site
        for site in await certs.load_sites()
        if site.get("enabled") and site.get("monitor_enabled") and site.get("monitor_url")
    ]
    results: List[Dict[str, Any]] = []
    for site in sites[:BATCH]:
        result = await check_site(site)
        if result:
            await certs.update_site(site)
            results.append(result)
    return results
