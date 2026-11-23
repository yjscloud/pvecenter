"""节点备注：面板侧为 PVE 节点保存的自定义说明。

PVE 本身没有「备注/别名」这类自由文本字段，所以由面板自己存一份
（settings KV），用于让运维在节点卡片上标记用途、机房、负责人等信息。

键的格式是 ``<连接id>|<节点名>``（见前端 ``nodeNoteKey``）：节点名在每套 PVE
上都是本地的，两台主机默认安装名都可能叫 ``pve1``，只按节点名存一份会让编辑
一边覆盖另一边。旧版本写入的「只有节点名」的键原样保留，读取时回落兼容，
用户再次编辑该节点时由前端迁移到新键。
"""
from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request

from .. import security, store

router = APIRouter(prefix="/api", tags=["node-notes"])
NOTES_KEY = "node_notes"


async def _load() -> Dict[str, str]:
    raw = await store.get_setting(NOTES_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


async def migrate_legacy_keys() -> int:
    """把「只有节点名」的旧键固定到升级时的当前连接。

    旧版本不分连接，两台 PVE 都叫 ``pve1`` 时编辑一边就覆盖另一边。升级后键
    形如 ``<连接id>|<节点>``；这里把存量备注搬到当时「当前连接」名下并删掉旧键
    —— 旧数据本来就没有连接信息，不选一个就会继续对所有同名节点生效。
    还没配置任何连接（``active`` 为空）时不动，等首次连接建好后的下次启动再迁。
    """
    notes = await _load()
    legacy = {k: v for k, v in notes.items() if "|" not in k}
    active = str(store.get_active_connection_id() or "")
    if not legacy or not active:
        return 0
    for node, text in legacy.items():
        notes.setdefault(f"{active}|{node}", text)
        notes.pop(node, None)
    await store.set_setting(NOTES_KEY, json.dumps(notes, ensure_ascii=False))
    return len(legacy)


@router.get("/node-notes")
async def get_node_notes(
    user: Dict[str, Any] = Depends(security.require_permission("node.view")),
) -> Dict[str, Any]:
    return {"notes": await _load()}


@router.put("/node-notes")
async def save_node_notes(
    payload: Dict[str, str],
    request: Request,
    user: Dict[str, Any] = Depends(security.require_permission("settings.manage")),
) -> Dict[str, Any]:
    await store.set_setting(NOTES_KEY, json.dumps(payload, ensure_ascii=False))
    await security.audit(
        request, user, "node_notes.save", target="node_notes",
        detail={"count": len(payload)},
    )
    return {"notes": payload}
