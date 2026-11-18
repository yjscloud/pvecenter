"""巡检快照里的「幽灵主机」过滤。

背景：端口进程页的「服务器」清单、KPI 卡片都读巡检快照（
``inspection_host_report``）。快照行按 ``host_id`` 存，主机被移除之后行还在，
而管理员读路径（不传 ``host_ids``）原本是「不过滤」—— 于是一台已经删掉的机器
会以「永远连不上」的样子留在清单里，最长到快照保留期（7 天）。

这里锁死两条边界：删掉的主机必须被认出来；**读不到真实主机清单时一台都不许判**
（宁可多显示一台，也不能因为一次库抖动把所有人的快照清空）。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import inspection, localhost, sshremote  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class TestGhostHostIds:
    def test_drops_hosts_that_no_longer_exist(self) -> None:
        assert inspection._ghost_host_ids({"h1", "h2"}, {"h1"}) == {"h2"}

    def test_keeps_everything_when_live_set_is_unknown(self) -> None:
        """``None`` = 本轮读不到主机清单：不判，避免误删所有人的快照。"""
        assert inspection._ghost_host_ids({"h1", "h2"}, None) == set()

    def test_no_ghosts_when_every_host_is_alive(self) -> None:
        assert inspection._ghost_host_ids({"h1"}, {"h1", "local"}) == set()


class TestLiveHostIds:
    def test_includes_the_local_host(self, monkeypatch) -> None:
        async def _list(include_disabled: bool = True):
            return [{"id": "h1"}]

        async def _enabled() -> bool:
            return True

        monkeypatch.setattr(sshremote, "list_hosts", _list)
        monkeypatch.setattr(localhost, "enabled", _enabled)

        assert run(inspection._live_host_ids()) == {"h1", "local"}

    def test_skips_local_when_the_switch_is_off(self, monkeypatch) -> None:
        async def _list(include_disabled: bool = True):
            return [{"id": "h1"}]

        async def _enabled() -> bool:
            return False

        monkeypatch.setattr(sshremote, "list_hosts", _list)
        monkeypatch.setattr(localhost, "enabled", _enabled)

        assert run(inspection._live_host_ids()) == {"h1"}

    def test_returns_none_when_the_inventory_cannot_be_read(self, monkeypatch) -> None:
        async def _boom():
            raise RuntimeError("库不可用")

        monkeypatch.setattr(sshremote, "list_hosts", _boom)

        assert run(inspection._live_host_ids()) is None

    def test_keeps_local_when_the_switch_cannot_be_read(self, monkeypatch) -> None:
        """开关读不出来时按「开着」处理：误删本机快照的代价比多显示一台大。"""

        async def _list(include_disabled: bool = True):
            return []

        async def _boom():
            raise RuntimeError("库不可用")

        monkeypatch.setattr(sshremote, "list_hosts", _list)
        monkeypatch.setattr(localhost, "enabled", _boom)

        assert run(inspection._live_host_ids()) == {"local"}
