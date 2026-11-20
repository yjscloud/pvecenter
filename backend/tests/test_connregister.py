"""一键接入：注册码 + 一条命令完成的安装脚本。

覆盖的是「节点脚本拿注册码自助建一条连接」这条链路，它与
``POST /api/connections`` 有本质差别：那条路要登录会话 + step-up，而脚本两样
都没有。这里钉住五件事：

* 一张码只能成功用一次（并发与重放都不行）；
* 探活失败 / 真冲突时**退码**，让运维改完参数直接重跑；
* 同集群换节点重复注册是「跳过」而不是「再建一条」；
* 列表接口与安装脚本接口都不回明文之外的额外信息；
* 安装脚本本身是**能过 ``bash -n`` 的真脚本**，码失效时也是一段会退出 1 的
  脚本，而不是让 bash 去执行一段 JSON。

桩 PVE（``MockPveServer``）没有 ``/cluster/status``，面板会退回「单机指纹」
``standalone:pve1`` —— 这正好给了两条路：同端点的重复注册，以及「同一个集群
换了地址」的判重。
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import connregister, database  # noqa: E402
from app import pve as pve_module  # noqa: E402
from app import store  # noqa: E402
from app.pve import PveConnection  # noqa: E402
from app.config import settings  # noqa: E402

from conftest import connect  # noqa: E402
from test_api_routes import _attach_csrf, auth_headers  # noqa: E402
from test_pve_client import TOKEN_ID, TOKEN_SECRET, MockPveServer  # noqa: E402


async def _shutdown_pool(coro: Any) -> Any:
    """跑完协程顺手关掉连接池（它绑在 asyncio.run 新建的循环上）。"""
    try:
        return await coro
    finally:
        await database.close_pool()


def _run(coro: Any) -> Any:
    return asyncio.run(_shutdown_pool(coro))


def _set_used(code_id: str, used: int) -> None:
    """直接改次数：等价于「已经用掉 N 次」，比反复走注册流程稳定。"""
    conn = connect(settings.db_name)
    try:
        with conn.cursor() as cursor:
            # 注意占位符是 pymysql 的 ``%s``，不是 app 那层封装的 ``?``
            cursor.execute(
                "UPDATE connection_register_codes SET used_count=%s, used=%s"
                " WHERE id=%s",
                (used, int(time.time()) if used else 0, code_id),
            )
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _plain_http_to_mock(monkeypatch) -> None:
    """桩 PVE 是明文 HTTP，而生产上 PVE 一律 https（注册模型也强制不带协议）。

    只在这组用例里把 URL 拼成 http：否则每台「节点」都要先有证书才能被测到。
    """
    monkeypatch.setattr(
        PveConnection,
        "base_url",
        property(lambda self: f"http://{self.host}:{self.port}"),
    )


@pytest.fixture()
def onboard(clean_mysql_db) -> Tuple[TestClient, int]:
    """一个连着桩 PVE 的面板客户端，外加桩的端口（= 注册时填的 host:port）。"""
    with MockPveServer() as server:
        conn = PveConnection(
            host=f"http://127.0.0.1:{server.port}",
            port=server.port,
            token_id=TOKEN_ID,
            token_secret=TOKEN_SECRET,
            verify_ssl=False,
        )
        pve_module._client = pve_module.ProxmoxClient(conn)

        from app.main import app

        with TestClient(app) as client:
            yield _attach_csrf(client), server.port

    pve_module._client = None


@pytest.fixture()
def code_db(clean_mysql_db):
    """只建表、不启动 app：纯数据库层的次数增减用例（同 test_apitokens 的做法）。"""
    _run(store.init_db())
    _run(connregister.init_table())
    yield


class TestUseQuota:
    """次数配额：递减准确、可退回、用尽即拒。"""

    def test_decrement_release_and_exhaust(self, code_db) -> None:
        row = _run(
            connregister.create_code(
                region="", note="", created_by="t", ttl_minutes=5, max_uses=2
            )
        )
        assert row["max_uses"] == 2
        assert row["remaining"] == 2
        assert _run(connregister.peek(row["code"]))["remaining"] == 2

        first = _run(connregister.consume(row["code"], used_by="node-a@10.0.0.1"))
        assert first["used_count"] == 1
        assert _run(connregister.peek(row["code"]))["remaining"] == 1

        # 退回一次：额度回来，且「首次使用时间」也清掉（列表不该显示用过）
        _run(connregister.release(first["id"]))
        info = _run(connregister.peek(row["code"]))
        assert info["remaining"] == 2
        assert info["used"] == 0
        assert info["status"] == "active"

        _run(connregister.consume(row["code"], used_by="a"))
        second = _run(connregister.consume(row["code"], used_by="b"))
        assert second["used_count"] == 2
        # 第三次越界
        assert _run(connregister.consume(row["code"], used_by="c")) is None
        info = _run(connregister.peek(row["code"]))
        assert info["status"] == "exhausted"
        assert info["remaining"] == 0
        assert info["used_count"] == 2

    def test_unlimited_never_exhausts(self, code_db) -> None:
        row = _run(
            connregister.create_code(
                region="", note="", created_by="t", ttl_minutes=5, max_uses=0
            )
        )
        assert row["remaining"] is None
        for index in range(5):
            assert _run(connregister.consume(row["code"], used_by=f"n{index}")) is not None
        info = _run(connregister.peek(row["code"]))
        assert info["used_count"] == 5
        assert info["remaining"] is None
        assert info["status"] == "active"

    def test_single_use_keeps_the_old_status_name(self, code_db) -> None:
        """一次性码用掉之后仍报 ``used``：升级前的前端 / 日志都按这个值判断。"""
        row = _run(
            connregister.create_code(
                region="", note="", created_by="t", ttl_minutes=5, max_uses=1
            )
        )
        _run(connregister.consume(row["code"], used_by="a"))
        info = _run(connregister.peek(row["code"]))
        assert info["status"] == "used"
        assert info["used_count"] == 1
        assert info["remaining"] == 0

    def test_negative_max_uses_is_clamped_to_unlimited(self, code_db) -> None:
        row = _run(
            connregister.create_code(
                region="", note="", created_by="t", ttl_minutes=5, max_uses=-3
            )
        )
        assert row["max_uses"] == 0


def _issue(client: TestClient, auth: Dict[str, str], **body: Any) -> Dict[str, Any]:
    resp = client.post(
        "/api/connections/register-codes", headers=auth, json=body or {}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _register(
    client: TestClient,
    port: int,
    code: str,
    *,
    host: str = "127.0.0.1",
    secret: str = TOKEN_SECRET,
    **extra: Any,
):
    return client.post(
        "/api/connections/register",
        json={
            "code": code,
            "host": host,
            "port": port,
            "token_id": TOKEN_ID,
            "token_secret": secret,
            "verify_ssl": False,
            "node_name": "pve1",
            **extra,
        },
    )


class TestRegisterCode:
    def test_issuer_sees_plaintext_once(self, onboard) -> None:
        client, _ = onboard
        auth = auth_headers(client)

        issued = _issue(client, auth, region="广州", note="批量", ttl_minutes=15)
        assert issued["code"].startswith("pvir_")
        assert issued["region"] == "广州"
        assert issued["ttl_minutes"] == 15

        # 列表只回状态：明文在库里也只有哈希
        rows = client.get("/api/connections/register-codes", headers=auth).json()
        assert len(rows) == 1
        assert "code" not in rows[0]
        assert rows[0]["status"] == "active"
        assert rows[0]["note"] == "批量"

    def test_revoke_makes_code_unusable(self, onboard) -> None:
        client, port = onboard
        auth = auth_headers(client)
        issued = _issue(client, auth)

        resp = client.post(
            f"/api/connections/register-codes/{issued['id']}/revoke", headers=auth
        )
        assert resp.status_code == 200, resp.text

        denied = _register(client, port, issued["code"])
        assert denied.status_code == 403

    def test_revoke_unknown_code_is_404(self, onboard) -> None:
        client, _ = onboard
        auth = auth_headers(client)
        resp = client.post("/api/connections/register-codes/nope/revoke", headers=auth)
        assert resp.status_code == 404


def _fetch_script(client: TestClient, url: str) -> str:
    """按 ``script_url`` 取脚本：只取 path+query，TestClient 的 origin 是 testserver。"""
    parsed = urlsplit(url)
    resp = client.get(f"{parsed.path}?{parsed.query}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers.get("cache-control") == "no-store"
    return resp.text


def _bash_syntax_ok(script: str) -> bool:
    if shutil.which("bash") is None:
        pytest.skip("环境里没有 bash，跳过脚本语法检查")
    proc = subprocess.run(
        ["bash", "-n"], input=script, text=True, capture_output=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return True


def _bash_exit_code(script: str) -> int:
    if shutil.which("bash") is None:
        pytest.skip("环境里没有 bash，跳过脚本执行检查")
    # 只对「失效链接」那一类脚本这么跑：它只有 printf 与 exit，不碰系统
    return subprocess.run(
        ["bash"], input=script, text=True, capture_output=True, check=False
    ).returncode


class TestSchemaMigration:
    """老库升级：多用途之前没有 max_uses / used_count 两列。"""

    def test_old_table_is_upgraded_and_backfilled(self, code_db) -> None:
        conn = connect(settings.db_name)
        with conn.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS connection_register_codes")
            cursor.execute(
                """
                CREATE TABLE connection_register_codes (
                    id            VARCHAR(40)  NOT NULL,
                    code_hash     CHAR(64)     NOT NULL,
                    region        VARCHAR(64)  NOT NULL DEFAULT '',
                    note          VARCHAR(191) NOT NULL DEFAULT '',
                    created_by    VARCHAR(64)  NOT NULL DEFAULT '',
                    created       BIGINT       NOT NULL DEFAULT 0,
                    expires       BIGINT       NOT NULL DEFAULT 0,
                    used          BIGINT       NOT NULL DEFAULT 0,
                    used_by       VARCHAR(191) NOT NULL DEFAULT '',
                    connection_id VARCHAR(40)  NOT NULL DEFAULT '',
                    revoked       TINYINT      NOT NULL DEFAULT 0,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_conn_reg_hash (code_hash)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 一条升级前「已经用过」的码
            cursor.execute(
                "INSERT INTO connection_register_codes (id, code_hash, used)"
                " VALUES ('old-row', 'hash-old', 1795000000)"
            )
        conn.close()

        _run(connregister.init_table())
        # 再跑一次确认幂等（启动会反复执行）
        _run(connregister.init_table())

        conn = connect(settings.db_name)
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT max_uses, used_count FROM connection_register_codes"
                " WHERE id='old-row'"
            )
            row = cursor.fetchone()
        conn.close()
        # 老行按「1 次上限、已用 1 次」补齐，升级后不会突然变回「还能用一次」
        assert row == (1, 1)


class TestUseQuotaHttp:
    """接口层的次数语义：签发带次数、每用一次减一、用尽后拒绝并说明原因。"""

    def test_default_is_single_use(self, onboard) -> None:
        client, _ = onboard
        issued = _issue(client, auth_headers(client))
        assert issued["max_uses"] == 1
        assert issued["used_count"] == 0
        assert issued["remaining"] == 1

    def test_multi_use_decrements_and_repeat_is_free(self, onboard) -> None:
        client, port = onboard
        auth = auth_headers(client)
        issued = _issue(client, auth, max_uses=3)
        assert issued["remaining"] == 3

        first = _register(client, port, issued["code"]).json()
        assert first["status"] == "created"
        assert first["used_count"] == 1
        assert first["remaining"] == 2

        # 同一个集群再跑一次：面板没有任何改动，不该扣次数
        again = _register(client, port, issued["code"]).json()
        assert again["status"] == "unchanged"
        assert again["used_count"] == 1
        assert again["remaining"] == 2

        rows = client.get("/api/connections/register-codes", headers=auth).json()
        assert rows[0]["used_count"] == 1
        assert rows[0]["remaining"] == 2
        assert rows[0]["status"] == "active"

    def test_exhausted_is_rejected_listed_and_deletable(self, onboard) -> None:
        client, port = onboard
        auth = auth_headers(client)
        issued = _issue(client, auth, max_uses=2)
        _set_used(issued["id"], 2)  # 等价于已经成功接了两次

        resp = _register(client, port, issued["code"])
        assert resp.status_code == 403
        assert "已用尽" in resp.json()["detail"]

        rows = client.get("/api/connections/register-codes", headers=auth).json()
        assert rows[0]["status"] == "exhausted"
        assert rows[0]["remaining"] == 0

        # 次数用尽 = 已失效，可以删掉
        assert (
            client.delete(
                f"/api/connections/register-codes/{issued['id']}", headers=auth
            ).status_code
            == 200
        )

    def test_exhausted_link_serves_an_error_script(self, onboard) -> None:
        client, _ = onboard
        issued = _issue(client, auth_headers(client), max_uses=2)
        _set_used(issued["id"], 2)

        body = _fetch_script(client, issued["script_url"])
        assert "已用尽" in body
        assert _bash_exit_code(body) == 1

    def test_unlimited_code_reports_null_remaining(self, onboard) -> None:
        client, port = onboard
        auth = auth_headers(client)
        issued = _issue(client, auth, max_uses=0)
        assert issued["max_uses"] == 0
        assert issued["remaining"] is None

        first = _register(client, port, issued["code"]).json()
        assert first["status"] == "created"
        assert first["used_count"] == 1
        assert first["remaining"] is None

    def test_max_uses_above_the_cap_is_rejected(self, onboard) -> None:
        client, _ = onboard
        resp = client.post(
            "/api/connections/register-codes",
            headers=auth_headers(client),
            json={"max_uses": 5000},
        )
        assert resp.status_code == 422


class TestOnboardScript:
    """一条命令的入口：面板渲染 bash，节点上粘贴执行。"""

    def test_issue_returns_a_pasteable_command(self, onboard) -> None:
        client, _ = onboard
        auth = auth_headers(client)

        issued = _issue(client, auth)
        assert issued["command"].startswith('bash -c "$(curl -fsSL')
        assert "/api/connections/onboard.sh?code=" in issued["script_url"]
        assert issued["code"] in issued["script_url"]
        # 命令里必须带引号：注册码是 URL 安全字符，但 URL 整体不能裸奔给 shell
        assert f"'{issued['script_url']}'" in issued["command"]

    def test_script_is_valid_bash_and_carries_the_code(self, onboard) -> None:
        client, _ = onboard
        issued = _issue(client, auth_headers(client))

        body = _fetch_script(client, issued["script_url"])
        assert issued["code"] in body
        # 关键步骤都在：环境自检、只读账号、令牌、注册、结果确认
        assert "pveversion" in body
        assert "pveum user token add" in body
        assert "/api/connections/register" in body
        assert _bash_syntax_ok(body)

    def test_script_asks_before_touching_pve(self, onboard) -> None:
        """已接入的集群应当在动 pveum 之前就停手，并说清为什么要停下。"""
        client, _ = onboard
        issued = _issue(client, auth_headers(client))
        body = _fetch_script(client, issued["script_url"])

        assert "/api/connections/onboard/preflight" in body
        # 停手之后要给出「同一集群只需接入一次」和「真要用本机地址就加 &force=1」
        assert "无需重复接入" in body
        assert "&force=1" in body
        # 这句判断必须排在第一个真正动 pveum 的调用之前
        assert body.index("onboard/preflight") < body.index("pveum user list")
        assert _bash_syntax_ok(body)

    def test_console_password_rotation_uses_the_api_not_user_modify(self, onboard) -> None:
        """`pveum user modify` 没有 --password（PVE 9 上直接 Unknown option）。"""
        client, _ = onboard
        issued = _issue(client, auth_headers(client))
        body = _fetch_script(client, issued["script_url"])

        # 注释里可以提到它（说明为什么不用），但绝不能真的调用
        assert 'pveum user modify "' not in body
        assert "pveum user modify --password" not in body
        assert "/access/password" in body
        assert _bash_syntax_ok(body)

    def test_script_bakes_panel_and_force(self, onboard) -> None:
        client, _ = onboard
        issued = _issue(client, auth_headers(client))
        body = _fetch_script(client, issued["script_url"] + "&force=1")
        assert "FORCE=1" in body
        assert "http://testserver" in body

    def test_script_pins_the_node_certificate(self, onboard) -> None:
        """面板回连走 https，而 PVE 出厂自签：脚本必须把本机证书带上去钉扎。

        不带 ``ca_cert`` 时面板按 ``PVE_VERIFY_SSL`` 校验，注册必然
        ``CERTIFICATE_VERIFY_FAILED`` —— 部署在真实机房里这几乎是必踩的一条。
        """
        client, _ = onboard
        issued = _issue(client, auth_headers(client))
        body = _fetch_script(client, issued["script_url"])

        assert "openssl s_client -connect '127.0.0.1:8006'" in body
        # -showcerts 不能少：s_client 默认只打印叶子，而叶子是被 PVE 根 CA 签的，
        # 只钉叶子时面板侧建不出链（CERTIFICATE_VERIFY_FAILED）
        assert "-showcerts" in body
        # 根 CA 也要带上，不能指望 s_client 一定吐得出根
        assert "/etc/pve/pve-root-ca.pem" in body
        assert "/etc/pve/local/pve-ssl.pem" in body
        # 拼接必须自己补换行（_pem_add），并在发出去之前就地清洗 + 自证可用：
        # 块粘在一起时 PEM 会整体失效，面板只能回一句看不懂的 500
        assert "_pem_add" in body
        assert "def normalize_pem" in body
        # 证书要随注册请求发上去，且是按证书钉扎（verify_ssl 仍为 True）
        assert 'data["ca_cert"] = ca_pem' in body
        # 取不到证书时不能默认把校验关掉：降级必须由运维显式要求
        assert "PVE_ONBOARD_INSECURE" in body
        assert _bash_syntax_ok(body)

    def test_used_code_serves_an_error_script_not_json(self, onboard) -> None:
        """码用过之后再来下载：仍是脚本，但只打印原因并退出 1。"""
        client, port = onboard
        auth = auth_headers(client)
        issued = _issue(client, auth)
        assert _register(client, port, issued["code"]).status_code == 200

        body = _fetch_script(client, issued["script_url"])
        assert "已经用过" in body
        assert "pveum" not in body
        assert _bash_syntax_ok(body)
        assert _bash_exit_code(body) == 1

    def test_expired_code_serves_an_error_script(self, onboard) -> None:
        client, _ = onboard
        auth = auth_headers(client)
        issued = _issue(client, auth, ttl_minutes=1)

        # 直接把它改成过期：比 sleep 一分钟诚实
        from app import database

        async def _expire() -> None:
            async with database.connect() as db:
                await db.execute(
                    "UPDATE connection_register_codes SET expires=1 WHERE id=?",
                    (issued["id"],),
                )
                await db.commit()

        import asyncio

        # 与面板同一个事件循环由 TestClient 提供，这里借它的 portal 执行
        client.portal.call(_expire)

        body = _fetch_script(client, issued["script_url"])
        assert "已过期" in body
        assert _bash_exit_code(body) == 1

    def test_unknown_code_serves_an_error_script(self, onboard) -> None:
        client, _ = onboard
        body = _fetch_script(
            client, "http://testserver/api/connections/onboard.sh?code=pvir_nope"
        )
        assert "不存在" in body
        assert _bash_exit_code(body) == 1


class TestDeleteRegisterCode:
    """已失效的码可以清掉；仍在有效期的必须先撤销 —— 否则「删了但还能用」。"""

    def test_revoked_code_can_be_deleted(self, onboard) -> None:
        client, _ = onboard
        auth = auth_headers(client)
        issued = _issue(client, auth)
        client.post(
            f"/api/connections/register-codes/{issued['id']}/revoke", headers=auth
        )

        resp = client.delete(
            f"/api/connections/register-codes/{issued['id']}", headers=auth
        )
        assert resp.status_code == 200, resp.text
        assert client.get("/api/connections/register-codes", headers=auth).json() == []

    def test_used_code_can_be_deleted_without_touching_the_connection(self, onboard) -> None:
        client, port = onboard
        auth = auth_headers(client)
        issued = _issue(client, auth)
        created = _register(client, port, issued["code"]).json()

        resp = client.delete(
            f"/api/connections/register-codes/{issued['id']}", headers=auth
        )
        assert resp.status_code == 200, resp.text
        assert client.get("/api/connections/register-codes", headers=auth).json() == []
        # 删的是凭据记录，接入结果还在
        assert store.get_connections()[0]["id"] == created["connection_id"]

    def test_active_code_must_be_revoked_first(self, onboard) -> None:
        client, _ = onboard
        auth = auth_headers(client)
        issued = _issue(client, auth)

        resp = client.delete(
            f"/api/connections/register-codes/{issued['id']}", headers=auth
        )
        assert resp.status_code == 400
        assert "撤销" in resp.json()["detail"]
        rows = client.get("/api/connections/register-codes", headers=auth).json()
        assert rows[0]["status"] == "active"

    def test_unknown_code_is_404(self, onboard) -> None:
        client, _ = onboard
        auth = auth_headers(client)
        resp = client.delete("/api/connections/register-codes/nope", headers=auth)
        assert resp.status_code == 404


class TestRegisterConnection:
    def test_creates_connection_and_activates_it(self, onboard) -> None:
        client, port = onboard
        auth = auth_headers(client)
        issued = _issue(client, auth, region="广州")

        resp = _register(client, port, issued["code"])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "created"
        assert body["cluster_key"] == "standalone:pve1"
        assert body["node_count"] == 1
        # 桩的节点带 maxmem：说明令牌权限够，别报「权限不足」
        assert body["node_metrics"] is True
        # 名字按「机房-集群」兜底，省得几十行都叫 127.0.0.1
        assert body["name"] == "广州-standalone:pve1"

        # 第一条连接必须自动生效，否则面板还停在「未配置」
        assert body["active"] is True
        assert store.get_active_connection_id() == body["connection_id"]

    def test_code_is_single_use(self, onboard) -> None:
        client, port = onboard
        auth = auth_headers(client)
        issued = _issue(client, auth)

        assert _register(client, port, issued["code"]).status_code == 200
        again = _register(client, port, issued["code"])
        assert again.status_code == 403
        assert len(store.get_connections()) == 1

    def test_unknown_code_is_rejected_before_probing(self, onboard) -> None:
        client, port = onboard
        resp = _register(client, port, "pvir_not-a-real-code")
        assert resp.status_code == 403
        assert store.get_connections() == []

    def test_failed_probe_refunds_the_code(self, onboard) -> None:
        """令牌填错不该把码白烧掉：改对参数再跑一次就行。"""
        client, port = onboard
        auth = auth_headers(client)
        issued = _issue(client, auth)

        bad = _register(client, port, issued["code"], secret="wrong-secret")
        assert bad.status_code == 400

        rows = client.get("/api/connections/register-codes", headers=auth).json()
        assert rows[0]["status"] == "active"

        ok = _register(client, port, issued["code"])
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "created"

    def test_same_endpoint_again_is_unchanged(self, onboard) -> None:
        client, port = onboard
        auth = auth_headers(client)

        first = _issue(client, auth)
        created = _register(client, port, first["code"]).json()

        second = _issue(client, auth)
        again = _register(client, port, second["code"])
        assert again.status_code == 200, again.text
        body = again.json()
        assert body["status"] == "unchanged"
        assert body["connection_id"] == created["connection_id"]
        assert len(store.get_connections()) == 1

    def test_force_updates_the_existing_endpoint(self, onboard) -> None:
        client, port = onboard
        auth = auth_headers(client)

        created = _register(client, port, _issue(client, auth)["code"]).json()

        forced = _register(
            client, port, _issue(client, auth)["code"], force_update=True
        )
        assert forced.status_code == 200, forced.text
        body = forced.json()
        assert body["status"] == "updated"
        assert body["connection_id"] == created["connection_id"]
        assert len(store.get_connections()) == 1

    def test_other_node_of_same_cluster_is_skipped(self, onboard) -> None:
        """同集群从另一台节点注册：跳过（不建第二条连接指向同一个集群）。"""
        client, port = onboard
        auth = auth_headers(client)
        first = _register(client, port, _issue(client, auth)["code"]).json()

        with MockPveServer() as other:
            resp = _register(client, other.port, _issue(client, auth)["code"])
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "unchanged"
            assert body["connection_id"] == first["connection_id"]
        assert len(store.get_connections()) == 1

    def test_endpoint_taken_by_another_cluster_conflicts(self, onboard) -> None:
        """同 host:port 但指纹不同 = 两台 PVE 抢地址：409，且把码退回。"""
        client, port = onboard
        auth = auth_headers(client)

        # 直接写一条「占着这个地址、但指纹不同」的连接：不用先跑一次注册，
        # 断言就只依赖本用例造的数据，不会跟着前面的用例漂。
        async def _seed() -> None:
            await store.save_connections(
                [
                    {
                        "id": "seed0001",
                        "name": "老机房-旧连接",
                        "host": "127.0.0.1",
                        "port": port,
                        "token_id": TOKEN_ID,
                        "token_secret": TOKEN_SECRET,
                        "verify_ssl": False,
                        "node_default": "pve1",
                        "cluster_key": "cluster-other",
                    }
                ]
            )

        client.portal.call(_seed)

        issued = _issue(client, auth)
        resp = _register(client, port, issued["code"])
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["connection_id"] == "seed0001"

        rows = client.get("/api/connections/register-codes", headers=auth).json()
        assert rows[0]["status"] == "active"


class TestOnboardPreflight:
    """第二个节点不该再跑一遍：同一集群共享账号与令牌，重跑会把别人的令牌轮换掉。"""

    def test_registered_cluster_is_reported_and_the_code_survives(
        self, onboard
    ) -> None:
        client, port = onboard
        auth = auth_headers(client)
        first = _register(client, port, _issue(client, auth)["code"])
        assert first.status_code == 200
        cluster_key = first.json()["cluster_key"]

        # 另一条链接 + 同一集群的另一台节点
        second = _issue(client, auth)
        resp = client.get(
            "/api/connections/onboard/preflight",
            params={"code": second["code"], "cluster_key": cluster_key},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["registered"] is True
        assert body["connection_id"] == first.json()["connection_id"]
        assert body["name"] and body["host"]

        # 问一句不等于用掉：这条链接还能拿去接别的集群
        rows = client.get("/api/connections/register-codes", headers=auth).json()
        row = next(r for r in rows if r["id"] == second["id"])
        assert row["status"] == "active"
        assert row["used_count"] == 0

    def test_unknown_cluster_is_not_registered(self, onboard) -> None:
        client, _ = onboard
        issued = _issue(client, auth_headers(client))
        resp = client.get(
            "/api/connections/onboard/preflight",
            params={"code": issued["code"], "cluster_key": "standalone:another"},
        )
        assert resp.json()["registered"] is False

    def test_bad_code_is_not_registered(self, onboard) -> None:
        client, _ = onboard
        resp = client.get(
            "/api/connections/onboard/preflight",
            params={"code": "pvir_nope", "cluster_key": "standalone:pve1"},
        )
        assert resp.json() == {"code_ok": False, "registered": False}


class TestOnboardPermissions:
    """一键接入直接把 PVE 的 root 权限交给面板：不建中间账号、不建角色、不写 ACL。"""

    def test_script_uses_the_root_token(self, onboard) -> None:
        client, _ = onboard
        issued = _issue(client, auth_headers(client))
        body = _fetch_script(client, issued["script_url"])

        assert "PVE_USER='root@pam'" in body
        # --privsep 0：令牌继承 root 的全部权限，不再有「特权分离」导致的缺权限
        assert '"$PVE_USER" "$PVE_TOKEN" --privsep 0' in body
        # 中间账号 / 自定义角色 / ACL 那一整套全部退场（只留一句清理旧账号的提示）
        assert 'pveum user add "$PVE_USER"' not in body
        assert "MANAGE_ROLE" not in body
        assert 'pveum acl modify / --users "$PVE_USER"' not in body
        # 运维执行完要看到「能做什么」，而不是一行权限名清单
        assert "  权限      创建 / 克隆 / 开关机 / 快照 / 备份 / 防火墙 / 节点网络" in body
        # root 令牌的风险要在接入完成时就讲清楚，不能只写在文档里
        assert "这份令牌等同 PVE 最高权限" in body
        assert "二次验证（TOTP）" in body
        assert _bash_syntax_ok(body)

    def test_permission_switches_are_gone(self, onboard) -> None:
        client, _ = onboard
        # 老前端缓存仍可能带着旧字段：后端要静默忽略，不能 422
        issued = _issue(client, auth_headers(client), read_only=True)

        assert "read_only" not in issued
        body = _fetch_script(client, issued["script_url"])
        assert "READONLY=" not in body
        assert "只读：只能看" not in body
        assert _bash_syntax_ok(body)

    def test_code_list_no_longer_carries_the_switches(self, onboard) -> None:
        client, _ = onboard
        auth = auth_headers(client)
        issued = _issue(client, auth)

        rows = client.get("/api/connections/register-codes", headers=auth).json()
        row = next(r for r in rows if r["id"] == issued["id"])
        assert "read_only" not in row
        assert "console_enabled" not in row


class TestRegisterTlsHint:
    """自签证书导致的 400，文案必须能直接照做。

    原始报错只有一句 ``unable to get local issuer certificate``，看不出这是
    「证书没钉扎」还是「面板连不上」；而 PVE 出厂自签，这条路上第一次接入
    几乎必踩。
    """

    _TLS_MSG = (
        "Cannot reach Proxmox at https://127.0.0.1:8006: "
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "unable to get local issuer certificate (_ssl.c:1016)"
    )

    def _boom(self, monkeypatch) -> None:
        message = self._TLS_MSG

        async def _raise(client: Any, *args: Any, **kwargs: Any) -> Any:
            raise pve_module.ProxmoxError(message)

        monkeypatch.setattr(pve_module.ProxmoxClient, "version", _raise)

    def test_tls_failure_explains_pinning(self, onboard, monkeypatch) -> None:
        client, port = onboard
        issued = _issue(client, auth_headers(client))
        self._boom(monkeypatch)

        resp = _register(client, port, issued["code"])
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        # 原文保留（排障要看），后面跟上能直接照做的下一步
        assert "CERTIFICATE_VERIFY_FAILED" in detail
        assert "钉扎" in detail
        assert "PVE_ONBOARD_INSECURE" in detail

    def test_unparsable_ca_cert_is_400_not_500(self, onboard) -> None:
        """坏 PEM 必须是 400 + 人话，不能是裸 500。

        真实踩过：命令替换吃掉换行，两段证书粘在一行，面板侧抛
        ``no start line: cadata does not contain a certificate`` 的 500。
        """
        client, port = onboard
        issued = _issue(client, auth_headers(client))

        resp = _register(
            client,
            port,
            issued["code"],
            ca_cert=(
                "-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----"
                "\n-----BEGIN CERTIFICATE-----\nnope2\n-----END CERTIFICATE-----"
            ),
        )
        assert resp.status_code == 400
        assert "证书 PEM 无法解析" in resp.json()["detail"]

    def test_pinned_cert_still_failing_points_at_rotation(
        self, onboard, monkeypatch
    ) -> None:
        """已经带证书还失败：多半是证书换过，提示要指向这一点而不是「没带证书」。"""
        client, port = onboard
        issued = _issue(client, auth_headers(client))
        self._boom(monkeypatch)

        resp = _register(
            client, port, issued["code"], ca_cert="-----BEGIN CERTIFICATE-----x"
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "对不上" in detail
        assert "PVE_ONBOARD_INSECURE" not in detail


class TestConsoleAccount:
    """控制台账号：一律创建 —— PVE 的 VNC 要「账号 + 密码」换票据，令牌换不了。

    哪怕面板手里握着 root 令牌，没有密码账号照样点不开控制台。
    """

    def test_script_always_renders_the_console_steps(self, onboard) -> None:
        client, _ = onboard
        issued = _issue(client, auth_headers(client))

        assert "console_enabled" not in issued
        body = _fetch_script(client, issued["script_url"])
        assert "CONSOLE=" not in body
        assert "pvecenter-console@pve" in body
        # 控制台角色必须自带 VM.Console：光有密码、没有权限，VNC 照样 403
        assert "VM.Console" in body
        # privs 必须是逗号分隔：空格版被 pveum 当成一个非法权限，角色建不出来，
        # 然后 ACL 才报「role does not exist」（PVE 8.4 上踩过）
        assert "VM.Audit,VM.Console" in body
        assert "--privs 'VM.Audit VM.Console'" not in body
        # 角色名不能以 PVE 开头（大小写不敏感）：那是 PVE 的保留命名空间，
        # `pveum role add` 会直接 400（PVECenterConsole 上踩过）
        role = re.search(r"^CONSOLE_ROLE='([^']+)'$", body, re.M).group(1)
        assert not role.lower().startswith("pve"), role
        # 顺序也不能反：先建角色，再授 ACL
        assert body.index("pveum role add") < body.index(
            'pveum acl modify / --users "$CONSOLE_USER" --roles "$CONSOLE_ROLE"'
        )
        assert _bash_syntax_ok(body)

    def test_credentials_are_always_stored(self, onboard) -> None:
        """控制台凭据无条件接收：签发时已经没有「是否启用控制台」可勾。"""
        client, port = onboard
        issued = _issue(client, auth_headers(client))

        resp = _register(
            client,
            port,
            issued["code"],
            console_user="pvecenter-console@pve",
            console_password="random-pw",
        )
        assert resp.status_code == 200
        conn = store.get_connections()[0]
        assert conn["console_user"] == "pvecenter-console@pve"
        assert conn["console_password"] == "random-pw"


class TestRegenerateFlow:
    """额度还在的码要能重新拿到一条命令。

    明文只在签发时回一次，所以「继续用」的实现是「按剩余额度再签一张 + 作废旧码」。
    这里钉住支撑它的那一步：**用过一部分次数的码也必须撤销得掉** —— 早先
    ``revoke`` 带着 ``used=0`` 条件，1/3 次的那种码根本作废不了。
    """

    def test_revoke_works_after_partial_use(self, onboard) -> None:
        client, port = onboard
        auth = auth_headers(client)
        issued = _issue(client, auth, max_uses=3)
        assert _register(client, port, issued["code"]).status_code == 200

        rows = client.get("/api/connections/register-codes", headers=auth).json()
        assert rows[0]["status"] == "active"
        assert rows[0]["remaining"] == 2

        resp = client.post(
            f"/api/connections/register-codes/{issued['id']}/revoke", headers=auth
        )
        assert resp.status_code == 200, resp.text

        rows = client.get("/api/connections/register-codes", headers=auth).json()
        assert rows[0]["status"] == "revoked"
        # 撤销即刻生效：同一个链接再来取的就是「已撤销」的提示脚本
        body = _fetch_script(client, issued["script_url"])
        assert "已被撤销" in body
        assert _bash_exit_code(body) == 1


class TestRepairPermissions:
    """「修复权限」不再走进「用受限账号去改 ACL」的死路。

    旧版一键接入建的账号刻意不含 ``Permissions.Modify``（CenterPanel 角色、
    控制台账号只有 VM.Audit + VM.Console），拿它去 ``PUT /access/acl`` 只会得到
    ``Permission check failed (/, Permissions.Modify)``。现在的行为是：
    全权令牌直接回「无需修复」，受限令牌给一条能照做的指引。
    """

    @staticmethod
    def _repair(client: TestClient, auth: Dict[str, str]):
        conn_id = str(store.get_connections()[0]["id"])
        return client.post(
            f"/api/connections/{conn_id}/repair-permissions", headers=auth
        )

    def test_full_privilege_token_needs_no_repair(self, onboard, monkeypatch) -> None:
        import test_pve_client as pve_stub

        # 全权令牌：/ 上带 Permissions.Modify（root@pam + --privsep 0 的形态）
        monkeypatch.setitem(
            pve_stub.PERMISSIONS, "/", ["Sys.Audit", "VM.Audit", "Permissions.Modify"]
        )
        client, port = onboard
        auth = auth_headers(client)
        issued = _issue(client, auth)
        assert _register(client, port, issued["code"]).status_code == 200

        resp = self._repair(client, auth)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["already_ok"] is True
        assert "Permissions.Modify@/" in body["effective_privileges"]

    def test_limited_account_gets_an_actionable_error(self, onboard) -> None:
        client, port = onboard
        auth = auth_headers(client)
        issued = _issue(client, auth)
        # 注册时带上旧版控制台账号：它只有 VM.Audit + VM.Console，改不了 ACL
        assert (
            _register(
                client,
                port,
                issued["code"],
                console_user="pvecenter-console@pve",
                console_password="random-pw",
            ).status_code
            == 200
        )

        resp = self._repair(client, auth)
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        # 说清楚「该干什么」，而不是把 PVE 的 403 原样抛给用户
        assert "一键接入" in detail
        assert "Permission check failed" not in detail
