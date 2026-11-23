"""备份接口的连接绑定：指定了节点的请求必须打那台 PVE。

节点名在每套 PVE 上都是本地的。请求只带 ``?node=node02`` 却落到「兜底连接」时，
那台 PVE 会把节点名当主机名去解析，回 ``hostname lookup 'node02' failed`` ——
删除接口更糟：归属检查读不到卷，可能把别人的归档判成「不存在」或反过来。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app.routers import backups  # noqa: E402

VOLID = "vzdump-qemu-100-2026_01_01-00_00_00.vma.zst"


class _Client:
    """只记录 delete 路径：断言「删的是绑定后那台 PVE 上的卷」。"""

    def __init__(self, calls: List[Any]) -> None:
        self.calls = calls

    async def delete(self, path: str) -> None:
        self.calls.append(("delete", path))


def test_delete_backup_binds_connection_before_checking_ownership(
    monkeypatch,
) -> None:
    calls: List[Any] = []

    async def fake_bind(node: str) -> None:
        calls.append(("bind", node))

    async def fake_access(*args: Any, **kwargs: Any) -> None:
        # (user, node, storage, volid)
        calls.append(("access", args[1] if len(args) > 1 else kwargs.get("node")))

    async def fake_protected(volid: str) -> bool:
        calls.append(("protected", volid))
        return False

    async def fake_audit(*args: Any, **kwargs: Any) -> None:
        calls.append(("audit", kwargs.get("target")))

    monkeypatch.setattr(backups, "bind_node_connection", fake_bind)
    monkeypatch.setattr(backups, "assert_backup_access", fake_access)
    monkeypatch.setattr(backups.backupguard, "is_protected", fake_protected)
    monkeypatch.setattr(backups.security, "audit", fake_audit)
    monkeypatch.setattr(backups, "_forget_backup_cache", lambda: None)
    monkeypatch.setattr(backups, "get_client", lambda: _Client(calls))

    result = asyncio.run(
        backups.delete_backup(
            request=None,  # type: ignore[arg-type] - 审计被替身接管
            node="node02",
            storage="backup",
            volid=VOLID,
            user={"username": "root", "role": "admin"},
            _step_up={"username": "root"},
        )
    )

    kinds = [item[0] for item in calls]
    # 绑定必须排在归属检查之前：归属检查自己会 get_client() 读存储内容
    assert kinds[:2] == ["bind", "access"], kinds
    assert calls[0] == ("bind", "node02")
    assert ("delete", f"/nodes/node02/storage/backup/content/{VOLID}") in calls
    assert result == {"deleted": VOLID}
