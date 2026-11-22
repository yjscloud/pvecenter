"""frpc 代理重名与启动失败信息的可读性（线上事故回归）。

两个问题来自同一份日志：

    proxy name [tunnel-1] is duplicated
    [看护] 自动拉起失败（第 3 次）：frpc 启动失败：[看护] 自动拉起失败（第 2 次）…

**问题一（致命）**：frpc 只有一个进程、一份 ``frpc.toml``，代理名必须全局唯一。
而规则名的唯一性只保证在**同一个用户**内（接口按 username 过滤），新建弹窗的
默认名又人人从 ``tunnel-1`` 起 —— 两个用户各建一条默认规则，合并渲染后就是重名，
frpc 直接拒绝启动，**所有人的隧道一起躺下**。所以渲染名统一带归属前缀。

**问题二（吵）**：启动失败的详情取的是「日志最后 4 行」，而看护把失败原因又写回
同一份日志 —— 每失败一次就套娃一层，几轮之后真正的报错被埋在十几 KB 的自我引用
里。现在只取本次启动（最后一个「=== 启动 ===」之后）frpc 自己写的行。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import frp  # noqa: E402
from test_api_routes import (  # noqa: E402,F401
    api,
    auth_headers,
    user_with_permissions,
)

SAFE = re.compile(r"^[A-Za-z0-9_-]+$")


def _rule(
    username: str,
    name: str = "tunnel-1",
    rule_id: str = "r1",
    remote_port: int = 40001,
    *,
    stale: bool = False,
) -> Dict[str, Any]:
    return {
        "id": rule_id,
        "username": username,
        "name": name,
        "type": "tcp",
        "local_ip": "10.0.0.5",
        "local_port": 8080,
        "remote_port": remote_port,
        "guest_ref": "",
        "stale": stale,
    }


SERVER = {"server_addr": "frp.example.com", "server_port": 7000, "token": ""}


# --------------------------------------------------------------- 代理名唯一性
class TestProxyNames:
    def test_two_users_with_the_same_rule_name_do_not_collide(self) -> None:
        """线上那条报错的根因：alice 与 bob 各有一条 tunnel-1。"""
        rules = [
            _rule("alice", remote_port=40001, rule_id="a1"),
            _rule("bob", remote_port=40002, rule_id="b1"),
        ]

        names = frp.proxy_names(rules)

        assert names["a1"] == "alice_tunnel-1"
        assert names["b1"] == "bob_tunnel-1"
        assert len(set(names.values())) == 2

    def test_rendered_config_has_no_duplicate_proxy(self) -> None:
        """渲染出来的 toml 里每个 name 只出现一次 —— 这才是 frpc 的判据。"""
        rules = [
            _rule("alice", remote_port=40001, rule_id="a1"),
            _rule("bob", remote_port=40002, rule_id="b1"),
            _rule("carol", name="web", remote_port=40003, rule_id="c1"),
        ]

        cfg = frp.effective_config(SERVER, rules)
        rendered = [item["name"] for item in cfg["proxies"]]

        assert rendered == ["alice_tunnel-1", "bob_tunnel-1", "carol_web"]
        assert len(set(rendered)) == len(rendered)

        toml = frp.render_toml(cfg)
        assert toml.count('name = "alice_tunnel-1"') == 1

    def test_usernames_with_chinese_or_spaces_still_produce_safe_names(self) -> None:
        """用户名允许中文 / 空格 / 邮箱符号，frp 代理名只认 ASCII 安全字符。"""
        rules = [
            _rule("张三", rule_id="c1"),
            _rule("li lei", rule_id="s1"),
            _rule("ops@example.com", rule_id="e1"),
        ]

        names = frp.proxy_names(rules)

        assert names["s1"] == "li_lei_tunnel-1"
        assert names["e1"] == "ops_example_com_tunnel-1"
        # 中文整段会被过滤空 → 退回稳定哈希，仍然合法且可重复
        assert SAFE.match(names["c1"])
        assert names["c1"].endswith("_tunnel-1")
        assert names["c1"] == frp.proxy_names(rules)["c1"]

    def test_owner_slug_collision_falls_back_to_a_suffix(self) -> None:
        """两个用户名清洗后相同（"a b" 与 "a-b"）：必须靠序号兜底。"""
        rules = [
            _rule("a b", rule_id="x1", remote_port=40001),
            _rule("a-b", rule_id="y1", remote_port=40002),
        ]

        names = frp.proxy_names(rules)

        assert sorted(names.values()) == ["a-b_tunnel-1", "a_b_tunnel-1"]
        # 与输入顺序无关：同一份规则每次算出同样的名字
        assert frp.proxy_names(list(reversed(rules))) == names

    def test_legacy_duplicate_inside_one_user_is_deduped(self) -> None:
        """历史数据（接口还没拦同人重名时留下的）也要能启动。"""
        rules = [
            _rule("alice", rule_id="a1", remote_port=40001),
            _rule("alice", rule_id="a2", remote_port=40002),
        ]

        names = frp.proxy_names(rules)

        assert sorted(names.values()) == ["alice_tunnel-1", "alice_tunnel-1-2"]

    def test_unowned_rule_gets_a_readable_placeholder(self) -> None:
        names = frp.proxy_names([_rule("", rule_id="n1")])
        assert names["n1"] == "shared_tunnel-1"

    def test_stale_rule_is_not_rendered_but_keeps_names_stable(self) -> None:
        live = _rule("alice", rule_id="a1", remote_port=40001)
        stale = _rule("bob", rule_id="b1", remote_port=40002, stale=True)

        cfg = frp.effective_config(SERVER, [live, stale])

        assert [item["name"] for item in cfg["proxies"]] == ["alice_tunnel-1"]


# ------------------------------------------------------------------ 接口口径
RULE_BODY = {
    "name": "tunnel-1",
    "type": "tcp",
    "local_ip": "10.0.0.5",
    "local_port": 8080,
    "remote_port": 40001,
}


class TestRuleApiExposesProxyName:
    """前端靠 ``proxy_name`` 判断「已生效」，不能再拿规则名去比。"""

    def _create(self, api, username: str, remote_port: int) -> Dict[str, Any]:
        resp = api.post(
            "/api/frp/rules",
            headers=auth_headers(api),
            json={**RULE_BODY, "username": username, "remote_port": remote_port},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_two_users_same_name_are_both_effective(self, api) -> None:
        alice_rule = self._create(api, "alice", 40001)
        bob_rule = self._create(api, "bob", 40002)

        assert alice_rule["proxy_name"] == "alice_tunnel-1"
        assert bob_rule["proxy_name"] == "bob_tunnel-1"

        # 合并视图里两条都在，且名字唯一 —— frpc 能起来了
        cfg = api.get("/api/frp/effective", headers=auth_headers(api)).json()["config"]
        rendered = [item["name"] for item in cfg["proxies"]]
        assert sorted(rendered) == ["alice_tunnel-1", "bob_tunnel-1"]

        # 列表里带回来的 proxy_name 与合并视图对得上（「已生效」徽章靠它）
        listed = api.get("/api/frp/rules", headers=auth_headers(api)).json()["rules"]
        assert {r["username"]: r["proxy_name"] for r in listed} == {
            "alice": "alice_tunnel-1",
            "bob": "bob_tunnel-1",
        }

    def test_ordinary_user_only_picks_up_the_global_name(self, api) -> None:
        """普通用户只看得到自己的规则，但名字要按全局规则算出来。"""
        self._create(api, "alice", 40001)
        self._create(api, "bob", 40002)

        viewer = user_with_permissions(api, ["frp.view"], "bob")
        listed = api.get("/api/frp/rules", headers=viewer).json()["rules"]

        assert [r["proxy_name"] for r in listed] == ["bob_tunnel-1"]
        cfg = api.get("/api/frp/effective", headers=viewer).json()["config"]
        assert [item["name"] for item in cfg["proxies"]] == ["bob_tunnel-1"]


# ------------------------------------------------------------ 失败信息不套娃
class TestStartOutput:
    def _log(self, monkeypatch, tmp_path: Path, lines: List[str]) -> None:
        path = tmp_path / "frpc.log"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setattr(frp, "LOG_PATH", path)

    def test_only_the_latest_attempt_is_reported(self, monkeypatch, tmp_path) -> None:
        nested = "frpc 启动失败：" + "；".join(["proxy name [tunnel-1] is duplicated"] * 3)
        self._log(
            monkeypatch,
            tmp_path,
            [
                "=== 启动 2026-11-22 17:38:15 ===",
                "2026-11-22 17:38:16 proxy name [tunnel-1] is duplicated",
                f"2026-11-22 17:38:17 [看护] 自动拉起失败（第 1 次）：{nested}",
                "=== 启动 2026-11-22 17:39:05 ===",
                "2026-11-22 17:39:06 proxy name [tunnel-1] is duplicated",
                f"2026-11-22 17:39:07 [看护] 自动拉起失败（第 2 次）：{nested}",
            ],
        )

        detail = frp._start_output()

        assert "duplicated" in detail
        # 不带上一次的失败记录、也不带任何分隔标记
        assert "第 1 次" not in detail and "第 2 次" not in detail
        assert "===" not in detail
        assert len(detail) <= 400

    def test_empty_or_missing_log_does_not_raise(self, monkeypatch, tmp_path) -> None:
        self._log(monkeypatch, tmp_path, [])
        assert frp._start_output() == ""

        monkeypatch.setattr(frp, "LOG_PATH", tmp_path / "nope.log")
        assert frp._start_output() == ""

    def test_frpc_multiline_output_is_capped(self, monkeypatch, tmp_path) -> None:
        self._log(
            monkeypatch,
            tmp_path,
            ["=== 启动 2026-11-22 17:42:10 ==="] + [f"line {i} " + "x" * 200 for i in range(5)],
        )
        detail = frp._start_output()
        assert len(detail) <= 400


@pytest.fixture(autouse=True)
def _reset_auto_restart(monkeypatch):
    """别让用例之间共享看护的退避状态。"""
    monkeypatch.setattr(frp, "_failures", 0)
    monkeypatch.setattr(frp, "_next_attempt_at", 0.0)
