"""资源规格的规整逻辑。

规格是管理员手填的一份列表，最怕的是「一行填错、整份保存失败」——所以这里
定的是**逐条丢弃**：名字为空的那条不要，其余照收；越界或非数字的字段回落到
默认值，而不是把这条也扔掉（管理员改了核数却忘了填磁盘时，他想要的是磁盘取
默认，不是这条消失）。

不依赖 MySQL：``store`` 用内存打桩。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import specs  # noqa: E402


class _FakeStore:
    def __init__(self) -> None:
        self.values: Dict[str, str] = {}

    async def get_setting(self, key: str, default=None) -> Optional[str]:
        return self.values.get(key, default)

    async def set_setting(self, key: str, value: str) -> None:
        self.values[key] = value


@pytest.fixture(autouse=True)
def fake_store(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(specs.store, "get_setting", store.get_setting)
    monkeypatch.setattr(specs.store, "set_setting", store.set_setting)
    return store


class TestNormalise:
    def test_keeps_the_good_and_drops_the_nameless(self) -> None:
        out = specs.normalise(
            [
                {"name": "标准型", "cores": 2, "memory": 4096, "disk": 100},
                {"name": "   ", "cores": 2, "memory": 4096, "disk": 100},  # 名字为空
                "不是字典",
                {"cores": 4, "memory": 8192, "disk": 100},  # 连名字都没有
            ]
        )
        assert [s["name"] for s in out] == ["标准型"]

    def test_out_of_range_fields_fall_back_to_defaults(self) -> None:
        (item,) = specs.normalise(
            [{"name": "怪数字", "cores": 0, "memory": 10, "disk": -5}]
        )
        assert (item["cores"], item["memory"], item["disk"]) == (1, 2048, 100)

    def test_unknown_kind_becomes_both(self) -> None:
        (item,) = specs.normalise([{"name": "x", "kind": "gpu"}])
        assert item["kind"] == "both"

    def test_ids_are_stable_and_unique(self) -> None:
        out = specs.normalise(
            [
                {"id": "spec-a", "name": "A"},
                {"id": "spec-a", "name": "B"},  # 重复 id：后一条要换一个
                {"name": "C"},  # 没给 id：自动生成
            ]
        )
        ids = [s["id"] for s in out]
        assert ids[0] == "spec-a"
        assert len(set(ids)) == 3, "id 必须唯一，前端拿它当 key 与表单值"
        assert all(ids)

    def test_id_is_scrubbed(self) -> None:
        """id 会进 URL / 当 React key，只留安全字符。"""
        (item,) = specs.normalise([{"id": "a/b c#d", "name": "x"}])
        assert item["id"] == "abcd"

    def test_caps_the_count(self) -> None:
        out = specs.normalise([{"name": f"规格 {i}"} for i in range(specs.MAX_SPECS + 20)])
        assert len(out) == specs.MAX_SPECS

    def test_accepts_garbage(self) -> None:
        assert specs.normalise(None) == []
        assert specs.normalise("nope") == []
        assert specs.normalise({"a": 1}) == []


class TestBuiltinCatalog:
    """内置那组常用规格的约束。

    这组数据是「开箱即用」的样例，也是一份可以直接照抄的选型表 —— 一旦出现
    「两张一模一样的 2C4G」或「1C1G 配 2T 盘」，用户第一眼就会觉得这份清单
    没被认真对待。用测试卡住这几条，比写在注释里靠谱。
    """

    def test_names_and_ids_are_unique(self) -> None:
        names = [s["name"] for s in specs.DEFAULT_SPECS]
        ids = [s["id"] for s in specs.DEFAULT_SPECS]
        assert len(set(names)) == len(names), "名字重复，下拉里分不清谁是谁"
        assert len(set(ids)) == len(ids), "id 重复，前端 key 会撞"

    def test_core_memory_disk_combinations_are_distinct(self) -> None:
        combos = [
            (s["cores"], s["memory"], s["disk"]) for s in specs.DEFAULT_SPECS
        ]
        assert len(set(combos)) == len(combos), "有两条规格的核/内存/盘完全一样"

    def test_sorted_and_monotonic(self) -> None:
        keys = [(s["cores"], s["memory"]) for s in specs.DEFAULT_SPECS]
        assert keys == sorted(keys), "按核数、内存升序排列，界面上才是一条阶梯"

    def test_every_entry_survives_normalise(self) -> None:
        """内置条目必须是**合法**的：名字非空、字段在取值范围内。

        不合法的话首访展示的和保存回去的不是同一份，改一下别的字段就会悄悄丢条目。
        """
        cleaned = specs.normalise(specs.DEFAULT_SPECS)
        assert cleaned == specs.DEFAULT_SPECS

    def test_covers_the_four_tiers(self) -> None:
        """四档都要有：入门（≤2 核小盘）、通用（2~4 核中盘）、进阶（8~16 核大盘）、
        高配（32 核起）。少一档，下单页的筛选条就少一个可点的分类。

        但**不预置 64 核 / 1T 以上的旗舰档**：公有云里那是按需定制的宿主级规格，
        1T 盘也走另挂的按量数据盘；塞进「常用规格」只会诱导过度配置（调研口径
        见 :mod:`app.specs` 的 ``DEFAULT_SPECS`` 注释）。
        """
        by_cores = {s["cores"] for s in specs.DEFAULT_SPECS}
        assert {1, 2, 4, 8, 16, 32} <= by_cores
        disks = {s["cores"]: s["disk"] for s in specs.DEFAULT_SPECS}
        assert min(disks.values()) <= 20, "入门档要有一个小盘"
        assert max(disks.values()) >= 500, "高配档要能放下一套业务 + 日志"

    def test_disk_grows_with_tier(self) -> None:
        """同一档位里核内存越大、盘不该反而更小；跨档更不能倒挂。"""
        prev = 0
        for item in sorted(specs.DEFAULT_SPECS, key=lambda s: (s["cores"], s["memory"])):
            assert item["disk"] >= prev, f"{item['name']} 的盘比更低的档还小"
            prev = item["disk"]

    def test_presets_are_copies(self) -> None:
        """返回副本：调用方改到的是自己的那份，不会污染模块级常量。"""
        got = specs.presets()
        assert got == specs.DEFAULT_SPECS
        got[0]["name"] = "改坏了"
        assert specs.DEFAULT_SPECS[0]["name"] != "改坏了"


class TestPersistence:
    def test_defaults_when_never_saved(self) -> None:
        got = asyncio.run(specs.list_specs())
        assert [s["name"] for s in got] == [s["name"] for s in specs.DEFAULT_SPECS]

    def test_saved_round_trip(self) -> None:
        asyncio.run(
            specs.save_specs([{"name": "迷你", "cores": 1, "memory": 512, "disk": 20}])
        )
        got = asyncio.run(specs.list_specs())
        assert len(got) == 1 and got[0]["name"] == "迷你"

    def test_broken_json_falls_back_to_defaults(self, fake_store) -> None:
        fake_store.values[specs.SPECS_KEY] = "{不是 JSON"
        got = asyncio.run(specs.list_specs())
        assert len(got) == len(specs.DEFAULT_SPECS)

    def test_defaults_are_not_persisted(self, fake_store) -> None:
        """只看不改：读到默认值不该把它写进库，管理员也就不会被一次误访问固化配置。"""
        asyncio.run(specs.list_specs())
        assert specs.SPECS_KEY not in fake_store.values

    def test_get_spec_and_for_kind(self) -> None:
        asyncio.run(
            specs.save_specs(
                [
                    {"id": "v1", "name": "只有虚拟机", "kind": "vm"},
                    {"id": "l1", "name": "只有容器", "kind": "lxc"},
                    {"id": "b1", "name": "通用", "kind": "both"},
                ]
            )
        )
        all_specs = asyncio.run(specs.list_specs())
        assert asyncio.run(specs.get_spec("b1"))["name"] == "通用"
        assert asyncio.run(specs.get_spec("nope")) is None

        vm_names = [s["name"] for s in specs.for_kind(all_specs, "vm")]
        lxc_names = [s["name"] for s in specs.for_kind(all_specs, "lxc")]
        assert vm_names == ["只有虚拟机", "通用"]
        assert lxc_names == ["只有容器", "通用"]

    def test_saved_json_is_readable(self, fake_store) -> None:
        asyncio.run(specs.save_specs([{"name": "中文名", "kind": "vm"}]))
        raw = json.loads(fake_store.values[specs.SPECS_KEY])
        assert raw[0]["name"] == "中文名"
