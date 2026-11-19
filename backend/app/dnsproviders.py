"""DNS 服务商适配层：给 ACME 的 DNS-01 挑战增删一条 TXT 记录。

只做一件事 —— ``create_txt(name, value) -> record_id`` 与
``delete_txt(name, record_id)``。四条实现都按各家签名规则手写，不引入 SDK：
项目里腾讯云 SSL 已经说明了这套取向，多拉四家 SDK 会让镜像大一圈。

* **DNSPod**（腾讯云）   TC3-HMAC-SHA256
* **阿里云 DNS**         RPC 风格，HMAC-SHA1
* **华为云 DNS**         SDK-HMAC-SHA256
* **Cloudflare**         Bearer Token，最省事的一家

``name`` 一律传完整记录名（``_acme-challenge.example.com``），内部自己找 zone；
泛域名的 ``*.`` 已在调用方剥掉，这里不处理通配符语义。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode
from xml.sax.saxutils import escape as _xml_escape

import httpx

HTTP_TIMEOUT = 20.0

#: 记录 TTL。验证期间要尽快生效，所以取各家允许的最小值附近。
TXT_TTL = 60

#: 各服务商的字段 schema（配置驱动扩展）。
#:
#: 新增一家 DNS 服务商只需要在这里加一条 + 在下面实现 ``XxxProvider``，然后
#: 在 :func:`build_provider` 里接上工厂即可；ACME 配置表单、加密字段、默认值与
#: 标签全部由 :func:`field_keys` / :func:`secret_keys` / :func:`schema_defaults`
#: 自动派生，不用再同步改动 ``certs.py`` 里的一堆映射表。
#:
#: 每个字段：``key`` 配置键、``label_key`` 前端文案键、``secret`` 是否加密落库、
#: ``default`` 默认值、``required`` 是否必填。
SCHEMAS: Dict[str, Dict[str, Any]] = {
    "dnspod": {
        "label": "DNSPod（腾讯云）",
        "fields": (
            {"key": "secret_id", "label_key": "cert.fieldSecretId", "secret": False, "default": "", "required": True},
            {"key": "secret_key", "label_key": "cert.fieldSecretKey", "secret": True, "default": "", "required": True},
        ),
    },
    "aliyun": {
        "label": "阿里云 DNS",
        "fields": (
            {"key": "access_key_id", "label_key": "cert.fieldAccessKeyId", "secret": False, "default": "", "required": True},
            {"key": "access_key_secret", "label_key": "cert.fieldAccessKeySecret", "secret": True, "default": "", "required": True},
        ),
    },
    "huawei": {
        "label": "华为云 DNS",
        "fields": (
            {"key": "access_key_id", "label_key": "cert.fieldAccessKeyId", "secret": False, "default": "", "required": True},
            {"key": "access_key_secret", "label_key": "cert.fieldAccessKeySecret", "secret": True, "default": "", "required": True},
        ),
    },
    "cloudflare": {
        "label": "Cloudflare",
        "fields": (
            {"key": "api_token", "label_key": "cert.fieldApiToken", "secret": True, "default": "", "required": True},
        ),
    },
    "godaddy": {
        "label": "GoDaddy",
        "fields": (
            {"key": "api_key", "label_key": "cert.fieldApiKey", "secret": False, "default": "", "required": True},
            {"key": "api_secret", "label_key": "cert.fieldApiSecret", "secret": True, "default": "", "required": True},
        ),
    },
    "route53": {
        "label": "AWS Route 53",
        "fields": (
            {"key": "access_key_id", "label_key": "cert.fieldAccessKeyId", "secret": False, "default": "", "required": True},
            {"key": "secret_access_key", "label_key": "cert.fieldSecretAccessKey", "secret": True, "default": "", "required": True},
            {"key": "region", "label_key": "cert.fieldRegion", "secret": False, "default": "us-east-1", "required": False},
        ),
    },
    "namecheap": {
        "label": "Namecheap",
        "fields": (
            {"key": "api_user", "label_key": "cert.fieldApiUser", "secret": False, "default": "", "required": True},
            {"key": "api_key", "label_key": "cert.fieldApiKey", "secret": True, "default": "", "required": True},
            {"key": "username", "label_key": "cert.fieldUsername", "secret": False, "default": "", "required": False},
            {"key": "client_ip", "label_key": "cert.fieldClientIp", "secret": False, "default": "", "required": False},
        ),
    },
    "volcengine": {
        "label": "火山引擎（Volcengine）",
        "fields": (
            {"key": "access_key", "label_key": "cert.fieldAccessKey", "secret": False, "default": "", "required": True},
            {"key": "secret_key", "label_key": "cert.fieldSecretKey", "secret": True, "default": "", "required": True},
            {"key": "region", "label_key": "cert.fieldRegion", "secret": False, "default": "cn-beijing", "required": False},
        ),
    },
}

PROVIDER_LABELS = {kind: str(schema["label"]) for kind, schema in SCHEMAS.items()}

PROVIDERS = tuple(SCHEMAS)

#: 字段名 → 前端文案键（同名字段在各家之间语义一致，可全局共用）
FIELD_LABELS: Dict[str, str] = {
    str(item["key"]): str(item["label_key"])
    for schema in SCHEMAS.values()
    for item in schema["fields"]
}


def field_keys(kind: str) -> Tuple[str, ...]:
    """某家服务商在配置表单里需要哪些字段（含非密文字段）。"""
    return tuple(str(item["key"]) for item in SCHEMAS.get(kind, {}).get("fields", ()))


def secret_keys(kind: str) -> Tuple[str, ...]:
    """某家服务商里需要加密落库的字段。"""
    return tuple(
        str(item["key"])
        for item in SCHEMAS.get(kind, {}).get("fields", ())
        if item.get("secret")
    )


def schema_defaults() -> Dict[str, Dict[str, Any]]:
    """各家服务商的默认配置块，用于初始化 ACME 配置。"""
    return {
        kind: {
            str(item["key"]): item.get("default", "")
            for item in schema["fields"]
        }
        for kind, schema in SCHEMAS.items()
    }


def form_fields(kind: str) -> Tuple[Dict[str, Any], ...]:
    """给前端配置表单用的字段描述（key / label_key / secret / required）。"""
    return tuple(
        {
            "key": str(item["key"]),
            "label_key": str(item["label_key"]),
            "secret": bool(item.get("secret")),
            "required": bool(item.get("required", True)),
        }
        for item in SCHEMAS.get(kind, {}).get("fields", ())
    )


class DnsError(Exception):
    """DNS 服务商接口返回的错误。"""


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT)


def zone_candidates(name: str) -> List[str]:
    """从完整记录名逐级截断出可能的 zone，供「哪个才是托管域」的探测用。"""
    parts = [p for p in str(name or "").strip(".").split(".") if p]
    out: List[str] = []
    for index in range(len(parts) - 1):
        candidate = ".".join(parts[index:])
        if candidate.count(".") >= 1:
            out.append(candidate)
    return out


class DnsProvider:
    """接口约定。实现类只需保证两个方法，挑战流程不关心是哪家。"""

    name = ""

    async def create_txt(self, name: str, value: str) -> str:
        raise NotImplementedError

    async def delete_txt(self, name: str, record_id: str) -> None:
        raise NotImplementedError


# ------------------------------------------------------------------ DNSPod
def _tc3_headers(
    secret_id: str,
    secret_key: str,
    *,
    service: str,
    host: str,
    action: str,
    version: str,
    payload: str,
) -> Dict[str, str]:
    """腾讯云 API 3.0 的 TC3-HMAC-SHA256 签名（与 app/tencent_ssl.py 同一套）。"""
    timestamp = int(time.time())
    date = time.strftime("%Y-%m-%d", time.gmtime(timestamp))
    content_type = "application/json; charset=utf-8"

    canonical_headers = f"content-type:{content_type}\nhost:{host}\nx-tc-action:{action.lower()}\n"
    signed_headers = "content-type;host;x-tc-action"
    hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canonical_request = "\n".join(
        ["POST", "/", "", canonical_headers, signed_headers, hashed_payload]
    )

    scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join(
        [
            "TC3-HMAC-SHA256",
            str(timestamp),
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    secret_date = _sign(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _sign(secret_date, service)
    secret_signing = _sign(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return {
        "Authorization": (
            f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": version,
    }


class DnspodProvider(DnsProvider):
    name = "dnspod"

    HOST = "dnspod.tencentcloudapi.com"
    VERSION = "2021-03-23"

    def __init__(self, secret_id: str, secret_key: str) -> None:
        if not secret_id or not secret_key:
            raise DnsError("DNSPod 需要 SecretId 与 SecretKey")
        self.secret_id = secret_id
        self.secret_key = secret_key

    async def _call(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"))
        headers = _tc3_headers(
            self.secret_id,
            self.secret_key,
            service="dnspod",
            host=self.HOST,
            action=action,
            version=self.VERSION,
            payload=body,
        )
        async with _client() as client:
            resp = await client.post("https://" + self.HOST, content=body, headers=headers)
        try:
            data = resp.json().get("Response") or {}
        except ValueError as exc:
            raise DnsError("DNSPod 返回的不是 JSON") from exc
        error = data.get("Error")
        if error:
            raise DnsError(f"DNSPod {action} 失败：{error.get('Message') or error.get('Code')}")
        return data

    async def _find_zone(self, name: str) -> str:
        last: Optional[Exception] = None
        for candidate in zone_candidates(name):
            try:
                data = await self._call("DescribeRecordList", {"Domain": candidate, "Limit": 1})
                if data.get("Domain"):
                    return str(data["Domain"])
                return candidate
            except DnsError as exc:
                last = exc
                continue
        raise DnsError(f"找不到 {name} 对应的 DNSPod 域名") from last

    async def create_txt(self, name: str, value: str) -> str:
        zone = await self._find_zone(name)
        sub = name[: -(len(zone) + 1)] if name.endswith("." + zone) else name
        data = await self._call(
            "CreateRecord",
            {
                "Domain": zone,
                "SubDomain": sub,
                "RecordType": "TXT",
                "RecordLine": "默认",
                "Value": value,
                "TTL": TXT_TTL,
            },
        )
        return str((data.get("RecordInfo") or {}).get("Id") or "")

    async def delete_txt(self, name: str, record_id: str) -> None:
        if not record_id:
            return
        zone = await self._find_zone(name)
        sub = name[: -(len(zone) + 1)] if name.endswith("." + zone) else name
        await self._call(
            "DeleteRecord", {"Domain": zone, "RecordId": int(record_id), "SubDomain": sub}
        )


# ------------------------------------------------------------------ 阿里云
def _aliyun_query(params: Dict[str, str], secret: str) -> str:
    """RPC 签名：按字典序拼 query，再 HMAC-SHA1(secret + "&")。"""
    encoded = "&".join(
        f"{quote(str(k), safe='')}={quote(str(v), safe='')}" for k, v in sorted(params.items())
    )
    string_to_sign = "GET&" + quote("/", safe="") + "&" + quote(encoded, safe="")
    digest = hmac.new(
        (secret + "&").encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1
    ).digest()
    return encoded + "&Signature=" + quote(base64.b64encode(digest).decode("ascii"), safe="")


class AliyunProvider(DnsProvider):
    name = "aliyun"

    HOST = "alidns.cn-hangzhou.aliyuncs.com"
    VERSION = "2015-01-09"

    def __init__(self, access_key_id: str, access_key_secret: str) -> None:
        if not access_key_id or not access_key_secret:
            raise DnsError("阿里云需要 AccessKey ID 与 Secret")
        self.key_id = access_key_id
        self.secret = access_key_secret

    async def _call(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        common = {
            "AccessKeyId": self.key_id,
            "Action": action,
            "Format": "JSON",
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": secrets.token_hex(16),
            "SignatureVersion": "1.0",
            "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "Version": self.VERSION,
        }
        merged = {**common, **{k: str(v) for k, v in params.items()}}
        query = _aliyun_query(merged, self.secret)
        async with _client() as client:
            resp = await client.get("https://" + self.HOST + "/?" + query)
        try:
            data = resp.json()
        except ValueError as exc:
            raise DnsError("阿里云返回的不是 JSON") from exc
        if resp.status_code >= 400 or data.get("Code"):
            raise DnsError(f"阿里云 {action} 失败：{data.get('Message') or data.get('Code')}")
        return data

    async def _find_zone(self, name: str) -> str:
        last: Optional[Exception] = None
        for candidate in zone_candidates(name):
            try:
                await self._call("DescribeDomainRecords", {"DomainName": candidate, "PageSize": 1})
                return candidate
            except DnsError as exc:
                last = exc
                continue
        raise DnsError(f"找不到 {name} 对应的阿里云域名") from last

    async def create_txt(self, name: str, value: str) -> str:
        zone = await self._find_zone(name)
        sub = name[: -(len(zone) + 1)] if name.endswith("." + zone) else name
        data = await self._call(
            "AddDomainRecord",
            {
                "DomainName": zone,
                "RR": sub,
                "Type": "TXT",
                "Value": value,
                "TTL": TXT_TTL,
            },
        )
        return str(data.get("RecordId") or "")

    async def delete_txt(self, name: str, record_id: str) -> None:
        if not record_id:
            return
        await self._call("DeleteDomainRecord", {"RecordId": record_id})


# ------------------------------------------------------------------ 华为云
def _huawei_headers(
    key_id: str, secret: str, method: str, uri: str, query: str, body: str
) -> Dict[str, str]:
    """SDK-HMAC-SHA256：签名串是「方法 / 路径 / 查询 / 头 / 头名 / 载荷哈希」。"""
    now = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    host = "dns.myhuaweicloud.com"
    content_type = "application/json;charset=utf8"
    canonical_headers = f"content-type:{content_type}\nhost:{host}\nx-sdk-date:{now}\n"
    signed_headers = "content-type;host;x-sdk-date"
    canonical_request = "\n".join(
        [
            method,
            uri if uri.endswith("/") else uri + "/",
            query,
            canonical_headers,
            signed_headers,
            hashlib.sha256(body.encode("utf-8")).hexdigest(),
        ]
    )
    string_to_sign = "\n".join(
        [
            "SDK-HMAC-SHA256",
            now,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {
        "Authorization": (
            f"SDK-HMAC-SHA256 Access={key_id}, SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        ),
        "Content-Type": content_type,
        "Host": host,
        "X-Sdk-Date": now,
    }


class HuaweiProvider(DnsProvider):
    name = "huawei"

    HOST = "dns.myhuaweicloud.com"

    def __init__(self, access_key_id: str, access_key_secret: str) -> None:
        if not access_key_id or not access_key_secret:
            raise DnsError("华为云需要 AK 与 SK")
        self.key_id = access_key_id
        self.secret = access_key_secret

    async def _call(
        self, method: str, path: str, query: str = "", payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":")) if payload else ""
        headers = _huawei_headers(self.key_id, self.secret, method, path, query, body)
        url = "https://" + self.HOST + path + (("?" + query) if query else "")
        async with _client() as client:
            resp = await client.request(method, url, content=body, headers=headers)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            raise DnsError(
                f"华为云 {path} 失败：{data.get('message') or data.get('error_msg') or resp.status_code}"
            )
        return data

    async def _find_zone(self, name: str) -> Dict[str, str]:
        last: Optional[Exception] = None
        for candidate in zone_candidates(name):
            try:
                data = await self._call(
                    "GET", "/v2/zones", urlencode({"name": candidate + "."})
                )
                zones = data.get("zones") or []
                if zones:
                    zone = zones[0]
                    return {"id": str(zone.get("id") or ""), "name": str(zone.get("name") or "")}
            except DnsError as exc:
                last = exc
                continue
        raise DnsError(f"找不到 {name} 对应的华为云域名") from last

    async def create_txt(self, name: str, value: str) -> str:
        zone = await self._find_zone(name)
        zone_name = zone["name"].rstrip(".")
        sub = name[: -(len(zone_name) + 1)] if name.endswith("." + zone_name) else name
        data = await self._call(
            "POST",
            f"/v2/zones/{zone['id']}/recordsets",
            payload={
                "name": sub + "." + zone_name + ".",
                "type": "TXT",
                "ttl": TXT_TTL,
                "records": [f'"{value}"'],
            },
        )
        return str(data.get("id") or "")

    async def delete_txt(self, name: str, record_id: str) -> None:
        if not record_id:
            return
        zone = await self._find_zone(name)
        await self._call("DELETE", f"/v2/zones/{zone['id']}/recordsets/{record_id}")


# ------------------------------------------------------------------ Cloudflare
class CloudflareProvider(DnsProvider):
    name = "cloudflare"

    API = "https://api.cloudflare.com/client/v4"

    def __init__(self, api_token: str) -> None:
        if not api_token:
            raise DnsError("Cloudflare 需要 API Token")
        self.token = api_token

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}

    async def _call(
        self, method: str, path: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        async with _client() as client:
            resp = await client.request(
                method, self.API + path, json=payload, headers=self._headers()
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise DnsError("Cloudflare 返回的不是 JSON") from exc
        if not data.get("success"):
            errors = data.get("errors") or []
            raise DnsError(
                "Cloudflare 请求失败："
                + "; ".join(str(e.get("message") or e) for e in errors)[:300]
            )
        return data

    async def _find_zone(self, name: str) -> Dict[str, str]:
        last: Optional[Exception] = None
        for candidate in zone_candidates(name):
            try:
                data = await self._call("GET", "/zones?" + urlencode({"name": candidate}))
                zones = data.get("result") or []
                if zones:
                    return {"id": str(zones[0].get("id") or ""), "name": str(zones[0].get("name") or "")}
            except DnsError as exc:
                last = exc
                continue
        raise DnsError(f"找不到 {name} 对应的 Cloudflare 域名") from last

    async def create_txt(self, name: str, value: str) -> str:
        zone = await self._find_zone(name)
        data = await self._call(
            "POST",
            f"/zones/{zone['id']}/dns_records",
            {"type": "TXT", "name": name, "content": value, "ttl": TXT_TTL},
        )
        return str((data.get("result") or {}).get("id") or "")

    async def delete_txt(self, name: str, record_id: str) -> None:
        if not record_id:
            return
        zone = await self._find_zone(name)
        await self._call("DELETE", f"/zones/{zone['id']}/dns_records/{record_id}")


# ------------------------------------------------------------------ GoDaddy
class GodaddyProvider(DnsProvider):
    """GoDaddy：``sso-key`` 认证；记录接口没有单条 ID，按 TXT 值定位。

    GoDaddy 的 PUT 是**覆盖**某个 name 下的整组记录，所以这里先读出来、合并再写，
    避免同一记录名下的多个挑战值互相顶掉（``example.com`` 与 ``*.example.com``
    会同时写 ``_acme-challenge.example.com``）。
    """

    name = "godaddy"

    API = "https://api.godaddy.com/v1"
    TTL = 600  # GoDaddy 允许的最小 TTL

    def __init__(self, api_key: str, api_secret: str) -> None:
        if not api_key or not api_secret:
            raise DnsError("GoDaddy 需要 API Key 与 API Secret")
        self.api_key = api_key
        self.api_secret = api_secret

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"sso-key {self.api_key}:{self.api_secret}",
            "Content-Type": "application/json",
        }

    async def _call(self, method: str, path: str, payload: Any = None) -> Any:
        async with _client() as client:
            resp = await client.request(
                method, self.API + path, json=payload, headers=self._headers()
            )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("message") or resp.text[:300]
            except ValueError:
                detail = resp.text[:300]
            raise DnsError(f"GoDaddy 请求失败（HTTP {resp.status_code}）：{detail}")
        if not resp.text:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    async def _find_zone(self, name: str) -> str:
        last: Optional[Exception] = None
        for candidate in zone_candidates(name):
            try:
                data = await self._call("GET", f"/domains/{candidate}")
                if data and data.get("domain"):
                    return str(data["domain"])
            except DnsError as exc:
                last = exc
        raise DnsError(f"找不到 {name} 对应的 GoDaddy 域名") from last

    async def _read_txt(self, zone: str, record_name: str) -> List[Dict[str, Any]]:
        path = f"/domains/{zone}/records/TXT/{quote(record_name, safe='')}"
        try:
            data = await self._call("GET", path)
        except DnsError:
            return []
        return [
            {"data": str(item.get("data") or ""), "ttl": self.TTL}
            for item in (data or [])
            if isinstance(item, dict)
        ]

    async def create_txt(self, name: str, value: str) -> str:
        zone = await self._find_zone(name)
        record_name = name[: -(len(zone) + 1)] if name.endswith("." + zone) else name
        records = [r for r in await self._read_txt(zone, record_name) if r["data"] != value]
        records.append({"data": value, "ttl": self.TTL})
        await self._call(
            "PUT", f"/domains/{zone}/records/TXT/{quote(record_name, safe='')}", records
        )
        return value

    async def delete_txt(self, name: str, record_id: str) -> None:
        if not record_id:
            return
        zone = await self._find_zone(name)
        record_name = name[: -(len(zone) + 1)] if name.endswith("." + zone) else name
        records = [r for r in await self._read_txt(zone, record_name) if r["data"] != record_id]
        path = f"/domains/{zone}/records/TXT/{quote(record_name, safe='')}"
        if records:
            await self._call("PUT", path, records)
        else:
            # 记录组清空后 PUT [] 会被 GoDaddy 拒绝，改用 DELETE 删掉整个名字
            await self._call("DELETE", path)


# ------------------------------------------------------------------ AWS Route 53
_R53_NS = {"r": "https://route53.amazonaws.com/doc/2013-04-01/"}


class Route53Provider(DnsProvider):
    """AWS Route 53：SigV4 签名 + XML 请求体。

    TXT 值在 Route 53 里必须是带双引号的字符串；同一记录名的多个值用 UPSERT
    合并写入，删除时按值精确移除，最后一个值删掉时发 DELETE 清掉整个 RRSet。
    """

    name = "route53"

    HOST = "route53.amazonaws.com"
    SERVICE = "route53"
    TTL = 60

    def __init__(
        self, access_key_id: str, secret_access_key: str, region: str = "us-east-1"
    ) -> None:
        if not access_key_id or not secret_access_key:
            raise DnsError("Route 53 需要 Access Key ID 与 Secret Access Key")
        self.key_id = access_key_id
        self.secret = secret_access_key
        # Route 53 是全局服务，签名 region 固定 us-east-1（用户填错也纠正回来）
        self.region = region or "us-east-1"
        if self.region not in ("us-east-1", "aws-global"):
            self.region = "us-east-1"

    def _sign(
        self, method: str, path: str, query: str, body: bytes
    ) -> Dict[str, str]:
        now = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        date = now[:8]
        payload_hash = hashlib.sha256(body).hexdigest()
        canonical_headers = (
            f"host:{self.HOST}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{now}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join(
            [method, path, query, canonical_headers, signed_headers, payload_hash]
        )
        scope = f"{date}/{self.region}/{self.SERVICE}/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                now,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )

        def _sign(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        k_date = _sign(("AWS4" + self.secret).encode("utf-8"), date)
        k_region = _sign(k_date, self.region)
        k_service = _sign(k_region, self.SERVICE)
        k_signing = _sign(k_service, "aws4_request")
        signature = hmac.new(
            k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return {
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={self.key_id}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
            "Content-Type": "application/xml",
            "Host": self.HOST,
            "X-Amz-Content-Sha256": payload_hash,
            "X-Amz-Date": now,
        }

    async def _call(
        self, method: str, path: str, params: Optional[Dict[str, Any]] = None, body: str = ""
    ) -> str:
        query = "&".join(
            f"{quote(str(k), safe='')}={quote(str(v), safe='')}"
            for k, v in sorted((params or {}).items())
        )
        payload = body.encode("utf-8")
        headers = self._sign(method, path, query, payload)
        url = "https://" + self.HOST + path + (("?" + query) if query else "")
        async with _client() as client:
            resp = await client.request(
                method, url, content=payload if body else None, headers=headers
            )
        if resp.status_code >= 400:
            raise DnsError(
                f"Route 53 请求失败（HTTP {resp.status_code}）：{resp.text[:300]}"
            )
        return resp.text

    async def _find_zone(self, name: str) -> Dict[str, str]:
        last: Optional[Exception] = None
        for candidate in zone_candidates(name):
            try:
                text = await self._call(
                    "GET",
                    "/2013-04-01/hostedzonesbyname",
                    {"dnsname": candidate, "maxitems": "1"},
                )
                root = ET.fromstring(text)
                zone = root.find(".//r:HostedZone", _R53_NS)
                if zone is None:
                    continue
                zname = (
                    zone.findtext("r:Name", default="", namespaces=_R53_NS) or ""
                ).rstrip(".")
                if zname != candidate:
                    continue
                zid = (
                    zone.findtext("r:Id", default="", namespaces=_R53_NS) or ""
                ).split("/")[-1]
                return {"id": zid, "name": zname}
            except (DnsError, ET.ParseError) as exc:
                last = exc
        raise DnsError(f"找不到 {name} 对应的 Route 53 托管区") from last

    async def _get_rrset(
        self, zone_id: str, fqdn: str
    ) -> Tuple[int, List[str]]:
        """读出该名字下现有的 TXT 值（带双引号）与 TTL。"""
        try:
            text = await self._call(
                "GET",
                f"/2013-04-01/hostedzone/{zone_id}/rrset",
                {"name": fqdn, "type": "TXT", "maxitems": "1"},
            )
        except DnsError:
            return self.TTL, []
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return self.TTL, []
        for rr in root.findall(".//r:ResourceRecordSet", _R53_NS):
            rname = rr.findtext("r:Name", default="", namespaces=_R53_NS) or ""
            rtype = rr.findtext("r:Type", default="", namespaces=_R53_NS) or ""
            if rname != fqdn or rtype != "TXT":
                continue
            ttl_text = rr.findtext("r:TTL", default="", namespaces=_R53_NS) or ""
            values = [
                (node.text or "")
                for node in rr.findall(
                    "r:ResourceRecords/r:ResourceRecord/r:Value", _R53_NS
                )
            ]
            return (int(ttl_text) if ttl_text.isdigit() else self.TTL), values
        return self.TTL, []

    def _change_body(
        self, action: str, fqdn: str, ttl: int, values: List[str]
    ) -> str:
        records = "".join(
            f"<ResourceRecord><Value>{_xml_escape(v)}</Value></ResourceRecord>"
            for v in values
        )
        return (
            '<ChangeResourceRecordSetsRequest '
            'xmlns="https://route53.amazonaws.com/doc/2013-04-01/">'
            "<ChangeBatch><Changes><Change>"
            f"<Action>{action}</Action><ResourceRecordSet>"
            f"<Name>{_xml_escape(fqdn)}</Name><Type>TXT</Type><TTL>{ttl}</TTL>"
            f"<ResourceRecords>{records}</ResourceRecords>"
            "</ResourceRecordSet></Change></Changes></ChangeBatch>"
            "</ChangeResourceRecordSetsRequest>"
        )

    async def create_txt(self, name: str, value: str) -> str:
        zone = await self._find_zone(name)
        fqdn = name.rstrip(".") + "."
        quoted = '"' + value.replace('"', '\\"') + '"'
        ttl, values = await self._get_rrset(zone["id"], fqdn)
        if quoted not in values:
            values.append(quoted)
        await self._call(
            "POST",
            f"/2013-04-01/hostedzone/{zone['id']}/rrset",
            body=self._change_body("UPSERT", fqdn, ttl, values),
        )
        return quoted

    async def delete_txt(self, name: str, record_id: str) -> None:
        if not record_id:
            return
        zone = await self._find_zone(name)
        fqdn = name.rstrip(".") + "."
        ttl, values = await self._get_rrset(zone["id"], fqdn)
        remain = [v for v in values if v != record_id]
        if remain:
            await self._call(
                "POST",
                f"/2013-04-01/hostedzone/{zone['id']}/rrset",
                body=self._change_body("UPSERT", fqdn, ttl, remain),
            )
        elif values:
            # 最后一个值：DELETE 必须与现有 RRSet 完全一致才能删掉
            await self._call(
                "POST",
                f"/2013-04-01/hostedzone/{zone['id']}/rrset",
                body=self._change_body("DELETE", fqdn, ttl, values),
            )


# ------------------------------------------------------------------ Namecheap
class NamecheapProvider(DnsProvider):
    """Namecheap：XML API，没有单条记录增删，只能 getHosts → 改 → setHosts。

    ``setHosts`` 是**全量覆盖**，所以每次都必须先读回全部记录再整体写回，任何
    一步失败都不会落库（接口是原子的）。官方要求把调用方 IP 加进白名单。
    """

    name = "namecheap"

    API = "https://api.namecheap.com/xml.response"

    def __init__(
        self, api_user: str, api_key: str, username: str = "", client_ip: str = ""
    ) -> None:
        if not api_user or not api_key:
            raise DnsError("Namecheap 需要 API User 与 API Key")
        self.api_user = api_user
        self.api_key = api_key
        self.username = username or api_user
        self.client_ip = client_ip

    async def _call(self, command: str, params: Dict[str, Any]) -> ET.Element:
        query: Dict[str, Any] = {
            "ApiUser": self.api_user,
            "ApiKey": self.api_key,
            "UserName": self.username,
            "Command": command,
            "ClientIp": self.client_ip,
        }
        query.update(params)
        async with _client() as client:
            resp = await client.get(self.API, params=query)
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as exc:
            raise DnsError("Namecheap 返回的不是合法 XML") from exc
        if root.attrib.get("Status", "").lower() != "ok":
            errors = [
                (node.text or "")
                for node in root.findall(".//{*}Error")
                if (node.text or "").strip()
            ]
            raise DnsError("Namecheap 请求失败：" + ("; ".join(errors)[:300] or "未知错误"))
        return root

    @staticmethod
    def _split_zone(zone: str) -> Tuple[str, str]:
        parts = str(zone or "").split(".", 1)
        return (parts[0], parts[1]) if len(parts) == 2 else (zone, "")

    async def _find_zone(self, name: str) -> Dict[str, str]:
        last: Optional[Exception] = None
        for candidate in zone_candidates(name):
            sld, tld = self._split_zone(candidate)
            if not sld or not tld:
                continue
            try:
                # 能读到 host 列表就说明这个注册域存在（否则返回 ERROR）
                await self._call(
                    "namecheap.domains.dns.getHosts", {"SLD": sld, "TLD": tld}
                )
                return {"sld": sld, "tld": tld, "zone": candidate}
            except DnsError as exc:
                last = exc
        raise DnsError(f"找不到 {name} 对应的 Namecheap 域名") from last

    async def _hosts(self, sld: str, tld: str) -> List[Dict[str, str]]:
        root = await self._call("namecheap.domains.dns.getHosts", {"SLD": sld, "TLD": tld})
        hosts: List[Dict[str, str]] = []
        for node in root.findall(".//{*}host"):
            hosts.append(
                {
                    "Name": node.attrib.get("Name", ""),
                    "Type": node.attrib.get("Type", ""),
                    "Address": node.attrib.get("Address", ""),
                    "TTL": node.attrib.get("TTL", "1800"),
                    "MXPref": node.attrib.get("MXPref", "10"),
                }
            )
        return hosts

    async def _set_hosts(
        self, sld: str, tld: str, hosts: List[Dict[str, str]]
    ) -> None:
        params: Dict[str, Any] = {"SLD": sld, "TLD": tld}
        for index, host in enumerate(hosts, 1):
            params[f"HostName{index}"] = host["Name"]
            params[f"RecordType{index}"] = host["Type"]
            params[f"Address{index}"] = host["Address"]
            params[f"TTL{index}"] = host["TTL"]
            if host["Type"] == "MX":
                params[f"MXPref{index}"] = host["MXPref"]
        await self._call("namecheap.domains.dns.setHosts", params)

    @staticmethod
    def _relative(name: str, zone: str) -> str:
        if name == zone:
            return "@"
        if name.endswith("." + zone):
            return name[: -(len(zone) + 1)]
        return name

    async def create_txt(self, name: str, value: str) -> str:
        zone = await self._find_zone(name)
        sub = self._relative(name, zone["zone"])
        hosts = await self._hosts(zone["sld"], zone["tld"])
        hosts.append(
            {"Name": sub, "Type": "TXT", "Address": value, "TTL": str(TXT_TTL), "MXPref": "10"}
        )
        await self._set_hosts(zone["sld"], zone["tld"], hosts)
        return value

    async def delete_txt(self, name: str, record_id: str) -> None:
        if not record_id:
            return
        zone = await self._find_zone(name)
        sub = self._relative(name, zone["zone"])
        hosts = await self._hosts(zone["sld"], zone["tld"])
        remain = [
            host
            for host in hosts
            if not (
                host["Name"] == sub
                and host["Type"] == "TXT"
                and host["Address"] == record_id
            )
        ]
        await self._set_hosts(zone["sld"], zone["tld"], remain)


# ------------------------------------------------------------------ 火山引擎
def _volc_headers(
    access_key: str,
    secret_key: str,
    region: str,
    method: str,
    path: str,
    query: str,
    body: bytes,
) -> Dict[str, str]:
    """火山引擎 OpenAPI 的 HMAC-SHA256 签名（与 AWS SigV4 同构，头名不同）。"""
    now = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    date = now[:8]
    host = "dns.volcengineapi.com"
    content_type = "application/json"
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{host}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{now}\n"
    )
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_request = "\n".join(
        [method, path, query, canonical_headers, signed_headers, payload_hash]
    )
    scope = f"{date}/{region}/DNS/request"
    string_to_sign = "\n".join(
        [
            "HMAC-SHA256",
            now,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _sign(secret_key.encode("utf-8"), date)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, "DNS")
    k_signing = _sign(k_service, "request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {
        "Authorization": (
            f"HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "Content-Type": content_type,
        "Host": host,
        "X-Content-Sha256": payload_hash,
        "X-Date": now,
    }


class VolcengineProvider(DnsProvider):
    """火山引擎云解析：Action 走 query，参数走 JSON body。"""

    name = "volcengine"

    HOST = "dns.volcengineapi.com"
    VERSION = "2022-02-01"

    def __init__(
        self, access_key: str, secret_key: str, region: str = "cn-beijing"
    ) -> None:
        if not access_key or not secret_key:
            raise DnsError("火山引擎需要 AccessKey 与 SecretKey")
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region or "cn-beijing"

    async def _call(
        self, action: str, payload: Optional[Dict[str, Any]] = None, method: str = "POST"
    ) -> Dict[str, Any]:
        params = {"Action": action, "Version": self.VERSION}
        query = "&".join(
            f"{quote(str(k), safe='')}={quote(str(v), safe='')}"
            for k, v in sorted(params.items())
        )
        body = (
            json.dumps(payload or {}, separators=(",", ":")).encode("utf-8")
            if method == "POST"
            else b""
        )
        headers = _volc_headers(
            self.access_key, self.secret_key, self.region, method, "/", query, body
        )
        url = "https://" + self.HOST + "/?" + query
        async with _client() as client:
            if method == "POST":
                resp = await client.post(url, content=body, headers=headers)
            else:
                resp = await client.get(url, headers=headers)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            meta = data.get("ResponseMetadata") or {}
            error = meta.get("Error") or data.get("Error") or {}
            raise DnsError(
                f"火山引擎 {action} 失败："
                + str(error.get("Message") or error.get("Code") or resp.status_code)
            )
        return data.get("Result") or {}

    async def _find_zone(self, name: str) -> Dict[str, str]:
        result = await self._call(
            "ListZones", {"PageNumber": 1, "PageSize": 100}, method="GET"
        )
        best: Optional[Dict[str, str]] = None
        for zone in result.get("Zones") or []:
            zname = str(zone.get("ZoneName") or "").rstrip(".")
            if zname and (name == zname or name.endswith("." + zname)):
                if best is None or len(zname) > len(best["name"]):
                    best = {"id": str(zone.get("ZID") or ""), "name": zname}
        if not best:
            raise DnsError(f"找不到 {name} 对应的火山引擎域名")
        return best

    async def create_txt(self, name: str, value: str) -> str:
        zone = await self._find_zone(name)
        host = name[: -(len(zone["name"]) + 1)] if name.endswith("." + zone["name"]) else name
        result = await self._call(
            "CreateRecord",
            {
                "ZID": zone["id"],
                "Host": host,
                "Type": "TXT",
                "Value": value,
                "TTL": TXT_TTL,
            },
        )
        return str(result.get("RecordID") or "")

    async def delete_txt(self, name: str, record_id: str) -> None:
        if not record_id:
            return
        zone = await self._find_zone(name)
        rid: Any = int(record_id) if str(record_id).isdigit() else record_id
        await self._call("DeleteRecord", {"ZID": zone["id"], "RecordID": rid})


# ------------------------------------------------------------------ 工厂
def build_provider(kind: str, cfg: Dict[str, Any]) -> DnsProvider:
    """按配置造一个 provider；``cfg`` 里的键就用各家界面上的字段名。"""
    kind = str(kind or "").strip().lower()
    data = cfg or {}
    if kind == "dnspod":
        return DnspodProvider(
            str(data.get("secret_id") or ""), str(data.get("secret_key") or "")
        )
    if kind == "aliyun":
        return AliyunProvider(
            str(data.get("access_key_id") or ""), str(data.get("access_key_secret") or "")
        )
    if kind == "huawei":
        return HuaweiProvider(
            str(data.get("access_key_id") or ""), str(data.get("access_key_secret") or "")
        )
    if kind == "cloudflare":
        return CloudflareProvider(str(data.get("api_token") or ""))
    if kind == "godaddy":
        return GodaddyProvider(
            str(data.get("api_key") or ""), str(data.get("api_secret") or "")
        )
    if kind == "route53":
        return Route53Provider(
            str(data.get("access_key_id") or ""),
            str(data.get("secret_access_key") or ""),
            str(data.get("region") or "us-east-1"),
        )
    if kind == "namecheap":
        return NamecheapProvider(
            str(data.get("api_user") or ""),
            str(data.get("api_key") or ""),
            str(data.get("username") or ""),
            str(data.get("client_ip") or ""),
        )
    if kind == "volcengine":
        return VolcengineProvider(
            str(data.get("access_key") or ""),
            str(data.get("secret_key") or ""),
            str(data.get("region") or "cn-beijing"),
        )
    raise DnsError("不支持的 DNS 服务商：" + (kind or "（未选择）"))
