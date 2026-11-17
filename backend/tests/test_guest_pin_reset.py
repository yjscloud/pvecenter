"""机器回收后，置顶 / 收藏必须一起失效。

`guest_pins` 的键只有 ``<连接>:<节点>/<vmid>``，没有任何能区分「第几代」机器的
信息；PVE 分配新机器用的又是最小空闲 VMID。所以清理规则一旦漏掉，一台被回收
的机器留下的键会原样命中同号的新机器 —— 表现为「刚下发的新机器自带星标和置顶」。

这里只测清理函数本身（含所有历史写法）；调用点在删除 / 建机流程里，
批量删除那条由 test_bulk.TestDelete 顺带覆盖。
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable

import pytest

from app import database, prefs


async def _with_pool(coro: Awaitable[Any]) -> Any:
    try:
        return await coro
    finally:
        # 每条用例自己的事件循环结束时释放连接池，否则下一条用例会借到
        # 属于已关闭 loop 的连接（aiomysql 会直接报 Event loop is closed）
        await database.close_pool()


def _run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(_with_pool(coro))


@pytest.fixture()
def pins_db(clean_mysql_db):
    _run(prefs.init_table())
    return clean_mysql_db


def _save(username: str, pinned: list[str], starred: list[str]) -> None:
    _run(
        prefs.set(
            username,
            prefs.PREF_GUEST_PINS,
            {"pinned": pinned, "starred": starred},
        )
    )


def _load(username: str) -> Any:
    return _run(prefs.get(username, prefs.PREF_GUEST_PINS, {}))


def test_forget_clears_every_user_and_keeps_order(pins_db) -> None:
    _save("alice", ["c1:node1/100", "c1:node1/101"], ["c1:node1/100"])
    _save("bob", ["c1:node1/100"], ["c2:node2/100", "c1:node1/100"])

    changed = _run(prefs.forget_guest_refs("c1", "node1", 100))

    assert changed == 2
    alice = _load("alice")
    # 置顶是有序的：删掉 100 之后 101 仍排在第一
    assert alice["pinned"] == ["c1:node1/101"]
    assert alice["starred"] == []
    bob = _load("bob")
    assert bob["pinned"] == []
    # 另一套集群上的同号机器不能被误伤
    assert bob["starred"] == ["c2:node2/100"]


def test_forget_is_a_noop_for_other_machines(pins_db) -> None:
    _save("carol", ["c1:node1/200"], [])
    before = _load("carol")

    assert _run(prefs.forget_guest_refs("c1", "node1", 100)) == 0
    assert _load("carol") == before


def test_forget_covers_legacy_ref_shapes(pins_db) -> None:
    """空连接、缺省连接写成 '-'、以及更早不带前缀的键都要清掉。"""
    _save(
        "dave",
        ["node1/100", ":node1/100", "-:node1/100"],
        # vmid 只是前缀相同，不能顺带删掉
        ["c1:node1/1000"],
    )

    _run(prefs.forget_guest_refs("", "node1", 100))

    dave = _load("dave")
    assert dave["pinned"] == []
    assert dave["starred"] == ["c1:node1/1000"]


def test_ref_shapes_include_expected_variants() -> None:
    refs = prefs.guest_pin_refs("c1", "node1", 100)

    assert "c1:node1/100" in refs
    assert ":node1/100" in refs
    assert "-:node1/100" in refs
    assert "node1/100" in refs
    # 去重：空连接时「c1 带前缀」与「空前缀」是同一个键
    assert len(refs) == len(set(refs))


def test_forget_survives_broken_preference(pins_db) -> None:
    """存坏的值不能让清理整体失败，更不能把别的用户一起清掉。"""
    _run(prefs.set("erin", prefs.PREF_GUEST_PINS, ["oops"]))
    _save("frank", ["c1:node1/100"], [])

    assert _run(prefs.forget_guest_refs("c1", "node1", 100)) == 1
    assert _load("erin") == ["oops"]
    assert _load("frank")["pinned"] == []
