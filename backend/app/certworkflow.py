"""证书工作流：节点目录、定义校验、YAML/JSON 导入与内置模板。

与 :mod:`app.certflow` 的分工
-----------------------------
``certflow`` 是**运行期引擎**：把一份写死在代码里的续期流程拆成节点并执行，
它的定义来自 ``certs.RENEW_FLOW``，不面向用户编辑。

本模块管的是**用户可编辑的工作流定义**：节点类型目录、参数校验、文件导入。
它刻意不碰数据库、网络与执行器 —— 这样导入校验可以纯函数单测，新增节点类型
也只需要在 :data:`NODE_SCHEMAS` 里补一条（前端会从 ``/node-types`` 自动渲染
参数表单），不用改页面结构。

定义格式
--------
JSON 与 YAML 共用同一套结构：

```yaml
name: 标准证书流程
description: 监控域名 → 申请证书 → 部署 → 通知
nodes:
  - id: monitor
    type: domain_monitor
    title: 监控域名
    options: {domain: example.com, port: 443}
    needs: []
    position: {x: 40, y: 80}
  - id: apply
    type: cert_apply
    options: {domains: [example.com]}
    needs: [monitor]
```

``needs`` 省略时默认连到上一个节点（手写 YAML 时不必为线性流程重复写依赖），
显式写 ``needs: []`` 才是根节点。``position`` 省略时由后端按顺序生成一个不重叠
的初始布局，导入后仍可在画布上任意拖动。
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from . import i18n

try:  # pragma: no cover - 缺依赖时导入接口会给出明确错误
    import yaml
except Exception:  # noqa: BLE001
    yaml = None  # type: ignore[assignment]

#: 单个工作流的节点上限。编排画布上超过这个数量已经无法阅读，也挡住恶意文件。
MAX_NODES = 64
#: 名称 / 描述与数据库列长度一致（``cert_workflow.name`` / ``description``）。
MAX_NAME = 128
MAX_DESCRIPTION = 512
MAX_NODE_ID = 64
MAX_NODE_TITLE = 80
#: 导入文件大小上限（1 MB）。工作流是文本配置，正常只有几 KB。
MAX_FILE_BYTES = 1024 * 1024
#: 允许导入的扩展名。没有扩展名（例如粘贴内容）时按内容嗅探。
ALLOWED_EXTENSIONS = (".yaml", ".yml", ".json")

ORIGIN_TEMPLATE = "template"
ORIGIN_BLANK = "blank"
ORIGIN_IMPORT = "import"
ORIGIN_EDITOR = "editor"
ORIGINS = (ORIGIN_TEMPLATE, ORIGIN_BLANK, ORIGIN_IMPORT, ORIGIN_EDITOR)


# --------------------------------------------------------------------- 节点目录
def _field(
    key: str,
    kind: str,
    label_key: str,
    *,
    default: Any = None,
    required: bool = False,
    hint_key: str = "",
    placeholder: str = "",
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    choices: Optional[List[Dict[str, str]]] = None,
    show_if: Optional[Dict[str, Any]] = None,
    fmt: str = "",
) -> Dict[str, Any]:
    """构造一条参数描述。

    ``show_if`` 只影响**必填判定与表单可见性**：被隐藏的字段不会进入归一化结果，
    这样「部署方式从 SSH 切回本机」之后，残留的 host 不会继续触发校验错误。
    """
    item: Dict[str, Any] = {
        "key": key,
        "type": kind,
        "label_key": label_key,
        "required": bool(required),
    }
    if default is not None:
        item["default"] = default
    if hint_key:
        item["hint_key"] = hint_key
    if placeholder:
        item["placeholder"] = placeholder
    if minimum is not None:
        item["min"] = minimum
    if maximum is not None:
        item["max"] = maximum
    if choices:
        item["choices"] = choices
    if show_if:
        item["show_if"] = show_if
    if fmt:
        item["format"] = fmt
    return item


def _choices(pairs: List[Tuple[str, str]]) -> List[Dict[str, str]]:
    return [{"value": value, "label_key": label} for value, label in pairs]


PROVIDER_CHOICES = _choices(
    [
        ("acme", "workflow.option.provider.acme"),
        ("tencent", "workflow.option.provider.tencent"),
        ("aliyun", "workflow.option.provider.aliyun"),
        ("huawei", "workflow.option.provider.huawei"),
    ]
)
KEY_CHOICES = _choices(
    [
        ("rsa2048", "workflow.option.key.rsa2048"),
        ("rsa4096", "workflow.option.key.rsa4096"),
        ("ec256", "workflow.option.key.ec256"),
        ("ec384", "workflow.option.key.ec384"),
    ]
)
TARGET_CHOICES = _choices(
    [
        ("local", "workflow.option.target.local"),
        ("ssh", "workflow.option.target.ssh"),
    ]
)
CHANNEL_CHOICES = _choices(
    [
        ("inapp", "workflow.option.channel.inapp"),
        ("email", "workflow.option.channel.email"),
        ("webhook", "workflow.option.channel.webhook"),
        ("feishu", "workflow.option.channel.feishu"),
    ]
)
EVENT_CHOICES = _choices(
    [
        ("success", "workflow.option.event.success"),
        ("failure", "workflow.option.event.failure"),
        ("expiring", "workflow.option.event.expiring"),
    ]
)

#: 节点类型 → 表单与校验契约。
#:
#: 新增节点类型的步骤：
#: 1. 在这里补一条 ``type``（``fields`` 会被前端直接渲染成参数面板）；
#: 2. 在 i18n 里补 ``workflow.node.<type>`` / ``.desc`` 与各字段的 ``label_key``；
#: 3. 执行器侧按 ``type`` 注册处理函数（本模块不负责执行）。
NODE_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "domain_monitor": {
        "type": "domain_monitor",
        "label_key": "workflow.node.domain_monitor",
        "desc_key": "workflow.node.domain_monitor.desc",
        "fields": [
            _field(
                "domain",
                "text",
                "workflow.field.domain",
                required=True,
                hint_key="workflow.field.domain.hint",
                placeholder="example.com",
                fmt="domain",
            ),
            _field("port", "number", "workflow.field.port", default=443, minimum=1, maximum=65535),
            _field(
                "interval_hours",
                "number",
                "workflow.field.intervalHours",
                default=6,
                minimum=1,
                maximum=720,
                hint_key="workflow.field.intervalHours.hint",
            ),
            _field(
                "expire_warn_days",
                "number",
                "workflow.field.expireWarnDays",
                default=30,
                minimum=1,
                maximum=180,
                hint_key="workflow.field.expireWarnDays.hint",
            ),
            _field("check_dns", "switch", "workflow.field.checkDns", default=True),
        ],
    },
    "cert_apply": {
        "type": "cert_apply",
        "label_key": "workflow.node.cert_apply",
        "desc_key": "workflow.node.cert_apply.desc",
        "fields": [
            _field(
                "domains",
                "stringlist",
                "workflow.field.domains",
                required=True,
                hint_key="workflow.field.domains.hint",
                placeholder="example.com\n*.example.com",
                fmt="domain",
            ),
            _field(
                "provider",
                "select",
                "workflow.field.provider",
                default="acme",
                choices=PROVIDER_CHOICES,
            ),
            _field(
                "key_algorithm",
                "select",
                "workflow.field.keyAlgorithm",
                default="rsa2048",
                choices=KEY_CHOICES,
            ),
            _field(
                "renew_before_days",
                "number",
                "workflow.field.renewBeforeDays",
                default=30,
                minimum=1,
                maximum=90,
            ),
            _field("email", "text", "workflow.field.email", default="", fmt="email"),
        ],
    },
    "deploy_upload": {
        "type": "deploy_upload",
        "label_key": "workflow.node.deploy_upload",
        "desc_key": "workflow.node.deploy_upload.desc",
        "fields": [
            _field(
                "target_type",
                "select",
                "workflow.field.targetType",
                default="ssh",
                choices=TARGET_CHOICES,
            ),
            _field(
                "path",
                "text",
                "workflow.field.path",
                required=True,
                hint_key="workflow.field.path.hint",
                placeholder="/etc/nginx/ssl",
            ),
            _field(
                "host",
                "text",
                "workflow.field.host",
                required=True,
                show_if={"key": "target_type", "in": ["ssh"]},
                placeholder="10.0.0.10",
            ),
            _field(
                "port",
                "number",
                "workflow.field.port",
                default=22,
                minimum=1,
                maximum=65535,
                show_if={"key": "target_type", "in": ["ssh"]},
            ),
            _field(
                "username",
                "text",
                "workflow.field.username",
                default="root",
                show_if={"key": "target_type", "in": ["ssh"]},
                hint_key="workflow.field.username.hint",
            ),
            _field(
                "reload_command",
                "text",
                "workflow.field.reloadCommand",
                default="",
                placeholder="nginx -s reload",
                hint_key="workflow.field.reloadCommand.hint",
            ),
        ],
    },
    "notification": {
        "type": "notification",
        "label_key": "workflow.node.notification",
        "desc_key": "workflow.node.notification.desc",
        "fields": [
            _field(
                "channels",
                "multiselect",
                "workflow.field.channels",
                required=True,
                default=["inapp"],
                choices=CHANNEL_CHOICES,
            ),
            _field(
                "recipients",
                "stringlist",
                "workflow.field.recipients",
                default=[],
                required=True,
                show_if={"key": "channels", "contains": "email"},
                hint_key="workflow.field.recipients.hint",
                placeholder="ops@example.com",
                fmt="email",
            ),
            _field(
                "webhook_url",
                "text",
                "workflow.field.webhookUrl",
                default="",
                required=True,
                show_if={"key": "channels", "contains": "webhook"},
                placeholder="https://example.com/hooks/cert",
                fmt="url",
            ),
            _field(
                "events",
                "multiselect",
                "workflow.field.events",
                default=["success", "failure"],
                choices=EVENT_CHOICES,
            ),
            _field("topic", "text", "workflow.field.topic", default=""),
        ],
    },
}

NODE_TYPES: Tuple[str, ...] = tuple(NODE_SCHEMAS)

#: 兼容常见写法（手工 YAML / 从其它工具导出的文件）。统一转成规范 type。
TYPE_ALIASES: Dict[str, str] = {
    "monitor": "domain_monitor",
    "domain-monitor": "domain_monitor",
    "domainmonitor": "domain_monitor",
    "domain": "domain_monitor",
    "apply": "cert_apply",
    "issue": "cert_apply",
    "cert-apply": "cert_apply",
    "certapply": "cert_apply",
    "applycert": "cert_apply",
    "deploy": "deploy_upload",
    "upload": "deploy_upload",
    "deploy-upload": "deploy_upload",
    "deployupload": "deploy_upload",
    "notify": "notification",
    "notification": "notification",
    "send-notification": "notification",
}

_DOMAIN_RE = re.compile(
    r"^(?:\*\.)?(?:[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_NODE_ID_BAD = re.compile(r"[^A-Za-z0-9_.-]+")
_TOP_LEVEL_KEYS = {"name", "description", "nodes", "version"}


def node_types() -> List[Dict[str, Any]]:
    """返回节点类型目录的深拷贝 —— 调用方改返回值不会污染全局契约。"""
    return [copy.deepcopy(NODE_SCHEMAS[key]) for key in NODE_TYPES]


def _issue(message: str, path: str = "", line: int = 0) -> Dict[str, Any]:
    item: Dict[str, Any] = {"message": message, "path": path}
    if line:
        item["line"] = line
    return item


# --------------------------------------------------------------------- 模板
def templates() -> List[Dict[str, Any]]:
    """内置工作流模板。

    只给节点骨架与可直接运行的默认参数，名称 / 描述用词条键返回，由前端按当前
    语言渲染。模板创建后仍是一份普通定义，可以任意改节点与参数。
    """
    return [
        {
            "key": "standard",
            "name_key": "workflow.template.standard.name",
            "description_key": "workflow.template.standard.desc",
            "nodes": [
                {
                    "id": "monitor",
                    "type": "domain_monitor",
                    "title": "",
                    "needs": [],
                    "position": {"x": 40, "y": 80},
                    "options": {
                        "domain": "example.com",
                        "port": 443,
                        "interval_hours": 6,
                        "expire_warn_days": 30,
                        "check_dns": True,
                    },
                },
                {
                    "id": "apply",
                    "type": "cert_apply",
                    "title": "",
                    "needs": ["monitor"],
                    "position": {"x": 320, "y": 80},
                    "options": {
                        "domains": ["example.com"],
                        "provider": "acme",
                        "key_algorithm": "rsa2048",
                        "renew_before_days": 30,
                        "email": "",
                    },
                },
                {
                    "id": "deploy",
                    "type": "deploy_upload",
                    "title": "",
                    "needs": ["apply"],
                    "position": {"x": 600, "y": 80},
                    "options": {
                        "target_type": "ssh",
                        "path": "/etc/nginx/ssl",
                        "host": "10.0.0.10",
                        "port": 22,
                        "username": "root",
                        "reload_command": "nginx -s reload",
                    },
                },
                {
                    "id": "notify",
                    "type": "notification",
                    "title": "",
                    "needs": ["deploy"],
                    "position": {"x": 880, "y": 80},
                    "options": {
                        "channels": ["inapp"],
                        "recipients": [],
                        "webhook_url": "",
                        "events": ["success", "failure"],
                        "topic": "",
                    },
                },
            ],
        },
        {
            "key": "domain-monitor",
            "name_key": "workflow.template.domainMonitor.name",
            "description_key": "workflow.template.domainMonitor.desc",
            "nodes": [
                {
                    "id": "monitor",
                    "type": "domain_monitor",
                    "title": "",
                    "needs": [],
                    "position": {"x": 40, "y": 80},
                    "options": {
                        "domain": "example.com",
                        "port": 443,
                        "interval_hours": 6,
                        "expire_warn_days": 30,
                        "check_dns": True,
                    },
                },
                {
                    "id": "notify",
                    "type": "notification",
                    "title": "",
                    "needs": ["monitor"],
                    "position": {"x": 320, "y": 80},
                    "options": {
                        "channels": ["inapp"],
                        "recipients": [],
                        "webhook_url": "",
                        "events": ["expiring", "failure"],
                        "topic": "",
                    },
                },
            ],
        },
    ]


# --------------------------------------------------------------------- 导入解析
def detect_format(filename: str, content: str) -> str:
    """按扩展名判定格式；没有扩展名时按内容嗅探。返回 ``json`` / ``yaml`` / ``""``。"""
    suffix = Path(filename or "").suffix.lower()
    if suffix:
        if suffix == ".json":
            return "json"
        if suffix in {".yaml", ".yml"}:
            return "yaml"
        return ""
    return "json" if content.lstrip()[:1] in {"{", "["} else "yaml"


def parse_document(
    content: str, filename: str = ""
) -> Tuple[Optional[Any], List[Dict[str, Any]], str]:
    """把文件内容解析成 Python 对象。

    返回 ``(raw, errors, fmt)``。解析失败时 ``raw`` 为 ``None``，错误里带行号
    （JSON 的 line/column、YAML 的 problem_mark），前端可以据此直接定位。
    """
    errors: List[Dict[str, Any]] = []
    if not str(content or "").strip():
        errors.append(_issue(i18n.t("workflow.error.empty_file")))
        return None, errors, ""

    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        errors.append(
            _issue(
                i18n.t("workflow.error.too_large", limit=str(MAX_FILE_BYTES // 1024))
            )
        )
        return None, errors, ""

    fmt = detect_format(filename, content)
    if not fmt:
        errors.append(
            _issue(i18n.t("workflow.error.file_type", extensions=".yaml / .yml / .json"))
        )
        return None, errors, ""

    if fmt == "json":
        try:
            return json.loads(content), errors, fmt
        except json.JSONDecodeError as exc:
            line = int(getattr(exc, "lineno", 0) or 0)
            col = int(getattr(exc, "colno", 0) or 0)
            detail = str(getattr(exc, "msg", "") or exc)
            suffix = f"（第 {line} 行第 {col} 列）" if line else ""
            errors.append(
                _issue(
                    i18n.t("workflow.error.json_parse", detail=detail) + suffix,
                    line=line,
                )
            )
            return None, errors, fmt

    if yaml is None:  # pragma: no cover - 依赖缺失时才会走到
        errors.append(_issue(i18n.t("workflow.error.yaml_unavailable")))
        return None, errors, fmt
    try:
        documents = [
            doc for doc in yaml.safe_load_all(content) if doc is not None
        ]
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = int(getattr(mark, "line", 0) or 0) + 1 if mark is not None else 0
        col = int(getattr(mark, "column", 0) or 0) + 1 if mark is not None else 0
        detail = str(getattr(exc, "problem", "") or "").strip() or str(exc).strip()
        suffix = f"（第 {line} 行第 {col} 列）" if line else ""
        errors.append(
            _issue(i18n.t("workflow.error.yaml_parse", detail=detail) + suffix, line=line)
        )
        return None, errors, fmt
    if len(documents) > 1:
        errors.append(_issue(i18n.t("workflow.error.multiple_docs")))
        return None, errors, fmt
    return (documents[0] if documents else None), errors, fmt


# --------------------------------------------------------------------- 校验
def _visible(field: Dict[str, Any], values: Dict[str, Any]) -> bool:
    cond = field.get("show_if")
    if not cond:
        return True
    key = str(cond.get("key") or "")
    actual = values.get(key)
    if "in" in cond:
        wanted = cond.get("in") or []
        return actual in wanted
    if "contains" in cond:
        needle = cond.get("contains")
        if isinstance(actual, (list, tuple, set)):
            return needle in actual
        if isinstance(actual, str):
            return needle in [part.strip() for part in re.split(r"[,，\n]", actual)]
        return False
    if "truthy" in cond:
        return bool(actual) is bool(cond.get("truthy"))
    return True


def _as_list(value: Any) -> Optional[List[Any]]:
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,，\n]", value) if part.strip()]
    return None


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"true", "1", "yes", "on", "是"}:
            return True
        if token in {"false", "0", "no", "off", "否"}:
            return False
    return None


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _check_format(fmt: str, value: str) -> bool:
    token = str(value or "").strip()
    if not token:
        return True
    if fmt == "domain":
        return bool(_DOMAIN_RE.match(token))
    if fmt == "email":
        return bool(_EMAIL_RE.match(token))
    if fmt == "url":
        parsed = urlparse(token)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    return True


def _normalize_options(
    schema: Dict[str, Any],
    raw_options: Any,
    node_id: str,
    base_path: str,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    if raw_options is None:
        raw_options = {}
    if not isinstance(raw_options, dict):
        errors.append(
            _issue(
                i18n.t("workflow.error.options_type", node=node_id),
                f"{base_path}.options",
            )
        )
        return {}, errors, warnings

    fields: List[Dict[str, Any]] = schema.get("fields") or []
    known = {str(field.get("key")) for field in fields}
    for key in raw_options:
        if key not in known:
            warnings.append(
                _issue(
                    i18n.t("workflow.warning.unknown_field", field=str(key)),
                    f"{base_path}.options.{key}",
                )
            )

    # 先算出「可见性」需要读的值：缺失时用默认值，否则 target_type 还没写入时
    # 条件字段会被误判为不可见。
    source: Dict[str, Any] = {}
    for field in fields:
        key = str(field.get("key"))
        source[key] = raw_options.get(key, field.get("default"))

    normalized: Dict[str, Any] = {}
    for field in fields:
        key = str(field.get("key"))
        path = f"{base_path}.options.{key}"
        if not _visible(field, source):
            continue
        value = raw_options.get(key, copy.deepcopy(field.get("default")))
        kind = str(field.get("type") or "text")
        required = bool(field.get("required"))
        empty = value is None or (isinstance(value, str) and not value.strip())

        if kind == "number":
            if empty:
                if required:
                    errors.append(
                        _issue(
                            i18n.t("workflow.error.option_required", node=node_id, field=key),
                            path,
                        )
                    )
                continue
            number = _as_number(value)
            if number is None:
                errors.append(
                    _issue(
                        i18n.t("workflow.error.option_number", node=node_id, field=key),
                        path,
                    )
                )
                continue
            minimum = field.get("min")
            maximum = field.get("max")
            if minimum is not None and number < float(minimum):
                errors.append(
                    _issue(
                        i18n.t(
                            "workflow.error.option_range",
                            node=node_id,
                            field=key,
                            min=str(minimum),
                            max=str(maximum),
                        ),
                        path,
                    )
                )
                continue
            if maximum is not None and number > float(maximum):
                errors.append(
                    _issue(
                        i18n.t(
                            "workflow.error.option_range",
                            node=node_id,
                            field=key,
                            min=str(minimum),
                            max=str(maximum),
                        ),
                        path,
                    )
                )
                continue
            normalized[key] = int(number) if float(number).is_integer() else number
            continue

        if kind == "switch":
            if empty:
                normalized[key] = bool(field.get("default"))
                continue
            flag = _as_bool(value)
            if flag is None:
                errors.append(
                    _issue(
                        i18n.t("workflow.error.option_invalid", node=node_id, field=key, value=str(value)),
                        path,
                    )
                )
                continue
            normalized[key] = flag
            continue

        if kind in {"stringlist", "multiselect"}:
            items = _as_list(value)
            if items is None:
                errors.append(
                    _issue(
                        i18n.t("workflow.error.option_list", node=node_id, field=key),
                        path,
                    )
                )
                continue
            cleaned: List[str] = []
            for item in items:
                text = str(item).strip()
                if text and text not in cleaned:
                    cleaned.append(text)
            if required and not cleaned:
                errors.append(
                    _issue(
                        i18n.t("workflow.error.option_required", node=node_id, field=key),
                        path,
                    )
                )
                continue
            choices = field.get("choices") or []
            if choices:
                allowed = {str(choice.get("value")) for choice in choices}
                bad = next((item for item in cleaned if item not in allowed), "")
                if bad:
                    errors.append(
                        _issue(
                            i18n.t(
                                "workflow.error.option_invalid",
                                node=node_id,
                                field=key,
                                value=bad,
                            ),
                            path,
                        )
                    )
                    continue
            fmt = str(field.get("format") or "")
            if fmt:
                bad = next((item for item in cleaned if not _check_format(fmt, item)), "")
                if bad:
                    errors.append(
                        _issue(
                            i18n.t(
                                f"workflow.error.{fmt}_invalid",
                                node=node_id,
                                field=key,
                                value=bad,
                            ),
                            path,
                        )
                    )
                    continue
            normalized[key] = cleaned
            continue

        # text / select
        text = "" if value is None else str(value).strip()
        if required and not text:
            errors.append(
                _issue(
                    i18n.t("workflow.error.option_required", node=node_id, field=key),
                    path,
                )
            )
            continue
        if kind == "select" and text:
            choices = field.get("choices") or []
            allowed = {str(choice.get("value")) for choice in choices}
            if allowed and text not in allowed:
                errors.append(
                    _issue(
                        i18n.t(
                            "workflow.error.option_invalid",
                            node=node_id,
                            field=key,
                            value=text,
                        ),
                        path,
                    )
                )
                continue
        fmt = str(field.get("format") or "")
        if text and not _check_format(fmt, text):
            errors.append(
                _issue(
                    i18n.t(
                        f"workflow.error.{fmt}_invalid",
                        node=node_id,
                        field=key,
                        value=text,
                    ),
                    path,
                )
            )
            continue
        normalized[key] = text

    # 未识别的参数原样保留：向前兼容（新版本导入到旧版本时不至于丢配置），
    # 但发一条 warning，让用户知道这个字段当前没有生效。
    for key, value in raw_options.items():
        if key not in known:
            normalized[str(key)] = value
    return normalized, errors, warnings


def _auto_position(index: int) -> Dict[str, int]:
    return {"x": 40 + (index % 4) * 280, "y": 80 + (index // 4) * 160}


def _normalize_node(
    raw: Any,
    index: int,
    seen_ids: Dict[str, int],
    previous_id: str,
    allow_empty: bool,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    path = f"nodes[{index}]"
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    if not isinstance(raw, dict):
        errors.append(_issue(i18n.t("workflow.error.node_object"), path))
        return None, errors, warnings

    raw_type = str(raw.get("type") or "").strip()
    if not raw_type:
        errors.append(_issue(i18n.t("workflow.error.node_type_required"), f"{path}.type"))
        return None, errors, warnings
    canonical = TYPE_ALIASES.get(raw_type.lower(), raw_type)
    if canonical not in NODE_SCHEMAS:
        errors.append(
            _issue(
                i18n.t(
                    "workflow.error.unknown_type",
                    type=raw_type,
                    supported=", ".join(NODE_TYPES),
                ),
                f"{path}.type",
            )
        )
        return None, errors, warnings

    node_id = str(raw.get("id") or "").strip()
    if not node_id:
        base = _NODE_ID_BAD.sub("_", canonical).strip("_") or "node"
        node_id = f"{base}_{index + 1}"
        while node_id in seen_ids:
            node_id = f"{node_id}_x"
        warnings.append(
            _issue(
                i18n.t("workflow.warning.generated_id", id=node_id),
                f"{path}.id",
            )
        )
    if not _NODE_ID_RE.match(node_id):
        errors.append(
            _issue(i18n.t("workflow.error.id_invalid", id=node_id), f"{path}.id")
        )
        return None, errors, warnings
    if node_id in seen_ids:
        errors.append(
            _issue(i18n.t("workflow.error.duplicate_id", id=node_id), f"{path}.id")
        )
        return None, errors, warnings

    for key in raw:
        if key not in {"id", "type", "title", "options", "needs", "position"}:
            warnings.append(
                _issue(
                    i18n.t("workflow.warning.unknown_field", field=str(key)),
                    f"{path}.{key}",
                )
            )

    options, option_errors, option_warnings = _normalize_options(
        NODE_SCHEMAS[canonical], raw.get("options"), node_id, path
    )
    errors.extend(option_errors)
    warnings.extend(option_warnings)

    if "needs" in raw and raw.get("needs") is not None:
        items = _as_list(raw.get("needs"))
        if items is None:
            errors.append(
                _issue(i18n.t("workflow.error.needs_type", node=node_id), f"{path}.needs")
            )
            needs: List[str] = []
        else:
            needs = []
            for item in items:
                dep = str(item).strip()
                if dep and dep not in needs:
                    needs.append(dep)
    else:
        # 手写 YAML 的线性流程通常不写 needs：默认接上一个节点。
        needs = [previous_id] if previous_id else []

    title = str(raw.get("title") or "").strip()[:MAX_NODE_TITLE]

    position_raw = raw.get("position")
    position: Dict[str, float] = {}
    if isinstance(position_raw, dict):
        x = _as_number(position_raw.get("x"))
        y = _as_number(position_raw.get("y"))
        if x is not None and y is not None:
            position = {"x": round(float(x), 1), "y": round(float(y), 1)}
    if not position:
        position = _auto_position(index)

    node = {
        "id": node_id,
        "type": canonical,
        "title": title,
        "options": options,
        "needs": needs,
        "position": position,
    }
    return node, errors, warnings


def _has_cycle(nodes: List[Dict[str, Any]]) -> List[str]:
    """Kahn 拓扑排序，返回仍留在环里的节点 id（无环时为空）。"""
    by_id = {node["id"]: node for node in nodes}
    indegree = {node["id"]: 0 for node in nodes}
    children: Dict[str, List[str]] = {node["id"]: [] for node in nodes}
    for node in nodes:
        for dep in node.get("needs") or []:
            if dep in by_id:
                indegree[node["id"]] += 1
                children[dep].append(node["id"])
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    ordered: List[str] = []
    while ready:
        node_id = ready.pop(0)
        ordered.append(node_id)
        for child in children[node_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    return sorted(set(by_id) - set(ordered))


def normalize(
    raw: Any,
    *,
    filename: str = "",
    allow_empty: bool = True,
    require_name: bool = True,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把任意输入归一化成工作流定义。

    返回 ``(definition, errors, warnings)``。错误信息带 ``path``（如
    ``nodes[2].options.domain``），前端可以直接标到对应字段旁边。
    ``definition`` 只在没有错误时非空。
    """
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    if not isinstance(raw, dict):
        errors.append(_issue(i18n.t("workflow.error.root_type")))
        return None, errors, warnings

    for key in raw:
        if key not in _TOP_LEVEL_KEYS:
            warnings.append(
                _issue(
                    i18n.t("workflow.warning.unknown_field", field=str(key)),
                    str(key),
                )
            )

    name = str(raw.get("name") or "").strip()
    if not name and filename:
        # 浏览器一般只给 File.name，但复制粘贴 / 旧接口可能带 Windows 路径，
        # 先把两种分隔符都归一掉，避免把整条路径当成工作流名称。
        base_name = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
        name = Path(base_name).stem.strip()
        if name:
            warnings.append(
                _issue(
                    i18n.t("workflow.warning.name_from_file", name=name),
                    "name",
                )
            )
    if not name:
        if require_name:
            errors.append(_issue(i18n.t("workflow.error.name_required"), "name"))
        name = ""
    if len(name) > MAX_NAME:
        errors.append(
            _issue(
                i18n.t("workflow.error.name_too_long", limit=str(MAX_NAME)),
                "name",
            )
        )

    description = str(raw.get("description") or "").strip()
    if len(description) > MAX_DESCRIPTION:
        errors.append(
            _issue(
                i18n.t("workflow.error.desc_too_long", limit=str(MAX_DESCRIPTION)),
                "description",
            )
        )

    raw_nodes = raw.get("nodes")
    if raw_nodes is None:
        raw_nodes = []
    if not isinstance(raw_nodes, list):
        errors.append(_issue(i18n.t("workflow.error.nodes_type"), "nodes"))
        return None, errors, warnings
    if len(raw_nodes) > MAX_NODES:
        errors.append(
            _issue(
                i18n.t("workflow.error.too_many_nodes", limit=str(MAX_NODES)),
                "nodes",
            )
        )
        return None, errors, warnings
    if not raw_nodes and not allow_empty:
        errors.append(_issue(i18n.t("workflow.error.nodes_required"), "nodes"))

    nodes: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    previous_id = ""
    for index, item in enumerate(raw_nodes):
        node, node_errors, node_warnings = _normalize_node(
            item, index, seen, previous_id, allow_empty
        )
        errors.extend(node_errors)
        warnings.extend(node_warnings)
        if node is None:
            continue
        seen[node["id"]] = index
        previous_id = node["id"]
        nodes.append(node)

    by_id = {node["id"]: node for node in nodes}
    for node in nodes:
        for dep in node.get("needs") or []:
            if dep not in by_id:
                errors.append(
                    _issue(
                        i18n.t("workflow.error.unknown_dep", node=node["id"], dep=dep),
                        f"nodes[{seen.get(node['id'], 0)}].needs",
                    )
                )

    cycle = _has_cycle(nodes)
    if cycle:
        errors.append(
            _issue(i18n.t("workflow.error.cycle", ids=", ".join(cycle)), "nodes")
        )

    if errors:
        return None, errors, warnings
    return {
        "name": name,
        "description": description,
        "nodes": nodes,
    }, errors, warnings


def unique_name(existing: set, name: str) -> str:
    """为导入 / 模板创建生成不冲突的名称：``X`` → ``X (2)`` → ``X (3)``。"""
    base = name.strip() or i18n.t("workflow.default_name")
    if base not in existing:
        return base
    for index in range(2, 1000):
        suffix = f" ({index})"
        candidate = base[: MAX_NAME - len(suffix)] + suffix
        if candidate not in existing:
            return candidate
    return base[: MAX_NAME - 5] + " (999)"


def export_document(definition: Dict[str, Any]) -> Dict[str, Any]:
    """导出时只带用户可编辑的字段，不写内部 id / 时间。"""
    return {
        "name": definition.get("name") or "",
        "description": definition.get("description") or "",
        "nodes": [
            {
                "id": node.get("id"),
                "type": node.get("type"),
                "title": node.get("title") or "",
                "options": copy.deepcopy(node.get("options") or {}),
                "needs": list(node.get("needs") or []),
                "position": copy.deepcopy(node.get("position") or {}),
            }
            for node in definition.get("nodes") or []
        ],
    }


def dump_document(definition: Dict[str, Any], fmt: str) -> str:
    """序列化成可下载的文本。"""
    document = export_document(definition)
    if fmt == "yaml":
        if yaml is None:  # pragma: no cover
            raise RuntimeError("PyYAML is not installed")
        return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    return json.dumps(document, ensure_ascii=False, indent=2)
