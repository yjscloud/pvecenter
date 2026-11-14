"""列表页「来源模板」：一次审计查询解析整页来源，并且只回名称。

    ../.venv/bin/python -m pytest tests/test_guest_source.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import security  # noqa: E402
from test_api_routes import api, auth_headers  # noqa: E402,F401


def test_list_vm_exposes_source_template(api) -> None:
    headers = auth_headers(api)

    async def seed() -> None:
        # 与克隆 / 重装落审计的格式一致：来源 -> 目标
        await security.audit(
            None,
            {"username": "admin"},
            "vm.clone",
            target="pve1/9000 -> pve1/100",
            result="success",
        )

    api.portal.call(seed)

    resp = api.get("/api/vms?type=all", headers=headers)
    assert resp.status_code == 200, resp.text
    row = next(item for item in resp.json() if item.get("vmid") == 100)

    source = row.get("source_template")
    assert source is not None
    assert source["vmid"] == 9000
    # 列表只关心名称；名字来自同一次 cluster_resources，不再额外问 PVE
    assert source["name"] == "ubuntu-template"


def test_machine_without_clone_record_has_no_source_template(api) -> None:
    headers = auth_headers(api)
    resp = api.get("/api/vms?type=all", headers=headers)
    assert resp.status_code == 200, resp.text
    row = next(item for item in resp.json() if item.get("vmid") == 100)
    assert row.get("source_template") is None


def test_container_created_from_os_template_reports_the_file(api) -> None:
    """容器最常见的建法不是克隆：选一个 vztmpl、``pct create`` 直接建。

    这种容器没有「来源机器」，来源就是那个模板文件 —— PVE 的 cluster/resources
    里没有这个字段，只能从面板建机时写的审计 detail 里取。列表页整列「—」的
    根因就在这里。
    """
    from app import guest_created

    async def seed() -> None:
        await security.audit(
            None,
            {"username": "admin"},
            "ct.create",
            target="pve1/105",
            detail={
                "hostname": "ct-105",
                # 与 routers/lxc.py 落审计时同一个形状
                "ostemplate": "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst",
            },
        )

    api.portal.call(seed)

    async def resolve() -> dict:
        return await guest_created.lxc_sources_for(
            [{"connection_id": "", "node": "pve1", "vmid": 105}]
        )

    sources = api.portal.call(resolve)
    assert sources[("", "pve1", 105)] == {
        "file": "debian-12-standard_12.7-1_amd64.tar.zst"
    }


def test_latest_record_wins_for_container_sources(api) -> None:
    """先新建、后来重装 / 克隆成别的模板：来源取**最近**那一条。

    同一秒内两条记录靠 ``id DESC`` 分先后（审计表的时间戳只有秒级精度），
    这也是列表来源不能只按 timestamp 排序的原因。
    """
    from app import guest_created

    async def seed() -> None:
        await security.audit(
            None,
            {"username": "admin"},
            "ct.create",
            target="pve1/106",
            detail={"ostemplate": "local:vztmpl/ubuntu-2204.tar.zst"},
        )
        await security.audit(
            None,
            {"username": "admin"},
            "ct.clone",
            target="pve1/9000 -> pve1/106",
            result="success",
        )

    api.portal.call(seed)

    async def resolve() -> dict:
        return await guest_created.lxc_sources_for(
            [{"connection_id": "", "node": "pve1", "vmid": 106}]
        )

    sources = api.portal.call(resolve)
    assert sources[("", "pve1", 106)] == {"node": "pve1", "vmid": 9000}


def test_template_file_name_strips_storage_and_query() -> None:
    from app import guest_created

    assert (
        guest_created.template_file_name("local:vztmpl/debian-12.tar.zst")
        == "debian-12.tar.zst"
    )
    # 受限卷会带 ``?uid=...``：那不是文件名的一部分
    assert (
        guest_created.template_file_name("local:vztmpl/alpine-3.20.tar.zst?uid=100000")
        == "alpine-3.20.tar.zst"
    )
    assert guest_created.template_file_name("") == ""
