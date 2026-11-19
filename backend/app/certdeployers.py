"""云负载均衡 / CDN 的证书部署。

与 :mod:`certs` 的分工：本模块只负责「把一张证书推到云端并绑定」，不关心站
点、流水线、日志。凭据由调用方（certs）按站点归属者解密后传进来，避免这里
再依赖 store / crypto。

支持四类目标：
- 腾讯云 CLB：上传到 SSL，再 ``ModifyListener`` 绑定到 HTTPS 监听器；
- 阿里云 SLB：``UploadServerCertificate`` + ``SetLoadBalancerHTTPSListenerAttribute``；
- 腾讯云 CDN：上传到 SSL，再 ``UpdateDomainConfig`` 开启 HTTPS；
- 阿里云 CDN：``SetDomainServerCertificate``。

这些云 API 的签名与现有云厂商适配器保持同一口径（腾讯 TC3-HMAC-SHA256、
阿里 RPC HMAC-SHA1），不额外引入 SDK。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Dict

import httpx

from . import i18n


class DeployError(ValueError):
    """云部署过程中的业务错误（已带可读说明）。"""


# ------------------------------------------------------------------ 腾讯云
def _tc3_request(
    secret_id: str,
    secret_key: str,
    service: str,
    version: str,
    action: str,
    payload: Dict[str, Any],
    region: str = "",
) -> tuple[str, Dict[str, str], str]:
    """按 TC3-HMAC-SHA256 组一个腾讯云 API 请求。"""
    host = service + ".tencentcloudapi.com"
    body = json.dumps(payload, separators=(",", ":"))
    ts = int(time.time())
    date = time.strftime("%Y-%m-%d", time.gmtime(ts))
    content_type = "application/json; charset=utf-8"

    canonical_headers = "content-type:" + content_type + "\nhost:" + host + "\n"
    signed_headers = "content-type;host"
    hashed_payload = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical_request = "\n".join(
        ["POST", "/", "", canonical_headers, signed_headers, hashed_payload]
    )

    scope = date + "/" + service + "/tc3_request"
    string_to_sign = "\n".join(
        [
            "TC3-HMAC-SHA256",
            str(ts),
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    def _sign(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

    secret_date = _sign(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _sign(secret_date, service)
    secret_signing = _sign(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    headers = {
        "Authorization": (
            "TC3-HMAC-SHA256 Credential="
            + secret_id
            + "/"
            + scope
            + ", SignedHeaders="
            + signed_headers
            + ", Signature="
            + signature
        ),
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(ts),
        "X-TC-Version": version,
    }
    if region:
        headers["X-TC-Region"] = region
    return "https://" + host, headers, body


async def _tencent_call(
    cfg: Dict[str, Any],
    service: str,
    version: str,
    action: str,
    payload: Dict[str, Any],
    region: str = "",
) -> Dict[str, Any]:
    secret_id = str(cfg.get("secret_id") or "")
    secret_key = str(cfg.get("secret_key") or "")
    if not secret_id or not secret_key:
        raise DeployError(i18n.tr("请先配置腾讯云 API 密钥"))

    url, headers, body = _tc3_request(
        secret_id, secret_key, service, version, action, payload, region
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, content=body.encode("utf-8"), headers=headers)
    except httpx.HTTPError as exc:
        raise DeployError("请求腾讯云 " + action + " 失败：" + str(exc)) from exc

    try:
        data = resp.json()
    except ValueError as exc:
        raise DeployError(
            "腾讯云 " + action + " 返回了无法解析的内容（HTTP " + str(resp.status_code) + "）"
        ) from exc

    response = data.get("Response") or {}
    error = response.get("Error") or {}
    if error:
        raise DeployError(
            "腾讯云 "
            + action
            + " 失败："
            + str(error.get("Message") or error.get("Code") or "未知错误")
        )
    if resp.status_code >= 400:
        raise DeployError("腾讯云 " + action + " 失败（HTTP " + str(resp.status_code) + "）")
    return response


def _cert_alias(domain: str, suffix: str) -> str:
    """云端证书别名：带域名与来源，方便在控制台里认出来。"""
    text = "pvecenter-" + str(domain or "cert") + "-" + suffix
    return text[:64]


# ------------------------------------------------------------------ 阿里云
def _aliyun_signed_query(params: Dict[str, str], secret: str) -> str:
    encoded = "&".join(
        f"{_quote(str(k))}={_quote(str(v))}" for k, v in sorted(params.items())
    )
    string_to_sign = "GET&" + _quote("/") + "&" + _quote(encoded)
    digest = hmac.new(
        (secret + "&").encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1
    ).digest()
    return encoded + "&Signature=" + _quote(base64.b64encode(digest).decode("ascii"))


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


async def _aliyun_call(
    cfg: Dict[str, Any],
    host: str,
    version: str,
    action: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    key_id = str(cfg.get("access_key_id") or "")
    secret = str(cfg.get("access_key_secret") or "")
    if not key_id or not secret:
        raise DeployError(i18n.tr("请先配置阿里云 API 密钥"))

    common = {
        "AccessKeyId": key_id,
        "Action": action,
        "Format": "JSON",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": secrets.token_hex(16),
        "SignatureVersion": "1.0",
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "Version": version,
    }
    merged = {**common, **{k: str(v) for k, v in params.items() if v is not None}}
    query = _aliyun_signed_query(merged, secret)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get("https://" + host + "/?" + query)
    except httpx.HTTPError as exc:
        raise DeployError("请求阿里云 " + action + " 失败：" + str(exc)) from exc

    try:
        data = resp.json()
    except ValueError as exc:
        raise DeployError(
            "阿里云 " + action + " 返回了无法解析的内容（HTTP " + str(resp.status_code) + "）"
        ) from exc
    if resp.status_code >= 400 or data.get("Code"):
        raise DeployError(
            "阿里云 "
            + action
            + " 失败："
            + str(data.get("Message") or data.get("Code") or "未知错误")
        )
    return data


# ------------------------------------------------------------------ 对外入口
async def deploy_tencent_clb(
    cfg: Dict[str, Any], bundle: Dict[str, Any], target: Dict[str, Any], domain: str
) -> str:
    """上传证书到腾讯云 SSL，并绑定 CLB 的 HTTPS 监听器。"""
    lb_id = str(target.get("lb_id") or "").strip()
    listener_id = str(target.get("listener_id") or "").strip()
    if not lb_id or not listener_id:
        raise DeployError(i18n.tr("腾讯云 CLB 需要负载均衡实例 ID 与监听器 ID"))

    uploaded = await _tencent_call(
        cfg,
        "ssl",
        "2019-12-05",
        "UploadCertificate",
        {
            "Certificate": bundle["fullchain"],
            "PrivateKey": bundle["private_key"],
            "Alias": _cert_alias(domain, "clb"),
        },
    )
    cert_id = str(uploaded.get("CertificateId") or "")
    if not cert_id:
        raise DeployError(i18n.tr("腾讯云没有返回证书 ID"))

    region = str(target.get("cloud_region") or "ap-guangzhou")
    await _tencent_call(
        cfg,
        "clb",
        "2018-03-17",
        "ModifyListener",
        {
            "LoadBalancerId": lb_id,
            "ListenerId": listener_id,
            "Certificate": {"SSLMode": "UNIDIRECTIONAL", "CertId": cert_id},
        },
        region=region,
    )
    return i18n.pick(
        "已上传证书并绑定腾讯云 CLB（" + cert_id + "）",
        "Uploaded and bound to Tencent Cloud CLB (" + cert_id + ")",
    )


async def deploy_aliyun_slb(
    cfg: Dict[str, Any], bundle: Dict[str, Any], target: Dict[str, Any], domain: str
) -> str:
    """上传证书到阿里云，并绑定 SLB 的 HTTPS 监听。"""
    lb_id = str(target.get("lb_id") or "").strip()
    if not lb_id:
        raise DeployError(i18n.tr("阿里云 SLB 需要负载均衡实例 ID"))
    port = str(target.get("listener_port") or "443")

    uploaded = await _aliyun_call(
        cfg,
        "slb.aliyuncs.com",
        "2014-05-15",
        "UploadServerCertificate",
        {
            "ServerCertificate": bundle["fullchain"],
            "PrivateKey": bundle["private_key"],
            "ServerCertificateName": _cert_alias(domain, "slb"),
        },
    )
    cert_id = str(uploaded.get("ServerCertificateId") or "")
    if not cert_id:
        raise DeployError(i18n.tr("阿里云没有返回证书 ID"))

    await _aliyun_call(
        cfg,
        "slb.aliyuncs.com",
        "2014-05-15",
        "SetLoadBalancerHTTPSListenerAttribute",
        {
            "LoadBalancerId": lb_id,
            "ListenerPort": port,
            "ServerCertificateId": cert_id,
        },
    )
    return i18n.pick(
        "已上传证书并绑定阿里云 SLB（" + cert_id + "）",
        "Uploaded and bound to Alibaba Cloud SLB (" + cert_id + ")",
    )


async def deploy_tencent_cdn(
    cfg: Dict[str, Any], bundle: Dict[str, Any], target: Dict[str, Any], domain: str
) -> str:
    """上传证书到腾讯云 SSL，并为 CDN 域名开启 HTTPS。"""
    cdn_domain = str(target.get("cdn_domain") or domain or "").strip()
    if not cdn_domain:
        raise DeployError(i18n.tr("腾讯云 CDN 需要加速域名"))

    uploaded = await _tencent_call(
        cfg,
        "ssl",
        "2019-12-05",
        "UploadCertificate",
        {
            "Certificate": bundle["fullchain"],
            "PrivateKey": bundle["private_key"],
            "Alias": _cert_alias(cdn_domain, "cdn"),
        },
    )
    cert_id = str(uploaded.get("CertificateId") or "")
    if not cert_id:
        raise DeployError(i18n.tr("腾讯云没有返回证书 ID"))

    await _tencent_call(
        cfg,
        "cdn",
        "2018-06-06",
        "UpdateDomainConfig",
        {"Domain": cdn_domain, "Https": {"Switch": "on", "CertId": cert_id}},
    )
    return i18n.pick(
        "已上传证书并开启腾讯云 CDN HTTPS（" + cert_id + "）",
        "Uploaded and enabled Tencent Cloud CDN HTTPS (" + cert_id + ")",
    )


async def deploy_aliyun_cdn(
    cfg: Dict[str, Any], bundle: Dict[str, Any], target: Dict[str, Any], domain: str
) -> str:
    """为阿里云 CDN 域名上传并启用证书。"""
    cdn_domain = str(target.get("cdn_domain") or domain or "").strip()
    if not cdn_domain:
        raise DeployError(i18n.tr("阿里云 CDN 需要加速域名"))

    await _aliyun_call(
        cfg,
        "cdn.aliyuncs.com",
        "2018-05-10",
        "SetDomainServerCertificate",
        {
            "DomainName": cdn_domain,
            "ServerCertificateStatus": "on",
            "CertName": _cert_alias(cdn_domain, "cdn"),
            "ServerCertificate": bundle["fullchain"],
            "PrivateKey": bundle["private_key"],
        },
    )
    return i18n.pick(
        "已上传证书并开启阿里云 CDN HTTPS",
        "Uploaded and enabled Alibaba Cloud CDN HTTPS",
    )
