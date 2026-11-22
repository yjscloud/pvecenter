"""资源规格（套餐）：管理员定义「几核 / 几 G 内存 / 多大盘」，用户下单时挑一个。

为什么不建表：这是一份**纯配置** —— 条数在几十以内、每次整份读写、不参与任何
计算，与 :mod:`app.routers.ip_pools` 的 IP 池同一性质。用 ``settings`` 表存一份
JSON 就够，省掉一次迁移与并发写入的复杂度。规格只在创建那一刻被翻译成
``cores`` / ``memory`` / 磁盘大小，交给既有的 ``POST /api/vms`` / ``POST /api/lxc``。

一条规格：::

    {
      "id": "s-1c2g",           # 稳定标识（默认规格自带，管理员新增的自动生成）
      "name": "入门型 1C2G",
      "kind": "both",           # vm | lxc | both —— 多数规格两类通用
      "cores": 1,
      "memory": 2048,           # MB
      "disk": 20,               # GB
      "description": "轻量服务、监控采集"
    }

首访（设置里还没有这份配置）返回 :data:`DEFAULT_SPECS`，管理员保存后即以保存的
为准。**不落库**默认值：这样管理员能直接看到一组可用的示例，改动也只在点保存后
生效，不会被一次误访问写成既定配置。
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional

from . import store

SPECS_KEY = "resource_specs"

KINDS = ("vm", "lxc", "both")

# 首访默认给的一组常用档位，也是设置页「添加常用规格」的来源（见 :func:`presets`）。
#
# 这份表按公有云的**实际售卖档位**选，调研口径（2026-11 查官方规格页）：
#   · 阿里云 ECS g7（1:4）：2C8G / 4C16G / 8C32G / 16C64G / 32C128G；
#     c 系（1:2）、r 系（1:8），u1 可调 1:2 / 1:4 / 1:8。
#   · 腾讯云 CVM 标准型 S5：1C1G / 1C2G、2C2G / 2C4G / 2C8G、4C8G / 4C16G、
#     8C16G / 8C32G、16C32G，产品线同时提供 1:2 与 1:4 两种处理器内存比。
#   · AWS EC2：t 系 2C1G~2C8G、m6i（1:4）2C8G / 4C16G / 8C32G / 16C64G、
#     c6i（1:2）、r6i（1:8）。
#   盘的基准是**系统盘**：公有云默认 40~100G，1T 以上是另挂的按量数据盘，
#   不打包进规格 —— 所以这里最高只到 500G，不再出现 1T / 2T。
#
# 四条原则，改这份表时别破：
#   1. **档位命名统一**「<档位>型 <核>C<内存>G」—— 下单页一行里同时能看到名字与数字时，
#      名字自己就能被认出来，不用现场换算。
#   2. **组合互不重复**：核 / 内存 / 磁盘三元组唯一（测试里卡着）。两张「2C4G 100G」
#      只是名字不同，选的人只会更犹豫。
#   3. **盘随档位长大**：入门 20~40G、通用 40~100G（业务 + 日志）、进阶 200~300G、
#      高配 500G（更大的数据盘在创建时按需另挂，别在这里一次性给足）。
#   4. **单调递增**：按核数、内存升序排列，界面上就是一条阶梯。
DEFAULT_SPECS: List[Dict[str, Any]] = [
    # ---- 入门型：小型站点、跳板机、探针（对应腾讯 S5.SMALL1/2、AWS t 系） ----
    {
        "id": "spec-1c1g",
        "name": "入门型 1C1G",
        "kind": "both",
        "cores": 1,
        "memory": 1024,
        "disk": 20,
        "description": "静态页面、跳板机、告警探针",
    },
    {
        "id": "spec-1c2g",
        "name": "入门型 1C2G",
        "kind": "both",
        "cores": 1,
        "memory": 2048,
        "disk": 20,
        "description": "轻量服务、反向代理、监控采集",
    },
    {
        "id": "spec-2c2g",
        "name": "入门型 2C2G",
        "kind": "both",
        "cores": 2,
        "memory": 2048,
        "disk": 40,
        "description": "测试环境、小型站点",
    },
    # ---- 通用型：常规业务、容器宿主、中小数据库（1:2 与 1:4 两条主线） ----
    {
        "id": "spec-2c4g",
        "name": "通用型 2C4G",
        "kind": "both",
        "cores": 2,
        "memory": 4096,
        "disk": 40,
        "description": "常规业务、小型数据库",
    },
    {
        "id": "spec-2c8g",
        "name": "通用型 2C8G",
        "kind": "both",
        "cores": 2,
        "memory": 8192,
        "disk": 50,
        "description": "Web 应用、容器宿主",
    },
    {
        "id": "spec-4c8g",
        "name": "通用型 4C8G",
        "kind": "both",
        "cores": 4,
        "memory": 8192,
        "disk": 80,
        "description": "中等并发 Web、CI 构建节点",
    },
    {
        "id": "spec-4c16g",
        "name": "通用型 4C16G",
        "kind": "both",
        "cores": 4,
        "memory": 16384,
        "disk": 100,
        "description": "Java 应用、中型数据库",
    },
    # ---- 进阶型：高负载应用、缓存集群、业务主库 ----
    {
        "id": "spec-8c16g",
        "name": "进阶型 8C16G",
        "kind": "both",
        "cores": 8,
        "memory": 16384,
        "disk": 200,
        "description": "高负载应用、编译构建",
    },
    {
        "id": "spec-8c32g",
        "name": "进阶型 8C32G",
        "kind": "both",
        "cores": 8,
        "memory": 32768,
        "disk": 300,
        "description": "Redis / ES、离线分析",
    },
    {
        "id": "spec-16c32g",
        "name": "进阶型 16C32G",
        "kind": "both",
        "cores": 16,
        "memory": 32768,
        "disk": 500,
        "description": "核心业务主库、高并发集群",
    },
    # ---- 高配型：只留到 32 核；再往上属于按需定制的宿主级规格 ----
    {
        "id": "spec-16c64g",
        "name": "高配型 16C64G",
        "kind": "both",
        "cores": 16,
        "memory": 65536,
        "disk": 500,
        "description": "内存型业务、数据仓库",
    },
    {
        "id": "spec-32c64g",
        "name": "高配型 32C64G",
        "kind": "both",
        "cores": 32,
        "memory": 65536,
        "disk": 500,
        "description": "大型业务、批量计算",
    },
]

# 规格的数量上限：这不是「越多越好」的列表，几百条会把人机两边的界面都拖垮
MAX_SPECS = 64
MAX_CORES = 128
# 内存下限 128 MB、上限 1 TB；磁盘下限 1 GB、上限 64 TB（与 PVE 的量级一致）
MIN_MEMORY, MAX_MEMORY = 128, 1024 * 1024
MIN_DISK, MAX_DISK = 1, 65536


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return num if low <= num <= high else default


def _clean_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    # 只留安全字符：id 会进 URL（/api/config/specs 的整份覆盖不按 id 取，但前端
    # 会拿它当 React key 与表单 value），别让奇怪字符混进来
    return re.sub(r"[^A-Za-z0-9_.-]", "", raw)[:64]


def normalise(raw: Any) -> List[Dict[str, Any]]:
    """把外部传进来的一坨规整成可用的规格列表。

    任何一条不合法（名字为空、核数为 0、类型不认识）就**丢那一条**，其余照收 ——
    管理员一次填错一行，不该让整份配置保存失败。条数上限 :data:`MAX_SPECS`。
    """
    items = raw if isinstance(raw, list) else []
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:64]
        if not name:
            continue
        kind = str(item.get("kind") or "both").strip().lower()
        if kind not in KINDS:
            kind = "both"
        spec_id = _clean_id(item.get("id")) or f"spec-{uuid.uuid4().hex[:8]}"
        if spec_id in seen:
            spec_id = f"spec-{uuid.uuid4().hex[:8]}"
        seen.add(spec_id)
        out.append(
            {
                "id": spec_id,
                "name": name,
                "kind": kind,
                "cores": _int(item.get("cores"), 1, 1, MAX_CORES),
                "memory": _int(item.get("memory"), 2048, MIN_MEMORY, MAX_MEMORY),
                "disk": _int(item.get("disk"), 100, MIN_DISK, MAX_DISK),
                "description": str(item.get("description") or "").strip()[:200],
            }
        )
        if len(out) >= MAX_SPECS:
            break
    return out


def presets() -> List[Dict[str, Any]]:
    """内置的一组常用规格（:data:`DEFAULT_SPECS` 的副本）。

    设置页的「添加常用规格」用它：已经在用的部署早把规格存进 KV 了，读
    ``/api/runtime/specs`` 拿不到这份内置表，所以单独给一个只读入口
    （``/api/runtime/specs/presets``）。返回副本，调用方拿去做 UI 时不可能
    改到模块级常量。
    """
    return [dict(item) for item in DEFAULT_SPECS]


async def list_specs() -> List[Dict[str, Any]]:
    """全量规格；从未保存过时返回默认那几档。"""
    raw = await store.get_setting(SPECS_KEY)
    if not raw:
        return [dict(item) for item in DEFAULT_SPECS]
    try:
        return normalise(json.loads(raw))
    except ValueError:
        return [dict(item) for item in DEFAULT_SPECS]


async def save_specs(items: Any) -> List[Dict[str, Any]]:
    """整份覆盖保存（与 IP 池一样的前端契约：本地编辑完一次性提交）。"""
    cleaned = normalise(items)
    await store.set_setting(SPECS_KEY, json.dumps(cleaned, ensure_ascii=False))
    return cleaned


async def get_spec(spec_id: str) -> Optional[Dict[str, Any]]:
    for item in await list_specs():
        if item["id"] == spec_id:
            return item
    return None


def for_kind(specs: Iterable[Dict[str, Any]], kind: str) -> List[Dict[str, Any]]:
    """只要某类可用的规格（``both`` 两类都算）。"""
    return [s for s in specs if s.get("kind") in (kind, "both")]
