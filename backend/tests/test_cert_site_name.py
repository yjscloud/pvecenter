"""站点名称不能被「部署目标名称」覆盖。

回归点：``normalise_site`` 末尾会把第一个目标的字段同步成站点级字段（列表页摘要
与旧接口都读那几个键）。目标里也有 ``name``（默认 ``primary`` 或空串），漏排除
就会把用户在「站点名称」里填的值冲掉 —— 保存成功、读回来是空的。

    ../.venv/bin/python -m pytest tests/test_cert_site_name.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import certs  # noqa: E402

DOMAIN = "pve.yjscloud.com"


def _payload(name: str, target_name: str = "") -> dict:
    return {
        "name": name,
        "domain": DOMAIN,
        "deploy_method": "local",
        "deploy_dir": "/etc/nginx/ssl/" + DOMAIN,
        "ca": "tencent",
        "targets": [
            {
                "id": "primary",
                "name": target_name,
                "method": "local",
                "deploy_dir": "/etc/nginx/ssl/" + DOMAIN,
            }
        ],
    }


def test_site_name_survives_target_sync():
    """目标名为空串时，站点名称必须保留。"""
    site = certs.normalise_site(_payload("我的证书站点"), None)
    assert site["name"] == "我的证书站点"


def test_site_name_not_replaced_by_primary():
    """目标名是 ``primary`` 时同样不能覆盖站点名称。"""
    site = certs.normalise_site(_payload("我的证书站点", target_name="primary"), None)
    assert site["name"] == "我的证书站点"


def test_site_name_falls_back_to_domain_when_empty():
    """站点名留空时仍按域名兜底（原有行为不变）。"""
    site = certs.normalise_site(_payload(""), None)
    assert site["name"] == DOMAIN


def test_target_fields_still_synced_to_site():
    """除 id/name 外的目标字段仍然要同步（部署目录等）。"""
    site = certs.normalise_site(_payload("站点"), None)
    assert site["deploy_dir"] == "/etc/nginx/ssl/" + DOMAIN
    assert site["deploy_method"] == "local"
    assert site["targets"][0]["id"] == "primary"
