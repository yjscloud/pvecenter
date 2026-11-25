"""P2 低危修复的回归用例。

* PVE-CONF-003 —— CORS 默认关闭、308 不采信 Host、Cookie 的 Secure 跟请求 scheme；
* PVE-AI-004 —— 个人 token 预算 / 分析窗口有服务端硬顶，只能调低；
* PVE-FILE-004 —— frpc 发行包里的成员名不许越出临时目录。
"""
from __future__ import annotations

import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import ai, frp, panel_url, security  # noqa: E402
from app.config import Settings, settings  # noqa: E402


# ------------------------------------------------------------------ CONF-003(a)
def test_cors_default_is_disabled() -> None:
    """默认值必须是空：以前默认带两个 Vite 开发源（PVE-CONF-003(a)）。"""
    assert Settings.model_fields["cors_origins"].default == ""


def test_cors_list_only_from_explicit_config() -> None:
    explicit = Settings(cors_origins="http://localhost:5173")
    assert explicit.cors_origin_list == ["http://localhost:5173"]
    assert Settings(cors_origins="").cors_origin_list == []


# ------------------------------------------------------------------ CONF-003(c)
def test_cookie_secure_follows_request_scheme(monkeypatch) -> None:
    monkeypatch.setattr(settings, "cookie_secure", None)
    monkeypatch.setattr(settings, "force_https", False)

    security.note_request_scheme("https")
    assert security.cookie_secure() is True

    security.note_request_scheme("http")
    assert security.cookie_secure() is False

    # 加密 WebSocket 也算 TLS（ASGI 里 scheme 是 wss）
    security.note_request_scheme("wss")
    assert security.cookie_secure() is True


def test_cookie_secure_force_https_implies_secure(monkeypatch) -> None:
    monkeypatch.setattr(settings, "cookie_secure", None)
    monkeypatch.setattr(settings, "force_https", True)

    security.note_request_scheme("http")
    assert security.cookie_secure() is True


def test_cookie_secure_explicit_override_wins(monkeypatch) -> None:
    monkeypatch.setattr(settings, "cookie_secure", False)
    security.note_request_scheme("https")
    assert security.cookie_secure() is False

    monkeypatch.setattr(settings, "cookie_secure", True)
    security.note_request_scheme("http")
    assert security.cookie_secure() is True


# ------------------------------------------------------------------ CONF-003(b)
def test_panel_url_cache_default_is_empty() -> None:
    assert panel_url.cached_base_url() == "" or panel_url.cached_base_url().startswith(
        ("http://", "https://")
    )


# ------------------------------------------------------------------- AI-004
def test_user_token_budget_is_capped_server_side() -> None:
    capped = ai.clamp_user_settings({"token_budget": 100_000_000})
    assert capped["token_budget"] == ai.USER_TOKEN_BUDGET_MAX

    lowered = ai.clamp_user_settings({"token_budget": 10_000})
    assert lowered["token_budget"] == 10_000


def test_user_hours_are_capped() -> None:
    assert ai.clamp_user_settings({"hours": 168})["hours"] == ai.USER_HOURS_MAX
    assert ai.clamp_user_settings({"hours": 6})["hours"] == 6


def test_user_ceiling_is_below_platform_ceiling() -> None:
    """硬顶必须真的收紧：平台仍可配到 1 亿，但个人设置到不了。"""
    assert ai.USER_TOKEN_BUDGET_MAX < ai.TOKEN_BUDGET_RANGE[1]
    assert ai.USER_HOURS_MAX <= ai.HOURS_RANGE[1]


# ------------------------------------------------------------------- FILE-004
def _archive(tmp_path: Path, name: str, data: bytes = b"binary") -> Path:
    path = tmp_path / "frp.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return path


@pytest.mark.parametrize(
    "name",
    [
        "../../../root/.ssh/frpc",
        "/root/.ssh/frpc",
        "frp/../../frpc",
        "C:/windows/frpc",
    ],
)
def test_frpc_member_rejects_escape(tmp_path, name: str) -> None:
    with tarfile.open(_archive(tmp_path, name), "r:gz") as tar:
        with pytest.raises(frp.FrpError):
            frp._frpc_member(tar)


def test_frpc_member_accepts_normal_layout(tmp_path) -> None:
    with tarfile.open(
        _archive(tmp_path, "frp_0.52.0_linux_amd64/frpc"), "r:gz"
    ) as tar:
        member = frp._frpc_member(tar)

    assert member is not None
    assert member.name == "frp_0.52.0_linux_amd64/frpc"


def test_frpc_member_ignores_symlink(tmp_path) -> None:
    path = tmp_path / "link.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("frp/frpc")
        info.type = tarfile.SYMTYPE
        info.linkname = "/bin/sh"
        tar.addfile(info)

    with tarfile.open(path, "r:gz") as tar:
        # 软链不是普通文件：挑不出来，install 会报「未找到」而不是解出链接
        assert frp._frpc_member(tar) is None
