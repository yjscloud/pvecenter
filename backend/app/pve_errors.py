"""识别 PVE 报错里那几类「其实是面板打错了机器」的错误。

多连接部署里，节点名在每台 PVE 上都是**本地的**。请求只带节点名（或 VMID）
却没有指定连接时，一旦落到别的那台 PVE，PVE 就会把这个名字当主机名去解析，
回一句 ``hostname lookup 'node02' failed`` / ``Name or service not known`` ——
看起来像 PVE 的 DNS 坏了，实际是连接选错了。

这类错误必须给用户一句能照着做的话（确认机器所属连接、检查各节点 /etc/hosts），
而不是把 PVE 原文透传出去。识别逻辑收在这里，避免每个路由各写一份（隔离与
防火墙已经各踩过一次）。

用法::

    from .. import pve_errors

    if pve_errors.is_unresolved(exc):
        node = pve_errors.unresolved_node(exc) or node_param
        raise HTTPException(502, i18n.t("pve.error.nodeUnresolved", node=node))
"""
from __future__ import annotations

import re
from typing import Any

#: PVE / 系统解析失败的几种说法（大小写不敏感，用 ``in`` 判断）
UNRESOLVED_MARKERS = (
    "hostname lookup",
    "name or service not known",
    "temporary failure in name resolution",
    "unable to resolve",
)

#: ``hostname lookup 'node02' failed`` —— 把那个名字挖出来，文案里点名更有用
_QUOTED_NODE = re.compile(r"hostname lookup '([^']+)' failed", re.IGNORECASE)


def is_unresolved(*texts: Any) -> bool:
    """这段 PVE 报错是不是「节点名解析失败」。

    ``ProxmoxError`` 的 ``str()`` 与 ``.message`` 可能一个有前缀一个没有，调用方
    把两个都传进来即可（``*texts``）。
    """
    for text in texts:
        lowered = str(text or "").lower()
        if any(marker in lowered for marker in UNRESOLVED_MARKERS):
            return True
    return False


def unresolved_node(*texts: Any) -> str:
    """从报错里挖出被当作主机名解析的那个节点名；挖不到返回空串。

    拿到名字后调用方仍应回落到自己手里的 ``node`` 参数 —— PVE 的报错文本在不同
    版本上并不总是带引号。
    """
    for text in texts:
        matched = _QUOTED_NODE.search(str(text or ""))
        if matched:
            return matched.group(1).strip()
    return ""
