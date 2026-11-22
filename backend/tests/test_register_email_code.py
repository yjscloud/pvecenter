"""自助注册的邮箱验证码：发码、限频、有效期、错误次数与重发。

这些用例跑的是真实路由 + 真实 MySQL（见 ``conftest``），只把「投递」这一步
换成内存里的假邮件 —— 验证码的生成、落库、比对、作废都在真路径上。
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import mailer, store  # noqa: E402
from app.config import settings  # noqa: E402

from conftest import connect  # noqa: E402

EMAIL = "alice@example.com"
PASSWORD = "Str0ng-Passw0rd!x"


@pytest.fixture()
def box(clean_mysql_db, monkeypatch) -> Iterator[Tuple[TestClient, Dict[str, Any]]]:
    """自助注册 + 邮箱验证码全开，邮件投递打到内存。"""
    monkeypatch.setattr(settings, "register_enabled", True)
    monkeypatch.setattr(settings, "register_email_code", True)
    monkeypatch.setattr(settings, "register_email_code_ttl_minutes", 10)
    monkeypatch.setattr(settings, "register_email_code_max_attempts", 5)
    monkeypatch.setattr(settings, "register_email_code_resend_seconds", 60)

    mailbox: Dict[str, Any] = {}

    async def _fake_send(
        to: str, subject: str, body: str, html: str | None = None
    ) -> Tuple[bool, str]:
        mailbox["to"] = to
        mailbox["subject"] = subject
        mailbox["body"] = body
        match = re.search(r"\b(\d{6})\b", body)
        mailbox["code"] = match.group(1) if match else ""
        mailbox["html"] = html or ""
        return True, ""

    monkeypatch.setattr(mailer, "send_mail", _fake_send)

    from app.main import app

    with TestClient(app) as client:
        yield _attach_csrf(client), mailbox


def _attach_csrf(client: TestClient) -> TestClient:
    """像浏览器一样给写请求补上 CSRF 头（登录后 cookie 里才有那枚令牌）。"""
    for method in ("post", "put", "patch", "delete"):
        original = getattr(client, method)

        def wrapper(*args: Any, _original=original, **kwargs: Any):
            headers = dict(kwargs.pop("headers", None) or {})
            if "X-CSRF-Token" not in headers:
                value = client.cookies.get("panel_csrf", "")
                if value:
                    headers["X-CSRF-Token"] = value
            return _original(*args, headers=headers, **kwargs)

        setattr(client, method, wrapper)
    return client


def _send(client: TestClient, email: str = EMAIL):
    return client.post("/api/auth/register/email-code", json={"email": email})


def _register(client: TestClient, username: str, code: str = "", email: str = EMAIL):
    return client.post(
        "/api/auth/register",
        json={
            "username": username,
            "password": PASSWORD,
            "email": email,
            "email_code": code,
        },
    )


def _sql(statement: str, args: tuple = ()) -> None:
    """直接改库：模拟「时间过去了」（过期 / 冷却结束）比等待更可靠。"""
    conn = connect(settings.db_name)
    try:
        with conn.cursor() as cursor:
            cursor.execute(statement, args)
        conn.commit()
    finally:
        conn.close()


def _age_last_code(email: str, seconds: float) -> None:
    _sql(
        "UPDATE email_verification_codes SET created = created - %s,"
        " expires = expires - %s WHERE email = %s",
        (seconds, seconds, email),
    )


def _user_status(username: str) -> str:
    conn = connect(settings.db_name)
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT status FROM users WHERE username = %s", (username,))
            row = cursor.fetchone()
        return str(row[0]) if row else ""
    finally:
        conn.close()


class TestSend:
    def test_sends_six_digit_code(self, box):
        client, mailbox = box
        resp = _send(client)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["ttl_seconds"] == 600
        assert data["max_attempts"] == 5
        assert data["resend_after"] == 60
        assert mailbox["to"] == EMAIL
        assert re.fullmatch(r"\d{6}", mailbox["code"])
        # 主题里也带一份码：邮件列表折叠正文时用户要能直接看到
        assert mailbox["code"] in mailbox["subject"]

    def test_mail_body_uses_the_attempt_limit(self, box):
        """正文里的「最多可以输错 N 次」必须是数字。

        曾经把 ``purpose`` 当第三个位置参数传进去，邮件里写成
        「最多可以输错 register 次」—— 位置参数一错位，用户看到的是一次
        内部变量的泄露，而不是限额。
        """
        client, mailbox = box
        assert _send(client).status_code == 200
        assert "最多可以输错 5 次" in mailbox["body"]
        assert "10 分钟内有效" in mailbox["body"]
        assert "register" not in mailbox["body"]
        assert "最多可以输错 5 次" in mailbox["html"]

    def test_resend_is_cooled_down(self, box):
        client, _ = box
        assert _send(client).status_code == 200
        again = _send(client)
        assert again.status_code == 429
        assert int(again.headers["Retry-After"]) > 0
        assert "秒后再试" in again.json()["detail"]

    def test_hourly_limit_per_email(self, box, monkeypatch):
        client, _ = box
        monkeypatch.setattr(settings, "register_email_code_hourly_limit", 2)
        monkeypatch.setattr(settings, "register_email_code_resend_seconds", 0)
        assert _send(client).status_code == 200
        assert _send(client).status_code == 200
        blocked = _send(client)
        assert blocked.status_code == 429
        assert "次数过多" in blocked.json()["detail"]

    def test_rejects_when_mode_is_not_self_service(self, box, monkeypatch):
        client, _ = box
        monkeypatch.setattr(settings, "register_enabled", False)
        resp = _send(client)
        assert resp.status_code == 403

    def test_mail_failure_does_not_leave_a_usable_code(self, box, monkeypatch):
        client, _ = box

        async def _broken(*args: Any, **kwargs: Any) -> Tuple[bool, str]:
            return False, "smtp down"

        monkeypatch.setattr(mailer, "send_mail", _broken)
        resp = _send(client)
        assert resp.status_code == 503
        assert "发送失败" in resp.json()["detail"]
        # 冷却不该被一条没发出去的码占住
        row_created = None
        conn = connect(settings.db_name)
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS n FROM email_verification_codes")
                row_created = cursor.fetchone()[0]
        finally:
            conn.close()
        assert row_created == 0


class TestVerify:
    def test_valid_code_completes_registration(self, box):
        client, mailbox = box
        assert _send(client).status_code == 200
        resp = _register(client, "alice", mailbox["code"])
        assert resp.status_code == 201, resp.text
        assert _user_status("alice") == "pending"

    def test_registration_without_code_is_rejected(self, box):
        client, _ = box
        _send(client)
        resp = _register(client, "alice", "")
        assert resp.status_code == 400
        assert "请先获取邮箱验证码" in resp.json()["detail"]

    def test_wrong_code_counts_down_attempts(self, box):
        client, mailbox = box
        _send(client)
        wrong = "000000" if mailbox["code"] != "000000" else "111111"
        first = _register(client, "alice", wrong)
        assert first.status_code == 400
        assert "还可尝试 4 次" in first.json()["detail"]
        second = _register(client, "alice", wrong)
        assert "还可尝试 3 次" in second.json()["detail"]
        # 真码仍然可用
        assert _register(client, "alice", mailbox["code"]).status_code == 201

    def test_attempts_exhausted_destroys_the_code(self, box, monkeypatch):
        client, mailbox = box
        monkeypatch.setattr(settings, "register_email_code_max_attempts", 2)
        _send(client)
        wrong = "000000" if mailbox["code"] != "000000" else "111111"
        assert _register(client, "alice", wrong).status_code == 400
        burned = _register(client, "alice", wrong)
        assert burned.status_code == 400
        assert "错误次数过多" in burned.json()["detail"]
        # 码已销毁：再填正确的也只会被告知「请先获取」
        after = _register(client, "alice", mailbox["code"])
        assert after.status_code == 400
        assert "请先获取邮箱验证码" in after.json()["detail"]

    def test_expired_code_asks_to_request_a_new_one(self, box):
        client, mailbox = box
        _send(client)
        _sql(
            "UPDATE email_verification_codes SET expires = %s WHERE email = %s",
            (time.time() - 1, EMAIL),
        )
        resp = _register(client, "alice", mailbox["code"])
        assert resp.status_code == 400
        assert "已过期" in resp.json()["detail"]

    def test_new_code_invalidates_the_previous_one(self, box):
        client, mailbox = box
        _send(client)
        old_code = mailbox["code"]
        _age_last_code(EMAIL, 120)  # 冷却已过，可以再发
        assert _send(client).status_code == 200
        new_code = mailbox["code"]
        assert new_code != old_code

        stale = _register(client, "alice", old_code)
        assert stale.status_code == 400
        assert _register(client, "alice", new_code).status_code == 201

    def test_code_cannot_be_reused(self, box):
        client, mailbox = box
        _send(client)
        assert _register(client, "alice", mailbox["code"]).status_code == 201
        again = _register(client, "bob", mailbox["code"])
        assert again.status_code == 400
        assert "请先获取邮箱验证码" in again.json()["detail"]

    def test_code_is_bound_to_the_email(self, box):
        client, mailbox = box
        _send(client)
        resp = _register(client, "alice", mailbox["code"], email="bob@example.com")
        assert resp.status_code == 400
        assert "请先获取" in resp.json()["detail"]

    def test_email_matching_ignores_case(self, box):
        client, mailbox = box
        assert _send(client, "Alice@Example.COM").status_code == 200
        resp = _register(client, "alice", mailbox["code"], email="alice@example.com")
        assert resp.status_code == 201, resp.text


def _admin_headers(client: TestClient) -> Dict[str, str]:
    resp = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": settings.admin_password},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


class TestToggle:
    """设置页的开关：关掉退回「只要人机校验」，再打开又要求验证码。"""

    def test_off_then_on(self, box):
        client, mailbox = box
        headers = _admin_headers(client)

        overview = client.get("/api/config/registration", headers=headers).json()
        assert overview["email_code_enabled"] is True
        assert client.get("/api/auth/registration-mode").json()["email_code"]["required"] is True

        off = client.put(
            "/api/config/registration/email-code",
            json={"enabled": False},
            headers=headers,
        )
        assert off.status_code == 200, off.text
        assert off.json()["email_code_enabled"] is False
        # 公开接口立刻跟着变：注册页据此把整块验证码收起来
        assert client.get("/api/auth/registration-mode").json()["email_code"]["required"] is False
        # 关掉后发码接口直接拒绝，注册不需要码
        assert _send(client).status_code == 400
        assert _register(client, "alice").status_code == 201

        on = client.put(
            "/api/config/registration/email-code",
            json={"enabled": True},
            headers=headers,
        )
        assert on.json()["email_code_enabled"] is True
        assert _register(client, "bob").status_code == 400
        assert "请先获取邮箱验证码" in _register(client, "bob").json()["detail"]

    def test_toggle_needs_authentication(self, box):
        client, _ = box
        resp = client.put(
            "/api/config/registration/email-code", json={"enabled": False}
        )
        assert resp.status_code in (401, 403)

    def test_toggle_does_not_affect_invite_mode(self, box, monkeypatch):
        """邀请制本来就不看这个开关：关掉它不该让邀请注册多出一步。"""
        client, _ = box
        headers = _admin_headers(client)
        client.put(
            "/api/config/registration/email-code",
            json={"enabled": False},
            headers=headers,
        )
        conn = connect(settings.db_name)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO settings (`key`, value) VALUES ('registration_mode', 'invite')"
                    " ON DUPLICATE KEY UPDATE value = 'invite'"
                )
            conn.commit()
        finally:
            conn.close()
        store.clear_settings_cache()

        info = client.get("/api/auth/registration-mode").json()
        assert info["mode"] == "invite"
        assert info["email_code"]["required"] is False


class TestModeInfo:
    def test_registration_mode_exposes_code_policy(self, box):
        client, _ = box
        info = client.get("/api/auth/registration-mode").json()
        assert info["mode"] == "open"
        assert info["email_code"] == {
            "required": True,
            "ttl_seconds": 600,
            "max_attempts": 5,
            "resend_after": 60,
        }

    def test_invite_mode_does_not_require_code(self, box, monkeypatch):
        client, _ = box
        monkeypatch.setattr(settings, "register_enabled", False)
        # 邀请制下注册不需要邮箱码，注册方式接口也应当如实说明
        conn = connect(settings.db_name)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO settings (`key`, value) VALUES ('registration_mode', 'invite')"
                    " ON DUPLICATE KEY UPDATE value = 'invite'"
                )
            conn.commit()
        finally:
            conn.close()
        store.clear_settings_cache()

        info = client.get("/api/auth/registration-mode").json()
        assert info["mode"] == "invite"
        assert info["email_code"]["required"] is False
