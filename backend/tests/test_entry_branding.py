"""入口页品牌注入：标题 / 描述 / 图标 / lang 都要跟着站点信息走。

背景：``index.html`` 是构建产物，标题被写死成 ``pvecenter``；浏览器先渲染它，
等 React 起来、``/api/config/site`` 回来才换成真实站点名 —— 标签页肉眼可见地
闪一下，图标还会从空白变成自定义 Logo。服务端在返回入口页时直接替换，
首帧就是对的。

这些用例只碰字符串和缓存，不需要 PVE，也不需要真实网络。
"""
from __future__ import annotations

import asyncio

import pytest
from starlette.requests import Request

from app import main, site

TEMPLATE = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="description" content="pvecenter - Proxmox VE 集群管理面板" />
    <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
    <title>pvecenter — Proxmox VE 管理面板</title>
  </head>
  <body><div id="root"></div></body>
</html>
"""


def _dist(tmp_path, monkeypatch, html: str = TEMPLATE):
    """把 DIST_DIR 指到临时目录，避免依赖前端构建产物。"""
    (tmp_path / "index.html").write_text(html, encoding="utf-8")
    monkeypatch.setattr(main, "DIST_DIR", tmp_path)
    monkeypatch.setattr(main, "_index_cache", {"mtime": -1.0, "text": ""})
    return tmp_path


def _request(accept_language: str = "") -> Request:
    headers = [(b"accept-language", accept_language.encode())] if accept_language else []
    return Request({"type": "http", "method": "GET", "headers": headers})


# ------------------------------------------------------------ 标题与描述
def test_title_and_description_follow_site_name(tmp_path, monkeypatch) -> None:
    _dist(tmp_path, monkeypatch)

    out = main._render_index("zh", "章北海的云管平台", None)

    assert "<title>章北海的云管平台 — Proxmox VE 管理面板</title>" in out
    assert 'content="章北海的云管平台 - Proxmox VE 集群管理面板"' in out
    # 写死的旧标题不能残留在页面里
    assert "<title>pvecenter" not in out


def test_english_title_from_accept_language(tmp_path, monkeypatch) -> None:
    _dist(tmp_path, monkeypatch)

    assert main._request_lang(_request("en-US,en;q=0.9")) == "en"
    assert main._request_lang(_request("zh-CN,zh;q=0.9")) == "zh"
    assert main._request_lang(_request()) == "zh"

    out = main._render_index("en", "My Panel", None)
    assert "<title>My Panel — Proxmox VE management panel</title>" in out
    assert '<html lang="en">' in out


def test_site_name_is_escaped(tmp_path, monkeypatch) -> None:
    _dist(tmp_path, monkeypatch)

    out = main._render_index("zh", "<script>alert(1)</script>", None)

    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out


# ------------------------------------------------------------------ 图标
def test_logo_replaces_icon_without_type_attribute(tmp_path, monkeypatch) -> None:
    _dist(tmp_path, monkeypatch)

    out = main._render_index("zh", "面板", "/api/config/site/logo?v=1234")

    assert '<link rel="icon" href="/api/config/site/logo?v=1234">' in out
    # 自定义 Logo 可能是 PNG / ICO，保留构建时的 svg 类型声明会误导浏览器
    assert 'type="image/svg+xml"' not in out


def test_missing_icon_link_gets_injected(tmp_path, monkeypatch) -> None:
    html = TEMPLATE.replace('<link rel="icon" href="/favicon.svg" type="image/svg+xml" />', "")
    _dist(tmp_path, monkeypatch, html)

    out = main._render_index("zh", "面板", "/api/config/site/logo?v=99")

    assert '<link rel="icon" href="/api/config/site/logo?v=99">' in out
    assert out.index('rel="icon"') < out.index("</head>")


def test_default_icon_kept_without_logo(tmp_path, monkeypatch) -> None:
    _dist(tmp_path, monkeypatch)

    out = main._render_index("zh", "面板", None)

    assert '<link rel="icon" href="/favicon.svg" type="image/svg+xml" />' in out


# -------------------------------------------------- 站点信息（前端 initialData）
def test_site_meta_injected_for_initial_data(tmp_path, monkeypatch) -> None:
    """前端用这三个 meta 做 React Query 的 initialData，首帧才不会闪兜底名。"""
    _dist(tmp_path, monkeypatch)

    out = main._render_index("zh", "章北海的云管平台", "/logo.svg?v=7", "集群管理")

    assert '<meta name="site-name" content="章北海的云管平台">' in out
    assert '<meta name="site-subtitle" content="集群管理">' in out
    assert '<meta name="site-logo" content="/logo.svg?v=7">' in out
    assert out.index('name="site-name"') < out.index("</head>")


def test_stale_template_gets_site_meta_added(tmp_path, monkeypatch) -> None:
    """旧构建产物里没有 site-* 声明，注入时要补出来，不必等重新构建。"""
    _dist(tmp_path, monkeypatch)

    out = main._render_index("zh", "面板", None, "")

    assert '<meta name="site-name" content="面板">' in out
    assert '<meta name="site-subtitle" content="">' in out
    assert '<meta name="site-logo" content="">' in out


def test_site_meta_is_escaped(tmp_path, monkeypatch) -> None:
    _dist(tmp_path, monkeypatch)

    out = main._render_index("zh", 'a" b', None, 'x" onload="y')

    assert '" onload="' not in out.split('name="site-subtitle"')[1][:80]


# ------------------------------------------------------- 读不到时的兜底
def test_missing_template_returns_empty(tmp_path, monkeypatch) -> None:
    """模板读不出来时返回空串，调用方据此退回原文件，而不是吐空页面。"""
    monkeypatch.setattr(main, "DIST_DIR", tmp_path)
    monkeypatch.setattr(main, "_index_cache", {"mtime": -1.0, "text": ""})

    assert main._render_index("zh", "面板", None) == ""


def test_branding_falls_back_when_db_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        main, "_brand_cache", {"at": -1e9, "rev": -1, "name": "pvecenter", "logo": None}
    )

    async def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(site, "get_site_info", boom)

    name, subtitle, logo = asyncio.run(main._site_branding())
    assert name == "pvecenter"
    assert subtitle == ""
    assert logo is None


def test_branding_cache_invalidated_by_revision(monkeypatch) -> None:
    monkeypatch.setattr(
        main, "_brand_cache", {"at": -1e9, "rev": -1, "name": "pvecenter", "logo": None}
    )
    monkeypatch.setattr(site, "brand_revision", 0)
    calls = {"n": 0}

    async def fake():
        calls["n"] += 1
        return {"name": f"面板{calls['n']}", "logo_url": None}

    monkeypatch.setattr(site, "get_site_info", fake)

    assert asyncio.run(main._site_branding())[0] == "面板1"

    # 同名未变更：命中缓存，不再查库
    assert asyncio.run(main._site_branding())[0] == "面板1"
    assert calls["n"] == 1

    # 后台改了站点名：revision 变化后立刻重读，不必等缓存过期
    monkeypatch.setattr(site, "brand_revision", 1)
    assert asyncio.run(main._site_branding())[0] == "面板2"
    assert calls["n"] == 2
