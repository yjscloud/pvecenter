"""删机后的清理钩子：顺序、容错与补偿。

「资源回收后，端口进程里的服务器没自动移除」的直接原因之一，就是这个钩子
太靠后、失败又只留一条 warning：

* 机器已经销毁，前面的善后步骤任何一步抛错，钩子就永远走不到；
* 钩子自己失败时没人重试，界面上留一台永远连不上的机器。

这里锁死：钩子失败要落补偿项；单步善后失败不能把「删除成功」变成 500。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import sshremote  # noqa: E402
from app.routers import vms as vms_router  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class TestPurgeManagedVm:
    def test_passes_the_node_so_the_pending_queue_can_be_cleared(
        self, monkeypatch
    ) -> None:
        calls: List[Tuple[int, str, str]] = []

        async def _purge(vmid, conn_id="", *, node="", name=""):
            calls.append((int(vmid), conn_id, node))
            return ["h1"]

        monkeypatch.setattr(sshremote, "purge_vm_hosts", _purge)
        monkeypatch.setattr(vms_router, "_managed_conn_id", lambda: "c1")

        run(vms_router._purge_managed_vm(100, "pve"))

        assert calls == [(100, "c1", "pve")]

    def test_failure_is_queued_for_retry(self, monkeypatch) -> None:
        """失败不能只记日志：机器已经删了，没人重试就等于永久残留。"""
        queued: List[Tuple[int, str, str]] = []

        async def _boom(vmid, conn_id="", *, node="", name=""):
            raise RuntimeError("库抖动")

        async def _enqueue(vmid, conn_id="", node=""):
            queued.append((int(vmid), conn_id, node))

        monkeypatch.setattr(sshremote, "purge_vm_hosts", _boom)
        monkeypatch.setattr(sshremote, "enqueue_cleanup", _enqueue)
        monkeypatch.setattr(vms_router, "_managed_conn_id", lambda: "c1")

        run(vms_router._purge_managed_vm(100, "pve"))

        assert queued == [(100, "c1", "pve")]


class TestBestEffort:
    def test_swallows_failures(self) -> None:
        async def _boom() -> Any:
            raise RuntimeError("库抖动")

        # 机器已经删了：这里抛出去只会让接口 500，调用方以为没删掉
        assert run(vms_router._best_effort("测试", _boom())) is None

    def test_awaits_the_action(self) -> None:
        seen: List[str] = []

        async def _ok() -> None:
            seen.append("done")

        run(vms_router._best_effort("测试", _ok()))

        assert seen == ["done"]
