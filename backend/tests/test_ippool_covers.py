"""列表上「池外」标记的判定口径。

两个函数回答两个不同的问题，混用就会误报：

* ``pool_contains``：这个地址能不能登记成池的占用（必须落在 start~end 的可分配
  区间里）—— 分配合规性用它；
* ``pool_covers``：这台机器用的地址是不是池管的这张网里的（只看子网）—— 界面上
  「池外」标记用它。

真实误报场景：池子网 10.0.0.0/24、可分配区间 100~200，运维手工把机器的地址填成
10.0.0.50（网关旁边、留给打印机 / 存储的号段都是常见做法）。按区间判会标「池外」，
而它明明就在池管的网里。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import ippool  # noqa: E402

POOL = {
    "id": "pool-a",
    "name": "业务池",
    "subnet": "10.0.0.0/24",
    "start": "10.0.0.100",
    "end": "10.0.0.200",
    "gateway": "10.0.0.1",
}


def test_manual_address_inside_subnet_but_outside_range_is_covered():
    """手动填 10.0.0.50：在池的子网里、不在可分配区间 —— 不该显示「池外」。"""
    assert ippool.pool_contains(POOL, "10.0.0.50") is False  # 不能拿来分配
    assert ippool.pool_covers(POOL, "10.0.0.50") is True  # 但属于这张网


def test_address_outside_subnet_is_not_covered():
    assert ippool.pool_covers(POOL, "192.168.9.9") is False


def test_cidr_suffix_and_gateway_are_tolerated():
    # 手动值可能带掩码；网关虽然不可分配，但确实在池这张网里
    assert ippool.pool_covers(POOL, "10.0.0.50/24") is True
    assert ippool.pool_covers(POOL, "10.0.0.1") is True


def test_pool_without_subnet_falls_back_to_range():
    pool = {"id": "p", "name": "只有区间", "subnet": "", "start": "10.0.0.10", "end": "10.0.0.20"}
    assert ippool.pool_covers(pool, "10.0.0.15") is True
    assert ippool.pool_covers(pool, "10.0.0.50") is False


def test_empty_or_invalid_input_is_not_covered():
    assert ippool.pool_covers(POOL, "") is False
    assert ippool.pool_covers(POOL, "not-an-ip") is False
