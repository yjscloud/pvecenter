"""网站证书管理接口：腾讯云免费证书的申请、绑定、部署与续期。

按用户隔离：站点带归属（``username``），腾讯云密钥按用户分别存放，各自的证书在
各自的腾讯云账号下申请与部署。管理员可以**看到**全部站点（便于排障），但读改
一律以「自己的站点」为界 —— 证书的申请、部署、删除都只作用于本人名下的站点。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from .. import certflow_store, certs, dnsproviders, i18n, security, store
from ..acme import DIRECTORIES
from ..certproviders import (
    PROVIDERS as CLOUD_PROVIDERS,
    SECRET_FIELDS as PROVIDER_SECRET_FIELDS,
    ProviderError,
    label as provider_label,
    schema as provider_schema,
)
from ..config import settings
from ..schemas import (
    CertBulkRequest,
    CertExportRequest,
    CertRevokeRequest,
    CertRotateRequest,
)
from ..tencent_ssl import TencentCloudError

router = APIRouter(prefix="/api/certs", tags=["certs"])

CERT_VIEW = security.require_permission("cert.view")
CERT_MANAGE = security.require_permission("cert.manage")
#: 导出证书（含私钥）：独立权限。查看权 ≠ 取走私钥权，默认仅管理员。
CERT_EXPORT = security.require_permission("cert.export")


def _site_view(site: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """补充页面需要的派生字段。

    **所有凭据都只回传"是否已配置"**：SSH 口令 / 私钥、站点证书链与证书私钥
    都不经这个视图下发。证书私钥只能走显式的导出接口（``cert.export`` 权限 +
    审计），否则任何拿到 ``cert.view`` 的账号（含 viewer）调一次列表就能拿走
    全部站点的明文私钥。
    """
    view = dict(site)
    view["ssh_password_set"] = bool(site.get("ssh_password"))
    view["ssh_key_set"] = bool(site.get("ssh_key"))
    view["ssh_password"] = ""
    view["ssh_key"] = ""
    view["acme_pem_set"] = bool(str(site.get("acme_pem") or "").strip())
    view["acme_key_set"] = bool(str(site.get("acme_key") or "").strip())
    view["acme_pem"] = ""
    view["acme_key"] = ""
    left = certs.days_left(site)
    threshold = certs.renew_threshold(site, cfg)
    view["days_left"] = left
    view["renew_before_days_effective"] = threshold
    view["needs_renew"] = left is not None and left <= threshold
    # 站点当前用哪家厂商签发：页面只读展示，切换厂商不影响历史日志
    view["ca"] = certs.ca_kind(site)
    view["ca_label"] = i18n.tr(provider_label(view["ca"]))
    # 腾云状态与上一次错误是存库的中文字符串，出口按请求语言渲染：
    # 状态是固定枚举（tencent_ssl.STATUS_TEXT）一定命中；last_error 多为
    # 「前缀 + 动态值」，在生成处已用双语拼好，这里兜底整句命中的情况。
    view["cert_status_text"] = i18n.tr(str(view.get("cert_status_text") or ""))
    view["last_error"] = i18n.tr(str(view.get("last_error") or ""))
    return view


def _own(user: Optional[Dict[str, Any]]) -> str:
    """当前用户名。

    正常情况下拿到的都是用户字典；这里容错非字典入参 —— 曾经有调用点把
    ``security.visible_owner(user)``（管理员为 ``None``）当作 user 传进来，
    直接 ``user.get`` 炸成 500。现在退化成「归属为空」，最坏是查不到站点（404）。
    """
    if not isinstance(user, dict):
        return ""
    return str(user.get("username") or "")


# ------------------------------------------------------ 流水线步骤日志（SSE）
#: 轮询间隔。半秒足够让「写盘 → 重载 → 推送」这类连续动作看起来是实时的，
#: 又不至于给数据库压力 —— 单条运行的日志总量是个位数到十几条。
RUN_STREAM_POLL = 0.5
#: 连续这么多轮没有新日志就认为运行已结束（约 5 秒）。旧路径不写运行头，
#: 没有明确的结束信号，只能靠空闲判定收尾。
RUN_STREAM_IDLE = 10
#: 单条连接最长存活时间。运行卡住时不能把连接永久挂在面板上。
RUN_STREAM_TIMEOUT = 300.0


async def _visible_run(run_id: str, scope: Optional[str]) -> Optional[Dict[str, Any]]:
    """取一条当前用户可见的运行详情，不可见统一返回 None（调用方转 404）。

    可见来源有两种：运行头属于本人，或者**归因日志**属于本人。后者覆盖旧路径
    —— 开关关闭时不写 ``cert_flow_run``，但日志照样带 ``run_id``。
    """
    record = await certs.run_detail(run_id, owner=scope)
    if scope is None:
        return record
    head_owner = str(record.get("username") or "")
    if head_owner:
        return record if head_owner == scope else None
    # 没有运行头：有本人日志才认为「这条运行存在」
    return record if record.get("logs") else None


def _sse(payload: Dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False, default=str) + "\n\n"


def _acme_view(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """ACME 配置的对外视图：只说明「有没有配」，凭据一律不回传。"""
    view: Dict[str, Any] = {
        "email": cfg.get("email") or "",
        "directory": cfg.get("directory") or "",
        "encrypt_algo": cfg.get("encrypt_algo") or "RSA",
        "dns_provider": cfg.get("dns_provider") or "",
        "dns_wait": int(cfg.get("dns_wait") or 0),
        "eab_kid": cfg.get("eab_kid") or "",
        "eab_hmac_key": "",
        "eab_hmac_key_set": bool(cfg.get("eab_hmac_key")),
        "account_key": "",
        "account_key_set": bool(cfg.get("account_key")),
    }
    secret_set: Dict[str, Dict[str, bool]] = {}
    for name, fields in certs.ACME_SECRET_FIELDS.items():
        block = dict(cfg.get(name) or {})
        secret_set[name] = {}
        for field in fields:
            secret_set[name][field] = bool(block.get(field))
            block[field] = ""
        view[name] = block  # secret_id / access_key_id 这类非密文字段照常回显
    view["secret_set"] = secret_set
    return view


async def _cfg_for(site: Dict[str, Any]) -> Dict[str, Any]:
    """站点对应的厂商配置：只有云厂商才有密钥，ACME / 托管证书返回空表。

    别直接调 ``certs.provider_cfg_for``：``load_provider`` 只认云厂商，遇到
    ``ca="upload"`` 会抛「不支持的证书签发厂商」，而这个异常在路由里没人接，
    最终变成 500 —— 「上传第三方证书」整个入口一创建站点就挂。
    """
    kind = certs.ca_kind(site)
    if not certs.is_cloud_provider(kind):
        return {}
    return await certs.provider_cfg_for(site)


async def _views(sites: List[Dict[str, Any]], own: str) -> List[Dict[str, Any]]:
    """批量构造站点视图。

    「是否需要续期」跟着站点归属者在该**签发渠道**上的配置走：同一个用户可能给
    不同站点配了不同厂商，阈值也各自独立。缓存键是「归属 + 厂商」。
    """
    cache: Dict[tuple, Dict[str, Any]] = {}
    views: List[Dict[str, Any]] = []
    for site in sites:
        key = (certs.site_owner(site), certs.ca_kind(site))
        if key not in cache:
            cache[key] = await _cfg_for(site)
        views.append(_site_view(site, cache[key]))
    return views


async def _view(site: Dict[str, Any]) -> Dict[str, Any]:
    return _site_view(site, await _cfg_for(site))


def _provider_view(kind: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """厂商配置的对外视图：结构给前端渲染表单，密文只回「有没有配」。"""
    cfg = item.get("_cfg") or {}
    view: Dict[str, Any] = {
        "kind": kind,
        "label": i18n.tr(provider_label(kind)),
        "label_key": item.get("label_key") or "",
        "tagline": i18n.tr(item.get("tagline") or ""),
        "tagline_key": item.get("tagline_key") or "",
        "help_url": item.get("help_url") or "",
        "configured": bool(item.get("_configured")),
        "synchronous": bool(item.get("synchronous")),
        "fields": item.get("fields") or [],
        "values": {},
        "secret_set": {},
    }
    defaults = item.get("defaults") or {}
    for field in item.get("fields") or []:
        key = str(field.get("key") or "")
        if field.get("secret"):
            view["secret_set"][key] = bool(cfg.get(key))
            view["values"][key] = ""
        else:
            value = cfg.get(key, defaults.get(key, ""))
            view["values"][key] = value
    return view


async def _providers_view(owner: str) -> List[Dict[str, Any]]:
    """把已登记的厂商逐个包成页面需要的结构（含当前用户的配置）。"""
    result: List[Dict[str, Any]] = []
    for item in provider_schema():
        kind = str(item.get("kind") or "")
        cfg = await certs.load_provider(kind, owner)
        try:
            configured = bool(certs.build_provider(kind, cfg).configured)
        except ProviderError:
            configured = False
        data = dict(item)
        data["_cfg"] = cfg
        data["_configured"] = configured
        result.append(_provider_view(kind, data))
    # ACME 不是厂商，但对用户来说同样是「一条签发渠道」：并进同一份列表，
    # 页面就不必再单独维护一张 ACME 卡片
    result.append(certs.acme_channel_view(await certs.load_acme(owner)))
    return result


def _bad_request(exc: Exception) -> HTTPException:
    """把模块抛出的业务错误转成 400，并按请求语言翻译。

    证书模块的校验消息（域名、目录、SSH、部署方式……几十条）都集中在
    ``certs`` 里以中文抛出，在这里收口翻译比在每个 ``raise`` 处包 ``tr()``
    干净得多；腾讯云返回的英文报错查不到译表，原样透传，不受影响。
    """
    return HTTPException(status_code=400, detail=i18n.tr(str(exc)))


@router.get("")
async def get_certs(
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    own = _own(user)
    scope = security.visible_owner(user)
    cfg = await certs.load_tencent(own)
    safe = dict(cfg)
    secret_set = bool(safe.get("secret_key"))
    safe["secret_key"] = ""
    sites = await _views(certs.visible_sites(await certs.load_sites(), scope), own)
    sites.sort(key=lambda s: (0 if s.get("needs_renew") else 1, s.get("days_left") if s.get("days_left") is not None else 9999))
    # 清单与统计复用同一份派生结果，避免一次请求里重建两次库存快照
    inventory_items = await certs.inventory(scope)
    return {
        # 兼容旧前端：腾讯云的单独视图继续保留
        "tencent": safe,
        "secret_set": secret_set,
        "acme": _acme_view(await certs.load_acme(own)),
        # 统一抽象层：每家厂商一份 schema + 当前用户的配置视图（密文只回「有没有」）
        "providers": await _providers_view(own),
        "sites": sites,
        "templates": await certs.load_templates(own),
        "logs": await certs.logs(60, owner=scope),
        "inventory": inventory_items,
        "inventory_stats": certs.inventory_stats_from_items(inventory_items),
        "own_username": own,
        "is_admin": scope is None,
        # 容器内运行时「本机目录」写的是容器文件系统，页面据这条提示引导改用 SSH / 虚拟机
        "local_deploy_hint": certs.local_deploy_hint(),
        "options": {
            "dv_auth_methods": [
                {
                    "value": "DNS_AUTO",
                    "label": i18n.tr("DNS_AUTO（自动添加解析，域名需托管在 DNS 服务商）"),
                },
                {"value": "DNS", "label": i18n.tr("DNS（手动添加解析记录）")},
                {
                    "value": "FILE",
                    "label": i18n.tr("FILE（站点根目录放置验证文件，需海外 CA 可访问）"),
                },
            ],
            "encrypt_algos": [
                {"value": "RSA", "label": "RSA 2048"},
                {"value": "ECC", "label": "ECC prime256v1"},
            ],
            "deploy_methods": [
                {
                    "value": value,
                    "label": i18n.tr(certs.DEPLOY_METHOD_LABELS[value]),
                }
                for value in certs.DEPLOY_METHODS
            ],
            # 签发渠道 = 已登记的云厂商 + ACME + 托管证书；新增厂商自动出现在这里
            "cas": [
                {
                    "value": kind,
                    "label": i18n.tr(provider_label(kind)),
                }
                for kind in CLOUD_PROVIDERS
            ]
            + [
                {
                    "value": "acme",
                    "label": i18n.tr("ACME（Let's Encrypt / ZeroSSL，支持泛域名与多域名）"),
                },
                {
                    "value": "upload",
                    "label": i18n.tr("托管证书（自行上传 PEM / 私钥）"),
                },
            ],
            "provider_kinds": list(CLOUD_PROVIDERS),
            "acme_challenges": [
                {
                    "value": "dns",
                    "label": i18n.tr("DNS-01（自动加 TXT 记录，支持泛域名）"),
                },
                {
                    "value": "http",
                    "label": i18n.tr("HTTP-01（把验证文件写进 webroot）"),
                },
            ],
            "acme_directories": [
                {"value": value, "label": name} for name, value in DIRECTORIES
            ],
            "dns_providers": [
                {"value": key, "label": dnsproviders.PROVIDER_LABELS[key]}
                for key in dnsproviders.PROVIDERS
            ],
        },
    }


@router.get("/providers")
async def list_providers(
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    """已登记的签发厂商 + 当前用户的配置视图（凭据只回「有没有配」）。"""
    return {"providers": await _providers_view(_own(user))}


@router.put("/providers/{kind}")
async def save_provider_config(
    kind: str,
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    """保存某家厂商的凭据：只影响当前用户自己的账号配置。

    密文字段留空表示沿用旧值，因此页面上不必每次都把 Secret 回填出来。
    """
    kind = str(kind or "").strip().lower()
    try:
        if kind == "acme":
            await certs.save_acme(certs.acme_payload(payload or {}), _own(user))
        else:
            await certs.save_provider(kind, payload or {}, _own(user))
    except (certs.CertError, ProviderError) as exc:
        raise _bad_request(exc) from exc
    await security.audit(
        request,
        user,
        "cert.provider." + kind,
        "certs",
        "success",
        "更新证书厂商配置（" + kind + "，本人）",
    )
    providers = await _providers_view(_own(user))
    current = next((item for item in providers if item["kind"] == kind), None)
    return {"provider": current, "providers": providers}


@router.post("/providers/{kind}/test")
async def test_provider(
    kind: str,
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    """验证当前用户在某家厂商上的密钥是否可用。"""
    kind = str(kind or "").strip().lower()
    try:
        result = await certs.test_connection(kind, _own(user))
    except (certs.CertError, ProviderError, TencentCloudError) as exc:
        await security.audit(
            request, user, "cert.provider.test", "certs", "failed", str(exc)
        )
        raise _bad_request(exc) from exc
    await security.audit(
        request, user, "cert.provider.test", "certs", "success", str(result.get("detail") or "")
    )
    return {**result, "provider": kind}


@router.get("/providers/{kind}/certificates")
async def list_provider_certificates(
    kind: str,
    search: str = "",
    limit: int = 100,
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    """列出某家厂商账号下的证书，供页面选择绑定。"""
    kind = str(kind or "").strip().lower()
    try:
        items = await certs.list_remote_certificates(
            kind, _own(user), search=search, limit=limit
        )
    except (certs.CertError, ProviderError, TencentCloudError) as exc:
        raise _bad_request(exc) from exc
    return {"certificates": items, "count": len(items), "provider": kind}


@router.put("/tencent")
async def save_tencent_config(
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    """兼容旧入口：等价于 ``PUT /providers/tencent``。"""
    try:
        cfg = await certs.save_provider("tencent", payload or {}, _own(user))
    except certs.CertError as exc:
        raise _bad_request(exc) from exc
    await security.audit(
        request, user, "cert.tencent", "certs", "success", "更新腾讯云证书配置（本人）"
    )
    safe = dict(cfg)
    safe["secret_key"] = ""
    return {"tencent": safe, "secret_set": bool(cfg.get("secret_key"))}


@router.put("/acme")
async def save_acme_config(
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    """保存 ACME 配置（邮箱、目录、DNS 服务商凭据），只影响当前用户。"""
    try:
        cfg = await certs.save_acme(payload or {}, _own(user))
    except certs.CertError as exc:
        raise _bad_request(exc) from exc
    await security.audit(
        request, user, "cert.acme", "certs", "success", "更新 ACME 证书配置（本人）"
    )
    return {"acme": _acme_view(cfg)}


@router.post("/tencent/test")
async def test_tencent(
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    """兼容旧入口：等价于 ``POST /providers/tencent/test``。"""
    return await test_provider("tencent", request, user)


@router.get("/tencent/list")
async def list_remote(
    search: str = "",
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    """兼容旧入口：等价于 ``GET /providers/tencent/certificates``。"""
    return await list_provider_certificates("tencent", search, 100, user)


# --------------------------------------------------------------- 密钥轮换收尾
@router.post("/keys/rewrap")
async def rewrap_secret_keys(
    request: Request,
    user: Dict[str, Any] = Depends(security.require_admin()),
) -> Dict[str, Any]:
    """用当前 ``SECRET_KEY`` 重写证书模块内的全部密文。

    轮换根密钥的流程：先把旧密钥填进 ``SECRET_KEY_PREVIOUS`` 重启（历史密文
    仍可解密），再调用本接口，最后清空 ``SECRET_KEY_PREVIOUS``。任一段密文
    解不开时整体中止并报错，不会把「解不开」写成空串。
    """
    try:
        counts = await certs.rewrap_secrets()
        # 证书模块之外的密文（PVE 连接凭据、SMTP 口令、Webhook 等）一起重写：
        # 只换一半，等于把「换根密钥」的故障推迟到下次用配置时才爆发。
        counts.update(await store.rewrap_all_settings())
    except certs.CertError as exc:
        await security.audit(
            request, user, "cert.keys.rewrap", "certs", "failed", str(exc)
        )
        raise _bad_request(exc) from exc
    await security.audit(request, user, "cert.keys.rewrap", "certs", "success", counts)
    return {"rewrapped": counts}


# ------------------------------------------------------------ 证书清单（多站点）
@router.get("/stats")
async def get_inventory_stats(
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    """证书到期分布与渠道/状态统计（页面统计卡与到期日历用）。"""
    return await certs.inventory_stats(security.visible_owner(user))


@router.get("/inventory")
async def get_inventory(
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    """站点证书清单：按「厂商 + 证书 ID」聚合，能看到一张证书绑了哪些站点。"""
    items = await certs.inventory(security.visible_owner(user))
    return {"items": items, "count": len(items)}


@router.post("/inventory/bind")
async def bind_inventory(
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    """把某家厂商账号下的一张证书一次绑到多个站点上。

    绑定同时把站点的签发渠道切到该厂商；证书覆盖不到域名的站点会被跳过并给出
    原因（不会让整批失败）。
    """
    body = payload or {}
    own = _own(user)
    kind = str(body.get("provider") or "tencent").strip().lower()
    site_ids = [str(item) for item in body.get("site_ids") or []]
    try:
        result = await certs.bind_to_sites(
            kind, str(body.get("cert_id") or ""), site_ids, owner=own
        )
    except (certs.CertError, ProviderError, TencentCloudError) as exc:
        await security.audit(
            request, user, "cert.inventory.bind", kind, "failed", str(exc)
        )
        raise _bad_request(exc) from exc
    await security.audit(
        request,
        user,
        "cert.inventory.bind",
        str(body.get("cert_id") or ""),
        "success",
        str(result.get("detail") or ""),
    )
    return {
        **result,
        "inventory": await certs.inventory(security.visible_owner(user)),
    }


# ------------------------------------------------------------------ 站点管理
@router.post("/sites")
async def create_site(
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    try:
        site = certs.normalise_site(payload)
    except certs.CertError as exc:
        raise _bad_request(exc) from exc
    # 新站点归创建者所有；归属不允许由请求体指定
    site["username"] = _own(user)
    cfg = await _cfg_for(site)
    await certs.update_site(site)
    await security.audit(
        request, user, "cert.site.create", site["domain"], "success", "新增证书站点 " + site["name"]
    )
    return {"site": _site_view(site, cfg)}


@router.put("/sites/{site_id}")
async def update_site(
    site_id: str,
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    scope = _own(user)
    existing = await certs.get_site(site_id, scope)
    if not existing:
        raise HTTPException(status_code=404, detail=i18n.tr("站点不存在"))
    try:
        site = certs.normalise_site(payload, existing)
    except certs.CertError as exc:
        raise _bad_request(exc) from exc
    site["id"] = existing["id"]
    cfg = await _cfg_for(site)
    await certs.update_site(site)
    await security.audit(
        request, user, "cert.site.update", site["domain"], "success", "更新证书站点 " + site["name"]
    )
    return {"site": _site_view(site, cfg)}


@router.delete("/sites/{site_id}")
async def remove_site(
    site_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    # 只能删自己的站点：管理员能看见别人的，但不能替他删
    existing = await certs.get_site(site_id, _own(user))
    if not existing:
        raise HTTPException(status_code=404, detail=i18n.tr("站点不存在"))
    await certs.delete_site(site_id)
    await security.audit(
        request, user, "cert.site.delete", existing.get("domain", ""), "success",
        "删除证书站点 " + str(existing.get("name") or ""),
    )
    return {
        "removed": True,
        "detail": i18n.tr("站点已删除（已部署的证书文件未做改动）"),
    }


async def _require_site(site_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
    """取自己的站点：站点变更一律只作用于本人（管理员也只读他人的）。"""
    site = await certs.get_site(site_id, _own(user))
    if not site:
        raise HTTPException(status_code=404, detail=i18n.tr("站点不存在"))
    return site


@router.post("/sites/{site_id}/apply")
async def apply_site_cert(
    site_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
    payload: Dict[str, Any] = Body(default_factory=dict),
) -> Dict[str, Any]:
    """申请免费证书；已到期或即将到期时自动走续期流程。"""
    own = _own(user)
    site = await _require_site(site_id, user)
    body = payload or {}
    if body.get("domain"):
        try:
            site["domain"] = certs.normalise_domain(body["domain"])
        except certs.CertError as exc:
            raise _bad_request(exc) from exc
        # 云厂商的免费 DV 证书都不支持泛域名：在提交前拦下，别等对面报错
        if certs.is_cloud_provider(certs.ca_kind(site)) and site["domain"].startswith("*."):
            raise HTTPException(
                status_code=400,
                detail=i18n.tr("该渠道的免费证书不支持泛域名，请把站点改成 ACME 渠道"),
            )
    renew = bool(site.get("cert_id"))
    try:
        # 走「一步运行」：页面上的续期流水线立刻能看到这次申请，而不是只看到
        # 上一轮定时任务的旧结果
        result = await certs.run_manual(
            site, "issue", lambda: certs.apply_certificate(site, renew=renew)
        )
    except certs.CertError as exc:
        raise _bad_request(exc) from exc
    except TencentCloudError as exc:
        await security.audit(request, user, "cert.apply", site["domain"], "failed", str(exc))
        raise _bad_request(exc) from exc
    await security.audit(
        request, user, "cert.apply", site["domain"], "success", result["detail"]
    )
    fresh = await certs.get_site(site_id, own) or site
    return {**result, "site": await _view(fresh)}


@router.post("/sites/{site_id}/deploy")
async def deploy_site_cert(
    site_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    """立即下载证书并写入部署目录。"""
    own = _own(user)
    site = await _require_site(site_id, user)
    action = "renew" if site.get("pending_cert_id") else "deploy"
    try:
        outcome = await certs.run_manual(
            site, "deploy", lambda: certs.deploy_site(site, action=action)
        )
    except certs.CertError as exc:
        await certs.record_log(site, action, "failed", str(exc))
        site["last_error"] = str(exc)
        await certs.update_site(site)
        await security.audit(request, user, "cert.deploy", site["domain"], "failed", str(exc))
        raise _bad_request(exc) from exc
    except TencentCloudError as exc:
        await certs.record_log(site, action, "failed", str(exc))
        site["last_error"] = str(exc)
        await certs.update_site(site)
        await security.audit(request, user, "cert.deploy", site["domain"], "failed", str(exc))
        raise _bad_request(exc) from exc
    await security.audit(
        request, user, "cert.deploy", site["domain"], "success", str(outcome.get("detail"))
    )
    fresh = await certs.get_site(site_id, own) or site
    return {**outcome, "site": await _view(fresh)}


@router.post("/sites/{site_id}/sync")
async def sync_site(
    site_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    """从腾讯云同步该站点证书的状态与到期时间（用站点归属者的密钥）。"""
    own = _own(user)
    site = await _require_site(site_id, user)
    if not (site.get("cert_id") or site.get("pending_cert_id")):
        raise HTTPException(status_code=400, detail="该站点还没有绑定证书")
    if certs.ca_kind(site) in ("acme", "upload"):
        # ACME / 托管证书在签发或上传时就把证书落到站点上了，没有「远端状态」这一说
        raise HTTPException(
            status_code=400,
            detail=i18n.tr("该站点的证书已保存在本地，没有远端状态可同步"),
        )
    cfg = await certs.provider_cfg_for(site)
    try:
        # 包成一步运行：与定时续期里的 sync 步骤同名，页面上共用同一份文案
        await certs.run_manual(site, "sync", lambda: certs.sync_site_cert(site))
    except (certs.CertError, TencentCloudError) as exc:
        raise _bad_request(exc) from exc
    await certs.update_site(site)
    await security.audit(request, user, "cert.sync", site["domain"], "success", "同步证书状态")
    fresh = await certs.get_site(site_id, own) or site
    return {"site": _site_view(fresh, cfg)}


@router.post("/sites/{site_id}/bind")
async def bind_site_cert(
    site_id: str,
    payload: Dict[str, Any],
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    """绑定归属者某家厂商账号下已有的证书（不重新申请）。

    请求体可带 ``provider``（``tencent`` / ``aliyun`` / ``huawei``）：带上就把站点
    的签发渠道一并切到这家厂商，之后续期与下载都走它。
    """
    own = _own(user)
    site = await _require_site(site_id, user)
    body = payload or {}
    try:
        result = await certs.bind_certificate(
            site,
            str(body.get("cert_id") or ""),
            kind=str(body.get("provider") or "") or None,
        )
    except (certs.CertError, ProviderError, TencentCloudError) as exc:
        raise _bad_request(exc) from exc
    await security.audit(
        request, user, "cert.bind", site["domain"], "success", str(result.get("detail"))
    )
    fresh = await certs.get_site(site_id, own) or site
    return {**result, "site": await _view(fresh)}


@router.post("/sites/{site_id}/certificate")
async def import_site_certificate(
    site_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
    payload: Dict[str, Any] = Body(default_factory=dict),
) -> Dict[str, Any]:
    """托管一张外部证书：校验 PEM/KEY 配对、域名覆盖与到期时间后落库。"""
    own = _own(user)
    site = await _require_site(site_id, user)
    body = payload or {}
    try:
        result = await certs.import_certificate(
            site, str(body.get("pem") or ""), str(body.get("key") or "")
        )
    except certs.CertError as exc:
        await security.audit(request, user, "cert.import", site["domain"], "failed", str(exc))
        raise _bad_request(exc) from exc
    await security.audit(
        request, user, "cert.import", site["domain"], "success", str(result.get("detail"))
    )
    fresh = await certs.get_site(site_id, own) or site
    return {**result, "site": await _view(fresh)}


def _export_response(result: Dict[str, Any]) -> Response:
    from urllib.parse import quote as _quote

    filename = _quote(str(result["filename"]))
    return Response(
        content=result["content"],
        media_type=str(result["media_type"]),
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            # 含私钥的响应不允许任何中间层缓存
            "Cache-Control": "no-store",
        },
    )


@router.get("/sites/{site_id}/download")
async def download_site_certificate(
    request: Request,
    site_id: str,
    fmt: str = Query("pem", alias="format"),
    user: Dict[str, Any] = Depends(CERT_EXPORT),
) -> Response:
    """导出 PEM（zip）。

    私钥属于最高敏感数据，因此：独立权限 ``cert.export``（``cert.view`` 不再
    够用）；每次导出写审计；GET 只允许 PEM，PFX 口令必须走 POST body，不能
    出现在 URL（URL 会进访问日志、浏览器历史与反代日志）。
    """
    site = await _require_site(site_id, user)
    if fmt.lower() != "pem":
        raise HTTPException(
            status_code=400,
            detail=i18n.tr("PFX 导出请使用 POST /certs/sites/{id}/export，口令不能放在 URL 里"),
        )
    try:
        result = certs.export_certificate(site, "pem", "")
    except certs.CertError as exc:
        raise _bad_request(exc) from exc
    await security.audit_read(
        request,
        user,
        "cert.export",
        site.get("domain", ""),
        {"site_id": site_id, "format": "pem", "with_key": True},
    )
    return _export_response(result)


@router.post("/sites/{site_id}/export")
async def export_site_certificate(
    request: Request,
    site_id: str,
    payload: CertExportRequest,
    user: Dict[str, Any] = Depends(CERT_EXPORT),
) -> Response:
    """导出证书（PEM / PFX），PFX 口令放在请求体里。"""
    site = await _require_site(site_id, user)
    fmt = payload.format
    password = payload.password
    try:
        result = certs.export_certificate(site, fmt, password)
    except certs.CertError as exc:
        raise _bad_request(exc) from exc
    await security.audit_read(
        request,
        user,
        "cert.export",
        site.get("domain", ""),
        {"site_id": site_id, "format": fmt, "with_key": True},
    )
    return _export_response(result)


@router.post("/sites/{site_id}/revoke")
async def revoke_site_certificate(
    request: Request,
    site_id: str,
    payload: Optional[CertRevokeRequest] = None,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    """主动吊销站点当前证书（ACME revoke / 云厂商删除 / 本地标记）。

    吊销是**不可逆**操作：通知 CA、销毁面板内的证书链与私钥、关闭自动续期。
    成功与失败都记审计（含原因码与 CA 侧回查结果）。
    """
    site = await _require_site(site_id, user)
    reason = payload.reason if payload else 0
    try:
        result = await certs.revoke_certificate(site, reason)
    except certs.CertError as exc:
        await security.audit(
            request,
            user,
            "cert.revoke",
            site.get("domain", ""),
            "failed",
            {"site_id": site_id, "reason": reason, "error": str(exc)},
        )
        raise _bad_request(exc) from exc
    await security.audit(
        request,
        user,
        "cert.revoke",
        site.get("domain", ""),
        "success",
        {
            "site_id": site_id,
            "cert_id": result.get("cert_id", ""),
            "reason": result.get("reason", reason),
            "verified": bool(result.get("verified")),
        },
    )
    fresh = await certs.get_site(site_id, _own(user)) or site
    return {**result, "site": await _view(fresh)}


# ------------------------------------------------------------------ 证书详情
@router.get("/sites/{site_id}/detail")
async def get_site_detail(
    site_id: str,
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    """证书详情：链、SAN、指纹、签发者、密钥参数 + 线上 TLS 体检结论。

    只读，不含私钥；云厂商缓存为空时会尝试回源下载一次（失败降级为元数据）。
    """
    site = await _require_site(site_id, user)
    return await certs.cert_detail(site)


# ------------------------------------------------------------------ 主动轮换
@router.post("/sites/{site_id}/rotate")
async def rotate_site_certificate(
    request: Request,
    site_id: str,
    payload: Optional[CertRotateRequest] = None,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    """主动轮换：不等到期就重新签发并换新私钥（托管证书会被拒绝）。"""
    site = await _require_site(site_id, user)
    deploy = payload.deploy if payload else True
    try:
        result = await certs.rotate_site(site, None, deploy=deploy)
    except (certs.CertError, ProviderError, TencentCloudError) as exc:
        await security.audit(
            request, user, "cert.rotate", site.get("domain", ""), "failed", str(exc)
        )
        raise _bad_request(exc) from exc
    await security.audit(
        request,
        user,
        "cert.rotate",
        site.get("domain", ""),
        "success",
        {
            "site_id": site_id,
            "cert_id": str(site.get("cert_id") or ""),
            "pending_cert_id": str(site.get("pending_cert_id") or ""),
            "deployed": bool(result.get("deployed")),
        },
    )
    fresh = await certs.get_site(site_id, _own(user)) or site
    return {**result, "site": await _view(fresh)}


# ------------------------------------------------------------------ 批量操作
@router.post("/sites/bulk")
async def bulk_site_action(
    request: Request,
    payload: CertBulkRequest,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    """一次对多个站点执行同一动作；逐站点返回结果，单点失败不影响其它站点。

    只作用于「自己的站点」：别人的 ID 混进来会作为失败项返回，而不是整批 404 ——
    批量操作里一个越权 ID 不该让另外 19 个合法站点白跑。
    """
    own = _own(user)
    scope = security.visible_owner(user)
    all_sites = certs.visible_sites(await certs.load_sites(), scope)
    by_id = {str(item.get("id")): item for item in all_sites}
    sites: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for site_id in payload.site_ids:
        site = by_id.get(str(site_id))
        if site is None or certs.site_owner(site) != own:
            skipped.append(
                {
                    "site_id": str(site_id),
                    "ok": False,
                    "detail": i18n.tr("站点不存在或无权限"),
                }
            )
            continue
        sites.append(site)

    result = await certs.bulk_action(sites, payload.action, payload.reason)
    if skipped:
        result["results"].extend(skipped)
        result["failed"] += len(skipped)
        result["total"] += len(skipped)
    await security.audit(
        request,
        user,
        "cert.bulk",
        payload.action,
        "success" if result["failed"] == 0 else "partial",
        {
            "action": payload.action,
            "total": result["total"],
            "ok": result["ok"],
            "failed": result["failed"],
        },
    )
    return result


# ------------------------------------------------------------ Prometheus 指标
@router.get("/metrics")
async def cert_prometheus_metrics(
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Response:
    """证书指标（Prometheus 文本格式）。

    权限与页面一致（``cert.view``）：管理员拿到全量，普通用户只拿到自己的站点。
    指标里只有站点 ID / 域名 / 渠道这类非敏感标签，不含任何私钥或凭据。
    """
    scope = security.visible_owner(user)
    sites = certs.visible_sites(await certs.load_sites(), scope)
    return Response(
        content=certs.metrics_text(sites),
        media_type="text/plain; version=0.0.4; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


# ------------------------------------------------------------------ 站点模板
@router.get("/templates")
async def list_templates(
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    """当前用户的站点模板（部署方式 / 目录 / 续期策略，不含凭据）。"""
    return {"templates": await certs.load_templates(_own(user))}


@router.post("/templates")
async def create_template(
    request: Request,
    payload: Dict[str, Any] = Body(default_factory=dict),
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    """保存模板：传 ``site_id`` 就从该站点复制，否则按请求体里的字段存。"""
    own = _own(user)
    body = payload or {}
    site = None
    if body.get("site_id"):
        site = await certs.get_site(str(body["site_id"]), own)
        if not site:
            raise HTTPException(status_code=404, detail=i18n.tr("站点不存在"))
    if site is None and not str(body.get("deploy_dir") or "").strip():
        raise HTTPException(
            status_code=400,
            detail=i18n.tr("请指定要复制的站点，或至少填写部署目录"),
        )
    try:
        template = await certs.save_template(body, own, site)
    except certs.CertError as exc:
        raise _bad_request(exc) from exc
    await security.audit(
        request, user, "cert.template", template.get("name", ""), "success", "保存站点模板"
    )
    return {"template": template}


@router.delete("/templates/{template_id}")
async def remove_template(
    template_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    if not await certs.delete_template(template_id, _own(user)):
        raise HTTPException(status_code=404, detail=i18n.tr("模板不存在"))
    await security.audit(request, user, "cert.template", template_id, "success", "删除站点模板")
    return {"removed": True}


@router.post("/sync")
async def sync_all(
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    """同步自己名下站点的证书状态（管理员也不代他人同步）。"""
    own = _own(user)
    try:
        sites = await certs.sync_all(own)
    except certs.CertError as exc:
        raise _bad_request(exc) from exc
    except TencentCloudError as exc:
        raise _bad_request(exc) from exc
    await security.audit(request, user, "cert.sync", "certs", "success", "同步证书状态")
    return {"sites": await _views(sites, own)}


@router.get("/runs")
async def list_flow_runs(
    site_id: str = "",
    limit: int = 50,
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    """流水运行记录（certflow 引擎）。

    只读旁路：``certflow_enabled`` 关闭时这些表是空的，返回空列表而不是报错 ——
    前端可以无条件请求，用 ``engine_enabled`` 决定是否展示「运行历史」入口。
    """
    own = _own(user)
    scope = security.visible_owner(user)
    items = await certflow_store.list_runs(
        scope, site_id=site_id, limit=max(1, min(200, limit))
    )
    # 归属兜底：管理员 scope=None 能看全部，普通用户只可能是自己那批
    if scope is not None:
        items = [r for r in items if str(r.get("username") or "") == own]
    return {"items": items, "engine_enabled": bool(settings.certflow_enabled)}


@router.get("/runs/{run_id}")
async def get_flow_run(
    run_id: str,
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    """单次运行详情：运行头 + 逐步状态 + 产物索引 + 每一步的部署日志。

    运行头是**可选**的：开关关闭时旧调度器不写 ``cert_flow_run``，但部署日志
    依然按 ``run_id``/``node_id`` 归因。所以这里始终返回 ``logs`` 与 ``groups``，
    前端不必区分「新引擎 / 旧路径」两套渲染。
    """
    record = await _visible_run(run_id, security.visible_owner(user))
    if record is None:
        raise HTTPException(status_code=404, detail="run not found")
    return record


@router.get("/runs/{run_id}/logs")
async def get_flow_run_logs(
    run_id: str,
    after: int = 0,
    node_id: str = "",
    limit: int = 200,
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> Dict[str, Any]:
    """按游标增量拉取某次运行的部署日志（SSE 不可用时的兜底）。

    前端把上次返回的 ``cursor`` 作为 ``after`` 传回来即可，不必记住时间戳 ——
    同一秒内的多条日志靠自增主键定序，不会漏也不会重。
    """
    scope = security.visible_owner(user)
    if await _visible_run(run_id, scope) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return await certs.run_logs(
        run_id,
        node_id=node_id,
        after_id=after,
        limit=max(1, min(500, limit)),
        owner=scope,
    )


@router.get("/runs/{run_id}/stream")
async def stream_flow_run(
    run_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(CERT_VIEW),
) -> StreamingResponse:
    """某次运行的实时进度（SSE）：步骤日志边产生边推。

    事件序列：

    * ``snapshot`` —— 建连时先给一份全量（运行头 + 步骤 + 已有日志），前端据此
      渲染首屏，不必先发一次普通请求；
    * ``logs`` —— 增量日志，附带 ``cursor``；
    * ``done`` —— 运行结束（``reason=finished``）、长时间无新日志（``idle``）或
      超时（``timeout``），发完即关闭连接。

    为什么内部是「按游标轮询数据库」而不是进程内事件总线：uvicorn 多 worker 下，
    跑流水线的进程和接 SSE 的进程通常不是同一个；数据库是唯一共享状态，按主键
    增量读既能跨进程，也天然保序（同一秒的多条日志不会乱序）。
    """
    scope = security.visible_owner(user)
    if await _visible_run(run_id, scope) is None:
        raise HTTPException(status_code=404, detail="run not found")

    async def gen() -> AsyncIterator[str]:
        snapshot = await certs.run_detail(run_id, owner=scope)
        cursor = max([int(r["id"]) for r in snapshot.get("logs") or []] or [0])
        yield _sse({"type": "snapshot", "run": snapshot})

        # 旧路径不写运行头，「结束」只能靠空闲判定：连续若干轮没有新日志就收尾。
        idle = 0
        deadline = time.monotonic() + RUN_STREAM_TIMEOUT
        while True:
            if await request.is_disconnected():
                return
            await asyncio.sleep(RUN_STREAM_POLL)
            fresh = await certs.run_logs(run_id, after_id=cursor, owner=scope)
            if fresh["items"]:
                cursor = fresh["cursor"]
                idle = 0
                yield _sse({"type": "logs", "items": fresh["items"], "cursor": cursor})
            else:
                idle += 1

            head = await certflow_store.get_run(run_id)
            if head is not None and str(head.get("status") or "") not in ("", "running"):
                yield _sse(
                    {"type": "done", "reason": "finished", "status": head.get("status")}
                )
                return
            if head is None and idle >= RUN_STREAM_IDLE:
                yield _sse({"type": "done", "reason": "idle"})
                return
            if time.monotonic() > deadline:
                yield _sse({"type": "done", "reason": "timeout"})
                return

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # 面板前面有 nginx：不关缓冲事件会被攒着一起发，流式就没意义了
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/logs")
async def clear_logs(
    request: Request,
    user: Dict[str, Any] = Depends(CERT_MANAGE),
) -> Dict[str, Any]:
    removed = await certs.clear_logs(security.visible_owner(user))
    await security.audit(
        request, user, "cert.logs.clear", "certs", "success", "清空证书部署日志 " + str(removed) + " 条"
    )
    return {"removed": removed, "detail": "已清除 " + str(removed) + " 条日志"}
