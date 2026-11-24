"""入参校验小工具（目前只有邮箱）。

历史写法是 ``if "@" not in email``：`a@`、`@b`、`a@b`、`a b@c` 全都能过，
而邮箱会被写进告警收件人、证书到期提醒和找回密码流程 —— 一个连域名都没有的
字符串只会在发送时炸在 SMTP 层，用户却以为已经设置成功。
"""
from __future__ import annotations

import re
from typing import Any

#: 「基本可用」的邮箱：一个 @、两侧非空且不含空白、域名至少带一个点且以字母结尾。
#: 不追求 RFC 5322 全覆盖（那需要引号 / IP 字面量 / 国际化域名），只挡明显错误。
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)*\.[A-Za-z]{2,}$")

#: 邮件头里不允许出现换行：防止收件人字段被注入额外头部（CWE-93）。
_HEADER_BREAK = ("\r", "\n")


def is_email(value: Any) -> bool:
    """``value`` 是否是一个基本可用的邮箱地址。"""
    text = str(value or "").strip()
    if not text or len(text) > 254:
        return False
    if any(ch in text for ch in _HEADER_BREAK):
        return False
    return bool(EMAIL_RE.match(text))
