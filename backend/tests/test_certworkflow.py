"""证书工作流定义层的回归测试（纯逻辑，不碰数据库）。

覆盖三件事：
1. 内置模板与节点目录本身必须通过自己的校验（否则「从模板创建」一点就报错）；
2. 导入解析：YAML / JSON 的成功路径与错误定位（行号、扩展名、空文件）；
3. 定义校验：未知类型、重复 id、缺失依赖、成环、必填参数、域名 / 邮箱格式、
   条件字段（target_type=local 时 host 不再是必填）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import certworkflow  # noqa: E402


def _messages(issues):
    return [item["message"] for item in issues]


def _paths(issues):
    return [item.get("path") for item in issues]


def test_templates_are_valid_and_canonical():
    for template in certworkflow.templates():
        definition, errors, warnings = certworkflow.normalize(
            {"name": template["key"], "nodes": template["nodes"]}
        )
        assert errors == [], (template["key"], errors)
        assert warnings == []
        assert definition is not None
        ids = [node["id"] for node in definition["nodes"]]
        assert len(ids) == len(set(ids))
        for node in definition["nodes"]:
            assert node["type"] in certworkflow.NODE_TYPES
            assert isinstance(node["position"], dict)


def test_node_catalog_exposes_form_contract():
    types = {item["type"]: item for item in certworkflow.node_types()}
    assert set(types) == set(certworkflow.NODE_TYPES)
    domain = types["domain_monitor"]["fields"]
    by_key = {field["key"]: field for field in domain}
    assert by_key["domain"]["required"] is True
    assert by_key["domain"]["format"] == "domain"
    assert by_key["port"]["default"] == 443


def test_linear_needs_default_to_previous_node():
    raw = {
        "name": "demo",
        "nodes": [
            {"type": "domain_monitor", "options": {"domain": "example.com"}},
            {"type": "cert_apply", "options": {"domains": ["example.com"]}},
            {"type": "notification", "options": {"channels": ["inapp"]}},
        ],
    }
    definition, errors, _ = certworkflow.normalize(raw)
    assert errors == []
    first, second, third = definition["nodes"]
    assert first["needs"] == []
    assert second["needs"] == [first["id"]]
    assert third["needs"] == [second["id"]]


def test_explicit_empty_needs_marks_root():
    raw = {
        "name": "demo",
        "nodes": [
            {"id": "a", "type": "domain_monitor", "options": {"domain": "a.com"}},
            {
                "id": "b",
                "type": "domain_monitor",
                "needs": [],
                "options": {"domain": "b.com"},
            },
        ],
    }
    definition, errors, _ = certworkflow.normalize(raw)
    assert errors == []
    assert definition["nodes"][1]["needs"] == []


def test_unknown_type_lists_supported_types():
    _, errors, _ = certworkflow.normalize(
        {"name": "x", "nodes": [{"type": "nope"}]}
    )
    message = _messages(errors)[0]
    assert "不支持的节点类型" in message
    assert "domain_monitor" in message
    assert _paths(errors)[0] == "nodes[0].type"


def test_duplicate_id_and_unknown_dependency():
    raw = {
        "name": "x",
        "nodes": [
            {"id": "a", "type": "domain_monitor", "options": {"domain": "a.com"}},
            {"id": "a", "type": "notification", "options": {"channels": ["inapp"]}},
        ],
    }
    _, errors, _ = certworkflow.normalize(raw)
    assert any("重复" in message for message in _messages(errors))

    raw = {
        "name": "x",
        "nodes": [
            {
                "id": "b",
                "type": "notification",
                "needs": ["missing"],
                "options": {"channels": ["inapp"]},
            }
        ],
    }
    _, errors, _ = certworkflow.normalize(raw)
    assert any("不存在的节点" in message for message in _messages(errors))


def test_cycle_is_rejected():
    raw = {
        "name": "x",
        "nodes": [
            {
                "id": "a",
                "type": "domain_monitor",
                "needs": ["b"],
                "options": {"domain": "a.com"},
            },
            {
                "id": "b",
                "type": "notification",
                "needs": ["a"],
                "options": {"channels": ["inapp"]},
            },
        ],
    }
    _, errors, _ = certworkflow.normalize(raw)
    assert any("成环" in message for message in _messages(errors))


def test_required_option_reports_path():
    raw = {
        "name": "x",
        "nodes": [{"id": "monitor", "type": "domain_monitor", "options": {}}],
    }
    _, errors, _ = certworkflow.normalize(raw)
    assert _paths(errors) == ["nodes[0].options.domain"]


def test_conditional_required_field_follows_target_type():
    base = {
        "id": "deploy",
        "type": "deploy_upload",
        "options": {"path": "/etc/ssl"},
    }
    local = dict(base, options={"target_type": "local", "path": "/etc/ssl"})
    _, errors, _ = certworkflow.normalize({"name": "x", "nodes": [local]})
    assert errors == []

    ssh = dict(base, options={"target_type": "ssh", "path": "/etc/ssl"})
    _, errors, _ = certworkflow.normalize({"name": "x", "nodes": [ssh]})
    assert any("host" in (item.get("path") or "") for item in errors)


def test_option_choice_number_range_and_format():
    raw = {
        "name": "x",
        "nodes": [
            {
                "id": "monitor",
                "type": "domain_monitor",
                "options": {"domain": "not a domain", "port": 70000},
            },
            {
                "id": "apply",
                "type": "cert_apply",
                "options": {"domains": ["ok.com"], "provider": "unknown"},
            },
        ],
    }
    _, errors, _ = certworkflow.normalize(raw)
    messages = _messages(errors)
    assert any("不是合法域名" in message for message in messages)
    assert any("之间" in message for message in messages)
    assert any("取值无效" in message for message in messages)


def test_unknown_fields_become_warnings_not_errors():
    raw = {
        "name": "x",
        "extra": 1,
        "nodes": [
            {
                "id": "m",
                "type": "domain_monitor",
                "foo": "bar",
                "options": {"domain": "a.com", "bogus": 1},
            }
        ],
    }
    definition, errors, warnings = certworkflow.normalize(raw)
    assert errors == []
    messages = _messages(warnings)
    assert any("extra" in message for message in messages)
    assert any("foo" in message for message in messages)
    assert any("bogus" in message for message in messages)
    # 未知参数保留，避免「新版本导入旧版本」时静默丢配置
    assert definition["nodes"][0]["options"]["bogus"] == 1


def test_parse_yaml_with_default_linear_flow():
    content = """name: demo
description: 监控并通知
nodes:
  - type: monitor
    options:
      domain: example.com
  - type: notify
    options:
      channels: [inapp, email]
      recipients: ops@example.com
"""
    raw, errors, fmt = certworkflow.parse_document(content, "demo.yaml")
    assert errors == [] and fmt == "yaml"
    definition, errors, _ = certworkflow.normalize(
        raw, filename="demo.yaml", allow_empty=False
    )
    assert errors == []
    assert [node["type"] for node in definition["nodes"]] == [
        "domain_monitor",
        "notification",
    ]
    assert definition["nodes"][1]["needs"] == [definition["nodes"][0]["id"]]


def test_parse_json_and_missing_name_falls_back_to_filename():
    content = json.dumps(
        {
            "nodes": [
                {"type": "domain_monitor", "options": {"domain": "example.com"}}
            ]
        }
    )
    raw, errors, fmt = certworkflow.parse_document(content, "site-a.json")
    assert errors == [] and fmt == "json"
    definition, errors, warnings = certworkflow.normalize(
        raw, filename="site-a.json", allow_empty=False
    )
    assert errors == []
    assert definition["name"] == "site-a"
    assert any("文件名" in item["message"] for item in warnings)


def test_parse_yaml_error_carries_line_number():
    _, errors, fmt = certworkflow.parse_document("name: x\nnodes: [", "bad.yaml")
    assert fmt == "yaml"
    assert errors[0]["line"] == 2
    assert "YAML 解析失败" in errors[0]["message"]


def test_parse_json_error_carries_line_number():
    _, errors, fmt = certworkflow.parse_document('{"name": "x",}', "bad.json")
    assert fmt == "json"
    assert errors[0]["line"] == 1
    assert "JSON 解析失败" in errors[0]["message"]


def test_parse_rejects_wrong_extension_empty_and_oversized():
    _, errors, _ = certworkflow.parse_document("name: x", "demo.txt")
    assert "仅支持导入" in errors[0]["message"]

    _, errors, _ = certworkflow.parse_document("   \n", "demo.yaml")
    assert "为空" in errors[0]["message"]

    huge = "name: x\nnodes: []\n#" + "a" * (certworkflow.MAX_FILE_BYTES + 10)
    _, errors, _ = certworkflow.parse_document(huge, "demo.yaml")
    assert "超过" in errors[0]["message"]


def test_parse_rejects_multiple_yaml_documents():
    content = "name: a\nnodes: []\n---\nname: b\nnodes: []\n"
    _, errors, _ = certworkflow.parse_document(content, "multi.yaml")
    assert "多个 YAML 文档" in errors[0]["message"]


def test_import_requires_at_least_one_node():
    _, errors, _ = certworkflow.normalize(
        {"name": "x", "nodes": []}, allow_empty=False
    )
    assert any("至少需要一个节点" in message for message in _messages(errors))


def test_blank_workflow_is_allowed_but_name_is_required():
    definition, errors, _ = certworkflow.normalize(
        {"name": "空白", "nodes": []}, allow_empty=True
    )
    assert errors == [] and definition["nodes"] == []

    _, errors, _ = certworkflow.normalize({"name": "", "nodes": []})
    assert any("名称" in message for message in _messages(errors))


def test_unique_name_suffixes():
    existing = {"demo", "demo (2)"}
    assert certworkflow.unique_name(existing, "other") == "other"
    assert certworkflow.unique_name(existing, "demo") == "demo (3)"


def test_dump_and_reimport_roundtrip():
    template = certworkflow.templates()[0]
    definition, _, _ = certworkflow.normalize(
        {"name": "roundtrip", "nodes": template["nodes"]}
    )
    for fmt in ("json", "yaml"):
        text = certworkflow.dump_document(definition, fmt)
        filename = f"roundtrip.{fmt}"
        raw, errors, detected = certworkflow.parse_document(text, filename)
        assert errors == [] and detected == fmt
        again, errors, _ = certworkflow.normalize(
            raw, filename=filename, allow_empty=False
        )
        assert errors == []
        assert again["nodes"] == definition["nodes"]
