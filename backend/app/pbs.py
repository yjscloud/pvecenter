"""Proxmox Backup Server（PBS）只读客户端。

## 为什么单独一个模块，而不是复用 :mod:`app.pve`

PVE 和 PBS 的 API 形状**相似但不等价**：

* 认证头不同 —— PVE 是 ``PVEAPIToken=<id>=<secret>``，PBS 是
  ``PBSAPIToken=<id>:<secret>``（**冒号**而非第二个等号）。照抄 PVE 的写法，
  PBS 会把整串当成 token 名，回一个不带任何说明的 401；
* 路径不同 —— PBS 的仓库在 ``/admin/datastore``，任务与节点在 ``/nodes``；
* 语义不同 —— PBS 的“节点”就是它自己，没有集群概念。

共用一套 ``ProxmoxError`` / SSL 上下文，但不共用连接对象：把两套凭据塞进同一个
类型里，迟早会出现「拿 PVE 的 token 去调 PBS」这种难查的 401。

## 只读

这里**只实现 GET**。PBS 的 prune / GC / verify / sync 都是破坏性或高负载操作，
面板不代劳 —— 需要时让用户去 PBS 自己的控制台。原因写在
``docs`` 与提交记录里：面板持有的 token 应当只授 ``Datastore.Audit`` /
``Sys.Audit``，能力边界靠凭据本身兜住，而不只靠代码里“记得别写”。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

import httpx

from . import store
from .config import settings
from .pve import ProxmoxError, build_ssl_context

logger = logging.getLogger(__name__)

#: PBS 连接配置在 settings 表里的键（与 PVE 的 ``pve_connections`` 并列）。
PBS_CONNECTIONS_KEY = "pbs_connections"

#: 与 PVE 连接共用同一组密钥字段名，这样 :mod:`app.store` 的加解密边界可以直接
#: 复用：拿到 token_secret 就等于能以面板身份调 PBS API，不能明文落库。
SECRET_FIELDS = store.SECRET_CONN_FIELDS


#: `generate-token` 的输出是给人看的几行文本，复制时很容易把前缀一起带进来。
#: 这类污染不会报错，只会被 PBS 判成 401 —— 属于“看着填对了但就是不通过”。
_SECRET_PREFIXES = (
    "token secret:",
    "tokensecret:",
    "secret:",
    "token secret",
)

#: `proxmox-backup-manager user generate-token` 的输出形如
#: ``Result: { "tokenid": "...", "value": "..." }`` —— 注意字段名是 **value**，
#: 不是 secret。整段粘贴在这里非常常见，先把它抽出来再谈别的。
_JSON_SECRET_RE = re.compile(
    r'"(?:value|secret)"\s*:\s*"([^"]+)"', re.IGNORECASE
)


def clean_secret(raw: str) -> str:
    """清掉复制 token secret 时常见的那几类噪声。

    只做「显然不是你本意」的清理，不去猜内容：

    * 整段 `Result: {...}` / `{"value": "..."}` 粘贴 → 取 `value` / `secret`；
    * 两端空白、包裹的引号；
    * ``Token secret: xxx`` 这种带标签的整行；
    * ``pvecenter@pbs!readonly=xxx`` 整段粘贴（取最后一个 ``=`` 之后）；
    * 多行 / 含空格时取最后一个词。
    """
    value = (raw or "").strip()
    # 先按 JSON 形态抽取值：这一步要在去掉首尾引号之前做，否则
    # `"value": "abc"` 会被 strip 破坏成 `value": "abc`
    match = _JSON_SECRET_RE.search(value)
    if match:
        return match.group(1).strip()
    value = value.strip("'\"")
    lowered = value.lower()
    for prefix in _SECRET_PREFIXES:
        if lowered.startswith(prefix):
            value = value[len(prefix):].strip().strip("'\"")
            break
    # `tokenid=secret` / `tokenid:secret` 整段粘贴：左边像 token id 才拆，
    # 避免误伤 base64 里的 `=`
    for sep_char in ("=", ":"):
        head, sep, tail = value.rpartition(sep_char)
        if sep and "!" in head and "@" in head:
            value = tail.strip()
            break
    if any(ch.isspace() for ch in value):
        parts = value.split()
        if parts:
            value = parts[-1]
    return value.strip()


class PbsConnection:
    """一条 PBS 连接的解析结果。"""

    def __init__(
        self,
        host: str = "",
        port: int = 8007,
        token_id: str = "",
        token_secret: str = "",
        verify_ssl: Optional[bool] = None,
        ca_cert: str = "",
        name: str = "",
        region: str = "",
        node: str = "",
        conn_id: str = "",
    ) -> None:
        self.id = conn_id
        self.name = name
        # 区域 / 机房：与 PVE 连接的 region 同一语义 —— 纯界面分组标签，
        # 留空 = 「未分区」；不参与连接逻辑，也不需要迁移老记录
        self.region = (region or "").strip()
        self.host = (host or "").strip()
        try:
            self.port = int(port or 8007)
        except (TypeError, ValueError):
            self.port = 8007
        self.token_id = (token_id or "").strip().strip("'\"")
        # secret 必须清洗：从终端复制 `generate-token` 的输出时，尾部空白、
        # `Token secret:` 前缀、整行文本都可能被一起带进来。带噪声的 header
        # 不会报错，只会被 PBS 判成 401 —— 那是最难查的一类“我明明填对了”。
        self.token_secret = clean_secret(token_secret)
        # 与 PVE 同一套默认策略：跟随 PVE_VERIFY_SSL（代码默认 False，自签开箱可用），
        # 显式传值才覆盖；要安全性用下面的 ca_cert 做钉扎
        self.verify_ssl = (
            settings.pve_verify_ssl if verify_ssl is None else bool(verify_ssl)
        )
        # 钉扎用的证书 / CA PEM（公开材料，不需要加密）
        self.ca_cert = ca_cert or ""
        self.node = (node or "").strip()

    @classmethod
    def from_dict(cls, raw: Any) -> "PbsConnection":
        item = raw if isinstance(raw, dict) else {}
        return cls(
            host=str(item.get("host") or ""),
            port=item.get("port") or 8007,
            token_id=str(item.get("token_id") or ""),
            token_secret=str(item.get("token_secret") or ""),
            verify_ssl=item.get("verify_ssl"),
            ca_cert=str(item.get("ca_cert") or ""),
            name=str(item.get("name") or ""),
            # 老记录没有这个键：`item.get(...) or ""` 即兜底，无需迁移
            region=str(item.get("region") or ""),
            node=str(item.get("node") or ""),
            conn_id=str(item.get("id") or ""),
        )

    @property
    def base_url(self) -> str:
        host = self.host.rstrip("/")
        if not host:
            return ""
        if not host.startswith(("http://", "https://")):
            return f"https://{host}:{self.port}"
        parsed = urlsplit(host)
        if parsed.port:
            return f"{parsed.scheme}://{parsed.netloc}"
        return f"{parsed.scheme}://{parsed.netloc}:{self.port}"

    @property
    def api_url(self) -> str:
        return f"{self.base_url}/api2/json"

    @property
    def configured(self) -> bool:
        return bool(self.host and self.token_id and self.token_secret)


def _extract_error(resp: httpx.Response) -> str:
    """尽量从 PBS 的响应里取出人话；取不到就回状态码。"""
    try:
        payload = resp.json()
    except ValueError:
        return f"PBS 返回 HTTP {resp.status_code}"
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if isinstance(errors, dict) and errors:
        return "; ".join(f"{k}: {v}" for k, v in errors.items())
    message = payload.get("message") if isinstance(payload, dict) else None
    return str(message or f"PBS 返回 HTTP {resp.status_code}")


class PbsClient:
    """PBS REST API 的只读封装（``/api2/json``）。"""

    def __init__(self, conn: PbsConnection) -> None:
        self.conn = conn
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                verify=build_ssl_context(self.conn.verify_ssl, self.conn.ca_cert),
                timeout=httpx.Timeout(settings.http_timeout, connect=8.0),
                follow_redirects=False,
                # 与 PVE 客户端一致：PBS 也在内网，不跟随 HTTP_PROXY
                trust_env=settings.pve_trust_env,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def _auth_headers(self) -> Dict[str, str]:
        """PBS 的 token 头与 PVE **不是**同一个形状。

        官方格式（PBS 4.2 文档 "API Tokens"）::

            Authorization: PBSAPIToken <tokenid>:<secret>

        即「scheme + 空格 + `tokenid:secret`」：scheme 与值之间是空格，
        tokenid 与 secret 之间是**冒号**。

        PVE 用的是 ``PVEAPIToken=<tokenid>=<secret>``（两个等号）。照抄 PVE 的
        写法，PBS 会把整串当成 token 名去找，回一个没有任何提示的 401 ——
        这也是为什么「secret 明明是对的」却一直认证失败。
        """
        return {
            # 前缀用 `=`、tokenid 与 secret 之间用 `:`。PBS 的解析器两种前缀
            # 都认，但 `=` 形式与 PBS 自带 Web UI / 官方示例一致，实测最稳。
            "Authorization": (
                f"PBSAPIToken={self.conn.token_id}:{self.conn.token_secret}"
            ),
        }

    async def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """执行一次 GET，返回解包后的 ``data``。"""
        if not self.conn.configured:
            raise ProxmoxError(
                "PBS connection is not configured. Open Nodes → Connections and "
                "add a read-only PBS token.",
                status_code=428,
                endpoint=f"GET {path}",
                code="pbs_not_configured",
            )
        client = await self._http()
        clean = (
            {k: v for k, v in params.items() if v is not None}
            if params
            else None
        )
        try:
            resp = await client.get(
                f"{self.conn.api_url}{path}",
                params=clean,
                headers=self._auth_headers(),
            )
        except httpx.TimeoutException as exc:
            raise ProxmoxError(
                f"PBS request timed out (GET {path})",
                status_code=504,
                endpoint=f"GET {path}",
            ) from exc
        except httpx.TransportError as exc:
            raise ProxmoxError(
                f"Cannot reach PBS at {self.conn.base_url}: {exc}",
                status_code=502,
                endpoint=f"GET {path}",
            ) from exc

        if resp.status_code >= 400:
            # 401/403 是 PBS 侧凭据或权限问题，不能当面板会话失效（否则前端会
            # 登出并进入「登录 → 再失败」的循环），统一按上游错误返回 502。
            if resp.status_code == 401:
                # PBS 对认证失败通常只回一个空 body，原样透出就是一句“HTTP 401”，
                # 对用户没有任何指向性。这里补一句可操作的说明，并用 code 让前端
                # 换成中文（后端错误文案面向日志，前端文案面向人）。
                raise ProxmoxError(
                    "PBS rejected the credentials (HTTP 401): the token ID must "
                    "look like user@realm!tokenname, and the secret must be the "
                    "token secret printed by `generate-token` (not the account "
                    "password).",
                    status_code=502,
                    endpoint=f"GET {path}",
                    pve_status=401,
                    code="pbs_auth_failed",
                )
            if resp.status_code == 403:
                raise ProxmoxError(
                    _extract_error(resp),
                    status_code=502,
                    endpoint=f"GET {path}",
                    pve_status=403,
                    code="pbs_permission_denied",
                )
            raise ProxmoxError(
                _extract_error(resp),
                status_code=resp.status_code,
                endpoint=f"GET {path}",
                pve_status=resp.status_code,
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProxmoxError(
                f"PBS returned a non-JSON response (GET {path})",
                status_code=502,
                endpoint=f"GET {path}",
            ) from exc
        return payload.get("data") if isinstance(payload, dict) else None

    # ------------------------------------------------------------- 只读接口
    async def version(self) -> Dict[str, Any]:
        return await self.get("/version") or {}

    async def nodes(self) -> List[Dict[str, Any]]:
        return await self.get("/nodes") or []

    async def node_status(self, node: str) -> Dict[str, Any]:
        return await self.get(f"/nodes/{node}/status") or {}

    async def tasks(self, node: str, limit: int = 20) -> List[Dict[str, Any]]:
        return await self.get(
            f"/nodes/{node}/tasks", {"limit": limit, "start": 0}
        ) or []

    async def datastores(self) -> List[Dict[str, Any]]:
        return await self.get("/admin/datastore") or []

    async def datastore_status(self, store_name: str) -> Dict[str, Any]:
        return await self.get(f"/admin/datastore/{store_name}/status") or {}

    async def disks(self, node: str) -> Any:
        """节点磁盘清单。

        路径必须是 ``/nodes/{node}/disks/list``：``/disks`` 本身是 API 的
        目录节点，PBS 会回一串 ``{"subdir": "smart"}`` 之类的导航项 —— 那种
        响应里没有任何磁盘，看起来就是“磁盘健康永远为空”。

        返回形状随版本变化（``{devices: [...]}`` 或裸数组），调用方自己兼容，
        归一写在 :func:`_disk_alerts` 里。
        """
        return await self.get(f"/nodes/{node}/disks/list")

    async def task_log(
        self, node: str, upid: str, limit: int = 200
    ) -> List[Dict[str, Any]]:
        """任务日志。UPID 里有 ``:`` / ``!`` / ``@``，必须整体转义后再进路径。"""
        return await self.get(
            f"/nodes/{node}/tasks/{quote(upid, safe='')}/log",
            {"limit": limit, "start": 0},
        ) or []


# ------------------------------------------------------------------ 配置存取
async def load_connections() -> List[Dict[str, Any]]:
    """读出全部 PBS 连接（密钥已解密）；缺失 / 损坏时返回空列表。"""
    raw = await store.get_setting(PBS_CONNECTIONS_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("PBS 连接配置不是合法 JSON，已忽略")
        return []
    if not isinstance(data, list):
        return []
    return [
        store._decrypt_secrets(item) for item in data if isinstance(item, dict)
    ]


async def save_connections(items: List[Dict[str, Any]]) -> None:
    """持久化 PBS 连接；密钥字段落库前统一加密。"""
    stored = [store._encrypt_secrets(dict(item)) for item in items]
    await store.set_setting(
        PBS_CONNECTIONS_KEY, json.dumps(stored, ensure_ascii=False)
    )
    # 保存后必须立刻作废概览缓存，否则用户会看到 20 秒前的“未配置 / 旧容量”
    invalidate_overview()


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX_RE = re.compile(r"^[0-9a-fA-F]{32,64}$")


def diagnose(cfg: PbsConnection) -> Dict[str, Any]:
    """凭据“长什么样”的自检，用于 401 时给出可操作的线索。

    **绝不返回 secret 本身**，只返回长度与形状判断 —— 足够区分「填的是账号密码」
    「填的是 token ID」「填了带前缀的整行」，又不把凭据写进日志/响应。
    """
    secret = cfg.token_secret
    return {
        "token_id": cfg.token_id,
        "token_id_has_bang": "!" in cfg.token_id,
        "secret_length": len(secret),
        "secret_uuid_like": bool(_UUID_RE.match(secret)),
        "secret_hex_like": bool(_HEX_RE.match(secret)),
        "secret_has_at": "@" in secret,
        # token ID 一定同时有 `@` 与 `!`；单看 `@` 会把邮箱形态的密码误判
        "secret_looks_like_token_id": "@" in secret and "!" in secret,
        "secret_has_space": any(ch.isspace() for ch in secret),
    }


def public_connection(conn: Dict[str, Any]) -> Dict[str, Any]:
    """给前端的脱敏视图：**绝不回吐 token_secret**。"""
    cfg = PbsConnection.from_dict(conn)
    return {
        "id": cfg.id,
        "name": cfg.name or cfg.host or "PBS",
        "region": cfg.region,
        "host": cfg.host,
        "port": cfg.port,
        "token_id": cfg.token_id,
        "token_secret_set": bool(cfg.token_secret),
        "verify_ssl": cfg.verify_ssl,
        "ca_cert": cfg.ca_cert,
        "node": cfg.node,
        "configured": cfg.configured,
    }


# ------------------------------------------------------------------ 数据采集
def _num(value: Any) -> Optional[float]:
    """把 PVE/PBS 的“可能是字符串的数字”统一成 float；不可解析时 None。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


#: 「最近任务」只展示与备份直接相关的任务：备份本身与备份校验。
#: prune / GC / sync / forget 属于仓库维护，混进列表会让「备份是否正常」这个
#: 问题被无关条目淹没 —— 它们的状态在 PBS 控制台里更合适。
BACKUP_TASK_TYPES = ("backup", "verify")


def _slim_task(raw: Dict[str, Any]) -> Dict[str, Any]:
    """任务条目里只留界面要用的字段。

    PBS 的任务条目相当宽（含 worker 类型、用户、进度字段），原样透传会随版本
    漂移；这里做白名单，前端不必跟着 PBS 版本改。
    """
    endtime = int(_num(raw.get("endtime")) or 0)
    return {
        "upid": str(raw.get("upid") or ""),
        # 任务日志接口要按节点取；不带上就得让前端再猜一次
        "node": str(raw.get("node") or ""),
        "type": str(raw.get("worker_type") or raw.get("type") or ""),
        "status": str(raw.get("status") or ""),
        "starttime": int(_num(raw.get("starttime")) or 0),
        "endtime": endtime,
        # endtime 为 0 表示还在跑：PBS 的列表接口就是这样表达的
        "running": endtime == 0,
        "worker_id": str(raw.get("worker_id") or ""),
        "user": str(raw.get("user") or ""),
    }


#: 备份盘使用率阈值（%）。达到 warn 标黄、达到 crit 标红，
#: 与 ``quotaalert`` 的「剩余比例」口径同源：先看警戒线，再谈数字。
PBS_USAGE_WARN = 85.0
PBS_USAGE_CRIT = 95.0


def usage_level(percent: float) -> str:
    """使用率 → ``ok | warn | crit``。前端据此选颜色，不再各写一套阈值。"""
    if percent >= PBS_USAGE_CRIT:
        return "crit"
    if percent >= PBS_USAGE_WARN:
        return "warn"
    return "ok"


def has_datastore_audit(perms: Any) -> bool:
    """``/access/permissions`` 里有没有落在 ``/datastore`` 下的 Datastore.Audit。

    必要性：PBS 的 ``GET /admin/datastore`` 在**没有权限**时返回空数组而不是
    403，于是「这台 PBS 没有仓库」和「token 缺 Datastore.Audit」在列表上长得
    一模一样。权限表能把两者分开：权限只授在 ``/system`` 时，Sys.Audit 生效
    （节点状态能读），但 Datastore.* 对 ``/datastore/{store}`` 不生效。
    """
    if not isinstance(perms, dict):
        return False
    for path, privs in perms.items():
        if not isinstance(privs, dict) or not privs.get("Datastore.Audit"):
            continue
        if path == "/datastore" or str(path).startswith("/datastore/"):
            return True
    return False


def _disk_alerts(raw: Any) -> List[Dict[str, Any]]:
    """从 PBS 的磁盘清单里挑出健康状态异常的盘。

    PBS 各版本返回形状不一（``{"devices": [...]}`` 或裸数组），字段也时有时无：
    PBS 3.x 用 ``health``（``PASSED``），4.x 的 ``/disks/list`` 用 ``status``
    （``passed``）。两种都认，缺字段就当"未知"，**不告警** —— 拿不准时沉默
    比误报更安全，磁盘告警一旦变成噪音，就没人看了。
    """
    items = raw.get("devices") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        health = str(item.get("health") or item.get("status") or "").strip().upper()
        if health in ("", "PASSED", "OK", "UNKNOWN"):
            continue
        out.append(
            {
                "device": str(
                    item.get("devpath") or item.get("dev") or item.get("name") or ""
                ),
                "model": str(item.get("model") or ""),
                "serial": str(item.get("serial") or ""),
                "health": health,
            }
        )
    return out


def _disk_count(raw: Any) -> int:
    """磁盘总数。

    前端要说「4 块盘正常」，而不只是「没有异常」—— 后者和「没读到」长得
    一模一样，用户没法判断监控到底在不在跑。
    """
    items = raw.get("devices") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return 0
    return sum(1 for item in items if isinstance(item, dict))


def _disk_summary(raw: Any) -> Dict[str, Any]:
    """磁盘汇总：盘数、健康数、总容量、SSD / HDD 构成。

    页面只写「2 块盘正常」信息量太少 —— 同一句话在「2 块 128G SSD」和
    「2 块 4T HDD」下含义完全不同，备份策略也不一样。这里只做汇总，
    不读温度 / 坏道之类的 SMART 细节（那些值在多数 PBS 版本上根本不下发）。
    """
    items = raw.get("devices") if isinstance(raw, dict) else raw
    empty = {"count": 0, "healthy": 0, "total_bytes": 0, "ssd": 0, "hdd": 0}
    if not isinstance(items, list):
        return empty
    count = healthy = total_bytes = ssd = hdd = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        count += 1
        health = str(item.get("health") or item.get("status") or "").strip().upper()
        # 与 _disk_alerts 同口径：空 / PASSED / OK / UNKNOWN 都不算异常
        if health in ("", "PASSED", "OK", "UNKNOWN"):
            healthy += 1
        total_bytes += int(_num(item.get("size")) or 0)
        kind = str(item.get("type") or "").strip().lower()
        if kind == "ssd":
            ssd += 1
        elif kind == "hdd":
            hdd += 1
    return {
        "count": count,
        "healthy": healthy,
        "total_bytes": total_bytes,
        "ssd": ssd,
        "hdd": hdd,
    }


def _disk_rows(raw: Any) -> List[Dict[str, Any]]:
    """全部磁盘的轻量明细（设备 / 型号 / 容量 / 健康）。

    卡片正文只放得下汇总，这份明细挂在悬停提示里：「2 块盘正常」背后的
    具体型号与容量，是判断「这批盘还能撑多久」的关键信息。
    """
    items = raw.get("devices") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "device": str(
                    item.get("devpath") or item.get("dev") or item.get("name") or ""
                ),
                "model": str(item.get("model") or ""),
                "size": int(_num(item.get("size")) or 0),
                "health": str(
                    item.get("health") or item.get("status") or ""
                ).strip().upper(),
            }
        )
    return out


def _latest_gc(tasks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """最近一次 GC 任务（含正在跑的那次）。

    ``tasks`` 里 GC 会被「最近任务」过滤掉，但卡片仍要显示「上次回收成不成」：
    GC 失败不会立刻爆盘，却是下一次备份失败的常见前因。
    """
    rows = [t for t in tasks if str(t.get("type") or "") == "garbage_collection"]
    if not rows:
        return None
    latest = max(rows, key=lambda t: int(t.get("starttime") or 0))
    return {
        "status": str(latest.get("status") or ""),
        "starttime": int(latest.get("starttime") or 0),
        "endtime": int(latest.get("endtime") or 0),
        "running": bool(latest.get("running")),
    }


async def datastore_row(client: "PbsClient", item: Dict[str, Any]) -> Dict[str, Any]:
    """一个 datastore 的容量快照（概览卡片与容量告警共用同一套口径）。

    ``used_percent`` 与 ``level`` 都在这里算：85 / 95 两条线只写一遍，
    界面上的红黄与告警里的阈值才不会说两套数字。
    """
    name = str(item.get("store") or "")
    status: Dict[str, Any] = {}
    # 容量读不到要留一句可读的原因：403 是权限、404 是版本/端点差异，
    # 两者在界面上长得都像「没有数据」，不给原因就只能靠猜。
    capacity_error: Optional[str] = None
    if name:
        try:
            status = await client.datastore_status(name)
        except ProxmoxError as exc:
            # 老版本 PBS 没有这个端点；容量留空，其余字段照常返回
            capacity_error = exc.code or f"HTTP {exc.pve_status}"
            logger.info(
                "读取 datastore %s 容量失败（%s）：%s",
                name,
                capacity_error,
                exc.message,
            )
    total = int(_num(status.get("total")) or 0)
    used = int(_num(status.get("used")) or 0)
    avail = int(_num(status.get("avail")) or max(total - used, 0))
    # 百分比与告警级别在服务端算：前端只负责上色，
    # 免得「85% / 95%」这两个数字在多个组件里各写一遍。
    used_percent = (
        round(min(100.0, max(0.0, used / total * 100.0)), 1)
        if total > 0
        else 0.0
    )
    return {
        "store": name,
        "path": str(item.get("path") or ""),
        "comment": str(item.get("comment") or ""),
        "total": total,
        "used": used,
        "avail": avail,
        # 剩余空间直接给（PBS 的 status 里有 avail；缺字段时按 total-used 兜底）
        "free": avail,
        "used_percent": used_percent,
        # total=0 说明这个版本读不到容量，级别留 unknown，界面不标红
        "level": usage_level(used_percent) if total > 0 else "unknown",
        # 读不到容量时的原因（权限码 / HTTP 码），成功时为 None
        "capacity_error": capacity_error,
        "gc_schedule": str(item.get("gc-schedule") or ""),
        "verify_new": bool(item.get("verify-new")),
        "maintenance_mode": str(item.get("maintenance-mode") or ""),
    }


#: 每个 PBS 连接上一轮真正读到数据的节点名（``host:port`` -> node）。
#: 用户把「节点」填错时，每轮都得先撞一次 400 再回退；记住结果后只付一次代价。
_node_name_cache: Dict[str, str] = {}


def _perm_hint(exc: ProxmoxError) -> str:
    """把一次上游失败归类成界面能直接用的原因。

    ``no_permission``：PBS 回 403 —— 凭据少了 Sys.Audit，属「补权限就能看见」；
    ``error``：其余情况（老版本没有该端点、临时网络问题等）。分开是为了不把
    「版本不支持」也写成「去加权限」，那种提示会把人带偏。
    """
    return "no_permission" if (exc.pve_status or 0) == 403 else "error"


async def _collect_one(conn: Dict[str, Any]) -> Dict[str, Any]:
    """采集一条 PBS 连接的全部只读指标；任何子项失败只影响它自己。"""
    cfg = PbsConnection.from_dict(conn)
    row: Dict[str, Any] = {
        "id": cfg.id,
        "name": cfg.name or cfg.host,
        # 区域随概览下发：节点页 / 备份页的 PBS 卡片要能标出这来自哪个机房
        "region": cfg.region,
        "host": cfg.host,
        "port": cfg.port,
        "configured": cfg.configured,
        "online": False,
        "version": "",
        "error": None,
        "node": None,
        "datastores": [],
        "tasks": [],
    }
    if not cfg.configured:
        row["error"] = "pbs_not_configured"
        return row

    client = PbsClient(cfg)
    try:
        version = await client.version()
        row["version"] = str(version.get("version") or version.get("release") or "")
        row["online"] = True
    except ProxmoxError as exc:
        row["error"] = exc.message
        return row

    # 节点名：`/nodes` 需要 Sys.Audit（缺权限就是 403），而 `cfg.node` 只是用户
    # 手填的偏好，写错（比如把连接名当节点名）会被 PBS 回 400 —— 之前正是这样，
    # 一处节点名打错，把内存、负载、运行时长、磁盘健康、任务列表全带没了。
    #
    # 所以这里按候选依次试，谁先返回数据就用谁：
    #   1. 用户填的节点名（且能在 `/nodes` 列表里对上）；
    #   2. 列表里的第一台；
    #   3. `localhost` —— PBS 对本地节点的官方别名，这台服务器只有一个节点，
    #      它永远指向本机，缺 Sys.Audit 拿不到列表时它是唯一可靠的写法。
    listed: List[str] = []
    try:
        nodes = await client.nodes()
        listed = [
            str(item.get("node") or "")
            for item in nodes
            if isinstance(item, dict)
        ]
        listed = [name for name in listed if name]
    except ProxmoxError as exc:
        logger.info("读取 PBS 节点列表失败（%s）：%s", cfg.host, exc.message)

    key = f"{cfg.host}:{cfg.port}"
    candidates: List[str] = []
    # 上一轮已经验证可用的名字排最前：节点名写错时不必每轮都先撞一次 400
    if _node_name_cache.get(key):
        candidates.append(_node_name_cache[key])
    if cfg.node and (not listed or cfg.node in listed):
        candidates.append(cfg.node)
    if listed:
        candidates.append(listed[0])
    candidates.append("localhost")
    candidates = list(dict.fromkeys(candidates))

    node_name = ""
    status: Dict[str, Any] = {}
    status_error: Optional[ProxmoxError] = None
    for candidate in candidates:
        try:
            status = await client.node_status(candidate)
        except ProxmoxError as exc:
            status_error = exc
            if len(candidates) > 1:
                logger.info(
                    "PBS 节点 %s 不可用（%s），换下一个候选",
                    candidate,
                    exc.message,
                )
            continue
        node_name = candidate
        _node_name_cache[key] = candidate
        break

    # 全都没通也留一个名字：任务与磁盘是另外两条 ACL，仍可能读得到
    if not node_name:
        node_name = candidates[-1]

    row["node"] = {"name": node_name, "status_available": bool(status)}
    if cfg.node and node_name != cfg.node:
        # 让界面能解释「我填的节点名怎么没生效」
        row["node"]["name_ignored"] = cfg.node
    if status:
        memory = status.get("memory") or {}
        loadavg = status.get("loadavg") or []
        cpuinfo = status.get("cpuinfo") or {}
        row["node"].update(
            {
                "uptime": int(_num(status.get("uptime")) or 0),
                "cpus": int(_num(cpuinfo.get("cpus")) or 0),
                "mem_total": int(_num(memory.get("total")) or 0),
                "mem_used": int(_num(memory.get("used")) or 0),
                "load1": _num(loadavg[0]) if len(loadavg) > 0 else None,
                "load5": _num(loadavg[1]) if len(loadavg) > 1 else None,
                "load15": _num(loadavg[2]) if len(loadavg) > 2 else None,
            }
        )
    else:
        # 403 = token 没有 Sys.Audit，属「补权限就能看见」；其余是别的上游问题
        row["node"]["status_hint"] = (
            _perm_hint(status_error) if status_error else "error"
        )
        # 原始报错留给悬停提示：只写「读不到」没法定位，管理员需要一个具体原因
        row["node"]["status_error"] = status_error.message if status_error else ""
        # 与磁盘分支同一套形状：读不到也把字段补齐，前端不必再判键存在与否
        row["node"].update(
            {
                "uptime": 0,
                "cpus": 0,
                "mem_total": 0,
                "mem_used": 0,
                "load1": None,
                "load5": None,
                "load15": None,
            }
        )

    if node_name:
        try:
            # 先多取一些再过滤：只要备份类任务，limit=20 里可能大半是 GC/同步，
            # 过滤后不够 8 条就白读了一趟。
            raw_tasks = await client.tasks(node_name, limit=60)
            slim = [
                _slim_task(item) for item in raw_tasks if isinstance(item, dict)
            ]
            row["tasks"] = [
                task for task in slim if task["type"] in BACKUP_TASK_TYPES
            ][:20]
            # 「最近任务」只留备份/校验，但 GC 状态要单独给卡片看
            row["node"]["gc"] = _latest_gc(slim)
        except ProxmoxError as exc:
            logger.info("读取 PBS 任务失败（%s）：%s", cfg.host, exc.message)
        try:
            raw_disks = await client.disks(node_name)
            row["node"]["disks"] = _disk_alerts(raw_disks)
            row["node"]["disks_total"] = _disk_count(raw_disks)
            row["node"]["disks_summary"] = _disk_summary(raw_disks)
            row["node"]["disks_all"] = _disk_rows(raw_disks)
        except ProxmoxError as exc:
            # 老版本 / 权限不足：磁盘健康留空，不影响其余字段
            logger.info("读取 PBS 磁盘状态失败（%s）：%s", cfg.host, exc.message)
            row["node"]["disks"] = []
            row["node"]["disks_total"] = 0
            row["node"]["disks_summary"] = _disk_summary(None)
            row["node"]["disks_all"] = []
            row["node"]["disks_hint"] = _perm_hint(exc)
            row["node"]["disks_error"] = exc.message

    # datastore：列表拿到后逐个补容量。单个数不出来不影响其它
    try:
        stores = [
            item for item in await client.datastores() if isinstance(item, dict)
        ]
    except ProxmoxError as exc:
        logger.info("读取 PBS datastore 失败（%s）：%s", cfg.host, exc.message)
        stores = []

    if stores:
        row["datastores"] = list(
            await asyncio.gather(*(datastore_row(client, item) for item in stores))
        )
    else:
        # 空列表 ≠ 没有仓库：见 has_datastore_audit 的说明。这里补一次权限查询，
        # 让界面能说「token 缺 /datastore 的 Datastore.Audit」而不是「没有仓库」。
        try:
            perms = await client.get("/access/permissions")
            if not has_datastore_audit(perms):
                row["datastore_hint"] = "no_permission"
        except ProxmoxError as exc:
            logger.info("读取 PBS 权限表失败（%s）：%s", cfg.host, exc.message)
    return row


#: 概览缓存时长（秒）。备份页 60 秒轮询一次，多人同时打开会各打一遍 PBS；
#: 20 秒足够覆盖「一起刷新」的那一波，又短到不会让新备份长期看不到。
_OVERVIEW_TTL = 20.0
_overview_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_overview_lock = asyncio.Lock()


def invalidate_overview() -> None:
    """配置变更后立刻作废：刚保存完凭据，不该还看到“未配置”。"""
    _overview_cache["ts"] = 0.0
    _overview_cache["data"] = None
    # 节点名缓存也要清：用户可能刚刚把「节点」改对，别继续用旧的回退值
    _node_name_cache.clear()


def _dedupe_by_host(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一台 PBS 被多条配置指向时只留一行。

    去重键是 ``host:port`` 而不是配置 id：同一台 PBS 登记两次（比如换 token 时
    忘了删旧条目）不该在界面上出现两份 —— 容量会看起来像翻倍，任务也会重复。
    被合并掉的名称记进 ``shared_with``，前端可以提示“还登记了 N 条”。
    """
    seen: Dict[str, Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []
    for row in rows:
        key = f"{row.get('host')}:{row.get('port')}"
        if key in seen:
            name = str(row.get("name") or "")
            if name:
                seen[key].setdefault("shared_with", []).append(name)
            continue
        row["shared_with"] = []
        seen[key] = row
        out.append(row)
    return out


async def collect_overview(force: bool = False) -> Dict[str, Any]:
    """汇总全部 PBS 连接（带 20 秒缓存）；任一连接失败不影响其它。

    返回的是**深拷贝**：调用方（routers）会往顶层加 ``pve_storages``，共享同一份
    对象会把缓存污染成“只有第一次请求看得到 PVE 存储”。
    """
    cached = _overview_cache["data"]
    if (
        not force
        and cached is not None
        and time.monotonic() - float(_overview_cache["ts"] or 0) < _OVERVIEW_TTL
    ):
        return copy.deepcopy(cached)

    async with _overview_lock:
        # 等锁期间可能已经有人填好了：再查一次，避免并发请求一起打 PBS
        cached = _overview_cache["data"]
        if (
            not force
            and cached is not None
            and time.monotonic() - float(_overview_cache["ts"] or 0) < _OVERVIEW_TTL
        ):
            return copy.deepcopy(cached)
        data = await _collect_all()
        _overview_cache["ts"] = time.monotonic()
        _overview_cache["data"] = data
        return copy.deepcopy(data)


async def _collect_all() -> Dict[str, Any]:
    conns = await load_connections()
    if not conns:
        return {
            "configured": False,
            "connections": [],
            "thresholds": {"warn": PBS_USAGE_WARN, "crit": PBS_USAGE_CRIT},
            "generated_at": int(time.time()),
        }
    rows = await asyncio.gather(
        *(_collect_one(item) for item in conns), return_exceptions=True
    )
    connections: List[Dict[str, Any]] = []
    for conn, row in zip(conns, rows):
        if isinstance(row, BaseException):
            cfg = PbsConnection.from_dict(conn)
            connections.append(
                {
                    "id": cfg.id,
                    "name": cfg.name or cfg.host,
                    "region": cfg.region,
                    "host": cfg.host,
                    "port": cfg.port,
                    "configured": cfg.configured,
                    "online": False,
                    "version": "",
                    "error": str(row),
                    "node": None,
                    "datastores": [],
                    "tasks": [],
                }
            )
            continue
        connections.append(row)
    return {
        "configured": True,
        "connections": _dedupe_by_host(connections),
        # 阈值随数据一起下发：界面提示「≥85% 警戒」时用的是服务端判定的同一组数
        "thresholds": {"warn": PBS_USAGE_WARN, "crit": PBS_USAGE_CRIT},
        # 采集时刻（unix 秒）：界面显示「数据更新于 X」用这个，
        # 而不是浏览器拿到响应的时间 —— 后者会被 20 秒缓存拉开误差。
        "generated_at": int(time.time()),
    }
